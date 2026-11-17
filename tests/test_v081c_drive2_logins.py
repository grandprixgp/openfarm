"""J2: the driver finds logins where they live and copies a refreshed one back safely."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core import driver as driver_mod
from openfarm_core.driver import DriverAgent

# The boundary when bwrap works here, as in a real drive; nothing otherwise.
TEST_CONFINE = "auto" if confine_lib.bwrap_status()[0] else "off"

_CODEX_OLD = {"auth_mode": "chatgpt", "last_refresh": "2026-11-01T00:00:00Z",
              "tokens": {"access_token": "OLD-AT", "refresh_token": "OLD-RT", "account_id": "acct-1"}}
_CODEX_NEW = {"auth_mode": "chatgpt", "last_refresh": "2026-11-17T00:00:00Z",
              "tokens": {"access_token": "NEW-AT", "refresh_token": "NEW-RT", "account_id": "acct-1"}}
_CLAUDE_OLD = {"claudeAiOauth": {"accessToken": "OLD-AT", "refreshToken": "OLD-RT",
                                 "expiresAt": 1000}}
_CLAUDE_NEW = {"claudeAiOauth": {"accessToken": "NEW-AT", "refreshToken": "NEW-RT",
                                 "expiresAt": 2000}}

# A fake CLI: reports where its login variables point and what it found there,
# then does what ACTION says to its isolated login (refresh it, write junk, or
# swap it for a symlink to another of the user's files).
_FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
provider = PROVIDER
action = ACTION
args = sys.argv[1:]
if provider != "antigravity":
    sys.stdin.read()
home = Path(os.environ["HOME"])
if provider == "codex":
    cred = Path(os.environ["CODEX_HOME"]) / "auth.json"
elif provider == "claude":
    cred = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
else:
    cred = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
seen = {"home": str(home), "codex_home": os.environ.get("CODEX_HOME", ""),
        "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR", ""),
        "cred": str(cred), "had_login": cred.is_file(),
        "login": cred.read_text() if cred.is_file() else ""}
if action == "refresh":
    cred.write_text(NEW)
elif action == "junk":
    cred.write_text("{}")
elif action == "symlink":
    cred.unlink()
    cred.symlink_to(TARGET)
body = json.dumps(seen)
if provider == "codex":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": body}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
elif provider == "antigravity":
    print(json.dumps({"response": body, "usage": {"promptTokenCount": 1, "candidatesTokenCount": 1}}))
else:
    print(json.dumps({"type": "result", "result": body, "total_cost_usd": 0.001,
                      "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


class DriverLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-login-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host_home = self.tmp / "host-home"
        self.host_home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        # Another of the user's files a child must never get copied over a login.
        (self.host_home / ".ssh").mkdir()
        self.ssh_key = self.host_home / ".ssh" / "id_ed25519"
        self.ssh_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEY\n")

    def _fake(self, provider: str, action: str, new: str = "") -> Path:
        name = {"claude": "claude", "codex": "codex", "antigravity": "agy"}[provider]
        path = self.bin_dir / f"{name}-{action}" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(_FAKE.replace("PROVIDER", repr(provider)).replace("ACTION", repr(action))
                        .replace("NEW", repr(new)).replace("TARGET", repr(str(self.ssh_key))))
        path.chmod(0o755)
        return path

    def _turn(self, provider: str, action: str, env: dict, new: str = "") -> tuple[DriverAgent, dict]:
        drv = DriverAgent(agent=provider, cwd=self.work,
                          driver_bin=str(self._fake(provider, action, new)),
                          env={"PATH": os.environ.get("PATH", "/usr/bin"), **env},
                          confine=TEST_CONFINE)
        with drv:
            seen = json.loads(drv.complete("hi", system_prompt="S").text)
        return drv, seen

    def test_codex_home_is_read_and_a_refresh_written_back_there(self) -> None:
        codex_home = self.tmp / "codex-elsewhere"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps(_CODEX_OLD))
        drv, seen = self._turn("codex", "refresh",
                               {"HOME": str(self.host_home), "CODEX_HOME": str(codex_home)},
                               new=json.dumps(_CODEX_NEW))
        self.assertTrue(seen["had_login"], "the login in CODEX_HOME was copied in")
        self.assertEqual(json.loads(seen["login"]), _CODEX_OLD)
        self.assertTrue(seen["codex_home"].startswith(seen["home"]),
                        "the child's CODEX_HOME points inside its isolated home")
        self.assertNotEqual(seen["codex_home"], str(codex_home))
        self.assertEqual(json.loads((codex_home / "auth.json").read_text()), _CODEX_NEW)
        self.assertEqual(json.loads((codex_home / "auth.json.openfarm-backup").read_text()),
                         _CODEX_OLD)
        self.assertEqual(drv.last_sync.status, "synced")
        self.assertFalse((self.host_home / ".codex").exists(), "$HOME/.codex is not the place")
        self.assertEqual(drv.notes, [])

    def test_claude_config_dir_is_read_and_a_refresh_written_back_there(self) -> None:
        config_dir = self.tmp / "claude-elsewhere"
        config_dir.mkdir()
        (config_dir / ".credentials.json").write_text(json.dumps(_CLAUDE_OLD))
        drv, seen = self._turn("claude", "refresh",
                               {"HOME": str(self.host_home), "CLAUDE_CONFIG_DIR": str(config_dir)},
                               new=json.dumps(_CLAUDE_NEW))
        self.assertTrue(seen["had_login"])
        self.assertTrue(seen["claude_config_dir"].startswith(seen["home"]))
        self.assertEqual(json.loads((config_dir / ".credentials.json").read_text()), _CLAUDE_NEW)
        self.assertFalse((self.host_home / ".claude").exists())

    def test_junk_from_the_child_never_replaces_the_real_login(self) -> None:
        (self.host_home / ".codex").mkdir()
        real = self.host_home / ".codex" / "auth.json"
        real.write_text(json.dumps(_CODEX_OLD))
        drv, _seen = self._turn("codex", "junk", {"HOME": str(self.host_home)})
        self.assertEqual(json.loads(real.read_text()), _CODEX_OLD)
        self.assertEqual(drv.last_sync.status, "refused")
        self.assertEqual(len(drv.notes), 1)
        self.assertIn("was not copied back", drv.notes[0])
        self.assertIn(str(real), drv.notes[0])

    def test_a_symlinked_login_is_refused_and_its_target_never_copied(self) -> None:
        tok_dir = self.host_home / ".gemini" / "antigravity-cli"
        tok_dir.mkdir(parents=True)
        real = tok_dir / "antigravity-oauth-token"
        real.write_text("REAL-AGY-TOKEN\n")
        drv, _seen = self._turn("antigravity", "symlink", {"HOME": str(self.host_home)})
        self.assertEqual(real.read_text(), "REAL-AGY-TOKEN\n")
        self.assertFalse(real.is_symlink())
        self.assertEqual(drv.last_sync.status, "refused")
        self.assertIn("symlink", drv.notes[0])
        self.assertNotIn("FAKEKEY", " ".join(drv.notes), "a note never carries content")

    def test_the_old_copy_of_the_sync_code_is_gone(self) -> None:
        self.assertFalse(hasattr(driver_mod, "_sync_refreshed_oauth"))
        self.assertFalse(hasattr(driver_mod, "_cred_relpath_for"))


if __name__ == "__main__":
    unittest.main()
