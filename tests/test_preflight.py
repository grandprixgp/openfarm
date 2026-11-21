"""R2.2: the plan preflight, run on the four cards the dogfood run's Architect really wrote."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openfarm_core.adapters.files import parse_card_file
from openfarm_core.preflight import apply_fixes, check_card
from openfarm_core.store import card_from

FIXTURES = Path(__file__).parent / "fixtures" / "dogfood-w1"

# What the test project held when the cards were drafted. The split had made
# placeholders for the client modules.
REAL_FILES = ("src/server.ts", "src/index.ts", "src/invariants.ts", "src/shared/palette.ts",
              "src/shared/theme.ts", "tests/slice.test.ts", "tests/baseline.test.ts")
PLACEHOLDERS = ("src/feed.ts", "src/proxy.ts", "src/router.ts", "src/client/main.ts",
                "src/client/sample.ts", "src/client/apply.ts")


def _cfg():
    return SimpleNamespace(
        languages=["typescript"], never_touch=["src/invariants.ts"], forbidden_files=["**/package.json"],
        categories=["small-feature"], require_reference=["small-feature"], refuse_touches_prefixes=[],
        known_failing=[])


class DogfoodCardsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for rel in REAL_FILES:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("export const real = 1;\n")
        for rel in PLACEHOLDERS:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("// Placeholder module for card W1 — implemented by openfarm worker.\nexport {};\n")
        self.raw = {p.stem: parse_card_file(p.read_text()) for p in sorted(FIXTURES.glob("W1-*.md"))}
        self.cards = {cid: card_from(header, body) for cid, (header, body) in self.raw.items()}

    def _checks(self, cid: str) -> dict[str, dict]:
        found = check_card(self.cards[cid], _cfg(), self.root, list(self.cards.values()))
        return {c["check"]: c for c in found if c["check"] != "rules"}

    def test_the_parameter_default_is_caught_and_repaired_in_the_open(self) -> None:
        scaffold = self._checks("W1-01")["scaffold"]
        self.assertEqual(scaffold["status"], "fail")
        self.assertIn("`fetchDay` can't be scaffolded: a parameter default", scaffold["detail"])

        header, body, notes = apply_fixes(*self.raw["W1-01"], [scaffold])
        self.assertIn("fetchImpl?: typeof fetch): Promise<DayImage | null>` (`fetchImpl` is `fetch` when it is left out)", body)
        self.assertEqual(len(notes), 1)
        self.assertIn("made `fetchImpl` optional", notes[0])
        again = check_card(card_from(header, body), _cfg(), self.root, list(self.cards.values()))
        self.assertEqual(next(c for c in again if c["check"] == "scaffold")["status"], "pass")

    def test_a_version_string_is_not_a_file(self) -> None:
        # the v0.20 live run: a user-agent `onthisday/0.1` was added to the files a task may write
        from types import SimpleNamespace

        from openfarm_core.preflight import named_files

        card = SimpleNamespace(done_text="Send `onthisday/0.1` as the User-Agent from `src/wiki.ts`, see `docs/v2/notes.md`.")
        self.assertEqual(named_files(card), ["src/wiki.ts", "docs/v2/notes.md"])

    def test_the_files_the_text_names_must_be_in_touches(self) -> None:
        touches = self._checks("W1-04")["touches"]
        self.assertEqual(touches["status"], "fail")
        for rel in ("src/client/sample.ts", "src/client/apply.ts", "src/client/style.css"):
            self.assertIn(rel, touches["detail"])
        header, _body, notes = apply_fixes(*self.raw["W1-04"], [touches])
        self.assertEqual(header["touches"], ["src/client/main.ts", "src/client/sample.ts",
                                             "src/client/apply.ts", "src/client/style.css"])
        self.assertIn("added", notes[0])

    def test_the_seam_between_the_page_and_its_stylesheet_is_flagged(self) -> None:
        seams = self._checks("W1-04")["seams"]
        self.assertEqual(seams["status"], "fail")
        self.assertIn("W1-03 serves the page with `#art`, `#date`, `#title`, `#story`, `#credit`", seams["detail"])
        self.assertIn("never names `#date`, `#title`, `#story`, `#credit`", seams["detail"])
        self.assertNotIn("fix", seams)  # only the Architect or a person can settle this one

    def test_sound_cards_pass(self) -> None:
        for cid in ("W1-02", "W1-03"):
            for name, check in self._checks(cid).items():
                if (cid, name) != ("W1-03", "network"):  # the server card: see the network test below
                    self.assertEqual(check["status"], "pass", f"{cid} {name}: {check['detail']}")

    def _one(self, header_changes: dict, body: str = "") -> tuple[dict, str]:
        header, old_body = self.raw["W1-02"]
        return dict(header, **header_changes), body or old_body

    def _repaired(self, header: dict, body: str, check: str) -> tuple[dict, dict, list[str]]:
        """One check's verdict on a card, and the card after the checks' own repairs."""
        from openfarm_core.preflight import apply_fixes

        found = check_card(card_from(header, body), _cfg(), self.root, [])
        fixed, new_body, notes = apply_fixes(header, body, found)
        again = {c["check"]: c for c in check_card(card_from(fixed, new_body), _cfg(), self.root, [])}
        self.assertEqual(again[check]["status"], "pass", again[check]["detail"])
        return {c["check"]: c for c in found}[check], fixed, notes

    def test_a_task_that_needs_the_network_is_caught_before_the_build_and_marked(self) -> None:
        # the acceptance run: the server task stopped in the build, because nothing said it may use the network
        header, body = self.raw["W1-03"]
        self.assertNotIn("network", header)
        verdict, fixed, notes = self._repaired(header, body, "network")
        self.assertEqual(verdict["status"], "fail")
        self.assertIn("not marked `network = true`", verdict["detail"])
        self.assertIs(fixed["network"], True)
        self.assertIn("marked as allowed to use the network", notes[-1])
        quiet = self._checks("W1-02")["network"]  # a pure helper: nothing to mark
        self.assertEqual(quiet["status"], "pass")

    def test_a_listed_test_that_is_in_the_project_already_is_taken_off_the_list(self) -> None:
        # the acceptance run: a guard test passed before any code was written, and the build wants a fail first
        (self.root / "tests/slice.test.ts").write_text("test('answers 400 for a missing src', () => {});\n")
        tests = [{"file": "tests/slice.test.ts", "name": "answers 400 for a missing src", "asserts": "status is 400"},
                 {"file": "tests/new.test.ts", "name": "builds the url from imageUrl", "asserts": "url has src"}]
        header, body = self._one({"tests": tests})
        verdict, fixed, notes = self._repaired(header, body, "guards")
        self.assertIn("is in the project already and passes", verdict["detail"])
        self.assertEqual([t["name"] for t in fixed["tests"]], ["builds the url from imageUrl"])
        self.assertIn("it still has to keep passing", notes[-1])
        only = check_card(card_from(dict(header, tests=tests[:1]), body), _cfg(), self.root, [])
        guards = {c["check"]: c for c in only}["guards"]
        self.assertEqual(guards["status"], "fail")
        self.assertNotIn("fix", guards)  # nothing would be left to prove the task: a person or the Architect decides
        allowed = check_card(card_from(dict(header, test_edits=["tests/slice.test.ts"]), body), _cfg(), self.root, [])
        self.assertEqual({c["check"]: c for c in allowed}["guards"]["status"], "pass")  # the task says it changes that test

    def test_a_task_that_changes_what_an_existing_test_checks_gets_leave_to_change_that_test(self) -> None:
        # the acceptance run: `id="hero"` moved off the image, an existing test checked it, and Retry could never work
        (self.root / "tests/page.test.ts").write_text("assert.match(html, /<img id=\"hero\"/);\n")
        header, body = self._one({}, "## Done means\n- The hero id moves: `#hero` is removed from the image and put on "
                                     "the new `<section>`.\n- `src/shared/palette.ts` keeps its exports.\n")
        verdict, fixed, notes = self._repaired(header, body, "frozen")
        self.assertIn("which the existing test `tests/page.test.ts` checks", verdict["detail"])
        self.assertEqual(fixed["test_edits"], ["tests/page.test.ts"])
        self.assertIn("tests/page.test.ts", fixed["touches"])
        self.assertIn("may change the existing test `tests/page.test.ts`", notes[-1])
        adds, _fixed, _notes = self._repaired(*self._one({}, "## Done means\n- Adds `#plate` beside `#hero`.\n"), "frozen")
        self.assertEqual(adds["status"], "pass")  # adding beside it contradicts nothing
        self.assertEqual(self._checks("W1-01")["touches"]["status"], "pass")
        self.assertEqual(self._checks("W1-01")["seams"]["status"], "pass")

    def test_dependencies_must_exist_be_buildable_and_not_loop(self) -> None:
        cards = dict(self.cards)
        del cards["W1-02"]
        checks = {c["check"]: c for c in check_card(cards["W1-04"], _cfg(), self.root, list(cards.values()))}
        self.assertIn("waits for W1-02, which doesn't exist", checks["depends"]["detail"])

        self.cards["W1-03"].state = "dropped"
        self.assertIn("W1-03, which was dropped", self._checks("W1-04")["depends"]["detail"])
        self.cards["W1-03"].state = "ready"
        self.cards["W1-01"].deps.append("W1-04")
        self.assertIn("waits for itself", self._checks("W1-04")["depends"]["detail"])

    def test_a_person_can_keep_a_card_as_written(self) -> None:
        self.cards["W1-04"].fields["preflight_skip"] = ["seams"]
        seams = self._checks("W1-04")["seams"]
        self.assertEqual(seams["status"], "skipped")
        self.assertIn("kept as written", seams["detail"])


if __name__ == "__main__":
    unittest.main()


class PlanTests(unittest.TestCase):
    """The same four cards through the whole plan step: repaired in the open, or parked with the reason."""

    def setUp(self) -> None:
        import os
        import subprocess
        from unittest import mock

        from openfarm_core.bootstrap import initialize_project
        from openfarm_core.config import load_config
        from openfarm_core.store import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(base / "projects.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = base / "iotd"
        initialize_project("iotd", directory=self.root, idea="image of the day", language="typescript",
                           state_dir=base / "state", secrets_dir=base / "secrets", commit=True)
        for rel in ("src/server.ts", "src/shared/palette.ts", "src/shared/theme.ts", "tests/slice.test.ts"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("export const real = 1;\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "slice"], check=True)
        self.cfg = load_config(self.root)
        self.store = Store(self.root, self.cfg)
        for path in sorted(FIXTURES.glob("W1-*.md")):
            header, body = parse_card_file(path.read_text())
            self.store.change_card(path.stem, "create", by="architect", header=header, body=body)

    def _plan(self, backend=None) -> dict:
        from openfarm_core.automode import preflight_plan

        plan = preflight_plan(self.root, self.cfg, self.store, backend=backend)
        return {entry["id"]: entry for entry in plan["cards"]} | {"_checks": plan["plan_checks"]}

    def test_fixable_faults_are_repaired_and_shown_and_the_seam_keeps_its_card_parked(self) -> None:
        plan = self._plan()
        self.assertTrue(any("made `fetchImpl` optional" in note for note in plan["W1-01"]["repairs"]))
        self.assertIn("fetchImpl?: typeof fetch", self.store.read("W1-01")[1])
        self.assertTrue(any("added `src/client/sample.ts`" in note for note in plan["W1-04"]["repairs"]))
        self.assertIn("src/client/style.css", self.store.read("W1-04")[0]["touches"])

        self.assertFalse(plan["W1-04"]["eligible"])  # nobody but the Architect or a person can settle the seam
        self.assertIn("seams", [f["rule"] for f in plan["W1-04"]["failures"]])
        self.assertEqual(self.store.read("W1-04")[0]["state"], "backlog")
        self.assertTrue(any(reason.startswith("seams:") for reason in self.store.read("W1-04")[0]["preflight"]))
        self.assertIn({"check": "seams", "status": "fail"}, plan["_checks"])
        self.assertIn({"check": "scaffold", "status": "pass"}, plan["_checks"])
        # the repairs are in the cards' history too, with who made them
        self.assertEqual(self.store.history("W1-01")[-1]["by"], "openfarm/preflight")
        self.assertNotIn("W1-04", self.store.src.next()["cards"])

    def test_the_architect_repairs_the_seam_in_the_same_pass(self) -> None:
        import json

        from openfarm_core.adapters.files import render_card_file

        asked: list[str] = []

        def architect(prompt: str) -> str:
            asked.append(prompt)
            header, body = self.store.read("W1-04")
            body = body.replace("  - the text card uses",
                                "  - `#date`, `#title`, `#story` and `#credit` sit in the text card, which uses")
            return json.dumps({"cards": {"W1-04": render_card_file(header, body)},
                               "changes": {"W1-04": "the stylesheet now names the page's four text ids"}})

        plan = self._plan(architect)
        self.assertEqual(len(asked), 1)
        self.assertIn("W1-04 fails these checks", asked[0])
        self.assertIn("seams:", asked[0])
        self.assertNotIn("### W1-02 fails", asked[0])
        self.assertTrue(plan["W1-04"]["eligible"], plan["W1-04"]["failures"])
        self.assertTrue(any("the Architect rewrote it: the stylesheet now names" in n for n in plan["W1-04"]["repairs"]))
        self.assertEqual(self.store.read("W1-04")[0]["state"], "ready")
