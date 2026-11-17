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


if __name__ == "__main__":
    unittest.main()
