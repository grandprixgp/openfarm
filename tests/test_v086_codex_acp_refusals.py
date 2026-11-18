"""Live, 18 Nov 2026: a standalone codex-acp 0.16.0 turn, step by step.

The server refused any effort for a model it has no preset for ("Reasoning
effort can only be set for known model presets"), and openfarm stopped the card
as infra over a preference. Then the ChatGPT backend refused the model, and the
server's `Internal error` carried the reason only in `error.data`, which the
dossier dropped. An effort refusal is now a note, a model refusal still stops
the card, and an RPC error keeps the server's own detail.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from openfarm_core.adapters.acp import ClaudeAcpAdapter, _rpc_error_text
from openfarm_core.errors import Infra

_SERVER = r"""#!/usr/bin/env python3
import json, sys
REFUSE = sys.argv[1]
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
        out({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1", "configOptions": [
            {"id": "model", "category": "model", "type": "select", "currentValue": "a",
             "options": [{"value": "a"}, {"value": "b"}]},
            {"id": "reasoning_effort", "category": "thought_level", "type": "select",
             "currentValue": "medium", "options": [{"value": "medium"}, {"value": "high"}]}]}})
    elif method == "session/set_config_option":
        cid = params.get("configId")
        if cid == REFUSE:
            out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Invalid params",
                 "data": "Reasoning effort can only be set for known model presets"}})
            continue
        calls.append([cid, params.get("value")])
        out({"jsonrpc": "2.0", "id": mid, "result": {"configOptions": []}})
    elif method == "session/prompt":
        if REFUSE == "prompt":
            body = {"type": "error", "status": 400, "error": {"type": "invalid_request_error",
                    "message": "The 'b' model is not supported when using Codex with a ChatGPT account.",
                    "code": "model_not_found"}}
            out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "Internal error",
                 "data": {"message": json.dumps(body), "codex_error_info": "other"}}})
            continue
        out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps({"calls": calls})}}}})
        out({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid is not None:
        out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
"""


class CodexAcpRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v086-acp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server = self.tmp / "codex-acp"
        self.server.write_text(_SERVER)
        self.server.chmod(0o755)
        self.work = self.tmp / "work"
        self.work.mkdir()

    def _adapter(self, refuse: str) -> ClaudeAcpAdapter:
        return ClaudeAcpAdapter([str(self.server), refuse], cwd=self.work, provider="codex",
                                auth_mode="api-key", confine="off")

    def test_a_refused_effort_is_a_note_and_the_turn_goes_on(self) -> None:
        with self._adapter("reasoning_effort") as acp:
            sid = acp.new_session(cwd=self.work, model="b", effort="high")
            calls = json.loads(acp.prompt(sid, "go", timeout_s=30).text)["calls"]
            notes = list(acp.notes)
        self.assertEqual(calls, [["model", "b"]])
        self.assertTrue(any("refused effort 'high'" in n for n in notes), notes)

    def test_a_refused_model_still_stops(self) -> None:
        with self._adapter("model") as acp:
            with self.assertRaises(Infra) as caught:
                acp.new_session(cwd=self.work, model="b", effort="high")
        self.assertEqual(caught.exception.code, "acp-rpc--32602")

    def test_a_prompt_error_keeps_the_servers_reason(self) -> None:
        with self._adapter("prompt") as acp:
            sid = acp.new_session(cwd=self.work)
            with self.assertRaises(Infra) as caught:
                acp.prompt(sid, "go", timeout_s=30)
        self.assertIn("Internal error: The 'b' model is not supported when using Codex with a "
                      "ChatGPT account. (model_not_found)", str(caught.exception))


class RpcErrorTextTests(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertEqual(_rpc_error_text("Method not found", "session/close"),
                         "Method not found: session/close")
        self.assertEqual(_rpc_error_text("Oops", None), "Oops")
        self.assertEqual(_rpc_error_text("Oops", {"message": "Oops"}), "Oops")
        self.assertEqual(_rpc_error_text("E", {"message": '{"message": "inner"}'}), "E: inner")
        self.assertEqual(len(_rpc_error_text("E", "x" * 2000)), len("E: ") + 400)


if __name__ == "__main__":
    unittest.main()
