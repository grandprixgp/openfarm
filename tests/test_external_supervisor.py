"""Tests for Stage 5: ExternalMicromanagerBackend, board answer, timeout, pause_clock."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import DirectRunner
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adapters.jsonl import MemoryTelemetry
from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.board import Board, board_dir_for
from openfarm_core.briefs import BriefBook
from openfarm_core.budget import Budget
from openfarm_core.cards import Card
from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from openfarm_core.config import load_config
from openfarm_core.errors import GateFailure, Infra, StopCode
from openfarm_core.flow import CardRun
from openfarm_core.git import Git
from openfarm_core.harness_memory import HarnessLibrary
from openfarm_core.micromanager import ExternalMicromanagerBackend, Micromanager
from openfarm_core.packs.base import pack_for
from openfarm_core.state import StateDir
from openfarm_core.swarm import swarm_join
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy


def _configure_external_supervisor(
    toy,
    *,
    external_wait: str = "5s",
    on_timeout: str = "fallback",
) -> None:
    cfg_path = toy.root / ".openfarm" / "config.toml"
    base = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(
        base
        + f'\n[swarm]\nenabled = true\ncoordinator = "code"\n'
        + f'supervisor = "external"\nexternal_wait = "{external_wait}"\n'
        + f'on_external_timeout = "{on_timeout}"\n'
        + '\n[micromanager]\nmode = "active"\nbinary = "/nonexistent/claude"\n',
        encoding="utf-8",
    )
    toy.commit("enable external supervisor")


def _write_card_1(toy) -> None:
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
    toy.commit("add CARD-1")


class ExternalSupervisorEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)

    def _script_file(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "script.json"
        path.write_text(
            json.dumps({
                "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
                "CARD-1/implementer": GOOD_IMPLEMENTATION,
                "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
                "CARD-1/checker_pass2": "{}",
            }),
            encoding="utf-8",
        )
        return path

    def test_external_supervisor_answered_through_board_cli_finishes_card(self) -> None:
        _configure_external_supervisor(self.toy, external_wait="10s", on_timeout="block")
        _write_card_1(self.toy)
        script_path = self._script_file()

        run_id = "run-ext-ok"
        cfg = load_config(self.toy.root)
        state = StateDir(self.toy.state_dir)
        board = Board(board_dir_for(state, run_id), state_dir=state)
        board.ensure()
        swarm_join(board, cfg, name="human-sup", seat="supervisor", owner_kind="human")

        ans_dir = Path(tempfile.mkdtemp(prefix="openfarm-ans-"))
        self.addCleanup(shutil.rmtree, ans_dir, True)

        errors: list[str] = []
        invalid_checked = [False]

        def responder() -> None:
            cursor = 0
            while True:
                res = board.wait("human-sup", since=cursor, timeout_s=10.0)
                if not res.get("woke"):
                    break
                for post in res.get("posts") or []:
                    cursor = max(cursor, int(post["seq"]))
                    if post.get("kind") != "question":
                        continue
                    op = (post.get("expects") or {}).get("op")
                    if op == "shape_and_plan":
                        # First test that an invalid answer is refused without posting
                        bad_file = ans_dir / f"bad-{post['seq']}.json"
                        bad_file.write_text(json.dumps({"verdict": "bogus"}), encoding="utf-8")
                        out_b, err_b = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(out_b), contextlib.redirect_stderr(err_b):
                            rc_bad = cli_main([
                                "--repo", str(self.toy.root), "--json",
                                "board", "answer", str(post["seq"]),
                                "--as", "human-sup", "--file", str(bad_file),
                                "--run", run_id,
                            ])
                        if rc_bad != EXIT_REFUSED:
                            errors.append(f"expected EXIT_REFUSED on bad answer, got {rc_bad}")
                        invalid_checked[0] = True
                        payload = {"verdict": "ready", "clarified_notes": "External note"}
                    elif op == "pre_review":
                        payload = {"verdict": "approve", "reviewer_brief": "Approved by human"}
                    elif op == "reflect_harness":
                        payload = {"lessons": []}
                    else:
                        payload = {}
                    ans_file = ans_dir / f"ans-{post['seq']}.json"
                    ans_file.write_text(json.dumps(payload), encoding="utf-8")
                    out_ok, err_ok = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out_ok), contextlib.redirect_stderr(err_ok):
                        rc = cli_main([
                            "--repo", str(self.toy.root), "--json",
                            "board", "answer", str(post["seq"]),
                            "--as", "human-sup", "--file", str(ans_file),
                            "--run", run_id,
                        ])
                    if rc != EXIT_OK:
                        errors.append(f"answer {post['seq']} failed: {out_ok.getvalue()} {err_ok.getvalue()}")

        t = threading.Thread(target=responder, daemon=True)
        t.start()

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main([
                "--repo", str(self.toy.root), "--json", "run",
                "--card", "CARD-1", "--run-id", run_id, "--script", str(script_path),
            ])
        t.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertTrue(invalid_checked[0])
        self.assertEqual(code, EXIT_OK, err.getvalue())
        res = json.loads(out.getvalue())
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review")

    def test_external_timeout_fallback_finishes_card(self) -> None:
        _configure_external_supervisor(self.toy, external_wait="0.1s", on_timeout="fallback")
        _write_card_1(self.toy)
        script_path = self._script_file()

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main([
                "--repo", str(self.toy.root), "--json", "run",
                "--card", "CARD-1", "--run-id", "run-ext-fallback", "--script", str(script_path),
            ])
        self.assertEqual(code, EXIT_OK, err.getvalue())
        res = json.loads(out.getvalue())
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review")

    def test_external_timeout_block_gives_seat_silent(self) -> None:
        _configure_external_supervisor(self.toy, external_wait="0.1s", on_timeout="block")
        _write_card_1(self.toy)
        script_path = self._script_file()

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main([
                "--repo", str(self.toy.root), "--json", "run",
                "--card", "CARD-1", "--run-id", "run-ext-block", "--script", str(script_path),
            ])
        self.assertEqual(code, EXIT_OK, err.getvalue())
        res = json.loads(out.getvalue())
        outcome = res["outcomes"][0]
        self.assertEqual(outcome["final_state"], "blocked")
        self.assertEqual(outcome["stop_code"], "seat-silent")

    def test_seat_silent_during_reflect_harness_blocks_without_corrupting_metrics(self) -> None:
        _configure_external_supervisor(self.toy, external_wait="0.15s", on_timeout="block")
        _write_card_1(self.toy)

        cfg = load_config(self.toy.root)
        # v0.8.5 leaves a clean first-try pass out of reflection by default; this
        # test is about the reflect call, so it asks for it on a pass too.
        cfg.harness.reflect_on = (*cfg.harness.reflect_on, "pass")
        state = StateDir(self.toy.state_dir)
        run_id = "run-reflect-silent"
        board = Board(board_dir_for(state, run_id), state_dir=state)
        board.ensure()
        swarm_join(board, cfg, name="sup", seat="supervisor", owner_kind="agent")

        source = FileCardSource(
            self.toy.root, "cards", worktree=state.root / "cards-source", base_branch="main"
        )
        source.prepare()
        claim = source.claim("CARD-1", f"openfarm:{run_id}", cfg.lease_ttl_s)
        card = Card.from_dict(source.read("CARD-1")["card"])

        worktree = state.worktrees / f"CARD-1-{run_id}"
        Git(self.toy.root).worktree_add(worktree, "main")
        self.addCleanup(Git(self.toy.root).worktree_remove, worktree)

        card_dir = state.create_card("CARD-1", run_id)
        pack = pack_for("python", cwd=worktree, python=sys.executable)
        pack.configure(worktree)
        backend = ScriptedBackend({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
            "CARD-1/checker_pass2": "{}",
        })
        book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
        book.pack = pack
        harness_lib = HarnessLibrary(state.harness(), config=cfg)

        # Answer shape_and_plan and pre_review, leave reflect_harness unanswered
        def partial_responder() -> None:
            cursor = 0
            while True:
                res = board.wait("sup", since=cursor, timeout_s=5.0)
                if not res.get("woke"):
                    break
                for post in res.get("posts") or []:
                    cursor = max(cursor, int(post["seq"]))
                    if post.get("kind") != "question":
                        continue
                    op = (post.get("expects") or {}).get("op")
                    if op == "shape_and_plan":
                        board.answer(post["seq"], as_name="sup", data={"verdict": "ready"})
                    elif op == "pre_review":
                        board.answer(post["seq"], as_name="sup", data={"verdict": "approve"})
                    elif op == "reflect_harness":
                        return

        t = threading.Thread(target=partial_responder, daemon=True)
        t.start()

        cr = CardRun(
            card=card, cfg=cfg, state=state, card_dir=card_dir, worktree=worktree,
            runner=DirectRunner(worktree), pack=pack, backend=backend, book=book,
            run_id=run_id, telemetry=MemoryTelemetry(), deps_done=True,
            harness_library=harness_lib, board=board, swarm=cfg.swarm,
        )
        cr.card_source = source
        record = cr.run(claim=claim)
        board.end_run("finished")
        t.join(timeout=2.0)

        self.assertEqual(record["final_state"], "blocked")
        self.assertEqual(record["stop_code"], "seat-silent")
        # Harness metrics must remain clean (0 cards recorded)
        self.assertEqual(harness_lib.status()["metrics"]["cards_total"], 0)


class ExternalWaitMechanicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = StateDir(self.tmp / "state")
        self.board = Board(board_dir_for(self.state, "run-mech"), state_dir=self.state)
        self.board.ensure()
        self.board.upsert_member("sup", kind="external", role="supervisor", owner_kind="human")

    def test_pause_clock_heartbeat_and_kill_switch(self) -> None:
        budget = Budget(wall_clock_s=0.08)
        heartbeats = [0]

        def hb() -> None:
            heartbeats[0] += 1

        ext = ExternalMicromanagerBackend(
            board=self.board,
            timeout_s=2.0,
            on_timeout="block",
            budget=budget,
            heartbeat_fn=hb,
            poll_interval_s=0.05,
        )

        # Wait ~0.18s (> wall_clock_s=0.08) before answering; clock stays paused
        def delayed_answer() -> None:
            gate = threading.Event()
            gate.wait(0.18)
            posts = self.board.read(as_name="sup", since=0, limit=None)
            q = next(p for p in posts if p["kind"] == "question")
            self.board.answer(q["seq"], as_name="sup", data={"verdict": "ready"})

        t = threading.Thread(target=delayed_answer, daemon=True)
        t.start()
        ans = ext.assist("shape_and_plan", {"card_id": "CARD-1"})
        t.join(timeout=2.0)

        self.assertEqual(ans["verdict"], "ready")
        self.assertGreaterEqual(heartbeats[0], 1)
        self.assertLess(budget.wall_clock_s, 0.08)
        budget.check_clock()  # must not raise BudgetExceeded

        # Kill switch interrupts wait immediately
        killed = ExternalMicromanagerBackend(
            board=self.board,
            timeout_s=5.0,
            on_timeout="fallback",
            cancel_check=lambda: True,
        )
        with self.assertRaises(GateFailure) as caught_kill:
            killed.assist("pre_review", {"card_id": "CARD-1"})
        self.assertEqual(caught_kill.exception.code, StopCode.KILLED)


if __name__ == "__main__":
    unittest.main()
