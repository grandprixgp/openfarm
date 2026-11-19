"""v0.9.1 review dossier summary, Go raw-string mutation, and cleanup tests."""

from __future__ import annotations

import json
import unittest

from openfarm_core import mutate
from tests.test_python_e2e import (
    GOOD_IMPLEMENTATION,
    GOOD_TEST,
    build_python_toy,
    run_cli,
    script,
)


class ReviewAndCleanupTests(unittest.TestCase):
    def test_go_mutants_handles_raw_backtick_strings_with_backslash(self) -> None:
        src = 'package p\n\nfunc F(x int) bool {\n\ts := `\\` + "ok"\n\treturn x > 0\n}\n'
        mutants = mutate.go_mutants(src, {5})
        self.assertTrue(any(">" in m["what"] for m in mutants))
        # Line 4 raw backtick string with trailing backslash must NOT swallow line 5!
        self.assertEqual(len([m for m in mutants if m["line"] == 5]), 1)

    def test_review_json_includes_gates_and_findings(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.",
        )
        toy.commit("add CARD-1")
        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full raises FullError",
            "CARD-1/checker_pass2": "VERDICT: accept\nREASONS: ok\nTRACEABILITY:\n- Done -> test\nEDGE_CASES: none\n",
        })
        run_cli(toy.root, "run", "--script", str(sp), "--cards", "1")
        rev = run_cli(toy.root, "review")
        data = json.loads(rev.stdout)
        self.assertEqual(data["count"], 1)
        card = data["cards"][0]
        self.assertIn("gates", card)
        self.assertGreater(card["gates"]["pass"], 0)
        self.assertIn("findings", card)


if __name__ == "__main__":
    unittest.main()
