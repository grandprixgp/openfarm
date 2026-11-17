"""Tests for Stage 9 (Done 12): StallMonitor (`role = "monitor"`, `kind = "code"`)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.config import Config
from openfarm_core.errors import StopCode
from openfarm_core.state import StateDir
from openfarm_core.swarm import StallMonitor
from tests.test_python_e2e import GOOD_TEST, build_python_toy


def _run_cli(repo: Path, *args: str) -> tuple[int, dict, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--repo", str(repo), "--json", *args])
    raw = out.getvalue().strip()
    return code, (json.loads(raw) if raw else {}), err.getvalue()


class StallMonitorUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(
            tempfile.mkdtemp(prefix="openfarm-monitor-", dir=os.environ.get("TMPDIR"))
        )
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = StateDir(self.tmp / "state")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.cfg = Config(
            {
                "project": {
                    "name": "demo",
                    "base_branch": "main",
                    "state_dir": str(self.state.root),
                },
                "policy": {"categories": ["pinned-bugfix"]},
                "tiers": {"dsf": {"model": "stub"}},
                "swarm": {
                    "enabled": True,
                    "coordinator": "code",
                    "supervisor": "external",
                    "stall_after": "5m",
                },
            },
            self.repo,
        )
        self.board = Board(board_dir_for(self.state, "run-mon-1"), state_dir=self.state)
        self.board.ensure()

    def test_stall_fires_once_for_oldest_unanswered_question_and_resets_on_activity(
        self,
    ) -> None:
        self.board.upsert_member("gates", kind="code", role="gates")
        self.board.upsert_member(
            "coordinator", kind="code", role="coordinator", state="waiting"
        )
        self.board.upsert_member("sup-ext", kind="external", role="supervisor")
        self.board.upsert_member(
            "CARD-1-implementer",
            kind="api",
            role="implementer",
            card="CARD-1",
            state="waiting",
        )

        q1 = self.board.post(
            "gates",
            ["supervisor"],
            "question",
            "triage CARD-1 handback?",
            card="CARD-1",
            attempt=1,
            expects={"op": "triage", "required": ["action"]},
        )
        q2 = self.board.post(
            "gates",
            ["coordinator"],
            "question",
            "clarify priority for CARD-1?",
            card="CARD-1",
            attempt=1,
            expects={"op": "clarify", "required": ["action"]},
        )

        fake_now = [100.0]
        monitor = StallMonitor(self.board, self.cfg, clock=lambda: fake_now[0])
        mon_member = monitor.ensure_member()
        self.assertEqual(mon_member["name"], "monitor")
        self.assertEqual(mon_member["kind"], "code")
        self.assertEqual(mon_member["role"], "monitor")
        self.assertEqual(monitor.stall_after_s, 300.0)
        self.assertEqual(monitor.poll_interval_s, 30.0)

        # Before stall_after (300s): silent
        fake_now[0] = 399.0
        self.assertIsNone(monitor.check_once())

        # At 300s quiet: posts ONE stall targeting supervisor (oldest q1) + coordinator
        fake_now[0] = 400.0
        stall1 = monitor.check_once()
        self.assertIsNotNone(stall1)
        self.assertEqual(stall1["from"], "monitor")
        self.assertEqual(stall1["kind"], "stall")
        self.assertEqual(stall1["ref"], q1["seq"])
        self.assertEqual(stall1["card"], "CARD-1")
        self.assertEqual(stall1["to"], ["supervisor", "coordinator"])

        # Subsequent checks without non-stall posts must NOT nag again
        fake_now[0] = 900.0
        self.assertIsNone(monitor.check_once())
        fake_now[0] = 1500.0
        self.assertIsNone(monitor.check_once())

        # Answering q1 creates non-stall activity and resets quiet timer
        fake_now[0] = 1505.0
        self.board.answer(q1["seq"], as_name="sup-ext", data={"action": "retry"})
        self.assertIsNone(monitor.check_once())

        # Quiet below stall_after: still nothing
        fake_now[0] = 1804.0
        self.assertIsNone(monitor.check_once())

        # Quiet >= 300s after answer: fires ONCE for remaining oldest unanswered q2
        fake_now[0] = 1805.0
        stall2 = monitor.check_once()
        self.assertIsNotNone(stall2)
        self.assertEqual(stall2["kind"], "stall")
        self.assertEqual(stall2["ref"], q2["seq"])
        self.assertEqual(stall2["to"], ["coordinator"])

    def test_monitor_stays_quiet_when_active_done_or_ended(self) -> None:
        self.board.upsert_member("gates", kind="code", role="gates")
        self.board.upsert_member(
            "coordinator", kind="code", role="coordinator", state="waiting"
        )
        self.board.upsert_member(
            "CARD-1-implementer",
            kind="api",
            role="implementer",
            card="CARD-1",
            state="active",
        )
        fake_now = [0.0]
        active_flag = [0]
        monitor = StallMonitor(
            self.board,
            self.cfg,
            active_work_fn=lambda: active_flag[0],
            clock=lambda: fake_now[0],
        )

        # Case 1: worker is still active -> no stall
        fake_now[0] = 1000.0
        self.assertIsNone(monitor.check_once())

        # Case 2: worker waiting, but active_work_fn > 0 -> no stall
        self.board.update_member("CARD-1-implementer", state="waiting")
        active_flag[0] = 1
        fake_now[0] = 2000.0
        self.assertIsNone(monitor.check_once())

        # Case 3: card finished (done posted, no unanswered questions) -> no stall
        active_flag[0] = 0
        self.board.post("gates", "coordinator", "done", "CARD-1 done", card="CARD-1")
        fake_now[0] = 3000.0
        self.assertIsNone(monitor.check_once())

        # Case 4: run ended with unfinished question -> no stall
        self.board.post(
            "gates",
            "coordinator",
            "question",
            "question after done",
            expects={"op": "ack", "required": ["ok"]},
        )
        self.board.end_run("finished")
        fake_now[0] = 4000.0
        self.assertIsNone(monitor.check_once())


class StallMonitorRunnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def test_handback_to_silent_external_supervisor_triggers_monitor_stall(self) -> None:
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text += (
            '\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low", "medium"]\n'
            '\n[swarm]\n'
            'enabled = true\n'
            'coordinator = "code"\n'
            'supervisor = "external"\n'
            'default_tier = "dsf"\n'
            'stall_after = "1s"\n'
            'external_wait = "2s"\n'
            'on_external_timeout = "block"\n'
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")
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

        sp = self.toy.base / "script.json"
        sp.write_text(
            json.dumps({
                "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "CARD-1/implementer/1": "<<<HANDBACK\nunclear error hierarchy\n>>>",
            }),
            encoding="utf-8",
        )

        code, payload, err = _run_cli(self.toy.root, "run", "--cards", "1", "--script", str(sp))
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(payload["outcomes"][0]["final_state"], "blocked")
        self.assertEqual(payload["outcomes"][0]["stop_code"], StopCode.SEAT_SILENT.value)

        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        mon = board.get_member("monitor")
        self.assertIsNotNone(mon)
        self.assertEqual(mon["kind"], "code")
        self.assertEqual(mon["role"], "monitor")

        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        stalls = [p for p in posts if p["kind"] == "stall"]
        self.assertEqual(len(stalls), 1, f"expected exactly 1 stall post, got {stalls}")
        self.assertEqual(stalls[0]["from"], "monitor")
        self.assertIn("supervisor", stalls[0]["to"])
        self.assertIn("coordinator", stalls[0]["to"])
        self.assertIsNotNone(stalls[0]["ref"])

    def test_silent_external_coordinator_triggers_one_stall_before_seat_silent(self) -> None:
        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text += (
            '\n[tiers.dsf]\nmodel = "stub"\nefforts = ["low"]\n'
            '\n[swarm]\n'
            'enabled = true\n'
            'coordinator = "external"\n'
            'supervisor = "off"\n'
            'default_tier = "dsf"\n'
            'stall_after = "1s"\n'
            'external_wait = "2s"\n'
            'on_external_timeout = "block"\n'
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")
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

        sp = self.toy.base / "script.json"
        sp.write_text("{}", encoding="utf-8")
        code, payload, err = _run_cli(self.toy.root, "run", "--cards", "1", "--script", str(sp))
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(payload["outcomes"][0]["final_state"], "blocked")
        self.assertEqual(payload["outcomes"][0]["stop_code"], StopCode.SEAT_SILENT.value)

        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        stalls = [p for p in posts if p["kind"] == "stall"]
        self.assertEqual(len(stalls), 1, f"expected exactly 1 stall post, got {stalls}")
        self.assertEqual(stalls[0]["from"], "monitor")
        self.assertEqual(stalls[0]["to"], ["coordinator"])


if __name__ == "__main__":
    unittest.main()
