"""v0.8.1 fix F4: a coordinator's stop request stops a running card.

`swarm block` (or a tier coordinator's `block`) on a card that is already running
only writes a board post. The card asks `Board.stop_requested` before each model
call and wherever the kill switch is checked, and ends blocked with `refused`
through the normal finish path. With the swarm off nothing changes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.briefs import Brief
from openfarm_core.errors import GateFailure, StopCode
from openfarm_core.state import read_json
from openfarm_core.swarm import swarm_block
from tests.test_v081_flow_handback import _FlowHarness

REASON = "changed plan"


class _HookedBackend(ScriptedBackend):
    """A scripted backend that runs a hook after a given call answers."""

    def __init__(self, script: dict, hooks: dict) -> None:
        super().__init__(script)
        self.hooks = dict(hooks)

    def complete(self, role, model, temperature, messages, seed=None,
                 max_tokens=4096, response_format=None, card_id="", attempt=1,
                 effort="", effort_param=""):
        answer = super().complete(role, model, temperature, messages, seed=seed,
                                  max_tokens=max_tokens,
                                  response_format=response_format,
                                  card_id=card_id, attempt=attempt, effort=effort,
                                  effort_param=effort_param)
        hook = self.hooks.pop((role, attempt), None)
        if hook is not None:
            hook()
        return answer


class CoordinatorStopTests(_FlowHarness):

    def _hooked_run(self, hooks_for, *, swarm_on: bool = True):
        run = self._card_run(self._script(), swarm_on=swarm_on)
        backend = _HookedBackend(run.backend.script, hooks_for(run))
        run.backend = backend
        run.roles.backend = backend
        return run

    def _block(self, run, reason: str = REASON) -> None:
        run._ensure_board_members()
        swarm_block(run.board, run.cfg, caller="coordinator", card_id="CARD-1",
                    reason=reason)

    def test_stop_during_the_implementer_call_ends_the_card_before_its_gates(self) -> None:
        run = self._hooked_run(lambda r: {("implementer", 1): lambda: self._block(r)})
        result = run.run()

        self.assertEqual(result["final_state"], "blocked")
        self.assertEqual(result["stop_code"], StopCode.REFUSED.value)
        self.assertEqual(result["detail"], f"stopped by coordinator: {REASON}")
        self.assertEqual(run.coordinator_stop, REASON)

        # No model call after the one in flight when the stop came.
        roles = [c["role"] for c in run.backend.calls]
        self.assertEqual(roles[-1], "implementer")
        self.assertNotIn("checker_pass1", roles)
        self.assertEqual(roles.count("implementer"), 1)
        # The good implementation was never judged: no named-test gate after it.
        self.assertFalse([g for g in run.gates if g.name == "checker-pass1"])

        # The normal finish path: dossier, card record and a final blocked post.
        self.assertTrue((run.card_layout / "dossier.json").exists())
        record = read_json(run.card_layout / "card-record.json")
        self.assertEqual(record["stop_code"], "refused")
        self.assertEqual(record["detail"], f"stopped by coordinator: {REASON}")
        posts = self._board_posts(run)
        final = posts[-1]
        self.assertEqual(final["from"], "gates")
        self.assertEqual(final["kind"], "blocked")
        self.assertIn("refused", final["body"])
        self.assertIn(f"stopped by coordinator: {REASON}", final["body"])

        if run._harness_enabled():
            # A stop is the coordinator's decision, not a lesson about the card.
            self.assertEqual(run.harness_lessons_added, [])
            self.assertEqual(run.harness_summary.get("skipped_reason"), "refused")

    def test_stop_during_the_test_writer_call_makes_no_more_model_calls(self) -> None:
        run = self._hooked_run(lambda r: {("test_writer", 1): lambda: self._block(r)})
        result = run.run()
        self.assertEqual(result["stop_code"], StopCode.REFUSED.value)
        self.assertEqual(result["detail"], f"stopped by coordinator: {REASON}")
        self.assertEqual([c["role"] for c in run.backend.calls], ["test_writer"])

    def test_a_board_call_asks_before_the_model_is_called(self) -> None:
        run = self._card_run(self._script(), swarm_on=True)
        self._block(run)
        with self.assertRaises(GateFailure) as caught:
            run._call_board_role("implementer", Brief("implementer", "brief"))
        self.assertEqual(caught.exception.code, StopCode.REFUSED)
        self.assertEqual(caught.exception.detail, f"stopped by coordinator: {REASON}")
        self.assertEqual(run.backend.calls, [])

    def test_a_build_asks_where_the_kill_switch_is_checked(self) -> None:
        run = self._card_run(self._script(), swarm_on=True)
        self._block(run)
        with self.assertRaises(GateFailure) as caught:
            run._check_kill_switch()
        self.assertEqual(caught.exception.code, StopCode.REFUSED)

    def test_a_blocked_post_from_an_unseated_name_does_not_stop_the_card(self) -> None:
        def unseated_block(run) -> None:
            # A name that holds no seat, and the supervisor's seat: neither is the
            # coordinator's, so neither stops a card.
            run.board.post("alice", ["all"], "blocked", REASON, card="CARD-1")
            run.board.upsert_member("sup", kind="external", role="supervisor",
                                    added_by="cli")
            run.board.post("sup", ["all"], "blocked", REASON, card="CARD-1")

        run = self._hooked_run(
            lambda r: {("implementer", 1): lambda: unseated_block(r)})
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))
        self.assertEqual(run.coordinator_stop, "")

    def test_the_cards_own_final_post_is_not_a_stop_request(self) -> None:
        run = self._card_run(self._script(), swarm_on=True)
        run._ensure_board_members()
        run._post_board(author="gates", to=["coordinator"], kind="blocked",
                        body="CARD-1 blocked (attempts): earlier run", trusted=True)
        run._check_stop_requested()
        self.assertEqual(run.coordinator_stop, "")

    def test_swarm_off_never_asks(self) -> None:
        run = self._card_run(self._script(), swarm_on=False)
        self.assertIsNone(run.board)
        run._check_stop_requested()
        result = run.run()
        self.assertEqual(result["final_state"], "in-review", result.get("detail"))


if __name__ == "__main__":
    unittest.main()
