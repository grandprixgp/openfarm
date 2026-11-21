"""Phase 3: the one screen. Which pane shows, its main button, its focus, and that it fits."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


def rail(app, which: str) -> list[str]:
    """The rows of the rail's steps or goals list, as written."""
    from textual.widgets import OptionList

    rows = app.query_one(f"#rail-{which}", OptionList)
    return [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]


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
        if stage != "plan" and path.stem == "W1-03":
            header = dict(header, network=True)  # the server task, as the plan step marked it
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
            "colours": [{"hex": "#101820", "use": "background"}, {"hex": "#3a5a78", "use": "background"},
                        {"hex": "#f2efe6", "use": "text"}],
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
    "projects": ("build", "projects", "▶ Back to it", "proj-list"),
    "providers": ("build", "providers", "▶ Save", "prov-architect"),
    "models": ("build", "models", "▶ Save", "mod-scope"),
    "settings": ("build", "settings", "▶ Run the check-up", "go"),
    "notes": ("build", "notes", "▶ Back", "go"),
}


class _Folder(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(os.environ.get("TMPDIR") or tempfile.gettempdir(), exist_ok=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patches = [mock.patch.dict(os.environ, {"OPENFARM_PROJECTS_FILE": str(self.base / "projects.json"), "EDITOR": "",
                                                "VISUAL": "", "OPENFARM_PROVIDERS_FILE": str(self.base / "providers.toml"),
                                               "OPENFARM_MODELS_FILE": str(self.base / "models.toml")}),
                   mock.patch("openfarm_tui.journey_more.inspect_acp_status", return_value={"oauth_ready": True}),
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
    elif overlay in ("projects", "providers", "models", "settings", "notes"):
        from openfarm_core.projects import register_project

        register_project(app.repo, name="iotd")
        app.open_more(overlay)
    elif overlay:
        task = "W1-04" if overlay == "blocked" else "W1-02"
        app._open(overlay, task)
    await pilot.pause()  # until what the screen queued for itself has run, however busy the machine
    await pilot.pause(0.2)
    await pilot.pause()


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
                await pilot.pause()  # a busy machine lays the buttons out a moment after their words change
                await pilot.pause(0.2)
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
        self.builds = mock.patch("openfarm_tui.journey.start_daemon")  # no test starts a real build by accident
        self.started = self.builds.start()
        self.addCleanup(self.builds.stop)

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
            self.assertIn("…", rail(app, "goals")[0])

        root2 = build_project(self.base / "spec", "spec")
        self.app = lambda stage, theme="openfarm-dark": journey.Journey(repo=root2, theme=theme)
        self.drive("spec", bar, size=(100, 30))

    def test_a_build_left_with_only_a_parked_task_still_has_a_main_button(self) -> None:
        # from the acceptance run: a fix that failed the plan checks parked the last task and left no way forward
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            for task in app.facts["tasks"]:
                header, text = store.read(task["id"])
                if task["id"] == "W1-04":
                    header = dict(header, state="backlog", preflight=["touches: it names a file no task may write"])
                elif header.get("state") != "done":
                    header = dict(header, state="done")
                store.src._write(task["id"], header, text, f"{task['id']}: as the run left it")
            app.refresh_screen()
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "build")
            self.assertEqual(self.label(app), "▶ Repair 1 task")

        self.drive("blocked", body)

    # spec and plan ----------------------------------------------------------------

    def test_the_plan_says_which_task_may_use_the_network_before_build(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            # the plan step marked the server task itself, and says so twice: as a repair, and as a warning
            box = str(app.query_one("#plan-checks").render())
            self.assertIn("✓ A task that needs the network says so", box)
            self.assertIn("Repaired W1-03: marked as allowed to use the network", box)
            self.assertIn("W1-03 may use the network", box)
            self.assertNotIn("W1-02 may use the network", box)
            store = Store(app.repo, app.facts["cfg"])
            self.assertIs(store.read("W1-03")[0]["network"], True)
            header, text = store.read("W1-03")
            store.change_card("W1-03", "edit", why="unmarked by hand", apply=lambda h, b: ({k: v for k, v in h.items() if k != "network"}, b))
            app.refresh_screen()
            await pilot.pause(0.1)
            box = str(app.query_one("#plan-checks").render())  # taken off by hand: parked, with the reason
            self.assertIn("✗ A task that needs the network says so", box)
            self.assertIn("not marked `network = true`", box)

        self.drive("plan", body)

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
            # "Space: why" is answered before the spec, where it used to sit below three sections
            self.assertLess(text.index("Why it can't be built yet"), text.index("Spec"))
            self.assertLess(text.index("W1-03 serves the page with `#art`"), text.index("Spec"))
            self.assertNotIn("(Space: why)", text)
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
            self.assertIn("!  Build · needs you", rail(app, "steps")[4])
            self.assertIn("$error", rail(app, "steps")[4])
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
                                        "state": "backlog", "category": "finding", "from_card": "W1-01",
                                        "audience": "owner"},
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
            await self.settle(app, pilot)
            self.started.assert_called_once()  # and the build goes back to work without being told to
            self.assertIn("Starting the build", "\n".join(app.activity))

        self.drive("build", body)

    def test_a_question_that_is_not_the_owners_is_answered_without_being_shown(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            self.backend.ask.return_value = json.dumps({"for": "architect", "answer": "Send openfarm-iotd/1.0."})
            store = Store(app.repo, app.facts["cfg"])
            header, text = store.read("W1-01")
            store.src._write("W1-01", dict(header, state="blocked"), text, "W1-01: blocked")
            store.src._write("FIND-1", {"id": "FIND-1", "title": "Which user-agent string should the feed request send?",
                                        "state": "backlog", "category": "finding", "from_card": "W1-01"},
                             "The existing proxy already sends one.", "FIND-1: asked")
            app.refresh_screen()
            self.assertEqual(app.facts["questions"], [])  # not shown while it is being sorted
            self.assertTrue(app.query_one("#build-question").has_class("hidden"))
            await self.settle(app, pilot)
            await self.settle(app, pilot)
            self.assertIn("## Answer from the Architect", store.read("W1-01")[1])
            self.assertIn("a question the Architect could answer", "\n".join(app.activity))
            self.assertEqual(app.facts["questions"], [])
            self.assertTrue(app.query_one("#build-question").has_class("hidden"))
            self.started.assert_called_once()  # and the build picked the task up again

        self.drive("build", body)

    # blocked ------------------------------------------------------------------------

    def test_a_wrong_task_offers_its_fix_as_a_diff_and_apply_puts_it_back(self) -> None:
        async def body(app, pilot) -> None:
            self.assertEqual(self.label(app), "▶ See why W1-04 stopped")
            self.assertEqual(app.query_one("#build-tasks").highlighted, 3)  # starts on the task that needs you
            from openfarm_core.store import Store as _Store

            _store = _Store(app.repo, app.facts["cfg"])
            _h, _t = _store.read("W1-03")
            _store.src._write("W1-03", dict(_h, state="in_review"), _t, "W1-03: finished while the list was open")
            app.refresh_screen()
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#build-tasks").highlighted, 2)  # and follows the next one that does
            _store.src._write("W1-03", dict(_h, state="ready"), _t, "W1-03: back")
            app.refresh_screen()
            await pilot.pause(0.2)
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

    def test_the_token_share_counts_builders_reviewers_and_the_architect(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.automode import _AcpArchitectBackend, record_architect_use, token_share

            state = Path(app.facts["cfg"].state_dir)
            (state / "cards" / "W1-01" / "run-9").mkdir(parents=True)
            (state / "cards" / "W1-01" / "run-9" / "card-record.json").write_text(json.dumps(
                {"card_id": "W1-01", "tokens_in": 5000, "tokens_out": 1000, "micromanager_tokens_in": 1500,
                 "micromanager_tokens_out": 500}))
            before = token_share(state)
            record_architect_use(state, 1800, 200)
            worker = mock.Mock()
            worker.complete.return_value = mock.Mock(content="{}", tokens_in=900, tokens_out=100)
            backend = _AcpArchitectBackend(worker, root=app.repo, state_dir=state)
            backend.ask("plan it")  # every call the Architect answers is counted, whoever made it
            share = token_share(state)
            self.assertEqual(share["builders"] - before["builders"], 0)
            self.assertEqual(share["architect"] - before["architect"], 3000)
            app.refresh_screen()
            await pilot.pause(0.1)
            self.assertEqual(app.real["tokens"], share)
            total = share["total"]
            self.assertIn(f"{total // 1000}k tokens", str(app.query_one("#health").render()))
            for who in ("builders", "reviewers", "Architect"):
                self.assertIn(f"{who} ", app.share)
            self.assertIn(f"Architect {share['architect']:,} tokens ({100 * share['architect'] // total}%)", app.share)
            await pilot.press("l")
            await pilot.pause(0.1)
            self.assertIn("Tokens so far: builders", str(app.query_one("#log-text").render()))

        self.drive("build", body)

    def test_architect_tokens_spent_before_there_is_a_project_are_not_lost(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.automode import _AcpArchitectBackend
            from openfarm_tui.journey_state import load_session

            worker = mock.Mock()
            worker.complete.return_value = mock.Mock(content="{}", tokens_in=700, tokens_out=50)
            backend = _AcpArchitectBackend(worker, root=app.repo)
            backend.ask("first question")
            app._settle_architect_use(backend)
            self.assertEqual(load_session(app.repo)["spent"], [[700, 50]])  # kept with the idea
            self.assertEqual(app._unrecorded, 750)
            app._settle_architect_use(None)
            self.assertEqual(load_session(app.repo)["spent"], [[700, 50]])  # and not counted twice

        self.drive("questions", body)

    def test_a_question_that_could_not_be_sorted_is_sorted_again_later(self) -> None:
        # v0.20 live run: the second of two questions hit "W1-04 is building; stop it first" and was never looked at again
        async def body(app, pilot) -> None:
            finding = {"id": "FIND-9", "from_card": "W1-01", "title": "t", "body": ""}
            app._sorting.add("FIND-9")
            with mock.patch("openfarm_tui.journey.settle_question", side_effect=RuntimeError("W1-01 is building; stop it first")), \
                    mock.patch("openfarm_tui.journey.open_architect_backend", return_value=(None, "")):
                app._sort_questions([finding])
                await app.workers.wait_for_complete()
                await pilot.pause(0.1)
            self.assertIn("could not be sorted: W1-01 is building", "\n".join(app.activity))
            self.assertNotIn("FIND-9", app._sorting)

        self.drive("build", body)

    def test_a_task_unblocked_while_the_last_build_is_still_ending_is_built_once_it_has_ended(self) -> None:
        # v0.20 live run: the answer came in the second the build finished, and the task sat at "not started"
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            for card in ("W1-01", "W1-03"):
                header, text = store.read(card)
                store.src._write(card, dict(header, state="ready"), text, f"{card}: ready")
            app._following = True  # the build that stopped it has not let go yet
            self.started.reset_mock()
            app._resume_build()
            await pilot.pause(0.2)
            self.started.assert_not_called()
            self.assertTrue(app._resume_wanted)
            app._following = False
            app._tick()
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            self.started.assert_called_once()
            self.assertFalse(app._resume_wanted)

        self.drive("build", body)

    def test_a_running_build_shows_a_live_log_of_its_checks_in_plain_words(self) -> None:
        async def body(app, pilot) -> None:
            box = app.query_one("#build-log")
            self.assertTrue(box.has_class("hidden"))  # nothing is being built
            app._following = True
            ts = "2026-11-21T10:55:13Z"
            app._run_event({"kind": "run_start", "ts": ts, "message": "Starting run r (2 card(s))"})
            app._run_event({"kind": "phase", "card_id": "W1-01", "phase": "check", "ts": ts})
            app._run_event({"kind": "log", "card_id": "W1-01", "ts": ts,
                            "message": "W1-01: suite pass — 36 existing test(s)"})
            app._run_event({"kind": "log", "card_id": "W1-01", "ts": ts, "message": "W1-01: fail-before fail — 1 passed"})
            app._run_event({"kind": "log", "card_id": "W1-01", "ts": ts, "message":
                            "W1-01: lint fail — rt path can only end with '.ts'\ntests/a.test.ts(4,31): error TS5097: An import "
                            "path can only end with a '.ts' extension when 'allowImportingTsExtensions' is enabled.\nmore"})
            app._run_event({"kind": "log", "card_id": "W1-01", "ts": ts, "message": "W1-01: something new happened"})
            await pilot.pause(0.2)
            self.assertFalse(box.has_class("hidden"))
            shown = str(box.render())
            self.assertIn("What the build is doing", shown)
            self.assertIn("10:55  W1-01  running the checks", shown)
            self.assertIn("W1-01  ✓ the whole test suite passes  36 existing test(s)", shown)
            self.assertIn("✗ failed: the new tests fail on the old code", shown)
            # a failed check shows its first whole error line in full, not the cut-off start of the tool's output
            self.assertIn("when 'allowImportingTsExtensions' is enabled.", " ".join(shown.split()))
            self.assertNotIn("end with '.ts'", shown)  # the cut-off first line (the whole one says "a '.ts'")
            self.assertIn("and 1 more lines", shown)
            self.assertIn("something new happened", shown)  # a message with no plain words is still shown
            self.assertNotIn("fail-before", shown)
            for n in range(40):
                app._run_event({"kind": "log", "ts": ts, "message": f"W1-01: lint pass — {n}"})
            await pilot.pause(0.2)
            self.assertEqual(len(str(box.render()).splitlines()), 8)  # the title and the newest lines; fewer
            # here because a task is waiting to be accepted above it, and both should stay in view
            app._following = False
            app.refresh_screen()
            await pilot.pause(0.2)
            self.assertTrue(box.has_class("hidden"))  # the build ended: the room goes back

        self.drive("build", body)

    def test_waiting_for_the_architect_shows_what_is_done_and_what_is_arriving(self) -> None:
        async def body(app, pilot) -> None:
            box = app.query_one("#busy-log")
            self.assertTrue(box.has_class("hidden"))
            app._set_busy("Asking the Architect how to fix W1-01")
            app._progress("Reading the answer.")
            app._heard("The typecheck fails because tsconfig lacks a setting.\n")
            app._heard("So the task should also list tsconfig.json")
            app._draw_busy()
            await pilot.pause(0.1)
            self.assertFalse(box.has_class("hidden"))
            shown = str(box.render())
            self.assertIn("✓ Asking the Architect how to fix W1-01", shown)  # the stage that is over
            self.assertIn("The Architect has written 96 characters so far:", shown)
            self.assertIn("So the task should also list tsconfig.json", shown)
            self.assertIn("Reading the answer", str(app.query_one("#busy").render()))
            app._set_busy("")
            await pilot.pause(0.1)
            self.assertTrue(box.has_class("hidden"))
            app._set_busy("Asking again")  # a new wait starts with an empty log
            app._draw_busy()
            await pilot.pause(0.1)
            self.assertTrue(box.has_class("hidden"))
            app._set_busy("")

        self.drive("build", body)

    def test_a_stopped_tasks_tries_show_the_whole_reason_wrapped(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.blocked import records

            cfg = app.facts["cfg"]
            said = "The blocker is outside this task. " + "The repo-wide typecheck fails because tsconfig lacks a setting. " * 4 + "END-OF-REASON"
            with mock.patch("openfarm_core.blocked.records",
                            return_value=[{**(records(cfg.state_dir, "W1-03") or [{}])[-1], "detail": said}]):
                app._open("blocked", "W1-03")
                await pilot.pause(0.3)
                shown = str(app.query_one("#blocked-tries").render())
            self.assertIn("END-OF-REASON", shown)  # nothing is cut off
            lines = shown.splitlines()
            self.assertGreater(len(lines), 4)  # it wraps under itself
            self.assertTrue(all(len(line) <= 120 for line in lines))

        self.drive("build", body)

    def test_a_task_the_builder_keeps_failing_can_be_reworked_by_the_architect_with_what_the_tries_found(self) -> None:
        # the owner's report: four tries ended on the same thing and the only offer was another Retry
        async def body(app, pilot) -> None:
            app._open("blocked", "W1-04")
            await pilot.pause(0.2)
            task = app._find()
            once = {**task, "triage": {**task["triage"], "kind": "builder"}, "tries": 1}
            app.fix = None
            with mock.patch.object(app, "_find", return_value=once):
                primary, others, _keys = app._actions_blocked()
            self.assertEqual(primary, ("retry_task", "Retry"))
            self.assertEqual(others[0], ("ask_fix", "Have the Architect rework the task"))
            with mock.patch.object(app, "_find", return_value={**once, "tries": 4}):
                primary, others, _keys = app._actions_blocked()
                self.assertEqual(primary, ("ask_fix", "Have the Architect rework the task"))  # Retry alone has not worked
                self.assertEqual(others[0], ("retry_task", "Retry"))
                tries = [{"claimed_at": "2026-11-21T10:33:00Z", "detail": "tsconfig lacks allowImportingTsExtensions"},
                         {"claimed_at": "2026-11-21T14:14:00Z", "detail": "the same error signature came back twice"}]
                with mock.patch("openfarm_core.blocked.records", return_value=tries), \
                        mock.patch("openfarm_tui.journey.propose_fix", return_value=None) as asked:
                    app.do_ask_fix()
                    await app.workers.wait_for_complete()
                    await pilot.pause(0.2)
            why = asked.call_args.kwargs["why"]
            self.assertIn("- try 1: tsconfig lacks allowImportingTsExtensions", why)  # every try's finding reaches the Architect
            self.assertIn("- try 2: the same error signature came back twice", why)

        self.drive("blocked", body)

    def test_a_project_failing_its_own_lint_stops_the_task_with_the_way_out_as_the_main_button(self) -> None:
        async def body(app, pilot) -> None:
            app._open("blocked", "W1-04")
            await pilot.pause(0.2)
            task = app._find()
            stopped = {**task, "triage": {**task["triage"], "kind": "builder", "base_lint": True}}
            fix = {"label": "Check syntax only", "table": "runner", "key": "typescript_typecheck", "value": "syntax",
                   "why": "the full TypeScript check is switched off for this project"}
            pack = mock.Mock(base_lint_fix=mock.Mock(return_value=fix))
            with mock.patch.object(app, "_find", return_value=stopped), \
                    mock.patch("openfarm_core.cli._configured_pack", return_value=pack):
                primary, others, _keys = app._actions_blocked()
                self.assertEqual(primary, ("base_lint_fix", "Check syntax only and retry"))
                self.assertEqual(others[0], ("retry_task", "Retry"))
                app.refresh_screen()
                await pilot.pause(0.2)
                shown = str(app.query_one("#blocked-fix").render())
                self.assertIn("The way out: check syntax only", shown)
                self.assertIn('typescript_typecheck = "syntax"', " ".join(shown.split()))
                with mock.patch.object(app, "do_retry_task") as retried:
                    app.do_base_lint_fix()
                retried.assert_called_once()
            config = (app.repo / ".openfarm" / "config.toml").read_text()
            self.assertIn('typescript_typecheck = "syntax"', config)
            self.assertEqual(config.count("typescript_typecheck ="), 1)  # set where it stood, not added twice
            from openfarm_core.config import load_config

            self.assertEqual(load_config(app.repo).typescript_typecheck, "syntax")

        self.drive("blocked", body)

    def test_the_safety_stop_says_why_and_is_cleared_from_the_build_screen(self) -> None:
        # the message used to point at Settings, which had nothing to clear it with
        async def body(app, pilot) -> None:
            from openfarm_core.state import StateDir
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            for card in ("W1-01", "W1-03"):
                header, text = store.read(card)
                store.src._write(card, dict(header, state="ready"), text, f"{card}: ready")
            header, text = store.read("W1-02")
            store.src._write("W1-02", dict(header, state="done"), text, "W1-02: done")
            state = StateDir(app.facts["cfg"].state_dir)
            state.write_breaker(True, ["W1-01", "W1-03"], "2 cards blocked in a row for phase-b-exhausted")
            app.refresh_screen()
            await pilot.pause(0.2)
            self.assertEqual(self.label(app), "▶ Clear the safety stop and build 2 tasks")
            title = str(app.query_one("#build-title").render())
            self.assertIn("the safety stop is on: W1-01, W1-03 stopped one after another", title)
            self.assertIn("phase-b-exhausted", title)
            self.assertNotIn("Settings", title)
            self.started.reset_mock()

            def cleared(commands, done, then=None):
                state.write_breaker(False, [])
                app._set_busy("")
                then()

            with mock.patch.object(app, "_cli_bg", side_effect=cleared) as cli:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            self.assertEqual(cli.call_args.args[0], [["resume", "--clear-breaker", "--clear-kill"]])
            self.started.assert_called_once()  # and the build goes on without a second press

        self.drive("build", body)

    def test_the_plan_step_offers_to_clear_the_safety_stop_too(self) -> None:
        # a stop left on from an earlier build met the owner at Plan, where "Build" could only refuse
        async def body(app, pilot) -> None:
            from openfarm_core.state import StateDir

            ready = sum(1 for t in app.facts["tasks"] if t["state"] == "ready")
            state = StateDir(app.facts["cfg"].state_dir)
            state.write_breaker(True, ["W1-01", "W1-02", "W1-03"], "3 cards blocked in a row for no-progress")
            app.refresh_screen()
            await pilot.pause(0.2)
            self.assertEqual(self.label(app), f"▶ Clear the safety stop and build {ready} task{'s' if ready != 1 else ''}")
            self.started.reset_mock()

            def cleared(commands, done, then=None):
                state.write_breaker(False, [])
                app._set_busy("")
                then()

            with mock.patch.object(app, "_cli_bg", side_effect=cleared) as cli:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            self.assertEqual(cli.call_args.args[0], [["resume", "--clear-breaker", "--clear-kill"]])
            self.started.assert_called_once()

        self.drive("plan", body)

    def test_a_build_that_paused_itself_says_why_and_resume_carries_on(self) -> None:
        # v0.20 live run: the build paused itself on a reviewer answer it could not read; the main button was still
        # "Build 1 task", which only said "use Resume"
        async def body(app, pilot) -> None:
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            for card in ("W1-01", "W1-03"):
                header, text = store.read(card)
                store.src._write(card, dict(header, state="ready"), text, f"{card}: ready")
            header, text = store.read("W1-02")
            store.src._write("W1-02", dict(header, state="done"), text, "W1-02: done")
            state = Path(app.facts["cfg"].state_dir)
            (state / "paused").write_text(json.dumps({"reason": "infra error on W1-03: the reviewer's answer was not JSON"}))
            app.refresh_screen()
            await pilot.pause(0.2)
            self.assertEqual(self.label(app), "▶ Resume and build 2 tasks")
            self.assertIn("The build can't go on: it is paused (infra error on W1-03", str(app.query_one("#build-title").render()))
            self.assertNotIn("Resume", self.others(app))  # one Resume, the main one
            self.started.reset_mock()

            def resumed(commands, done, then=None):
                (state / "paused").unlink()
                app.say(done)
                app._set_busy("")
                then()

            with mock.patch.object(app, "_cli_bg", side_effect=resumed) as cli:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            self.assertEqual(cli.call_args.args[0], [["resume", "--clear-breaker", "--clear-kill"]])
            self.started.assert_called_once()  # resumed, and the waiting tasks are being built

        self.drive("build", body)

    def test_any_goal_can_be_opened_from_the_keyboard(self) -> None:
        async def body(app, pilot) -> None:
            actions = dict(app.palette_actions())
            wanted = [label for label in actions if label.startswith("Look at goal ")]
            self.assertEqual(len(wanted), len(app.real["goals"]))
            actions[wanted[1]]()  # Ctrl+K, "goal 2"
            await pilot.pause(0.2)
            self.assertEqual((app.view["goal"], app.view["step"]), (2, "plan"))
            self.assertIn("Goal 2 starts once goal 1 is accepted", str(app.query_one("#ahead-text").render()))
            await pilot.press("n")
            await pilot.pause(0.2)
            self.assertIsNone(app.view)
            app.query_one("#rail-goals").focus()  # and through the rail: Tab reaches it, arrows and Enter pick
            await pilot.press("down", "enter")
            await pilot.pause(0.2)
            self.assertEqual(app.view["goal"], 2)

        self.drive("build", body)

    def test_the_top_bar_keeps_its_gaps_when_the_token_count_grows(self) -> None:
        # v0.20 live run: at 491k tokens the middle ran into the project's name and its last word was cut
        async def body(app, pilot) -> None:
            from openfarm_core.automode import record_architect_use

            record_architect_use(app.facts["cfg"].state_dir, 480_000, 11_000)
            app._build_started = time.time() - 200
            with mock.patch.dict(app.real, {"running": True}):
                app.refresh_screen()
                await pilot.pause(0.2)
                where = app.query_one("#where")
                shown = str(where.render())
                self.assertIn("491k tokens", str(app.query_one("#health").render()))
                self.assertLessEqual(len(shown) + 2, where.size.width)
                self.assertNotIn("…", shown)  # a whole part is dropped before a word is cut
                self.assertTrue(shown.startswith("Goal 1 of 3"))

        self.drive("build", body, size=(100, 30))

    def test_try_it_shows_the_pages_colours_as_swatches(self) -> None:
        async def body(app, pilot) -> None:
            panel = app.query_one("#try-colours")
            self.assertFalse(panel.has_class("hidden"))
            text = str(panel.render())
            self.assertIn("Colours on the first screen", text)
            self.assertIn("Backgrounds", text)
            self.assertIn("#101820", text)
            self.assertIn("#3a5a78", text)
            self.assertRegex(text, r"Text\s+.*#f2efe6")

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
            self.assertIn("✓  Living Image", rail(app, "goals")[0])
            self.assertIn("Goal 2 of 3 · Recursive Threads", str(app.query_one("#where").render()))

        self.drive("accept", body)

    def test_every_earlier_step_can_be_looked_at_and_looking_changes_nothing(self) -> None:
        async def body(app, pilot) -> None:
            from textual.widgets import TextArea

            def cards_head() -> str:
                return subprocess.run(["git", "-C", str(app.repo), "rev-parse", "openfarm-cards", "HEAD"],
                                      capture_output=True, text=True).stdout

            before = cards_head()
            self.assertEqual(app.screen_name, "accept")
            seen = []
            for _back in range(6):  # Try it, Build, Plan, Spec, Questions, Idea
                await pilot.press("left_square_bracket")
                await pilot.pause(0.1)
                seen.append(app.screen_name)
                self.assertEqual(self.label(app), "▶ Back to Accept")
                self.assertEqual(self.others(app), [])
                self.assertIn("nothing changes from here", str(app.query_one("#where").render()))
            self.assertEqual(seen, ["try", "build", "plan", "spec", "questions", "idea"])
            self.assertIn("◂", rail(app, "steps")[0])
            self.assertTrue(app.query_one("#idea-text", TextArea).read_only)
            await pilot.press("right_square_bracket", "right_square_bracket", "right_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "plan")
            self.assertEqual(app.query_one("#plan-tasks").option_count, 4)
            for name in ("build", "edit_task", "change_plan", "accept_goal", "something_wrong"):
                app.action_act(name)  # every button and key ends here
                await pilot.pause(0.05)
                self.assertEqual(len(app.screen_stack), 1, name)
                self.assertIn("nothing changes from here", str(app.query_one("#busy").render()))
            await pilot.press("e", "enter")  # the edit key is refused; Enter is the way back
            await pilot.pause(0.1)
            self.assertEqual((app.screen_name, app.view), ("accept", None))
            self.assertEqual(self.label(app), "▶ Accept goal 1")
            await pilot.press("left_square_bracket", "n")
            await pilot.pause(0.1)
            self.assertEqual((app.screen_name, app.view), ("accept", None))
            await pilot.press("left_square_bracket", "escape")
            await pilot.pause(0.1)
            self.assertEqual((app.screen_name, app.view), ("accept", None))
            self.assertEqual(cards_head(), before)

        self.drive("accept", body)

    def test_the_rail_is_two_lists_that_keyboard_and_mouse_both_work(self) -> None:
        async def body(app, pilot) -> None:
            app.query_one("#rail-steps").focus()
            await pilot.press("up", "up", "enter")  # from Accept up to Build
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "build")
            self.assertEqual(app.query_one("#build-tasks").option_count, 4)
            await pilot.click("#rail-steps", offset=(4, 2))  # the third row: Spec
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "spec")
            await pilot.click("#rail-goals", offset=(4, 1))  # goal 2 has not started
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "ahead")
            self.assertIn("Goal 2 starts once goal 1 is accepted.", str(app.query_one("#ahead-text").render()))
            self.assertIn("Recursive Threads", str(app.query_one("#ahead-text").render()))
            await pilot.click("#rail-goals", offset=(4, 0))  # the goal the project is on: back to now
            await pilot.pause(0.1)
            self.assertEqual((app.screen_name, app.view), ("accept", None))

        self.drive("accept", body)

    def test_a_step_not_reached_says_what_has_to_happen_first(self) -> None:
        async def body(app, pilot) -> None:
            self.assertEqual(app.screen_name, "plan")
            for step, words in (("build", "Building starts from the Plan step"),
                                ("try", "once every task of this goal is built and accepted"),
                                ("accept", "you have said it looks right")):
                await pilot.press("right_square_bracket")
                await pilot.pause(0.1)
                self.assertEqual((app.screen_name, app.view["step"]), ("ahead", step))
                self.assertIn(words, str(app.query_one("#ahead-text").render()))
                self.assertEqual(self.label(app), "▶ Back to Plan")
            await pilot.press("n")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "plan")

        self.drive("plan", body)

    def test_an_accepted_goal_can_still_be_looked_at(self) -> None:
        async def body(app, pilot) -> None:
            await pilot.press("enter")  # accept goal 1
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "spec")
            app.look(goal=1)
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "accept")
            self.assertIn("was accepted", str(app.query_one("#accept-title").render()))
            self.assertIn("4 built and merged", str(app.query_one("#accept-text").render()))
            self.assertIn("◂", rail(app, "goals")[0])
            await pilot.press("left_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.screen_name, "try")
            self.assertIn("Here it is running", str(app.query_one("#try-title").render()))
            await pilot.press("left_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.query_one("#build-tasks").option_count, 4)
            self.assertEqual(app.facts["goal_no"], 2)  # the project's own facts are untouched
            await pilot.press("n")
            await pilot.pause(0.1)
            self.assertEqual((app.screen_name, self.label(app)), ("spec", "▶ Plan goal 2"))

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

    def test_no_older_screen_is_reachable_from_the_palette(self) -> None:
        async def body(app, pilot) -> None:
            left = []
            with mock.patch.object(app, "exit", side_effect=left.append):
                for label, action in app.palette_actions():
                    if label != "Quit" and label.split(":")[0] in ("Projects", "Providers and sign-in", "Settings and check-up",
                                                                   "Questions and notes from builders (all of them)"):
                        action()
                        await pilot.pause(0.1)
                        self.assertIn(app.screen_name, ("projects", "providers", "models", "settings", "notes"), label)
                        self.assertIn("Esc goes back to the project", str(app.query_one("#where").render()))
                        await pilot.press("escape")
                        await pilot.pause(0.1)
                        self.assertEqual(app.screen_name, "build")
            self.assertEqual(left, [])  # none of them leaves this screen for an older one
            self.assertFalse([label for label, _ in app.palette_actions() if "old" in label.lower()])

        self.drive("build", body)

    def test_projects_are_listed_added_opened_and_removed_without_touching_files(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.projects import list_projects, register_project

            register_project(app.repo, name="demo")
            app.open_more("projects")
            await pilot.pause(0.2)
            self.assertEqual(app.focused.id, "proj-list")
            self.assertIn("● demo", str(rail_text(app, "proj-list")))
            self.assertIn("open now", str(app.query_one("#proj-detail").render()))
            self.assertEqual(self.label(app), "▶ Back to it")
            other = self.base / "another one"
            app.do_project_add()
            await pilot.pause(0.2)
            app.screen.query_one("#ask-text").load_text(str(other))
            await pilot.press("ctrl+j")
            await pilot.pause(0.2)
            self.assertTrue(other.is_dir())
            self.assertEqual({p["name"] for p in list_projects()}, {"demo", "another one"})
            names = [p["id"] for p in list_projects()]
            app.query_one("#proj-list").highlighted = names.index(next(p["id"] for p in list_projects() if p["name"] == "another one"))
            await pilot.pause(0.2)
            self.assertEqual(self.label(app), "▶ Open another one")
            self.assertIn("starts at the Idea step", str(app.query_one("#proj-detail").render()))
            left = []
            with mock.patch.object(app, "exit", side_effect=left.append):
                await pilot.press("enter")
                await pilot.pause(0.1)
            self.assertEqual(left, [("project", str(other.resolve()))])
            app.do_project_remove()
            await pilot.pause(0.2)
            self.assertEqual(str(app.focused.label), "Keep it")  # Enter keeps it
            await pilot.click("#choice-remove")
            await pilot.pause(0.2)
            self.assertEqual([p["name"] for p in list_projects()], ["demo"])
            self.assertTrue(other.is_dir())  # only the list changed
            app.do_project_remove()  # the open project stays
            await pilot.pause(0.1)
            self.assertIn("Open another one first", app.note)
            self.assertEqual(len(app.screen_stack), 1)

        self.drive("build", body)

    def test_opening_another_project_starts_the_screen_on_it(self) -> None:
        import openfarm_tui

        other = self.base / "other"
        other.mkdir()
        runs = iter([("project", str(other)), None])
        made = []

        def journey(self_, repo, config_path=None, theme="", open_at=""):
            made.append((Path(repo), open_at))

        with mock.patch("openfarm_tui.journey.Journey.__init__", autospec=True, side_effect=journey, return_value=None), \
                mock.patch("openfarm_tui.journey.Journey.run", side_effect=lambda: next(runs)), \
                mock.patch("openfarm_tui.app.OpenfarmApp.__init__", side_effect=AssertionError("an older screen")):
            self.assertEqual(openfarm_tui.run_textual_tui(self.base, initial_view="providers"), 0)
        self.assertEqual(made, [(self.base.resolve(), "providers"), (other.resolve(), "")])

    def test_providers_are_shown_changed_tested_and_saved(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.providers import load_global_providers

            app.open_more("providers")
            await pilot.pause(0.3)
            self.assertIn("Claude is signed in", str(app.query_one("#prov-signin").render()))
            self.assertIn("no key saved yet", str(app.query_one("#prov-status").render()))
            app.query_one("#prov-preset").value = "custom"
            await pilot.pause(0.1)
            app.query_one("#prov-endpoint").focus()
            app.query_one("#prov-endpoint").value = ""
            await pilot.press(*"https://builders.example/v1")
            await pilot.press("tab")
            app.query_one("#prov-model").value = ""
            await pilot.press(*"nl-model")  # n, l and - are letters here, not keys of the screen
            self.assertEqual(app.screen_name, "providers")
            self.assertEqual(app.query_one("#prov-model").value, "nl-model")
            app.query_one("#prov-key").value = "sk-test-not-real"
            with mock.patch("openfarm_tui.journey_more.probe_worker_endpoint",
                            return_value={"ok": True, "latency_ms": 42, "status": 200}) as probe:
                app.do_providers_test()
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            self.assertEqual(probe.call_args.args[0], "https://builders.example/v1")
            self.assertIn("answered in 42 ms", str(app.query_one("#prov-status").render()))
            app.do_providers_save()
            await pilot.pause(0.1)
            saved = load_global_providers()
            self.assertEqual((saved["worker"]["preset"], saved["worker"]["endpoint"], saved["worker"]["model"]),
                             ("custom", "https://builders.example/v1", "nl-model"))
            self.assertTrue(saved["worker"]["key_present"])
            self.assertEqual(app.query_one("#prov-key").value, "")  # the key is not kept on screen
            self.assertNotIn("sk-test-not-real", (self.base / "providers.toml").read_text())
            self.assertIn("Saved", str(app.query_one("#prov-status").render()))
            app.query_one("#prov-endpoint").value = "not an address"
            app.do_providers_save()
            await pilot.pause(0.1)
            self.assertIn("Not saved", str(app.query_one("#prov-status").render()))
            self.assertEqual(load_global_providers()["worker"]["endpoint"], "https://builders.example/v1")

        self.drive("build", body)

    def test_a_preset_fills_in_its_own_address_and_the_sign_in_is_rechecked(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.providers import WORKER_PRESETS

            app.open_more("providers")
            await pilot.pause(0.3)
            preset = next(p for p in WORKER_PRESETS if p["endpoint"] and p["id"] != app.query_one("#prov-preset").value)
            app.query_one("#prov-preset").value = preset["id"]
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#prov-endpoint").value, preset["endpoint"])
            with mock.patch("openfarm_tui.journey_more.inspect_acp_status", return_value={"oauth_ready": False}) as status:
                app.query_one("#prov-architect").value = "codex"
                await pilot.pause(0.1)
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            self.assertEqual(status.call_args.kwargs["provider"], "codex")
            self.assertIn("Codex is not signed in", str(app.query_one("#prov-signin").render()))
            self.assertIn("Sign in…", [label for _name, label in app._actions()[1]])

        self.drive("idea", body)

    def test_models_and_effort_are_chosen_for_this_project_or_for_every_project(self) -> None:
        async def body(app, pilot) -> None:
            from textual.widgets import Select

            from openfarm_core import model_choices as mc
            from openfarm_core.config import load_config

            app.open_more("models")
            await pilot.pause(0.3)
            self.assertEqual(app.query_one("#mod-scope").value, "project")
            self.assertIn("(config.toml)", str(app.query_one("#mod-now").render()))
            app.query_one("#mod-architect-effort").value = "high"
            app.query_one("#mod-builder-model").focus()
            await pilot.pause(0.1)
            await pilot.press(*"nl-coder")  # letters here, not keys of the screen
            self.assertEqual((app.screen_name, app.focused.id, app.query_one("#mod-builder-model").value),
                             ("models", "mod-builder-model", "nl-coder"))
            await pilot.pause(0.1)
            app.do_models_save()
            await pilot.pause(0.2)
            self.assertEqual(mc.load_choices(mc.project_path(app.repo)),
                             {"architect": {"model": "", "effort": "high"}, "builder": {"model": "nl-coder", "effort": ""}})
            now = str(app.query_one("#mod-now").render())
            self.assertIn("Saved for this project", now)
            self.assertIn("nl-coder  (this project)", now)
            self.assertEqual(load_config(app.repo).role_models["implementer"].model, "nl-coder")

            app.query_one("#mod-scope").value = "global"  # the other place: its own, empty, choices
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#mod-builder-model").value, "")
            self.assertTrue(app.query_one("#mod-architect-effort", Select).is_blank())
            app.query_one("#mod-reviewer-effort").value = "low"
            app.do_models_save()
            await pilot.pause(0.2)
            self.assertEqual(mc.load_choices(mc.global_path()), {"reviewer": {"model": "", "effort": "low"}})
            self.assertIn("effort low  (every project)", str(app.query_one("#mod-now").render()))
            self.assertEqual(load_config(app.repo).micromanager.effort, "low")

            listed = {"frontier": "claude", "endpoint": "https://b.example/v1",
                      "architect": {"models": ["opus"], "efforts": []}, "reviewer": {"models": ["opus"], "efforts": []},
                      "builder": {"models": ["acme/coder-1", "acme/coder-2"], "efforts": [], "ok": True, "error": ""}}
            with mock.patch("openfarm_core.setup_helper.catalogue", return_value=listed):
                app.do_models_list()
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            self.assertIn("2 models on offer: acme/coder-1, acme/coder-2", str(app.query_one("#mod-now").render()))
            self.assertEqual(await app.query_one("#mod-builder-model").suggester.get_suggestion("acme/coder-2"[:11] + "2"),
                             "acme/coder-2")

            app.do_models_clear()
            await pilot.pause(0.2)
            self.assertEqual(mc.load_choices(mc.global_path()), {})
            self.assertEqual(mc.load_choices(mc.project_path(app.repo))["builder"]["model"], "nl-coder")  # untouched
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertEqual(app.screen_name, "build")

        self.drive("build", body)

    def test_settings_show_the_project_and_run_the_check_up(self) -> None:
        async def body(app, pilot) -> None:
            app.open_more("settings")
            await pilot.pause(0.2)
            text = str(app.query_one("#set-project").render())
            for wanted in ("Folder", "State", "Architect", "Builders", "Never touch", "Theme        dark", "Tokens"):
                self.assertIn(wanted, text)
            self.assertIn("Not run yet", str(app.query_one("#set-doctor").render()))
            self.assertEqual(self.label(app), "▶ Run the check-up")
            report = {"checks": [{"status": "ok", "label": "Git repository", "detail": "clean"},
                                 {"status": "warn", "label": "Ignore file", "detail": "state folder not ignored", "fixable": True}],
                      "fixes_applied": []}
            with mock.patch("openfarm_tui.journey_more.run_doctor", return_value=report) as doctor:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
                self.assertFalse(doctor.call_args.kwargs["fix"])
                shown = str(app.query_one("#set-doctor").render())
                self.assertIn("Git repository", shown)
                self.assertIn("can be fixed", shown)
                self.assertIn("Fix what can be fixed", [label for _name, label in app._actions()[1]])
                app.action_act("checkup_fix")
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
                self.assertTrue(doctor.call_args.kwargs["fix"])
            app.action_act("toggle_theme")
            await pilot.pause(0.1)
            self.assertIn("Theme        light", str(app.query_one("#set-project").render()))

        self.drive("build", body)

    def test_builders_notes_are_listed_and_decided_in_the_new_screen(self) -> None:
        async def body(app, pilot) -> None:
            from openfarm_core.findings import list_findings
            from openfarm_core.store import Store

            store = Store(app.repo, app.facts["cfg"])
            for number, title in ((1, "The feed has no retry"), (2, "A helper is copied in two files")):
                store.src._write(f"FIND-{number}", {"id": f"FIND-{number}", "title": title, "state": "backlog",
                                                    "category": "finding", "from_card": "W1-02"},
                                 f"More about note {number}.", f"FIND-{number}: noted")
            app.open_more("notes")
            await pilot.pause(0.2)
            self.assertEqual(app.focused.id, "notes-list")
            self.assertIn("The feed has no retry", rail_text(app, "notes-list"))
            first = str(app.query_one("#notes-detail").render())
            await pilot.press("down")
            await pilot.pause(0.2)
            second = str(app.query_one("#notes-detail").render())
            self.assertEqual({first[-18:], second[-18:]}, {"More about note 1.", "More about note 2."})
            gone = "FIND-1" if second.endswith("note 1.") else "FIND-2"
            app.action_act("note_dismiss")
            await pilot.pause(0.2)
            app.screen.query_one("#ask-text").load_text("fine as it is")
            await pilot.press("ctrl+j")
            await pilot.pause(0.2)
            waiting = [f["id"] for f in list_findings(app.repo, app.facts["cfg"], state_filter="waiting")["findings"]]
            self.assertEqual(waiting, [({"FIND-1", "FIND-2"} - {gone}).pop()])
            await pilot.press("enter")  # keep the other for the next goal
            await pilot.pause(0.2)
            self.assertEqual(list_findings(app.repo, app.facts["cfg"], state_filter="waiting")["findings"], [])
            self.assertIn("Nothing is waiting", str(app.query_one("#notes-detail").render()))

        self.drive("build", body)


def rail_text(app, list_id: str) -> str:
    widget = app.query_one(f"#{list_id}")
    return "\n".join(str(widget.get_option_at_index(i).prompt) for i in range(widget.option_count))


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

        with mock.patch("openfarm_tui.journey.ask_background", return_value=None):  # a terminal that does not say
            with mock.patch.dict(os.environ, {"COLORFGBG": "0;15"}):
                self.assertTrue(journey.terminal_is_light())
                self.assertEqual(journey.Journey(repo=Path(".")).__dict__["_theme_name"], "openfarm-light")
            with mock.patch.dict(os.environ, {"COLORFGBG": "15;0"}):
                self.assertFalse(journey.terminal_is_light())

    def test_the_terminal_is_asked_for_its_colour_so_no_variable_is_needed(self) -> None:
        from openfarm_tui import journey
        from openfarm_tui.terminal import background_is_light

        self.assertTrue(background_is_light("\033]11;rgb:ffff/ffff/ffff\033\\"))
        self.assertTrue(background_is_light("\033]11;rgb:fa/f4/e8\007"))
        self.assertFalse(background_is_light("\033]11;rgb:1e1e/1e1e/2e2e\033\\"))
        self.assertIsNone(background_is_light("\033[?62;22c"))
        env = {k: v for k, v in os.environ.items() if k != "COLORFGBG"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch("openfarm_tui.journey.ask_background", return_value=True):
                self.assertEqual(journey.Journey(repo=Path(".")).__dict__["_theme_name"], "openfarm-light")
            with mock.patch("openfarm_tui.journey.ask_background", return_value=False):
                self.assertEqual(journey.Journey(repo=Path(".")).__dict__["_theme_name"], "openfarm-dark")
            with mock.patch("openfarm_tui.journey.ask_background", return_value=False), \
                    mock.patch.dict(os.environ, {"COLORFGBG": "0;15"}):
                self.assertFalse(journey.terminal_is_light())  # what the terminal says wins over the variable

    def test_asking_a_terminal_reads_its_answer_and_leaves_it_as_it_was(self) -> None:
        import pty
        import termios
        import threading

        from openfarm_tui.terminal import ask_background

        def terminal(answer: bytes):
            ours, theirs = pty.openpty()
            asked = []

            def play() -> None:
                asked.append(os.read(ours, 64))
                os.write(ours, answer)

            thread = threading.Thread(target=play, daemon=True)
            thread.start()
            before = termios.tcgetattr(theirs)
            try:
                return ask_background(theirs, wait_s=5), asked, before == termios.tcgetattr(theirs), thread
            finally:
                thread.join(5)
                os.close(ours)
                os.close(theirs)

        light, asked, restored, _ = terminal(b"\033]11;rgb:ffff/fafa/f0f0\033\\\033[?62;22c")
        self.assertIs(light, True)
        self.assertEqual(asked, [b"\033]11;?\033\\\033[c"])
        self.assertTrue(restored)
        dark, _, restored, _ = terminal(b"\033]11;rgb:0000/0000/0000\007\033[?1;2c")
        self.assertIs(dark, False)
        started = time.monotonic()
        unknown, _, restored, _ = terminal(b"\033[?1;2c")  # knows what it is, not its colour: no waiting
        self.assertIsNone(unknown)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(restored)


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
            if screen == "build":  # and the journey walked: an earlier step, and a goal not reached yet
                for name, where in (("back", {"step": "plan"}), ("ahead", {"goal": 2})):
                    app.look(**where)
                    await pilot.pause(0.3)
                    app.save_screenshot(f"{name}-{theme.split('-')[1]}.svg", path=str(out))

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
