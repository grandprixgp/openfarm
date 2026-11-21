"""Regression tests for Wave 1 Journey state-machine, TUI, streaming, and terminal fixes."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core import automode
from openfarm_core.bootstrap import initialize_project
from openfarm_core.cards import Card
from openfarm_core.config import load_config
from openfarm_core.state import StateDir, write_json
from openfarm_core.store import open_store, write_doc
from openfarm_tui import textual_available
from openfarm_tui.journey_state import _task, gather, looking_at, reached, screen_for
from openfarm_tui.terminal import ask_background

from tests.test_journey import GOALS, build_project


class JourneyStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        patcher = mock.patch.dict(
            os.environ,
            {
                "OPENFARM_PROJECTS_FILE": str(self.base / "projects.json"),
                "OPENFARM_MODELS_FILE": str(self.base / "models.toml"),
                "OPENFARM_PROVIDERS_FILE": str(self.base / "providers.toml"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_looking_at_defaults_to_last_goal_when_all_goals_accepted(self) -> None:
        goals = [
            {"id": "C1", "title": "First", "goal": "g1", "status": "done"},
            {"id": "C2", "title": "Second", "goal": "g2", "status": "done"},
        ]
        all_tasks = [
            {"id": "W1-01", "state": "done", "goal_no": 1},
            {"id": "W2-01", "state": "done", "goal_no": 2},
        ]
        facts = {
            "repo": self.base,
            "cfg": None,
            "spec": True,
            "goals": goals,
            "goal": None,
            "goal_no": 0,
            "tasks": [],
            "all_tasks": all_tasks,
            "tried": None,
            "tried_any": None,
        }
        view = looking_at(facts, 0)
        self.assertEqual(view["goal"], goals[-1])
        self.assertEqual(view["goal_no"], 2)
        self.assertEqual(view["tasks"], [all_tasks[1]])

    def test_split_cards_filtered_by_screen_for_and_reached(self) -> None:
        base = {"spec": True, "goal": GOALS[0], "goal_no": 1, "goals": GOALS, "try_needed": False}
        self.assertEqual(screen_for({**base, "tasks": [{"state": "split"}]}), {"screen": "spec", "flag": False})
        self.assertEqual(
            screen_for({**base, "tasks": [{"state": "done"}, {"state": "split"}]}),
            {"screen": "accept", "flag": False},
        )
        self.assertNotEqual(reached({**base, "tasks": [{"state": "split"}]}, 1)["plan"], "")

    def test_retrying_given_back_task_unblocks_via_released_at(self) -> None:
        state_dir = self.base / "state"
        cdir = StateDir(state_dir).create_card("CARD-1", "run-1")
        write_json(
            cdir / "card-record.json",
            {
                "card": "CARD-1",
                "final_state": "released",
                "stop_code": "ineligible",
                "ended_at": "2026-11-21T10:00:00Z",
            },
        )
        card = Card(id="CARD-1", title="T", state="ready")
        # Same timestamp as ended_at -> still shown as blocked (unretried)
        still_blocked = _task(card, {"CARD-1": card}, state_dir, {"released_at": "2026-11-21T10:00:00Z"})
        self.assertEqual(still_blocked["state"], "blocked")
        # Retried later (newer released_at, even without edited_at) -> unblocked back to ready
        retried = _task(card, {"CARD-1": card}, state_dir, {"released_at": "2026-11-21T10:05:00Z"})
        self.assertEqual(retried["state"], "ready")

    def test_gather_guards_corrupt_store_and_non_int_wave(self) -> None:
        repo = build_project(self.base, "plan")
        cfg = load_config(repo)
        store = open_store(repo, cfg)
        first = store.cards()[0]
        # Set a non-integer wave field on the card
        store.change_card(first.id, "edit", why="bad wave", apply=lambda h, b: ({**h, "wave": "two"}, b))
        facts = gather(repo)
        self.assertTrue(any(t["id"] == first.id and t["goal_no"] == 1 for t in facts["all_tasks"]))

        # Store failure during gather() must degrade cleanly instead of crashing
        with mock.patch("openfarm_core.store.store_or_none", side_effect=RuntimeError("corrupt git")):
            facts2 = gather(repo)
        self.assertEqual(facts2["all_tasks"], [])
        self.assertEqual(facts2["tasks"], [])

    def test_something_wrong_reopens_done_goal_in_chunks_json(self) -> None:
        repo = self.base / "app"
        initialize_project(
            "app", directory=repo, idea="a tool", language="python",
            state_dir=self.base / "state", secrets_dir=self.base / "secrets", commit=True,
        )
        cfg = load_config(repo)
        write_doc(repo, cfg, "spec", "# Spec\n", why="spec")
        automode.save_project_chunks(repo, [
            {"id": "C1", "title": "Goal 1", "goal": "do thing", "status": "done"},
        ])
        with mock.patch.object(automode, "draft_wave_plan", return_value={"new_cards": ["W1-02"], "cards": []}):
            res = automode.something_wrong(repo, "button is broken", chunk_id="C1", backend=mock.Mock())
        self.assertEqual(res["new_cards"], ["W1-02"])
        chunks = automode.load_project_chunks(repo)
        self.assertEqual(chunks[0]["status"], "active")

    def test_stream_file_progress_preserves_and_restores_existing_on_chunk(self) -> None:
        class _FakeBackend:
            def __init__(self) -> None:
                self.on_chunk = None

            def stream_to(self, cb):
                prev = self.on_chunk
                self.on_chunk = cb
                return prev

        heard: list[str] = []
        progress: list[str] = []
        backend = _FakeBackend()
        cb = heard.append
        backend.stream_to(cb)
        prev = automode._stream_file_progress(backend, progress.append)
        self.assertIs(prev, cb)

        chunk = '{"files": {"cards/W1-01.md": "hello"}}'
        backend.on_chunk(chunk)
        self.assertEqual(heard, [chunk])
        self.assertTrue(any("W1-01" in msg for msg in progress), progress)

        backend.stream_to(prev)
        self.assertIs(backend.on_chunk, cb)

    def test_terminal_ask_background_breaks_on_eof(self) -> None:
        r_fd, w_fd = os.pipe()
        os.close(w_fd)  # immediate EOF on r_fd
        try:
            with mock.patch("termios.tcgetattr", return_value=[]), \
                    mock.patch("termios.tcsetattr"), \
                    mock.patch("tty.setcbreak"), \
                    mock.patch("os.write", return_value=1):
                t0 = time.monotonic()
                res = ask_background(fd=r_fd, wait_s=2.0)
                elapsed = time.monotonic() - t0
            self.assertIsNone(res)
            self.assertLess(elapsed, 0.5, "ask_background busy-spun on EOF instead of breaking")
        finally:
            os.close(r_fd)


@unittest.skipUnless(textual_available(), "Textual not installed")
class JourneyAppHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        patcher = mock.patch.dict(
            os.environ,
            {
                "OPENFARM_PROJECTS_FILE": str(self.base / "projects.json"),
                "OPENFARM_MODELS_FILE": str(self.base / "models.toml"),
                "OPENFARM_PROVIDERS_FILE": str(self.base / "providers.toml"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_walk_back_to_plan_after_all_goals_accepted_and_draw_plan_none_goal(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "accept")
        automode.save_project_chunks(repo, [{**g, "status": "done"} for g in GOALS[:1]])

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                self.assertEqual(app.screen_name, "accept")
                self.assertIsNone(app.real["goal"])
                # Walk back to Plan and Try it without TypeError
                app.look("plan")
                await pilot.pause()
                self.assertEqual(app.screen_name, "plan")
                app.look("try")
                await pilot.pause()
                self.assertEqual(app.screen_name, "try")
                # Even if _draw_plan is called with goal=None directly, it does not crash
                app.facts = {**app.real, "goal": None, "tasks": []}
                app._draw_plan()

        asyncio.run(scenario())

    def test_split_cards_hidden_on_plan_build_accept_and_board(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "plan")
        cfg = load_config(repo)
        store = open_store(repo, cfg)
        cards = store.cards()
        split_id = cards[0].id
        store.change_card(split_id, "edit", why="split", apply=lambda h, b: ({**h, "state": "split"}, b))

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                rows = app._board_rows()
                all_board_ids = [oid for col in rows.values() for oid, _ in col if oid]
                self.assertNotIn(split_id, all_board_ids)
                app.action_board()
                await pilot.pause()
                self.assertEqual(app.screen_name, "board")

        asyncio.run(scenario())

    def test_full_diff_from_blocked_preserves_fix_and_esc_returns_to_blocked(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "blocked")

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                app.do_open_blocked()
                await pilot.pause()
                self.assertEqual(app.screen_name, "blocked")
                app.fix = {"notes": ["fixed touch"], "diff": "--- a/card\n+++ b/card\n+new line\n"}
                app.do_full_diff()
                await pilot.pause()
                self.assertEqual(app.screen_name, "details")
                self.assertIsNotNone(app.fix)
                details_rendered = str(app.query_one("#details-text").render())
                self.assertIn("Suggested fix, in full", details_rendered)
                app.action_back()
                await pilot.pause()
                self.assertEqual(app.screen_name, "blocked")

        asyncio.run(scenario())

    def test_step_cancel_and_restart_race_cannot_clobber_newer_step(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "plan")
        step1_started = threading.Event()
        step1_release = threading.Event()
        step2_started = threading.Event()
        step2_release = threading.Event()
        b1 = mock.Mock(_unwritten=[])
        b2 = mock.Mock(_unwritten=[])
        backends = iter([(b1, "Claude"), (b2, "Claude")])

        def fn1(_backend, _progress):
            step1_started.set()
            step1_release.wait(timeout=5.0)
            return {"cards": [{"id": "STALE"}]}

        def fn2(_backend, _progress):
            step2_started.set()
            step2_release.wait(timeout=5.0)
            return {"cards": [{"id": "FRESH"}]}

        async def wait_event(pilot, evt: threading.Event, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while not evt.is_set() and time.monotonic() < deadline:
                await pilot.pause(0.02)
            return evt.is_set()

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            with mock.patch("openfarm_tui.journey.open_architect_backend", side_effect=lambda *_a, **_k: next(backends)):
                async with app.run_test(size=(110, 32)) as pilot:
                    await pilot.pause()
                    app._step("plan", "Step 1", fn1)
                    self.assertTrue(await wait_event(pilot, step1_started))
                    # Cancel step 1 while fn1 is still blocked, then immediately start step 2
                    app.do_cancel()
                    app._step("plan", "Step 2", fn2)
                    self.assertTrue(await wait_event(pilot, step2_started))
                    self.assertIs(app._backend, b2)

                    # Now let stale step 1 unwind and finish; it must NOT clear b2 or busy!
                    step1_release.set()
                    await pilot.pause(0.15)
                    self.assertEqual(app.busy, "Step 2")
                    self.assertIs(app._backend, b2)

                    # Now let step 2 finish normally
                    step2_release.set()
                    await pilot.pause(0.2)
                    self.assertEqual(app.busy, "")
                    self.assertIsNone(app._backend)
                    b1.close.assert_called()
                    b2.close.assert_called()

        asyncio.run(scenario())

    def test_sort_questions_closes_acp_backend(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "build")
        fake_backend = mock.Mock()
        done_evt = threading.Event()

        def fake_settle(*_a, **_kw):
            done_evt.set()
            return {"for": "nobody"}

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            with mock.patch("openfarm_tui.journey.open_architect_backend", return_value=(fake_backend, "Claude")), \
                    mock.patch("openfarm_tui.journey.settle_question", side_effect=fake_settle):
                async with app.run_test(size=(110, 32)) as pilot:
                    await pilot.pause()
                    app._sort_questions([{"id": "FIND-1", "from_card": "W1-01"}])
                    deadline = time.monotonic() + 3.0
                    while not done_evt.is_set() and time.monotonic() < deadline:
                        await pilot.pause(0.02)
                    self.assertTrue(done_evt.is_set())
                    await pilot.pause(0.1)
                    fake_backend.close.assert_called_once()

        asyncio.run(scenario())

    def test_do_try_again_and_do_run_check_set_busy_before_spawning_threads(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "blocked")

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                app.do_open_blocked()
                await pilot.pause()
                with mock.patch.object(app, "_run_check_bg") as run_bg:
                    app.do_run_check()
                    app.do_run_check()  # second rapid call must be ignored because busy is already set
                    self.assertTrue(app.busy)
                    run_bg.assert_called_once()
                app._set_busy("")
                with mock.patch.object(app, "_try_again_bg") as try_bg:
                    app.do_try_again()
                    app.do_try_again()  # second rapid call must be ignored
                    self.assertTrue(app.busy)
                    try_bg.assert_called_once()

        asyncio.run(scenario())

    def test_multi_card_live_token_bar_sums_inflight_and_disk_tokens(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "build")

        async def scenario() -> None:
            app = Journey(repo, theme="openfarm-dark")
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                # Pretend prior runs already wrote 10k builder tokens on disk
                app.real["tokens"] = {"builders": 10_000, "reviewers": 2_000, "architect": 0, "total": 12_000}
                ts = "2026-11-21T10:00:00Z"
                app._run_event({"kind": "run_start", "ts": ts, "message": "Starting run r (2 card(s))"}, quiet=True)
                # Two cards in flight concurrently: 4k and 6k builder tokens
                app._run_event({"kind": "phase", "card_id": "W1-01", "phase": "code", "ts": ts,
                                "worker_tokens": 4_000, "supervisor_tokens": 500}, quiet=True)
                app._run_event({"kind": "phase", "card_id": "W1-02", "phase": "code", "ts": ts,
                                "worker_tokens": 6_000, "supervisor_tokens": 500}, quiet=True)
                app._draw_health()
                # Total should be 10k disk + 10k live builders + 2k disk + 1k live reviewers = 23k
                self.assertIn("23k tokens", str(app.query_one("#health").render()))

                # Card 1 finishes and writes its 4k/500 to disk
                app.real["tokens"] = {"builders": 14_000, "reviewers": 2_500, "architect": 0, "total": 16_500}
                app._run_event({"kind": "card_done", "card_id": "W1-01", "final_state": "in-review", "ts": ts,
                                "worker_tokens": 4_000, "supervisor_tokens": 500}, quiet=True)
                app._draw_health()
                # Still 23k (no double-counting W1-01, W1-02's 6k/500 still counted live!)
                self.assertIn("23k tokens", str(app.query_one("#health").render()))

        asyncio.run(scenario())

    def test_do_signin_passes_explicit_config_and_action_column_guards_gone(self) -> None:
        from openfarm_tui.journey import Journey

        repo = build_project(self.base, "plan")
        custom_cfg = repo / ".openfarm" / "config.toml"

        async def scenario() -> None:
            app = Journey(repo, config_path=custom_cfg, theme="openfarm-dark")
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause()
                app.open_more("providers")
                await pilot.pause()
                with mock.patch("openfarm_tui.journey_more.subprocess.run") as sub_run, \
                        mock.patch.object(app, "suspend", return_value=mock.MagicMock()):
                    app.do_signin()
                acp_calls = [c.args[0] for c in sub_run.call_args_list if c.args and "acp" in c.args[0]]
                self.assertEqual(len(acp_calls), 1, sub_run.call_args_list)
                argv = acp_calls[0]
                self.assertIn("--config", argv)
                self.assertEqual(argv[argv.index("--config") + 1], str(custom_cfg))

                # action_column's call_after_refresh callback must not crash if _gone() is True
                app.action_back()
                app.action_board()
                await pilot.pause()
                captured = []
                with mock.patch.object(app, "call_after_refresh", side_effect=captured.append):
                    app.action_column(1)
                self.assertEqual(len(captured), 1)
                with mock.patch.object(app, "_gone", return_value=True), \
                        mock.patch.object(app, "_show_actions") as show_act:
                    captured[0]()
                    show_act.assert_not_called()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
