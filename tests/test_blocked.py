"""R2.3: a stopped task is sorted into one of three kinds, each with the action that fits."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openfarm_core.blocked import apply_fix_and_retry, propose_fix, triage
from tests.test_store import BODY, _header, _Project

FIXTURES = Path(__file__).parent / "fixtures" / "dogfood-blocked"


def _record(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class TriageTests(unittest.TestCase):
    def test_a_refused_scaffold_means_the_task_is_wrong(self) -> None:
        got = triage(_record("w1-01-scaffold-refused-2"), [_record("w1-01-scaffold-refused-1")])
        self.assertEqual((got["kind"], got["main_label"]), ("task", "Apply fix and retry"))
        self.assertTrue(got["repeated"])
        self.assertIn("the test writer left the allowed paths: src/feed.ts", got["why"])
        self.assertEqual([o["label"] for o in got["others"]], ["Edit task", "Skip this task"])

    def test_touches_that_dont_cover_the_tests_means_the_task_is_wrong(self) -> None:
        got = triage(_record("w1-04-touches"))
        self.assertEqual((got["kind"], got["main"]), ("task", "fix"))
        self.assertIn("`card.touches` lists only `src/client/main.ts`", got["why"])  # the builder's own diagnosis

    def test_a_banned_pattern_on_a_sound_task_is_the_builder_falling_short(self) -> None:
        got = triage(_record("banned-pattern"))
        self.assertEqual((got["kind"], got["main_label"]), ("builder", "Retry"))
        self.assertEqual([o["label"] for o in got["others"]], ["Edit task", "Skip this task"])

    def test_a_revert_check_that_disagrees_may_be_a_wrong_check(self) -> None:
        got = triage(_record("w1-01-revert-check"))
        self.assertEqual((got["kind"], got["main_label"]), ("check", "Run it myself"))
        self.assertEqual([o["label"] for o in got["others"]], ["Accept anyway", "Retry"])

    def test_a_review_whose_answer_was_cut_off_is_not_the_builders_fault(self) -> None:
        got = triage(_record("review-cut-off"))  # from the acceptance run: the reviewer hit its token limit
        self.assertEqual((got["kind"], got["main_label"]), ("check", "Run it myself"))
        self.assertTrue(got["why"].startswith("The reviewer's answer could not be read, so the work was never judged."))
        self.assertNotIn("turned the work down", got["why"])

    def test_the_check_that_really_failed_is_named_when_the_stop_reason_hides_it(self) -> None:
        # from the acceptance run: "planner step 1 failed twice" hid a build rule broken on every try
        import tempfile
        from pathlib import Path

        from openfarm_core.blocked import BUILD_RULES, triage_card

        state = Path(tempfile.mkdtemp())
        run = state / "cards" / "W1-03" / "run-1"
        for attempt, label in (("1", "network-call"), ("2", "dynamic-exec")):
            (run / "gates" / attempt).mkdir(parents=True)
            (run / "gates" / attempt / "banned-patterns.json").write_text(json.dumps({
                "name": "banned-patterns", "result": "fail", "detail": f"1 banned pattern(s) introduced: src/page.ts:147 {label}",
                "evidence": {"banned": [{"file": "src/page.ts", "pattern": label, "line": 147}]}}))
        (run / "card-record.json").write_text(json.dumps({
            "run_id": "run-1", "card_id": "W1-03", "claimed_at": "1", "stop_code": "cannot-split", "attempts": 4,
            "detail": "planner step 1 failed twice"}))
        got = triage_card(state, "W1-03")
        self.assertEqual((got["kind"], got["main_label"]), ("task", "Apply fix and retry"))
        self.assertIn("the code calls the network directly", got["why"])
        self.assertIn("the code builds and runs code at run time", got["why"])
        self.assertIn("calls the network directly", BUILD_RULES)

    def test_the_same_failure_twice_stops_being_the_builders_fault(self) -> None:
        record = _record("banned-pattern")
        self.assertEqual(triage(record, [dict(record)])["kind"], "task")

    def test_release_alone_is_never_the_main_action(self) -> None:
        for path in FIXTURES.glob("*.json"):
            self.assertNotIn(triage(json.loads(path.read_text()))["main"], ("release", ""))


class FixTests(_Project):
    def test_the_fix_is_shown_as_a_diff_before_it_is_saved(self) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"),
                               body=BODY + "- `tally/extra.py` holds the helper.\n")
        fix = propose_fix(self.root, self.cfg, self.store, "W1-01")
        self.assertIn('+touches = ["tally/core.py", "tally/extra.py"]', fix["diff"])
        self.assertIn("added `tally/extra.py`", fix["notes"][0])
        self.assertEqual(self.store.read("W1-01")[0]["touches"], ["tally/core.py"])  # nothing saved yet

    def test_a_fix_that_lets_the_task_use_the_network_says_so_and_the_build_reads_it(self) -> None:
        from openfarm_core.adapters.files import render_card_file

        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        self.assertFalse(next(c for c in self.store.cards() if c.id == "W1-01").network)
        fix = propose_fix(self.root, self.cfg, self.store, "W1-01", why="x", backend=lambda _p: json.dumps(
            {"card": render_card_file(dict(_header("W1-01"), network=True), BODY), "change": "it is the server"}))
        self.assertIn("this task may now use the network", fix["notes"][-1])
        self.assertIn("+network = true", fix["diff"])
        apply_fix_and_retry(self.store, "W1-01", fix)
        self.assertTrue(next(c for c in self.store.cards() if c.id == "W1-01").network)

    def test_a_builder_that_edits_its_frozen_tests_gets_a_retry_not_a_task_fix(self) -> None:
        got = triage({"stop_code": "refused", "detail": "changed: tests/server.test.ts", "attempts": 3})  # acceptance run
        self.assertEqual((got["kind"], got["main_label"]), ("builder", "Retry"))
        self.assertIn("changed the tests it was given", got["why"])

    def test_no_fix_is_said_plainly(self) -> None:
        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        self.assertIsNone(propose_fix(self.root, self.cfg, self.store, "W1-01"))

    def test_the_architects_fix_carries_the_builders_diagnosis(self) -> None:
        from openfarm_core.adapters.files import render_card_file

        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        asked = []

        def architect(prompt: str) -> str:
            asked.append(prompt)
            return json.dumps({"card": render_card_file(_header("W1-01", title="Count, smaller"), BODY),
                               "change": "cut the scope to one function"})

        fix = propose_fix(self.root, self.cfg, self.store, "W1-01", why="tests import a module the task may not write",
                          backend=architect)
        self.assertIn("tests import a module the task may not write", asked[0])
        self.assertIn("the Architect rewrote it: cut the scope to one function", fix["notes"][0])
        self.assertIn('+title = "Count, smaller"', fix["diff"])

    def test_the_architects_fix_takes_the_plan_checks_own_repairs_before_it_is_shown(self) -> None:
        # from the acceptance run: a rewrite that failed the plan checks was saved and the task parked
        from openfarm_core.adapters.files import render_card_file

        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        asked = []

        def architect(prompt: str) -> str:
            asked.append(prompt)
            return json.dumps({"card": render_card_file(_header("W1-01"), BODY + "- `tally/extra.py` holds a helper.\n"),
                               "change": "moved the helper out"})

        fix = propose_fix(self.root, self.cfg, self.store, "W1-01", why="x", backend=architect)
        self.assertIn("Files no task may write", asked[0])
        self.assertEqual(fix["header"]["touches"], ["tally/core.py", "tally/extra.py"])
        self.assertTrue(any("added `tally/extra.py`" in note for note in fix["notes"][1:]))


if __name__ == "__main__":
    unittest.main()
