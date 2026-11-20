"""R2.6 and R2.7: builds belong to the background service, and one build covers dependent tasks."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from openfarm_core.config import load_config
from openfarm_core.daemon import daemon_status, start_daemon, stop_daemon
from openfarm_tui import textual_available
from tests.test_tui_phase1 import _log_text, _RegistryIsolation, _two_card_toy

# A stand-in for `openfarm run`: says it started, waits for a flag file, then finishes one task.
FAKE_RUN = '''
import json, pathlib, sys, time
events, flag = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
events.parent.mkdir(parents=True, exist_ok=True)
def say(**event):
    with events.open("a") as fh:
        fh.write(json.dumps(event) + "\\n")
say(kind="run_start", ts="2026-11-20T10:00:00Z", message="Starting run (1 card(s))")
say(kind="phase", card_id="CARD-1", phase="tests", ts="2026-11-20T10:00:01Z", message="writing the tests")
while not flag.exists():
    time.sleep(0.05)
say(kind="card_done", card_id="CARD-1", final_state="in-review", ts="2026-11-20T10:00:09Z", message="in-review")
'''


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class BuildSurvivesTheAppTests(_RegistryIsolation):
    def test_killing_the_app_leaves_the_build_running_and_reopening_shows_it(self) -> None:
        from openfarm_core.state import StateDir
        from openfarm_tui.app import OpenfarmApp

        toy, _src = _two_card_toy(self)
        cfg = load_config(toy.root)
        flag = self.tmp / "finish"
        self.addCleanup(lambda: (flag.touch(), stop_daemon(toy.root)))

        def fake_start(repo, *, run_id, **kwargs):
            events = StateDir(cfg.state_dir).run_dir(run_id) / "events.jsonl"
            return start_daemon(repo, run_id=run_id, command_override=[sys.executable, "-c", FAKE_RUN, str(events), str(flag)])

        async def until(app, pilot, wanted: str) -> None:
            for _ in range(100):
                if wanted in _log_text(app):
                    return
                await pilot.pause(0.1)
            self.fail(f"{wanted!r} never showed in: {_log_text(app)}")

        async def first_window() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="studio")
            async with app.run_test(size=(140, 44)) as pilot:
                await pilot.pause(0.15)
                with mock.patch("openfarm_tui.app.start_daemon", side_effect=fake_start):
                    app.start_nonblocking_wave()
                    await until(app, pilot, "CARD-1 → tests")
                self.assertTrue(app._running_wave)
            # leaving the `with` closes the app with the build unfinished, as a crash would

        async def second_window() -> None:
            app = OpenfarmApp(repo=toy.root, initial_view="auto")
            async with app.run_test(size=(140, 44)) as pilot:
                await until(app, pilot, "CARD-1 → tests")  # the progress so far is shown again
                self.assertTrue(app._running_wave)
                self.assertIn("A build is already running", _log_text(app))
                self.assertIn("RUNNING", str(app.query_one("#status-badge").render()))
                self.assertEqual(app.query_one("#studio-switcher").current, "studio-run")
                flag.touch()
                await until(app, pilot, "Build finished: 1 task(s) (CARD-1: in-review)")
                await app.workers.wait_for_complete()
                self.assertFalse(app._running_wave)

        asyncio.run(first_window())
        status = daemon_status(toy.root)
        self.assertTrue(status["running"], "the build must outlive the window that started it")
        self.assertTrue(status["run_id"].startswith("run-"))
        asyncio.run(second_window())
        self.assertFalse(daemon_status(toy.root)["running"])


class FollowTests(_RegistryIsolation):
    """R2.7: with `--follow`, a task starts when the one it waits for is accepted, in the same run."""

    def test_a_dependent_task_starts_once_its_dependency_is_accepted(self) -> None:
        import contextlib
        import io
        import threading

        from openfarm_core.cli import main as cli_main

        toy, src = _two_card_toy(self)
        ran: list[str] = []
        waiting = threading.Event()

        def fake_run_card(ctx, source, card_id, *args, on_event=None, **kwargs):
            ran.append(card_id)
            claim = source.claim(card_id, holder="test-lane")
            source.move(card_id, claim["lease_id"], "in-review")
            return {"card_id": card_id, "final_state": "in-review"}

        def accept_when_asked() -> None:
            waiting.wait(20)
            src.mark_done("CARD-1")

        real_emit = None

        def spy(self_bus, **event):
            if event.get("kind") == "run_waiting":
                self.assertIn("CARD-2 starts when CARD-1 is accepted", event["message"])
                waiting.set()
            return real_emit(self_bus, **event)

        from openfarm_core import cli

        bus_cls = cli.RunEventBus if hasattr(cli, "RunEventBus") else None
        threading.Thread(target=accept_when_asked, daemon=True).start()
        out = io.StringIO()
        patches = [mock.patch("openfarm_core.cli._run_card", side_effect=fake_run_card),
                   mock.patch.dict(os.environ, {"OPENFARM_FOLLOW_POLL_S": "0.05"})]
        if bus_cls is not None:
            real_emit = bus_cls.emit
            patches.append(mock.patch.object(bus_cls, "emit", spy))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            if bus_cls is None:
                threading.Timer(1.0, waiting.set).start()
            code = cli_main(["--repo", str(toy.root), "--json", "run", "--cards", "3", "--follow"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(ran, ["CARD-1", "CARD-2"])

    def test_without_follow_the_run_ends_after_the_first_layer(self) -> None:
        import contextlib
        import io

        from openfarm_core.cli import main as cli_main

        toy, _src = _two_card_toy(self)
        ran: list[str] = []

        def fake_run_card(ctx, source, card_id, *args, **kwargs):
            ran.append(card_id)
            claim = source.claim(card_id, holder="test-lane")
            source.move(card_id, claim["lease_id"], "in-review")
            return {"card_id": card_id, "final_state": "in-review"}

        with mock.patch("openfarm_core.cli._run_card", side_effect=fake_run_card), \
                contextlib.redirect_stdout(io.StringIO()):
            cli_main(["--repo", str(toy.root), "--json", "run", "--cards", "3"])
        self.assertEqual(ran, ["CARD-1"])


if __name__ == "__main__":
    unittest.main()
