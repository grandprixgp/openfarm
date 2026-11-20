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
                self.assertEqual(check["status"], "pass", f"{cid} {name}: {check['detail']}")
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
