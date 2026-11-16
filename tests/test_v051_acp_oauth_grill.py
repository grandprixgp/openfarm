"""Tests for Openfarm v0.5.1 native Claude ACP OAuth integration & openfarm-grill-me."""

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
sys.path.insert(0, str(ROOT))

from openfarm_core.acp_setup import (  # noqa: E402
    configure_acp_repo,
    inspect_acp_status,
    probe_acp,
    run_acp_login,
)
from openfarm_core.adapters.acp import (  # noqa: E402
    ClaudeAcpAdapter,
    _sanitised_env,
    resolve_acp_command,
)
from openfarm_core.bootstrap import initialize_project  # noqa: E402
from openfarm_core.cli import EXIT_USAGE, main as cli_main  # noqa: E402
from openfarm_core.config import Config, ConfigError, load_config  # noqa: E402
from openfarm_core.errors import Refused  # noqa: E402

STAND_IN = ROOT / "tests" / "acp_stand_in.py"
NATIVE_ACP = ROOT / "openfarm_core" / "adapters" / "claude_native_acp.py"


def _base_config_dict(state_dir: Path) -> dict:
    return {
        "project": {"name": "demo", "state_dir": str(state_dir)},
        "adapters": {
            "languages": ["rust"],
            "model": "openai-compatible",
            "micromanager": "claude-acp",
        },
        "model": {
            "endpoint": "https://api.example.invalid/v1",
            "allowed_endpoints": ["https://api.example.invalid/v1"],
            "implementer": {"model": "lesser-coder"},
        },
        "policy": {"categories": ["pattern"]},
    }


def _write_mock_claude(directory: Path, *, mode: str = "ok") -> Path:
    script = directory / "claude"
    code = f"""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

mode = {mode!r}
args = sys.argv[1:]
if len(args) >= 2 and args[0] == "auth" and args[1] == "status":
    if mode == "logged-out":
        print(json.dumps({{"loggedIn": False, "authMethod": "none"}}))
        raise SystemExit(1)
    print(json.dumps({{
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
        "planTier": "max_20x",
        "loginExpiresAt": "2026-12-03T00:00:00Z",
        "email": "private-user@example.invalid",
        "orgId": "org-secret-id",
        "orgName": "Secret Org",
    }}))
    raise SystemExit(0)

if mode == "sleep":
    time.sleep(15.0)
    raise SystemExit(0)

prompt_text = sys.stdin.read()
if mode == "logged-out":
    print(json.dumps({{
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Not logged in · Please run /login",
        "usage": {{"input_tokens": 0, "output_tokens": 0}},
        "total_cost_usd": 0,
    }}))
    raise SystemExit(1)

payload = {{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": json.dumps({{
        "seen_args": args,
        "has_bare": "--bare" in args,
        "prompt": prompt_text,
        "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
        "oauth_token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        "home": os.environ.get("HOME", ""),
        "cwd": os.getcwd(),
    }}),
    "usage": {{
        "input_tokens": 30,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 5,
        "output_tokens": 12,
    }},
    "total_cost_usd": 0.042,
}}
print(json.dumps(payload))
"""
    script.write_text(code, encoding="utf-8")
    script.chmod(0o755)
    return script


class MicromanagerAuthConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v051-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"

    def test_oauth_default_and_api_key_inference(self) -> None:
        cfg = Config(_base_config_dict(self.state), self.repo)
        self.assertEqual(cfg.micromanager.auth, "oauth")
        self.assertEqual(cfg.micromanager.binary, "auto")
        self.assertTrue(cfg.micromanager_enabled)

        auth_file = self.tmp / "mm.key"
        data_api = _base_config_dict(self.state)
        data_api["micromanager"] = {"auth_file": str(auth_file)}
        cfg_api = Config(data_api, self.repo)
        self.assertEqual(cfg_api.micromanager.auth, "api-key")
        self.assertEqual(cfg_api.micromanager.auth_file, auth_file.resolve())

    def test_oauth_with_auth_file_rejected_and_bad_auth_rejected(self) -> None:
        bad = _base_config_dict(self.state)
        bad["micromanager"] = {"auth": "oauth", "auth_file": str(self.tmp / "mm.key")}
        with self.assertRaises(ConfigError):
            Config(bad, self.repo)

        bad_choice = _base_config_dict(self.state)
        bad_choice["micromanager"] = {"auth": "magic"}
        with self.assertRaises(ConfigError):
            Config(bad_choice, self.repo)

        bad_bin = _base_config_dict(self.state)
        bad_bin["micromanager"] = {"binary": "   "}
        with self.assertRaises(ConfigError):
            Config(bad_bin, self.repo)

    def test_init_defaults_frontier_to_oauth_and_workers_to_api_key(self) -> None:
        target = self.tmp / "greenfield"
        res = initialize_project(
            "calorie-log",
            directory=target,
            state_dir=self.tmp / "g-state",
            secrets_dir=self.tmp / "g-secrets",
            no_git=True,
        )
        self.assertEqual(res["profile"], "active")
        self.assertEqual(res["micromanager_adapter"], "claude-acp")
        self.assertEqual(res["micromanager_mode"], "active")
        self.assertEqual(res["micromanager_auth"], "oauth")
        self.assertEqual(res["micromanager_binary"], "auto")

        cfg = load_config(target)
        self.assertTrue(cfg.micromanager_enabled)
        self.assertEqual(cfg.micromanager.auth, "oauth")
        self.assertEqual(cfg.micromanager.binary, "auto")
        self.assertEqual(cfg.model_adapter, "openai-compatible")
        self.assertIsNotNone(cfg.key_file)


class OAuthEnvAndCredentialsIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v051-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_oauth_strips_api_key_preserves_oauth_token_and_slims_claude_json(self) -> None:
        fake_home = self.tmp / "host-home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / ".credentials.json").write_text(
            '{"claudeAiOauth":{"accessToken":"tok-1"}}\n',
            encoding="utf-8",
        )
        (fake_home / ".claude.json").write_text(
            json.dumps({
                "oauthAccount": {"accountUuid": "acct-1"},
                "userID": "user-1",
                "mcpServers": {"evil": {"command": "nc"}},
                "projects": {"/secret": {}},
                "hooks": {"PreToolUse": []},
            }),
            encoding="utf-8",
        )

        env, iso = _sanitised_env(
            base_env={
                "HOME": str(fake_home),
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "ANTHROPIC_API_KEY": "sk-console-must-strip",
                "ANTHROPIC_AUTH_TOKEN": "sk-auth-must-strip",
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-long-lived",
                "CLAUDE_CODE_EXTRA": "strip-me",
                "CLAUDECODE": "1",
            },
            isolate_home=True,
            auth_mode="oauth",
        )
        assert iso is not None
        self.addCleanup(shutil.rmtree, iso, ignore_errors=True)

        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_EXTRA", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-long-lived")

        iso_meta = json.loads((iso / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE((iso / ".claude.json").stat().st_mode), 0o600)
        self.assertEqual(iso_meta["oauthAccount"], {"accountUuid": "acct-1"})
        self.assertEqual(iso_meta["userID"], "user-1")
        self.assertTrue(iso_meta["hasCompletedOnboarding"])
        self.assertNotIn("mcpServers", iso_meta)
        self.assertNotIn("projects", iso_meta)
        self.assertNotIn("hooks", iso_meta)

    def test_refreshed_oauth_credentials_sync_back_atomically_on_close(self) -> None:
        fake_home = self.tmp / "host-home"
        (fake_home / ".claude").mkdir(parents=True)
        host_creds = fake_home / ".claude" / ".credentials.json"
        host_creds.write_text(
            '{"claudeAiOauth":{"accessToken":"old-tok"}}\n',
            encoding="utf-8",
        )

        acp = ClaudeAcpAdapter(
            [sys.executable, str(STAND_IN), "--mode", "normal"],
            cwd=self.tmp,
            isolate_home=True,
            auth_mode="oauth",
            env={"HOME": str(fake_home)},
        )
        acp.initialize()
        assert acp._isolated_home is not None
        iso_creds = acp._isolated_home / ".claude" / ".credentials.json"
        iso_creds.write_text(
            '{"claudeAiOauth":{"accessToken":"refreshed-tok"}}\n',
            encoding="utf-8",
        )
        acp.close()

        updated = json.loads(host_creds.read_text(encoding="utf-8"))
        self.assertEqual(updated["claudeAiOauth"]["accessToken"], "refreshed-tok")
        self.assertEqual(stat.S_IMODE(host_creds.stat().st_mode), 0o600)


class AcpAuthHandshakeAndNativeBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v051-acp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_resolve_acp_command_prefers_external_else_native_claude(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda x: "/usr/bin/claude-agent-acp" if x == "claude-agent-acp" else "/usr/bin/claude"):
            self.assertEqual(resolve_acp_command("auto"), ["/usr/bin/claude-agent-acp"])

        with mock.patch("shutil.which", side_effect=lambda x: "/usr/local/bin/claude" if x == "claude" else None):
            resolved = resolve_acp_command("auto")
            self.assertEqual(resolved[0], sys.executable)
            self.assertEqual(resolved[1], str(NATIVE_ACP))
            self.assertEqual(resolved[2:], ["--claude-bin", "/usr/local/bin/claude"])

        explicit = resolve_acp_command("claude --verbose")
        self.assertEqual(explicit[1], str(NATIVE_ACP))
        self.assertEqual(explicit[2], "--claude-bin")
        self.assertEqual(explicit[-1], "--verbose")

    def test_standin_oauth_auto_authenticate_and_unauthenticated_refusal(self) -> None:
        with ClaudeAcpAdapter(
            [
                sys.executable,
                str(STAND_IN),
                "--auth-methods",
                "oauth,api-key",
                "--fail-unauthenticated",
            ],
            cwd=self.tmp,
            auth_mode="oauth",
        ) as acp:
            init = acp.initialize()
            self.assertEqual(len(init["authMethods"]), 2)
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            self.assertTrue(acp._authenticated)
            res = acp.prompt(sid, "hello")
            self.assertIn("ok", res.text.lower())

        with ClaudeAcpAdapter(
            [sys.executable, str(STAND_IN), "--fail-unauthenticated"],
            cwd=self.tmp,
            auth_mode="oauth",
        ) as acp:
            sid = acp.new_session(cwd=self.tmp, tool_policy="none")
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "hello")
            self.assertEqual(caught.exception.code, "acp-auth-required")

    def test_native_claude_acp_bridge_none_readonly_usage_auth_error_and_cancel(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake_home = self.tmp / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / ".credentials.json").write_text(
            '{"claudeAiOauth":{"accessToken":"tok"}}\n', encoding="utf-8"
        )
        mock_claude = _write_mock_claude(bin_dir, mode="ok")

        with ClaudeAcpAdapter(
            [sys.executable, str(NATIVE_ACP), "--claude-bin", str(mock_claude)],
            cwd=self.tmp,
            isolate_home=True,
            auth_mode="oauth",
            env={
                "HOME": str(fake_home),
                "ANTHROPIC_API_KEY": "sk-should-be-stripped",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-env",
            },
        ) as acp:
            init = acp.initialize()
            self.assertTrue(init["agentCapabilities"]["nativeClaudeCli"])
            self.assertEqual([m["id"] for m in acp.auth_methods], ["oauth", "api-key"])

            sid_none = acp.new_session(
                cwd=self.tmp,
                system_prompt="SYS_NOTE",
                model="claude-sonnet-4-5",
                effort="high",
                tool_policy="none",
            )
            res_none = acp.prompt(sid_none, "shape card")
            body = json.loads(res_none.text)
            self.assertFalse(body["has_bare"])
            self.assertFalse(body["api_key_present"])
            self.assertEqual(body["oauth_token"], "tok-env")
            self.assertIn("--tools", body["seen_args"])
            idx = body["seen_args"].index("--tools")
            self.assertEqual(body["seen_args"][idx + 1], "")
            self.assertIn("--model", body["seen_args"])
            self.assertEqual(res_none.tokens_in, 45)
            self.assertEqual(res_none.tokens_out, 12)
            self.assertAlmostEqual(res_none.vendor_cost_usd, 0.042)

            sid_ro = acp.new_session(cwd=self.tmp, tool_policy="read-only")
            res_ro = acp.prompt(sid_ro, "read only check")
            ro_body = json.loads(res_ro.text)
            ro_idx = ro_body["seen_args"].index("--tools")
            self.assertEqual(ro_body["seen_args"][ro_idx + 1], "Read,Glob,Grep")
            self.assertIn("--disallowedTools", ro_body["seen_args"])

        # Logged-out error -> Refused("acp-auth-required")
        out_dir = self.tmp / "bin-logged-out"
        out_dir.mkdir()
        logged_out_claude = _write_mock_claude(out_dir, mode="logged-out")
        with ClaudeAcpAdapter(
            [sys.executable, str(NATIVE_ACP), "--claude-bin", str(logged_out_claude)],
            cwd=self.tmp,
            auth_mode="oauth",
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "hello")
            self.assertEqual(caught.exception.code, "acp-auth-required")

        # Cancel terminates active mock claude subprocess cleanly
        sleep_dir = self.tmp / "bin-sleep"
        sleep_dir.mkdir()
        sleep_claude = _write_mock_claude(sleep_dir, mode="sleep")
        calls = {"n": 0}

        def cancel_soon() -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        with ClaudeAcpAdapter(
            [sys.executable, str(NATIVE_ACP), "--claude-bin", str(sleep_claude)],
            cwd=self.tmp,
            auth_mode="oauth",
        ) as acp:
            sid = acp.new_session(cwd=self.tmp)
            with self.assertRaises(Refused) as caught:
                acp.prompt(sid, "wait", timeout_s=10.0, cancel_check=cancel_soon)
            self.assertEqual(caught.exception.code, "acp-cancelled")


class OpenfarmAcpCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v051-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_status_redacts_pii_and_configure_roundtrips_with_probe(self) -> None:
        fake_home = self.tmp / "home"
        (fake_home / ".claude").mkdir(parents=True)
        creds = fake_home / ".claude" / ".credentials.json"
        creds.write_text('{"claudeAiOauth":{"accessToken":"x"}}\n', encoding="utf-8")
        creds.chmod(0o600)

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        mock_claude = _write_mock_claude(bin_dir, mode="ok")

        status = inspect_acp_status(
            home=fake_home,
            claude_bin=str(mock_claude),
            acp_bin="",
            env={},
        )
        self.assertTrue(status["oauth_ready"])
        self.assertEqual(status["resolved_transport"], "claude-native-acp")
        self.assertEqual(status["cli_auth"]["authMethod"], "claude.ai")
        raw_dump = json.dumps(status)
        self.assertNotIn("private-user@example.invalid", raw_dump)
        self.assertNotIn("org-secret-id", raw_dump)

        repo = self.tmp / "app"
        initialize_project(
            "calorie-log",
            directory=repo,
            state_dir=self.tmp / "state",
            secrets_dir=self.tmp / "secrets",
            profile="solo",
            no_git=True,
        )

        # Configure Frontier Claude ACP with OAuth via CLI
        rc = cli_main([
            "--repo", str(repo),
            "acp", "configure",
            "--mode", "advisory",
            "--auth", "oauth",
            "--binary", "claude",
            "--effort", "high",
            "--tool-policy", "read-only",
            "--json",
        ])
        self.assertEqual(rc, 0)
        cfg = load_config(repo)
        self.assertTrue(cfg.micromanager_enabled)
        self.assertEqual(cfg.micromanager_adapter, "claude-acp")
        self.assertEqual(cfg.micromanager.mode, "advisory")
        self.assertEqual(cfg.micromanager.auth, "oauth")
        self.assertEqual(cfg.micromanager.binary, "claude")
        self.assertEqual(cfg.micromanager.effort, "high")
        self.assertEqual(cfg.micromanager.tool_policy, "read-only")
        self.assertEqual(cfg.model_adapter, "openai-compatible")

        # Invalid combination (--auth oauth with --auth-file) fails closed without touching file
        before = (repo / ".openfarm" / "config.toml").read_text(encoding="utf-8")
        rc_bad = cli_main([
            "--repo", str(repo),
            "acp", "configure",
            "--auth", "oauth",
            "--auth-file", str(self.tmp / "mm.key"),
        ])
        self.assertEqual(rc_bad, EXIT_USAGE)
        self.assertEqual((repo / ".openfarm" / "config.toml").read_text(encoding="utf-8"), before)

        probe_res = probe_acp(
            repo,
            command=[sys.executable, str(STAND_IN), "--auth-methods", "oauth"],
        )
        self.assertEqual(probe_res["protocol_version"], 1)
        self.assertEqual(probe_res["auth_methods"][0]["id"], "oauth")

    def test_run_acp_login_invokes_expected_claude_auth_commands(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        mock_claude = _write_mock_claude(bin_dir, mode="ok")
        recorded: list[list[str]] = []

        def fake_run(argv, **kwargs):
            if len(argv) >= 2 and argv[1] in ("auth", "setup-token"):
                recorded.append(list(argv))
                if len(argv) >= 3 and argv[1] == "auth" and argv[2] == "status":
                    return subprocess.CompletedProcess(
                        argv, 0, stdout='{"loggedIn": true, "authMethod": "claude.ai"}', stderr=""
                    )
                return subprocess.CompletedProcess(argv, 0)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch("openfarm_core.acp_setup.subprocess.run", side_effect=fake_run):
            res1 = run_acp_login(mode="claudeai", sso=True, claude_bin=str(mock_claude))
            res2 = run_acp_login(setup_token=True, claude_bin=str(mock_claude))

        self.assertTrue(res1["ok"])
        self.assertEqual(recorded[0], [str(mock_claude), "auth", "login", "--claudeai", "--sso"])
        self.assertEqual(recorded[2], [str(mock_claude), "setup-token"])


class OpenfarmGrillMeIntegrationTests(unittest.TestCase):
    def test_grill_me_skill_command_and_initializer_wiring(self) -> None:
        skills_dir = ROOT / "skills"
        grill_skill = (skills_dir / "openfarm-grill-me" / "SKILL.md").read_text(encoding="utf-8")
        grill_proto = (
            skills_dir / "openfarm-grill-me" / "prompts" / "grill_protocol.md"
        ).read_text(encoding="utf-8")
        grill_cmd = (
            ROOT / "openfarm_core" / "skill_assets" / "commands" / "openfarm-grill-me.md"
        ).read_text(encoding="utf-8")
        init_skill = (skills_dir / "openfarm-initialize" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        root_skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("one question at a time", grill_skill.lower())
        self.assertIn("❓", grill_skill)
        self.assertIn("➡️", grill_skill)
        self.assertIn("openfarm acp status", grill_skill)
        self.assertIn(".openfarm/GRILL_DECISIONS.md", grill_proto)
        self.assertIn("$ARGUMENTS", grill_cmd)
        self.assertIn("openfarm-grill-me", init_skill)
        self.assertIn("openfarm acp status", init_skill)
        self.assertIn("openfarm-grill-me", root_skill)


if __name__ == "__main__":
    unittest.main()
