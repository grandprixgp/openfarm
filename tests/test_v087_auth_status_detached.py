"""Auth-status checks must detach from the controlling terminal.

Running `claude auth status` or `codex login status` inside a background
process group (such as `timeout` or a systemd scope) without
`start_new_session=True` and `stdin=subprocess.DEVNULL` lets the child touch
the controlling TTY and stop the entire process group with SIGTTIN/SIGTTOU.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.acp_setup import _safe_claude_auth_status, _safe_codex_auth_status


class AuthStatusDetachedTests(unittest.TestCase):
    def test_claude_auth_status_detaches_from_tty(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true}', "")

        with mock.patch("openfarm_core.acp_setup.subprocess.run", side_effect=fake_run):
            res = _safe_claude_auth_status("claude", home=Path(tempfile.gettempdir()))
        self.assertTrue(res["loggedIn"])
        self.assertEqual(seen["kwargs"].get("stdin"), subprocess.DEVNULL)
        self.assertTrue(seen["kwargs"].get("start_new_session"))

    def test_codex_auth_status_detaches_from_tty(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT\n", "")

        with mock.patch("openfarm_core.acp_setup.subprocess.run", side_effect=fake_run):
            res = _safe_codex_auth_status("codex", home=Path(tempfile.gettempdir()))
        self.assertTrue(res["loggedIn"])
        self.assertEqual(seen["kwargs"].get("stdin"), subprocess.DEVNULL)
        self.assertTrue(seen["kwargs"].get("start_new_session"))


if __name__ == "__main__":
    unittest.main()
