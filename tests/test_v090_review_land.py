"""v0.9.0: openfarm review and openfarm land (local-only serial merge queue)."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_python_e2e import build_python_toy, run_cli, script, GOOD_TEST, GOOD_IMPLEMENTATION


class ReviewAndLandTests(unittest.TestCase):
    def test_review_and_land_two_cards_serially_locally(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)

        # Card 1: existing bounded bugfix
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.",
        )
        # Card 2: independent helper file
        (toy.root / "tally" / "extra.py").write_text("def double(x: int) -> int:\n    return x\n")
        toy.write_card(
            "CARD-2", title="Double returns twice x", category="pinned-bugfix",
            unit=".", touches=["tally/extra.py"],
            tests=[{"name": "test_double_works", "file": "tests/test_extra.py"}],
            done_text="`double(3) == 6`.",
        )
        toy.commit("add CARD-1 and CARD-2")

        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push",
            "CARD-1/checker_pass2": "VERDICT: accept\nREASONS: ok\nTRACEABILITY:\n- Done -> test\nEDGE_CASES: none\n",
            "CARD-2/test_writer": (
                "=== whole_file: tests/test_extra.py\n"
                "import unittest\nfrom tally.extra import double\n"
                "class T(unittest.TestCase):\n"
                "    def test_double_works(self):\n"
                "        self.assertEqual(double(3), 6)\n"
            ),
            "CARD-2/implementer": (
                "=== search_replace: tally/extra.py\n"
                "<<< SEARCH\n    return x\n>>> REPLACE\n    return x * 2\n"
            ),
            "CARD-2/checker_pass1": "RESTATEMENT: double returns twice x",
            "CARD-2/checker_pass2": "VERDICT: accept\nREASONS: ok\nTRACEABILITY:\n- Done -> test\nEDGE_CASES: none\n",
        })
        res = run_cli(toy.root, "run", "--script", str(sp), "--cards", "2")
        outcomes = json.loads(res.stdout)["outcomes"]
        self.assertEqual([o["final_state"] for o in outcomes], ["in-review", "in-review"], outcomes)

        # 1. openfarm review --json --verdict clean
        rev = run_cli(toy.root, "review", "--verdict", "clean", "--note", "looks good")
        rev_data = json.loads(rev.stdout)
        self.assertEqual(rev_data["count"], 2)
        self.assertTrue(all(c["verdict"] == "clean" for c in rev_data["cards"]))

        # 2. openfarm land --dry-run does not advance main
        head_before = subprocess.run(
            ["git", "rev-parse", "main"], cwd=toy.root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dry = run_cli(toy.root, "land", "CARD-1", "CARD-2", "--dry-run")
        self.assertEqual(dry.returncode, 0, f"stdout={dry.stdout!r} stderr={dry.stderr!r}")
        dry_data = json.loads(dry.stdout)
        self.assertEqual(dry_data["count"], 2)
        self.assertTrue(dry_data["dry_run"])
        head_after_dry = subprocess.run(
            ["git", "rev-parse", "main"], cwd=toy.root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(head_before, head_after_dry)

        # 3. openfarm land merges both cards serially with --no-ff and marks done
        land = run_cli(toy.root, "land", "CARD-1", "CARD-2")
        land_data = json.loads(land.stdout)
        self.assertEqual(land_data["count"], 2)
        self.assertTrue(all(item["merged"] for item in land_data["landed"]))
        head_after = subprocess.run(
            ["git", "rev-parse", "main"], cwd=toy.root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertNotEqual(head_before, head_after)
        # Verify both fixes are now on main
        self.assertIn("x * 2", (toy.root / "tally" / "extra.py").read_text())
        self.assertIn("FullError", (toy.root / "tally" / "bounded.py").read_text())


if __name__ == "__main__":
    unittest.main()
