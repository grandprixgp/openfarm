"""C2: the code policy's escalation never lowers effort, and says what it did."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.config import DEFAULT_EFFORTS
from openfarm_core.coordinator import escalation_step
from tests.test_v081_coord_support import all_posts, board_for, make_toy, run_cli, write_script

_BAD1 = "=== whole_file: tally/m_a.py\ndef a(x: int) -> int:\n    return x + 5\n"
_BAD2 = "=== whole_file: tally/m_a.py\ndef a(x: int) -> int:\n    return x + 7777\n"


class EscalationStepTests(unittest.TestCase):
    def test_set_effort_steps_one_rung_up_and_stops_at_the_top(self) -> None:
        ladder = ("low", "medium", "high", "max")
        self.assertEqual(escalation_step(ladder, "low"), "medium")
        self.assertEqual(escalation_step(ladder, "medium"), "high")
        self.assertEqual(escalation_step(ladder, "high"), "max")
        self.assertIsNone(escalation_step(ladder, "max"))
        self.assertIsNone(escalation_step(ladder, "xhigh"))  # not on this tier

    def test_set_effort_never_steps_down_a_badly_ordered_list(self) -> None:
        self.assertEqual(escalation_step(("high", "low", "max"), "high"), "max")
        self.assertIsNone(escalation_step(("high", "off"), "high"))

    def test_unset_effort_never_goes_to_off_or_the_lowest_rung(self) -> None:
        # The implicit default tier and the documented [tiers.dsf] list.
        self.assertEqual(escalation_step(DEFAULT_EFFORTS, ""), "high")
        self.assertEqual(escalation_step(("low", "medium", "high", "max"), ""), "high")
        self.assertEqual(escalation_step(("low", "high", "max"), ""), "high")
        self.assertEqual(escalation_step(("off", "low", "medium", "high", "xhigh", "max"), ""), "high")
        for ladder in (DEFAULT_EFFORTS, ("low", "medium", "high", "max"), ("minimal", "low", "medium", "high")):
            step = escalation_step(ladder, "")
            self.assertNotIn(step, ("off", "none", "minimal", "low", "medium"), ladder)

    def test_unset_effort_does_nothing_when_no_rung_is_a_sure_raise(self) -> None:
        self.assertIsNone(escalation_step(("low", "medium"), ""))
        self.assertIsNone(escalation_step(("off", "low"), ""))
        self.assertIsNone(escalation_step(("low",), ""))
        self.assertIsNone(escalation_step((), ""))
        self.assertIsNone(escalation_step(("off",), ""))

    def test_unset_effort_with_unknown_names_takes_the_middle_rounded_up(self) -> None:
        self.assertEqual(escalation_step(("quick", "deep"), ""), "deep")
        self.assertEqual(escalation_step(("a", "b", "c"), ""), "b")
        self.assertEqual(escalation_step(("a", "b", "c", "d"), ""), "c")
        self.assertEqual(escalation_step(("off", "a", "b", "c", "d"), ""), "c")
        self.assertIsNone(escalation_step(("off", "solo"), ""))


class CodePolicyEscalationRunTests(unittest.TestCase):
    def _run(self, tiers):
        swarm = '\n[swarm]\nenabled = true\nsupervisor = "off"\n'
        toy, script = make_toy(1, swarm=swarm, tiers=tiers)
        self.addCleanup(toy.cleanup)
        good = script.pop("CARD-1/implementer")
        script["CARD-1/implementer/1"] = _BAD1
        script["CARD-1/implementer/2"] = _BAD2
        script["CARD-1/implementer"] = good
        code, payload, err = run_cli(
            toy.root, "run", "--cards", "1", "--script", str(write_script(self, script))
        )
        self.assertEqual(code, 0, err)
        board = board_for(toy, payload["run_id"])
        sets = [
            p for p in all_posts(board)
            if p["kind"] == "action" and p["body"].startswith("set CARD-1-implementer")
        ]
        return payload, sets

    def test_implicit_default_tier_escalates_up_not_to_off(self) -> None:
        payload, sets = self._run(None)
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")
        self.assertEqual(len(sets), 1, sets)
        self.assertIn("effort=high", sets[0]["body"])
        self.assertIn("endpoint's default effort", sets[0]["body"])
        self.assertNotIn("off", payload["tokens"]["by_effort"])

    def test_documented_dsf_tier_escalates_up_not_to_low(self) -> None:
        tiers = (
            '\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n'
        )
        payload, sets = self._run(tiers)
        self.assertEqual(len(sets), 1, sets)
        self.assertIn("effort=high", sets[0]["body"])
        self.assertNotIn("low", payload["tokens"]["by_effort"])

    def test_tier_with_no_sure_raise_leaves_the_member_alone(self) -> None:
        tiers = (
            '\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low", "medium"]\n'
            'effort_param = "reasoning_effort"\n'
        )
        payload, sets = self._run(tiers)
        self.assertEqual(sets, [])
        self.assertEqual(set(payload["tokens"]["by_effort"]), {"none"})


if __name__ == "__main__":
    unittest.main()
