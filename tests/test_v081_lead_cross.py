"""v0.8.1: places where the flow and the board fixes meet.

The flow registers a card's workers itself when no `swarm start` did. It has to
use the same rule as `swarm start` (`Config.member_defaults`): a role with its
own `[model]` entry keeps that model until the coordinator names a tier.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.swarm import worker_member_name
from tests.test_v081_flow_handback import _FlowHarness


class FlowRegistersMembersLikeSwarmStartTests(_FlowHarness):

    def test_a_role_with_its_own_model_keeps_it_when_the_flow_registers_it(self) -> None:
        run = self._card_run(self._script(), swarm_on=True)
        run.cfg.role_models["checker"].model = "independent-checker"
        run._ensure_board_members()
        by_role = {
            role: run.board.get_member(worker_member_name("CARD-1", role))
            for role in ("planner", "test-writer", "implementer", "checker")
        }
        self.assertEqual(by_role["checker"]["model"], "independent-checker")
        for role in ("planner", "test-writer", "implementer"):
            self.assertEqual(by_role[role]["model"], "stub-fast", role)
            self.assertEqual(by_role[role]["tier"], "fast", role)
        # What the flow registers is what `swarm start` would have registered.
        for role, member in by_role.items():
            self.assertEqual(
                (member["tier"], member["model"]), run.cfg.member_defaults(role), role)


class ReplanIfOnlyCountsWithTheSwarmOnTests(_FlowHarness):
    """A malformed `replan_if` is an unknown field with the swarm off (v0.7)."""

    def _rules(self, *, swarm_on: bool) -> list[str]:
        from openfarm_core.gates.eligibility import check_eligibility

        run = self._card_run(self._script(), swarm_on=swarm_on)
        run.card.fields["replan_if"] = "not a list"
        result = check_eligibility(run.card, run.cfg, worktree=run.worktree)
        return [failure["rule"] for failure in result.failures]

    def test_swarm_off_ignores_a_malformed_replan_if(self) -> None:
        self.assertNotIn("replan-if", self._rules(swarm_on=False))

    def test_swarm_on_still_refuses_a_malformed_replan_if(self) -> None:
        self.assertIn("replan-if", self._rules(swarm_on=True))


class CounterTestBlockEndTests(unittest.TestCase):
    def test_a_code_line_starting_with_triple_equals_does_not_end_a_block(self) -> None:
        from openfarm_core.roles import parse_pass2
        answer = parse_pass2(
            "VERDICT: changes-requested\nREASONS:\nx\nTRACEABILITY:\ny\nEDGE CASES:\nz\n"
            "=== counter_test: wraps\n"
            "import test from \"node:test\";\n"
            "test(\"wraps\", () => {\n  if (a\n    === b) throw new Error(\"x\");\n});\n"
            "=== counter_test: second\ntest(\"second\", () => {});\n")
        self.assertTrue(answer.readable)
        self.assertEqual([t["name"] for t in answer.counter_tests], ["wraps", "second"])
        self.assertIn("=== b) throw", answer.counter_tests[0]["content"])
        self.assertNotIn("second", answer.counter_tests[0]["content"])



class CheckBaselineNamedRowsTests(unittest.TestCase):
    """`check --baseline` judges named tests by the run's own row rule.

    The base suite has to be green and a card's test cannot be on known_failing,
    so on base a named test is either absent or passing; what matters is whose
    rows are counted for it.
    """

    def _check(self, files: dict) -> dict:
        import json as _json
        from tests.test_v081b_packs_gates import build_python_toy, run_cli
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        for path, content in files.items():
            toy.write(path, content)
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py", "asserts": "push raises FullError"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        toy.commit("tally: CARD-1")
        result = run_cli(toy.root, "check", "CARD-1", "--baseline")
        return _json.loads(result.stdout)

    def test_a_same_named_passing_test_in_another_file_does_not_refuse_the_card(self) -> None:
        # The old rule took the last row with the name, whatever file it came from,
        # and refused the card as "already passing on base".
        from tests.test_v081b_packs_gates import OTHER_FILE_TEST
        payload = self._check({"tests/test_other.py": OTHER_FILE_TEST})
        baseline = payload["baseline"]
        self.assertEqual(baseline["named"],
                         {"test_a_full_bundle_refuses_a_push": "not-run"})
        self.assertTrue(baseline["ok"], baseline)
        self.assertTrue(payload["eligible"], payload)

    def test_the_card_s_own_test_passing_on_base_still_refuses_it(self) -> None:
        from tests.test_v081b_packs_gates import OTHER_FILE_TEST
        payload = self._check({"tests/test_full.py": OTHER_FILE_TEST})
        baseline = payload["baseline"]
        self.assertEqual(baseline["passing_on_base"],
                         ["test_a_full_bundle_refuses_a_push"])
        self.assertFalse(baseline["ok"])
        self.assertFalse(payload["eligible"])


if __name__ == "__main__":
    unittest.main()
