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
