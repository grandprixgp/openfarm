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

    def test_a_refused_stub_is_named_as_the_cause_and_retry_is_offered(self) -> None:
        # v0.20 live run: "the test writer left the allowed paths" was shown as "the task is wrong", with only the
        # Architect to ask; the reviewer's stub had been refused and nothing said so
        import tempfile
        from pathlib import Path

        from openfarm_core.blocked import triage_card

        state = Path(tempfile.mkdtemp())
        for run in ("run-1", "run-2"):
            gates = state / "cards" / "W1-03" / run / "gates" / "1"
            gates.mkdir(parents=True)
            (gates / "micromanager-scaffold.json").write_text(json.dumps({
                "name": "micromanager-scaffold", "result": "fail",
                "detail": "scaffold_contract degraded (Refused): src/render.ts: a TypeScript scaffold may only declare: imports "
                          "and functions whose whole body is one `throw new Error(...)` (a body that is not exactly one throw "
                          "new Error(...))"}))
            (gates / "fail-before.json").write_text(json.dumps({
                "name": "fail-before", "result": "fail", "detail": "on base the frozen tests do not compile"}))
            (state / "cards" / "W1-03" / run / "card-record.json").write_text(json.dumps({
                "run_id": run, "card_id": "W1-03", "claimed_at": run, "stop_code": "refused", "attempts": 2,
                "detail": "the test writer left the allowed paths: src/render.ts"}))
        got = triage_card(state, "W1-03")
        self.assertEqual((got["kind"], got["title"], got["main_label"]), ("builder", "The starting stub was refused", "Retry"))
        self.assertIn("What was wrong with the stub: a body that is not exactly one throw new Error(...)", got["why"])
        self.assertIn("The task itself may be fine", got["why"])
        self.assertTrue(got["repeated"])

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

    def test_a_listed_test_that_already_passes_is_taken_off_the_list_as_the_fix(self) -> None:
        # from the acceptance run: a fix task listed a regression guard, which can never fail first
        from pathlib import Path

        from openfarm_core.blocked import triage_card

        header = _header("W1-01")
        header["tests"] = [*header["tests"], {"name": "never hardcodes undefined", "file": header["tests"][0]["file"],
                                              "asserts": "the page has no src=undefined"}]
        self.store.change_card("W1-01", "create", header=header, body=BODY)
        run = Path(self.cfg.state_dir) / "cards" / "W1-01" / "run-1"
        (run / "gates" / "1").mkdir(parents=True)
        (run / "gates" / "1" / "fail-before.json").write_text(json.dumps({
            "name": "fail-before", "result": "fail", "evidence": {"passing_on_base": ["never hardcodes undefined"]}}))
        (run / "card-record.json").write_text(json.dumps({
            "run_id": "run-1", "card_id": "W1-01", "claimed_at": "1", "stop_code": "tests-unprovable", "detail": "x"}))
        got = triage_card(Path(self.cfg.state_dir), "W1-01")
        self.assertEqual((got["kind"], got["main_label"]), ("task", "Apply fix and retry"))
        self.assertIn("never hardcodes undefined", got["why"])
        fix = propose_fix(self.root, self.cfg, self.store, "W1-01")
        self.assertEqual([t["name"] for t in fix["header"]["tests"]], [t["name"] for t in _header("W1-01")["tests"]])
        self.assertIn("stays in the test file as a guard", fix["notes"][-1])

    def test_a_task_that_keeps_breaking_an_older_test_is_a_wrong_task(self) -> None:
        # from the acceptance run: the task moved an id an existing test checks; "Retry" could never work
        import tempfile
        from pathlib import Path

        from openfarm_core.blocked import triage_card

        state = Path(tempfile.mkdtemp())
        run = state / "cards" / "W1-09" / "run-1"
        (run / "gates" / "1").mkdir(parents=True)
        (run / "gates" / "1" / "suite.json").write_text(json.dumps({
            "name": "suite", "result": "fail", "detail": "1 red test(s) not on known_failing: page has hero structure"}))
        (run / "card-record.json").write_text(json.dumps({
            "run_id": "run-1", "card_id": "W1-09", "claimed_at": "1", "stop_code": "no-progress", "attempts": 3,
            "detail": "the same error signature came back twice"}))
        got = triage_card(state, "W1-09")
        self.assertEqual((got["kind"], got["main_label"]), ("task", "Apply fix and retry"))
        self.assertIn("breaks a test that was already there", got["why"])
        self.assertIn("page has hero structure", got["why"])

    def test_a_task_the_builder_gave_back_is_stopped_until_it_changes_and_the_fix_sees_the_code(self) -> None:
        # from the acceptance run: a rewrite made without the code was handed back, and "Build 1 task" was all the screen offered
        from pathlib import Path

        from openfarm_core.adapters.files import render_card_file
        from openfarm_core.blocked import triage_card
        from openfarm_core.store import card_from
        from openfarm_tui.journey_state import _task

        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        run = Path(self.cfg.state_dir) / "cards" / "W1-01" / "run-1"
        run.mkdir(parents=True)
        (run / "card-record.json").write_text(json.dumps({
            "run_id": "run-1", "card_id": "W1-01", "claimed_at": "1", "ended_at": "2026-01-01T00:00:00Z",
            "final_state": "released", "stop_code": "ineligible", "detail": "the hero is an img, it can't hold a plate"}))

        def shown() -> dict:
            header, body = self.store.read("W1-01")
            return _task(card_from(header, body), {}, Path(self.cfg.state_dir), header)

        self.assertEqual(shown()["state"], "blocked")
        self.assertIn("gave it back", shown()["triage"]["why"])
        self.assertEqual(triage_card(Path(self.cfg.state_dir), "W1-01")["main_label"], "Apply fix and retry")
        asked = []
        fix = propose_fix(self.root, self.cfg, self.store, "W1-01", why="x", backend=lambda prompt: asked.append(prompt) or json.dumps(
            {"card": render_card_file(_header("W1-01", title="Count, with the code in view"), BODY), "change": "fits the code"}))
        self.assertIn("### tally/core.py (as it is now)", asked[0])
        self.assertEqual(apply_fix_and_retry(self.store, "W1-01", fix)["state"], "ready")  # no retry needed: it was never blocked
        self.assertEqual(shown()["state"], "ready")

    def _question(self, title: str, body: str = "") -> dict:
        from openfarm_core.findings import list_findings

        self.store.change_card("W1-01", "create", header=_header("W1-01"), body=BODY)
        header, text = self.store.read("W1-01")
        self.store.src._write("W1-01", dict(header, state="blocked"), text, "W1-01: blocked")
        self.store.src._write("FIND-1", {"id": "FIND-1", "title": title, "state": "backlog", "category": "finding",
                                         "from_card": "W1-01"}, body, "FIND-1: asked")
        return list_findings(self.root, self.cfg)["findings"][0]

    def _waiting(self) -> list[dict]:
        from openfarm_core.findings import list_findings

        return list_findings(self.root, self.cfg, state_filter="waiting")["findings"]

    def test_a_question_the_architect_can_answer_never_reaches_the_owner(self) -> None:
        # from the acceptance run: "regex case sensitivity differs from sizedImageUrl" was shown as a question for the owner
        from openfarm_core.blocked import settle_question

        finding = self._question("Should the count ignore case?", "The task says count words; the helper lowercases.")
        asked = []
        got = settle_question(self.root, self.cfg, self.store, finding, backend=lambda prompt: asked.append(prompt) or json.dumps(
            {"for": "architect", "answer": "Yes: lowercase first, as the existing helper does."}))
        self.assertEqual(got["for"], "architect")
        self.assertIn("Should the count ignore case?", asked[0])
        self.assertIn("The owner is asked ONLY for a choice about what the product should do", asked[0])
        header, body = self.store.read("W1-01")
        self.assertIn("## Answer from the Architect", body)
        self.assertIn("lowercase first", body)
        self.assertNotEqual(header["state"], "blocked")  # back in the queue
        self.assertEqual(self._waiting(), [])

    def test_a_product_choice_is_left_for_the_owner_and_asked_once(self) -> None:
        from openfarm_core.blocked import settle_question

        finding = self._question("Should deleted words still be counted?")
        got = settle_question(self.root, self.cfg, self.store, finding, backend=lambda prompt: json.dumps({"for": "owner", "answer": ""}))
        self.assertEqual(got["for"], "owner")
        self.assertEqual([f["audience"] for f in self._waiting()], ["owner"])
        self.assertEqual(self.store.read("W1-01")[0]["state"], "blocked")
        self.assertNotIn("Answer from", self.store.read("W1-01")[1])

    def test_an_answer_does_not_rebuild_a_task_that_stopped_the_same_way_twice(self) -> None:
        # v0.20 live run: a task stopped three times for one reason while each try's question was answered and retried
        from openfarm_core.blocked import settle_question

        def stopped(run: str, detail: str) -> None:
            folder = Path(self.cfg.state_dir) / "cards" / "W1-01" / run
            folder.mkdir(parents=True)
            (folder / "card-record.json").write_text(json.dumps(
                {"card_id": "W1-01", "claimed_at": run, "final_state": "blocked", "stop_code": "refused", "detail": detail}))

        asked = []

        def answered(title: str) -> dict:
            from openfarm_core.findings import list_findings

            if not asked:
                finding = self._question(title, "It is not said.")
            else:
                fid = f"FIND-{len(asked) + 1}"
                self.store.src._write(fid, {"id": fid, "title": title, "state": "backlog", "category": "finding",
                                            "from_card": "W1-01"}, "It is not said.", f"{fid}: asked")
                finding = next(f for f in list_findings(self.root, self.cfg)["findings"] if f["id"] == fid)
            asked.append(title)
            return settle_question(self.root, self.cfg, self.store, finding,
                                   backend=lambda prompt: json.dumps({"for": "architect", "answer": "Do it the plain way."}))

        stopped("run-1", "the test writer left the allowed paths: src/select.ts")
        first = answered("Which way round?")
        self.assertEqual(self.store.read("W1-01")[0]["state"], "ready")
        self.assertTrue(first["retried"])  # one stop: the answer may be what was missing
        header, text = self.store.read("W1-01")
        self.store.src._write("W1-01", dict(header, state="blocked"), text, "W1-01: blocked again")
        stopped("run-2", "the test writer left the allowed paths: src/select.ts")
        second = answered("And the other thing?")
        self.assertEqual((second["for"], second["retried"], second["stuck"]), ("architect", False, True))
        self.assertFalse(first["stuck"])
        header, body = self.store.read("W1-01")
        self.assertEqual(header["state"], "blocked")  # it waits for a person now
        self.assertEqual(body.count("## Answer from the Architect"), 2)  # the answer is still kept
        self.assertEqual(self._waiting(), [])
        stopped("run-3", "the reviewer turned the work down")
        third = answered("A third thing?")  # three stops, even if the last is a new kind: no more tries by itself
        self.assertEqual((third["retried"], third["stuck"]), (False, True))

    def test_a_second_stop_of_a_different_kind_is_still_tried_again(self) -> None:
        from openfarm_core.blocked import settle_question

        for run, detail in (("run-1", "the test writer left the allowed paths"), ("run-2", "2 counter-tests fail")):
            folder = Path(self.cfg.state_dir) / "cards" / "W1-01" / run
            folder.mkdir(parents=True)
            (folder / "card-record.json").write_text(json.dumps(
                {"card_id": "W1-01", "claimed_at": run, "final_state": "blocked", "stop_code": "refused", "detail": detail}))
        got = settle_question(self.root, self.cfg, self.store, self._question("Which way?", "Not said."),
                              backend=lambda prompt: json.dumps({"for": "architect", "answer": "The plain way."}))
        self.assertEqual((got["retried"], got["stuck"]), (True, False))  # a different stop is a new situation

    def test_with_no_architect_the_question_goes_to_the_owner(self) -> None:
        from openfarm_core.blocked import settle_question

        def down(prompt):
            raise TimeoutError("no answer")

        for backend in (None, down, lambda prompt: "not json at all"):
            finding = self._question("Which separator should the output use?") if backend is None else self._waiting()[0]
            self.assertEqual(settle_question(self.root, self.cfg, self.store, finding, backend=backend)["for"], "owner")
        self.assertEqual([f["audience"] for f in self._waiting()], ["owner"])

    def test_a_note_about_the_tooling_is_filed_and_nobody_is_asked(self) -> None:
        # from the acceptance run: "Harness cues are inconsistent with this card" was shown as a question to answer
        from openfarm_core.blocked import settle_question

        finding = self._question("Harness cues are inconsistent with this card", "Cue 3 says X, the card says Y.")
        got = settle_question(self.root, self.cfg, self.store, finding, backend=lambda prompt: self.fail("nobody is asked"))
        self.assertEqual(got["for"], "nobody")
        self.assertEqual(self._waiting(), [])
        self.assertEqual(self.store.read("W1-01")[0]["state"], "blocked")  # it stopped for its own reason: that still shows

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
