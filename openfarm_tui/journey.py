"""The openfarm screen: a journey rail on the left, the one thing in front of you on the right.

Which pane is shown comes from the project's state (`journey_state.screen_for`),
never from the last thing clicked. Each pane has one main action, always the
last button, and Enter does it.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, ContentSwitcher, Input, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

import openfarm_core
from openfarm_core.automode import (
    architect_grill_turn,
    draft_spec_and_chunks,
    draft_wave_plan,
    ensure_baseline_committed,
    new_feature,
    next_goal,
    open_architect_backend,
    record_architect_use,
    preflight_plan,
    something_wrong,
)
from openfarm_core.blocked import apply_fix_and_retry, propose_fix, run_check, settle_question
from openfarm_core.bootstrap import initialize_project
from openfarm_core.config import CONFIG_PATH, load_config
from openfarm_core.daemon import daemon_status, read_daemon_logs, start_daemon
from openfarm_core.findings import decide_finding
from openfarm_core.state import StateDir
from openfarm_core.store import card_from, open_store, read_doc, write_doc
from openfarm_core.tryit import fix_request, looks_right, try_it

from .journey_more import MORE, MORE_LABELS, MorePanes
from .terminal import ask_background
from .journey_state import (
    PROJECT_STEPS,
    STEP_OF,
    STEPS,
    gather,
    load_session,
    looking_at,
    reached,
    save_session,
    screen_for,
    session_path,
)
from .markup import esc

THEMES = [
    Theme(name="openfarm-dark", dark=True, primary="#9BCB8F", secondary="#86AECB",
          accent="#E9B95F", warning="#E9B95F", error="#EE7B6C", success="#9BCB8F",
          foreground="#E8E4D9", background="#131512", surface="#1A1D19", panel="#252A24"),
    Theme(name="openfarm-light", dark=False, primary="#2F6B2B", secondary="#2C5F86",
          accent="#8A5A00", warning="#8A5A00", error="#A93226", success="#2F6B2B",
          foreground="#1D201B", background="#F5F1E6", surface="#FFFCF4", panel="#E9E3D3"),
]
STACKS = [("Work it out from the idea", "auto"), ("TypeScript / Web", "typescript"), ("Python", "python"),
          ("Rust", "rust"), ("Go", "go")]
CHECKS = [  # the plan checks, in the words the Plan screen uses
    ("scaffold", "Every function in the specs can be scaffolded"),
    ("touches", "Every file a task names is one it may touch"),
    ("depends", "The order works: nothing waits on a task that comes later"),
    ("seams", "Shared pieces agree: files that must match name the same things"),
    ("network", "A task that needs the network says so"),
    ("guards", "Every test a task lists is new, so it can fail before the code is written"),
    ("frozen", "No task changes what an existing test checks without leave to change that test"),
    ("rules", "Every task is small and exact enough to build"),
]
STOPPED = {"task": "the task is wrong, not the code", "builder": "the builder fell short, the task is sound",
           "check": "a check may be wrong"}
PHASES = {  # what the build reports -> what it means for the reader
    "claim": "starting", "worktree": "setting up its own copy of the code", "baseline": "checking the starting point",
    "plan": "reading the task", "test-writer": "writing the tests", "tests": "writing the tests",
    "prove": "proving the tests fail without the code", "implement": "writing code", "code": "writing code",
    "check": "running the checks", "gates": "running the checks", "checker": "being reviewed", "review": "being reviewed",
    "pre-review": "being reviewed", "catch-up": "catching up with main", "finish": "finishing",
}


def terminal_is_light() -> bool:
    """Follow the terminal: ask it for its background colour, and where it does not say, read `COLORFGBG`."""
    asked = ask_background() if sys.stdin.isatty() and sys.stdout.isatty() else None
    if asked is not None:
        return asked
    last = os.environ.get("COLORFGBG", "").rpartition(";")[2]
    return last.isdigit() and int(last) in (7, 15)


class AskModal(ModalScreen[str | None]):
    """One question to the user, answered in a text box. Ctrl+Enter sends, Esc cancels."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("ctrl+j,ctrl+enter", "send", "Send", priority=True)]
    DEFAULT_CSS = """
    AskModal { align: center middle; }
    AskModal > Vertical { width: 76; max-width: 96%; height: auto; background: $surface; border: round $primary; padding: 1 2; }
    AskModal TextArea { height: 7; margin: 1 0; }
    AskModal .hint { height: auto; max-height: 12; }
    AskModal .keys { color: $foreground-muted; }
    """

    def __init__(self, title: str, hint: str = "", text: str = "") -> None:
        super().__init__()
        self._title, self._hint, self._text = title, hint, text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{esc(self._title)}[/]")
            if self._hint:
                with VerticalScroll(classes="hint"):  # a builder's whole question fits here, scrolled if long
                    yield Static(esc(self._hint), classes="keys")
            yield TextArea(self._text, id="ask-text", soft_wrap=True)
            yield Static("Ctrl+Enter send · Esc cancel", classes="keys")

    def on_mount(self) -> None:
        self.query_one("#ask-text").focus()

    def action_send(self) -> None:
        self.dismiss(self.query_one("#ask-text", TextArea).text.strip() or None)


class EditModal(ModalScreen[str | None]):
    """The built-in full-height editor, used when `$EDITOR` is not set."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("ctrl+s", "save", "Save", priority=True)]
    DEFAULT_CSS = """
    EditModal > Vertical { background: $surface; padding: 0 1; }
    EditModal TextArea { height: 1fr; }
    EditModal .keys { height: 1; color: $foreground-muted; }
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title, self._text = title, text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{esc(self._title)}[/]", classes="keys")
            yield TextArea(self._text, id="edit-text", soft_wrap=True, show_line_numbers=True)
            yield Static("Ctrl+S save · Esc cancel", classes="keys")

    def on_mount(self) -> None:
        self.query_one("#edit-text").focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#edit-text", TextArea).text)


class ChoiceModal(ModalScreen[str | None]):
    """A question with two or three answers, each a button. Used for the one-time move of tasks off main."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Not now")]
    DEFAULT_CSS = """
    ChoiceModal { align: center middle; }
    ChoiceModal > Vertical { width: 110; max-width: 96%; height: auto; max-height: 90%; background: $surface;
                             border: round $primary; padding: 1 2; }
    ChoiceModal VerticalScroll { height: auto; max-height: 22; margin: 1 0; }
    ChoiceModal Horizontal { height: 3; align: right middle; }
    ChoiceModal Button { margin-left: 1; }
    """

    def __init__(self, title: str, text: str, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title, self._text, self._choices = title, text, choices

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{esc(self._title)}[/]")
            with VerticalScroll():
                yield Static(esc(self._text))
            with Horizontal():
                for index, (value, label) in enumerate(self._choices):
                    yield Button(label, id=f"choice-{value}", variant="primary" if index == len(self._choices) - 1 else "default")

    def on_mount(self) -> None:
        self.query(Button).last().focus()

    @on(Button.Pressed)
    def _chosen(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(str(event.button.id)[7:])


class Everything(Provider):
    """Ctrl+K: what is not on the screen in front of you."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, action in self.app.palette_actions():
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), action)

    async def discover(self) -> Hits:
        for label, action in self.app.palette_actions():
            yield Hit(1, label, action)


STEP_NAMES = list(STEP_OF)  # the steps in order, by their pane's name
LOOK_ONLY = {"now", "back", "details", "open_task", "shot_desktop", "shot_phone", "full_diff", "tries", "cancel"}


class Rail(Vertical):
    """The steps and the goals. Both are lists: Enter or a click looks at one."""

    def compose(self) -> ComposeResult:
        yield Static("JOURNEY", classes="rail-head")
        yield OptionList(id="rail-steps")
        yield Static("GOALS", classes="rail-head gap")
        yield OptionList(id="rail-goals")
        yield Static("none yet", id="goals-none")
        self._drawn: dict[str, tuple] = {}

    def _fill(self, list_id: str, rows: list[tuple[str, str]], at: int | None) -> None:
        key = (tuple(rows), at)
        if self._drawn.get(list_id) == key:
            return
        self._drawn[list_id] = key
        widget = self.query_one(f"#{list_id}", OptionList)
        widget.clear_options()
        widget.add_options([Option(prompt, id=option_id) for option_id, prompt in rows])
        widget.set_class(not rows, "hidden")
        if rows and at is not None:
            widget.highlighted = at

    def show(self, step: int, flag: bool, goals: list[dict], current: int, open_steps: dict[str, str],
             look_step: int | None = None, look_goal: int = 0) -> None:
        rows = []
        for index, name in enumerate(STEPS):
            glyph, colour = ("✓", "$success") if index < step else ("●", "b $accent") if index == step else (
                "○", "$foreground-muted")
            if index > step and not open_steps.get(STEP_NAMES[index]):
                glyph, colour = "◌", "$foreground"  # ahead of where the project is, but there is something to see
            if index == step and flag:
                glyph, name, colour = "!", f"{name} · needs you", "b $error"
            here = "  ◂" if index == look_step else ""
            rows.append((f"step-{index}", f"[{colour}]{glyph}  {esc(name)}[/]{here}"))
        self._fill("rail-steps", rows, step if look_step is None else look_step)
        rows = []
        for number, goal in enumerate(goals, start=1):
            mark = "✓" if goal["status"] == "done" else str(number)
            text = f"{mark}  {goal['title']}"
            text = esc(text if len(text) <= 19 else text[:18] + "…")
            text = f"[b $foreground]{text}[/]" if number == current else f"[$foreground-muted]{text}[/]"
            rows.append((f"goal-{number}", text + ("  ◂" if number == look_goal else "")))
        self._fill("rail-goals", rows, (look_goal or current or 1) - 1 if goals else None)
        self.query_one("#goals-none", Static).set_class(bool(goals), "hidden")


class Journey(MorePanes, App):
    TITLE = "openfarm"
    COMMANDS = {Everything}
    CSS = """
    Screen { layout: vertical; background: $background; }
    #top { height: 1; background: $panel; padding: 0 1; }
    #brand { width: auto; text-style: bold; color: $primary; }
    #where { width: 1fr; content-align: center middle; color: $foreground-muted; }
    #health { width: auto; color: $foreground-muted; }
    #body { height: 1fr; }
    Rail { width: 26; padding: 1 1 0 2; background: $surface; border-right: tall $panel; }
    .rail-head { color: $foreground-muted; text-style: bold; margin-bottom: 1; }
    .rail-head.gap { margin-top: 2; }
    Rail OptionList { border: none; background: transparent; margin-bottom: 0; padding: 0; max-height: 9; }
    Rail OptionList:focus { border: none; background: $accent 10%; }
    #goals-none { color: $foreground-muted; }
    #main { width: 1fr; }
    #panes { height: 1fr; padding: 1 3 0 3; }
    #panes > VerticalScroll { height: 1fr; scrollbar-size-vertical: 1; }
    .h1 { text-style: bold; margin-bottom: 1; }
    .sub { color: $foreground-muted; margin-bottom: 1; }
    .label { color: $foreground-muted; text-style: bold; }
    #idea-text { height: 6; border: tall $primary; }
    #stack { width: 36; margin-bottom: 1; }
    OptionList { height: auto; max-height: 10; border: tall $panel; background: $surface; margin-bottom: 1; }
    OptionList:focus { border: tall $primary; }
    .card { border: round $panel; padding: 0 1; margin-bottom: 1; height: auto; }
    .card.ok { border: round $success 60%; }
    .card.bad { border: round $error; }
    .card.ask { border: round $accent; }
    .diff { background: $surface; padding: 0 1; margin-bottom: 1; height: auto; }
    .hidden { display: none; }
    #busy { height: 1; padding: 0 3; color: $accent; }
    #actions { height: 3; padding: 0 3; align: right middle; background: $background; }
    #actions Button { margin-left: 1; min-width: 6; border: none; height: 3; background: $panel; }
    #actions Button:focus { text-style: bold; background-tint: transparent; }
    #actions Button.-primary { background: $primary; color: $background; text-style: bold; }
    #actions Button.-primary:focus { background: $primary; color: $background; text-style: bold underline; }
    #activity { height: 4; border-top: tall $panel; background: $surface; padding: 0 2; color: $foreground-muted; }
    #keys { height: 1; background: $panel; padding: 0 1; color: $foreground-muted; }
    #board-cols { height: auto; }
    .col { width: 1fr; height: auto; margin-right: 1; }
    .colhead { text-style: bold; color: $foreground-muted; border-bottom: solid $panel; }
    .col OptionList { max-height: 18; }
    """
    BINDINGS = [
        Binding("ctrl+k", "command_palette", "Everything else", priority=True),
        Binding("ctrl+j,ctrl+enter", "go", "Do it", priority=True),
        Binding("enter", "go", "Do it"),
        Binding("escape", "back", "Back"),
        Binding("n", "now", "Where the project is"),
        Binding("left_square_bracket", "step(-1)", "Earlier step"),
        Binding("right_square_bracket", "step(1)", "Later step"),
        Binding("b", "board", "Board"),
        Binding("l", "log", "Log"),
        Binding("space", "details", "Details"),
        Binding("e", "act('edit_task')", "Edit"),
        Binding("d", "act('full_diff')", "Diff"),
        Binding("t", "act('tries')", "Tries"),
        Binding("o", "act('shot_desktop')", "Desktop shot"),
        Binding("p", "act('shot_phone')", "Phone shot"),
        Binding("w", "act('open_browser')", "Browser"),
        Binding("left", "column(-1)", "Column", show=False),
        Binding("right", "column(1)", "Column", show=False),
        Binding("f9", "toggle_theme", "Theme"),
    ]

    def __init__(self, repo: Path, config_path: Path | None = None, theme: str = "", open_at: str = "") -> None:
        super().__init__()
        self._open_at = open_at if open_at in MORE else ""  # a pane beside the journey to start on
        self._doctor: dict | None = None  # the last check-up, for Settings
        self._prov_note, self._prov_preset = "", ""
        self._signin: tuple[str, bool] | None = None  # (provider, signed in) once it has been checked
        self.repo = Path(repo)
        self.config_path = config_path
        self._theme_name = theme or ("openfarm-light" if terminal_is_light() else "openfarm-dark")
        self.facts: dict = {}
        self.screen_name = ""
        self.overlay = ""  # "", "blocked", "details", "board" or "log": a look aside, Esc returns
        self.view: dict | None = None  # {"step", "goal", "why"}: a step the user went to look at; nothing changes from there
        self.real: dict = {}  # the project's facts; `facts` is what the drawn step shows (another goal's, when looking)
        self.now = "idea"  # the step the project is at
        self.task_id = ""
        self.busy = ""
        self.busy_since = 0.0
        self.failure: dict | None = None  # the Architect step that failed, for Retry
        self.activity: list[str] = []
        self.health = {"Architect": None, "Builders": None}
        self.tokens = {"worker": 0, "frontier": 0}
        self.phases: dict[str, str] = {}
        self.fix: dict | None = None
        self.check_output: dict | None = None
        self.note = ""  # one line under the heading: what just happened on this screen
        self._backend = None
        self._cancelled = False
        self._following = False
        self._actions_shown: tuple = ()
        self._lists: dict[str, tuple] = {}
        self._app_server: subprocess.Popen | None = None
        self._app_url = ""
        self._last_step: tuple = ()
        self._template = False
        self._build_started = 0.0  # when the build being shown began, for the top bar
        self._slots: dict[str, str] = {}  # button id -> the action it does now
        self._more: list[tuple] = []  # actions of this screen that had no room for a button
        self.shown: dict = {}  # the facts the step on screen was drawn from
        self._health_len = 26
        self.share = ""  # who spent the tokens, in words; the top bar's tooltip and the log say it
        self._unrecorded = 0  # Architect tokens spent before the folder was a project, so not in its ledger yet
        self._sorting: set[str] = set()  # builders' questions being sorted, or sorted, in this window
        self._auto_tried: set[str] = set()  # goals tried without being asked, so a try that can't be saved isn't repeated

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("openfarm", id="brand")
            yield Static("", id="where")
            yield Static("", id="health")
        with Horizontal(id="body"):
            yield Rail()
            with Vertical(id="main"):
                with ContentSwitcher(initial="idea", id="panes"):
                    yield from self._panes()
                yield Static("", id="busy")
                with Horizontal(id="actions"):
                    for index in range(4):
                        yield Button("", id=f"act-{index}")
                    yield Button("", variant="primary", id="go")
        yield Static("", id="activity")
        yield Static("", id="keys")

    def _panes(self) -> ComposeResult:
        with VerticalScroll(id="idea"):
            yield Static("What do you want to build?", classes="h1")
            yield Static("A sentence is enough. A full spec works too.", classes="sub")
            yield TextArea("", id="idea-text", soft_wrap=True)
            yield Static("Stack", classes="label")
            yield Select(STACKS, value="auto", allow_blank=False, id="stack")
            yield Static("", id="idea-next", classes="sub")
        with VerticalScroll(id="questions"):
            yield Static("", id="q-progress", classes="sub")
            yield Static("", id="q-title", classes="h1")
            yield Static("", id="q-why", classes="sub")
            yield OptionList(id="q-options")
            yield Static("Or say it in your own words", classes="label")
            yield Input(placeholder="type an answer, Enter sends it", id="q-own")
            yield Static("Answered so far", classes="label")
            yield OptionList(id="q-answered")
        with VerticalScroll(id="spec"):
            yield Static("", id="spec-title", classes="h1")
            yield Static("", id="spec-goals", classes="card")
            yield Static("", id="spec-text")
        with VerticalScroll(id="plan"):
            yield Static("", id="plan-title", classes="h1")
            yield Static("", id="plan-sub", classes="sub")
            yield OptionList(id="plan-tasks")
            yield Static("", id="plan-checks", classes="card ok")
        with VerticalScroll(id="build"):
            yield Static("", id="build-title", classes="h1")
            yield OptionList(id="build-tasks")
            yield Static("", id="build-question", classes="card ask")
            yield Static("", id="build-ready", classes="card ok")
        with VerticalScroll(id="blocked"):
            yield Static("", id="blocked-title", classes="h1")
            yield Static("", id="blocked-why", classes="sub")
            yield Static("", id="blocked-fix", classes="diff")
            yield Static("", id="blocked-tries", classes="card bad")
        with VerticalScroll(id="try"):
            yield Static("", id="try-title", classes="h1")
            yield Static("", id="try-sub", classes="sub")
            yield Static("", id="try-checks", classes="card")
            yield Static("", id="try-colours", classes="card")
            yield Static("", id="try-shots")
        with VerticalScroll(id="accept"):
            yield Static("", id="accept-title", classes="h1")
            yield Static("", id="accept-text")
        with VerticalScroll(id="ahead"):
            yield Static("", id="ahead-title", classes="h1")
            yield Static("", id="ahead-text")
        with VerticalScroll(id="details"):
            yield Static("", id="details-text")
        with VerticalScroll(id="board"):
            yield Static("All tasks", classes="h1")
            with Horizontal(id="board-cols"):
                for key, head in (("waiting", "WAITING"), ("building", "BUILDING"), ("needs", "NEEDS YOU"), ("done", "DONE")):
                    with Vertical(classes="col"):
                        yield Static(head, id=f"head-{key}", classes="colhead")
                        yield OptionList(id=f"col-{key}")
            yield Static("", id="board-note", classes="sub")
        with VerticalScroll(id="log"):
            yield Static("Everything that happened", classes="h1")
            yield Static("", id="log-text")
        yield from self.compose_more()

    def on_resize(self) -> None:
        if self.screen_name:
            self.refresh_screen()

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = self._theme_name
        try:
            self.query_one("#idea-text", TextArea).load_text(session_path(self.repo).with_suffix(".txt").read_text(encoding="utf-8"))
        except OSError:
            pass
        self.query_one("#stack", Select).value = load_session(self.repo)["stack"]
        self._unrecorded = sum(int(a) + int(b) for a, b in load_session(self.repo)["spent"])
        self._load_last_run()
        self.refresh_screen(focus=True)
        self.set_interval(1.0, self._tick)
        self._attach_running_build()
        if self._open_at:
            self.call_after_refresh(lambda: self.open_more(self._open_at))
        try:
            if self.facts["cfg"] is not None and open_store(self.repo, self.facts["cfg"]).migration()["needed"]:
                self.call_after_refresh(self.offer_migration)
        except Exception:  # noqa: BLE001 - not a repository yet
            pass

    # -- an older project: its tasks move off main once, and only when told to -----

    def offer_migration(self) -> None:
        cfg = self._cfg()
        if cfg is None:
            return
        try:
            store = open_store(self.repo, cfg)
            status = store.migration()
        except Exception:  # noqa: BLE001 - not a repository yet, or nothing to move
            return
        if not status["needed"]:
            self.note = "This project's tasks already live in one place."
            return self._draw_busy()
        take: dict[str, str] = {}
        waiting = list(status["differences"])

        def ask_next(choice: str | None = "start") -> None:
            if choice is None:
                return self.say("The tasks stay on main for now (Ctrl+K: move them)")
            if choice in ("main", "state"):
                take[waiting.pop(0)["id"]] = choice
            if waiting:
                diff = waiting[0]
                return self.push_screen(ChoiceModal(
                    f"{diff['id']} is different in the two places. Which one is right?",
                    f"ON MAIN\n{diff['main']}\n\nON THE CARDS BRANCH\n{diff['state']}",
                    [("state", "Keep the cards branch's"), ("main", "Keep main's")]), ask_next)
            try:
                store.migrate(take)
                self.say(f"{status['cards']} task(s) and the spec now live on the cards branch; main's cards/ was removed in one commit")
            except Exception as exc:  # noqa: BLE001 - shown, and nothing was moved
                self.say(f"The tasks could not be moved: {getattr(exc, 'message', exc)}")
            self.refresh_screen(focus=True)

        self.push_screen(ChoiceModal(
            "This project keeps its tasks and spec on the main branch",
            f"openfarm now keeps them in one place, the cards branch, so main holds only your code. Moving them is done "
            f"once: {status['cards']} task(s) are compared with the cards branch"
            + (f", {len(waiting)} differ and you choose which copy to keep" if waiting else ", none differ")
            + ", then main's cards/ folder and spec files are removed in one commit. Nothing is pushed.",
            [("later", "Not now"), ("start", "Move them")]), lambda choice: ask_next(None if choice in (None, "later") else "start"))

    # -- small helpers ----------------------------------------------------------

    def _cfg(self):
        try:
            return load_config(self.repo, self.config_path) if self.config_path else load_config(self.repo)
        except Exception:  # noqa: BLE001 - not a project yet
            return None

    def _idea(self) -> str:
        return self.query_one("#idea-text", TextArea).text.strip()

    def _session(self) -> dict:
        return load_session(self.repo)

    def say(self, text: str) -> None:
        """Add a line to the activity strip and the log."""
        self.activity.append(f"{time.strftime('%H:%M')}  {text}")
        self._render_activity()

    def _render_activity(self) -> None:
        if self._gone():
            return
        self.query_one("#activity", Static).update("\n".join(esc(line) for line in self.activity[-3:]))
        if self.overlay == "log":
            self._draw_log()

    def _set_list(self, list_id: str, rows: list[tuple[str, str]]) -> None:
        """Fill an option list, keeping the highlighted row when the rows did not change."""
        key = tuple(rows)
        if self._lists.get(list_id) == key:
            return
        self._lists[list_id] = key
        widget = self.query_one(f"#{list_id}", OptionList)
        at = widget.highlighted
        widget.clear_options()
        widget.add_options([Option(prompt, id=option_id or None, disabled=not option_id) for option_id, prompt in rows])
        widget.set_class(not rows, "hidden")
        usable = [index for index, (option_id, _prompt) in enumerate(rows) if option_id]  # a row with no id is a heading
        if usable:
            widget.highlighted = min(usable, key=lambda index: abs(index - (at or 0)))

    def _picked(self, list_id: str) -> str:
        widget = self.query_one(f"#{list_id}", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return ""
        return str(widget.get_option_at_index(widget.highlighted).id or "")

    def _find(self, task_id: str = "") -> dict | None:
        wanted = task_id or self.task_id
        return next((t for t in self.facts.get("all_tasks") or [] if t["id"] == wanted), None)

    def _selected_task(self) -> dict | None:
        if self.overlay in ("blocked", "details"):
            return self._find()
        if self.overlay == "board":
            focused = self.focused
            if isinstance(focused, OptionList) and (focused.id or "").startswith("col-"):
                return self._find(self._picked(focused.id))
            return None
        if self.screen_name in ("plan", "build"):
            return self._find(self._picked(f"{self.screen_name}-tasks"))
        return None

    def _row(self, glyph: str, colour: str, task: dict, words: str) -> str:
        """One task line that fits the pane: the title gives way first, then the words."""
        room = max(30, self.size.width - 26 - 6 - 8) - len(task["id"]) - 6
        words = words if len(words) <= room - 14 else words[: room - 15] + "…"
        title = task["title"] if len(task["title"]) <= room - len(words) else task["title"][: max(0, room - len(words) - 1)] + "…"
        return (f"[{colour}]{glyph}[/]  [b]{esc(task['id'])}[/]  {esc(title)}{' ' * (room - len(title) - len(words))} "
                f"[{colour}]{esc(words)}[/]")

    def _first(self, state: str) -> dict | None:
        return next((t for t in self.facts.get("tasks") or [] if t["state"] == state), None)

    # -- which screen, and drawing it --------------------------------------------

    def _gone(self) -> bool:
        """A timer or a finished thread can call in after the window has gone: there is nothing to draw on."""
        return not self._screen_stack or self._exit or self._closing or self._closed

    def refresh_screen(self, focus: bool = False) -> None:
        if self._gone():
            return
        self.real = self.facts = gather(self.repo, self.config_path)
        picked = screen_for(self.facts)
        self.now = picked["screen"]
        if self.overlay in ("blocked", "details") and self._find() is None:
            self.overlay = ""
        view = self.view
        if view:
            view["why"] = reached(self.real, view["goal"])[view["step"]]
            if view["step"] == self.now and (view["step"] in PROJECT_STEPS or view["goal"] == self.real["goal_no"]):
                view = self.view = None  # the project is where the user was looking
        self._lock(bool(view))
        name = self.overlay or ((("ahead" if view["why"] else view["step"]) if view else picked["screen"]))
        changed = name != self.screen_name
        self.screen_name = name
        if changed:
            self.note = ""
        facts = self.facts
        self.query_one("#brand", Static).update(esc(f"openfarm · {facts['name']}"))
        self.query_one("#panes", ContentSwitcher).current = name
        self.query_one(Rail).show(STEP_OF[picked["screen"]], picked["flag"], facts["goals"], facts["goal_no"],
                                  reached(facts), STEP_OF[view["step"]] if view else None,
                                  view["goal"] if view and view["step"] not in PROJECT_STEPS else 0)
        if view and view["step"] not in PROJECT_STEPS:
            self.facts = looking_at(self.real, view["goal"])  # drawn from that goal's tasks; put back below
        try:
            getattr(self, f"_draw_{name}")()
            primary, secondary, keys = self._actions()
        finally:
            self.shown, self.facts = self.facts, self.real
        where = self._where(picked)
        self._draw_health()  # first: how much room the middle has depends on how long the token count is
        room = self.size.width - len(f"openfarm · {facts['name']}") - self._health_len - 8
        while len(where) > room and where.count(" · ") > 1:  # the last part (minutes, then the count) goes before a word is cut
            where = where.rpartition(" · ")[0]
        self.query_one("#where", Static).update(esc(where if len(where) <= room else where[: max(8, room - 1)] + "…"))
        line = f"{keys} · Ctrl+K everything else"
        if not view and not self.overlay and len(line) + 12 <= self.size.width - 2:  # said where there is room; Ctrl+K has it too
            line = f"{keys} · [ ] steps · Ctrl+K everything else"
        self.query_one("#keys", Static).update(esc(line))
        self._draw_busy()
        self._show_actions(primary, secondary)
        self._render_activity()
        fresh = [f for f in self.real.get("unsorted") or [] if f["id"] not in self._sorting]
        if fresh:
            self._sorting.update(f["id"] for f in fresh)
            self._sort_questions(fresh)
        if changed or focus:
            self.call_after_refresh(self._focus_for_screen)
        if name == "build" and not changed:
            self.call_after_refresh(self._follow_needs)
        if changed:
            self.call_after_refresh(lambda: self.query_one(f"#{name}").scroll_home(animate=False))

    def _lock(self, looking: bool) -> None:
        """Looking at an earlier step: its fields can be read, not typed in."""
        self.query_one("#idea-text", TextArea).read_only = looking
        self.query_one("#stack", Select).disabled = looking
        self.query_one("#q-own", Input).disabled = looking

    def look(self, step: str = "", goal: int = 0) -> None:
        """Show a step of a goal as it stands. Looking changes nothing; `n` or Esc returns."""
        if self.busy or self.failure or len(self.screen_stack) > 1:
            return
        current = int(self.real.get("goal_no") or 0)
        was = self.view or {}
        if not step:  # a goal was picked: where that goal is
            done = 0 < goal <= len(self.real["goals"]) and self.real["goals"][goal - 1]["status"] == "done"
            step = self.now if goal == current else "accept" if done else "plan"
        goal = goal or was.get("goal") or current
        self.view = {"step": step, "goal": goal, "why": ""}
        self.overlay = ""
        self.refresh_screen(focus=True)

    def action_step(self, by: int) -> None:
        at = STEP_OF[(self.view or {}).get("step") or self.now] + by
        if 0 <= at < len(STEP_NAMES):
            self.look(STEP_NAMES[at])

    def action_now(self) -> None:
        if self.view or self.overlay:
            self.view, self.overlay = None, ""
            self.refresh_screen(focus=True)

    def do_now(self) -> None:
        self.action_now()

    @on(OptionList.OptionSelected, "#rail-steps")
    def _rail_step(self, event: OptionList.OptionSelected) -> None:
        self.look(STEP_NAMES[int(str(event.option.id).split("-")[1])])

    @on(OptionList.OptionSelected, "#rail-goals")
    def _rail_goal(self, event: OptionList.OptionSelected) -> None:
        self.look(goal=int(str(event.option.id).split("-")[1]))

    def _draw_ahead(self) -> None:
        view = self.view or {}
        name = STEPS[STEP_OF[view["step"]]]
        goals, number = self.real["goals"], view["goal"]
        ahead = view["step"] not in PROJECT_STEPS and number != self.real["goal_no"] and 0 < number <= len(goals)
        self.query_one("#ahead-title", Static).update(esc(f"{name}: not yet" + (f" for goal {number}" if ahead else "")))
        lines = [esc(view["why"])]
        if ahead:
            lines += ["", f"[b]{number}  {esc(goals[number - 1]['title'])}[/]", esc(goals[number - 1].get("goal") or "")]
        self.query_one("#ahead-text", Static).update("\n".join(lines))

    def _where(self, picked: dict) -> str:
        facts = self.real
        if self.view and self.overlay != "board":
            step = STEPS[STEP_OF[self.view["step"]]]
            of_goal = "" if self.view["step"] in PROJECT_STEPS or not self.view["goal"] else f" of goal {self.view['goal']}"
            return f"Looking at {step}{of_goal} · nothing changes from here"
        if self.overlay == "board":
            return "Board · all goals"
        if self.overlay in MORE:
            return f"{MORE[self.overlay]} · Esc goes back to the project"
        if not facts["spec"]:
            history, pending = facts["session"]["history"], facts["session"]["pending"]
            return f"Questions · {len(history) + 1}" if (pending and picked["screen"] == "questions") else "new project"
        if facts["goal"] is None:
            return f"All {len(facts['goals'])} goals accepted"
        text = f"Goal {facts['goal_no']} of {len(facts['goals'])} · {facts['goal']['title']}"
        needing = sum(1 for t in facts["tasks"] if t["state"] in ("blocked", "in_review")) + len(facts["questions"])
        if needing:
            text += f" · {needing} need{'s' if needing == 1 else ''} you"
        elif facts["running"]:
            text += " · building"
        if facts["running"] and self._build_started:
            text += f" · {max(1, int((time.time() - self._build_started) // 60))} min"
        return text

    def _draw_health(self) -> None:
        marks = {True: "[$success]●[/]", False: "[$error]✗[/]", None: "[$foreground-muted]○[/]"}
        spent = dict(self.real.get("tokens") or {})
        # a running build's own count is ahead of the records, which are written as each task ends
        spent["builders"] = max(int(spent.get("builders") or 0), self.tokens["worker"])
        spent["reviewers"] = max(int(spent.get("reviewers") or 0), self.tokens["frontier"])
        spent["architect"] = int(spent.get("architect") or 0) + self._unrecorded
        total = spent["builders"] + spent["reviewers"] + spent["architect"]
        tokens = f"   {total // 1000}k tokens" if total >= 1000 else (f"   {total} tokens" if total else "")
        health = self.query_one("#health", Static)
        health.update("   ".join(f"{marks[ok]} {name}" for name, ok in self.health.items()) + tokens)
        self._health_len = len("   ".join(f"● {name}" for name in self.health) + tokens)
        self.share = (" · ".join(f"{name} {spent[key]:,} tokens ({100 * spent[key] // total}%)" for key, name in (
            ("builders", "builders"), ("reviewers", "reviewers"), ("architect", "Architect")))
                      if total else "no tokens spent on this project yet")
        share = self.share
        words = {True: "answered last time it was asked", False: "did not answer last time", None: "not asked yet"}
        health.tooltip = share + "\n" + "\n".join(f"{name}: {words[ok]}" for name, ok in self.health.items())

    def _draw_busy(self) -> None:
        text = ""
        if self.busy:
            text = f"◌ {self.busy} · {int(time.monotonic() - self.busy_since)}s"
        elif self.failure:
            text = f"[$error]✗ {esc(self.failure['reason'])}[/]"
        elif self.note:
            text = esc(self.note)
        self.query_one("#busy", Static).update(text)

    def _tick(self) -> None:
        if self._gone():
            return
        if self.busy:
            self._draw_busy()
        elif len(self.screen_stack) == 1 and self.facts.get("cfg") is not None:
            try:
                self.refresh_screen()  # picks up what a running build did
            except NoMatches:  # the window is being taken down under this tick: nobody is left to show it to
                pass

    def _show_actions(self, primary: tuple | None, secondary: list[tuple]) -> None:
        # Nothing is cut off: what doesn't fit beside the main button stays on its key and under Ctrl+K.
        room = self.size.width - 26 - 6 - (len(primary[1]) + 7 if primary else 0)
        self._more = []
        while secondary and sum(len(label) + 5 for _name, label in secondary) > room:
            self._more.append(secondary.pop())
        shown = (primary, tuple(secondary), bool(self.busy))
        if shown == self._actions_shown:
            return
        self._actions_shown = shown
        slots = list(self.query("#actions Button"))[:-1]
        hidden = len(slots) - len(secondary)
        self._slots = {}
        for index, button in enumerate(slots):  # the buttons stay put; only their words change
            used = index >= hidden
            button.display = used
            if used:
                name, label = secondary[index - hidden]
                self._slots[button.id] = name
                button.label = label
                button.styles.width = len(label) + 4  # a changed label does not resize a button by itself
                button.disabled = bool(self.busy) and name != "cancel"
        go = self.query_one("#go", Button)
        go.display = primary is not None
        if primary:
            self._slots["go"] = primary[0]
            go.label = f"▶ {primary[1]}"
            go.styles.width = len(primary[1]) + 6
            go.disabled = bool(self.busy)
        if isinstance(self.focused, Button) and (not self.focused.display or self.focused.disabled):
            self.call_after_refresh(self._focus_for_screen)

    def _focus_for_screen(self) -> None:
        """One focus: the input the user needs, else the main button."""
        if self._gone() or len(self.screen_stack) > 1:
            return
        if self.busy or self.failure:  # the way forward is the button
            target = "#go" if self.failure else "#act-3"
            return self.query_one(target).focus()
        target = {"idea": "#idea-text", "questions": "#q-options", "plan": "#plan-tasks", "build": "#build-tasks",
                  "board": "#col-needs", "projects": "#proj-list", "notes": "#notes-list",
                  "providers": "#prov-architect"}.get(self.screen_name, "#go")
        if self.screen_name == "questions" and not self.query_one("#q-options", OptionList).option_count:
            target = "#q-own" if self.facts["session"]["pending"] else "#go"
        if self.screen_name == "build":  # start on the task that needs the user, not the first row
            self._needs_row = None
            self._follow_needs()
        if self.screen_name == "board":
            target = next((f"#col-{key}" for key in ("needs", "building", "waiting", "done")
                           if self.query_one(f"#col-{key}", OptionList).option_count), "#go")
        for selector in (target, "#go", "#act-3"):
            widget = self.query_one(selector)
            if widget.display and not widget.has_class("hidden") and not getattr(widget, "disabled", False):
                widget.focus(scroll_visible=self.screen_name not in MORE)  # those open at their heading
                if self.screen_name == "board" and not self.busy:  # its main button names the row the focus landed on
                    self.call_after_refresh(lambda: self._gone() or self._show_actions(*self._actions()[:2]))
                return

    def _follow_needs(self) -> None:
        """Move the build list to the task that needs the user, once each time that task changes."""
        if self._gone():
            return
        tasks = self.facts.get("tasks") or []
        questions = self.facts.get("questions") or []
        needs = self._first("in_review") or next((t for t in tasks if questions and t["id"] == questions[0]["from_card"]),
                                                 None) or self._first("blocked")
        key = (needs["id"], needs["state"]) if needs else None
        if key and key != getattr(self, "_needs_row", None):
            self.query_one("#build-tasks", OptionList).highlighted = [t["id"] for t in tasks].index(needs["id"])
        self._needs_row = key

    def _actions(self) -> tuple[tuple | None, list[tuple], str]:
        """The main action, the others, and the key line for the screen shown."""
        if self.busy:
            return None, [("cancel", "Cancel")], "Esc cancel"
        if self.failure:
            others = [("template", "Go on without the Architect")] if self.failure["step"] in ("ask", "spec", "plan") else []
            return ("retry", "Retry"), [*others, ("dismiss_failure", "Back")], "Enter retry"
        if self.view and self.overlay in ("details", "blocked"):
            return ("back", "Back"), [], "Esc back · ↑↓ scroll"
        if self.view and not self.overlay:
            keys = {"plan": " · Space details", "build": " · Space details", "try": " · o desktop shot · p phone shot"}
            return ("now", f"Back to {STEPS[STEP_OF[self.now]]}"), [], \
                "n back to where the project is · [ ] other steps" + keys.get(self.screen_name, "")
        return getattr(self, f"_actions_{self.screen_name}")()

    # idea ----------------------------------------------------------------------

    def _draw_idea(self) -> None:
        if self.view:
            return self.query_one("#idea-next", Static).update("This is the idea the project started from.")
        self.query_one("#idea-next", Static).update(
            "Next the Architect asks a few questions, one at a time, then writes a spec you can read "
            "before anything is built.")

    def _actions_idea(self):
        return ("continue", "Continue"), [("skip_questions", "Skip the questions")], \
            "Enter newline · Ctrl+Enter continue · Tab next field"

    @on(TextArea.Changed, "#idea-text")
    def _idea_typed(self, event: TextArea.Changed) -> None:
        path = session_path(self.repo).with_suffix(".txt")  # saved as typed, so a closed window loses nothing
        try:
            if event.text_area.text.strip():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(event.text_area.text, encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning(f"could not save the idea: {exc}")

    @on(Select.Changed, "#stack")
    def _stack_chosen(self, event: Select.Changed) -> None:
        save_session(self.repo, {**self._session(), "stack": str(event.value)})

    def do_continue(self) -> None:
        if not self._idea():
            self.note = "Say what you want to build first."
            self.query_one("#idea-text").focus()
            return self._draw_busy()
        save_session(self.repo, {**self._session(), "asking": True})
        self._ask()

    def do_skip_questions(self) -> None:
        if not self._idea():
            self.note = "Say what you want to build first."
            return self._draw_busy()
        self._write_spec()

    # questions -----------------------------------------------------------------

    def _draw_questions(self) -> None:
        session = self.facts["session"]
        history, pending = session["history"], session["pending"] or {}
        total = max(int(pending.get("total") or 0), len(history) + (1 if pending else 0))
        dots = " ".join(["[$success]●[/]"] * len(history) + (["[$accent]●[/]"] if pending else [])
                        + ["○"] * (total - len(history) - (1 if pending else 0)))
        self.query_one("#q-progress", Static).update(
            f"[$foreground-muted]Question {len(history) + 1} of {total}[/]   {dots}" if pending else dots)
        self.query_one("#q-title", Static).update(esc(pending.get("question") or (
            "Asking the Architect about your idea..." if self.busy else
            ("The questions you answered" if history else "No questions were asked.") if self.view else "No question is open.")))
        self.query_one("#q-why", Static).update(esc(pending.get("why") or ""))
        suggested = str(pending.get("recommended") or "").strip().lower()
        self._set_list("q-options", [
            (f"opt-{i}", esc(option) + ("   (suggested)" if suggested and (
                option.lower().startswith(suggested[:24]) or suggested.startswith(option.lower()[:24])) else ""))
            for i, option in enumerate(pending.get("options") or [])])
        self._set_list("q-answered", [(f"ans-{i}", f"[$success]✓[/] {esc(turn['question'])}\n   {esc(turn['answer'])}"
                                       + ("" if self.view else "   [$foreground-muted](Enter: change)[/]"))
                                      for i, turn in enumerate(history)])

    def _actions_questions(self):
        if not self.facts["session"]["pending"]:
            return ("enough", "Write the spec"), [("ask_more", "Ask me more")], "Enter write the spec"
        return ("answer", "Answer"), [("enough", "That's enough, write the spec")], \
            "↑↓ choose · Enter answer · Tab type your own"

    def do_answer(self) -> None:
        session = self._session()
        pending = session["pending"]
        if not pending:
            return
        own = self.query_one("#q-own", Input)
        index = self.query_one("#q-options", OptionList).highlighted
        answer = own.value.strip() or (pending["options"][index] if index is not None and pending.get("options") else "")
        if not answer:
            self.note = "Choose an answer or type your own."
            return self._draw_busy()
        own.value = ""
        session["history"].append({"question": pending["question"], "answer": answer})
        session["pending"] = None
        save_session(self.repo, session)
        self.say(f"You answered: {pending['question'][:40]} → {answer[:50]}")
        self._ask()

    @on(Input.Submitted, "#q-own")
    def _own_answer_sent(self) -> None:
        self.action_act("answer")

    @on(OptionList.OptionSelected, "#q-answered")
    def _change_answer(self, event: OptionList.OptionSelected) -> None:
        """Reopen an earlier question: it and what followed are asked again."""
        if self.view:
            return
        session = self._session()
        index = int(str(event.option.id).split("-")[1])
        turn = session["history"][index]
        session["history"] = session["history"][:index]
        session["pending"] = {"question": turn["question"], "options": [turn["answer"]], "recommended": "", "why": "",
                              "total": 0}
        save_session(self.repo, session)
        self.refresh_screen(focus=True)

    def do_enough(self) -> None:
        self._write_spec()

    def do_ask_more(self) -> None:
        self._ask()

    def _ask(self, template: bool = False) -> None:
        session = self._session()
        idea, stack = self._idea(), session["stack"]

        def step(backend, _progress):
            return architect_grill_turn(self.repo, idea=idea, history=list(session["history"]),
                                        language="typescript" if stack == "auto" else stack, backend=backend)

        self._step("ask", "Asking the Architect about your idea", step, template)

    def _asked(self, turn: dict) -> None:
        session = self._session()
        if turn.get("ready_to_plan") or not turn.get("question"):
            self.say("The Architect has what it needs")
            return self._write_spec(template=self._template)
        session["pending"] = {key: turn.get(key) for key in ("question", "options", "recommended", "why", "total")}
        session["asking"] = True
        save_session(self.repo, session)
        self.say(f"The Architect asks: {turn['question'][:70]}")
        self.refresh_screen(focus=True)

    # spec ----------------------------------------------------------------------

    def _write_spec(self, template: bool = False, change: str = "") -> None:
        session = self._session()
        idea, stack = self._idea(), session["stack"]
        history = list(session["history"]) + ([{"question": "Change asked for in the spec", "answer": change}] if change else [])

        def step(backend, progress):
            if self._cfg() is None:  # the Architect is open before anything is set up: a failure leaves the folder alone
                progress("Setting up the project folder")
                initialize_project(self.repo.name or "my-app", directory=self.repo, idea=idea, language=stack,
                                   force=True, commit=True)
            return draft_spec_and_chunks(self.repo, idea=idea, grill_history=history,
                                         language="" if stack == "auto" else stack, backend=backend, on_progress=progress)

        self._step("spec", "Writing the spec and its goals", step, template)

    def _spec_written(self, result: dict) -> None:
        save_session(self.repo, {**self._session(), "asking": False, "pending": None})
        self.say(f"Spec written: {len(result.get('chunks') or [])} goals")
        self.refresh_screen(focus=True)

    def _draw_spec(self) -> None:
        facts = self.facts
        self.query_one("#spec-title", Static).update(esc(f"The spec · {len(facts['goals'])} goals"))
        self.query_one("#spec-goals", Static).update("\n".join(
            f"{'[$success]✓[/]' if g['status'] == 'done' else ('[$accent]●[/]' if g is facts['goal'] else '○')} "
            f"[b]{n}  {esc(g['title'])}[/]  [$foreground-muted]{esc(g.get('goal') or '')}[/]"
            for n, g in enumerate(facts["goals"], start=1)))
        self.query_one("#spec-text", Static).update(esc(facts["spec_text"]))

    def _actions_spec(self):
        return ("plan_goal", f"Plan goal {self.facts['goal_no']}"), \
            [("change_spec", "Change the spec…"), ("edit_spec", "Edit in $EDITOR")], "Enter plan · ↑↓ scroll"

    def do_plan_goal(self) -> None:
        self._plan()

    def do_change_spec(self) -> None:
        self.push_screen(AskModal("What should change in the spec?", "The Architect rewrites it with this in mind."),
                         lambda text: text and self._write_spec(change=text))

    def do_edit_spec(self) -> None:
        self._edit("The spec", self.facts["spec_text"] + "\n", ".md",
                   lambda text: (write_doc(self.repo, self._cfg(), "spec", text, why="edited by hand"),
                                 self.say("Spec edited by hand")))

    def _edit(self, title: str, text: str, suffix: str, save) -> None:
        """Open text in `$EDITOR`, or in the built-in editor when none is set."""
        def done(new: str | None) -> None:
            if new is not None and new != text:
                try:
                    save(new)
                except Exception as exc:  # noqa: BLE001 - a refused edit is shown, not raised
                    self.note = f"Not saved: {getattr(exc, 'message', exc)}"
            self.refresh_screen()

        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "")
        if not editor or shutil.which(editor[0]) is None or self.is_headless:
            return self.push_screen(EditModal(title, text), done)
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as handle:
            handle.write(text)
        try:
            with self.suspend():
                subprocess.call([*editor, handle.name])
            done(Path(handle.name).read_text(encoding="utf-8"))
        finally:
            os.unlink(handle.name)

    # plan ----------------------------------------------------------------------

    def _plan(self, template: bool = False, change: str = "") -> None:
        goal = self.facts["goal"]

        def step(backend, progress):
            return draft_wave_plan(self.repo, idea=change, chunk_id=goal["id"], backend=backend, on_progress=progress)

        self._step("plan", f"Splitting goal {self.facts['goal_no']} into tasks and checking them", step, template)

    def _planned(self, result: dict) -> None:
        cards = result.get("cards") or []
        repaired = [c["id"] for c in cards if c.get("repairs")]
        self.say(f"Goal {self.facts['goal_no']} split into {len(result.get('new_cards') or cards)} tasks")
        if repaired:
            self.say(f"The checks repaired {len(repaired)} task(s) before showing them ({', '.join(repaired)})")
        self.refresh_screen(focus=True)

    @staticmethod
    def _order(task: dict) -> str:
        return "first" if not task["deps"] else "after " + ", ".join(task["deps"])

    def _draw_plan(self) -> None:
        facts = self.facts
        tasks = [t for t in facts["tasks"] if t["state"] != "dropped"]
        parked = [t for t in tasks if t["state"] == "backlog"]
        verdict = "all checked" if not parked else f"{len(parked)} can't be built yet"
        self.query_one("#plan-title", Static).update(
            esc(f"Goal {facts['goal_no']} · {facts['goal']['title']}: {len(tasks)} tasks, {verdict}"))
        self.query_one("#plan-sub", Static).update(esc(facts["goal"].get("goal") or ""))
        self._set_list("plan-tasks", [
            (t["id"], self._row(*(("✗", "$error") if t["state"] == "backlog" else ("◇", "$foreground-muted")), t,
                                f"{len(t['files'])} files · {self._order(t)}")) for t in tasks])
        failed: dict[str, list[str]] = {}
        for task in parked:
            for reason in task["parked"]:
                failed.setdefault(reason.partition(":")[0], []).append(f"{task['id']}: {reason.partition(':')[2].strip()}")
        lines = ["[b]Checked before you see it[/]"]
        for key, words in CHECKS:
            if key in failed:
                lines.append(f"[$error]✗[/] {words}")
                lines += [f"[$foreground-muted]    {esc(why)}[/]" for why in failed[key]]
            else:
                lines.append(f"[$success]✓[/] {words}")
        for task in tasks:
            lines += [f"[$accent]↻[/] Repaired {esc(task['id'])}: {esc(note)}  [$foreground-muted](Space: why)[/]"
                      for note in task["repairs"]]
        lines += [f"[$warning]![/] {esc(task['id'])} may use the network: its text says why  [$foreground-muted](Space)[/]"
                  for task in tasks if task["network"]]
        box = self.query_one("#plan-checks", Static)
        box.update("\n".join(lines))
        box.set_classes("card " + ("bad" if failed else "ok"))

    def _actions_plan(self):
        tasks = self.facts["tasks"]
        ready = sum(1 for t in tasks if t["state"] == "ready")
        parked = sum(1 for t in tasks if t["state"] == "backlog")
        others = [("edit_task", "Edit a task"), ("change_plan", "Change the plan…")]
        keys = "↑↓ task · e edit · Space details · b board"
        if parked and not ready:
            return ("fix_parked", f"Repair {parked} task{'s' if parked != 1 else ''}"), others, "Enter repair · " + keys
        if parked:
            others.insert(0, ("fix_parked", f"Repair {parked} parked"))
        return ("build", f"Build {ready} task{'s' if ready != 1 else ''}"), others, "Enter build · " + keys

    def do_change_plan(self) -> None:
        self.push_screen(AskModal("What should change in the plan?", "The Architect redrafts this goal's tasks."),
                         lambda text: text and self._plan(change=text))

    def do_fix_parked(self) -> None:
        def step(backend, progress):
            cfg = self._cfg()
            return preflight_plan(self.repo, cfg, open_store(self.repo, cfg), backend=backend, on_progress=progress)

        self._step("repair", "Asking the Architect to repair the tasks that can't be built", step)

    def do_edit_task(self) -> None:
        from openfarm_core.adapters.files import parse_card_file, render_card_file

        task = self._selected_task()
        if task is None or self.busy:
            return
        cfg = self._cfg()
        store = open_store(self.repo, cfg)
        header, body = store.read(task["id"])

        def save(text: str) -> None:
            new_header, new_body = parse_card_file(text)
            store.change_card(task["id"], "edit", why="edited by hand",
                              apply=lambda h, _b: ({**new_header, "id": h["id"], "state": h.get("state", "ready")}, new_body))
            self.say(f"{task['id']} edited by hand")

        self._edit(f"Task {task['id']}", render_card_file(header, body), ".md", save)

    # build ---------------------------------------------------------------------

    def do_build(self) -> None:
        if self._following or self.facts["running"]:
            return
        refusal = self._refusal()
        if refusal:
            self.note = f"The build can't start: {refusal}."
            return self._draw_busy()
        self._following = True
        self._build_bg("")

    @work(thread=True, exit_on_error=False, group="sort")
    def _sort_questions(self, findings: list[dict]) -> None:
        """A builder's question is shown only if it is the owner's. The rest never interrupt: sorted here, quietly."""
        try:
            cfg = self._cfg()
            backend, _reason = open_architect_backend(self.repo, cfg)
        except Exception:  # noqa: BLE001 - with no Architect the questions go to the owner
            backend = None
        for finding in findings:
            try:
                outcome = settle_question(self.repo, cfg, open_store(self.repo, cfg), finding, backend)
            except Exception as exc:  # noqa: BLE001 - shown in the strip
                self.call_from_thread(self.say, f"{finding['from_card']}'s question could not be sorted: {exc}")
                # e.g. its task went back to building on an earlier answer: sorted again if it ever holds the task up
                self.call_from_thread(self._sorting.discard, finding["id"])
                continue
            self.call_from_thread(self._question_sorted, finding, outcome)

    def _question_sorted(self, finding: dict, outcome: dict) -> None:
        who = finding["from_card"]
        if outcome["for"] == "architect":
            if not outcome.get("stuck"):
                self.say(f"{who} asked a question the Architect could answer; the answer is in the task")
                return self._resume_build()
            self.say(f"{who} asked a question the Architect answered, but it has stopped the same way twice: it waits for you")
        if outcome["for"] == "nobody":
            self.say(f"{who} left a note about the tooling; it is filed with the builders' notes")
        self.refresh_screen()

    def _resume_build(self) -> None:
        """The user just unblocked a task: the build goes back to work without a second press."""
        self.refresh_screen(focus=True)
        waiting = [t for t in self.facts["tasks"] if t["state"] == "ready"]
        if waiting and not self._following and not self.facts["running"] and not self._refusal():
            self.do_build()

    def _refusal(self) -> str:
        cfg = self._cfg()
        state = StateDir(cfg.state_dir)
        if state.kill_requested(cfg.kill_switch):
            return "it was stopped (use Resume)"
        if state.is_paused():
            return "it is paused (use Resume)"
        if state.read_breaker().get("tripped"):
            return "too many tasks failed in a row, so the safety stop is on (Ctrl+K, Settings)"
        return ""

    def _attach_running_build(self) -> None:
        try:
            status = daemon_status(self.repo, config_path=self.config_path)
        except Exception:  # noqa: BLE001 - no project, nothing running
            return
        if status.get("running") and status.get("run_id") and not self._following:
            self._following = True
            self._build_bg(str(status["run_id"]))

    def _load_last_run(self) -> None:
        """Show the last build's events again after the app was closed."""
        cfg = self._cfg()
        if cfg is None:
            return
        runs = sorted(p for p in StateDir(cfg.state_dir).runs.glob("*/events.jsonl")) if StateDir(cfg.state_dir).runs.is_dir() else []
        if runs:
            for line in runs[-1].read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    self._run_event(json.loads(line), quiet=True)
                except ValueError:
                    continue

    @work(thread=True, exit_on_error=False)
    def _build_bg(self, attach_run_id: str) -> None:
        """Start a build in the background service and follow it. Closing the app leaves it running."""
        worker = get_current_worker()
        try:
            cfg = self._cfg()
            run_id = attach_run_id
            if run_id:
                self.call_from_thread(self.say, "A build is already running; showing its progress")
            else:
                self.call_from_thread(self.say, "Starting the build")
                ensure_baseline_committed(self.repo)
                run_id = "run-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                start_daemon(self.repo, cards=24, run_id=run_id, follow=True, config_path=self.config_path)
            events = StateDir(cfg.state_dir).run_dir(run_id) / "events.jsonl"
            seen, ended, started = 0, 0, False
            while not worker.is_cancelled:
                running = bool(daemon_status(self.repo, config_path=self.config_path).get("running"))
                try:
                    with events.open("rb") as handle:
                        handle.seek(seen)
                        data = handle.read()
                    started = True
                except OSError:
                    data = b""
                whole = data[: data.rfind(b"\n") + 1]
                seen += len(whole)
                for line in whole.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    ended += event.get("kind") == "card_done"
                    self.call_from_thread(self._run_event, event)
                if not running:
                    break
                time.sleep(0.25)
            if worker.is_cancelled:
                return
            if not started:
                log = read_daemon_logs(self.repo, tail=12, config_path=self.config_path)
                lines = [ln for ln in (log.get("lines") or []) if ln.strip() and not ln.startswith("===")]
                self.call_from_thread(self.say, f"The build did not start: {lines[-1] if lines else 'no reason was given'}")
            else:
                self.call_from_thread(self.say, f"Build finished: {ended} task(s)" if ended
                                      else "The build ended without starting a task")
        except Exception as exc:  # noqa: BLE001 - shown in the strip; the app stays up
            self.call_from_thread(self.say, f"The build could not be followed: {exc}")
        finally:
            self._following = False
            if not worker.is_cancelled:
                self.call_from_thread(self.refresh_screen)

    def _run_event(self, event: dict, quiet: bool = False) -> None:
        kind, task_id = str(event.get("kind") or ""), str(event.get("card_id") or "")
        phase, message = str(event.get("phase") or ""), str(event.get("message") or "")
        if event.get("worker_tokens") or event.get("supervisor_tokens"):
            self.tokens["worker"] = max(self.tokens["worker"], int(event.get("worker_tokens") or 0))
            self.tokens["frontier"] = max(self.tokens["frontier"], int(event.get("supervisor_tokens") or 0))
            self.health["Builders"] = True
        stamp = str(event.get("ts") or "")[11:16]
        line = ""
        if kind == "card_done" and task_id:
            self.phases.pop(task_id, None)
            final = str(event.get("final_state") or "")
            line = f"{task_id}  " + ("passed every check, ready for you" if final in ("in-review", "in_review")
                                     else f"stopped ({final}): {message}" if message else f"stopped ({final})")
        elif kind == "phase" and task_id and phase and self.phases.get(task_id) != phase:
            self.phases[task_id] = phase
            line = f"{task_id}  {PHASES.get(phase, phase)}"
        elif kind == "run_start":
            import calendar

            try:
                self._build_started = calendar.timegm(time.strptime(str(event.get("ts"))[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                self._build_started = time.time()
            line = "Build started" + (": " + message.partition("; ")[2] if "; " in message else "")
        elif kind in ("run_more", "run_waiting", "question") and message:
            line = message.replace("card(s)", "task(s)")
        if line:
            self.activity.append(f"{stamp or time.strftime('%H:%M')}  {line}")
            if not quiet:
                self.refresh_screen()

    def _state_words(self, task: dict) -> tuple[str, str, str]:
        """Glyph, colour and sentence for a task's state."""
        state = task["state"]
        if state == "done":
            return "✓", "$success", "accepted"
        if state == "in_review":
            return "✓", "$success", "passed every check · ready for you"
        if state == "blocked":
            kind = (task["triage"] or {}).get("kind", "builder")
            return "✗", "$error", f"stopped: {STOPPED[kind]}"
        if state == "in_progress":
            phase = PHASES.get(self.phases.get(task["id"], ""), "being built")
            return "●", "$accent", f"{phase} · try {task['tries'] + 1}"
        if state == "backlog":
            return "✗", "$error", "can't be built yet (Space: why)"
        if task["waits"]:
            stopped = {t["id"] for t in self.facts["all_tasks"] if t["state"] == "blocked"}
            return "◌", "$foreground-muted", "waits for " + ", ".join(
                f"{dep} (stopped)" if dep in stopped else dep for dep in task["waits"])
        return "◌", "$foreground-muted", "next in line" if self.facts["running"] else "not started"

    def _draw_build(self) -> None:
        facts = self.facts
        tasks = [t for t in facts["tasks"] if t["state"] != "dropped"]
        accepted = sum(1 for t in tasks if t["state"] == "done")
        started = sum(1 for t in tasks if t["state"] in ("in_progress", "in_review", "blocked", "done"))
        self.query_one("#build-title", Static).update(
            esc(f"Building {started} of {len(tasks)} tasks" if facts["running"]
                else f"The build is not running · {accepted} of {len(tasks)} tasks accepted"))
        rows = []
        for task in tasks:
            glyph, colour, words = self._state_words(task)
            rows.append((task["id"], self._row(glyph, colour, task, words)))
        self._set_list("build-tasks", rows)
        question = self.query_one("#build-question", Static)
        question.set_class(not facts["questions"], "hidden")
        if facts["questions"]:
            first = facts["questions"][0]
            held = [c for c in first.get("holds_up") or [] if c != first["from_card"]]
            waits = (f"Only {first['from_card']} waits for this." if not held
                     else f"{first['from_card']} waits for this, and so do {', '.join(held)}.")
            question.update(f"[$accent]?[/] [b]{esc(first['from_card'])} asks:[/] {esc(first['title'])}\n"
                            f"[$foreground-muted]  {esc(first.get('body_preview') or '')}\n  {esc(waits)}[/]")
        ready = self._first("in_review")
        box = self.query_one("#build-ready", Static)
        box.set_class(ready is None, "hidden")
        if ready is not None:
            box.update(f"[b]{esc(ready['id'])} · {esc(ready['title'])}[/]   [$success]ready to accept[/]\n"
                       f"{esc(self._evidence(ready))}\n"
                       "[$foreground-muted]Accept merges it into main on this machine. Nothing is pushed.[/]")

    @staticmethod
    def _evidence(task: dict) -> str:
        record = task["last"] or {}
        parts = [f"{len(task['files'])} files"]
        named = record.get("named_tests") or []
        if named:
            parts.append(f"{len(named)} tests pass")
            parts.append("the old code fails them (so they test something)")
        return " · ".join(parts)

    def _actions_build(self):
        facts = self.facts
        ready, blocked = self._first("in_review"), self._first("blocked")
        waiting = [t for t in facts["tasks"] if t["state"] == "ready"]
        others: list[tuple] = []
        primary = None
        if ready:
            primary = ("accept_task", f"Accept {ready['id']}")
        elif facts["questions"]:
            primary = ("answer_question", f"Answer {facts['questions'][0]['from_card']}'s question")
        elif blocked:
            primary = ("open_blocked", f"See why {blocked['id']} stopped")
        elif waiting and not facts["running"] and not self._following:
            primary = ("build", f"Build {len(waiting)} task{'s' if len(waiting) != 1 else ''}")
        elif not facts["running"] and not self._following and self._first("backlog"):  # parked by the plan checks
            parked = sum(1 for t in facts["tasks"] if t["state"] == "backlog")
            primary = ("fix_parked", f"Repair {parked} task{'s' if parked != 1 else ''}")
            others.append(("edit_task", "Edit a task"))
        if facts["questions"] and primary and primary[0] != "answer_question":
            others.append(("answer_question", "Answer the question"))
        if blocked and primary and primary[0] != "open_blocked":
            others.append(("open_blocked", f"See why {blocked['id']} stopped"))
        cfg = facts["cfg"]
        held = cfg is not None and (StateDir(cfg.state_dir).is_paused() or StateDir(cfg.state_dir).kill_requested(cfg.kill_switch))
        if held:
            others.append(("resume", "Resume"))
        elif facts["running"] or self._following:  # nothing to pause when nothing runs
            others.append(("pause", "Pause"))
        verb = primary[1].split()[0].lower() if primary else ""
        return primary, others, (f"Enter {verb} · " if verb else "") + "↑↓ task · Space details · b board · l log"

    def do_accept_task(self) -> None:
        task = self._first("in_review")
        if task:
            self._run_cli(f"Accepting {task['id']}", [["verdict", task["id"], "clean"], ["land", task["id"]]],
                          f"{task['id']} accepted and merged into main on this machine")

    def do_answer_question(self) -> None:
        if not self.facts["questions"]:
            return
        finding = self.facts["questions"][0]

        def answered(text: str | None) -> None:
            if not text:
                return
            cfg = self._cfg()
            store = open_store(self.repo, cfg)
            target = finding["from_card"]
            store.change_card(target, "edit", why=f"answer to its question ({finding['id']})",
                              apply=lambda h, b: (h, b.rstrip() + f"\n\n## Answer from the owner\n\n{finding['title']}\n\n{text}\n"))
            decide_finding(self.repo, cfg, finding["id"], "folded", reason=text, target_card=target)
            if store.states().get(target) == "blocked":
                store.change_card(target, "retry", why="its question was answered")
            self.say(f"You answered {target}'s question; it is back in the queue")
            self._resume_build()

        self.push_screen(AskModal(f"{finding['from_card']} asks: {finding['title']}",
                                  (finding.get("body") or finding.get("body_preview") or "")[:3000]), answered)

    def do_pause(self) -> None:
        self._run_cli("Pausing the build", [["pause"]], "Paused: tasks being built finish, no new one starts")

    def do_resume(self) -> None:
        self._run_cli("Resuming", [["resume"]], "Resumed")

    def do_open_blocked(self) -> None:
        task = self._selected_task()
        if task is None or task["state"] != "blocked":
            task = self._first("blocked")
        if task:
            self._open("blocked", task["id"])

    def _open(self, overlay: str, task_id: str = "") -> None:
        self.overlay, self.task_id = overlay, task_id or self.task_id
        self.fix = self.check_output = None
        self.refresh_screen(focus=True)

    # blocked task --------------------------------------------------------------

    def _draw_blocked(self) -> None:
        task = self._find()
        triage = task["triage"] or {"kind": "builder", "why": "The task stopped.", "main": "retry"}
        self.query_one("#blocked-title", Static).update(
            f"[$error]✗[/] {esc(task['id'])} stopped: {STOPPED[triage['kind']]}")
        why = triage["why"]
        if len(why) > 420:  # the builder's whole report is in the details; here, what is wrong
            why = why[:420].rsplit(". ", 1)[0] + ".  (Space: the builder's whole report)"
        self.query_one("#blocked-why", Static).update(esc(why))
        if triage["kind"] == "task" and self.fix is None:
            cfg = self._cfg()
            try:
                self.fix = propose_fix(self.repo, cfg, open_store(self.repo, cfg), task["id"]) or {}
            except Exception:  # noqa: BLE001 - no fix to show is a state the screen handles
                self.fix = {}
        box = self.query_one("#blocked-fix", Static)
        if self.check_output:
            box.update(f"[b]{esc(self.check_output['command'])}[/]  [$foreground-muted]exit {self.check_output['exit']}[/]\n"
                       + esc(self.check_output["output"][-3000:]))
        elif self.fix:
            lines = [f"[b]Suggested fix[/] [$foreground-muted]{esc('; '.join(self.fix['notes']))}[/]"]
            for line in [ln for ln in self.fix["diff"].splitlines()[2:] if not ln.startswith("@@")][:10]:
                colour = "$success" if line.startswith("+") else "$error" if line.startswith("-") else "$foreground-muted"
                lines.append(f"[{colour}]{esc(line)}[/]")
            box.update("\n".join(lines))
        elif triage["kind"] == "task":
            box.update("[$foreground-muted]The checks have no fix of their own. The Architect can be asked for one.[/]")
        elif triage["kind"] == "check":
            box.update("[$foreground-muted]Run the check yourself to see its raw output before deciding.[/]")
        else:
            box.update("[$foreground-muted]The task reads as buildable. A new try starts from the same task.[/]")
        cfg = self._cfg()
        from openfarm_core.blocked import records

        tries = records(cfg.state_dir, task["id"])
        lines = ["[b]Tries[/]"]
        for number, record in enumerate(tries, start=1):
            lines.append(f"[$error]✗[/] {number}  {esc(str(record.get('claimed_at') or '')[11:16])}  "
                         f"{esc(str(record.get('detail') or record.get('stop_code') or 'stopped')[:150])}")
        lines.append("[$foreground-muted]  nothing was merged[/]")
        self.query_one("#blocked-tries", Static).update("\n".join(lines))

    def _actions_blocked(self):
        task = self._find()
        kind = (task["triage"] or {}).get("kind", "builder")
        keys = "d full diff · t tries · Esc back"
        if kind == "task":
            main = ("apply_fix", "Apply fix and retry") if self.fix else ("ask_fix", "Ask the Architect for a fix")
            return main, [("edit_task", "Edit the task myself"), ("skip_task", "Skip this task")], \
                f"Enter {'apply and retry' if self.fix else 'ask'} · " + keys
        if kind == "check":
            return ("run_check", "Run it myself"), [("accept_anyway", "Accept anyway"), ("retry_task", "Retry")], \
                "Enter run the check · " + keys
        return ("retry_task", "Retry"), [("edit_task", "Edit the task myself"), ("skip_task", "Skip this task")], \
            "Enter retry · " + keys

    def do_apply_fix(self) -> None:
        cfg = self._cfg()
        outcome = apply_fix_and_retry(open_store(self.repo, cfg), self.task_id, self.fix)
        self.say(f"{self.task_id}: fix applied, " + ("back in the queue" if outcome["state"] == "ready"
                                                    else "but it still fails the plan checks"))
        self.overlay = ""
        self._resume_build()

    def do_ask_fix(self) -> None:
        task = self._find()

        def step(backend, _progress):
            cfg = self._cfg()
            return propose_fix(self.repo, cfg, open_store(self.repo, cfg), task["id"],
                               why=(task["triage"] or {}).get("why", ""), backend=backend)

        self._step("fix", f"Asking the Architect how to fix {task['id']}", step)

    def do_retry_task(self) -> None:
        cfg = self._cfg()
        outcome = open_store(self.repo, cfg).change_card(self.task_id, "retry", why="retried from the blocked screen")
        self.say(f"{self.task_id} is back in the queue" if outcome["state"] == "ready"
                 else f"{self.task_id} still fails the plan checks")
        self.overlay = ""
        self._resume_build()

    def do_skip_task(self) -> None:
        def skipped(why: str | None) -> None:
            if why:
                open_store(self.repo, self._cfg()).change_card(self.task_id, "skip", why=why)
                self.say(f"{self.task_id} skipped: {why[:60]}")
                self.overlay = ""
                self.refresh_screen(focus=True)

        self.push_screen(AskModal(f"Why skip {self.task_id}?", "It is kept in the history, and nothing waits for it any more."),
                         skipped)

    def do_accept_anyway(self) -> None:
        def accepted(note: str | None) -> None:
            if note:
                self.overlay = ""
                self._run_cli(f"Accepting {self.task_id}", [["land", self.task_id, "--accept-blocked", "--note", note]],
                              f"{self.task_id} accepted by hand and merged")

        self.push_screen(AskModal(f"Why is {self.task_id} right although a check failed?",
                                  "Your reason is kept with the task."), accepted)

    @work(thread=True, exit_on_error=False)
    def do_run_check(self) -> None:
        task_id = self.task_id
        self.call_from_thread(self._set_busy, f"Running {task_id}'s tests on its own branch")
        try:
            cfg = self._cfg()
            header, body = open_store(self.repo, cfg).read(task_id)
            self.check_output = run_check(self.repo, cfg, card_from(header, body))
        except Exception as exc:  # noqa: BLE001
            self.check_output = {"command": "", "exit": None, "output": f"The check could not be run: {exc}"}
        self.call_from_thread(self._set_busy, "")

    def do_full_diff(self) -> None:
        task = self._selected_task()
        if task is not None and not self.busy:
            self._open("details", task["id"])

    def do_tries(self) -> None:
        if self.screen_name == "blocked":
            self.query_one("#blocked-tries").scroll_visible()

    # try it --------------------------------------------------------------------

    def _draw_try(self) -> None:
        facts, result = self.facts, self.facts["tried"]
        number = facts["goal_no"]
        if result is None and self.view:
            self.query_one("#try-title", Static).update(esc(f"Goal {number} was not tried here."))
            for part in ("#try-sub", "#try-checks", "#try-shots"):
                self.query_one(part, Static).update("")
            self.query_one("#try-colours", Static).add_class("hidden")
            return
        if result is None:
            self.query_one("#try-title", Static).update(esc(f"Goal {number} is built. Starting it to have a look."))
            self.query_one("#try-sub", Static).update("")
            self.query_one("#try-checks", Static).update("[$foreground-muted]◌ not tried yet[/]")
            self.query_one("#try-shots", Static).update("")
            self.query_one("#try-colours", Static).add_class("hidden")
            if not self.busy and not self.failure and facts["goal"]["id"] not in self._auto_tried:
                self._auto_tried.add(facts["goal"]["id"])
                self.call_after_refresh(self.do_try_again)
            return
        self.query_one("#try-title", Static).update(esc(f"Goal {number} is built. Here it is running."))
        where = f" on {result['url']}" if result["url"] else ""
        how = " · screenshots from a headless browser" if result["screenshots"] else ""
        self.query_one("#try-sub", Static).update(esc(f"{result['command'] or 'no start command'}{where}{how}"))
        lines = ["[b]Checks[/]"]
        for check in result["checks"]:
            mark = {True: "[$success]✓[/]", False: "[$error]✗[/]", None: "[$accent]?[/]"}[check["ok"]]
            head, _, why = str(check["detail"]).partition(" (")
            lines.append(f"{mark} {esc(head)}")
            if why and check["ok"] is not True:
                lines.append(f"[$foreground-muted]    {esc(why.rstrip(')'))}[/]")
        if result["kind"] == "cli" and result["output"]:
            lines += ["", esc(result["output"][-1200:])]
        box = self.query_one("#try-checks", Static)
        box.update("\n".join(lines))
        box.set_classes("card " + ("bad" if result["ok"] is False else "ok" if result["ok"] else ""))
        colours = [c for c in result.get("colours") or [] if len(str(c.get("hex"))) == 7 and str(c["hex"]).startswith("#")]
        swatches = self.query_one("#try-colours", Static)
        swatches.set_class(not colours, "hidden")
        if colours:  # the page's own colours, so "themed from the picture" can be judged without opening a screenshot
            rows = ["[b]Colours on the first screen[/]  [$foreground-muted]largest first[/]"]
            for use, label in (("background", "Backgrounds"), ("text", "Text")):
                chips = [f"[on {c['hex']}]      [/] {c['hex']}" for c in colours if c.get("use") == use]
                if chips:
                    rows.append(f"{label:<12}" + "   ".join(chips))
            swatches.update("\n".join(rows))
        names = [Path(shot).stem.replace("-", " ") for shot in result["screenshots"]]
        self.query_one("#try-shots", Static).update(
            "[b]Screenshots[/]   " + "   ".join(f"{esc(name)} [$foreground-muted]({key})[/]" for name, key in zip(names, "op"))
            if names else "")

    def _actions_try(self):
        result = self.facts["tried"]
        if result is None:
            return None, [("try_again", "Try it now")], "Esc back"
        common = [("something_wrong", "Something else is wrong…")]
        if result["ok"] is False:
            return ("fix_this", "Fix this"), [*common, ("looks_right", "Looks right anyway"), ("open_browser", "Open in browser")], \
                "Enter fix this · o desktop shot · p phone shot · w open in browser"
        return ("looks_right", "Looks right"), [*common, ("try_again", "Try again"), ("open_browser", "Open in browser")], \
            "Enter looks right · o desktop shot · p phone shot · w open in browser"

    @work(thread=True, exit_on_error=False)
    def do_try_again(self) -> None:
        goal = self.facts.get("goal")
        if goal is None or self.busy:
            return
        self.call_from_thread(self._set_busy, "Starting the app and looking at it")
        try:
            result = try_it(self.repo, self._cfg(), goal)
            failed = sum(1 for c in result["checks"] if c["ok"] is False)
            self.call_from_thread(self.say, f"Tried goal {self.facts['goal_no']}: "
                                  + (f"{failed} of {len(result['checks'])} checks failed" if failed else "every check passed"))
        except Exception as exc:  # noqa: BLE001
            self.failure = {"step": "try", "reason": f"The app could not be tried: {exc}"}
        self.call_from_thread(self._set_busy, "")

    def do_fix_this(self) -> None:
        self._iterate(fix_request(self.facts["tried"]))

    def do_something_wrong(self) -> None:
        self.push_screen(AskModal("What's wrong?", "Say what you see. It becomes fix tasks under this goal."),
                         lambda text: text and self._iterate(text))

    def _iterate(self, text: str) -> None:
        goal = self.facts["goal"] or (self.facts["goals"][-1] if self.facts["goals"] else None)

        def step(backend, progress):
            return something_wrong(self.repo, text, chunk_id=goal["id"], backend=backend, on_progress=progress)

        self._step("iterate", "Asking the Architect for fix tasks", step)

    def do_looks_right(self) -> None:
        result = self.facts["tried"]
        if result is None:
            return
        if result["ok"] is False:
            return self.push_screen(
                AskModal("A check failed. Why does it look right to you?", "Your reason is kept with the result."),
                lambda why: why and self._looks_right(why))
        self._looks_right("")

    def _looks_right(self, why: str) -> None:
        looks_right(self._cfg(), self.facts["goal"]["id"], why)
        self.say(f"You looked at goal {self.facts['goal_no']}: looks right")
        self.refresh_screen(focus=True)

    def _shot(self, index: int) -> None:
        shots = (self.shown.get("tried") or {}).get("screenshots") or []
        if self.screen_name == "try" and index < len(shots):
            webbrowser.open(Path(shots[index]).as_uri())
            self.note = f"Opened {shots[index]}"
            self._draw_busy()

    def do_shot_desktop(self) -> None:
        self._shot(0)

    def do_shot_phone(self) -> None:
        self._shot(1)

    def do_open_browser(self) -> None:
        """Start the app again and leave it running until this window closes."""
        from openfarm_core.tryit import _free_port

        plan = self.facts.get("try_plan") or {}
        if self.screen_name not in ("try", "accept") or plan.get("kind") != "web":
            return
        if self._app_server is None or self._app_server.poll() is not None:
            port = plan.get("port") or _free_port()
            self._app_server = subprocess.Popen(
                plan["start"], cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, plan.get("port_env") or "PORT": str(port)}, start_new_session=True)
            self._app_url = f"http://127.0.0.1:{port}{plan.get('path') or '/'}"
        webbrowser.open(self._app_url)
        self.note = f"The app is running at {self._app_url} until you close openfarm."
        self._draw_busy()

    def on_unmount(self) -> None:
        if self._app_server is not None and self._app_server.poll() is None:
            try:
                os.killpg(self._app_server.pid, 15)
            except OSError:
                pass

    # accept --------------------------------------------------------------------

    def _draw_accept(self) -> None:
        facts = self.facts
        if facts["goal"] is None:
            self.query_one("#accept-title", Static).update(esc(f"All {len(facts['goals'])} goals are built and accepted."))
            self.query_one("#accept-text", Static).update(
                "\n".join(f"[$success]✓[/] {n}  {esc(g['title'])}" for n, g in enumerate(facts["goals"], start=1))
                + "\n\n[$foreground-muted]Add a feature to keep going. It becomes the next goal.[/]")
            return
        tasks = [t for t in facts["tasks"] if t["state"] != "dropped"]
        tests = sum(len((t["last"] or {}).get("named_tests") or []) for t in tasks)
        result = facts["tried"]
        lines = [esc(facts["goal"].get("goal") or ""), "", f"[b]Tasks[/]  {len(tasks)} built and merged"]
        lines += [f"[$success]✓[/] [b]{esc(t['id'])}[/]  {esc(t['title'])}" for t in tasks]
        lines += ["", f"[b]Tests[/]  {tests} written for these tasks, all passing" if tests else "[b]Tests[/]  none recorded"]
        if result is None:
            lines += ["", "[b]Try it[/]  " + ("not tried" if not facts["try_needed"] else "not tried for this code")]
        else:
            passed = sum(1 for c in result["checks"] if c["ok"])
            said = " · you said it looks right" if result.get("looks_right") else ""
            lines += ["", f"[b]Try it[/]  {passed} of {len(result['checks'])} checks passed{said}"]
            lines += [f"{'[$success]✓[/]' if c['ok'] else '[$error]✗[/]'} {esc(str(c['detail']).partition(' (')[0])}"
                      for c in result["checks"]]
        self.query_one("#accept-title", Static).update(
            esc(f"Goal {facts['goal_no']} · {facts['goal']['title']} "
                + ("was accepted" if facts["goal"]["status"] == "done" else "is ready to accept")))
        self.query_one("#accept-text", Static).update("\n".join(lines))

    def _actions_accept(self):
        facts = self.facts
        if facts["goal"] is None:
            return ("new_feature", "Add a feature…"), [("something_wrong", "Something's wrong…")], "Enter add a feature"
        others = [("something_wrong", "Something's wrong…")]
        if facts["try_plan"]:
            others.append(("try_again", "Try it"))
        return ("accept_goal", f"Accept goal {facts['goal_no']}"), others, "Enter accept"

    def do_accept_goal(self) -> None:
        number = self.facts["goal_no"]
        following = next_goal(self.repo)
        self.say(f"Goal {number} accepted" + (f"; next is {following['title']}" if following else "; that was the last goal"))
        self.refresh_screen(focus=True)

    def do_new_feature(self) -> None:
        def added(text: str | None) -> None:
            if text:
                goal = new_feature(self.repo, text)
                self.say(f"New goal {goal['id']}: {goal['title']}")
                self.refresh_screen(focus=True)

        self.push_screen(AskModal("What should it do next?", "A sentence is enough. It is added to the spec as a new goal."),
                         added)

    # task details, board, log ----------------------------------------------------

    def _draw_details(self) -> None:
        from openfarm_core.adapters.files import render_card_file
        from openfarm_core.blocked import records
        from openfarm_core.git import Git

        task = self._find()
        cfg = self._cfg()
        store = open_store(self.repo, cfg)
        header, body = store.read(task["id"])
        glyph, colour, words = self._state_words(task)
        out = [f"[b]{esc(task['id'])} · {esc(task['title'])}[/]   [{colour}]{glyph} {esc(words)}[/]", "",
               "[b]Spec[/]", esc(body.strip()), "", "[b]Tests[/]"]
        out += [f"  {esc(t.get('name', ''))}  [$foreground-muted]{esc(t.get('file', ''))}[/]" for t in header.get("tests") or []] or ["  none named"]
        out += ["", "[b]Files it may touch[/]", *[f"  {esc(f)}" for f in task["files"]]]
        if task["repairs"] or task["parked"]:
            out += ["", "[b]Plan checks[/]", *[f"  [$accent]↻[/] {esc(r)}" for r in task["repairs"]],
                    *[f"  [$error]✗[/] {esc(r)}" for r in task["parked"]]]
        tries = records(cfg.state_dir, task["id"])
        if task["triage"]:
            out += ["", "[b]Why it stopped[/]", esc(task["triage"]["why"])]
        out += ["", "[b]Tries[/]"] + ([
            f"  {n}  {esc(str(r.get('claimed_at') or '')[:16])}  {esc(str(r.get('final_state') or ''))}  "
            f"{esc(str(r.get('detail') or '')[:200])}" for n, r in enumerate(tries, start=1)] or ["  not built yet"])
        gates = (tries[-1].get("gates") if tries else None) or []
        if gates:
            out += ["", "[b]Checks on the last try[/]", *[
                f"  {'[$success]✓[/]' if g.get('status') == 'pass' else '[$error]✗[/]'} {esc(g.get('gate', ''))}  "
                f"[$foreground-muted]{esc(str(g.get('detail') or '')[:120])}[/]" for g in gates if isinstance(g, dict)]]
        asked = [f for f in self.facts["questions"] if f.get("from_card") == task["id"]]
        if asked:
            out += ["", "[b]Questions[/]", *[f"  [$accent]?[/] {esc(f['title'])}" for f in asked]]
        out += ["", "[b]History[/]"] + ([
            f"  {esc(e['at'][:16])}  {esc(e['by'])}  {esc(e['kind'])}  [$foreground-muted]{esc(e.get('why') or '')}[/]"
            for e in store.history(task["id"])] or ["  nothing recorded"])
        try:
            card = card_from(header, body)
            diff = Git(self.repo)._run(["diff", "--stat", "-p", f"{cfg.base_branch}...openfarm/{card.id}-{card.slug()}"],
                                       check=False).stdout
        except Exception:  # noqa: BLE001 - no branch yet means no diff
            diff = ""
        out += ["", "[b]Diff[/]"] + ([esc(diff[:6000])] if diff.strip() else ["  nothing built yet"])
        if self.fix:
            out += ["", "[b]Suggested fix, in full[/]", esc(self.fix["diff"])]
        out += ["", "[b]The task file[/]", esc(render_card_file(header, body))]
        self.query_one("#details-text", Static).update("\n".join(out))

    def _actions_details(self):
        return ("back", "Back"), [("edit_task", "Edit the task")], "Esc back · ↑↓ scroll · e edit"

    def _board_rows(self) -> dict[str, list[tuple[str, str]]]:
        columns: dict[str, list[tuple[str, str]]] = {"waiting": [], "building": [], "needs": [], "done": []}
        for task in self.facts["all_tasks"]:
            state = task["state"]
            if state == "dropped":
                continue
            key = {"done": "done", "in_progress": "building", "in_review": "needs", "blocked": "needs"}.get(state, "waiting")
            why = {"in_review": "ready to accept", "done": "accepted",
                   "blocked": STOPPED[(task["triage"] or {}).get("kind", "builder")].split(",")[0],
                   "in_progress": f"try {task['tries'] + 1}", "backlog": "can't be built yet"}.get(
                       state, self._order(task))
            goal = f"Goal {task['goal_no']}"
            if goal not in [prompt for option_id, prompt in columns[key] if not option_id]:
                columns[key].append(("", goal))
            columns[key].append((task["id"], f"[b]{esc(task['id'])}[/]\n{esc(task['title'][:22])}\n"
                                             f"[$foreground-muted]{esc(why)}[/]"))
        return columns

    def _draw_board(self) -> None:
        heads = {"waiting": "WAITING", "building": "BUILDING", "needs": "NEEDS YOU", "done": "DONE"}
        for key, rows in self._board_rows().items():
            self.query_one(f"#head-{key}", Static).update(f"{heads[key]}  {sum(1 for option_id, _p in rows if option_id)}")
            self._set_list(f"col-{key}", rows)
        planned = {t["goal_no"] for t in self.facts["all_tasks"]}
        later = [str(n) for n, g in enumerate(self.facts["goals"], start=1) if n not in planned and g["status"] != "done"]
        self.query_one("#board-note", Static).update(
            esc(f"Goal{'s' if len(later) != 1 else ''} {' and '.join(later)} "
                f"{'have' if len(later) != 1 else 'has'} no tasks yet. They are planned when the goal before is accepted.")
            if later else "")

    def _actions_board(self):
        task = self._selected_task()
        return (("open_task", f"Open {task['id']}") if task else None), [("back", "Back")], \
            "←→↑↓ move · Enter open · Esc back"

    def do_open_task(self) -> None:
        task = self._selected_task()
        if task:
            self._open("blocked" if task["state"] == "blocked" else "details", task["id"])

    @on(OptionList.OptionHighlighted)
    def _row_moved(self, event: OptionList.OptionHighlighted) -> None:
        if self.screen_name == "board" and not self.busy and not self._gone():
            self._show_actions(*self._actions()[:2])
        if self.screen_name in ("projects", "notes") and not self.busy and not self._gone() and len(self.screen_stack) == 1:
            self.refresh_screen()  # the card under the list is about the row picked

    @on(Select.Changed, "#prov-preset")
    def _preset_changed(self, event: Select.Changed) -> None:
        if self.overlay == "providers":
            self.preset_picked(str(event.value))

    @on(Select.Changed, "#prov-architect")
    def _architect_changed(self, event: Select.Changed) -> None:
        if self.overlay == "providers":
            self.architect_picked(str(event.value))

    def action_column(self, step: int) -> None:
        if self.screen_name != "board":
            return
        order = [f"col-{key}" for key in ("waiting", "building", "needs", "done")
                 if self.query_one(f"#col-{key}", OptionList).option_count]
        current = getattr(self.focused, "id", "") or ""
        if order:
            at = order.index(current) if current in order else 0
            self.query_one(f"#{order[(at + step) % len(order)]}").focus()
            self.call_after_refresh(lambda: self._show_actions(*self._actions()[:2]))

    def _draw_log(self) -> None:
        self.query_one("#log-text", Static).update(
            f"[$foreground-muted]Tokens so far: {esc(self.share)}[/]\n\n" + ("\n".join(esc(line) for line in self.activity) or "Nothing yet."))

    def _actions_log(self):
        return ("back", "Back"), [], "Esc back · ↑↓ scroll"

    def action_board(self) -> None:
        if self.facts.get("cfg") is not None and not self.busy:
            self._open("board")

    def action_log(self) -> None:
        if not self.busy:
            self.overlay = "" if self.overlay == "log" else "log"
            self.refresh_screen(focus=True)

    def action_details(self) -> None:
        self.do_details()

    def do_details(self) -> None:
        task = self._selected_task()
        if task is not None and not self.busy:
            # a stopped task's first screen says why it stopped and what to do; d goes on to everything
            self._open("blocked" if task["state"] == "blocked" and self.screen_name != "blocked" else "details", task["id"])

    def do_back(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        if self.busy:
            return self.do_cancel()
        if self.failure:
            return self.do_dismiss_failure()
        if self.overlay:
            self.overlay = "blocked" if (self.overlay == "details" and (self._find() or {}).get("state") == "blocked"
                                         and self.fix is not None) else ""
            self.refresh_screen(focus=True)
        elif self.view:
            self.action_now()

    # -- running things -----------------------------------------------------------

    def _set_busy(self, doing: str) -> None:
        self.busy, self.busy_since = doing, time.monotonic()
        self.refresh_screen(focus=not doing)

    def _step(self, step: str, doing: str, fn, template: bool = False) -> None:
        """Run one Architect step off the UI thread. A failure is shown with Retry; nothing is drafted."""
        if self.busy:
            return
        self.failure = None
        self._last_step = (step, doing, fn)
        self._template = template
        self._cancelled = False
        self._set_busy(doing if not template else f"{doing} (basic version, the Architect is not asked)")
        self._step_bg(step, fn, template)

    @work(thread=True, exit_on_error=False)
    def _step_bg(self, step: str, fn, template: bool) -> None:
        backend = None
        try:
            if not template:
                backend, reason = open_architect_backend(self.repo, self._cfg())
                if backend is None:
                    self.health["Architect"] = False
                    return self.call_from_thread(self._step_failed, step, f"The Architect can't be reached: {reason}")
                self._backend = backend
            result = fn(backend, lambda message: self.call_from_thread(self._progress, str(message)))
            if self._cancelled:
                return
            if backend is not None:
                self.health["Architect"] = True
            self.call_from_thread(self._step_done, step, result)
        except Exception as exc:  # noqa: BLE001 - every failure is shown with a way forward
            if not self._cancelled:
                if backend is not None:
                    self.health["Architect"] = False
                self.call_from_thread(self._step_failed, step,
                                      f"{'The Architect did not answer' if backend is not None else 'That did not work'}: "
                                      f"{getattr(exc, 'message', None) or exc}")
        finally:
            self._backend = None
            self._settle_architect_use(backend)
            if backend is not None and hasattr(backend, "close"):
                try:
                    backend.close()
                except Exception:  # noqa: BLE001
                    pass

    def _settle_architect_use(self, backend) -> None:
        """Calls made before the folder was a project are kept with the idea, and put in its ledger once it is one."""
        try:
            session = self._session()
            unwritten = getattr(backend, "_unwritten", None)
            owed = [*session["spent"], *[list(pair) for pair in (unwritten if isinstance(unwritten, list) else [])]]
            if isinstance(unwritten, list):
                unwritten.clear()
            cfg = self._cfg()
            if cfg is not None:
                for tokens_in, tokens_out in owed:
                    record_architect_use(cfg.state_dir, tokens_in, tokens_out)
                owed = []
            if owed != session["spent"]:
                save_session(self.repo, {**session, "spent": owed})
            self._unrecorded = sum(int(a) + int(b) for a, b in owed)
        except Exception as exc:  # noqa: BLE001 - counting never gets in the way of the work
            self.log.warning(f"could not note the Architect's tokens: {exc}")

    def _progress(self, message: str) -> None:
        if self.busy and message.strip():
            self.busy = message.strip().rstrip(".")[:90]
            self._draw_busy()

    def _step_failed(self, step: str, reason: str) -> None:
        self.busy = ""
        self.failure = {"step": step, "reason": reason}
        self.say(reason)
        self.refresh_screen(focus=True)

    def _step_done(self, step: str, result) -> None:
        self.busy = ""
        if step == "ask":
            return self._asked(result)
        if step == "spec":
            return self._spec_written(result)
        if step == "plan":
            return self._planned(result)
        if step == "iterate":
            new = result.get("new_cards") or []
            self.say(f"{len(new)} fix task(s) added under this goal: {', '.join(new)}")
        elif step == "repair":
            still = sum(1 for c in result.get("cards") or [] if not c.get("eligible"))
            self.say("Every task passes the plan checks now" if not still else f"{still} task(s) still can't be built")
        elif step == "fix":
            self.fix = result or {}
            if not result:
                self.note = "The Architect had no fix either. Edit the task yourself, or skip it."
        self.refresh_screen(focus=True)

    def do_retry(self) -> None:
        if self.failure and self.failure["step"] == "try":
            self.failure = None
            return self.do_try_again()
        step, doing, fn = self._last_step
        self._step(step, doing, fn)

    def do_template(self) -> None:
        step, doing, fn = self._last_step
        self._step(step, doing, fn, template=True)

    def do_dismiss_failure(self) -> None:
        self.failure = None
        self.refresh_screen(focus=True)

    def do_cancel(self) -> None:
        self._cancelled = True
        backend = self._backend
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass
        self.busy = ""
        self.say("Cancelled")
        self.refresh_screen(focus=True)

    def _run_cli(self, doing: str, commands: list[list[str]], done: str) -> None:
        if not self.busy:
            self._set_busy(doing)
            self._cli_bg(commands, done)

    @work(thread=True, exit_on_error=False)
    def _cli_bg(self, commands: list[list[str]], done: str) -> None:
        base = [sys.executable, "-m", "openfarm_core.cli", "--repo", str(self.repo)]
        if self.config_path is not None:
            base += ["--config", str(self.config_path)]
        source = str(Path(openfarm_core.__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": source + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
        said = done
        for argv in commands:
            proc = subprocess.run([*base, *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
            if proc.returncode != 0:
                out = (proc.stdout.strip() + "\n" + proc.stderr.strip()).strip()
                said = f"{' '.join(argv[:2])} did not work: {out.splitlines()[-1] if out else f'exit code {proc.returncode}'}"
                break
        self.call_from_thread(self.say, said)
        self.call_from_thread(self._set_busy, "")

    # -- keys ---------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Single keys never steal typing: in a text field only the Ctrl keys and Esc act."""
        if len(self.screen_stack) > 1 and action in ("go", "back", "board", "log", "details", "act", "column", "now", "step"):
            return False  # a dialog is open: its own keys act
        typing = isinstance(self.focused, (Input, TextArea)) and not self.view
        if typing and action in ("board", "log", "details", "act", "column", "go", "now", "step") and not (
                action == "go" and isinstance(self.focused, TextArea)):
            return False
        if action == "go" and isinstance(self.focused, (Button, OptionList)):
            return False  # the widget's own Enter does it
        if action == "column" and self.screen_name != "board":
            return False
        return True

    def action_go(self) -> None:
        go = self.query_one("#go", Button)
        if go.display and not go.disabled and len(self.screen_stack) == 1:
            self.action_act(self._slots["go"])

    def action_act(self, name: str) -> None:
        if self.view and name not in LOOK_ONLY:  # every button and key comes through here
            self.note = f"You are looking back: nothing changes from here. n returns to {STEPS[STEP_OF[self.now]]}."
            return self._draw_busy()
        if len(self.screen_stack) == 1 and (not self.busy or name == "cancel"):
            getattr(self, f"do_{name}")()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        self.action_act(self._slots[event.button.id])

    @on(OptionList.OptionSelected)
    def _row_chosen(self, event: OptionList.OptionSelected) -> None:
        list_id = event.option_list.id or ""
        if list_id in ("q-answered", "rail-steps", "rail-goals"):
            return
        self.action_go()  # Enter always does the main action; Space looks at the row

    def action_toggle_theme(self) -> None:
        self.theme = "openfarm-light" if self.theme == "openfarm-dark" else "openfarm-dark"

    def palette_actions(self) -> list[tuple[str, object]]:
        items = [(label.rstrip("…"), lambda name=name: self.action_act(name)) for name, label in self._more]
        items += [("Board: every task of every goal", self.action_board), ("Log: everything that happened", self.action_log),
                 ("Switch between the light and dark theme", self.action_toggle_theme)]
        items += [(f"Look at the {name} step", lambda step=step: self.look(step)) for step, name in zip(STEP_NAMES, STEPS)]
        items += [(f"Look at goal {number}: {goal['title']}", lambda number=number: self.look(goal=number))
                  for number, goal in enumerate(self.real.get("goals") or [], 1)]
        more = [(label, lambda name=name: self.open_more(name)) for name, label in MORE_LABELS]
        if self.view:
            return [("Back to where the project is", self.action_now), *items, *more, ("Quit", self.exit)]
        if self.facts.get("spec"):
            items += [("Something's wrong: ask for fix tasks", self.do_something_wrong),
                      ("New feature: add a goal", self.do_new_feature)]
        items.append(("Move this project's tasks and spec off the main branch (once)", self.offer_migration))
        return [*items, *more, ("Quit", self.exit)]
