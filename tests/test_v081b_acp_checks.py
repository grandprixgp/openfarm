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


class SpendMeasuredTests(_Tmp):
    """A4: spend counts as measured only when the provider reports cost or rates are set."""

    def test_backend_prices_spend(self) -> None:
        for adapter, want in (("claude-acp", True), ("codex-acp", False), ("antigravity-acp", False)):
            cfg = types.SimpleNamespace(model_adapter=adapter, cost_per_1k_in=0.0, cost_per_1k_out=0.0)
            self.assertEqual(_backend_prices_spend(cfg, None), want, adapter)
            cfg.cost_per_1k_out = 0.002
            self.assertTrue(_backend_prices_spend(cfg, None), adapter)

    def test_worker_prices_spend(self) -> None:
        for prov, want in (("claude", True), ("codex", False), ("antigravity", False)):
            w = ClaudeAcpWorkerBackend(command="auto", provider=prov, cwd=self.tmp)
            self.assertEqual(w.prices_spend, want, prov)
            w = ClaudeAcpWorkerBackend(command="auto", provider=prov, cwd=self.tmp, cost_per_1k_in=0.01)
            self.assertTrue(w.prices_spend, prov)


class ProbeAndChildTests(_Tmp):
    """A6: probe all is ok only when all are ok; Codex status is sanitised; groups end."""

    def test_probe_all_fails_when_one_provider_fails(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        good = [sys.executable, str(STAND_IN), "--mode", "normal"]

        def fake_probe(_repo, *, provider, cmd, **_kw):
            if provider == "codex":
                raise Invalid("acp-x", "codex is broken")
            return {"ok": True, "provider": provider}

        with mock.patch("openfarm_core.acp_setup._probe_single_provider", side_effect=fake_probe), \
                mock.patch("openfarm_core.acp_setup.inspect_acp_status", return_value={}):
            res = probe_acp(repo, provider="all", command=good)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed"], ["codex"])
        self.assertTrue(res["results"]["claude"]["ok"])
        self.assertFalse(res["results"]["codex"]["ok"])
        with mock.patch("openfarm_core.acp_setup._probe_single_provider",
                        side_effect=lambda _r, *, provider, cmd, **_k: {"ok": True, "provider": provider}), \
                mock.patch("openfarm_core.acp_setup.inspect_acp_status", return_value={}):
            self.assertTrue(probe_acp(repo, provider="all", command=good)["ok"])

    def test_codex_login_status_gets_the_sanitised_environment(self) -> None:
        seen = self.tmp / "seen.json"
        codex = _script(self.bin / "codex", (
            "#!/usr/bin/env python3\nimport json, os\n"
            f"open({str(seen)!r}, 'w').write(json.dumps(sorted(os.environ)))\n"
            "print('Logged in using ChatGPT')\n"))
        leak = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
                "ANTIGRAVITY_X": "1", "OPENAI_API_KEY": "sk-x", "AI_AGENT": "1",
                "CLAUDE_CONFIG_DIR": str(self.tmp / "cfg")}
        with mock.patch.dict(os.environ, leak):
            res = _safe_codex_auth_status(str(codex), home=self.tmp)
        self.assertTrue(res.get("loggedIn"))
        names = set(json.loads(seen.read_text()))
        for key in leak:
            self.assertNotIn(key, names)
        self.assertIn("CODEX_HOME", names)

    def _grandchild_codex(self, pid_file: Path) -> Path:
        return _script(self.bin / "codex", (
            "#!/usr/bin/env python3\nimport signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdin.read()\n"
            "p = subprocess.Popen(['sh', '-c', 'trap \"\" TERM; sleep 300 & wait'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(300)\n"))

    def _gone(self, pid: int, within_s: float = 10.0) -> bool:
        deadline = time.monotonic() + within_s
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split()[2]
            except OSError:
                return True
            if state == "Z":
                return True
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)  # do not leave it behind
        return False

    def _wait_for(self, path: Path) -> int:
        deadline = time.monotonic() + 20
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        return int(path.read_text())

    def test_a_timed_out_turn_ends_the_whole_child_group(self) -> None:
        pid_file = self.tmp / "grandchild.pid"
        codex = self._grandchild_codex(pid_file)
        acp = ClaudeAcpAdapter([sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(codex)],
                               cwd=self.tmp, provider="codex", auth_mode="api-key", confine="off")
        sid = acp.new_session(cwd=self.tmp, tool_policy="none")
        with self.assertRaises(AdapterError):
            acp.prompt(sid, "hang", timeout_s=3)
        acp.close()
        self.assertTrue(self._gone(self._wait_for(pid_file)), "the grandchild outlived the turn")

    def test_a_stopped_bridge_ends_its_child_group(self) -> None:
        # The adapter's last resort on close is SIGTERM to the bridge's group; the
        # bridge must take its CLI child's own group down with it.
        import threading

        pid_file = self.tmp / "grandchild.pid"
        codex = self._grandchild_codex(pid_file)
        acp = ClaudeAcpAdapter([sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(codex)],
                               cwd=self.tmp, provider="codex", auth_mode="api-key", confine="off")
        self.addCleanup(acp.close)
        sid = acp.new_session(cwd=self.tmp, tool_policy="none")
        th = threading.Thread(target=lambda: self.assertRaises(AdapterError, acp.prompt, sid, "hang",
                                                               timeout_s=60), daemon=True)
        th.start()
        pid = self._wait_for(pid_file)
        os.killpg(acp._proc.pid, signal.SIGTERM)
        th.join(timeout=30)
        self.assertTrue(self._gone(pid), "the bridge's child group outlived the bridge")


class AgyPromptLimitTests(_Tmp):
    """A3: a prompt too large for one argv word is refused before agy starts."""

    def test_huge_prompt_is_refused_before_spawn(self) -> None:
        calls = self.tmp / "agy-calls.log"
        agy = _script(self.bin / "agy", (
            "#!/usr/bin/env python3\nimport json, sys\n"
            f"open({str(calls)!r}, 'a').write('called\\n')\n"
            "print(json.dumps({'response': 'ok', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n"))
        with ClaudeAcpAdapter([sys.executable, str(AGY_NATIVE_ACP), "--agy-bin", str(agy)],
                              cwd=self.tmp, provider="antigravity", confine="off") as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            with self.assertRaises(AdapterError) as caught:
                acp.prompt(sid, "x" * 200_000, timeout_s=30)
            self.assertIn("131072", caught.exception.message)
            self.assertFalse(calls.exists())
            # An ordinary prompt still goes through the -p argument.
            res = acp.prompt(sid, "small", timeout_s=30)
            self.assertIn("ok", res.text)
        self.assertEqual(calls.read_text(), "called\n")


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
