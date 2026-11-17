"""Comprehensive unit and integration tests for Stage 1 multi-provider ACP support."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core.acp_setup import (
    configure_acp_repo,
    inspect_acp_status,
    probe_acp,
    run_acp_login,
)
from openfarm_core.adapters.acp import (
    ACP_PROVIDERS,
    AGY_NATIVE_ACP,
    CLAUDE_NATIVE_ACP,
    CODEX_NATIVE_ACP,
    AcpAdapter,
    ClaudeAcpAdapter,
    _sanitised_env,
    acp_adapter_for_provider,
    infer_acp_provider,
    is_acp_adapter,
    normalize_acp_provider,
    resolve_acp_command,
)
from openfarm_core.adapters.claude_worker import (
    AcpWorkerBackend,
    ClaudeAcpWorkerBackend,
    open_acp_worker,
    open_claude_worker,
)
from openfarm_core.adapters.openai_compat import open_backend
from openfarm_core.bench import load_arm
from openfarm_core.board import Board
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.cli import EXIT_OK, _backend_prices_spend, main as cli_main
from openfarm_core.config import Config, ConfigError, load_config
from openfarm_core.coordinator import AcpCoordinatorBackend, SwarmRunner
from openfarm_core.errors import Invalid, Refused
from openfarm_core.git import Git
from openfarm_core.human_cli import Style, format_human
from openfarm_core.micromanager import ClaudeAcpMicromanager, open_micromanager
from openfarm_core.swarm import worker_kind_for_config
from openfarm_core.tui import collect_snapshot, render_snapshot

STAND_IN = ROOT / "tests" / "acp_stand_in.py"

from openfarm_core import confine as confine_lib
from openfarm_core.confine import codex_tool_flags

# Codex and Antigravity children need the bubblewrap boundary under `auto`; on a
# host where bwrap cannot run, these tests run them unconfined instead.
TEST_CONFINE = "auto" if confine_lib.bwrap_status()[0] else "off"


def _base_config(state_dir: Path) -> dict:
    return {
        "project": {"name": "demo", "state_dir": str(state_dir)},
        "adapters": {
            "languages": ["python"],
            "model": "openai-compatible",
            "micromanager": "off",
        },
        "model": {
            "endpoint": "https://api.example.invalid/v1",
            "allowed_endpoints": ["https://api.example.invalid/v1"],
            "implementer": {"model": "lesser-coder"},
        },
        "policy": {"categories": ["pattern"]},
    }


def _write_mock_codex(directory: Path, *, mode: str = "ok") -> Path:
    script = directory / "codex"
    code = f"""#!/usr/bin/env python3
import json, os, sys, time

mode = {mode!r}
args = sys.argv[1:]
if len(args) >= 2 and args[0] == "login" and args[1] == "status":
    if mode == "logged-out":
        print("Not logged in", file=sys.stderr)
        raise SystemExit(1)
    print("Logged in using ChatGPT (secret-codex@example.invalid)")
    raise SystemExit(0)

if mode == "sleep":
    time.sleep(15.0)
    raise SystemExit(0)

if "-p" in args:
    _p_idx = args.index("-p")
    prompt_text = args[_p_idx + 1] if _p_idx + 1 < len(args) else sys.stdin.read()
else:
    prompt_text = sys.stdin.read()
if mode == "logged-out":
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "Not logged in; run codex login"}}}}))
    raise SystemExit(1)

resp_payload = {{
    "seen_args": args,
    "prompt": prompt_text,
    "codex_home": os.environ.get("CODEX_HOME", ""),
    "openai_key_present": "OPENAI_API_KEY" in os.environ,
    "openai_key": os.environ.get("OPENAI_API_KEY", ""),
    "cwd": os.getcwd(),
}}
if mode == "coordinator":
    agent_text = json.dumps({{"actions": [{{"action": "wait", "reason": "mock-codex-coord"}}]}})
else:
    agent_text = json.dumps(resp_payload)

print(json.dumps({{"type": "thread.started", "thread_id": "t-1"}}))
print(json.dumps({{"type": "item.completed", "item": {{"id": "i-1", "type": "agent_message", "text": agent_text}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 120, "cached_input_tokens": 30, "output_tokens": 25}}}}))
"""
    script.write_text(code, encoding="utf-8")
    script.chmod(0o755)
    return script


def _write_mock_agy(directory: Path, *, mode: str = "ok", use_response_key: bool = False) -> Path:
    script = directory / "agy"
    key_name = "response" if use_response_key else "result"
    code = f"""#!/usr/bin/env python3
import json, os, sys, time

mode = {mode!r}
args = sys.argv[1:]
if mode == "sleep":
    time.sleep(15.0)
    raise SystemExit(0)

if args and args[0] == "acp":
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": mid,
                "result": {{
                    "protocolVersion": 1,
                    "agentCapabilities": {{"loadSession": True}},
                    "authMethods": [
                        {{"id": "oauth-personal", "name": "Log in with Google"}},
                        {{"id": "gemini-api-key", "name": "Gemini API key"}},
                    ],
                }},
            }}), flush=True)
        elif method == "authenticate":
            raise AssertionError("browser oauth-personal must not be auto-invoked")
        elif method == "session/new":
            if mode == "auth-required":
                print(json.dumps({{
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {{"code": -32000, "message": "Authentication required"}},
                }}), flush=True)
                continue
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": mid,
                "result": {{
                    "sessionId": "agy-acp-s1",
                    "configOptions": [
                        {{"id": "model", "category": "model", "type": "select"}},
                        {{"id": "thought_level", "category": "thought_level", "type": "select"}},
                    ],
                }},
            }}), flush=True)
        elif method == "session/set_config_option":
            if mode == "ignore-32601":
                print(json.dumps({{
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {{"code": -32601, "message": "Method not found"}},
                }}), flush=True)
            else:
                params = msg.get("params") or {{}}
                if not hasattr(sys, "_cfg_calls"):
                    sys._cfg_calls = []
                sys._cfg_calls.append((params.get("configId"), params.get("value")))
                print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{}}}}), flush=True)
        elif method == "session/prompt":
            cfg_calls = getattr(sys, "_cfg_calls", [])
            txt = json.dumps({{"cfg_calls": cfg_calls, "home": os.environ.get("HOME", "")}})
            print(json.dumps({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{
                    "sessionId": "agy-acp-s1",
                    "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": txt}}}},
                }},
            }}), flush=True)
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": mid,
                "result": {{"stopReason": "end_turn", "usage": {{"inputTokens": 40, "outputTokens": 10}}}},
            }}), flush=True)
        elif method == "session/close":
            print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{}}}}), flush=True)
            break
    raise SystemExit(0)

if "-p" in args:
    _p_idx = args.index("-p")
    prompt_text = args[_p_idx + 1] if _p_idx + 1 < len(args) else sys.stdin.read()
else:
    prompt_text = sys.stdin.read()
if mode == "logged-out":
    print(json.dumps({{
        "is_error": True,
        "result": "Authentication required: run agy acp --login",
    }}))
    raise SystemExit(1)

body = {{
    "seen_args": args,
    "prompt": prompt_text,
    "gemini_key_present": "GEMINI_API_KEY" in os.environ,
    "google_key_present": "GOOGLE_API_KEY" in os.environ,
    "gemini_key": os.environ.get("GEMINI_API_KEY", ""),
    "home": os.environ.get("HOME", ""),
}}
payload = {{
    "is_error": False,
    {key_name!r}: json.dumps(body),
    "usage": {{"promptTokenCount": 70, "candidatesTokenCount": 18}},
    "total_cost_usd": 0.019,
}}
print(json.dumps(payload))
"""
    script.write_text(code, encoding="utf-8")
    script.chmod(0o755)
    return script


class ProviderHelpersAndCommandTests(unittest.TestCase):
    def test_provider_normalization_aliases_and_inference(self) -> None:
        self.assertEqual(ACP_PROVIDERS, ("claude", "antigravity", "codex"))
        self.assertIs(AcpAdapter, ClaudeAcpAdapter)
        self.assertIs(AcpWorkerBackend, ClaudeAcpWorkerBackend)

        for raw, expected in (
            ("claude", "claude"),
            ("claude-acp", "claude"),
            ("acp", "claude"),
            ("antigravity", "antigravity"),
            ("antigravity-acp", "antigravity"),
            ("agy", "antigravity"),
            ("agy-acp", "antigravity"),
            ("codex", "codex"),
            ("codex-acp", "codex"),
        ):
            self.assertEqual(normalize_acp_provider(raw), expected)
            self.assertTrue(is_acp_adapter(raw))

        self.assertEqual(acp_adapter_for_provider("agy"), "antigravity-acp")
        self.assertEqual(acp_adapter_for_provider("codex"), "codex-acp")
        self.assertEqual(acp_adapter_for_provider("claude"), "claude-acp")
        self.assertFalse(is_acp_adapter("openai-compatible"))
        self.assertFalse(is_acp_adapter("tier:dsf"))
        self.assertFalse(is_acp_adapter("code"))

        with self.assertRaises(Invalid):
            normalize_acp_provider("unknown-provider")

        self.assertEqual(infer_acp_provider("agy"), "antigravity")
        self.assertEqual(infer_acp_provider("agy-print"), "antigravity")
        self.assertEqual(infer_acp_provider([sys.executable, str(AGY_NATIVE_ACP)]), "antigravity")
        self.assertEqual(infer_acp_provider("codex"), "codex")
        self.assertEqual(infer_acp_provider([sys.executable, str(CODEX_NATIVE_ACP)]), "codex")
        self.assertEqual(infer_acp_provider("auto"), "claude")
        self.assertEqual(infer_acp_provider("auto", provider="codex"), "codex")

    def test_resolve_acp_command_for_all_providers(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda x: "/usr/bin/claude-agent-acp" if x == "claude-agent-acp" else None):
            self.assertEqual(resolve_acp_command("auto", provider="claude"), ["/usr/bin/claude-agent-acp"])

        with mock.patch("shutil.which", side_effect=lambda x: "/usr/local/bin/agy" if x == "agy" else None):
            self.assertEqual(resolve_acp_command("auto", provider="antigravity"), ["/usr/local/bin/agy", "acp"])
            self.assertEqual(resolve_acp_command("agy", provider="antigravity"), ["/usr/local/bin/agy", "acp"])
            self.assertEqual(resolve_acp_command("agy acp", provider="antigravity"), ["/usr/local/bin/agy", "acp"])
            self.assertEqual(
                resolve_acp_command("agy-print --model gemini", provider="antigravity"),
                [sys.executable, str(AGY_NATIVE_ACP), "--agy-bin", "/usr/local/bin/agy", "--model", "gemini"],
            )

        with mock.patch("shutil.which", side_effect=lambda x: "/usr/bin/antigravity-acp" if x == "antigravity-acp" else None):
            self.assertEqual(resolve_acp_command("auto", provider="antigravity"), ["/usr/bin/antigravity-acp"])

        with mock.patch("shutil.which", side_effect=lambda x: "/usr/local/bin/codex" if x == "codex" else None):
            self.assertEqual(
                resolve_acp_command("auto", provider="codex"),
                [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", "/usr/local/bin/codex"],
            )
            self.assertEqual(
                resolve_acp_command("codex", provider="codex"),
                [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", "/usr/local/bin/codex"],
            )

        with mock.patch("shutil.which", side_effect=lambda x: "/usr/bin/codex-acp" if x == "codex-acp" else None):
            self.assertEqual(resolve_acp_command("auto", provider="codex"), ["/usr/bin/codex-acp"])


class MultiProviderEnvAndSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-multi-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_antigravity_oauth_and_api_key_isolation_and_sync(self) -> None:
        fake_home = self.tmp / "ag-home"
        tok_dir = fake_home / ".gemini" / "antigravity-cli"
        tok_dir.mkdir(parents=True)
        host_tok = tok_dir / "antigravity-oauth-token"
        host_tok.write_text("initial-ag-token\n", encoding="utf-8")

        env, iso = _sanitised_env(
            provider="antigravity",
            base_env={
                "HOME": str(fake_home),
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "GEMINI_API_KEY": "should-be-stripped",
                "GOOGLE_API_KEY": "should-be-stripped-too",
            },
            isolate_home=True,
            auth_mode="oauth",
        )
        assert iso is not None
        self.addCleanup(shutil.rmtree, iso, ignore_errors=True)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)

        env_ag_strip, iso_ag_strip = _sanitised_env(
            provider="claude",
            base_env={
                "HOME": str(fake_home),
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "ANTIGRAVITY_APP_DATA_DIR": "/tmp/leaked",
                "ANTIGRAVITY_CSRF_TOKEN": "csrf-secret",
            },
            isolate_home=True,
            auth_mode="oauth",
        )
        if iso_ag_strip is not None:
            self.addCleanup(shutil.rmtree, iso_ag_strip, ignore_errors=True)
        self.assertNotIn("ANTIGRAVITY_APP_DATA_DIR", env_ag_strip)
        self.assertNotIn("ANTIGRAVITY_CSRF_TOKEN", env_ag_strip)
        iso_tok = iso / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        self.assertTrue(iso_tok.is_file())
        self.assertEqual(stat.S_IMODE(iso_tok.stat().st_mode), 0o600)
        self.assertEqual(iso_tok.read_text(encoding="utf-8"), "initial-ag-token\n")

        # API-key mode sets GEMINI_API_KEY from auth_file and skips host token
        key_file = self.tmp / "gemini.key"
        key_file.write_text("gk-test-key\n", encoding="utf-8")
        env_api, iso_api = _sanitised_env(
            provider="antigravity",
            base_env={"HOME": str(fake_home)},
            isolate_home=True,
            auth_file=key_file,
            auth_mode="api-key",
        )
        assert iso_api is not None
        self.addCleanup(shutil.rmtree, iso_api, ignore_errors=True)
        self.assertEqual(env_api["GEMINI_API_KEY"], "gk-test-key")
        self.assertFalse((iso_api / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists())

        # Refresh sync-back on close()
        acp = ClaudeAcpAdapter(
            [sys.executable, str(STAND_IN), "--mode", "normal"],
            cwd=self.tmp,
            provider="antigravity", confine=TEST_CONFINE,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home)},
        )
        for pat in (".openfarm/**", ".claude/**", ".gemini/**", ".codex/**", ".agents/**"):
            self.assertIn(pat, acp.forbidden_files)
        self.assertEqual(acp.name, "antigravity-acp")
        acp.initialize()
        assert acp._isolated_home is not None
        iso_tok2 = acp._isolated_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        iso_tok2.write_text("refreshed-ag-token\n", encoding="utf-8")
        acp.close()
        self.assertEqual(host_tok.read_text(encoding="utf-8"), "refreshed-ag-token\n")
        self.assertEqual(stat.S_IMODE(host_tok.stat().st_mode), 0o600)

    def test_codex_oauth_and_api_key_isolation_and_sync(self) -> None:
        fake_home = self.tmp / "codex-home"
        (fake_home / ".codex").mkdir(parents=True)
        host_auth = fake_home / ".codex" / "auth.json"
        host_auth.write_text('{"tokens":{"access_token":"cx-1"}}\n', encoding="utf-8")

        env, iso = _sanitised_env(
            provider="codex",
            base_env={
                "HOME": str(fake_home),
                "CODEX_HOME": str(fake_home / ".codex"),
                "OPENAI_API_KEY": "sk-must-strip",
            },
            isolate_home=True,
            auth_mode="oauth",
        )
        assert iso is not None
        self.addCleanup(shutil.rmtree, iso, ignore_errors=True)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["CODEX_HOME"], str(iso / ".codex"))
        self.assertEqual(stat.S_IMODE((iso / ".codex").stat().st_mode), 0o700)
        iso_auth = iso / ".codex" / "auth.json"
        self.assertTrue(iso_auth.is_file())
        self.assertEqual(stat.S_IMODE(iso_auth.stat().st_mode), 0o600)

        key_file = self.tmp / "openai.key"
        key_file.write_text("sk-codex-api\n", encoding="utf-8")
        env_api, iso_api = _sanitised_env(
            provider="codex",
            base_env={"HOME": str(fake_home)},
            isolate_home=True,
            auth_file=key_file,
            auth_mode="api-key",
        )
        assert iso_api is not None
        self.addCleanup(shutil.rmtree, iso_api, ignore_errors=True)
        self.assertEqual(env_api["OPENAI_API_KEY"], "sk-codex-api")
        self.assertFalse((iso_api / ".codex" / "auth.json").exists())

        # Valid JSON auth.json syncs back atomically on close()
        acp = ClaudeAcpAdapter(
            [sys.executable, str(STAND_IN), "--mode", "normal"],
            cwd=self.tmp,
            provider="codex", confine=TEST_CONFINE,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home)},
        )
        self.assertEqual(acp.name, "codex-acp")
        acp.initialize()
        assert acp._isolated_home is not None
        (acp._isolated_home / ".codex" / "auth.json").write_text(
            '{"tokens":{"access_token":"cx-refreshed"}}\n', encoding="utf-8"
        )
        acp.close()
        self.assertEqual(
            json.loads(host_auth.read_text(encoding="utf-8"))["tokens"]["access_token"],
            "cx-refreshed",
        )
        self.assertEqual(stat.S_IMODE(host_auth.stat().st_mode), 0o600)

        # Invalid JSON must NOT overwrite host auth.json
        acp_bad = ClaudeAcpAdapter(
            [sys.executable, str(STAND_IN), "--mode", "normal"],
            cwd=self.tmp,
            provider="codex", confine=TEST_CONFINE,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home)},
        )
        acp_bad.initialize()
        assert acp_bad._isolated_home is not None
        (acp_bad._isolated_home / ".codex" / "auth.json").write_text("{corrupt", encoding="utf-8")
        acp_bad.close()
        self.assertEqual(
            json.loads(host_auth.read_text(encoding="utf-8"))["tokens"]["access_token"],
            "cx-refreshed",
        )


class NativeBridgesAndHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-multi-bridge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_codex_native_acp_bridge_argv_usage_auth_and_cancel(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        mock_codex = _write_mock_codex(bin_dir, mode="ok")
        fake_home = self.tmp / "home"
        (fake_home / ".codex").mkdir(parents=True)
        (fake_home / ".codex" / "auth.json").write_text('{"tokens":{"access_token":"cx"}}\n', encoding="utf-8")

        with ClaudeAcpAdapter(
            [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(mock_codex)],
            cwd=self.tmp,
            provider="codex", confine=TEST_CONFINE,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home), "OPENAI_API_KEY": "sk-strip"},
        ) as acp:
            init = acp.initialize()
            self.assertTrue(init["agentCapabilities"]["nativeCodexCli"])
            sid = acp.new_session(
                cwd=self.tmp,
                system_prompt="SYS_CODEX",
                model="gpt-5-codex",
                effort="high",
                tool_policy="read-only",
            )
            res = acp.prompt(sid, "check invariants")
            body = json.loads(res.text)
            self.assertEqual(
                body["seen_args"],
                [
                    "exec",
                    "--json",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-s",
                    "read-only",
                    *codex_tool_flags("read-only"),
                    "-C",
                    str(self.tmp),
                    "-m",
                    "gpt-5-codex",
                    "-c",
                    'model_reasoning_effort="high"',
                    "-",
                ],
            )
            self.assertIn("SYS_CODEX", body["prompt"])
            self.assertIn("check invariants", body["prompt"])
            self.assertFalse(body["openai_key_present"])
            self.assertTrue(body["codex_home"].endswith("/.codex"))
            self.assertEqual(res.tokens_in, 120)
            self.assertEqual(res.tokens_out, 25)

        out_dir = self.tmp / "bin-logged-out"
        out_dir.mkdir()
        bad_codex = _write_mock_codex(out_dir, mode="logged-out")
        with ClaudeAcpAdapter(
            [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(bad_codex)],
            cwd=self.tmp,
            provider="codex", confine=TEST_CONFINE,
            auth_mode="oauth",
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "hello")
            self.assertEqual(caught.exception.code, "acp-auth-required")

        sleep_dir = self.tmp / "bin-sleep"
        sleep_dir.mkdir()
        sleep_codex = _write_mock_codex(sleep_dir, mode="sleep")
        calls = {"n": 0}

        def cancel_soon() -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        with ClaudeAcpAdapter(
            [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(sleep_codex)],
            cwd=self.tmp,
            provider="codex", confine=TEST_CONFINE,
            auth_mode="oauth",
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "wait", timeout_s=10.0, cancel_check=cancel_soon)
            self.assertEqual(caught.exception.code, "acp-cancelled")

    def test_agy_native_acp_and_agy_acp_handshake(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        mock_agy = _write_mock_agy(bin_dir, mode="ok", use_response_key=True)
        fake_home = self.tmp / "ag-home"
        (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
        (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("tok\n", encoding="utf-8")

        # 1) agy_native_acp.py bridge wrapping `agy -p --output-format json`
        with ClaudeAcpAdapter(
            [sys.executable, str(AGY_NATIVE_ACP), "--agy-bin", str(mock_agy)],
            cwd=self.tmp,
            provider="antigravity", confine=TEST_CONFINE,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home), "GEMINI_API_KEY": "strip-me", "GOOGLE_API_KEY": "strip-too"},
        ) as acp:
            init = acp.initialize()
            self.assertTrue(init["agentCapabilities"]["nativeAgyCli"])
            sid = acp.new_session(
                cwd=self.tmp,
                system_prompt="SYS_AGY",
                model="gemini-3-pro",
                effort="max",
                tool_policy="none",
            )
            res = acp.prompt(sid, "shape card")
            body = json.loads(res.text)
            self.assertEqual(
                body["seen_args"],
                [
                    "--output-format",
                    "json",
                    "--sandbox",
                    "--disable-slash-commands",
                    "--model",
                    "gemini-3-pro",
                    "--effort",
                    "max",
                    "-p",
                    "SYS_AGY\n\nshape card",
                ],
            )
            self.assertIn("SYS_AGY", body["prompt"])
            self.assertIn("shape card", body["prompt"])
            self.assertFalse(body["gemini_key_present"])
            self.assertFalse(body["google_key_present"])
            self.assertEqual(res.tokens_in, 70)
            self.assertEqual(res.tokens_out, 18)
            self.assertAlmostEqual(res.vendor_cost_usd, 0.019)

        # 2) Native `agy acp` handshake skips browser oauth-personal and sends set_config_option
        with ClaudeAcpAdapter(
            [str(mock_agy), "acp"],
            cwd=self.tmp,
            provider="antigravity", confine=TEST_CONFINE,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home)},
        ) as acp:
            acp.initialize()
            sid = acp.new_session(
                cwd=self.tmp,
                system_prompt="SYS_AGY_ACP",
                model="gemini-2.5",
                effort="high",
            )
            self.assertFalse(acp._authenticated)
            res = acp.prompt(sid, "go")
            data = json.loads(res.text)
            self.assertEqual(data["cfg_calls"], [["model", "gemini-2.5"], ["thought_level", "high"]])

        # 3) Native `agy acp` ignoring -32601 on session/set_config_option + Authentication required -> Refused
        bin_32601 = self.tmp / "bin-32601"
        bin_32601.mkdir()
        agy_32601 = _write_mock_agy(bin_32601, mode="ignore-32601")
        with ClaudeAcpAdapter([str(agy_32601), "acp"], cwd=self.tmp, provider="antigravity", confine=TEST_CONFINE) as acp:
            sid = acp.new_session(cwd=self.tmp, model="m1", effort="low")
            res = acp.prompt(sid, "ok")
            self.assertEqual(json.loads(res.text)["cfg_calls"], [])

        bin_auth = self.tmp / "bin-auth"
        bin_auth.mkdir()
        agy_auth = _write_mock_agy(bin_auth, mode="auth-required")
        with ClaudeAcpAdapter([str(agy_auth), "acp"], cwd=self.tmp, provider="antigravity", confine=TEST_CONFINE) as acp:
            with self.assertRaises(Refused) as caught:
                acp.new_session(cwd=self.tmp)
            self.assertEqual(caught.exception.code, "acp-auth-required")


class ConfigWiringCoordinatorAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-multi-wire-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()

    def test_config_acp_adapters_and_seat_effort_without_tiers(self) -> None:
        for adapter in ("claude-acp", "antigravity-acp", "agy-acp", "codex-acp"):
            data = _base_config(self.state)
            data["adapters"]["micromanager"] = adapter
            data["adapters"]["model"] = adapter
            data["tiers"] = {"plain": {"model": "x", "effort_param": ""}}
            data["swarm"] = {
                "enabled": True,
                "coordinator": adapter,
                "supervisor": adapter,
                "coordinator_effort": "medium",
                "supervisor_effort": "xhigh",
            }
            cfg = Config(data, self.repo)
            self.assertEqual(cfg.micromanager_adapter, adapter)
            self.assertEqual(cfg.swarm.coordinator, adapter)
            self.assertEqual(cfg.swarm.supervisor, adapter)
            self.assertEqual(cfg.swarm.coordinator_effort, "medium")
            self.assertEqual(cfg.swarm.supervisor_effort, "xhigh")
            self.assertEqual(worker_kind_for_config(cfg), "acp")
            self.assertTrue(_backend_prices_spend(cfg, None))

        bad = _base_config(self.state)
        bad["swarm"] = {"coordinator": "antigravity-acp", "coordinator_effort": "turbo"}
        with self.assertRaises(ConfigError):
            Config(bad, self.repo)

    def test_workers_micromanager_and_acp_coordinator_wiring(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        mock_codex = _write_mock_codex(bin_dir, mode="ok")
        mock_agy = _write_mock_agy(bin_dir, mode="ok")

        data = _base_config(self.state)
        data["adapters"]["model"] = "codex-acp"
        data["adapters"]["micromanager"] = "antigravity-acp"
        data["micromanager"] = {
            "mode": "active",
            "binary": str(mock_agy),
            "effort": "high",
            "confine": TEST_CONFINE,
        }
        cfg = Config(data, self.repo)
        backend = open_backend(cfg)
        self.assertIsInstance(backend, ClaudeAcpWorkerBackend)
        self.assertEqual(backend.provider, "codex")
        self.assertEqual(backend.name, "codex-acp-worker")
        self.addCleanup(backend.close)

        mm = open_micromanager(cfg, self.state, self.repo, worktree=self.repo)
        self.assertIsInstance(mm.backend, ClaudeAcpMicromanager)
        self.assertEqual(mm.backend.provider, "antigravity")
        self.assertEqual(mm.backend.name, "antigravity-acp")
        mm.close()

        # Swapping binary to codex for worker completion
        wb = open_acp_worker(cfg, provider="codex")
        wb.command = [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(mock_codex)]
        self.addCleanup(wb.close)
        comp = wb.complete("implementer", "", 0.2, [{"role": "user", "content": "hi"}])
        self.assertEqual(comp.tokens_in, 120)
        self.assertEqual(comp.tokens_out, 25)
        self.assertIn("CODEX_HOME", json.dumps(json.loads(comp.content)).upper())

        # AcpCoordinatorBackend + SwarmRunner with codex-acp coordinator
        coord_codex = _write_mock_codex(self.tmp / "coord-bin" if False else bin_dir, mode="coordinator")
        coord_data = _base_config(self.state)
        coord_data["micromanager"] = {
            "mode": "active",
            "binary": str(coord_codex),
            "effort": "medium",
            "confine": TEST_CONFINE,
        }
        coord_data["swarm"] = {
            "enabled": True,
            "coordinator": "codex-acp",
            "supervisor": "off",
            "coordinator_effort": "high",
        }
        coord_cfg = Config(coord_data, self.repo)
        coord_backend = AcpCoordinatorBackend(coord_cfg, provider="codex", effort="high")
        self.assertEqual(coord_backend.name, "codex-acp")
        self.assertTrue(coord_backend.prices_spend)
        self.addCleanup(coord_backend.close)

        board = Board(self.state / "board", state_dir=self.state)
        source = SimpleNamespace(read=lambda cid: {"card": Card(id=cid, title="T", state="ready", done_text="d").as_dict()})
        night = SimpleNamespace(exhausted=lambda: "", cards_cap=None, cards_claimed=0, note_card=lambda: None, total_spend=lambda: 0.0)
        state_obj = SimpleNamespace(kill_requested=lambda _p: False, is_paused=lambda: False)
        runner = SwarmRunner(
            cfg=coord_cfg,
            state=state_obj,
            source=source,
            board=board,
            identifiers=["CARD-1"],
            night=night,
            deadline=None,
            run_card_fn=lambda cid, **_kw: {"card_id": cid, "final_state": "in-review"},
            record_outcome_fn=lambda _cid, _o: False,
            backend=coord_backend,
        )
        outcomes = runner.run()
        coord_member = board.get_member("coordinator")
        self.assertIsNotNone(coord_member)
        assert coord_member is not None
        self.assertEqual(coord_member["kind"], "acp")
        self.assertEqual(coord_member["tier"], "codex-acp")
        self.assertEqual(coord_member["effort"], "high")
        self.assertEqual(outcomes[0]["skipped"], "no eligible non-colliding card could start")

    def test_init_acp_cli_human_tui_and_bench_arms(self) -> None:
        # init --acp-provider antigravity & codex
        for prov in ("antigravity", "codex"):
            proj = self.tmp / f"proj-{prov}"
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = cli_main([
                    "init", f"demo-{prov}",
                    "--dir", str(proj),
                    "--state-dir", str(self.tmp / f"st-{prov}"),
                    "--secrets-dir", str(self.tmp / f"sec-{prov}"),
                    "--acp-provider", prov,
                    "--no-git",
                    "--json",
                ])
            self.assertEqual(rc, EXIT_OK)
            res = json.loads(out_buf.getvalue())
            self.assertEqual(res["acp_provider"], prov)
            self.assertEqual(res["micromanager_adapter"], f"{prov}-acp")
            cfg = load_config(proj)
            self.assertEqual(cfg.micromanager_adapter, f"{prov}-acp")

        # acp configure --provider codex --effort xhigh
        proj_cfg = self.tmp / "proj-antigravity"
        cfg_res = configure_acp_repo(
            proj_cfg,
            provider="codex",
            mode="advisory",
            auth="oauth",
            effort="xhigh",
        )
        self.assertEqual(cfg_res["provider"], "codex")
        self.assertEqual(load_config(proj_cfg).micromanager_adapter, "codex-acp")
        self.assertEqual(load_config(proj_cfg).micromanager.effort, "xhigh")

        # inspect_acp_status --provider all redacts PII and inspects all 3 providers
        fake_home = self.tmp / "fake-home"
        (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
        (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("SECRET_AGY_TOKEN\n", encoding="utf-8")
        (fake_home / ".codex").mkdir(parents=True)
        (fake_home / ".codex" / "auth.json").write_text('{"tokens":{"access_token":"SECRET_CODEX_TOKEN"}}\n', encoding="utf-8")
        bin_dir = self.tmp / "cli-bins"
        bin_dir.mkdir()
        mock_codex = _write_mock_codex(bin_dir, mode="ok")
        mock_agy = _write_mock_agy(bin_dir, mode="ok")

        status = inspect_acp_status(
            proj_cfg,
            provider="all",
            home=fake_home,
            claude_bin="",
            acp_bin="",
            agy_bin=str(mock_agy),
            antigravity_acp_bin="",
            codex_bin=str(mock_codex),
            codex_acp_bin="",
            env={},
        )
        dumped = json.dumps(status)
        self.assertNotIn("SECRET_AGY_TOKEN", dumped)
        self.assertNotIn("SECRET_CODEX_TOKEN", dumped)
        self.assertNotIn("secret-codex@example.invalid", dumped)
        self.assertIn("providers", status)
        self.assertEqual(set(status["providers"]), {"claude", "antigravity", "codex"})
        self.assertTrue(status["providers"]["antigravity"]["oauth_ready"])
        self.assertTrue(status["providers"]["codex"]["oauth_ready"])

        # human_cli renders multi-provider status & preserves Claude heading
        human_all = format_human("acp_status", status, style=Style(enabled=False))
        self.assertIn("Providers", human_all)
        self.assertIn("antigravity", human_all)
        self.assertIn("codex", human_all)

        # run_acp_login invokes agy acp --login and codex login
        recorded: list[list[str]] = []

        def fake_run(argv, **kwargs):
            recorded.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("openfarm_core.acp_setup.subprocess.run", side_effect=fake_run):
            res_ag = run_acp_login(provider="antigravity", agy_bin=str(mock_agy), home=fake_home)
            res_cx = run_acp_login(provider="codex", codex_bin=str(mock_codex), home=fake_home)
        self.assertEqual(recorded[0], [str(mock_agy), "acp", "--login"])
        self.assertEqual(recorded[1], [str(mock_codex), "login"])
        self.assertEqual(res_ag["provider"], "antigravity")
        self.assertEqual(res_cx["provider"], "codex")

        # probe_acp --provider antigravity / codex / all
        p_ag = probe_acp(proj_cfg, provider="antigravity", command=[str(mock_agy), "acp"])
        self.assertEqual(p_ag["provider"], "antigravity")
        self.assertEqual(p_ag["protocol_version"], 1)

        p_all = probe_acp(proj_cfg, provider="all", command=[sys.executable, str(STAND_IN), "--mode", "normal"])
        self.assertTrue(p_all["ok"])
        self.assertEqual(set(p_all["results"]), {"claude", "antigravity", "codex"})

        # TUI ops snapshot & rendering include multi-provider ACP info
        snap = collect_snapshot(
            proj_cfg,
            view="ops",
            acp_home=fake_home,
            claude_bin="",
            acp_bin="",
            agy_bin=str(mock_agy),
            codex_bin=str(mock_codex),
        )
        self.assertEqual(set(snap["data"]["acp"]["providers"]), {"claude", "antigravity", "codex"})
        ops_txt = render_snapshot(
            proj_cfg,
            view="ops",
            acp_home=fake_home,
            claude_bin="",
            acp_bin="",
            agy_bin=str(mock_agy),
            codex_bin=str(mock_codex),
        )
        self.assertIn("Providers:", ops_txt)
        self.assertIn("antigravity=", ops_txt)
        self.assertIn("codex=", ops_txt)

        # All 4 new bench arms pass load_arm validation
        for arm_name in (
            "solo-antigravity.example.toml",
            "solo-codex.example.toml",
            "farm-antigravity.example.toml",
            "farm-codex.example.toml",
        ):
            arm_path = ROOT / "bench" / "arms" / arm_name
            self.assertTrue(arm_path.is_file(), arm_name)
            text = load_arm(arm_path)
            self.assertIn("[adapters]", text)

    def test_cli_acp_subcommands_and_handshake_with_acp_coordinator_supervisor(self) -> None:
        proj = self.tmp / "cli-run-proj"
        out_init = io.StringIO()
        with contextlib.redirect_stdout(out_init):
            rc_init = cli_main([
                "init", "acp-cli-demo",
                "--dir", str(proj),
                "--state-dir", str(self.tmp / "cli-run-state"),
                "--secrets-dir", str(self.tmp / "cli-run-sec"),
                "--acp-provider", "antigravity",
                "--mutation", "off",
                "--commit",
                "--json",
            ])
        self.assertEqual(rc_init, EXIT_OK)

        # Configure swarm with antigravity-acp coordinator & codex-acp supervisor
        cfg_path = proj / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(
            'enabled = false\ncoordinator = "code"\nsupervisor = "config"',
            'enabled = true\ncoordinator = "antigravity-acp"\nsupervisor = "codex-acp"\ncoordinator_effort = "medium"\nsupervisor_effort = "high"',
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")

        card_path = proj / "cards" / "CARD-1.md"
        card_path.write_text(
            "+++\n"
            'id = "CARD-1"\n'
            'title = "Demo"\n'
            'state = "ready"\n'
            'touches = ["src/lib.rs"]\n'
            'depends = []\n'
            'category = "small-feature"\n'
            "+++\n\n"
            "## Done\n\n"
            "Add `demo() -> u32`.\n\n"
            "```toml\n"
            'category = "small-feature"\n'
            "```\n",
            encoding="utf-8",
        )
        Git(proj).commit_all("add swarm acp config and card")

        # `openfarm acp status --provider all --json` via CLI
        out_st = io.StringIO()
        with contextlib.redirect_stdout(out_st):
            rc_st = cli_main(["--repo", str(proj), "acp", "status", "--provider", "all", "--json"])
        self.assertEqual(rc_st, EXIT_OK)
        st_payload = json.loads(out_st.getvalue())
        self.assertEqual(st_payload["provider"], "all")
        self.assertEqual(st_payload["primary_provider"], "antigravity")

        # `openfarm acp configure --provider codex --mode active --effort max --json`
        out_cf = io.StringIO()
        with contextlib.redirect_stdout(out_cf):
            rc_cf = cli_main([
                "--repo", str(proj), "acp", "configure",
                "--provider", "codex", "--mode", "active", "--effort", "max", "--json",
            ])
        self.assertEqual(rc_cf, EXIT_OK)
        cf_payload = json.loads(out_cf.getvalue())
        self.assertEqual(cf_payload["provider"], "codex")
        self.assertEqual(load_config(proj).micromanager_adapter, "codex-acp")
        self.assertEqual(load_config(proj).micromanager.effort, "max")

        # `openfarm run --dry-run` exercises _handshake across ACP coordinator & supervisor
        script_file = self.tmp / "dry-script.json"
        script_file.write_text("{}", encoding="utf-8")
        out_run = io.StringIO()
        with contextlib.redirect_stdout(out_run):
            rc_run = cli_main(["--repo", str(proj), "run", "--dry-run", "--cards", "1", "--script", str(script_file), "--json"])
        self.assertEqual(rc_run, EXIT_OK)
        run_payload = json.loads(out_run.getvalue())
        self.assertEqual(len(run_payload["outcomes"]), 1)


if __name__ == "__main__":
    unittest.main()
