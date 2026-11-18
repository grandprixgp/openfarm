"""v0.8.1 fix wave, unit drive, D1: an aborted drive leaves nothing working or locked.

Covers the driver's clean-up (board ended, kill switch kept until the run thread
has ended, seat members removed, lock gone), what happens when that clean-up
itself fails or is interrupted, and the run lock's holder record with its
stale-holder takeover.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import driver as driver_mod
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.driver import drive_run
from openfarm_core.state import StateDir, read_json
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(args))
    return code, out.getvalue(), err.getvalue()


def _drive_threads() -> list[threading.Thread]:
    """The managed run's thread, under its new name or the old default one."""
    return [
        t for t in threading.enumerate()
        if t.name == "openfarm-drive-run" or "(_bg)" in t.name
    ]


def _dead_pid() -> int:
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    return int(proc.stdout.strip())


class _AbortAfter:
    """Wraps `_parse_board_state`: raises once `ready()` has held for `delay` checks."""

    def __init__(self, ready, delay_checks: int = 0) -> None:
        self.ready = ready
        self.delay = delay_checks
        self.seen = 0
        self.real = driver_mod._parse_board_state

    def __call__(self, *args, **kwargs):
        if self.ready():
            self.seen += 1
            if self.seen > self.delay:
                raise RuntimeError("driver blew up mid-run")
        return self.real(*args, **kwargs)


class DriveAbortTests(unittest.TestCase):
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
        self.sd = self.toy.state_dir
        self.addCleanup(self._wait_for_leftover_threads)

    def _wait_for_leftover_threads(self) -> None:
        # A failing run of these tests may leave the run thread going: stop it
        # before the toy folder is deleted under it.
        leftovers = [t for t in _drive_threads() if t.is_alive()]
        if leftovers:
            stop = self.sd / "STOP"
            stop.write_text("test clean-up\n", encoding="utf-8")
            for t in leftovers:
                t.join(120)

    def _script(self, test_text: str) -> Path:
        sp = Path(self.toy.base) / "script.json"
        sp.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + test_text,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
            "CARD-1/checker_pass2": "{}",
            "driver/plan": {"actions": [{"action": "start", "card": "CARD-1"}], "answers": []},
            "driver/question/shape_and_plan": {"actions": [], "answers": [
                {"seq": "auto", "data": {"verdict": "ready", "clarified_notes": "ok"}}]},
            "driver/*": {"actions": [], "answers": []},
        }), encoding="utf-8")
        return sp

    def _test_file_written(self) -> bool:
        wts = self.sd / "worktrees"
        return wts.is_dir() and any((w / "tests" / "test_full.py").is_file() for w in wts.iterdir())

    def _assert_nothing_left(self, run_id: str) -> None:
        self.assertEqual([t.name for t in _drive_threads() if t.is_alive()], [],
                         "the managed run's thread is still working after the drive ended")
        state = read_json(self.sd / "runs" / run_id / "board" / "state.json", {})
        self.assertTrue(state.get("ended"), f"board not ended: {state}")
        members = read_json(self.sd / "runs" / run_id / "board" / "members.json", {})
        for name in ("driver-coord", "driver-sup"):
            self.assertEqual(members[name]["state"], "removed", f"{name}: {members[name]}")
        self.assertFalse((self.sd / "run.lock").exists(), "run.lock left behind")
        self.assertFalse((self.sd / "STOP").exists(), "kill switch left behind")

    def _only_run_id(self) -> str:
        runs = sorted(p.name for p in (self.sd / "runs").iterdir())
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def test_abort_while_a_gate_runs_waits_for_the_run_and_cleans_up(self) -> None:
        # The card's test sleeps 20 s whenever it is imported, so the run thread is
        # inside a gate for longer than the old 15 s wait when the driver fails.
        slow_test = GOOD_TEST.replace("import unittest\n", "import time\nimport unittest\ntime.sleep(20)\n", 1)
        self.assertIn("time.sleep(20)", slow_test)
        sp = self._script(slow_test)
        abort = _AbortAfter(self._test_file_written, delay_checks=10)
        with mock.patch.object(driver_mod, "_parse_board_state", abort):
            with self.assertRaises(RuntimeError) as caught:
                drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.sd),
                          agent="claude", seat="both", card_ids=["CARD-1"], script=sp)
        self.assertIn("blew up", str(caught.exception))
        run_id = self._only_run_id()
        self._assert_nothing_left(run_id)
        record = read_json(next((self.sd / "cards" / "CARD-1").glob("*/card-record.json")), {})
        self.assertEqual(record.get("final_state"), "blocked", record)

    def test_abort_reports_a_failed_board_end_instead_of_swallowing_it(self) -> None:
        sp = self._script(GOOD_TEST)

        class _BrokenEndBoard(driver_mod.Board):
            def end_run(self, reason: str = "finished") -> dict:
                if reason == "driver-aborted":
                    raise OSError("board disk went away")
                return super().end_run(reason)

        abort = _AbortAfter(lambda: True)
        err = io.StringIO()
        with mock.patch.object(driver_mod, "_parse_board_state", abort), \
                mock.patch.object(driver_mod, "Board", _BrokenEndBoard), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(RuntimeError) as caught:
                drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.sd),
                          agent="claude", seat="both", card_ids=["CARD-1"], script=sp)
        notes = "\n".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("could not end the run's board", notes)
        self.assertIn("board disk went away", notes)
        self.assertIn("could not end the run's board", err.getvalue())
        # The run still stopped (kill switch), ended its own board and let go of the lock.
        self.assertEqual([t.name for t in _drive_threads() if t.is_alive()], [])
        self.assertFalse((self.sd / "run.lock").exists())
        self.assertFalse((self.sd / "STOP").exists())

    def test_interrupted_wait_leaves_the_kill_switch_and_says_how_to_clear_it(self) -> None:
        sp = self._script(GOOD_TEST)
        abort = _AbortAfter(lambda: True)

        def _interrupted(_thread) -> None:
            raise KeyboardInterrupt

        err = io.StringIO()
        with mock.patch.object(driver_mod, "_parse_board_state", abort), \
                mock.patch.object(driver_mod, "_wait_for_run_thread", _interrupted), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(RuntimeError) as caught:
                drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.sd),
                          agent="claude", seat="both", card_ids=["CARD-1"], script=sp)
        notes = "\n".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("openfarm resume --clear-kill", notes)
        self.assertIn("left in place", err.getvalue())
        self.assertTrue((self.sd / "STOP").exists(), "the kill switch must stay while the run may still be going")
        members = read_json(self.sd / "runs" / self._only_run_id() / "board" / "members.json", {})
        self.assertEqual(members["driver-coord"]["state"], "removed")
        self.assertEqual(members["driver-sup"]["state"], "removed")
        for t in _drive_threads():
            t.join(120)
        self.assertFalse((self.sd / "run.lock").exists())
        (self.sd / "STOP").unlink()

    def test_normal_drive_marks_the_driver_seats_removed(self) -> None:
        sp = self._script(GOOD_TEST)
        res = drive_run(self.toy.root, load_config(self.toy.root), StateDir(self.sd),
                        agent="claude", seat="both", card_ids=["CARD-1"], script=sp)
        self._assert_nothing_left(res["run_id"])
        self.assertNotIn("cleanup_problems", res)


class RunLockHolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-lock-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = StateDir(self.tmp / "state")

    def _write_lock(self, data: dict) -> None:
        self.state.run_lock.write_text(json.dumps(data), encoding="utf-8")

    def test_lock_records_pid_host_and_start(self) -> None:
        self.assertTrue(self.state.acquire_run_lock("run-a"))
        holder = self.state.lock_holder()
        self.assertEqual(holder["run_id"], "run-a")
        self.assertEqual(holder["pid"], os.getpid())
        self.assertEqual(holder["host"], platform.node())
        self.assertTrue(holder["started_at"])
        self.assertIsNone(self.state.last_lock_takeover)
        self.state.release_run_lock("run-a")
        self.assertFalse(self.state.run_lock.exists())

    def test_dead_holder_on_this_host_is_taken_over(self) -> None:
        dead = _dead_pid()
        self._write_lock({"run_id": "run-old", "started_at": "2026-11-17T01:00:00Z",
                          "pid": dead, "host": platform.node()})
        self.assertTrue(self.state.acquire_run_lock("run-new"))
        self.assertEqual(self.state.lock_holder()["run_id"], "run-new")
        self.assertEqual(self.state.last_lock_takeover["run_id"], "run-old")
        self.assertEqual(self.state.last_lock_takeover["pid"], dead)

    def test_live_other_host_and_old_style_holders_are_refused(self) -> None:
        for data in (
            {"run_id": "run-live", "started_at": "x", "pid": os.getpid(), "host": platform.node()},
            {"run_id": "run-far", "started_at": "x", "pid": _dead_pid(), "host": platform.node() + "-elsewhere"},
            {"run_id": "run-v07", "started_at": "x"},
            {"run_id": "run-odd", "started_at": "x", "pid": "12", "host": platform.node()},
        ):
            with self.subTest(holder=data["run_id"]):
                self._write_lock(data)
                self.assertFalse(self.state.acquire_run_lock("run-new"))
                self.assertEqual(self.state.lock_holder()["run_id"], data["run_id"])
                self.assertIsNone(self.state.last_lock_takeover)

    def test_run_command_takes_over_a_dead_lock_with_a_note(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix", unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="push on a full bundle raises `FullError`.")
        toy.commit("add CARD-1")
        lock = toy.state_dir / "run.lock"
        lock.write_text(json.dumps({"run_id": "run-crashed", "started_at": "2026-11-17T01:00:00Z",
                                    "pid": _dead_pid(), "host": platform.node()}), encoding="utf-8")
        rc, out, err = _run_cli("--repo", str(toy.root), "--json", "run", "--dry-run", "--card", "CARD-1")
        self.assertEqual(rc, EXIT_OK, err or out)
        self.assertIn("took over run.lock from run 'run-crashed'", err)
        self.assertFalse(lock.exists())

        # A live holder is still refused, and its lock is left alone.
        lock.write_text(json.dumps({"run_id": "run-busy", "started_at": "x",
                                    "pid": os.getpid(), "host": platform.node()}), encoding="utf-8")
        rc2, out2, _ = _run_cli("--repo", str(toy.root), "--json", "run", "--dry-run", "--card", "CARD-1")
        self.assertEqual(rc2, EXIT_REFUSED)
        self.assertIn("run-busy", out2)
        self.assertTrue(lock.exists())

    def test_run_command_releases_the_lock_when_the_run_raises(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        with mock.patch("openfarm_core.cli._do_run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cli_main(["--repo", str(toy.root), "--json", "run", "--dry-run"])
        self.assertFalse((toy.state_dir / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
