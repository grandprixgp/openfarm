"""`openfarm done`: the reviewer marks a merged card done, so dependents can run."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_python_e2e import (GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy,
                                   run_cli, script)


class DoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        self.toy.commit("tally: CARD-1")

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.toy.root, check=True,
                              capture_output=True, text=True).stdout

    def _done(self) -> tuple[int, dict]:
        result = run_cli(self.toy.root, "done", "CARD-1")
        return result.returncode, json.loads(result.stdout)

    def _card_state(self) -> str:
        cards = self.toy.state_dir / "cards-source" / "cards" / "CARD-1.md"
        line = next(l for l in cards.read_text().splitlines() if l.startswith("state"))
        return line.split("=", 1)[1].strip().strip('"')

    def test_a_card_is_done_only_after_its_branch_is_merged(self) -> None:
        # Not run yet: nothing to mark.
        code, payload = self._done()
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["done"])

        path = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"})
        run = run_cli(self.toy.root, "run", "--card", "CARD-1", "--script", str(path),
                      "--cards", "1")
        outcome = json.loads(run.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", run.stdout)

        code, payload = self._done()
        self.assertNotEqual(code, 0)
        self.assertIn("not merged into main", payload["reason"])
        self.assertEqual(self._card_state(), "in_review")

        branch = self._git("branch", "--list", "openfarm/CARD-1*",
                           "--format=%(refname:short)").split()[0]
        self._git("merge", "--no-ff", "-q", "-m", "merge CARD-1", branch)
        code, payload = self._done()
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["done"])
        self.assertEqual(self._card_state(), "done")

        # Done is final: a second call is refused, not repeated.
        code, payload = self._done()
        self.assertNotEqual(code, 0)
        self.assertIn("not in review", payload["reason"])


if __name__ == "__main__":
    unittest.main()
