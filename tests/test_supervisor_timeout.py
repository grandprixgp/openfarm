"""A supervisor slow to plan costs a card its plan, not its run."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_python_e2e import (GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy,
                                   run_cli, script)

TIMEOUT = {"fail": True, "kind": "infra", "code": "acp-timeout",
           "message": "ACP prompt turn timed out after 600.0s"}


class SupervisorTimeoutTests(unittest.TestCase):
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

    def _run(self, shape: dict) -> dict:
        model = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: a full bundle refuses a push",
            "CARD-1/checker_pass2": "{}"})
        mm = script(**{"CARD-1/shape_and_plan": shape,
                       "CARD-1/scaffold_contract": TIMEOUT,
                       "CARD-1/pre_review": {"verdict": "approve", "reviewer_brief": "ok",
                                             "missed_clause": "", "findings": []}})
        result = run_cli(self.toy.root, "run", "--card", "CARD-1", "--cards", "1",
                         "--script", str(model), "--mm-script", str(mm))
        return json.loads(result.stdout)

    def test_a_timed_out_plan_degrades_and_the_card_goes_on(self) -> None:
        payload = self._run(TIMEOUT)
        outcome = payload["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        status = json.loads(run_cli(self.toy.root, "status").stdout)
        self.assertFalse(status["paused"])

    def test_other_supervisor_infra_still_stops_the_card(self) -> None:
        payload = self._run({"fail": True, "kind": "infra", "code": "acp-exit",
                             "message": "the adapter died"})
        outcome = payload["outcomes"][0]
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "infra")


if __name__ == "__main__":
    unittest.main()
