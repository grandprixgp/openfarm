"""Phase 3: the one screen. Which pane shows, its main button, its focus, and that it fits."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfarm_tui import textual_available
from openfarm_tui.journey_state import save_session, screen_for

FIXTURES = Path(__file__).parent / "fixtures"
IDEA = "A website showing Wikimedia's image of the day, themed from the photo."
GOALS = [
    {"id": "C1", "title": "Living Image", "goal": "The page shows today's image full screen with its title, story and credit.",
     "status": "active"},
    {"id": "C2", "title": "Recursive Threads", "goal": "Each fact opens into more.", "status": "pending"},
    {"id": "C3", "title": "Realtime and polish", "goal": "The page follows the day.", "status": "pending"},
]
SIZES = [(100, 30), (120, 36)]


def t(state: str) -> dict:
    return {"state": state}


class ScreenForTests(unittest.TestCase):
    """R3.6: one pure function, a test for every case."""

    BUILT = {"spec": True, "goal": GOALS[0]}

    def _is(self, facts: dict, screen: str, flag: bool = False) -> None:
        self.assertEqual(screen_for(facts), {"screen": screen, "flag": flag}, facts)

    def test_no_project_is_the_idea(self) -> None:
        self._is({}, "idea")

    def test_open_questions_are_the_questions(self) -> None:
        self._is({"asking": True}, "questions")

    def test_a_spec_with_no_plan_for_the_goal_is_the_spec(self) -> None:
        self._is({**self.BUILT, "tasks": []}, "spec")
        self._is({**self.BUILT, "tasks": [t("dropped")]}, "spec")
        self._is({**self.BUILT, "tasks": [], "asking": True}, "spec")  # a written spec ends the questions

    def test_a_plan_nobody_has_started_is_the_plan(self) -> None:
        self._is({**self.BUILT, "tasks": [t("ready"), t("backlog")]}, "plan")

    def test_a_running_build_is_the_build(self) -> None:
        self._is({**self.BUILT, "tasks": [t("ready"), t("ready")], "running": True}, "build")
        self._is({**self.BUILT, "tasks": [t("in_progress"), t("ready")]}, "build")

    def test_tasks_still_to_build_after_some_were_accepted_is_the_build(self) -> None:
        self._is({**self.BUILT, "tasks": [t("done"), t("ready")]}, "build")

    def test_a_task_needing_the_user_flags_the_build(self) -> None:
        for needing in ("blocked", "in_review"):
            self._is({**self.BUILT, "tasks": [t(needing), t("ready")], "running": True}, "build", True)
        self._is({**self.BUILT, "tasks": [t("in_progress")], "questions": [{"id": "FIND-1"}]}, "build", True)
        self._is({**self.BUILT, "tasks": [t("done"), t("blocked")]}, "build", True)

    def test_every_task_accepted_is_try_it(self) -> None:
        done = {**self.BUILT, "tasks": [t("done"), t("done"), t("dropped")], "try_needed": True}
        self._is(done, "try")
        self._is({**done, "tried": {"ok": False}}, "try")
        self._is({**done, "tried": {"ok": None}}, "try")
        self._is({**done, "tried": {"ok": True}}, "try")  # the checks passed; a person has not looked yet

    def test_try_it_passed_is_accept(self) -> None:
        done = {**self.BUILT, "tasks": [t("done")], "try_needed": True}
        self._is({**done, "tried": {"ok": True, "looks_right": True}}, "accept")
        self._is({**done, "tried": {"ok": False, "looks_right": True}}, "accept")

    def test_try_it_is_only_offered_where_the_app_is_not_a_web_app(self) -> None:
        self._is({**self.BUILT, "tasks": [t("done")], "try_needed": False}, "accept")

    def test_every_goal_accepted_is_accept(self) -> None:
        self._is({"spec": True, "goal": None, "tasks": []}, "accept")

    def test_the_last_screen_looked_at_is_not_an_input(self) -> None:
        import inspect

        self.assertEqual(list(inspect.signature(screen_for).parameters), ["facts"])


def build_project(base: Path, stage: str) -> Path:
    """A project folder in the state a screen needs. Stages: idea, questions, spec, plan, build, blocked, try, accept."""
    from openfarm_core.adapters.files import parse_card_file
    from openfarm_core.automode import save_project_chunks
    from openfarm_core.bootstrap import initialize_project
    from openfarm_core.config import load_config
    from openfarm_core.store import Store
    from openfarm_core.tryit import code_version

    root = base / "iotd"
    root.mkdir(parents=True)
    if stage in ("idea", "questions"):
        if stage == "questions":
            save_session(root, {"asking": True, "stack": "typescript", "history": [
                {"question": "Where does the image come from?", "answer": "Wikimedia's featured picture of the day, no API key"}],
                "pending": {"question": "How deep should “expand” go?", "total": 5,
                            "why": "Each fact about the image can open into more detail. This decides how many goals the spec needs.",
                            "options": ["Two levels: the image, then each fact opens one layer",
                                        "Open-ended: every layer offers new threads to follow",
                                        "One level: facts expand once, nothing deeper"],
                            "recommended": "Two levels: the image, then each fact opens one layer"}})
        return root
    initialize_project("iotd", directory=root, idea=IDEA, language="typescript", state_dir=base / "state",
                       secrets_dir=base / "secrets", commit=True)
    for rel, text in {"src/server.ts": "import { createServer } from 'node:http';\ncreateServer().listen(8931);\n",
                      "src/shared/palette.ts": "export const real = 1;\n", "src/shared/theme.ts": "export const real = 1;\n",
                      "tests/slice.test.ts": "export const real = 1;\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "slice"], check=True)
    cfg = load_config(root)
    store = Store(root, cfg)
    store.write_doc("spec", "# Image of the day\n\nA page built around today's Wikimedia image.\n\n## How to run it\n\n"
                    "`node src/server.ts`, then open the page.\n")
    save_project_chunks(root, [dict(goal) for goal in GOALS])
    if stage == "spec":
        return root
    states = {"plan": ["ready"] * 4, "build": ["in_progress", "in_review", "ready", "ready"],
              "blocked": ["done", "done", "ready", "blocked"], "try": ["done"] * 4, "accept": ["done"] * 4}[stage]
    for path, state in zip(sorted((FIXTURES / "dogfood-w1").glob("W1-*.md")), states):
        header, body = parse_card_file(path.read_text())
        if state == "ready":
            store.change_card(path.stem, "create", by="architect", header=header, body=body)
        else:
            store.src._write(path.stem, dict(header, state=state), body, f"{path.stem}: {state}")
    if stage == "plan":  # what the plan step does: repair what it can, park what it can't
        from openfarm_core.automode import preflight_plan

        preflight_plan(root, cfg, store)
    if stage in ("build", "blocked"):
        name, card = ("w1-04-touches.json", "W1-04") if stage == "blocked" else ("w1-01-revert-check.json", "W1-02")
        record = json.loads((FIXTURES / "dogfood-blocked" / name).read_text())
        record.update(card_id=card, **({"final_state": "in-review", "stop_code": "", "detail": ""} if stage == "build" else {}))
        folder = Path(cfg.state_dir) / "cards" / card / "run-1"
        folder.mkdir(parents=True)
        (folder / "card-record.json").write_text(json.dumps(record))
    if stage in ("try", "accept"):
        out = Path(cfg.state_dir) / "tryit" / "C1"
        out.mkdir(parents=True)
        seen = stage == "accept"
        (out / "result.json").write_text(json.dumps({
            "kind": "web", "goal": "C1", "command": "node src/server.ts", "url": "http://127.0.0.1:8931/", "output": "",
            "words": ["title", "story", "credit"], "head": code_version(root), "ok": False, "looks_right": seen,
            "screenshots": [str(out / "desktop-1440x900.png"), str(out / "phone-390x844.png")],
            "checks": [{"name": "loads", "ok": True, "detail": "the page loads at /"},
                       {"name": "image", "ok": True, "detail": "an image is shown, full screen"},
                       {"name": "words", "ok": seen, "detail": "the title, story and credit are on the page but can't be "
                        "seen (it is covered by img#art)" if not seen else "the title, story and credit can be seen"}]}))
    return root


# screen -> (stage, overlay to open, main button, focused widget)
SCREENS = {
    "idea": ("idea", "", "▶ Continue", "idea-text"),
    "questions": ("questions", "", "▶ Answer", "q-options"),
    "spec": ("spec", "", "▶ Plan goal 1", "go"),
    "plan": ("plan", "", "▶ Build 3 tasks", "plan-tasks"),
    "build": ("build", "", "▶ Accept W1-02", "build-tasks"),
    "blocked": ("blocked", "blocked", "▶ Apply fix and retry", "go"),
    "try": ("try", "", "▶ Fix this", "go"),
    "accept": ("accept", "", "▶ Accept goal 1", "go"),
    "details": ("build", "details", "▶ Back", "go"),
    "board": ("blocked", "board", "▶ Open W1-04", "col-needs"),
}


class _Folder(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(os.environ.get("TMPDIR") or tempfile.gettempdir(), exist_ok=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patches = [mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(self.base / "projects.json"), "EDITOR": "",
                                                "VISUAL": ""}),
                   mock.patch("openfarm_tui.journey.try_it", side_effect=AssertionError("the app must not be started")),
                   mock.patch("openfarm_tui.journey.webbrowser.open")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def app(self, stage: str, theme: str = "openfarm-dark"):
        from openfarm_tui.journey import Journey

        return Journey(repo=build_project(self.base / stage, stage), theme=theme)


async def open_screen(app, pilot, screen: str) -> None:
    await pilot.pause(0.2)
    overlay = SCREENS[screen][1]
    if overlay == "board":
        app.action_board()
    elif overlay:
        task = "W1-04" if overlay == "blocked" else "W1-02"
        app._open(overlay, task)
    await pilot.pause(0.2)


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class ScreenTests(_Folder):
    """R3 check: per screen, the main button's label, the focused widget, and nothing overflowing at both sizes."""

    def _fits(self, app, size: tuple[int, int], screen: str) -> None:
        from textual.widgets import Button, Static

        width, height = size
        where = f"{screen} at {width}x{height}"
        for bar in ("#top", "#actions", "#keys", "#activity"):
            for widget in [app.query_one(bar), *app.query_one(bar).query("*")]:
                region = widget.region
                self.assertGreaterEqual(region.x, 0, f"{where}: {widget} starts off screen")
                self.assertLessEqual(region.right, width, f"{where}: {widget} runs off the right edge")
        buttons = [b for b in app.query("#actions Button") if b.display]
        self.assertLessEqual(sum(b.outer_size.width for b in buttons), app.query_one("#actions").content_size.width,
                             f"{where}: the buttons do not fit their bar")
        for button in buttons:
            self.assertLessEqual(len(str(button.label)) + 2, button.size.width, f"{where}: {button.label} is cut off")
        self.assertLessEqual(len(app.query_one("#keys", Static).render().plain if hasattr(
            app.query_one("#keys", Static).render(), "plain") else str(app.query_one("#keys", Static).render())), width - 2,
            f"{where}: the key line is cut off")
        top = sum(app.query_one(f"#{part}").outer_size.width for part in ("brand", "health"))
        self.assertLess(top, width - 12, f"{where}: the top bar has no room for where you are")
        pane = app.query_one(f"#{screen}")
        self.assertLessEqual(pane.virtual_size.width, pane.size.width, f"{where}: the pane needs sideways scrolling")
        for widget in pane.query("*"):
            if widget.display and widget.region.width:
                self.assertLessEqual(widget.region.right, pane.region.right, f"{where}: {widget} is wider than the pane")
        from textual.widgets import OptionList

        for rows in pane.query(OptionList):
            if rows.display and rows.id in ("plan-tasks", "build-tasks"):
                for index in range(rows.option_count):
                    line = rows.get_option_at_index(index).prompt
                    plain = __import__("textual.content", fromlist=["Content"]).Content.from_markup(str(line)).plain
                    self.assertLessEqual(len(plain), rows.content_size.width, f"{where}: a task row wraps: {plain!r}")
        shown = str(app.query_one("#where").render())
        self.assertLessEqual(len(shown) + 2, app.query_one("#where").size.width, f"{where}: the top bar's middle is cut off")
        self.assertEqual(pane.scroll_y, 0, f"{where}: the heading is scrolled out of view")
        # fixed zones: the same rows whatever the screen (nothing moves under the pointer)
        self.assertEqual((app.query_one("#top").region.height, app.query_one("Rail").region.width,
                          app.query_one("#actions").region.height, app.query_one("#activity").region.height,
                          app.query_one("#keys").region.height), (1, 26, 3, 4, 1), where)
        self.assertEqual(app.query_one("#keys").region.bottom, height, where)

    def test_each_screen_has_its_button_its_focus_and_fits(self) -> None:
        async def check(screen: str, size: tuple[int, int]) -> None:
            stage, _overlay, button, focus = SCREENS[screen]
            app = self.app(stage)
            async with app.run_test(size=size) as pilot:
                await open_screen(app, pilot, screen)
                where = f"{screen} at {size[0]}x{size[1]}"
                self.assertEqual(app.screen_name, screen, where)
                self.assertEqual(str(app.query_one("#go").label), button, where)
                self.assertEqual(getattr(app.focused, "id", None), focus, where)
                self.assertEqual(len(app.query("#actions Button.-primary")), 1, where)
                self.assertIs(list(app.query("#actions Button"))[-1], app.query_one("#go"), f"{where}: main button last")
                self.assertTrue(str(app.query_one("#keys").render()).endswith("Ctrl+K everything else"), where)
                self._fits(app, size, screen)

        for screen in SCREENS:
            for size in SIZES:
                with self.subTest(screen=screen, size=size):
                    shutil.rmtree(self.base / SCREENS[screen][0], ignore_errors=True)
                    asyncio.run(check(screen, size))


QUESTION = {"ready_to_plan": False, "question": "Where does the image come from?", "why": "It decides the first task.",
            "options": ["Wikimedia's featured picture", "NASA's picture of the day"],
            "recommended": "Wikimedia's featured picture", "total": 2, "summary": ""}
READY = {"ready_to_plan": True, "question": "", "options": [], "recommended": "", "summary": "enough"}


def fake_spec(repo, *, idea, grill_history=None, language="", backend=None, on_progress=None):
    from openfarm_core.automode import save_project_chunks
    from openfarm_core.config import load_config
    from openfarm_core.store import write_doc

    write_doc(repo, load_config(repo), "spec", f"# Spec\n\n{idea}\n", why="test")
    save_project_chunks(repo, [dict(goal) for goal in GOALS])
    return {"chunks": GOALS}


def fake_plan(repo, *, idea="", chunk_id=None, backend=None, on_progress=None, **_more):
    from openfarm_core.adapters.files import parse_card_file
    from openfarm_core.config import load_config
    from openfarm_core.store import Store

    store = Store(repo, load_config(repo))
    for path in sorted((FIXTURES / "dogfood-w1").glob("W1-0[12].md")):
        header, body = parse_card_file(path.read_text())
        store.change_card(path.stem, "create", by="architect", header=header, body=body)
    from openfarm_core.automode import preflight_plan

    return {"new_cards": ["W1-01", "W1-02"], "cards": preflight_plan(repo, load_config(repo), store)["cards"]}


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class FlowTests(_Folder):
    """What the buttons and keys do, screen by screen."""

    def setUp(self) -> None:
        super().setUp()
        self.backend = mock.Mock(name="architect")
        self.opened = mock.patch("openfarm_tui.journey.open_architect_backend", return_value=(self.backend, "Claude"))
        self.opened.start()
        self.addCleanup(self.opened.stop)

    def drive(self, stage: str, body, size=(120, 36)) -> None:
        async def run() -> None:
            app = self.app(stage)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.2)
                await body(app, pilot)

        asyncio.run(run())

    @staticmethod
    async def settle(app, pilot) -> None:
        await app.workers.wait_for_complete()
        await pilot.pause(0.15)

    @staticmethod
    def label(app) -> str:
        return str(app.query_one("#go").label)

    @staticmethod
    def others(app) -> list[str]:
        return [str(b.label) for b in app.query("#actions Button") if b.display and b.id != "go"]

    @staticmethod
    async def say(app, pilot, text: str) -> None:
        """Type into the question box that is open and send it."""
        await pilot.pause(0.1)
        app.screen.query_one("#ask-text").load_text(text)
        await pilot.press("ctrl+j")
        await pilot.pause(0.15)

    # idea and questions ---------------------------------------------------------

    def test_the_idea_is_saved_as_typed_and_single_keys_are_just_letters(self) -> None:
        async def first(app, pilot) -> None:
            await pilot.press(*"blob")  # b and l are keys elsewhere; here they are text
            self.assertEqual(app.query_one("#idea-text").text, "blob")
            self.assertEqual(app.screen_name, "idea")

        async def second(app, pilot) -> None:
            self.assertEqual(app.query_one("#idea-text").text, "blob")

        self.drive("idea", first)
        shutil.move(self.base / "idea", self.base / "kept")
        from openfarm_tui.journey import Journey

        async def reopen() -> None:
            app = Journey(repo=self.base / "idea" / "iotd")
            shutil.move(self.base / "kept", self.base / "idea")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.2)
                await second(app, pilot)

        asyncio.run(reopen())

    def test_continue_needs_an_idea(self) -> None:
        async def body(app, pilot) -> None:
            await pilot.press("ctrl+j")
            self.assertIn("Say what you want to build first", str(app.query_one("#busy").render()))
            self.assertEqual(app.screen_name, "idea")

        self.drive("idea", body)

    def test_idea_to_questions_to_spec(self) -> None:
        async def body(app, pilot) -> None:
            app.query_one("#idea-text").load_text(IDEA)
            with mock.patch("openfarm_tui.journey.architect_grill_turn", side_effect=[QUESTION, READY]) as asked, \
                    mock.patch("openfarm_tui.journey.draft_spec_and_chunks", side_effect=fake_spec) as spec:
                await pilot.press("ctrl+j")
                await self.settle(app, pilot)
                self.assertEqual(app.screen_name, "questions")
                self.assertEqual(app.focused.id, "q-options")
                self.assertEqual(self.label(app), "▶ Answer")
                self.assertEqual(self.others(app), ["That's enough, write the spec"])
                self.assertIn("Question 1 of 2", str(app.query_one("#q-progress").render()))
                self.assertIn("(suggested)", str(app.query_one("#q-options").get_option_at_index(0).prompt))
                self.assertIn("It decides the first task.", str(app.query_one("#q-why").render()))
                await pilot.press("down", "enter")  # the second choice
                await self.settle(app, pilot)
                self.assertEqual(asked.call_args.kwargs["history"],
                                 [{"question": QUESTION["question"], "answer": "NASA's picture of the day"}])
                self.assertIs(asked.call_args.kwargs["backend"], self.backend)
                self.assertEqual(spec.call_args.kwargs["grill_history"][0]["answer"], "NASA's picture of the day")
            self.assertEqual(app.screen_name, "spec")
            self.assertEqual(self.label(app), "▶ Plan goal 1")
            self.assertEqual(self.others(app), ["Change the spec…", "Edit in $EDITOR"])
            self.assertEqual(app.focused.id, "go")
            self.assertIn(IDEA, str(app.query_one("#spec-text").render()))
            self.assertIn("Recursive Threads", str(app.query_one("#spec-goals").render()))
            self.assertIn("● Architect", str(app.query_one("#health").render()))

        self.drive("idea", body)

    def test_your_own_words_win_and_an_answer_can_be_changed(self) -> None:
        async def body(app, pilot) -> None:
            with mock.patch("openfarm_tui.journey.architect_grill_turn", return_value=QUESTION) as asked:
                app.query_one("#q-own").focus()
                await pilot.press(*"both", "enter")
                await self.settle(app, pilot)
                self.assertEqual(asked.call_args.kwargs["history"][-1]["answer"], "both")
                self.assertEqual(app.query_one("#q-answered").option_count, 2)
                app.query_one("#q-answered").focus()
                await pilot.press("enter")  # change the first answer: it is asked again
                await pilot.pause(0.1)
            self.assertIn("Where does the image come from?", str(app.query_one("#q-title").render()))
            self.assertEqual(app.query_one("#q-answered").option_count, 0)

        self.drive("questions", body)

    def test_an_unreachable_architect_says_why_and_offers_retry_and_nothing_is_set_up(self) -> None:
        async def body(app, pilot) -> None:
            app.query_one("#idea-text").load_text(IDEA)
            with mock.patch("openfarm_tui.journey.open_architect_backend", return_value=(None, "you are not signed in")):
                await pilot.press("ctrl+j")
                await self.settle(app, pilot)
            self.assertIn("The Architect can't be reached: you are not signed in", str(app.query_one("#busy").render()))
            self.assertEqual(self.label(app), "▶ Retry")
            self.assertEqual(self.others(app), ["Go on without the Architect", "Back"])
            self.assertIn("✗ Architect", str(app.query_one("#health").render()))
            self.assertFalse((app.repo / ".openfarm").exists())
            with mock.patch("openfarm_tui.journey.architect_grill_turn", return_value=QUESTION) as asked:
                await pilot.press("enter")  # Retry, now that the Architect answers
                await self.settle(app, pilot)
            self.assertEqual(app.screen_name, "questions")
            self.assertIs(asked.call_args.kwargs["backend"], self.backend)

        self.drive("idea", body)

    def test_a_step_can_be_cancelled(self) -> None:
        import threading

        hold = threading.Event()

        def slow(*_a, **_k):
            hold.wait(10)
            return QUESTION

        async def body(app, pilot) -> None:
            app.query_one("#idea-text").load_text(IDEA)
            with mock.patch("openfarm_tui.journey.architect_grill_turn", side_effect=slow):
                await pilot.press("ctrl+j")
                await pilot.pause(0.2)
                self.assertIn("Asking the Architect about your idea", str(app.query_one("#busy").render()))
                self.assertEqual(self.others(app), ["Cancel"])
                self.assertFalse(app.query_one("#go").display)
                await pilot.press("escape")
                hold.set()
                await self.settle(app, pilot)
            self.assertEqual(app.screen_name, "idea")  # the late answer is thrown away
            self.backend.close.assert_called()

        self.drive("idea", body)

    def test_long_names_and_long_choices_leave_the_heading_and_the_top_bar_whole(self) -> None:
        async def body(app, pilot) -> None:
            self.assertEqual(app.query_one("#questions").scroll_y, 0)
            self.assertIn("(suggested)", str(app.query_one("#q-options").get_option_at_index(1).prompt))

        root = build_project(self.base / "questions", "questions")
        from openfarm_tui.journey_state import load_session

        session = load_session(root)
        session["pending"]["options"] = [f"Choice {n}: " + "a long explanation of what this choice means " * 4 for n in range(5)]
        session["pending"]["recommended"] = "Choice 1: a long explanation"  # the Architect names it in fewer words
        save_session(root, session)
        import openfarm_tui.journey as journey

        self.app = lambda stage, theme="openfarm-dark": journey.Journey(repo=root, theme=theme)
        self.drive("questions", body, size=(100, 30))

        async def bar(app, pilot) -> None:
            from openfarm_core.automode import load_project_chunks, save_project_chunks

            goals = load_project_chunks(app.repo)
            goals[0]["title"] = "Image-of-the-day hero with live palette, served locally and themed from the photograph"
            save_project_chunks(app.repo, goals)
            app.refresh_screen()
            await pilot.pause(0.1)
            where = app.query_one("#where")
            self.assertLessEqual(len(str(where.render())) + 2, where.size.width)
            self.assertTrue(str(where.render()).endswith("…"))
            self.assertTrue(str(app.query_one("#goals").render()).splitlines()[0].endswith("…"))

        root2 = build_project(self.base / "spec", "spec")
        self.app = lambda stage, theme="openfarm-dark": journey.Journey(repo=root2, theme=theme)
        self.drive("spec", bar, size=(100, 30))

    # spec and plan ----------------------------------------------------------------

    def test_spec_to_plan_shows_the_tasks_the_checks_and_the_repairs(self) -> None:
        async def body(app, pilot) -> None:
            with mock.patch("openfarm_tui.journey.draft_wave_plan", side_effect=fake_plan) as plan:
                await pilot.press("enter")
                await self.settle(app, pilot)
            self.assertEqual(plan.call_args.kwargs["chunk_id"], "C1")
            self.assertEqual(app.screen_name, "plan")
            self.assertEqual(self.label(app), "▶ Build 2 tasks")
            self.assertEqual(self.others(app), ["Edit a task", "Change the plan…"])
            self.assertIn("Goal 1 · Living Image: 2 tasks, all checked", str(app.query_one("#plan-title").render()))
            row = str(app.query_one("#plan-tasks").get_option_at_index(0).prompt)
            self.assertIn("W1-01", row)
            self.assertIn("3 files · first", row)
            self.assertIn("The checks repaired 1 task(s) before showing them (W1-01)", "\n".join(app.activity))

        self.drive("spec", body)

    def test_a_parked_task_shows_which_check_failed_and_why(self) -> None:
        async def body(app, pilot) -> None:
            checks = str(app.query_one("#plan-checks").render())
            self.assertIn("✗ Shared pieces agree", checks)
            self.assertIn("W1-04: W1-03 serves the page with `#art`", checks)
            self.assertIn("✓ The order works", checks)
            self.assertIn("↻ Repaired W1-04: added `src/client/sample.ts`", checks)
            self.assertIn("(Space: why)", checks)
            self.assertIn("4 tasks, 1 can't be built yet", str(app.query_one("#plan-title").render()))
            self.assertEqual(self.others(app), ["Repair 1 parked", "Edit a task", "Change the plan…"])
            await pilot.press("end", "space")  # details of the parked task
            await pilot.pause(0.15)
            self.assertEqual(app.screen_name, "details")
            text = str(app.query_one("#details-text").render())
            for part in ("W1-04", "Spec", "Tests", "Plan checks", "History", "openfarm/preflight", "Diff"):
                self.assertIn(part, text)
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "plan")

        self.drive("plan", body)

    def test_edit_a_task_uses_the_full_height_editor_and_saves_through_the_store(self) -> None:
        async def body(app, pilot) -> None:
            await pilot.press("e")
            await pilot.pause(0.15)
            editor = app.screen.query_one("#edit-text")
            self.assertGreater(editor.size.height, 25)  # not a six-line box
            editor.load_text(editor.text.replace("Feed: fetch", "Feed: get"))
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            self.assertTrue(store.read("W1-01")[0]["title"].startswith("Feed: get"))
            self.assertEqual(store.history("W1-01")[-1]["why"], "edited by hand")

        self.drive("plan", body)

    # build ------------------------------------------------------------------------

    def test_build_starts_in_the_background_service_and_reopening_finds_it(self) -> None:
        from openfarm_core.daemon import daemon_status, start_daemon, stop_daemon
        from openfarm_core.state import StateDir
        from tests.test_phase2_build import FAKE_RUN

        flag = self.base / "finish"
        started: list[dict] = []

        def fake_start(repo, *, run_id, **kwargs):
            started.append(kwargs)
            events = StateDir(self.base / "plan" / "state").run_dir(run_id) / "events.jsonl"
            return start_daemon(repo, run_id=run_id,
                                command_override=[sys.executable, "-c", FAKE_RUN, str(events), str(flag)])

        async def until(app, pilot, wanted: str) -> None:
            for _ in range(100):
                if wanted in "\n".join(app.activity):
                    return
                await pilot.pause(0.1)
            self.fail(f"{wanted!r} never showed in {app.activity}")

        async def first(app, pilot) -> None:
            self.addCleanup(lambda: (flag.touch(), stop_daemon(app.repo)))
            with mock.patch("openfarm_tui.journey.start_daemon", side_effect=fake_start):
                await pilot.press("enter")
                await until(app, pilot, "CARD-1  writing the tests")
            self.assertTrue(started[0]["follow"])
            self.assertEqual(app.screen_name, "build")
            self.assertIn("Building", str(app.query_one("#build-title").render()))
            self.assertRegex(str(app.query_one("#where").render()), r"building · \d+ min")

        self.drive("plan", first)
        repo = self.base / "plan" / "iotd"
        self.assertTrue(daemon_status(repo)["running"], "the build outlives the window")

        async def reopen() -> None:
            from openfarm_tui.journey import Journey

            app = Journey(repo=repo)
            async with app.run_test(size=(120, 36)) as pilot:
                await until(app, pilot, "A build is already running; showing its progress")
                self.assertEqual(app.screen_name, "build")  # from the state, not from where the app was closed
                self.assertIn("CARD-1  writing the tests", "\n".join(app.activity))
                flag.touch()
                await until(app, pilot, "Build finished: 1 task(s)")

        asyncio.run(reopen())

    def test_the_build_screen_says_each_tasks_state_in_words_and_accepts_the_ready_one(self) -> None:
        async def body(app, pilot) -> None:
            rows = [str(app.query_one("#build-tasks").get_option_at_index(i).prompt) for i in range(4)]
            self.assertIn("being built · try 1", rows[0])
            self.assertIn("passed every check · ready for you", rows[1])
            self.assertIn("waits for W1-01", rows[2])
            ready = str(app.query_one("#build-ready").render())
            self.assertIn("W1-02", ready)
            self.assertIn("3 files · 4 tests pass · the old code fails them", ready)
            self.assertIn("Accept merges it into main on this machine. Nothing is pushed.", ready)
            self.assertIn("!  Build · needs you", str(app.query_one("#step-4").render()))
            self.assertTrue(app.query_one("#step-4").has_class("alert"))
            self.assertIn("1 needs you", str(app.query_one("#where").render()))
            with mock.patch.object(type(app), "_cli_bg") as cli:
                await pilot.press("enter")
                await pilot.pause(0.1)
            self.assertEqual(cli.call_args.args[0], [["verdict", "W1-02", "clean"], ["land", "W1-02"]])

        self.drive("build", body)

    def test_a_builders_question_is_shown_with_who_it_holds_up_and_the_answer_goes_into_the_task(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            header, text = store.read("W1-01")
            store.src._write("W1-01", dict(header, state="blocked"), text, "W1-01: blocked")
            store.src._write("FIND-1", {"id": "FIND-1", "title": "Which user-agent string should the feed request send?",
                                        "state": "backlog", "category": "finding", "from_card": "W1-01"},
                             "Wikimedia asks for one that names the app.", "FIND-1: asked")
            header, text = store.read("W1-02")
            store.src._write("W1-02", dict(header, state="done"), text, "W1-02: done")
            app.refresh_screen()
            await pilot.pause(0.1)
            card = str(app.query_one("#build-question").render())
            self.assertIn("W1-01 asks: Which user-agent string", card)
            self.assertIn("W1-01 waits for this, and so do W1-03", card)
            self.assertEqual(self.label(app), "▶ Answer W1-01's question")
            await pilot.press("enter")
            await self.say(app, pilot, "openfarm-iotd/1.0 (contact in the README)")
            self.assertIn("## Answer from the owner", store.read("W1-01")[1])
            self.assertIn("openfarm-iotd/1.0", store.read("W1-01")[1])
            self.assertNotEqual(store.read("W1-01")[0]["state"], "blocked")  # back in the plan, re-checked
            self.assertEqual(app.facts["questions"], [])

        self.drive("build", body)

    # blocked ------------------------------------------------------------------------

    def test_a_wrong_task_offers_its_fix_as_a_diff_and_apply_puts_it_back(self) -> None:
        async def body(app, pilot) -> None:
            self.assertEqual(self.label(app), "▶ See why W1-04 stopped")
            self.assertEqual(app.query_one("#build-tasks").highlighted, 3)  # starts on the task that needs you
            self.assertNotIn("Pause", [str(b.label) for b in app.query("#actions Button") if b.display])
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            header, text = store.read("W1-03")
            store.src._write("W1-03", dict(header, depends=["W1-04"]), text, "W1-03: waits for the stopped one")
            app.refresh_screen()
            await pilot.pause(0.1)
            self.assertIn("waits for W1-04 (stopped)", str(app.query_one("#build-tasks").get_option_at_index(2).prompt))
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "blocked")
            self.assertEqual(str(app.query_one("#blocked-title").render()), "✗ W1-04 stopped: the task is wrong, not the code")
            self.assertIn("src/client/sample.ts", str(app.query_one("#blocked-why").render()))
            fix = str(app.query_one("#blocked-fix").render())
            self.assertIn("Suggested fix", fix)
            self.assertIn('+touches = ["src/client/main.ts", "src/client/sample.ts"', fix)
            self.assertIn("nothing was merged", str(app.query_one("#blocked-tries").render()))
            self.assertEqual(self.label(app), "▶ Apply fix and retry")
            self.assertEqual(self.others(app), ["Edit the task myself", "Skip this task"])
            self.assertNotIn("Release", " ".join(self.others(app)))
            await pilot.press("enter")
            await pilot.pause(0.2)
            from openfarm_core.store import Store

            header = Store(app.repo, app.facts["cfg"]).read("W1-04")[0]
            self.assertIn("src/client/apply.ts", header["touches"])
            self.assertNotEqual(header["state"], "blocked")
            self.assertEqual(app.screen_name, "build")

        self.drive("blocked", body)

    def test_skip_asks_why_and_a_check_that_may_be_wrong_can_be_run_by_hand(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            cfg = app.facts["cfg"]
            record = json.loads((FIXTURES / "dogfood-blocked" / "w1-01-revert-check.json").read_text())
            folder = Path(cfg.state_dir) / "cards" / "W1-03" / "run-1"
            folder.mkdir(parents=True)
            (folder / "card-record.json").write_text(json.dumps(dict(record, card_id="W1-03")))
            store = Store(app.repo, cfg)
            header, text = store.read("W1-03")
            store.src._write("W1-03", dict(header, state="blocked"), text, "W1-03: blocked")
            app._open("blocked", "W1-03")
            await pilot.pause(0.2)
            self.assertIn("a check may be wrong", str(app.query_one("#blocked-title").render()))
            self.assertEqual(self.label(app), "▶ Run it myself")
            self.assertEqual(self.others(app), ["Accept anyway", "Retry"])
            ran = {"command": "npx vitest run tests/router.test.ts", "exit": 1, "output": "FAIL feedUrl builds the dated endpoint"}
            with mock.patch("openfarm_tui.journey.run_check", return_value=ran):
                await pilot.press("enter")
                await self.settle(app, pilot)
            shown = str(app.query_one("#blocked-fix").render())
            self.assertIn("npx vitest run tests/router.test.ts", shown)
            self.assertIn("FAIL feedUrl builds the dated endpoint", shown)

            app._open("blocked", "W1-04")
            await pilot.pause(0.2)
            app.action_act("skip_task")
            await self.say(app, pilot, "the page works without it")
            self.assertEqual(store.read("W1-04")[0]["state"], "dropped")
            self.assertEqual(store.history("W1-04")[-1]["why"], "the page works without it")

        self.drive("blocked", body)

    # try it and accept -----------------------------------------------------------------

    def test_try_it_shows_the_failed_check_and_fix_this_asks_for_fix_tasks(self) -> None:
        async def body(app, pilot) -> None:
            checks = str(app.query_one("#try-checks").render())
            self.assertIn("✗ the title, story and credit are on the page but can't be seen", checks)
            self.assertIn("it is covered by img#art", checks)
            self.assertIn("node src/server.ts on http://127.0.0.1:8931/", str(app.query_one("#try-sub").render()))
            self.assertEqual(self.label(app), "▶ Fix this")
            self.assertEqual(self.others(app), ["Something else is wrong…", "Looks right anyway", "Open in browser"])
            await pilot.press("o")
            import openfarm_tui.journey as journey

            self.assertTrue(journey.webbrowser.open.call_args.args[0].endswith("desktop-1440x900.png"))
            with mock.patch("openfarm_tui.journey.something_wrong", return_value={"new_cards": ["W1-05"]}) as wrong:
                await pilot.press("enter")
                await self.settle(app, pilot)
            self.assertIn("can't be seen (it is covered by img#art)", wrong.call_args.args[1])
            self.assertEqual(wrong.call_args.kwargs["chunk_id"], "C1")
            self.assertIn("1 fix task(s) added under this goal: W1-05", "\n".join(app.activity))

        self.drive("try", body)

    def test_looks_right_anyway_asks_why_and_moves_to_accept(self) -> None:
        async def body(app, pilot) -> None:
            app.action_act("looks_right")
            await self.say(app, pilot, "the cover is the design")
            self.assertEqual(app.screen_name, "accept")
            saved = json.loads((Path(app.facts["cfg"].state_dir) / "tryit" / "C1" / "result.json").read_text())
            self.assertEqual((saved["looks_right"], saved["looks_right_why"]), (True, "the cover is the design"))

        self.drive("try", body)

    def test_a_goal_not_yet_tried_is_tried_on_arrival(self) -> None:
        async def body(app, pilot) -> None:
            await self.settle(app, pilot)
            self.assertEqual(app.screen_name, "try")  # the checks passed, and a person still looks
            self.assertEqual(self.label(app), "▶ Looks right")
            self.assertEqual(self.others(app), ["Something else is wrong…", "Try again", "Open in browser"])
            await pilot.press("enter")
            await pilot.pause(0.15)
            self.assertEqual(app.screen_name, "accept")

        import openfarm_tui.journey as journey
        from openfarm_core.tryit import code_version

        def fake_try(root, cfg, goal):
            out = Path(cfg.state_dir) / "tryit" / goal["id"] / "result.json"
            result = {"kind": "web", "goal": goal["id"], "command": "node src/server.ts", "url": "http://127.0.0.1:1/",
                      "checks": [{"name": "loads", "ok": True, "detail": "the page loads at /"}], "screenshots": [],
                      "output": "", "words": [], "ok": True, "head": code_version(root)}
            out.write_text(json.dumps(result))
            return result

        journey.try_it.side_effect = fake_try
        build_project(self.base / "try", "try")
        (self.base / "try" / "state" / "tryit" / "C1" / "result.json").unlink()
        self.app = lambda stage, theme="openfarm-dark": journey.Journey(repo=self.base / "try" / "iotd", theme=theme)
        self.drive("try", body)

    def test_accepting_a_goal_moves_to_the_next_goals_plan_step(self) -> None:
        async def body(app, pilot) -> None:
            text = str(app.query_one("#accept-text").render())
            self.assertIn("4 built and merged", text)
            self.assertIn("you said it looks right", text)
            self.assertEqual(self.label(app), "▶ Accept goal 1")
            self.assertEqual(self.others(app), ["Something's wrong…", "Try it"])
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "spec")
            self.assertEqual(self.label(app), "▶ Plan goal 2")
            self.assertIn("✓  Living Image", str(app.query_one("#goals").render()))
            self.assertIn("Goal 2 of 3 · Recursive Threads", str(app.query_one("#where").render()))

        self.drive("accept", body)

    def test_when_every_goal_is_accepted_a_new_feature_becomes_the_next_goal(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.automode import load_project_chunks, save_project_chunks

            save_project_chunks(app.repo, [dict(g, status="done") for g in load_project_chunks(app.repo)])
            app.refresh_screen()
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "accept")
            self.assertEqual(self.label(app), "▶ Add a feature…")
            await pilot.press("enter")
            await self.say(app, pilot, "Share a day's image")
            self.assertEqual(app.screen_name, "spec")
            self.assertEqual(self.label(app), "▶ Plan goal 4")

        self.drive("accept", body)

    # board, log, palette, theme -----------------------------------------------------------

    def test_the_board_groups_every_task_and_opens_the_one_chosen(self) -> None:
        async def body(app, pilot) -> None:
            await pilot.press("b")
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "board")
            heads = [str(app.query_one(f"#head-{key}").render()) for key in ("waiting", "building", "needs", "done")]
            self.assertEqual(heads, ["WAITING  1", "BUILDING  0", "NEEDS YOU  1", "DONE  2"])
            tile = str(app.query_one("#col-needs").get_option_at_index(1).prompt)
            self.assertIn("W1-04", tile)
            self.assertIn("the task is wrong", tile)
            self.assertEqual(str(app.query_one("#col-needs").get_option_at_index(0).prompt), "Goal 1")  # grouped by goal
            self.assertTrue(app.query_one("#col-needs").get_option_at_index(0).disabled)
            self.assertIn("Goals 2 and 3 have no tasks yet", str(app.query_one("#board-note").render()))
            self.assertEqual(self.label(app), "▶ Open W1-04")
            await pilot.press("right")
            await pilot.pause(0.1)
            self.assertEqual(app.focused.id, "col-done")
            self.assertEqual(self.label(app), "▶ Open W1-01")
            await pilot.press("left", "enter")
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "blocked")
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "build")

        self.drive("blocked", body)

    def test_the_activity_strip_keeps_its_height_and_l_opens_the_whole_log(self) -> None:
        async def body(app, pilot) -> None:
            for number in range(9):
                app.say(f"event {number}")
            await pilot.pause(0.1)
            strip = app.query_one("#activity")
            self.assertEqual(strip.region.height, 4)
            self.assertNotIn("event 5", str(strip.render()))
            self.assertIn("event 8", str(strip.render()))
            await pilot.press("l")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "log")
            self.assertIn("event 0", str(app.query_one("#log-text").render()))
            self.assertEqual(strip.region.height, 4)
            await pilot.press("l")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "build")

        self.drive("build", body)

    def test_ctrl_k_holds_everything_else_including_buttons_that_had_no_room(self) -> None:
        async def body(app, pilot) -> None:
            self.assertEqual(self.others(app), ["Something else is wrong…", "Looks right anyway"])  # no room for the third
            labels = [label for label, _action in app.palette_actions()]
            self.assertIn("Open in browser", labels)
            for wanted in ("Projects: switch, add or remove a project", "Providers and sign-in", "Board: every task of every goal"):
                self.assertIn(wanted, labels)
            self.assertIn("ctrl+k", [b.key for b in app.BINDINGS])

        self.drive("try", body, size=(100, 30))

    def test_the_other_screens_open_from_the_palette_and_come_back(self) -> None:
        import openfarm_tui

        runs = iter([("classic", "projects"), None])
        with mock.patch("openfarm_tui.journey.Journey.run", side_effect=lambda: next(runs)) as journey, \
                mock.patch("openfarm_tui.app.OpenfarmApp.run") as classic, \
                mock.patch("openfarm_tui.app.OpenfarmApp.__init__", return_value=None) as made:
            self.assertEqual(openfarm_tui.run_textual_tui(self.base), 0)
        self.assertEqual(journey.call_count, 2)
        self.assertEqual(classic.call_count, 1)
        self.assertEqual(made.call_args.kwargs["initial_view"], "projects")


from tests.test_store import MigrationTests as _OlderProject  # noqa: E402


@unittest.skipUnless(textual_available(), "textual optional extra not installed")
class MigrationScreenTests(_OlderProject):
    """An older project: the screen offers the one-time move, shows both copies of a task that differs, and waits."""

    for _name in [n for n in dir(_OlderProject) if n.startswith("test_")]:
        locals()[_name] = None  # the store's own tests run in test_store
    del _name

    def test_the_move_is_offered_shows_both_copies_and_does_nothing_until_chosen(self) -> None:
        from openfarm_tui.journey import ChoiceModal, Journey

        async def run() -> None:
            app = Journey(repo=self.root)
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause(0.3)
                self.assertIsInstance(app.screen, ChoiceModal)
                self.assertIn("removed in one commit", str(app.screen.query_one("VerticalScroll Static").render()))
                self.assertEqual(str(app.focused.label), "Move them")
                await pilot.press("escape")  # not now: nothing moves
                await pilot.pause(0.1)
                self.assertTrue((self.root / "cards" / "W1-02.md").is_file())
                app.offer_migration()
                await pilot.pause(0.1)
                await pilot.press("enter")
                await pilot.pause(0.2)
                both = str(app.screen.query_one("VerticalScroll Static").render())
                self.assertIn("W1-02 is different", str(app.screen.query("Static").first().render()))
                self.assertIn("Edited on main", both)
                self.assertIn("Task W1-02", both)
                self.assertTrue((self.root / "cards").is_dir())  # still waiting for the choice
                await pilot.press("enter")  # the last button: keep main's
                await pilot.pause(0.3)
                self.assertFalse((self.root / "cards").exists())
                self.assertIn("now live on the cards branch", "\n".join(app.activity))

        with mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(self.root.parent / "projects.json")}):
            asyncio.run(run())
        from openfarm_core.store import Store

        self.assertEqual(Store(self.root, self.cfg).read("W1-02")[0]["title"], "Edited on main")


class ThemeTests(unittest.TestCase):
    def test_two_themes_and_no_colour_outside_them(self) -> None:
        import re

        from openfarm_tui import journey

        self.assertEqual([theme.name for theme in journey.THEMES], ["openfarm-dark", "openfarm-light"])
        source = Path(journey.__file__).read_text()
        rest = source.replace(source[source.index("THEMES = ["):source.index("STACKS = [")], "")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![\w-])", rest), [])
        for name in re.findall(r"\$([a-z-]+)", journey.Journey.CSS):
            self.assertIn(name, ("primary", "accent", "success", "error", "foreground", "foreground-muted", "panel",
                                 "surface", "background"), name)

    def test_the_theme_follows_the_terminal(self) -> None:
        from openfarm_tui import journey

        with mock.patch.dict(os.environ, {"COLORFGBG": "0;15"}):
            self.assertTrue(journey.terminal_is_light())
            self.assertEqual(journey.Journey(repo=Path(".")).__dict__["_theme_name"], "openfarm-light")
        with mock.patch.dict(os.environ, {"COLORFGBG": "15;0"}):
            self.assertFalse(journey.terminal_is_light())


def shots(out: Path) -> None:
    """SVG screenshots of every screen, light and dark, for the report."""
    case = ScreenTests("test_each_screen_has_its_button_its_focus_and_fits")
    case.setUp()

    async def one(screen: str, theme: str) -> None:
        shutil.rmtree(case.base / SCREENS[screen][0], ignore_errors=True)
        app = case.app(SCREENS[screen][0], theme)
        async with app.run_test(size=(120, 36)) as pilot:
            await open_screen(app, pilot, screen)
            app.save_screenshot(f"{screen}-{theme.split('-')[1]}.svg", path=str(out))

    try:
        out.mkdir(parents=True, exist_ok=True)
        for theme in ("openfarm-dark", "openfarm-light"):
            for screen in SCREENS:
                asyncio.run(one(screen, theme))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--shots":
        shots(Path(sys.argv[2]))
    else:
        unittest.main()
