"""Tests for Stage 5: TierMicromanagerBackend across all 6 supervisor ops."""

from __future__ import annotations

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

from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.board import Board, board_dir_for
from openfarm_core.cards import Card
from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.config import Config
from openfarm_core.micromanager import (
    MicromanagerBudget,
    TierMicromanagerBackend,
    open_micromanager,
)
from openfarm_core.state import StateDir
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _make_config(
    repo: Path,
    state_root: Path,
    *,
    endpoint: str = "https://api.example.invalid/v1",
    supervisor: str = "tier:dsf",
    supervisor_effort: str = "max",
    model_adapter: str = "openai-compatible",
) -> Config:
    return Config(
        {
            "project": {"name": "demo", "base_branch": "main", "state_dir": str(state_root)},
            "adapters": {
                "languages": ["python"],
                "model": model_adapter,
                "micromanager": "claude-acp",
            },
            "model": {
                "endpoint": endpoint,
                "allowed_endpoints": [endpoint],
                "headers": {"x-cmd-zdr": "1"},
                "implementer": {"model": "deepseek/deepseek-v4.1-flash"},
            },
            "tiers": {
                "dsf": {
                    "model": "deepseek/deepseek-v4.1-flash",
                    "efforts": ["low", "medium", "high", "max"],
                    "effort_param": "reasoning_effort",
                }
            },
            "swarm": {
                "enabled": True,
                "coordinator": "code",
                "supervisor": supervisor,
                "supervisor_effort": supervisor_effort,
            },
            "micromanager": {
                "mode": "active",
                "binary": "/nonexistent/claude-not-used",
            },
            "policy": {"categories": ["pattern", "pinned-bugfix"]},
        },
        repo,
    )


def _sample_card() -> Card:
    return Card(
        id="CARD-1",
        title="Refuse push when full",
        state="ready",
        done_text="push on full returns `FullError`.",
        touches=["tally/bounded.py"],
        fields={
            "category": "pinned-bugfix",
            "new_symbols": True,
            "tests": [{"name": "test_full", "file": "tests/test_full.py"}],
        },
    )


class TierSupervisorAllOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state_root = self.tmp / "state"
        self.state = StateDir(self.state_root)
        self.cfg = _make_config(self.repo, self.state_root, model_adapter="scripted")
        self.board = Board(board_dir_for(self.state, "run-tier"), state_dir=self.state)
        self.board.ensure()

    def test_tier_supervisor_answers_all_six_ops_and_records_on_board(self) -> None:
        script_answers = {
            "CARD-1/supervisor:shape_and_plan": {
                "content": json.dumps({
                    "verdict": "ready",
                    "clarified_notes": "Keep FullError signature.",
                    "extra_context_files": [],
                    "steps": [],
                    "findings": [],
                }),
                "tokens_in": 40,
                "tokens_out": 15,
            },
            "CARD-1/supervisor:scaffold_contract": {
                "content": json.dumps({
                    "edits": [{
                        "format": "whole_file",
                        "path": "tally/bounded.py",
                        "content": "def push(x: int) -> int:\n    raise NotImplementedError\n",
                    }]
                }),
                "tokens_in": 50,
                "tokens_out": 20,
            },
            "CARD-1/supervisor:coach": {
                "content": json.dumps({
                    "root_cause": "missing capacity check",
                    "guidance": "Check len(segments) >= capacity before appending.",
                    "abort": False,
                    "abort_reason": "",
                }),
                "tokens_in": 45,
                "tokens_out": 18,
            },
            "CARD-1/supervisor:adjudicate_checker": {
                "content": json.dumps({
                    "verdict": "dismiss_false_reject",
                    "rationale": "counter-test assumes negative capacity",
                    "fix_hint": "",
                }),
                "tokens_in": 35,
                "tokens_out": 12,
            },
            "CARD-1/supervisor:pre_review": {
                "content": json.dumps({
                    "verdict": "approve",
                    "reviewer_brief": "Clean bounded fix.",
                    "missed_clause": "",
                    "findings": [],
                }),
                "tokens_in": 60,
                "tokens_out": 20,
            },
            "supervisor:triage_blocked": {
                "content": json.dumps({
                    "summary": "1 blocked card",
                    "recommendations": [{"card_id": "CARD-9", "action": "split"}],
                    "findings": [],
                }),
                "tokens_in": 30,
                "tokens_out": 10,
            },
            "CARD-1/supervisor:reflect_harness": {
                "content": json.dumps({
                    "summary": "Good recovery.",
                    "lessons": [{
                        "title": "Check capacity bounds",
                        "surface": "implementer_guidance",
                        "role": "implementer",
                        "language": "python",
                        "kind": "pinned-bugfix",
                        "cue": "Always check boundary equality on push.",
                        "lesson": "Verify boundary equality when enforcing capacity.",
                        "confidence": 0.8,
                        "scope": "general",
                        "scope_reason": "General boundary check.",
                    }],
                }),
                "tokens_in": 55,
                "tokens_out": 25,
            },
        }
        worker_backend = ScriptedBackend(script_answers)
        mm = open_micromanager(
            self.cfg,
            self.state_root,
            self.repo,
            card_dir=self.state_root / "cards" / "CARD-1",
            budget=MicromanagerBudget(max_calls_per_card=10),
            board=self.board,
            worker_backend=worker_backend,
        )
        self.assertIsInstance(mm.backend, TierMicromanagerBackend)
        card = _sample_card()

        shape = mm.shape_and_plan(card, {"tally/bounded.py": "def push():"}, {}, cfg=self.cfg)
        self.assertEqual(shape.verdict, "ready")
        self.assertEqual(shape.clarified_notes, "Keep FullError signature.")

        scaff = mm.scaffold_contract(card, {}, {})
        self.assertEqual(len(scaff), 1)
        self.assertEqual(scaff[0].path, "tally/bounded.py")

        advice = mm.coach(card, "implementer", 1, {}, "diff", "err", cfg=self.cfg)
        self.assertEqual(advice.root_cause, "missing capacity check")
        self.assertFalse(advice.abort)

        verdict = mm.adjudicate_checker(card, "diff", {"text": "ok"}, [{"name": "t"}])
        self.assertEqual(verdict.verdict, "dismiss_false_reject")

        review = mm.triage_and_review("pre_review", {"card_id": "CARD-1", "card": card.as_dict()})
        self.assertEqual(review.verdict, "approve")
        self.assertEqual(review.reviewer_brief, "Clean bounded fix.")

        triage = mm.triage_and_review("triage_blocked", {"cards": []})
        self.assertEqual(triage.summary, "1 blocked card")

        refl = mm.reflect_harness(card, {"card_id": "CARD-1", "kind": "pinned-bugfix"}, {}, "", {}, cfg=self.cfg)
        self.assertEqual(len(refl["lessons"]), 1)

        # Verify every call passed tier model + effort + effort_param
        self.assertEqual(len(worker_backend.calls), 7)
        for call in worker_backend.calls:
            self.assertTrue(call["role"].startswith("supervisor:"))
            self.assertEqual(call["model"], "deepseek/deepseek-v4.1-flash")
            self.assertEqual(call["effort"], "max")
            self.assertEqual(call["effort_param"], "reasoning_effort")

        # Verify board recorded supervisor questions, answers, and tokens
        sup_member = self.board.get_member("supervisor")
        self.assertIsNotNone(sup_member)
        self.assertEqual(sup_member["calls"], 7)
        self.assertEqual(sup_member["tokens_in"], 315)
        self.assertEqual(sup_member["tokens_out"], 120)
        posts = self.board.read(as_name="*", since=0, limit=None, for_model=False)
        self.assertEqual(sum(1 for p in posts if p["kind"] == "question"), 7)
        self.assertEqual(sum(1 for p in posts if p["kind"] == "answer"), 7)


class TierSupervisorLoopbackHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state_root = self.tmp / "state"
        self.key_file = self.tmp / "worker.key"
        self.key_file.write_text("secret-worker-key\n", encoding="utf-8")

        captured: list[dict] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                captured.append({
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw),
                })
                answer = {
                    "verdict": "approve",
                    "reviewer_brief": "Approved over loopback.",
                    "missed_clause": "",
                    "findings": [],
                }
                payload = json.dumps({
                    "model": "deepseek/deepseek-v4.1-flash",
                    "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 6},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.captured = captured
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def test_tier_supervisor_sends_zdr_header_and_effort_to_worker_endpoint(self) -> None:
        cfg = _make_config(
            self.repo,
            self.state_root,
            endpoint=self.endpoint,
            supervisor="tier:dsf",
            supervisor_effort="high",
            model_adapter="openai-compatible",
        )
        cfg.key_file = self.key_file
        mm = open_micromanager(cfg, self.state_root, self.repo)
        self.addCleanup(mm.close)
        report = mm.triage_and_review("pre_review", {"card_id": "CARD-1"})
        self.assertEqual(report.verdict, "approve")
        self.assertEqual(len(self.captured), 1)
        req = self.captured[0]
        self.assertEqual(req["headers"]["x-cmd-zdr"], "1")
        self.assertEqual(req["body"]["reasoning_effort"], "high")
        self.assertEqual(req["body"]["model"], "deepseek/deepseek-v4.1-flash")

    def test_cli_run_skips_claude_acp_handshake_when_tier_supervisor_enabled(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        cfg_path = toy.root / ".openfarm" / "config.toml"
        base = cfg_path.read_text(encoding="utf-8")
        cfg_path.write_text(
            base
            + '\n[tiers.dsf]\nmodel = "stub"\n'
            + 'efforts = ["low", "medium", "high", "max"]\n'
            + '\n[swarm]\nenabled = true\ncoordinator = "code"\n'
            + 'supervisor = "tier:dsf"\nsupervisor_effort = "high"\n'
            + '\n[micromanager]\nmode = "active"\nbinary = "/nonexistent/claude"\n',
            encoding="utf-8",
        )
        toy.write_card(
            "CARD-1",
            title="A full bundle refuses a push",
            category="pinned-bugfix",
            unit=".",
            touches=["tally/bounded.py"],
            tests=[{
                "name": "test_a_full_bundle_refuses_a_push",
                "file": "tests/test_full.py",
                "asserts": "push raises FullError",
            }],
            done_text="push on a full bundle raises `FullError`.",
        )
        toy.commit("add CARD-1 and tier supervisor")

        script_path = self.tmp / "script.json"
        script_path.write_text(
            json.dumps({
                "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "CARD-1/implementer": GOOD_IMPLEMENTATION,
                "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
                "CARD-1/checker_pass2": "{}",
                "CARD-1/supervisor:shape_and_plan": json.dumps({"verdict": "ready"}),
                "CARD-1/supervisor:pre_review": json.dumps({
                    "verdict": "approve",
                    "reviewer_brief": "LGTM from DeepSeek tier supervisor",
                }),
                "CARD-1/supervisor:reflect_harness": json.dumps({"lessons": []}),
            }),
            encoding="utf-8",
        )
        out = io.StringIO()
        err = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main([
                "--repo", str(toy.root), "--json", "run",
                "--card", "CARD-1", "--run-id", "run-tier-cli", "--script", str(script_path),
            ])
        self.assertEqual(code, EXIT_OK, err.getvalue())
        res = json.loads(out.getvalue())
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review")


if __name__ == "__main__":
    unittest.main()
