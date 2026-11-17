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


if __name__ == "__main__":
    unittest.main()
