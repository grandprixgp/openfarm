"""v0.8.1 B3: the stall monitor fires whatever kind of supervisor is seated."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.config import Config
from openfarm_core.errors import StopCode
from openfarm_core.state import StateDir
from openfarm_core.swarm import StallMonitor
from tests.test_monitor import _run_cli
from tests.test_python_e2e import build_python_toy


class SeatedMembersDoNotBlockStallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(
            tempfile.mkdtemp(prefix="openfarm-v081-stall-", dir=os.environ.get("TMPDIR"))
        )
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = StateDir(self.tmp / "state")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.cfg = Config(
            {
                "project": {"name": "demo", "base_branch": "main",
                            "state_dir": str(self.state.root)},
                "policy": {"categories": ["pinned-bugfix"]},
                "tiers": {"dsf": {"model": "stub"}},
                "swarm": {"enabled": True, "coordinator": "external",
                          "supervisor": "tier:dsf", "stall_after": "5m"},
            },
            self.repo,
        )
        self.board = Board(board_dir_for(self.state, "run-st"), state_dir=self.state)
        self.board.ensure()
        self.board.upsert_member("gates", kind="code", role="gates")

    def _monitor(self, now: list[float], *, active: list[int] | None = None,
                 unfinished: list[bool] | None = None) -> StallMonitor:
        active = active if active is not None else [0]
        unfinished = unfinished if unfinished is not None else [True]
        return StallMonitor(
            self.board, self.cfg,
            active_work_fn=lambda: active[0],
            unfinished_fn=lambda: unfinished[0],
            clock=lambda: now[0],
        )

    def test_tier_supervisor_and_code_coordinator_never_count_as_working(self) -> None:
        # A tier supervisor and the code coordinator are registered active for the
        # whole run; they only act when asked.
        self.board.upsert_member("supervisor", kind="scripted", role="supervisor")
        self.board.upsert_member("coordinator", kind="code", role="coordinator")
        for role in ("planner", "test-writer", "implementer", "checker"):
            self.board.upsert_member(
                f"CARD-1-{role}", kind="scripted", role=role, card="CARD-1", state="waiting"
            )
        now = [0.0]
        monitor = self._monitor(now)
        now[0] = 299.0
        self.assertIsNone(monitor.check_once())
        now[0] = 301.0
        post = monitor.check_once()
        self.assertIsNotNone(post)
        self.assertEqual(post["kind"], "stall")
        self.assertIn("coordinator", post["to"])
        # exactly one per stall
        now[0] = 900.0
        self.assertIsNone(monitor.check_once())

    def test_config_and_acp_supervisors_do_not_block_stall(self) -> None:
        for kind in ("acp", "api"):
            with self.subTest(kind=kind):
                board = Board(board_dir_for(self.state, f"run-{kind}"), state_dir=self.state)
                board.ensure()
                board.upsert_member("supervisor", kind=kind, role="supervisor")
                now = [0.0]
                monitor = StallMonitor(
                    board, self.cfg, active_work_fn=lambda: 0,
                    unfinished_fn=lambda: True, clock=lambda: now[0],
                )
                now[0] = 400.0
                self.assertIsNotNone(monitor.check_once())

    def test_busy_card_thread_or_active_worker_still_holds_stall_off(self) -> None:
        self.board.upsert_member("supervisor", kind="scripted", role="supervisor")
        self.board.upsert_member(
            "CARD-1-implementer", kind="scripted", role="implementer", card="CARD-1"
        )
        now = [0.0]
        active = [1]
        monitor = self._monitor(now, active=active)
        now[0] = 1000.0
        self.assertIsNone(monitor.check_once())  # a card thread is executing
        active[0] = 0
        now[0] = 2000.0
        self.assertIsNone(monitor.check_once())  # its worker is active
        # once the card has its own final post, its workers no longer count
        self.board.post("gates", "coordinator", "done", "CARD-1 done",
                        card="CARD-1", trusted=True)
        now[0] = 2100.0
        monitor.check_once()  # sees the post: activity resets the quiet timer
        now[0] = 2500.0
        self.assertIsNotNone(monitor.check_once())

    def test_monitor_keeps_only_open_questions_not_every_post(self) -> None:
        now = [0.0]
        monitor = self._monitor(now, active=[1])
        q = self.board.post("gates", ["supervisor"], "question", "triage?", card="CARD-1",
                            expects={"op": "triage", "required": ["action"]}, trusted=True)
        for idx in range(200):
            self.board.post("gates", [], "note", f"note {idx}", trusted=True)
        monitor.check_once()
        self.assertEqual(list(monitor._open_questions), [q["seq"]])
        self.assertFalse(getattr(monitor, "_all_posts", []))
        self.board.upsert_member("sup", kind="external", role="supervisor")
        self.board.answer(q["seq"], as_name="sup", data={"action": "hint"})
        monitor.check_once()
        self.assertEqual(monitor._open_questions, {})


class TierSupervisorStallRunTests(unittest.TestCase):
    """The repro's second scenario: external coordinator never joins, supervisor on a tier."""

    def test_silent_external_coordinator_with_tier_supervisor_gets_one_stall(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        cfg_path = toy.root / ".openfarm" / "config.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8")
            + '\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low"]\n'
            '\n[tiers.sup]\nmodel = "sup-model"\nefforts = ["low", "high", "max"]\n'
            '\n[swarm]\nenabled = true\ncoordinator = "external"\nsupervisor = "tier:sup"\n'
            'default_tier = "dsf"\nstall_after = "1s"\nexternal_wait = "4s"\n'
            'on_external_timeout = "block"\n',
            encoding="utf-8",
        )
        toy.write_card(
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
        toy.commit("add CARD-1")
        sp = toy.base / "script.json"
        sp.write_text("{}", encoding="utf-8")
        code, payload, err = _run_cli(toy.root, "run", "--cards", "1", "--script", str(sp))
        self.assertIn("outcomes", payload, err)
        self.assertEqual(payload["outcomes"][0]["stop_code"], StopCode.SEAT_SILENT.value)
        state = StateDir(toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        roster = {m["name"]: m for m in board.list_members()}
        self.assertEqual(roster.get("supervisor", {}).get("role"), "supervisor", roster)
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        stalls = [p for p in posts if p["kind"] == "stall"]
        self.assertEqual(len(stalls), 1, json.dumps(stalls))
        self.assertEqual(stalls[0]["from"], "monitor")
        self.assertIn("coordinator", stalls[0]["to"])


if __name__ == "__main__":
    unittest.main()
