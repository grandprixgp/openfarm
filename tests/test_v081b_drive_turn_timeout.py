"""v0.8.1 fix wave, unit drive, D6: the driver turn's time limit and its children.

`openfarm drive --turn-timeout` sets how long one driver turn may take (180 s
by default). The driver CLI runs in its own process group, and the whole group
is ended on a timeout, on an abort, and after a normal exit, before the
isolated HOME it ran in is deleted.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import driver as driver_mod
from openfarm_core.cli import EXIT_USAGE, main as cli_main
from openfarm_core.driver import DriverAgent
from openfarm_core.errors import Infra

_FAKE = """#!{python}
import os, subprocess, sys, time
from pathlib import Path
out = Path({out!r})
child = subprocess.Popen(
    [sys.executable, "-c",
     "import os, sys, time; from pathlib import Path; "
     "Path(sys.argv[1]).write_text(os.environ.get('HOME', '')); time.sleep(120)",
     str(out / "child-home")],
    stdin=subprocess.DEVNULL, stdout={child_out}, stderr={child_out},
)
(out / "child.pid").write_text(str(child.pid))
(out / "cli-home").write_text(os.environ.get("HOME", ""))
for _ in range(200):
    if (out / "child-home").exists():
        break
    time.sleep(0.02)
if {hang}:
    time.sleep(120)
print('{{"type": "result", "is_error": false, "result": "{{}}"}}')
"""


def _gone(pid: int, wait_s: float = 5.0) -> bool:
    """True once `pid` no longer runs (gone, or a zombie waiting to be reaped)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        if stat.rsplit(")", 1)[1].split()[0] in ("Z", "X"):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class TurnTimeoutProcessGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-turn-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._kill_leftovers)

    def _kill_leftovers(self) -> None:
        pid_file = self.tmp / "out" / "child.pid"
        if pid_file.exists():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid_file.read_text()), 9)

    def _fake(self, *, hang: bool, child_keeps_pipes: bool) -> Path:
        out = self.tmp / "out"
        out.mkdir(exist_ok=True)
        script = self.tmp / "claude"
        script.write_text(_FAKE.format(
            python=sys.executable, out=str(out), hang=hang,
            child_out="None" if child_keeps_pipes else "subprocess.DEVNULL",
        ), encoding="utf-8")
        script.chmod(0o755)
        return script

    def _agent(self, script: Path, timeout_s: float) -> DriverAgent:
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        return DriverAgent(agent="claude", cwd=work, driver_bin=str(script), timeout_s=timeout_s)

    def _child(self) -> int:
        return int((self.tmp / "out" / "child.pid").read_text())

    def test_timeout_ends_the_whole_group_before_home_is_deleted(self) -> None:
        agent = self._agent(self._fake(hang=True, child_keeps_pipes=True), timeout_s=1.5)
        started = time.monotonic()
        with self.assertRaises(Infra) as caught:
            agent.complete("hello", turn_key="driver/plan")
        self.assertLess(time.monotonic() - started, 30, "the turn ran past its limit")
        self.assertEqual(caught.exception.code, "driver-timeout")
        self.assertIn("1.5s", str(caught.exception))
        self.assertIn("--turn-timeout", str(caught.exception))
        self.assertTrue(_gone(self._child()), "the CLI's child outlived the timed-out turn")
        home = (self.tmp / "out" / "cli-home").read_text()
        self.assertTrue(home)
        self.assertFalse(Path(home).exists(), "the isolated HOME was left behind")

    def test_children_left_after_a_normal_exit_are_ended(self) -> None:
        agent = self._agent(self._fake(hang=False, child_keeps_pipes=False), timeout_s=60)
        with contextlib.suppress(Infra):
            agent.complete("hello", turn_key="driver/plan")
        self.assertTrue(_gone(self._child()), "a child the CLI left running was not ended")

    def test_an_abort_mid_turn_ends_the_group(self) -> None:
        agent = self._agent(self._fake(hang=True, child_keeps_pipes=True), timeout_s=60)
        real = subprocess.Popen.communicate
        calls = {"n": 0}

        def interrupted(proc, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                deadline = time.monotonic() + 10
                while not (self.tmp / "out" / "child.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                raise KeyboardInterrupt
            return real(proc, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, "communicate", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                agent.complete("hello", turn_key="driver/plan")
        self.assertTrue(_gone(self._child()), "an aborted turn left the CLI's child running")
        home = (self.tmp / "out" / "cli-home").read_text()
        self.assertFalse(Path(home).exists())


class TurnTimeoutOptionTests(unittest.TestCase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_option_reaches_the_driver_and_defaults_to_the_supervisor_wait(self) -> None:
        self.assertEqual(DriverAgent(agent="claude").timeout_s, 180.0)
        seen: list[float] = []

        def fake_drive_run(*args, **kwargs):
            seen.append(kwargs["turn_timeout"])
            return {"ok": True, "agent": "claude", "seat": "both", "run_id": "r", "outcomes": [],
                    "reviews": [], "actions_taken": [], "answers_sent": []}

        from tests.test_python_e2e import build_python_toy
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        with mock.patch.object(driver_mod, "drive_run", fake_drive_run):
            self._cli("--repo", str(toy.root), "--json", "drive", "--turn-timeout", "7.5")
            self._cli("--repo", str(toy.root), "--json", "drive")
        # With no flag, drive_run fills it in from [micromanager] timeout_s, never
        # under 180 (v0.8.5: the wait an ACP supervisor gets).
        self.assertEqual(seen, [7.5, None])
        from openfarm_core.config import load_config
        from openfarm_core.driver import drive_defaults

        cfg = load_config(toy.root)
        self.assertEqual(drive_defaults(cfg, "claude", None, None, None)[2],
                         max(180.0, float(cfg.micromanager.timeout_s)))

    def test_bad_values_are_refused(self) -> None:
        from tests.test_python_e2e import build_python_toy
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        for bad in ("0", "-3", "inf", "nan"):
            with self.subTest(value=bad):
                rc, text = self._cli("--repo", str(toy.root), "--json", "drive", "--turn-timeout", bad)
                self.assertEqual(rc, EXIT_USAGE, text)
                self.assertIn("--turn-timeout", text)


if __name__ == "__main__":
    unittest.main()
