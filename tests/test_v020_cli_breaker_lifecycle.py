"""Regression tests for Wave 1 CLI, circuit breaker, budget, land, and coordinator fixes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfarm_core import cli as cli_mod
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.board import Board
from openfarm_core.budget import BudgetExceeded
from openfarm_core.cards import Card, TestRef
from openfarm_core.config import load_config
from openfarm_core.coordinator import SwarmRunner
from openfarm_core.git import Git
from openfarm_core.state import StateDir, write_json


def _make_python_repo(tmp: Path, *, circuit_breaker: int = 2, spend_per_night: float = 20.0,
                      swarm_enabled: bool = False) -> tuple[Path, Path]:
    repo = tmp / "repo"
    state = tmp / "state"
    repo.mkdir()
    (repo / ".openfarm").mkdir()
    (repo / "pkg").mkdir()
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "import unittest\nfrom pkg.core import add\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n",
        encoding="utf-8",
    )
    (repo / ".openfarm" / "config.toml").write_text(
        f'[project]\nname = "demo"\nlane = "farm"\nbase_branch = "main"\nstate_dir = "{state}"\n\n'
        '[adapters]\ncards = "files:cards/"\nrunner = "direct"\nlanguages = ["python"]\n\n'
        '[model]\nendpoint = "https://x.invalid/v1"\nallowed_endpoints = ["https://x.invalid/v1"]\n'
        'implementer = "test-coder"\n\n'
        '[policy]\ncategories = ["pinned-bugfix", "small-feature"]\n\n'
        f'[budget]\nspend_per_night = {spend_per_night}\nconcurrent_cards = 2\n\n'
        f'[gates]\ncircuit_breaker = {circuit_breaker}\nmutation = "off"\n\n'
        f'[swarm]\nenabled = {"true" if swarm_enabled else "false"}\n',
        encoding="utf-8",
    )
    git = Git(repo)
    git.init(branch="main")
    git.commit_all("initial commit")
    return repo, state


def _add_card(source: FileCardSource, cid: str, *, state: str = "ready", deps: list[str] | None = None) -> None:
    source.add_card(
        Card(
            id=cid,
            title=f"Card {cid}",
            state=state,
            done_text=f"Implement {cid}.",
            touches=[f"pkg/{cid.lower()}.py"],
            deps=deps or [],
            fields={
                "category": "small-feature",
                "tests": [TestRef(name=f"test_{cid.lower()}", file="tests/test_core.py", asserts="ok")],
            },
        )
    )


def _run_args(**overrides) -> SimpleNamespace:
    base = dict(
        cards=5,
        card=None,
        dry_run=False,
        spend=None,
        for_duration=None,
        until=None,
        run_id="run-test",
        script=None,
        mm_script=None,
        profile=None,
        learn_from=None,
        follow=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class CircuitBreakerActiveRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_circuit_breaker_stops_sequential_run_cards_3_to_5(self) -> None:
        repo, state_dir = _make_python_repo(self.tmp, circuit_breaker=2, swarm_enabled=False)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        source = ctx.cards()
        source.prepare()
        for i in range(1, 6):
            _add_card(source, f"CARD-{i}")

        called: list[str] = []

        def fake_run_card(_ctx, _source, cid, *_a, **_kw):
            called.append(cid)
            return {
                "card": cid,
                "final_state": "blocked",
                "stop_code": "tests-unprovable",
                "detail": "test passed on base",
                "spend": 0.0,
                "micromanager_spend": 0.0,
            }

        buf = io.StringIO()
        with mock.patch.object(cli_mod, "_handshake", return_value={"refused": False, "adapters": {}}), \
                mock.patch.object(cli_mod, "_run_card", side_effect=fake_run_card), \
                contextlib.redirect_stdout(buf):
            rc = cli_mod._do_run(ctx, _run_args(cards=5), "run-breaker-seq")

        self.assertEqual(rc, cli_mod.EXIT_OK)
        self.assertEqual(called, ["CARD-1", "CARD-2"])
        breaker = StateDir(state_dir).read_breaker()
        self.assertTrue(breaker["tripped"], breaker)
        self.assertEqual(breaker["cards"], ["CARD-1", "CARD-2"])

        payload = json.loads(buf.getvalue())
        skipped = [o for o in payload["outcomes"] if o.get("skipped")]
        self.assertEqual([o["card"] for o in skipped], ["CARD-3", "CARD-4", "CARD-5"])
        self.assertTrue(all("circuit breaker" in o["skipped"] for o in skipped))

    def test_update_breaker_never_silently_clears_tripped_breaker(self) -> None:
        repo, state_dir = _make_python_repo(self.tmp, circuit_breaker=2)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        tripped = cli_mod._update_breaker(ctx, [("CARD-1", "budget", "x"), ("CARD-2", "budget", "y")])
        self.assertTrue(tripped)
        self.assertTrue(StateDir(state_dir).read_breaker()["tripped"])

        # A subsequent passing card resets `consecutive` to [] and calls _update_breaker;
        # the already-tripped breaker must remain tripped.
        still = cli_mod._update_breaker(ctx, [])
        self.assertTrue(still)
        breaker = StateDir(state_dir).read_breaker()
        self.assertTrue(breaker["tripped"])
        self.assertEqual(breaker["cards"], ["CARD-1", "CARD-2"])

    def test_circuit_breaker_stops_swarm_runner_and_keeps_tripped_on_inflight_pass(self) -> None:
        repo, state_dir = _make_python_repo(self.tmp, circuit_breaker=2, swarm_enabled=True)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        source = ctx.cards()
        source.prepare()
        for i in range(1, 6):
            _add_card(source, f"CARD-{i}")

        state = StateDir(state_dir)
        run_dir = state.create_run("run-swarm-breaker")
        board = Board(run_dir / "board")
        night = cli_mod._LiveNightBudget(20.0, cards_cap=5)
        consecutive: list[tuple[str, str, str]] = []
        rec_lock = threading.Lock()
        launched: list[str] = []
        card3_started = threading.Event()
        card2_release = threading.Event()

        def fake_run_card(cid: str, **_kw) -> dict:
            launched.append(cid)
            if cid == "CARD-1":
                return {"card": cid, "final_state": "blocked", "stop_code": "attempts", "detail": "failed"}
            if cid == "CARD-2":
                # Wait until CARD-3 is already in flight, then block to trip the breaker
                card3_started.wait(timeout=3.0)
                return {"card": cid, "final_state": "blocked", "stop_code": "attempts", "detail": "failed"}
            if cid == "CARD-3":
                card3_started.set()
                card2_release.wait(timeout=3.0)
                return {"card": cid, "final_state": "in-review", "stop_code": "", "detail": "ok"}
            return {"card": cid, "final_state": "in-review", "stop_code": "", "detail": "ok"}

        def record_outcome(cid: str, outcome: dict) -> bool:
            nonlocal consecutive
            with rec_lock:
                if outcome.get("final_state") == "blocked":
                    consecutive.append((cid, outcome.get("stop_code", ""), outcome.get("detail", "")))
                else:
                    consecutive = []
                tripped = cli_mod._update_breaker(ctx, consecutive)
                if cid == "CARD-2":
                    card2_release.set()
                return tripped

        runner = SwarmRunner(
            cfg=cfg,
            state=state,
            source=source,
            board=board,
            identifiers=["CARD-1", "CARD-2", "CARD-3", "CARD-4", "CARD-5"],
            night=night,
            deadline=None,
            run_card_fn=fake_run_card,
            record_outcome_fn=record_outcome,
            backend=None,
        )
        outcomes = runner.run()
        # CARD-4 and CARD-5 must never launch after breaker trips, and already-in-flight CARD-3 passing
        # must NOT silently clear the tripped breaker.
        self.assertEqual(set(launched), {"CARD-1", "CARD-2", "CARD-3"})
        self.assertTrue(state.read_breaker()["tripped"])
        skipped = [o["card"] for o in outcomes if o.get("skipped")]
        self.assertEqual(skipped, ["CARD-4", "CARD-5"])

    def test_follow_loop_stops_when_breaker_trips(self) -> None:
        repo, state_dir = _make_python_repo(self.tmp, circuit_breaker=2, swarm_enabled=False)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        source = ctx.cards()
        source.prepare()
        _add_card(source, "CARD-1")
        _add_card(source, "CARD-2")
        _add_card(source, "CARD-3", deps=["CARD-1"])

        called: list[str] = []

        def fake_run_card(_ctx, _source, cid, *_a, **_kw):
            called.append(cid)
            return {"card": cid, "final_state": "blocked", "stop_code": "attempts", "detail": "boom"}

        with mock.patch.dict(os.environ, {"OPENFARM_FOLLOW_POLL_S": "0.01"}), \
                mock.patch.object(cli_mod, "_handshake", return_value={"refused": False, "adapters": {}}), \
                mock.patch.object(cli_mod, "_run_card", side_effect=fake_run_card), \
                contextlib.redirect_stdout(io.StringIO()):
            cli_mod._do_run(ctx, _run_args(cards=5, follow=True), "run-follow-breaker")

        self.assertEqual(called, ["CARD-1", "CARD-2"])
        self.assertTrue(StateDir(state_dir).read_breaker()["tripped"])


class CliLifecycleAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_land_accept_blocked_does_not_mutate_on_suite_failure(self) -> None:
        repo, state_dir = _make_python_repo(self.tmp)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        source = ctx.cards()
        source.prepare()
        _add_card(source, "CARD-1", state="blocked")

        card = next(c for c in source.all_cards() if c.id == "CARD-1")
        branch = f"openfarm/{card.id}-{card.slug()}"
        git = Git(repo)
        git.create_branch(branch, "HEAD")
        wt = state_dir / "tmp-bad-branch"
        git.worktree_add(wt, branch)
        try:
            # Break the test suite on the card branch so suite_green fails on trial merge.
            (wt / "pkg" / "core.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8")
            Git(wt).commit_all("broken impl")
        finally:
            git.worktree_remove(wt)

        cdir = StateDir(state_dir).create_card("CARD-1", "run-1")
        write_json(cdir / "card-record.json", {"card": "CARD-1", "final_state": "blocked"})
        verdict_path = cdir / "verdict.json"

        land_args = SimpleNamespace(
            cards=["CARD-1"], all=False, dry_run=False, accept_blocked=True, note="checked by hand"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_mod.cmd_land(ctx, land_args)

        self.assertEqual(rc, cli_mod.EXIT_REFUSED)
        # Card must remain blocked and verdict.json must NOT have been written.
        card_after = next(c for c in source.all_cards() if c.id == "CARD-1")
        self.assertEqual(card_after.state, "blocked")
        self.assertFalse(verdict_path.exists())

    def test_run_card_cleans_up_lease_worktree_and_backend_on_setup_error(self) -> None:
        repo, state_dir = _make_python_repo(self.tmp)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        source = ctx.cards()
        source.prepare()
        _add_card(source, "CARD-1")

        fake_backend = mock.Mock()
        run_dir = StateDir(state_dir).create_run("run-setup-fail")
        worktree_expected = StateDir(state_dir).worktrees / "CARD-1-run-setup-fail"

        with mock.patch.object(cli_mod, "open_backend", return_value=fake_backend), \
                mock.patch.object(cli_mod, "open_micromanager", side_effect=RuntimeError("boom in mm")):
            with self.assertRaisesRegex(RuntimeError, "boom in mm"):
                cli_mod._run_card(
                    ctx, source, "CARD-1", "run-setup-fail",
                    telemetry=mock.Mock(), run_dir=run_dir, script=None,
                )

        # Lease released back to ready, worktree removed, opened backend closed.
        card_after = next(c for c in source.all_cards() if c.id == "CARD-1")
        self.assertEqual(card_after.state, "ready")
        self.assertFalse(worktree_expected.exists())
        fake_backend.close.assert_called_once()

    def test_spend_flag_propagates_to_card_budget_and_enforces_parallel_night_cap(self) -> None:
        repo, _state_dir = _make_python_repo(self.tmp, spend_per_night=20.0)
        cfg = load_config(repo)
        night = cli_mod._LiveNightBudget(2.0, cards_cap=4)
        card_a = Card(id="CARD-1", title="A")
        card_b = Card(id="CARD-2", title="B")

        b1 = cli_mod._build_card_budget(cfg, card_a, night=night)
        b2 = cli_mod._build_card_budget(cfg, card_b, night=night)
        self.assertEqual(b1.spend_cap, 2.0)
        self.assertEqual(b2.spend_cap, 2.0)

        # Two parallel cards spend $1.10 each: combined $2.20 > $2.00 must raise BudgetExceeded immediately
        b1.note_tokens(100, 100, spend=1.10)
        self.assertIsNone(night.exhausted())
        with self.assertRaises(BudgetExceeded):
            b2.note_tokens(100, 100, spend=1.10)
        self.assertIsNotNone(night.exhausted())

    def test_follow_picks_up_retried_card_while_waiting_on_in_review(self) -> None:
        repo, _state_dir = _make_python_repo(self.tmp, circuit_breaker=5, swarm_enabled=False)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)
        source = ctx.cards()
        source.prepare()
        _add_card(source, "CARD-1")
        _add_card(source, "CARD-2")
        _add_card(source, "CARD-3", deps=["CARD-1"])

        runs_seen: list[str] = []

        def fake_run_card(_ctx, src, cid, *_a, **_kw):
            runs_seen.append(cid)
            claim = src.claim(cid, "test-holder", 60.0)
            if cid == "CARD-1":
                src.move(cid, claim["lease_id"], "in-review")
                return {"card": cid, "final_state": "in-review", "stop_code": "", "detail": "ok"}
            if cid == "CARD-2" and runs_seen.count("CARD-2") == 1:
                src.move(cid, claim["lease_id"], "blocked")
                return {"card": cid, "final_state": "blocked", "stop_code": "attempts", "detail": "first try failed"}
            src.move(cid, claim["lease_id"], "in-review")
            # Stop the follow loop cleanly once CARD-2 ran a second time.
            _ctx.state.pause("done testing")
            return {"card": cid, "final_state": "in-review", "stop_code": "", "detail": "passed on retry"}

        # Retry CARD-2 on the first follow poll sleep while waiting for CARD-1 to be accepted.
        retried = {"done": False}

        def fake_sleep(_s: float) -> None:
            if not retried["done"]:
                retried["done"] = True
                source.release("CARD-2", reason="retried by user")

        with mock.patch.object(cli_mod, "_handshake", return_value={"refused": False, "adapters": {}}), \
                mock.patch.object(cli_mod, "_run_card", side_effect=fake_run_card), \
                mock.patch.object(cli_mod._time, "sleep", side_effect=fake_sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            cli_mod._do_run(ctx, _run_args(cards=5, follow=True), "run-follow-retry")

        self.assertEqual(runs_seen, ["CARD-1", "CARD-2", "CARD-2"])

    def test_card_migrate_take_matches_case_insensitively(self) -> None:
        repo, _state_dir = _make_python_repo(self.tmp)
        cfg = load_config(repo)
        ctx = cli_mod.Context(repo, None, json_output=True)

        fake_store = mock.Mock()
        fake_store.migration.return_value = {
            "needed": True,
            "cards": 1,
            "differences": [{"id": "CARD-1", "main": "a", "state": "b"}],
        }
        fake_store.migrate.return_value = {"migrated": True}
        args = SimpleNamespace(card_command="migrate", take=["card-1=main"])
        buf = io.StringIO()
        with mock.patch("openfarm_core.store.open_store", return_value=fake_store), \
                contextlib.redirect_stdout(buf):
            rc = cli_mod.cmd_card(ctx, args)

        self.assertEqual(rc, cli_mod.EXIT_OK)
        fake_store.migrate.assert_called_once_with({"CARD-1": "main"})


if __name__ == "__main__":
    unittest.main()
