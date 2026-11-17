"""v0.8.1 B2: a coordinator's stop request does not make a running card finished."""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir
from openfarm_core.swarm import (
    swarm_block,
    swarm_join,
    swarm_remove,
    swarm_start,
    swarm_status,
    worker_member_name,
)
from tests.test_python_e2e import build_python_toy
from tests.test_swarm_roster import _card, _make_config


class StopRequestKeepsCardRunningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-v081-finish-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = StateDir(self.tmp / "state")
        self.cfg = _make_config(self.repo, self.state.root)  # concurrent_cards = 2
        self.board = Board(board_dir_for(self.state, "run-f"), state_dir=self.state)
        self.board.ensure()
        self.board.upsert_member("gates", kind="code", role="gates")
        swarm_join(self.board, self.cfg, name="lead", seat="coordinator", owner_kind="agent")
        self.cards = {
            "CARD-1": _card("CARD-1", touches=["pkg/a.py"]),
            "CARD-2": _card("CARD-2", touches=["pkg/a.py", "pkg/b.py"]),
            "CARD-3": _card("CARD-3", touches=["pkg/c.py"]),
            "CARD-4": _card("CARD-4", touches=["pkg/d.py"]),
        }

    def _start(self, cid: str) -> dict:
        return swarm_start(
            self.board, self.cfg, caller="lead", card_id=cid, cards_by_id=self.cards
        )

    def _final(self, cid: str, kind: str = "blocked") -> None:
        self.board.post(
            "gates", "coordinator", kind, f"{cid} {kind}", card=cid, trusted=True
        )

    def test_block_on_running_card_keeps_collision_and_slot_checks(self) -> None:
        self._start("CARD-1")
        res = swarm_block(
            self.board, self.cfg, caller="lead", card_id="CARD-1",
            reason="changed my mind", cards_by_id=self.cards,
        )
        self.assertTrue(res["running"])
        self.assertTrue(res["stop_requested"])
        self.assertIn("still running until it ends", res["message"])
        self.assertIn("keeps running until it ends", res["post"]["body"])
        self.assertEqual(self.board.stop_requested("CARD-1"), "changed my mind")

        status = swarm_status(self.board, self.cfg)
        self.assertEqual(status["running_cards"], ["CARD-1"])
        self.assertEqual(status["stopping_cards"], ["CARD-1"])

        # CARD-2 overlaps CARD-1's touches: refused while CARD-1 still runs
        with self.assertRaises(Refused) as collide:
            self._start("CARD-2")
        self.assertEqual(collide.exception.code, "card-collision")
        # CARD-1 itself cannot be started again either
        with self.assertRaises(Refused) as again:
            self._start("CARD-1")
        self.assertEqual(again.exception.code, "already-running")

        # Slots: CARD-3 takes the second slot; CARD-4 finds none free
        self._start("CARD-3")
        with self.assertRaises(Refused) as slots:
            self._start("CARD-4")
        self.assertEqual(slots.exception.code, "slot-limit")

        # Members of a card asked to stop cannot be removed yet
        impl = worker_member_name("CARD-1", "implementer")
        with self.assertRaises(Refused) as not_fin:
            swarm_remove(self.board, self.cfg, caller="lead", member_name=impl)
        self.assertEqual(not_fin.exception.code, "card-not-finished")

        # Once CARD-1's own final post lands it is finished and frees its slot
        self._final("CARD-1")
        status = swarm_status(self.board, self.cfg)
        self.assertEqual(status["running_cards"], ["CARD-3"])
        self.assertEqual(status["stopping_cards"], [])
        self._start("CARD-2")
        swarm_remove(self.board, self.cfg, caller="lead", member_name=impl)

    def test_second_block_on_same_card_is_refused(self) -> None:
        self._start("CARD-1")
        swarm_block(
            self.board, self.cfg, caller="lead", card_id="CARD-1",
            reason="first", cards_by_id=self.cards,
        )
        with self.assertRaises(Refused) as caught:
            swarm_block(
                self.board, self.cfg, caller="lead", card_id="CARD-1",
                reason="second", cards_by_id=self.cards,
            )
        self.assertEqual(caught.exception.code, "already-stopping")
        posts = self.board.read(as_name="*", since=0, limit=None, for_model=False)
        self.assertEqual(
            len([p for p in posts if p["kind"] == "blocked" and p["from"] == "lead"]), 1
        )
        self.assertEqual(self.board.stop_requested("CARD-1"), "first")

    def test_block_before_start_finishes_the_card(self) -> None:
        res = swarm_block(
            self.board, self.cfg, caller="lead", card_id="CARD-1",
            reason="not tonight", cards_by_id=self.cards,
        )
        self.assertFalse(res["running"])
        self.assertEqual(res["post"]["body"], "not tonight")
        with self.assertRaises(Refused) as caught:
            self._start("CARD-1")
        self.assertEqual(caught.exception.code, "already-finished")
        with self.assertRaises(Refused) as twice:
            swarm_block(
                self.board, self.cfg, caller="lead", card_id="CARD-1",
                reason="again", cards_by_id=self.cards,
            )
        self.assertEqual(twice.exception.code, "already-finished")
        # it never ran, so it holds no slot and collides with nothing
        self._start("CARD-2")

    def test_only_gates_final_post_or_coordinator_counts(self) -> None:
        self._start("CARD-1")
        # a worker's blocked post is neither a final post nor a stop request
        self.board.post(
            worker_member_name("CARD-1", "implementer"), "all", "blocked",
            "I give up", card="CARD-1",
        )
        self.assertEqual(self.board.stop_requested("CARD-1"), "")
        self.assertEqual(swarm_status(self.board, self.cfg)["running_cards"], ["CARD-1"])
        with self.assertRaises(Refused):
            self._start("CARD-2")
        # gates' final blocked post is not a stop request either
        self._final("CARD-1")
        self.assertEqual(self.board.stop_requested("CARD-1"), "")
        self.assertEqual(swarm_status(self.board, self.cfg)["running_cards"], [])


class StopRequestRunnerTests(unittest.TestCase):
    """The repro's second scenario: block CARD-1 while it runs, then start CARD-2."""

    def test_colliding_card_waits_for_stopped_card_to_really_end(self) -> None:
        from openfarm_core.budget import NightBudget
        from openfarm_core.cli import Context
        from openfarm_core.coordinator import SwarmRunner

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        for nm in ("a", "b"):
            toy.write(f"tally/m_{nm}.py", f"def {nm}(x: int) -> int:\n    return x\n")
        cfg_path = toy.root / ".openfarm" / "config.toml"
        text = cfg_path.read_text(encoding="utf-8")
        if "concurrent_cards =" in text:
            text = text.replace("concurrent_cards = 1", "concurrent_cards = 2")
        else:
            text = text.replace(
                "concurrent_builds = 2", "concurrent_builds = 2\nconcurrent_cards = 2"
            )
        text += (
            '\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "off"\n'
            'external_wait = "20s"\non_external_timeout = "block"\n'
        )
        cfg_path.write_text(text, encoding="utf-8")
        touches = {"CARD-1": ["tally/m_a.py"], "CARD-2": ["tally/m_a.py", "tally/m_b.py"]}
        for i, nm in enumerate(("a", "b"), 1):
            cid = f"CARD-{i}"
            toy.write_card(
                cid, title=f"Mod {nm}", category="pinned-bugfix", unit=".",
                touches=touches[cid],
                tests=[{"name": f"test_{nm}", "file": f"tests/test_{nm}.py",
                        "asserts": f"{nm}(1)==2"}],
                done_text=f"`{nm}(x)` returns `x + 1`.",
            )
        toy.commit("add cards")

        ctx = Context(toy.root)
        cfg = ctx.cfg
        source = ctx.cards()
        source.prepare()
        board = Board(board_dir_for(ctx.state, "rev-e2"), state_dir=ctx.state)
        board.ensure()
        board.upsert_member("gates", kind="code", role="gates", added_by="system")
        spans: dict[str, list] = {}

        def run_card(cid, on_handback=None, on_struggling=None):
            spans[cid] = [time.monotonic(), None]
            time.sleep(2.0)
            spans[cid][1] = time.monotonic()
            board.post(author="gates", to=["coordinator"], kind="done", card=cid,
                       body=f"{cid} done", trusted=True)
            return {"card_id": cid, "final_state": "in-review"}

        runner = SwarmRunner(
            cfg=cfg, state=ctx.state, source=source, board=board,
            identifiers=["CARD-1", "CARD-2"], night=NightBudget(0, cards_cap=2),
            deadline=None, run_card_fn=run_card, record_outcome_fn=lambda c, o: False,
        )
        runner.coordinator.poll_interval_s = 0.1
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        refused_codes: list[str] = []
        try:
            swarm_join(board, cfg, name="lead", seat="coordinator", owner_kind="agent")
            swarm_start(board, cfg, caller="lead", card_id="CARD-1", source=source)
            deadline = time.monotonic() + 30
            while "CARD-1" not in spans and time.monotonic() < deadline:
                time.sleep(0.05)
            swarm_block(board, cfg, caller="lead", card_id="CARD-1",
                        reason="changed my mind", source=source)
            try:
                swarm_start(board, cfg, caller="lead", card_id="CARD-2", source=source)
            except Refused as exc:
                refused_codes.append(exc.code)
            # once CARD-1 has really ended, CARD-2 may start
            while (spans.get("CARD-1") or [0, None])[1] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            while "CARD-1" in swarm_status(board, cfg)["running_cards"] and time.monotonic() < deadline:
                time.sleep(0.05)
            if "CARD-2" not in spans:
                swarm_start(board, cfg, caller="lead", card_id="CARD-2", source=source)
        finally:
            thread.join(60)
            board.end_run()
        self.assertEqual(refused_codes, ["card-collision"])
        a, b = spans.get("CARD-1"), spans.get("CARD-2")
        self.assertTrue(a and b, spans)
        overlap = a[0] < b[1] and b[0] < a[1]
        self.assertFalse(overlap, f"CARD-1 {a} and CARD-2 {b} ran at the same time")


if __name__ == "__main__":
    unittest.main()
