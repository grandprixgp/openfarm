"""U3: long commands started from the TUI run off the screen thread.

In v0.8 every command typed in the TUI ran on the screen thread, so a `run` or a
`board wait` froze the display until it ended.
"""

from __future__ import annotations

import curses
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import tui  # noqa: E402
from openfarm_core.board import Board, board_dir_for  # noqa: E402
from openfarm_core.state import StateDir, read_json  # noqa: E402
from tests.test_v081_cli_toy import SWARM_ON, make_toy, write_script  # noqa: E402

RUN_ID = "run-tui-bg"


def _wait_until(fn, timeout: float = 60.0, step: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return None


class FakeStdscr:
    """Just enough of a curses window for `_curses_main`."""

    def __init__(self, keys, on_key=None) -> None:
        self.keys = list(keys)
        self.on_key = on_key
        self.drawn: list[str] = []
        self.timeouts: list[int] = []

    def erase(self) -> None:
        return None

    def keypad(self, flag: bool) -> None:
        return None

    def timeout(self, ms: int) -> None:
        self.timeouts.append(ms)

    def getmaxyx(self) -> tuple[int, int]:
        return (30, 160)

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        self.drawn.append(text[:n])

    def refresh(self) -> None:
        return None

    def getch(self) -> int:
        if self.on_key is not None:
            self.on_key(self)
        if self.keys:
            key = self.keys.pop(0)
            if key == "sleep":
                time.sleep(0.2)
                return curses.ERR
            return key
        time.sleep(0.05)
        return curses.ERR


class LongCommandClassTests(unittest.TestCase):
    def test_which_commands_run_in_the_background(self) -> None:
        for line in ("run --cards 3", "run --dry-run", "drive --agent claude", "supervise --triage-blocked",
                     "bench run --dry-run", "board wait --as x", "swarm wait",
                     "check CARD-1 --baseline", "openfarm run --cards 1"):
            self.assertTrue(tui.is_long_command(line), line)
        for line in ("status", "check CARD-1", "board read", "board post --as a --to b --body c",
                     "swarm status", "bench list", "stop", "pause", ""):
            self.assertFalse(tui.is_long_command(line), line)


class BoardWaitInBackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy, _script = make_toy(1, extra_config=SWARM_ON)
        self.addCleanup(self.toy.cleanup)
        self.state = StateDir(self.toy.state_dir)
        self.board = Board(board_dir_for(self.state, RUN_ID), state_dir=self.state)
        self.board.ensure()
        self.board.upsert_member("gates", kind="code", role="gates", added_by="system")
        self.board.upsert_member("watcher", kind="external", role="supervisor",
                                 added_by="system")
        self.ctrl = tui.TuiController(self.toy.root, run_id=RUN_ID)
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        if self.ctrl.job_running():
            self.ctrl.job.shutdown(grace_s=10)

    def test_board_wait_does_not_block_and_reports_its_exit(self) -> None:
        screen_out = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = screen_out
        self.addCleanup(setattr, sys, "stdout", real_stdout)
        t0 = time.monotonic()
        res = self.ctrl.execute_command(f"board wait --run {RUN_ID} --as watcher --timeout 60s")
        self.assertLess(time.monotonic() - t0, 3.0, "the command bar waited for the command")
        self.assertTrue(res["background"])
        self.assertIsNone(res["exit_code"])
        self.assertTrue(self.ctrl.job_running())
        self.assertIn("running", self.ctrl.job_status_text())

        # The views keep refreshing and short commands still answer meanwhile.
        before = self.ctrl.snapshot.get("generated_at")
        self.ctrl.refresh()
        self.assertTrue(self.ctrl.snapshot)
        status = self.ctrl.execute_command("status")
        self.assertEqual(status["exit_code"], 0)
        self.ctrl.close_modal()
        self.assertTrue(self.ctrl.job_running(), before)

        # A second long command is refused with a clear message; the first runs on.
        second = self.ctrl.execute_command("run --cards 1")
        self.assertEqual(second["exit_code"], 1)
        self.assertTrue(second.get("refused"))
        self.assertIn("still running", second["output"])
        self.assertIn("board wait", second["output"])
        self.assertTrue(self.ctrl.job_running())

        # Ending the board wakes the waiter; the TUI then reports the exit code.
        self.board.end_run("finished")
        done = self.ctrl.wait_for_job(30)
        self.assertFalse(self.ctrl.job_running())
        self.assertEqual(done["exit_code"], 0, done["output"])
        self.assertIn("finished (exit 0)", self.ctrl.job_status_text())
        self.assertIn("finished (exit 0)", self.ctrl.status_message)
        self.assertIn("run-ended", "\n".join(self.ctrl.modal_lines))
        # Nothing the command printed reached the screen's own stream, and the
        # stream is the screen's again once the command has been reported.
        self.assertEqual(screen_out.getvalue(), "")
        self.assertIs(sys.stdout, screen_out)

    def test_a_failed_command_shows_its_exit_code_and_message(self) -> None:
        self.ctrl.execute_command(f"board wait --run {RUN_ID} --as nobody --timeout 5s")
        done = self.ctrl.wait_for_job(30)
        self.assertEqual(done["exit_code"], 1, done["output"])
        text = self.ctrl.job_status_text()
        self.assertIn("failed (exit 1)", text)
        self.assertIn("nobody", text)

    def test_quit_asks_then_interrupts_a_waiting_command(self) -> None:
        self.ctrl.execute_command(f"board wait --run {RUN_ID} --as watcher --timeout 120s")
        self.assertTrue(self.ctrl.job_running())
        question = self.ctrl.quit_question()
        self.assertIn("still running", question)
        self.assertIn("[y/N]", question)

        # `q` then `n`: nothing stops; `q` then `y`: the command is interrupted and
        # the loop ends once it has.
        screen = FakeStdscr([ord("q"), ord("n"), "sleep", ord("q"), ord("y")])
        with mock.patch("curses.curs_set", return_value=0), \
                mock.patch.object(tui, "_init_curses_theme", return_value={}):
            rc = tui._curses_main(screen, self.ctrl)
        self.assertEqual(rc, 0)
        self.assertFalse(self.ctrl.job_running())
        self.assertEqual(self.ctrl.job.stop_requested, "interrupt")
        self.assertEqual(self.ctrl.job.exit_code, 3)  # `openfarm` exits 3 on an interrupt
        joined = "\n".join(screen.drawn)
        self.assertIn("still running", joined)
        self.assertIn("▶ board wait", joined)


SLOW_TEST = (
    "import time, unittest\n"
    "class Slow(unittest.TestCase):\n"
    "    def test_slow(self):\n"
    "        time.sleep(1.5)\n"
)


class RunInBackgroundTests(unittest.TestCase):
    def test_quitting_stops_a_run_with_the_kill_switch_and_leaves_no_lock(self) -> None:
        toy, script = make_toy(3)
        self.addCleanup(toy.cleanup)
        # A slow suite keeps the run busy long enough to stop it mid-way.
        toy.write("tests/test_slow.py", SLOW_TEST)
        toy.commit("slow suite")
        sp = write_script(script, toy.root.parent / "script-bg")
        state = StateDir(toy.state_dir)
        ctrl = tui.TuiController(toy.root)
        self.addCleanup(lambda: ctrl.job_running() and ctrl.job.shutdown(grace_s=10))

        t0 = time.monotonic()
        res = ctrl.action_launch_run(cards=3, script=str(sp))
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(res["background"])
        holder = _wait_until(lambda: (state.lock_holder() or {}).get("run_id"), 60)
        self.assertTrue(holder, "the run never took its lock")
        # Stop it while the first card is in flight, not before it starts.
        self.assertTrue(_wait_until(lambda: (state.cards / "CARD-1").is_dir(), 60))

        self.assertIn("kill switch", ctrl.quit_question())
        how = ctrl.stop_job()
        self.assertEqual(how, "kill-switch")
        kill_path = state.kill_switch(None)
        self.assertTrue(state.kill_requested(None))
        self.assertTrue(ctrl.quit_pending)
        self.assertIn("stopping", ctrl.job_status_text())

        ctrl.wait_for_job(180)
        self.assertFalse(ctrl.job_running())
        self.assertTrue(ctrl.ready_to_quit())
        # Clean: the run released its lock, wrote its record, and the kill switch
        # this TUI set is cleared again.
        self.assertFalse(state.lock_holder())
        self.assertFalse(kill_path.exists())
        record = read_json(state.run_dir(holder) / "run.json", {})
        self.assertTrue(record, "the run wrote no record")
        first = record["outcomes"][0]
        self.assertEqual(first.get("card_id"), "CARD-1", record["outcomes"])
        self.assertEqual((first.get("final_state"), first.get("stop_code")),
                         ("blocked", "killed"), first)
        self.assertNotIn("in-review", [o.get("final_state") for o in record["outcomes"]])
        # The run's own summary is kept as the last result (no modal: the TUI is quitting).
        self.assertIn("killed", ctrl.last_command_result["output"])
        self.assertEqual(ctrl.last_command_result["exit_code"], 0)

    def test_a_kill_switch_set_by_someone_else_is_left_alone(self) -> None:
        toy, script = make_toy(1)
        self.addCleanup(toy.cleanup)
        state = StateDir(toy.state_dir)
        state.request_kill(None, "operator")
        ctrl = tui.TuiController(toy.root)
        ctrl.action_launch_run(cards=1, script=str(write_script(script, toy.root.parent / "s")))
        ctrl.stop_job()
        res = ctrl.wait_for_job(60)
        self.assertEqual(res["exit_code"], 1, res["output"])  # refused: the kill switch exists
        self.assertTrue(state.kill_requested(None))
        self.assertEqual(read_json(state.kill_switch(None), {}).get("reason"), "operator")


if __name__ == "__main__":
    unittest.main()
