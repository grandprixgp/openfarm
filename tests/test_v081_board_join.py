"""v0.8.1 B4: an external coordinator that joins after the code fallback acted is let in."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.coordinator import CodeCoordinator
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir
from openfarm_core.swarm import (
    swarm_block,
    swarm_join,
    swarm_start,
    swarm_wait,
    worker_member_name,
    swarm_hint,
)
from tests.test_swarm_roster import _card, _make_config


class LateExternalJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(
            tempfile.mkdtemp(prefix="openfarm-v081-join-", dir=os.environ.get("TMPDIR"))
        )
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = StateDir(self.tmp / "state")
        self.cfg = _make_config(self.repo, self.state.root)  # coordinator = "external"
        self.board = Board(board_dir_for(self.state, "run-j"), state_dir=self.state)
        self.board.ensure()
        # The code fallback acted for the silent external coordinator and
        # registered its `coordinator` code member.
        CodeCoordinator().ensure_member(self.board)
        self.assertEqual(self.board.get_member("coordinator")["kind"], "code")
        self.cards = {"CARD-1": _card("CARD-1"), "CARD-2": _card("CARD-2")}

    def test_late_join_takes_the_seat_and_its_commands_work(self) -> None:
        member = swarm_join(
            self.board, self.cfg, name="lead", seat="coordinator", owner_kind="agent"
        )
        self.assertEqual(member["role"], "coordinator")
        self.assertEqual(self.board.seat_of("lead"), "coordinator")
        swarm_start(
            self.board, self.cfg, caller="lead", card_id="CARD-1",
            cards_by_id=self.cards, outside=True,
        )
        swarm_hint(
            self.board, self.cfg, caller="lead",
            member_name=worker_member_name("CARD-1", "implementer"),
            body="look at the edge", outside=True,
        )
        swarm_block(
            self.board, self.cfg, caller="lead", card_id="CARD-2",
            reason="not tonight", cards_by_id=self.cards, outside=True,
        )
        swarm_wait(self.board, self.cfg, caller="lead", outside=True)

    def test_a_second_external_holder_is_still_refused(self) -> None:
        swarm_join(self.board, self.cfg, name="lead", seat="coordinator", owner_kind="agent")
        with self.assertRaises(Refused) as caught:
            swarm_join(self.board, self.cfg, name="other", seat="coordinator", owner_kind="human")
        self.assertEqual(caught.exception.code, "seat-occupied")
        self.assertIn("lead", caught.exception.message)
        # rejoining under the same name is fine
        again = swarm_join(
            self.board, self.cfg, name="lead", seat="coordinator", owner_kind="agent"
        )
        self.assertEqual(again["name"], "lead")

    def test_joining_under_the_code_members_name_is_refused(self) -> None:
        with self.assertRaises(Refused) as caught:
            swarm_join(
                self.board, self.cfg, name="coordinator", seat="coordinator", owner_kind="agent"
            )
        self.assertEqual(caught.exception.code, "name-taken")
        self.assertEqual(self.board.get_member("coordinator")["kind"], "code")

    def test_join_refuses_code_names_and_other_members_names(self) -> None:
        for name in ("gates", "monitor", "system"):
            with self.assertRaises(Refused) as caught:
                swarm_join(self.board, self.cfg, name=name, seat="supervisor", owner_kind="agent")
            self.assertEqual(caught.exception.code, "reserved-name")
        self.board.upsert_member(
            "CARD-9-implementer", kind="scripted", role="implementer", card="CARD-9"
        )
        with self.assertRaises(Refused) as caught:
            swarm_join(
                self.board, self.cfg, name="CARD-9-implementer",
                seat="coordinator", owner_kind="agent",
            )
        self.assertEqual(caught.exception.code, "name-taken")
        self.assertEqual(self.board.get_member("CARD-9-implementer")["role"], "implementer")

    def test_join_never_moves_or_revives_another_member(self) -> None:
        swarm_join(self.board, self.cfg, name="sup", seat="supervisor", owner_kind="human")
        with self.assertRaises(Refused) as other_seat:
            swarm_join(self.board, self.cfg, name="sup", seat="coordinator", owner_kind="human")
        self.assertEqual(other_seat.exception.code, "name-taken")
        self.assertEqual(self.board.get_member("sup")["role"], "supervisor")
        self.board.update_member("sup", state="removed")
        with self.assertRaises(Refused) as removed:
            swarm_join(self.board, self.cfg, name="sup", seat="supervisor", owner_kind="human")
        self.assertEqual(removed.exception.code, "name-taken")
        self.assertEqual(self.board.get_member("sup")["state"], "removed")
        # a worker's tier record is left alone
        self.board.upsert_member(
            "CARD-7-checker", kind="scripted", role="checker", card="CARD-7",
            tier="dsf", model="deepseek/deepseek-v4.1-flash", effort="high",
        )
        with self.assertRaises(Refused):
            swarm_join(self.board, self.cfg, name="CARD-7-checker",
                       seat="supervisor", owner_kind="agent")
        worker = self.board.get_member("CARD-7-checker")
        self.assertEqual((worker["role"], worker["tier"], worker["effort"]),
                         ("checker", "dsf", "high"))


if __name__ == "__main__":
    unittest.main()
