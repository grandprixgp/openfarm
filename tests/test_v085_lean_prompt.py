"""v0.8.5: openfarm's own bridges replace the provider CLI's built-in system prompt.

Measured live on 18 Nov 2026 with a one-line request: Claude Code 2,791 input
tokens with its own prompt and openfarm's appended, 580 with openfarm's alone;
codex-cli 10,599 with its built-in instructions, 4,332 lean. These tests pin the
argv that does it, the switch that turns it off, and that the child still gets
the environment it had (so a proxy such as `ANTHROPIC_BASE_URL` still applies).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core import confine as confine_lib
from openfarm_core.adapters.acp import (
    CLAUDE_NATIVE_ACP,
    CODEX_NATIVE_ACP,
    ClaudeAcpAdapter,
    _sanitised_env,
    acp_command,
)
from openfarm_core.adapters.claude_native_acp import LEAN_SYSTEM_PROMPT
from openfarm_core.adapters.codex_native_acp import LEAN_INSTRUCTIONS, lean_codex_args
import tomllib

from openfarm_core.config import Config

from tests.test_multi_provider_acp import TEST_CONFINE, _write_mock_codex
from tests.test_v051_acp_oauth_grill import _write_mock_claude


def _claude_home(root: Path) -> Path:
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        '{"claudeAiOauth":{"accessToken":"tok"}}\n', encoding="utf-8")
    return home


def _codex_home(root: Path) -> Path:
    home = root / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        '{"tokens":{"access_token":"cx"}}\n', encoding="utf-8")
    return home


class LeanClaudeBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v085-lean-claude-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.claude = _write_mock_claude(bin_dir, mode="ok")
        self.home = _claude_home(self.tmp)

    def _args(self, *, lean, system_prompt="SYS_NOTE", tool_policy="none") -> list[str]:
        command = acp_command(
            [sys.executable, str(CLAUDE_NATIVE_ACP), "--claude-bin", str(self.claude)],
            "off", lean_prompt=lean)
        with ClaudeAcpAdapter(command, cwd=self.tmp, isolate_home=True,
                              auth_mode="oauth", env={"HOME": str(self.home)}) as acp:
            sid = acp.new_session(cwd=self.tmp, system_prompt=system_prompt,
                                  tool_policy=tool_policy)
            return json.loads(acp.prompt(sid, "shape card").text)["seen_args"]

    def test_a_lean_session_replaces_the_system_prompt(self) -> None:
        args = self._args(lean=True)
        self.assertEqual(args[args.index("--system-prompt") + 1], "SYS_NOTE")
        self.assertNotIn("--append-system-prompt", args)
        # Everything that holds the child stays as it was.
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--bare", args)

    def test_a_lean_session_with_no_prompt_gets_openfarms_short_one(self) -> None:
        args = self._args(lean=True, system_prompt="")
        self.assertEqual(args[args.index("--system-prompt") + 1], LEAN_SYSTEM_PROMPT)

    def test_lean_read_only_keeps_its_tools_and_deny_list(self) -> None:
        args = self._args(lean=True, tool_policy="read-only")
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--disallowedTools", args)
        self.assertIn("--system-prompt", args)

    def test_lean_prompt_off_appends_as_before(self) -> None:
        args = self._args(lean=False)
        self.assertEqual(args[args.index("--append-system-prompt") + 1], "SYS_NOTE")
        self.assertNotIn("--system-prompt", args)

    def test_a_bridge_given_no_lean_flag_keeps_the_old_behaviour(self) -> None:
        # An older adapter (no `leanPrompt` in the session meta) gets what it had.
        from openfarm_core.adapters import claude_native_acp as bridge

        server = bridge.NativeClaudeAcpServer.__new__(bridge.NativeClaudeAcpServer)
        server.claude_bin = "claude"
        argv = server._build_claude_argv({
            "tool_policy": "none", "disallowed_tools": [], "model": "", "effort": "",
            "system_prompt": "SYS"})
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "SYS")


class LeanCodexBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v085-lean-codex-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = _write_mock_codex(bin_dir, mode="ok")
        self.home = _codex_home(self.tmp)
        nests = mock.patch.object(confine_lib, "nested_status", return_value=(True, "test"))
        nests.start()
        self.addCleanup(nests.stop)

    def _body(self, *, lean, system_prompt="SYS_CODEX") -> dict:
        command = acp_command(
            [sys.executable, str(CODEX_NATIVE_ACP), "--codex-bin", str(self.codex)],
            TEST_CONFINE, lean_prompt=lean)
        with ClaudeAcpAdapter(command, cwd=self.tmp, provider="codex", isolate_home=True,
                              auth_mode="oauth", env={"HOME": str(self.home)}) as acp:
            sid = acp.new_session(cwd=self.tmp, system_prompt=system_prompt,
                                  tool_policy="none")
            return json.loads(acp.prompt(sid, "check invariants").text)

    def test_a_lean_session_sends_openfarms_instructions_as_codexs_own(self) -> None:
        body = self._body(lean=True)
        args = body["seen_args"]
        self.assertIn('instructions="SYS_CODEX"', args)
        for setting in ("include_environment_context=false",
                        "include_permissions_instructions=false",
                        "skills.include_instructions=false"):
            self.assertEqual(args[args.index(setting) - 1], "-c")
        self.assertEqual(args[-1], "-")
        self.assertEqual(body["prompt"].strip(), "check invariants")

    def test_lean_prompt_off_glues_the_prompt_on_as_before(self) -> None:
        body = self._body(lean=False)
        self.assertFalse(any(a.startswith("instructions=") for a in body["seen_args"]))
        self.assertTrue(body["prompt"].startswith("SYS_CODEX"))
        self.assertIn("check invariants", body["prompt"])

    def test_the_instructions_are_one_toml_string(self) -> None:
        import tomllib

        text = 'line one\nsays "quoted" and \\ back\tslash, ünïcode'
        args = lean_codex_args(text)
        value = args[1].split("=", 1)[1]
        self.assertEqual(tomllib.loads(f"x = {value}")["x"], text)
        self.assertEqual(lean_codex_args("  ")[1],
                         f"instructions={json.dumps(LEAN_INSTRUCTIONS)}")


class LeanPromptConfigTests(unittest.TestCase):
    def _config(self, micromanager: dict) -> Config:
        repo = Path(__file__).resolve().parent.parent
        with (repo / ".openfarm" / "config.toml").open("rb") as handle:
            data = tomllib.load(handle)
        data["micromanager"] = micromanager
        return Config(data, repo)

    def test_on_by_default_and_can_be_turned_off(self) -> None:
        self.assertTrue(self._config({}).micromanager.lean_prompt)
        self.assertFalse(self._config({"lean_prompt": False}).micromanager.lean_prompt)

    def test_the_command_carries_it_to_the_adapter(self) -> None:
        cmd = acp_command(["claude"], "off", lean_prompt=False)
        self.assertFalse(cmd.lean_prompt)
        self.assertTrue(acp_command(["claude"], "off").lean_prompt)


class ChildEnvironmentTests(unittest.TestCase):
    def test_a_proxy_base_url_still_reaches_the_child(self) -> None:
        # A local proxy in front of the provider API (set by the user's shell or
        # tooling) must keep applying to supervisor children.
        env, home = _sanitised_env(
            provider="claude", isolate_home=True, auth_mode="oauth",
            base_env={"HOME": "/nonexistent", "PATH": "/usr/bin",
                      "ANTHROPIC_BASE_URL": "http://127.0.0.1:8797"})
        try:
            self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "http://127.0.0.1:8797")
        finally:
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
