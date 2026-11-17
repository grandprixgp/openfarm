"""Tests for Stage 1: tiers, effort, StopCode, replan_if, and globs_overlap."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.claude_worker import ClaudeAcpWorkerBackend
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adapters.openai_compat import ScriptedBackend, open_backend
from openfarm_core.briefs import Brief
from openfarm_core.cards import Card, OPTIONAL_CARD_FIELDS
from openfarm_core.config import Config, DEFAULT_EFFORTS
from openfarm_core.errors import ConfigError, Invalid, StopCode
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.globs import globs_overlap
from openfarm_core.protocol import CmdModelBackend
from openfarm_core.roles import Roles


def _base_config(state: Path, *, endpoint: str = "https://api.example.invalid/v1") -> dict:
    return {
        "project": {"name": "demo", "state_dir": str(state)},
        "adapters": {"languages": ["python"], "model": "openai-compatible"},
        "model": {
            "endpoint": endpoint,
            "allowed_endpoints": [endpoint],
            "headers": {"x-cmd-zdr": "1"},
            "implementer": {"model": "deepseek/deepseek-v4.1-flash"},
        },
        "policy": {"categories": ["pattern"]},
    }


def _start_loopback_server():
    captured: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            captured.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw),
            })
            payload = json.dumps({
                "model": "deepseek/deepseek-v4.1-flash",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, captured


class TiersAndEffortConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"

    def test_default_synthesized_tier_and_swarm_defaults(self) -> None:
        cfg = Config(_base_config(self.state), self.repo)
        self.assertEqual(list(cfg.tiers), ["default"])
        tier = cfg.tiers["default"]
        self.assertEqual(tier.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(tier.efforts, DEFAULT_EFFORTS)
        self.assertEqual(tier.effort_param, "reasoning_effort")
        self.assertEqual(cfg.swarm.enabled, False)
        self.assertEqual(cfg.swarm.coordinator, "code")
        self.assertEqual(cfg.swarm.supervisor, "config")
        self.assertEqual(cfg.swarm.coordinator_effort, "max")
        self.assertEqual(cfg.swarm.supervisor_effort, "max")
        self.assertEqual(cfg.swarm.default_tier, "default")
        self.assertEqual(cfg.swarm.escalate_after, 2)
        self.assertEqual(cfg.swarm.handbacks_per_card, 2)
        self.assertEqual(cfg.swarm.max_members, 24)
        self.assertEqual(cfg.swarm.coordinator_calls_per_run, 60)
        self.assertEqual(cfg.swarm.coordinator_tokens_per_run, 600_000)
        self.assertEqual(cfg.swarm.external_wait_s, 1800.0)
        self.assertEqual(cfg.swarm.on_external_timeout, "fallback")
        self.assertEqual(cfg.swarm.stall_after_s, 300.0)

    def test_tier_refuses_endpoint_key_headers_and_bad_effort_param(self) -> None:
        for forbidden in ("endpoint", "allowed_endpoints", "key_file", "headers"):
            data = _base_config(self.state)
            data["tiers"] = {"dsf": {"model": "m", forbidden: "nope"}}
            with self.assertRaises(ConfigError, msg=forbidden):
                Config(data, self.repo)

        data = _base_config(self.state)
        data["tiers"] = {"dsf": {"model": "m", "bogus": 1}}
        with self.assertRaises(ConfigError):
            Config(data, self.repo)

        for bad_param in ("bad param", "x\ny", "model", "messages", "max_tokens",
                          "temperature", "seed", "response_format"):
            data = _base_config(self.state)
            data["tiers"] = {"dsf": {"model": "m", "effort_param": bad_param}}
            with self.assertRaises(ConfigError, msg=repr(bad_param)):
                Config(data, self.repo)

    def test_tier_empty_effort_param_defaults_to_empty_efforts(self) -> None:
        data = _base_config(self.state)
        data["tiers"] = {"plain": {"model": "deepseek/deepseek-v4.1-flash", "effort_param": ""}}
        cfg = Config(data, self.repo)
        self.assertEqual(cfg.tiers["plain"].efforts, ())
        self.assertEqual(cfg.tiers["plain"].effort_param, "")

    def test_role_model_effort_validated_and_works_with_swarm_off(self) -> None:
        data = _base_config(self.state)
        data["tiers"] = {
            "dsf": {
                "model": "deepseek/deepseek-v4.1-flash",
                "efforts": ["low", "medium", "high"],
                "effort_param": "reasoning_effort",
            }
        }
        data["model"]["implementer"] = {
            "model": "deepseek/deepseek-v4.1-flash",
            "effort": "medium",
        }
        data["swarm"] = {"enabled": False}
        cfg = Config(data, self.repo)
        self.assertEqual(cfg.role_models["implementer"].effort, "medium")
        self.assertEqual(cfg.effort_for_role("implementer"), ("medium", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("checker_pass1"), ("", ""))

        # Omitted max on tier does not fail when coordinator is "code"
        self.assertEqual(cfg.swarm.coordinator, "code")

        bad = _base_config(self.state)
        bad["tiers"] = data["tiers"]
        bad["model"]["implementer"] = {
            "model": "deepseek/deepseek-v4.1-flash",
            "effort": "max",
        }
        with self.assertRaises(ConfigError):
            Config(bad, self.repo)

    def test_swarm_validation_refuses_bad_seats_efforts_and_bounds(self) -> None:
        cases = [
            {"coordinator": "bogus"},
            {"coordinator": "tier:missing"},
            {"supervisor": "code"},
            {"supervisor": "tier:missing"},
            {"coordinator": "tier:dsf"},  # default "max" not in ["low", "high"]
            {"coordinator": "code", "coordinator_effort": "turbo"},
            {"supervisor": "tier:dsf", "supervisor_effort": "turbo"},
            {"default_tier": "missing"},
            {"escalate_after": 0},
            {"handbacks_per_card": -1},
            {"max_members": 0},
            {"coordinator_calls_per_run": -1},
            {"coordinator_tokens_per_run": -1},
            {"external_wait": "0s"},
            {"on_external_timeout": "retry"},
            {"stall_after": "0m"},
            {"unknown_key": True},
        ]
        for swarm_cfg in cases:
            data = _base_config(self.state)
            data["tiers"] = {
                "dsf": {
                    "model": "deepseek/deepseek-v4.1-flash",
                    "efforts": ["low", "high"],
                }
            }
            data["swarm"] = swarm_cfg
            with self.assertRaises(ConfigError, msg=repr(swarm_cfg)):
                Config(data, self.repo)


class LoopbackEffortHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"
        self.key = self.tmp / "worker.key"
        self.key.write_text("test-key\n")
        self.server, self.thread, self.captured = _start_loopback_server()
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def test_effort_and_zdr_header_reach_http_body(self) -> None:
        data = _base_config(self.state, endpoint=self.endpoint)
        data["tiers"] = {
            "dsf": {
                "model": "deepseek/deepseek-v4.1-flash",
                "efforts": ["low", "medium", "high", "max"],
                "effort_param": "reasoning_effort",
            }
        }
        data["model"]["implementer"] = {
            "model": "deepseek/deepseek-v4.1-flash",
            "effort": "medium",
        }
        data["swarm"] = {"enabled": False}
        cfg = Config(data, self.repo)
        cfg.key_file = self.key
        backend = open_backend(cfg)

        roles = Roles(cfg, backend, card_id="CARD-1")
        call = roles.call("implementer", Brief("implementer", "solve it"))
        self.assertEqual(call.effort, "medium")
        self.assertEqual(call.effort_param, "reasoning_effort")
        self.assertEqual(len(self.captured), 1)
        req = self.captured[0]
        self.assertEqual(req["headers"]["x-cmd-zdr"], "1")
        self.assertEqual(req["body"]["reasoning_effort"], "medium")
        self.assertEqual(req["body"]["model"], "deepseek/deepseek-v4.1-flash")

        # Explicit member override on Roles.call
        call2 = roles.call(
            "implementer",
            Brief("implementer", "harder"),
            model="deepseek/deepseek-v4.1-flash",
            effort="high",
            effort_param="reasoning_effort",
        )
        self.assertEqual(call2.effort, "high")
        req2 = self.captured[1]
        self.assertEqual(req2["headers"]["x-cmd-zdr"], "1")
        self.assertEqual(req2["body"]["reasoning_effort"], "high")

        # Empty effort omits effort_param while keeping x-cmd-zdr
        roles.call("test_writer", Brief("test_writer", "tests"))
        req3 = self.captured[2]
        self.assertEqual(req3["headers"]["x-cmd-zdr"], "1")
        self.assertNotIn("reasoning_effort", req3["body"])

    def test_reserved_effort_param_refused_by_backend(self) -> None:
        cfg = Config(_base_config(self.state, endpoint=self.endpoint), self.repo)
        backend = open_backend(cfg)
        with self.assertRaises(Invalid):
            backend.complete(
                "implementer",
                "deepseek/deepseek-v4.1-flash",
                0.2,
                [{"role": "user", "content": "hi"}],
                effort="high",
                effort_param="model",
            )


class BackendEffortRecordingTests(unittest.TestCase):
    def test_scripted_backend_records_effort_and_fallbacks(self) -> None:
        backend = ScriptedBackend({
            "CARD-1/implementer/1/high": "high-answer",
            "implementer/2": "attempt-2",
            "implementer": "default-impl",
        })
        c1 = backend.complete(
            "implementer", "m", 0.2, [{"role": "user", "content": "a"}],
            card_id="CARD-1", attempt=1, effort="high", effort_param="reasoning_effort",
        )
        self.assertEqual(c1.content, "high-answer")
        self.assertEqual(backend.calls[0]["effort"], "high")
        self.assertEqual(backend.calls[0]["effort_param"], "reasoning_effort")

        c2 = backend.complete(
            "implementer", "m", 0.2, [{"role": "user", "content": "b"}],
            card_id="CARD-2", attempt=2,
        )
        self.assertEqual(c2.content, "attempt-2")

        # Thread-safe recording across concurrent calls
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                backend.complete(
                    "implementer", "m", 0.2, [{"role": "user", "content": str(i)}],
                    card_id="CARD-3", attempt=1, effort="low", effort_param="reasoning_effort",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(backend.calls), 10)

    def test_cmd_backend_accepts_effort(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        adapter_py = tmp / "model_adapter.py"
        adapter_py.write_text(
            "import json, sys\n"
            "req = json.load(sys.stdin)\n"
            "if req['op'] == 'hello':\n"
            "    res = {'name': 'm', 'versions': [1], 'ops': ['hello', 'complete']}\n"
            "else:\n"
            "    res = {'content': req['args'].get('effort', ''), 'tokens_in': 1, 'tokens_out': 1}\n"
            "print(json.dumps({'openfarm': 1, 'request_id': req['request_id'], 'ok': True, 'result': res}))\n",
            encoding="utf-8",
        )
        mb = CmdModelBackend(f"cmd:{sys.executable} {adapter_py}", tmp)
        comp = mb.complete(
            "implementer", "m", 0.2, [{"role": "user", "content": "hi"}],
            effort="xhigh", effort_param="reasoning_effort",
        )
        self.assertEqual(comp.content, "xhigh")

    def test_claude_worker_accepts_effort(self) -> None:
        from types import SimpleNamespace

        seen: list[str] = []

        class _StubAcp:
            _initialized = True

            def new_session(self, *, cwd, system_prompt, model, effort, tool_policy, timeout_s):
                seen.append(effort)
                return "sess-1"

            def prompt(self, session_id, text, timeout_s):
                return SimpleNamespace(
                    text="done",
                    tokens_in=4,
                    tokens_out=2,
                    vendor_cost_usd=0.0,
                    stop_reason="end_turn",
                    latency_s=0.01,
                )

            def close(self) -> None:
                return

        backend = ClaudeAcpWorkerBackend(effort="medium", adapter=_StubAcp())
        comp = backend.complete(
            "implementer", "claude", 0.2, [{"role": "user", "content": "hi"}],
            effort="high", effort_param="reasoning_effort",
        )
        self.assertEqual(comp.content, "done")
        self.assertEqual(seen, ["high"])
        backend.complete("implementer", "claude", 0.2, [{"role": "user", "content": "hi"}])
        self.assertEqual(seen, ["high", "medium"])


class StopCodeReplanAndGlobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"

    def test_new_stop_codes(self) -> None:
        self.assertEqual(StopCode.HANDED_BACK.value, "handed-back")
        self.assertEqual(StopCode.SEAT_SILENT.value, "seat-silent")

    def test_replan_if_card_and_eligibility(self) -> None:
        self.assertIn("replan_if", OPTIONAL_CARD_FIELDS)
        cfg = Config(_base_config(self.state), self.repo)
        cfg.languages = ["python"]
        cfg.categories = ["pinned-bugfix", "pattern"]
        cfg.require_reference = []

        valid_card = Card(
            id="CARD-1",
            title="Check replan_if",
            state="ready",
            done_text="returns `42`",
            touches=["pkg/a.py"],
            fields={
                "category": "pinned-bugfix",
                "tests": [{"name": "test_ok", "file": "tests/test_a.py"}],
                "replan_if": ["test already passes on base", "symbol missing"],
            },
        )
        self.assertEqual(valid_card.replan_if, ["test already passes on base", "symbol missing"])
        self.assertTrue(check_eligibility(valid_card, cfg).ok)

        for bad_val in ("a string", [123], {"a": "b"}):
            bad_card = Card(
                id="CARD-2",
                title="Bad replan_if",
                state="ready",
                done_text="returns `42`",
                touches=["pkg/a.py"],
                fields={
                    "category": "pinned-bugfix",
                    "tests": [{"name": "test_ok", "file": "tests/test_a.py"}],
                    "replan_if": bad_val,
                },
            )
            # With the swarm off the field is unknown, as in v0.7, and is ignored.
            cfg.swarm.enabled = False
            self.assertNotIn(
                "replan-if",
                [f["rule"] for f in check_eligibility(bad_card, cfg).failures],
            )
            cfg.swarm.enabled = True
            res = check_eligibility(bad_card, cfg)
            self.assertFalse(res.ok, repr(bad_val))
            self.assertIn("replan-if", [f["rule"] for f in res.failures])

        # Round-trip through FileCardSource
        src = FileCardSource(self.repo, "cards", worktree=self.tmp / "wt", fields_block="toml")
        src.cards_dir.mkdir(parents=True)
        src.add_card(valid_card, commit=False)
        loaded = src.read("CARD-1")["card"]
        self.assertEqual(loaded["fields"]["replan_if"], ["test already passes on base", "symbol missing"])

    def test_globs_overlap_cases(self) -> None:
        should_overlap = [
            ("src/a.py", "src/a.py"),
            ("./src/a.py", "src//a.py"),
            ("src/*.py", "src/a.py"),
            ("src/*", "src/**"),
            ("src/auth/**", "src/auth"),
            ("a/**/b", "a/b"),
            ("a/**/b", "a/x/y/b"),
            ("**/Cargo.toml", "Cargo.toml"),
            ("**/Cargo.toml", "crates/foo/Cargo.toml"),
            ("**/*_test.rs", "crates/*/tests/*.rs"),
            ("src/?a.rs", "src/b?.rs"),
            ("src/a*", "src/*b"),
            ("a**b", "a/b"),
            ("crates/*/src/*.rs", "crates/parse/src/lib.rs"),
        ]
        for p1, p2 in should_overlap:
            self.assertTrue(globs_overlap(p1, p2), f"expected overlap: {p1!r} vs {p2!r}")
            self.assertTrue(globs_overlap(p2, p1), f"expected symmetric overlap: {p2!r} vs {p1!r}")

        should_not_overlap = [
            ("src/a.py", "src/b.py"),
            ("src/*", "src"),
            ("src/auth/*", "src/auth"),
            ("src/*", "src/*/*"),
            ("a/*.rs", "a/b/*.rs"),
            ("a/**/c.rs", "b/**/c.rs"),
            ("src/?.rs", "src/??.rs"),
            ("src/a*", "src/b*"),
            ("src/*a", "src/*b"),
            ("a*b", "a/b"),
            ("a/*/b", "a/b"),
            ("a/**/b/**/c", "a/xb/c"),
            ("crates/*/src/*.rs", "crates/parse/tests/*.rs"),
            ("a/??", "a/..*"),
            ("*", ".."),
            ("", "**"),
            ("../outside.py", "**"),
        ]
        for p1, p2 in should_not_overlap:
            self.assertFalse(globs_overlap(p1, p2), f"expected disjoint: {p1!r} vs {p2!r}")
            self.assertFalse(globs_overlap(p2, p1), f"expected symmetric disjoint: {p2!r} vs {p1!r}")


if __name__ == "__main__":
    unittest.main()
