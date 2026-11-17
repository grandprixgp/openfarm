"""A standalone Codex ACP server gets its model and effort the way the real one reads them."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from openfarm_core.adapters.acp import ClaudeAcpAdapter

_FAKE_CODEX_ACP = r"""#!/usr/bin/env python3
import json, sys
calls = []
def out(obj):
    print(json.dumps(obj), flush=True)
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        out({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif method == "session/new":
        # The real server reads none of `_meta`; it offers these instead.
        out({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1", "configOptions": [
            {"id": "mode", "category": "mode", "type": "select", "currentValue": "read-only"},
            {"id": "model", "category": "model", "type": "select", "currentValue": "old-default",
             "options": [{"value": "old-default"}, {"value": "wanted-model"}]},
            {"id": "reasoning_effort", "category": "thought_level", "type": "select",
             "currentValue": "medium",
             "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}]},
        ]}})
    elif method == "session/set_config_option":
        calls.append([params.get("configId"), params.get("value")])
        out({"jsonrpc": "2.0", "id": mid, "result": {"configOptions": []}})
    elif method == "session/prompt":
        out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps({"calls": calls})}}}})
        out({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid is not None:
        out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
"""


class StandaloneCodexServerOptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-acp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server = self.tmp / "bin" / "codex-acp"
        self.server.parent.mkdir()
        self.server.write_text(_FAKE_CODEX_ACP)
        self.server.chmod(0o755)
        self.work = self.tmp / "work"
        self.work.mkdir()

    def _calls(self, **session) -> list:
        with ClaudeAcpAdapter([str(self.server)], cwd=self.work, provider="codex",
                              auth_mode="api-key", confine="off") as acp:
            self.assertEqual(acp.transport, "server")
            sid = acp.new_session(cwd=self.work, **session)
            return json.loads(acp.prompt(sid, "go", timeout_s=30).text)["calls"]

    def test_model_and_effort_are_set_through_the_servers_own_options(self) -> None:
        self.assertEqual(self._calls(model="wanted-model", effort="high"),
                         [["model", "wanted-model"], ["reasoning_effort", "high"]])

    def test_an_effort_the_server_does_not_offer_is_stepped_down(self) -> None:
        self.assertEqual(self._calls(effort="max"), [["reasoning_effort", "high"]])

    def test_nothing_is_set_when_nothing_was_asked(self) -> None:
        self.assertEqual(self._calls(), [])


class CachedInputTokensTests(unittest.TestCase):
    """A server that reports cached input apart still has it counted as input."""

    def test_cached_tokens_are_added_unless_the_total_says_they_are_already_in(self) -> None:
        from openfarm_core.adapters.acp import _with_cached_input
        live = {"inputTokens": 2, "outputTokens": 4, "cachedReadTokens": 7095,
                "cachedWriteTokens": 9208, "totalTokens": 16309}
        self.assertEqual(_with_cached_input(live, 2, 4), 16305)
        already = {"inputTokens": 16305, "outputTokens": 4, "cachedReadTokens": 7095,
                   "totalTokens": 16309}
        self.assertEqual(_with_cached_input(already, 16305, 4), 16305)
        self.assertEqual(_with_cached_input({"inputTokens": 10, "cachedReadTokens": 5}, 10, 1), 15)
        self.assertEqual(_with_cached_input({"inputTokens": 10}, 10, 1), 10)
        self.assertEqual(_with_cached_input({"cachedReadTokens": "9", "cachedWriteTokens": -3}, 10, 1), 10)

    def test_a_prompt_through_the_adapter_counts_them(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="of-v081-lead-use-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        server = tmp / "claude-agent-acp"
        server.write_text(r"""#!/usr/bin/env python3
import json, sys
def out(obj):
    print(json.dumps(obj), flush=True)
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        out({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif method == "session/new":
        out({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}}}})
        out({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn", "usage": {
            "inputTokens": 2, "outputTokens": 4, "cachedReadTokens": 7095,
            "cachedWriteTokens": 9208, "totalTokens": 16309}}})
    elif mid is not None:
        out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
""")
        server.chmod(0o755)
        work = tmp / "work"
        work.mkdir()
        with ClaudeAcpAdapter([str(server)], cwd=work, provider="claude", auth_mode="api-key",
                              isolate_home=False) as acp:
            res = acp.prompt(acp.new_session(cwd=work), "go", timeout_s=30)
        self.assertEqual((res.text, res.tokens_in, res.tokens_out), ("pong", 16305, 4))


class CodexAutoChoiceTests(unittest.TestCase):
    """`binary = "auto"` for Codex means openfarm's own bridge when the Codex CLI is there.

    It is the route seen working live and the only one that turns Codex's tools
    off. The standalone `codex-acp` server (0.16.0 completed no turn on a ChatGPT
    login) is used when asked for by name, or when it is all there is.
    """

    def setUp(self) -> None:
        import os
        from unittest import mock
        self.bin = Path(tempfile.mkdtemp(prefix="openfarm-codex-auto-"))
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _program(self, name: str) -> str:
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return str(path)

    def _resolve(self, binary: str) -> list[str]:
        from openfarm_core.adapters.acp import resolve_acp_command
        return resolve_acp_command([binary], provider="codex")

    def test_both_installed_auto_and_codex_take_the_bridge(self) -> None:
        cli, server = self._program("codex"), self._program("codex-acp")
        for binary in ("auto", "codex", "codex-exec"):
            with self.subTest(binary):
                cmd = self._resolve(binary)
                self.assertTrue(cmd[1].endswith("codex_native_acp.py"), cmd)
                self.assertEqual(cmd[2:4], ["--codex-bin", cli])
        self.assertEqual(self._resolve("codex-acp"), [server])

    def test_only_the_server_installed_auto_takes_it(self) -> None:
        server = self._program("codex-acp")
        self.assertEqual(self._resolve("auto"), [server])
        self.assertEqual(self._resolve("codex"), [server])
        # Asked for by name, the bridge is never swapped for the server.
        self.assertTrue(self._resolve("codex-exec")[1].endswith("codex_native_acp.py"))

    def test_only_the_cli_installed_every_name_takes_the_bridge(self) -> None:
        self._program("codex")
        for binary in ("auto", "codex", "codex-acp", "codex-exec"):
            with self.subTest(binary):
                self.assertTrue(self._resolve(binary)[1].endswith("codex_native_acp.py"))

    def test_status_reports_what_auto_would_run(self) -> None:
        from openfarm_core.acp_setup import inspect_acp_status
        home = Path(tempfile.mkdtemp(prefix="openfarm-codex-auto-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        cli, server = self._program("codex"), self._program("codex-acp")

        def codex(**bins) -> dict:
            status = inspect_acp_status(None, provider="codex", home=home, env={}, **bins)
            return status["providers"]["codex"]

        both = codex(codex_bin=cli, codex_acp_bin=server)
        self.assertEqual(both["resolved_transport"], "codex-native-acp")
        self.assertEqual(both["resolved_command"][-1], cli)
        self.assertEqual(both["confinement"]["transport"], "bridge")
        only_server = codex(codex_bin="", codex_acp_bin=server)
        self.assertEqual(only_server["resolved_transport"], "codex-acp")
        self.assertEqual(only_server["resolved_command"], [server])
        self.assertEqual(only_server["confinement"]["transport"], "server")


class UnpricedSupervisorNoteTests(unittest.TestCase):
    """A supervisor whose spend cannot be measured is said so in the run's notes."""

    def _note(self, adapter: str, *, mode: str = "active", rate: float = 0.0,
              script: Path | None = None) -> str:
        from types import SimpleNamespace
        from openfarm_core.cli import _unpriced_supervisor_note
        cfg = SimpleNamespace(
            micromanager_adapter=adapter,
            micromanager=SimpleNamespace(mode=mode, cost_per_1k_in=rate, cost_per_1k_out=0.0))
        return _unpriced_supervisor_note(cfg, script)

    def test_codex_and_antigravity_without_rates_are_noted(self) -> None:
        for adapter, name in (("codex-acp", "codex"), ("antigravity-acp", "antigravity"),
                              ("agy-acp", "antigravity")):
            with self.subTest(adapter):
                note = self._note(adapter)
                self.assertIn(f"the {name} supervisor reports no cost", note)
                self.assertIn("[micromanager] cost_per_1k_in", note)

    def test_nothing_to_say_when_spend_is_measured_or_nothing_runs(self) -> None:
        self.assertEqual(self._note("codex-acp", rate=0.003), "")
        self.assertEqual(self._note("claude-acp"), "", "claude reports its own cost")
        self.assertEqual(self._note("codex-acp", mode="off"), "")
        self.assertEqual(self._note("off"), "")
        self.assertEqual(self._note("scripted"), "")
        self.assertEqual(self._note("codex-acp", script=Path("mm.json")), "")


class ClaudeApiKeyInsideTheBoundaryTests(unittest.TestCase):
    """A Claude child on an API key finds it under `confine = "bwrap"` too.

    The key helper runs inside the child. It used to read the real key file,
    which the boundary does not show.
    """

    def _env(self) -> tuple[dict, Path, Path]:
        from openfarm_core.adapters.acp import _sanitised_env
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-claude-key-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        key = tmp / "elsewhere" / "claude.key"
        key.parent.mkdir()
        key.write_text("sk-test-KEY-123\n")
        env, home = _sanitised_env(base_env={"PATH": "/usr/bin:/bin"}, auth_file=key,
                                   auth_mode="api-key", provider="claude")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return env, home, key

    def test_the_helper_reads_a_private_copy_in_the_isolated_home(self) -> None:
        import stat
        import subprocess
        _env, home, key = self._env()
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        helper = Path(settings["apiKeyHelper"])
        self.assertTrue(helper.is_relative_to(home))
        self.assertNotIn(str(key), helper.read_text())
        copy = home / ".claude" / "api-key"
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        done = subprocess.run([str(helper)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout, "sk-test-KEY-123")

    def test_it_works_inside_a_real_boundary(self) -> None:
        import subprocess
        from openfarm_core import confine
        usable, why = confine.bwrap_status()
        if not usable:
            self.skipTest(f"bwrap is not usable here: {why}")
        _env, home, key = self._env()
        session = Path(tempfile.mkdtemp(prefix="openfarm-claude-key-session-"))
        self.addCleanup(shutil.rmtree, session, ignore_errors=True)
        helper = json.loads((home / ".claude" / "settings.json").read_text())["apiKeyHelper"]
        script = f"echo helper=$({helper} 2>&1); echo real=$(cat {key} 2>&1 | head -c 40)"
        argv = confine.boundary_argv(["/bin/sh", "-c", script], session_dir=session,
                                     home_dir=home)
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        out = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        self.assertEqual(out.get("helper"), "sk-test-KEY-123", done.stderr)
        self.assertNotIn("sk-test", out.get("real", ""), "the real key file is not shown")


if __name__ == "__main__":
    unittest.main()
