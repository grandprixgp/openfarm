"""m10: a driver turn that times out or fails still tries to copy a refreshed login back.

The CLI may refresh its login and then run past `--turn-timeout`, or the wait
may be interrupted. Once its process group is ended, the same checked copy
(`logins.sync_back`) is tried before the isolated home is removed. A failure
there is a note, never a second error over the turn's own.
"""

from __future__ import annotations

import json
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

from openfarm_core import confine as confine_lib
from openfarm_core import logins
from openfarm_core.driver import DriverAgent
from openfarm_core.errors import Infra

# The boundary when bwrap works here, as in a real drive; nothing otherwise.
TEST_CONFINE = "auto" if confine_lib.bwrap_status()[0] else "off"

_OLD = {"auth_mode": "chatgpt", "last_refresh": "2026-11-01T00:00:00Z",
        "tokens": {"access_token": "OLD-AT", "refresh_token": "OLD-RT", "account_id": "acct-1"}}
_NEW = {"auth_mode": "chatgpt", "last_refresh": "2026-11-17T00:00:00Z",
        "tokens": {"access_token": "NEW-AT", "refresh_token": "NEW-RT", "account_id": "acct-1"}}

# A fake `codex`: writes LOGIN over its isolated login, then hangs past any
# turn timeout the tests give.
_FAKE = r'''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
sys.stdin.read()
cred = Path(os.environ["CODEX_HOME"]) / "auth.json"
cred.write_text(LOGIN)
time.sleep(60)
'''


class FailedTurnSyncBackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081d-seats-sync-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir()
        self.real = self.codex_home / "auth.json"
        self.real.write_text(json.dumps(_OLD))
        self.work = self.tmp / "work"
        self.work.mkdir()

    def _agent(self, login: str, timeout_s: float) -> DriverAgent:
        fake = self.tmp / "bin" / "codex"
        fake.parent.mkdir(exist_ok=True)
        fake.write_text(_FAKE.replace("LOGIN", repr(login)))
        fake.chmod(0o755)
        agent = DriverAgent(agent="codex", cwd=self.work, driver_bin=str(fake),
                            env={"PATH": os.environ.get("PATH", "/usr/bin"),
                                 "HOME": str(self.tmp / "host-home"),
                                 "CODEX_HOME": str(self.codex_home)},
                            timeout_s=timeout_s, confine=TEST_CONFINE)
        self.addCleanup(agent.close)
        return agent

    def _homes(self) -> set[str]:
        return {p.name for p in Path(tempfile.gettempdir()).glob("openfarm-acp-home-*")}

    def _refreshed_inside(self, before: set[str]) -> bool:
        """Whether the child has written the new login into its isolated home yet."""
        for name in self._homes() - before:
            for cred in (Path(tempfile.gettempdir()) / name).rglob("auth.json"):
                try:
                    if "NEW-RT" in cred.read_text():
                        return True
                except OSError:
                    continue
        return False

    def test_a_refresh_before_a_timeout_is_copied_back(self) -> None:
        agent = self._agent(json.dumps(_NEW), timeout_s=2.0)
        before = self._homes()
        with self.assertRaises(Infra) as caught:
            agent.complete("hello", turn_key="driver/plan")
        self.assertEqual(caught.exception.code, "driver-timeout")
        self.assertEqual(json.loads(self.real.read_text()), _NEW)
        self.assertEqual(agent.last_sync.status, "synced")
        self.assertEqual(self._homes() - before, set(), "the isolated home was left behind")

    def test_junk_before_a_timeout_is_still_refused(self) -> None:
        agent = self._agent("{}", timeout_s=2.0)
        with self.assertRaises(Infra):
            agent.complete("hello", turn_key="driver/plan")
        self.assertEqual(json.loads(self.real.read_text()), _OLD)
        self.assertEqual(agent.last_sync.status, "refused")
        self.assertTrue(any("was not copied back" in n for n in agent.notes), agent.notes)

    def test_a_refresh_before_an_interrupted_wait_is_copied_back(self) -> None:
        agent = self._agent(json.dumps(_NEW), timeout_s=60.0)
        real = subprocess.Popen.communicate
        calls = {"n": 0}
        before = self._homes()

        def interrupted(proc, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # The prompt goes in as the real wait would send it, then the
                # wait is interrupted once the child has refreshed its login.
                proc.stdin.write(kwargs.get("input") or "")
                proc.stdin.close()
                deadline = time.monotonic() + 20
                while not self._refreshed_inside(before) and time.monotonic() < deadline:
                    time.sleep(0.05)
                raise KeyboardInterrupt
            return real(proc, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, "communicate", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                agent.complete("hello", turn_key="driver/plan")
        self.assertEqual(json.loads(self.real.read_text()), _NEW)

    def test_a_failed_copy_after_a_timeout_is_a_note_not_a_second_error(self) -> None:
        agent = self._agent(json.dumps(_NEW), timeout_s=2.0)
        with mock.patch.object(logins, "sync_back", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(Infra) as caught:
                agent.complete("hello", turn_key="driver/plan")
        self.assertEqual(caught.exception.code, "driver-timeout")
        self.assertEqual(json.loads(self.real.read_text()), _OLD)
        self.assertTrue(any("RuntimeError: disk on fire" in n for n in agent.notes), agent.notes)

    def test_a_turn_refused_before_any_spawn_copies_nothing(self) -> None:
        agent = self._agent(json.dumps(_NEW), timeout_s=2.0)
        with mock.patch.object(logins, "sync_back") as sync, \
                mock.patch.object(DriverAgent, "child_argv",
                                  side_effect=Infra("boom", "no argv")):
            with self.assertRaises(Infra):
                agent.complete("hello", turn_key="driver/plan")
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
