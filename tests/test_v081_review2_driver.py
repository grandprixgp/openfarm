"""Second outside review, 8 to 10: spend that went uncounted, corrections with no task.

8: sessions of parallel cards share one ACP process, and one running total for
all of them lost the smaller session's cost. 9: a driver turn that failed but
reported what it cost was booked as nothing. 10: a correction turn is a fresh CLI
call, and it was sent only the complaint.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import driver
from openfarm_core.adapters.acp import ClaudeAcpAdapter
from openfarm_core.driver import DriverAgent, DriverResponse, _correction_prompt, _parse_driver_output
from openfarm_core.errors import Infra, Refused
from tests.test_acp_micromanager import _stand_in_cmd
from tests.test_v081b_drive_wave import _WaveFixture
from tests.test_v081c_drive2_spend import DriveSpendTotalTests, _workers


def _cost_turn(amount: float) -> dict:
    return {"text": "{}", "usage_updates": [
        {"used": 100, "size": 200000, "cost": {"amount": amount, "currency": "USD"}}]}


class CostPerSessionTests(unittest.TestCase):
    def test_interleaved_sessions_each_count_from_their_own_total(self) -> None:
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="of-v081-r2-cost-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        script = tmp / "turns.json"
        # A: 2 then 3. B: 10 then 11. Asked in the order A, B, A, B.
        script.write_text(json.dumps({"turns": [
            _cost_turn(2.0), _cost_turn(10.0), _cost_turn(3.0), _cost_turn(11.0)]}))
        with ClaudeAcpAdapter(
                _stand_in_cmd("--mode", "script", "--script", str(script),
                              "--distinct-sessions"), cwd=tmp) as acp:
            acp.initialize(timeout_s=10)
            a = acp.new_session(cwd=tmp, timeout_s=10)
            b = acp.new_session(cwd=tmp, timeout_s=10)
            self.assertNotEqual(a, b)
            costs = [acp.prompt(sid, "go", timeout_s=10).vendor_cost_usd
                     for sid in (a, b, a, b)]
        self.assertEqual([round(c, 6) for c in costs], [2.0, 10.0, 1.0, 1.0])


class FailedTurnIsStillPaidForTests(unittest.TestCase):
    def test_the_parser_keeps_what_a_failed_turn_cost(self) -> None:
        out = json.dumps({"is_error": True, "result": "overloaded", "total_cost_usd": 0.07,
                          "usage": {"input_tokens": 120, "output_tokens": 4}})
        with self.assertRaises(Infra) as caught:
            _parse_driver_output("claude", out, "", 0, "prompt", ["claude"])
        paid = caught.exception.usage
        self.assertIsInstance(paid, DriverResponse)
        self.assertAlmostEqual(paid.cost_usd, 0.07)
        self.assertEqual((paid.tokens_in, paid.tokens_out), (120, 4))

    def test_a_failure_that_reports_nothing_carries_nothing(self) -> None:
        with self.assertRaises(Infra) as caught:
            _parse_driver_output("claude", "", "boom", 1, "prompt", ["claude"])
        self.assertFalse(hasattr(caught.exception, "usage"))

    def test_an_auth_failure_keeps_it_too(self) -> None:
        out = json.dumps({"is_error": True, "result": "Please run /login", "total_cost_usd": 0.01})
        with self.assertRaises(Refused) as caught:
            _parse_driver_output("claude", out, "", 1, "prompt", ["claude"])
        self.assertAlmostEqual(caught.exception.usage.cost_usd, 0.01)


class FailedTurnsCountInTheRunTests(DriveSpendTotalTests):
    def test_a_failed_turns_cost_reaches_the_cap(self) -> None:
        sp = self._script({
            **_workers("CARD-1"),
            "driver/plan": {"fail": "driver-cli-failed", "message": "overloaded",
                            "tokens_in": 10, "tokens_out": 1, "cost_usd": 0.06},
        })
        res = self._drive(sp, ["CARD-1"], 0.05)
        self.assertAlmostEqual(res["driver_spend"], 0.06, places=6)
        self.assertIn("reached its cap of 0.0500", " ".join(
            [res.get("spend_stopped") or ""] + [str(o.get("skipped", "")) for o in res["outcomes"]]))


class CorrectionPromptTests(unittest.TestCase):
    def test_it_carries_the_task_the_rejected_answer_and_the_problem(self) -> None:
        text = _correction_prompt('{"card": "CARD-9", "diff": "+ x"}', "I think it is fine",
                                  "Invalid wave review response (no JSON).", "Reply with ONLY JSON.")
        self.assertIn('"card": "CARD-9"', text)
        self.assertIn("I think it is fine", text)
        self.assertTrue(text.startswith("Invalid wave review response (no JSON)."))
        self.assertTrue(text.rstrip().endswith("Reply with ONLY JSON."))
        self.assertLess(text.index("=== THE TASK ==="), text.index("=== YOUR REJECTED ANSWER ==="))

    def test_a_very_long_rejected_answer_is_cut(self) -> None:
        text = _correction_prompt("task", "z" * 50_000, "bad.", "again.")
        self.assertLess(len(text), 8_000)
        self.assertIn("[... cut ...]", text)


class WaveCorrectionSeesTheDiffTests(_WaveFixture):
    def test_the_correction_turn_is_given_the_dossier_and_diff_again(self) -> None:
        seen: dict[str, str] = {}
        real = DriverAgent._complete_scripted

        def spy(agent, prompt, *, turn_key):
            seen[turn_key] = prompt
            return real(agent, prompt, turn_key=turn_key)

        with mock.patch.object(driver.DriverAgent, "_complete_scripted", spy):
            rev = self._review({
                "driver/wave/CARD-1": {"content": "looks good to me"},
                "driver/wave/CARD-1/correction": {"verdict": "minor-fix", "note": "ok"},
            })
        first, again = seen["driver/wave/CARD-1"], seen["driver/wave/CARD-1/correction"]
        self.assertIn(first, again, "the correction turn was not given the review task")
        self.assertIn("looks good to me", again)
        self.assertIn('"diff"', again)
        self.assertTrue(rev["reviewed"], rev)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for case in (CostPerSessionTests, FailedTurnIsStillPaidForTests, CorrectionPromptTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    suite.addTest(FailedTurnsCountInTheRunTests("test_a_failed_turns_cost_reaches_the_cap"))
    suite.addTest(WaveCorrectionSeesTheDiffTests(
        "test_the_correction_turn_is_given_the_dossier_and_diff_again"))
    return suite


if __name__ == "__main__":
    unittest.main()
