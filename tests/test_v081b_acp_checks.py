"""A5, A8, A4, A6, A3, A9: provider checks, permission roots, spend, probes, agy limits."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core.acp_setup import (
    _safe_codex_auth_status,
    inspect_acp_status,
    probe_acp,
)
from openfarm_core.adapters.acp import (
    AGY_NATIVE_ACP,
    CODEX_NATIVE_ACP,
    ClaudeAcpAdapter,
    resolve_acp_command,
)
from openfarm_core.adapters.claude_worker import ClaudeAcpWorkerBackend
from openfarm_core.bootstrap import install_skill_bundle
from openfarm_core.cli import _backend_prices_spend
from openfarm_core.config import Config
from openfarm_core.errors import AdapterError, Invalid, UsageError
from openfarm_core.micromanager import open_micromanager

STAND_IN = ROOT / "tests" / "acp_stand_in.py"
TEST_CONFINE = "auto" if confine_lib.bwrap_status()[0] else "off"


def _script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _base_config(state_dir: Path) -> dict:
    return {
        "project": {"name": "demo", "state_dir": str(state_dir)},
        "adapters": {"languages": ["python"], "model": "openai-compatible", "micromanager": "off"},
        "model": {
            "endpoint": "https://api.example.invalid/v1",
            "allowed_endpoints": ["https://api.example.invalid/v1"],
            "implementer": {"model": "lesser-coder"},
        },
        "policy": {"categories": ["pattern"]},
    }


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081b-chk-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()


class ProviderMismatchTests(_Tmp):
    """A5: one resolved provider; a binary naming another one is refused."""

    def test_adapter_refuses_a_codex_binary_for_the_claude_provider(self) -> None:
        codex = _script(self.bin / "codex", "#!/bin/sh\nexit 0\n")
        for command in ("codex", str(codex), ["agy", "acp"], [sys.executable, str(CODEX_NATIVE_ACP)]):
            with self.assertRaises(Invalid) as caught:
                ClaudeAcpAdapter(command=command, provider="claude", cwd=self.tmp)
            self.assertEqual(caught.exception.code, "acp-provider-mismatch")
            self.assertIn("claude", caught.exception.message)
        with self.assertRaises(Invalid) as caught:
            ClaudeAcpAdapter(command="claude", provider="codex", cwd=self.tmp)
        self.assertIn("codex", caught.exception.message)
        self.assertIn("'claude'", caught.exception.message)

    def test_resolve_no_longer_switches_provider_silently(self) -> None:
        with self.assertRaises(Invalid):
            resolve_acp_command("codex", provider="claude")
        # Generic commands (a stand-in, an absolute path not named for a provider) still work.
        argv = resolve_acp_command([sys.executable, str(STAND_IN)], provider="claude")
        self.assertEqual(argv[1], str(STAND_IN))
        self.assertEqual(resolve_acp_command("auto", provider="codex")[1], str(CODEX_NATIVE_ACP))

    def test_open_micromanager_refuses_at_open(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        state = self.tmp / "state"
        state.mkdir()
        data = _base_config(state)
        data["adapters"]["micromanager"] = "claude-acp"
        data["micromanager"] = {"mode": "active", "binary": "codex"}
        cfg = Config(data, repo)
        with self.assertRaises(Invalid) as caught:
            open_micromanager(cfg, state, repo, worktree=repo)
        self.assertIn("[micromanager] binary", caught.exception.message)


class SessionRootPermissionTests(_Tmp):
    """A8: a read is judged against the session's own folder, not the adapter's."""

    def test_reads_are_judged_against_the_session_folder(self) -> None:
        adapter_root = self.tmp / "adapter-root"
        (adapter_root / "src").mkdir(parents=True)
        (adapter_root / "src" / "only-here.rs").write_text("x")
        card = self.tmp / "card-wt"
        (card / "src").mkdir(parents=True)
        (card / "src" / "main.rs").write_text("fn main() {}")
        (card / "seams").mkdir()
        (card / "seams" / "core.rs").write_text("x")

        def run(path: str) -> dict:
            cmd = [sys.executable, str(STAND_IN), "--mode", "hostile-perm",
                   "--perm-kind", "read", "--perm-path", path]
            with ClaudeAcpAdapter(cmd, cwd=adapter_root, provider="claude", confine="off",
                                  never_touch=["seams/**"]) as acp:
                sid = acp.new_session(cwd=card, tool_policy="read-only")
                return acp.prompt(sid, "go").permission_events[0]

        self.assertEqual(run(str(card / "src" / "main.rs"))["decision"], "allow_once")
        self.assertEqual(run("src/main.rs")["decision"], "allow_once")
        outside = run(str(adapter_root / "src" / "only-here.rs"))
        self.assertEqual(outside["decision"], "reject_once")
        self.assertEqual(outside["reason"], "outside-allowed-root")
        self.assertEqual(run("seams/core.rs")["reason"], "never-touch")

    def test_without_a_session_folder_the_adapter_root_is_used(self) -> None:
        root = self.tmp / "root"
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.rs").write_text("x")
        acp = ClaudeAcpAdapter([sys.executable, str(STAND_IN)], cwd=root, provider="claude")
        self.addCleanup(acp.close)
        self.assertTrue(acp._is_path_allowed_readonly("src/a.rs")[0])
        self.assertFalse(acp._is_path_allowed_readonly(str(self.tmp / "elsewhere"))[0])


class StatusModeTests(_Tmp):
    def test_off_mode_says_so_in_status(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".openfarm").mkdir(parents=True)
        cfg_text = (
            '[project]\nname = "demo"\nstate_dir = "%s"\n'
            '[adapters]\nlanguages = ["python"]\nmodel = "openai-compatible"\nmicromanager = "codex-acp"\n'
            '[model]\nendpoint = "https://api.example.invalid/v1"\n'
            'allowed_endpoints = ["https://api.example.invalid/v1"]\n'
            '[model.implementer]\nmodel = "m"\n'
            '[policy]\ncategories = ["pattern"]\n'
            '[micromanager]\nmode = "active"\nconfine = "off"\n'
        ).replace("%s", str(self.tmp / "state"))
        (repo / ".openfarm" / "config.toml").write_text(cfg_text)
        codex = _script(self.bin / "codex", "#!/bin/sh\necho 'Not logged in'\nexit 1\n")
        status = inspect_acp_status(repo, provider="codex", home=self.tmp / "home",
                                    codex_bin=str(codex), codex_acp_bin="", env={})
        self.assertEqual(status["confinement"]["mode"], "off")
        self.assertEqual(status["providers"]["codex"]["confinement"]["none"], "flags")
        self.assertIn("can read whatever your user can", status["hint"])


if __name__ == "__main__":
    unittest.main()
