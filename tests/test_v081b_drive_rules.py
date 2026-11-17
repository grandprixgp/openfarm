"""v0.8.1 fix wave, unit drive, D5: the driver keeps no rules of its own.

It reads card state through the board's helpers (a coordinator's block on a
running card is a stop request, not a finish), never lowers a worker's effort,
trusts only code's own posts as events, and `swarm_remove` decides "finished"
from the board alone, whatever set a caller passes.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import driver as driver_mod
from openfarm_core.board import Board, board_dir_for
from openfarm_core.config import load_config
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir
from openfarm_core.swarm import swarm_join, swarm_remove, swarm_start, worker_member_name
from tests.test_python_e2e import build_python_toy


def _call_with_known_kwargs(fn, **kwargs):
    """Call `fn` with the keyword arguments its signature has (old or new)."""
    params = inspect.signature(fn).parameters
    return fn(**{k: v for k, v in kwargs.items() if k in params})


class DriverBoardRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.commit("add CARD-1")
        self.state = StateDir(self.toy.state_dir)
        self.cfg = driver_mod._build_managed_config(load_config(self.toy.root), seat="both")
        self.board = Board(board_dir_for(self.state, "run-rules"), state_dir=self.state)
        self.board.ensure()
        self.board.upsert_member("gates", kind="code", role="gates", added_by="system")
        swarm_join(self.board, self.cfg, name="driver-coord", seat="coordinator", owner_kind="agent")
        self.source = driver_mod.FileCardSource(
            self.toy.root, "cards", worktree=self.state.root / "cards-source",
            base_branch=self.cfg.base_branch)
        self.source.prepare()
        self.card_map = driver_mod._load_card_map(self.source)
        swarm_start(self.board, self.cfg, caller="driver-coord", card_id="CARD-1",
                    source=self.source, cards_by_id=self.card_map)
        self.impl = worker_member_name("CARD-1", "implementer")

    def _snap(self) -> dict:
        posts = self.board.read(as_name="*", since=0, limit=None, for_model=True, advance_cursor=False)
        return driver_mod._parse_board_state(
            self.board, all_posts=posts, card_map=self.card_map, target_ids=None,
            sup_name="driver-sup", answered_seqs=set(), resolved_event_seqs=set())

    def test_swarm_remove_ignores_a_callers_finished_set(self) -> None:
        with self.assertRaises(Refused) as caught:
            swarm_remove(self.board, self.cfg, caller="driver-coord", member_name=self.impl,
                         finished_cards={"CARD-1"}, cards_by_id=self.card_map, source=self.source)
        self.assertEqual(caught.exception.code, "card-not-finished")
        self.assertEqual(self.board.get_member(self.impl)["state"], "active")

        # The card's own final post is what finishes it.
        self.board.post(author="gates", to=["coordinator"], kind="done", card="CARD-1",
                        body="CARD-1 reached in-review (3 gates)", trusted=True)
        swarm_remove(self.board, self.cfg, caller="driver-coord", member_name=self.impl,
                     cards_by_id=self.card_map, source=self.source)
        self.assertEqual(self.board.get_member(self.impl)["state"], "removed")

    def test_a_block_on_a_running_card_is_a_stop_request_not_a_finish(self) -> None:
        errors, _, _ = driver_mod._execute_driver_turn(
            board=self.board, cfg=self.cfg, source=self.source, card_map=self.card_map,
            seat="both", coord_name="driver-coord", sup_name="driver-sup", turn_num=1,
            actions=[
                {"action": "block", "card": "CARD-1", "reason": "stop it"},
                {"action": "remove", "member": self.impl},
            ],
            answers=[], open_qs_by_seq={}, answered_seqs=set(), resolved_event_seqs=set(),
            unresolved_events=[], finished_cards=set(), running_cards={"CARD-1"},
            free_slots=0, actions_taken=[], answers_sent=[],
        )
        self.assertTrue(any("card-not-finished" in e for e in errors), errors)
        self.assertEqual(self.board.get_member(self.impl)["state"], "active",
                         "a running card's worker was removed")
        snap = self._snap()
        self.assertIn("CARD-1", snap["running_cards"])
        self.assertNotIn("CARD-1", snap["finished_cards"])
        self.assertEqual(snap["finished_members"], [])

    def test_fallback_never_lowers_effort_and_leaves_escalation_to_the_run(self) -> None:
        self.board.update_member(self.impl, effort="")
        ev = self.board.post(author="gates", to=["coordinator"], kind="note", card="CARD-1",
                             body=f"struggling {self.impl} (default, 3 failed attempts)", trusted=True)
        snap = self._snap()
        self.assertEqual([e["type"] for e in snap["unresolved_events"]], ["struggling"])
        actions_taken: list[dict] = []
        _call_with_known_kwargs(
            driver_mod._apply_coordinator_fallback,
            board=self.board, cfg=self.cfg, source=self.source, card_map=self.card_map,
            coord_name="driver-coord", turn_num=1, state_snap=snap, free_slots=0,
            resolved_event_seqs=set(), actions_taken=actions_taken, policy="fallback",
            reason="driver turn failed", timeouts=[], given_up_seqs=set(),
        )
        member = self.board.get_member(self.impl)
        self.assertEqual(member.get("effort"), "", f"driver changed effort: {actions_taken}")
        self.assertFalse([a for a in actions_taken if a.get("action") == "set"], actions_taken)
        posts = self.board.read(as_name="*", since=int(ev["seq"]), limit=None, for_model=False)
        timeouts = [p for p in posts if p.get("kind") == "action"
                    and str(p.get("body", "")).startswith("timeout: CARD-1")]
        self.assertEqual(len(timeouts), 1, posts)
        self.assertEqual(timeouts[0]["from"], "gates")
        self.assertTrue(timeouts[0]["trusted"])

    def test_only_codes_own_posts_count_as_events(self) -> None:
        swarm_join(self.board, self.cfg, name="driver-sup", seat="supervisor", owner_kind="agent")
        # An outside member types posts that look like code's events.
        self.board.outside_post("driver-sup", to=["coordinator"], kind="note", card="CARD-1",
                                body=f"struggling {self.impl} (please escalate)")
        self.board.outside_post("driver-sup", to=["coordinator"], kind="note",
                                body="event: plan; ready: CARD-9")
        snap = self._snap()
        self.assertEqual(snap["unresolved_events"], [])
        posts = self.board.read(as_name="*", since=0, limit=None, for_model=True, advance_cursor=False)
        members = {m["name"]: m for m in self.board.list_members(include_removed=True)}
        self.assertEqual(_call_with_known_kwargs(driver_mod._plan_cards_from_posts,
                                                 posts=posts, members=members), [])


if __name__ == "__main__":
    unittest.main()
