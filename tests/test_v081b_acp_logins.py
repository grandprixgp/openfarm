"""A7 and A2: where logins live, and copying a refreshed login back safely."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core import logins
from openfarm_core.adapters.acp import CODEX_NATIVE_ACP, ClaudeAcpAdapter
from openfarm_core.bootstrap import install_skill_bundle

STAND_IN = ROOT / "tests" / "acp_stand_in.py"
TEST_CONFINE = "auto" if confine_lib.bwrap_status()[0] else "off"

CLAUDE_CRED = {"claudeAiOauth": {"accessToken": "at-1", "refreshToken": "rt-1",
                                 "expiresAt": 1000}}
CODEX_AUTH = {"auth_mode": "chatgpt", "last_refresh": "2026-11-01T00:00:00Z",
              "tokens": {"access_token": "cx-1", "refresh_token": "crt-1", "account_id": "acct-1"}}


def _codex_writer(path: Path, payload: str) -> None:
    """A fake codex CLI that writes `payload` as its own auth.json, then answers."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "home = Path(os.environ['CODEX_HOME'])\n"
        "body = {'codex_home': str(home), 'home': os.environ.get('HOME', '')}\n"
        f"payload = {payload!r}\n"
        "if payload == 'SYMLINK':\n"
        "    (home / 'auth.json').unlink()\n"
        "    (home / 'auth.json').symlink_to(os.environ['OF_TEST_SECRET'])\n"
        "elif payload:\n"
        "    (home / 'auth.json').write_text(payload)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(body)}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


class LoginPlaceTests(unittest.TestCase):
    def test_environment_first_home_second(self) -> None:
        home = Path("/h")
        self.assertEqual(logins.login_place("claude", env={"HOME": "/h"}).credential,
                         home / ".claude" / ".credentials.json")
        self.assertEqual(logins.login_place("claude", env={"HOME": "/h"}).meta, home / ".claude.json")
        place = logins.login_place("claude", env={"HOME": "/h", "CLAUDE_CONFIG_DIR": "/c"})
        self.assertEqual(place.credential, Path("/c/.credentials.json"))
        self.assertEqual(place.meta, Path("/c/.claude.json"))
        self.assertEqual(place.from_env, "CLAUDE_CONFIG_DIR")
        self.assertEqual(logins.login_place("codex", env={"HOME": "/h"}).credential,
                         home / ".codex" / "auth.json")
        self.assertEqual(logins.login_place("codex", env={"HOME": "/h", "CODEX_HOME": "/x"}).credential,
                         Path("/x/auth.json"))
        # An explicit home alone looks in that home, not this process's variables.
        self.assertEqual(logins.login_place("codex", home="/h").credential, home / ".codex" / "auth.json")

    def test_isolated_child_variables_point_inside_the_isolated_home(self) -> None:
        self.assertEqual(logins.isolated_login_env("claude", "/iso"), {"CLAUDE_CONFIG_DIR": "/iso/.claude"})
        self.assertEqual(logins.isolated_login_env("codex", "/iso"), {"CODEX_HOME": "/iso/.codex"})
        self.assertEqual(logins.isolated_login_env("antigravity", "/iso"), {})


class AdapterLoginTests(unittest.TestCase):
    """The adapter reads, isolates and writes back through `logins`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-alog-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.host_home = self.tmp / "host-home"
        self.host_home.mkdir()
        self.codex_home = self.tmp / "custom-codex-home"
        self.codex_home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.secret = self.tmp / "id_ed25519"
        self.secret.write_text("PRIVATE KEY\n")

    def _run(self, payload: str, env: dict) -> dict:
        codex = self.bin / "codex"
        _codex_writer(codex, payload)
        with ClaudeAcpAdapter(
            [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(codex)],
            cwd=self.work, provider="codex", auth_mode="oauth", env=env, confine=TEST_CONFINE,
        ) as acp:
            sid = acp.new_session(cwd=self.work, tool_policy="none")
            body = json.loads(acp.prompt(sid, "hi", timeout_s=60).text)
            iso = acp._isolated_home
        body["iso"] = str(iso)
        return body

    def test_codex_home_is_read_isolated_and_written_back(self) -> None:
        (self.codex_home / "auth.json").write_text(json.dumps(CODEX_AUTH))
        # The HOME copy must not be used when CODEX_HOME names another folder.
        (self.host_home / ".codex").mkdir()
        (self.host_home / ".codex" / "auth.json").write_text('{"tokens": {"access_token": "HOME-COPY"}}')
        refreshed = json.loads(json.dumps(CODEX_AUTH))
        refreshed["tokens"]["access_token"] = "cx-refreshed"
        refreshed["last_refresh"] = "2026-11-03T00:00:00Z"
        env = {"HOME": str(self.host_home), "CODEX_HOME": str(self.codex_home)}
        body = self._run(json.dumps(refreshed), env)
        self.assertEqual(body["codex_home"], str(Path(body["iso"]) / ".codex"))
        self.assertTrue(body["codex_home"].startswith(body["iso"]))
        self.assertEqual(json.loads((self.codex_home / "auth.json").read_text())["tokens"]["access_token"],
                         "cx-refreshed")
        self.assertIn("HOME-COPY", (self.host_home / ".codex" / "auth.json").read_text())

    def test_claude_config_dir_is_read_and_written_back(self) -> None:
        cfg_dir = self.tmp / "claude-profile"
        cfg_dir.mkdir()
        (cfg_dir / ".credentials.json").write_text(json.dumps(CLAUDE_CRED))
        (cfg_dir / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
                                                          "oauthAccount": {"emailAddress": "x"}}))
        env = {"HOME": str(self.host_home), "CLAUDE_CONFIG_DIR": str(cfg_dir)}
        acp = ClaudeAcpAdapter([sys.executable, str(STAND_IN)], cwd=self.work, provider="claude",
                               auth_mode="oauth", env=env)
        try:
            with mock.patch("openfarm_core.adapters.acp.subprocess.Popen",
                            wraps=subprocess.Popen) as popen:
                acp.initialize()
            child_env = popen.call_args.kwargs["env"]
            iso = acp._isolated_home
            self.assertEqual(child_env.get("CLAUDE_CONFIG_DIR"), str(iso / ".claude"))
            self.assertEqual(json.loads((iso / ".claude" / ".credentials.json").read_text()), CLAUDE_CRED)
            new = {"claudeAiOauth": {"accessToken": "at-2", "refreshToken": "rt-2", "expiresAt": 5000}}
            (iso / ".claude" / ".credentials.json").write_text(json.dumps(new))
        finally:
            acp.close()
        self.assertEqual(json.loads((cfg_dir / ".credentials.json").read_text()), new)
        self.assertFalse((self.host_home / ".claude").exists())


class InstallSkillTargetTests(unittest.TestCase):
    def test_codex_target_honours_codex_home(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-v081b-skill-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        codex_home = tmp / "codex-home"
        claude_dir = tmp / "claude-dir"
        old = {k: os.environ.get(k) for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "HOME")}
        os.environ["CODEX_HOME"] = str(codex_home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
        os.environ["HOME"] = str(tmp / "home")

        def restore() -> None:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(restore)
        res = install_skill_bundle(target="all")
        dests = " ".join(str(d) for d in (res.get("destinations") or res.get("installed") or [res]))
        self.assertTrue((codex_home / "skills").is_dir(), dests)
        self.assertTrue((claude_dir / "skills").is_dir(), dests)
        self.assertFalse((tmp / "home" / ".codex").exists())
        self.assertFalse((tmp / "home" / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
