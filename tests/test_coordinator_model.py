"""Tests for Stage 8 (Done 6): ModelCoordinator (tier:<name>), caps, fallback, and HTTP headers."""

from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.board import Board, board_dir_for
from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.state import StateDir
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _run_cli(repo: Path, *args: str) -> tuple[int, dict, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--repo", str(repo), "--json", *args])
    raw = out.getvalue().strip()
    return code, (json.loads(raw) if raw else {}), err.getvalue()


class ModelCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def _setup_two_cards(
        self,
        *,
        coordinator_calls_per_run: int = 60,
        coordinator_tokens_per_run: int = 600_000,
        endpoint: str = "https://api.example.invalid/v1",
        model_adapter: str = "scripted",
        concurrent_cards: int = 2,
    ) -> dict:
        self.toy.write("tally/alpha.py", "def a(x: int) -> int:\n    return x\n")
        self.toy.write("tally/beta.py", "def b(x: int) -> int:\n    return x\n")
        self.toy.commit("add alpha and beta")

        cfg_path = self.toy.root / ".openfarm" / "config.toml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        if model_adapter != "scripted":
            cfg_text = cfg_text.replace('model = "scripted"', f'model = "{model_adapter}"')
            key_path = self.toy.base / "coord-test.key"
            key_path.write_text("sk-test-zdr\n", encoding="utf-8")
            cfg_text = cfg_text.replace(
                'endpoint = "https://scripted.invalid/v1"',
                f'endpoint = "{endpoint}"',
            )
            cfg_text = cfg_text.replace(
                'allowed_endpoints = ["https://scripted.invalid/v1"]',
                (
                    f'allowed_endpoints = ["{endpoint}"]\n'
                    f'key_file = "{key_path}"\n'
                    'headers = {"x-cmd-zdr" = "1"}'
                ),
            )
        if "concurrent_cards =" in cfg_text:
            cfg_text = cfg_text.replace(
                "concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}"
            )
        else:
            cfg_text = cfg_text.replace(
                "concurrent_builds = 2",
                f"concurrent_builds = 2\nconcurrent_cards = {concurrent_cards}",
            )
        cfg_text += (
            '\n[tiers.dsf]\n'
            'model = "stub"\n'
            'efforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n'
            '\n[tiers.coord]\n'
            'model = "deepseek/deepseek-coord"\n'
            'efforts = ["low", "medium", "high", "max"]\n'
            'effort_param = "reasoning_effort"\n'
            '\n[swarm]\n'
            'enabled = true\n'
            'coordinator = "tier:coord"\n'
            'coordinator_effort = "max"\n'
            'supervisor = "off"\n'
            'default_tier = "dsf"\n'
            f'coordinator_calls_per_run = {coordinator_calls_per_run}\n'
            f'coordinator_tokens_per_run = {coordinator_tokens_per_run}\n'
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")

        self.toy.write_card(
            "CARD-1",
            title="Alpha",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/alpha.py"],
            tests=[{"name": "test_a", "file": "tests/test_a.py", "asserts": "a(1)==2"}],
            done_text="`a(x)` returns `x + 1`.",
        )
        self.toy.write_card(
            "CARD-2",
            title="Beta",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/beta.py"],
            tests=[{"name": "test_b", "file": "tests/test_b.py", "asserts": "b(1)==3"}],
            done_text="`b(x)` returns `x + 2`.",
        )
        self.toy.commit("add CARD-1 and CARD-2")

        return {
            "CARD-1/test_writer": (
                "=== whole_file: tests/test_a.py\n"
                "import unittest\nfrom tally.alpha import a\n"
                "class T(unittest.TestCase):\n"
                "    def test_a(self): self.assertEqual(a(1), 2)\n"
            ),
            "CARD-1/implementer/1/low": {
                "content": "=== whole_file: tally/alpha.py\ndef a(x: int) -> int:\n    return x + 1\n",
                "tokens_in": 30,
                "tokens_out": 10,
            },
            "CARD-1/checker_pass1": "RESTATEMENT: a returns x + 1",
            "CARD-1/checker_pass2": "{}",
            "CARD-2/test_writer": (
                "=== whole_file: tests/test_b.py\n"
                "import unittest\nfrom tally.beta import b\n"
                "class T(unittest.TestCase):\n"
                "    def test_b(self): self.assertEqual(b(1), 3)\n"
            ),
            "CARD-2/implementer/1/high": {
                "content": "=== whole_file: tally/beta.py\ndef b(x: int) -> int:\n    return x + 2\n",
                "tokens_in": 45,
                "tokens_out": 15,
            },
            "CARD-2/checker_pass1": "RESTATEMENT: b returns x + 2",
            "CARD-2/checker_pass2": "{}",
        }

    def test_starts_cards_with_different_efforts_and_falls_back_after_two_bad_answers(self) -> None:
        base_script = self._setup_two_cards(concurrent_cards=1)
        script = {
            **base_script,
            # Call 1 (plan): bad action ("done") -> refused and posted back!
            "coordinator/1": {
                "content": json.dumps({"actions": [{"action": "done", "card": "CARD-1"}]}),
                "tokens_in": 50,
                "tokens_out": 10,
            },
            # Call 2 (retry plan after seeing refusal): start CARD-1 at low!
            "coordinator/2": {
                "content": json.dumps({
                    "actions": [{
                        "action": "start",
                        "card": "CARD-1",
                        "team": {"implementer": {"tier": "dsf", "effort": "low"}},
                    }]
                }),
                "tokens_in": 60,
                "tokens_out": 20,
            },
            # Call 3 (finished CARD-1): valid start CARD-2 at high!
            "coordinator/3": {
                "content": json.dumps({
                    "actions": [{
                        "action": "start",
                        "card": "CARD-2",
                        "team": {"implementer": {"tier": "dsf", "effort": "high"}},
                    }]
                }),
                "tokens_in": 65,
                "tokens_out": 22,
            },
            # Call 4 (finished CARD-2): wait cleanly
            "coordinator/4": {
                "content": json.dumps({"actions": [{"action": "wait", "reason": "done"}]}),
                "tokens_in": 20,
                "tokens_out": 5,
            },
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")

        code, payload, err = _run_cli(
            self.toy.root, "run", "--cards", "2", "--script", str(sp)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(
            {o["card_id"]: o["final_state"] for o in payload["outcomes"]},
            {"CARD-1": "in-review", "CARD-2": "in-review"},
        )
        # Coordinator tokens recorded on coordinator member and by_effort["max"]
        self.assertGreater(payload["tokens"]["by_member"].get("coordinator", 0), 0)
        self.assertEqual(payload["tokens"]["by_effort"].get("low", 0), 40)
        self.assertEqual(payload["tokens"]["by_effort"].get("high", 0), 60)

        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        posts = board.read(as_name="*", since=0, limit=None, for_model=False)
        refusals = [p for p in posts if p["kind"] == "action" and "refused" in p["body"]]
        self.assertEqual(len(refusals), 1)
        self.assertIn("unknown coordinator action 'done'", refusals[0]["body"])

    def test_two_bad_answers_in_a_row_hand_decision_to_code_policy(self) -> None:
        base_script = self._setup_two_cards(concurrent_cards=2)
        script = {
            **base_script,
            # Use default effort "max" / worker entries without explicit effort suffix too
            "CARD-1/implementer": "=== whole_file: tally/alpha.py\ndef a(x: int) -> int:\n    return x + 1\n",
            "CARD-2/implementer": "=== whole_file: tally/beta.py\ndef b(x: int) -> int:\n    return x + 2\n",
            "coordinator/1": {"content": "not json at all", "tokens_in": 10, "tokens_out": 5},
            "coordinator/2": {
                "content": json.dumps({"actions": [{"action": "set", "member": "CARD-1-implementer", "effort": "bogus"}]}),
                "tokens_in": 12,
                "tokens_out": 6,
            },
            # After 2 bad answers on plan, CodeCoordinator takes plan and starts CARD-1 & CARD-2!
            "coordinator": {
                "content": json.dumps({"actions": [{"action": "wait"}]}),
                "tokens_in": 10,
                "tokens_out": 4,
            },
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")

        code, payload, err = _run_cli(
            self.toy.root, "run", "--cards", "2", "--script", str(sp)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(
            {o["card_id"]: o["final_state"] for o in payload["outcomes"]},
            {"CARD-1": "in-review", "CARD-2": "in-review"},
        )

    def test_coordinator_call_and_token_caps_switch_to_code_permanently(self) -> None:
        base_script = self._setup_two_cards(
            coordinator_calls_per_run=1, concurrent_cards=1
        )
        script = {
            **base_script,
            "CARD-1/implementer": "=== whole_file: tally/alpha.py\ndef a(x: int) -> int:\n    return x + 1\n",
            "CARD-2/implementer": "=== whole_file: tally/beta.py\ndef b(x: int) -> int:\n    return x + 2\n",
            "coordinator/1": {
                "content": json.dumps({
                    "actions": [{"action": "start", "card": "CARD-1"}]
                }),
                "tokens_in": 40,
                "tokens_out": 15,
            },
            # Would raise if a second coordinator model call were ever attempted!
            "coordinator/2": {"fail": True, "kind": "refused", "code": "cap-missed", "message": "should not call coordinator after cap"},
        }
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sp = tmp / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")

        code, payload, err = _run_cli(
            self.toy.root, "run", "--cards", "2", "--script", str(sp)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(
            {o["card_id"]: o["final_state"] for o in payload["outcomes"]},
            {"CARD-1": "in-review", "CARD-2": "in-review"},
        )
        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, payload["run_id"]), state_dir=state)
        self.assertEqual(board.get_member("coordinator")["calls"], 1)

    def test_loopback_http_carries_x_cmd_zdr_and_coordinator_effort(self) -> None:
        captured: list[tuple[dict, dict]] = []
        guard = threading.Lock()

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                with guard:
                    captured.append((hdrs, body))
                model = body.get("model", "")
                sys_msg = body.get("messages", [{}])[0].get("content", "")
                if model == "deepseek/deepseek-coord":
                    reply = json.dumps({"actions": [{"action": "start", "card": "CARD-1"}]})
                elif "test_writer" in str(body) or "You write the tests" in str(body):
                    reply = "=== whole_file: tests/test_a.py\nimport unittest\nfrom tally.alpha import a\nclass T(unittest.TestCase):\n    def test_a(self): self.assertEqual(a(1), 2)\n"
                elif "You make the frozen tests pass" in str(body):
                    reply = "=== whole_file: tally/alpha.py\ndef a(x: int) -> int:\n    return x + 1\n"
                elif "pass 2" in str(body) or "traceability" in str(body):
                    reply = "{}"
                else:
                    reply = "RESTATEMENT: a returns x + 1"
                resp = {
                    "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                }
                encoded = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _fmt: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        self._setup_two_cards(
            endpoint=f"http://127.0.0.1:{port}/v1",
            model_adapter="openai-compatible",
            concurrent_cards=1,
        )
        code, payload, err = _run_cli(self.toy.root, "run", "--cards", "1")
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(payload["outcomes"][0]["final_state"], "in-review")

        coord_reqs = [
            (h, b) for h, b in captured if b.get("model") == "deepseek/deepseek-coord"
        ]
        self.assertGreaterEqual(len(coord_reqs), 1)
        for hdrs, body in coord_reqs:
            self.assertEqual(hdrs.get("x-cmd-zdr"), "1")
            self.assertEqual(body.get("reasoning_effort"), "max")


if __name__ == "__main__":
    unittest.main()
