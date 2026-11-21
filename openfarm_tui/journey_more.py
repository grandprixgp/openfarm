"""The screens beside the journey: projects, providers and sign-in, settings, and builders' notes.

They open from Ctrl+K as panes of the one screen and Esc returns to the journey. Nothing here
is a step: none of them moves the project along.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from rich.markup import escape as esc
from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, OptionList, Select, Static

import openfarm_core
from openfarm_core.acp_setup import inspect_acp_status
from openfarm_core.doctor import run_doctor
from openfarm_core.errors import AdapterError, ConfigError, Refused
from openfarm_core.findings import decide_finding, list_findings
from openfarm_core.projects import edit_project, list_projects, register_project, remove_project
from openfarm_core.providers import (
    VALID_FRONTIER_PROVIDERS,
    WORKER_PRESETS,
    load_global_providers,
    probe_worker_endpoint,
    save_global_providers,
)

MORE = {"projects": "Projects", "providers": "Providers and sign-in", "settings": "Settings", "notes": "Notes from builders"}
MORE_LABELS = (
    ("projects", "Projects: switch, add or remove a project"),
    ("providers", "Providers and sign-in"),
    ("settings", "Settings and check-up"),
    ("notes", "Questions and notes from builders (all of them)"),
)
_KEEPS = ((True, "Ask the provider not to keep what is sent (zero data retention)"),
          (False, "Let the provider keep what is sent"))


class MorePanes:
    """Mixed into the journey app: the panes, what they show and what their buttons do."""

    def compose_more(self) -> ComposeResult:
        with VerticalScroll(id="projects"):
            yield Static("Projects", classes="h1")
            yield Static("Every folder openfarm knows. Opening one leaves the others as they are.", classes="sub")
            yield OptionList(id="proj-list")
            yield Static("", id="proj-detail", classes="card")
        with VerticalScroll(id="providers"):
            yield Static("Providers and sign-in", classes="h1")
            yield Static("Who plans and who builds. The same for every project.", classes="sub")
            yield Static("ARCHITECT  plans, reviews and answers builders", classes="label")
            yield Select([(name.capitalize(), name) for name in VALID_FRONTIER_PROVIDERS], id="prov-architect",
                         allow_blank=False)
            yield Static("", id="prov-signin", classes="card")
            yield Static("BUILDERS  write the code", classes="label")
            yield Select([(preset["label"], preset["id"]) for preset in WORKER_PRESETS], id="prov-preset", allow_blank=False)
            yield Input(placeholder="Address the builders' requests go to (https://…)", id="prov-endpoint")
            yield Input(placeholder="Model", id="prov-model")
            yield Input(placeholder="API key (empty keeps the saved one)", password=True, id="prov-key")
            yield Select([(label, keep) for keep, label in _KEEPS], id="prov-zdr", allow_blank=False)
            yield Static("", id="prov-status", classes="card")
        with VerticalScroll(id="settings"):
            yield Static("Settings", classes="h1")
            yield Static("", id="set-project", classes="card")
            yield Static("", id="set-doctor", classes="card")
        with VerticalScroll(id="notes"):
            yield Static("Notes from builders", classes="h1")
            yield Static("What builders noticed and nobody has decided on yet. A question that stops a task is asked on "
                         "the Build step.", classes="sub")
            yield OptionList(id="notes-list")
            yield Static("", id="notes-detail", classes="card")

    def open_more(self, name: str) -> None:
        if self.busy or self.failure or len(self.screen_stack) > 1:
            return
        self.view = None
        self.overlay = name
        self.note = ""
        if name == "providers":
            self._load_providers()
        self.refresh_screen(focus=True)

    # projects ------------------------------------------------------------------

    def _projects(self) -> list[dict]:
        try:
            return list_projects()
        except Exception:  # noqa: BLE001 - an unreadable list is shown as an empty one
            return []

    def _is_open(self, item: dict) -> bool:
        return Path(item["repo"]).expanduser().resolve() == Path(self.repo).resolve()

    def _project(self) -> dict | None:
        wanted = self._picked("proj-list")
        return next((p for p in self._projects() if p["id"] == wanted), None)

    def _draw_projects(self) -> None:
        items = self._projects()
        rows = []
        for item in items:
            state = "ready" if item.get("configured") else ("empty folder" if item.get("repo_exists") else "folder missing")
            mark = "●" if self._is_open(item) else " "
            rows.append((item["id"], f"{mark} {esc(item['name'][:28]):<28}  {state:<14}  {esc(str(item['repo'])[-44:])}"))
        self._set_list("proj-list", rows)
        item = self._project()
        detail = self.query_one("#proj-detail", Static)
        if item is None:
            return detail.update("No projects yet. Add a folder: an empty one starts at the Idea step.")
        where = ("[$success]has a project[/]" if item.get("configured") else
                 "an empty folder, starts at the Idea step" if item.get("repo_exists") else "[$error]the folder is not there[/]")
        detail.update(
            f"[b]{esc(item['name'])}[/]{'   [$success]open now[/]' if self._is_open(item) else ''}\n"
            f"Folder       {esc(str(item['repo']))}\n"
            f"State        {where}\n"
            f"Stack        {esc(item.get('language') or 'chosen at the Idea step')}\n"
            f"Idea         {esc(item.get('idea') or 'none noted')}\n"
            "[$foreground-muted]Removing a project only takes it off this list. Its files are never touched.[/]")

    def _actions_projects(self):
        item = self._project()
        others = [("project_add", "Add a folder…")]
        if item is None:
            return ("back", "Back"), others, "Esc back"
        others += [("project_rename", "Rename…"), ("project_remove", "Remove from the list…")]
        label = "Back to it" if self._is_open(item) else f"Open {item['name'][:24]}"
        return ("project_open", label), others, "↑↓ pick · Enter open · Esc back"

    def do_project_open(self) -> None:
        item = self._project()
        if item is None or self._is_open(item):
            return self.action_back()
        if not Path(item["repo"]).expanduser().is_dir():
            self.note = "That folder is not there any more. Remove it from the list, or put the folder back."
            return self._draw_busy()
        self.exit(("project", str(item["repo"])))

    def do_project_add(self) -> None:
        def done(text: str | None) -> None:
            if not (text or "").strip():
                return
            folder = Path(text.strip()).expanduser().resolve()
            try:
                folder.mkdir(parents=True, exist_ok=True)
                entry = register_project(folder, name=folder.name)
            except (OSError, Refused, ConfigError) as exc:
                self.note = f"Could not add {folder}: {getattr(exc, 'message', None) or exc}"
            else:
                self.say(f"Added {entry['name']} to the projects")
                self._lists.pop("proj-list", None)
            self.refresh_screen(focus=True)

        from .journey import AskModal

        self.push_screen(AskModal("Which folder?", "Its full path. A folder that is not there yet is made, empty. "
                                                   "Nothing in an existing folder is changed."), done)

    def do_project_rename(self) -> None:
        item = self._project()
        if item is None:
            return

        def done(text: str | None) -> None:
            if not (text or "").strip():
                return
            try:
                edit_project(item["id"], name=text.strip())
            except (OSError, Refused, ConfigError) as exc:
                self.note = f"Could not rename it: {getattr(exc, 'message', None) or exc}"
            else:
                self.say(f"{item['name']} is now called {text.strip()}")
                self._lists.pop("proj-list", None)
            self.refresh_screen()

        from .journey import AskModal

        self.push_screen(AskModal(f"New name for {item['name']}", "Only the name in this list changes.", item["name"]), done)

    def do_project_remove(self) -> None:
        item = self._project()
        if item is None:
            return
        if self._is_open(item):
            self.note = "This is the project that is open. Open another one first."
            return self._draw_busy()

        def done(choice: str | None) -> None:
            if choice != "remove":
                return
            try:
                remove_project(item["id"])
            except (OSError, Refused, ConfigError) as exc:
                self.note = f"Could not remove it: {getattr(exc, 'message', None) or exc}"
            else:
                self.say(f"{item['name']} is off the list. Its files are untouched.")
                self._lists.pop("proj-list", None)
            self.refresh_screen()

        from .journey import ChoiceModal

        self.push_screen(ChoiceModal(f"Take {item['name']} off the list?",
                                     f"{item['repo']}\n\nThe folder and everything in it stay exactly as they are. "
                                     "You can add it again at any time.",
                                     [("remove", "Remove from the list"), ("keep", "Keep it")]), done)

    # providers -----------------------------------------------------------------

    def _load_providers(self) -> None:
        saved = load_global_providers()
        builders = saved["worker"]
        self._prov_preset = builders["preset"]
        self._prov_note, self._signin = "", None
        self.query_one("#prov-architect", Select).value = saved["frontier"]["provider"]
        self.query_one("#prov-preset", Select).value = builders["preset"]
        self.query_one("#prov-endpoint", Input).value = builders.get("endpoint") or ""
        self.query_one("#prov-model", Input).value = builders.get("model") or ""
        self.query_one("#prov-key", Input).value = ""
        self.query_one("#prov-zdr", Select).value = bool(builders.get("zdr", True))
        self._check_signin(saved["frontier"]["provider"])

    @work(thread=True, exit_on_error=False, group="signin", exclusive=True)
    def _check_signin(self, provider: str) -> None:
        try:
            status = inspect_acp_status(self.repo, provider=provider)
            ready = bool(status.get("oauth_ready") or status.get("ok"))
        except Exception:  # noqa: BLE001 - not knowing is shown as not signed in
            ready = False
        self.call_from_thread(self._signin_known, provider, ready)

    def _signin_known(self, provider: str, ready: bool) -> None:
        self._signin = (provider, ready)
        if not self._gone() and self.overlay == "providers":
            self.refresh_screen()

    def preset_picked(self, preset_id: str) -> None:
        """A builder preset chosen by the user fills in its own address and model."""
        if preset_id == self._prov_preset:
            return
        self._prov_preset = preset_id
        preset = next((p for p in WORKER_PRESETS if p["id"] == preset_id), None)
        if preset:
            if preset["endpoint"]:
                self.query_one("#prov-endpoint", Input).value = preset["endpoint"]
            if preset["default_model"]:
                self.query_one("#prov-model", Input).value = preset["default_model"]
            self.query_one("#prov-zdr", Select).value = bool(preset["zdr"])

    def architect_picked(self, provider: str) -> None:
        if self._signin is None or self._signin[0] != provider:
            self._signin = None
            self._check_signin(provider)
            self.refresh_screen()

    def _draw_providers(self) -> None:
        provider = str(self.query_one("#prov-architect", Select).value)
        signin = self.query_one("#prov-signin", Static)
        known = self._signin if self._signin and self._signin[0] == provider else None
        signin.set_class(bool(known and known[1]), "ok")
        if provider == "antigravity":
            signin.update("Antigravity only runs when a person starts it, so it can't plan here. Choose another.")
        elif known is None:
            signin.update(f"[$foreground-muted]Checking the {provider.capitalize()} sign-in…[/]")
        elif known[1]:
            signin.update(f"[$success]✓[/] {provider.capitalize()} is signed in")
        else:
            signin.update(f"[$warning]○[/] {provider.capitalize()} is not signed in. Sign in opens its own sign-in and comes back here.")
        saved = load_global_providers()["worker"]
        key = "a key is saved" if saved.get("key_present") else "no key saved yet"
        self.query_one("#prov-status", Static).update(self._prov_note or f"[$foreground-muted]Not tested since this opened · {key}[/]")

    def _actions_providers(self):
        return ("providers_save", "Save"), [("providers_test", "Test the builders' connection"), ("signin", "Sign in…")], \
            "Tab next field · Esc back"

    def _provider_form(self) -> dict:
        return {"frontier": str(self.query_one("#prov-architect", Select).value),
                "preset": str(self.query_one("#prov-preset", Select).value),
                "endpoint": self.query_one("#prov-endpoint", Input).value.strip(),
                "model": self.query_one("#prov-model", Input).value.strip(),
                "api_key": self.query_one("#prov-key", Input).value.strip(),
                "zdr": bool(self.query_one("#prov-zdr", Select).value)}

    def do_providers_save(self) -> None:
        form = self._provider_form()
        try:
            save_global_providers(frontier_provider=form["frontier"], worker_preset=form["preset"], endpoint=form["endpoint"],
                                  model=form["model"], api_key=form["api_key"], zdr=form["zdr"])
        except (Refused, AdapterError, ConfigError, OSError) as exc:
            self._prov_note = f"[$error]Not saved:[/] {esc(str(getattr(exc, 'message', None) or exc))}"
        else:
            self.query_one("#prov-key", Input).value = ""
            self._prov_note = "[$success]✓[/] Saved. Every project uses this from its next step."
            self.say("Saved the providers")
        self.refresh_screen()

    def do_providers_test(self) -> None:
        if not self.busy:
            self._set_busy("Asking the builders' provider for its models")
            self._probe_bg(self._provider_form())

    @work(thread=True, exit_on_error=False)
    def _probe_bg(self, form: dict) -> None:
        try:
            key_file = None if form["api_key"] else Path(load_global_providers()["worker"]["key_file"])
            result = probe_worker_endpoint(form["endpoint"], key_file=key_file, api_key=form["api_key"], model=form["model"],
                                           zdr=form["zdr"])
            why = str(result.get("error") or f"HTTP {result.get('status')}")
            if result["ok"]:
                said = f"[$success]✓[/] The provider answered in {result['latency_ms']} ms"
            else:
                said = f"[$error]✗[/] No answer: {esc(why)}"
        except Exception as exc:  # noqa: BLE001 - shown, never raised
            said = f"[$error]✗[/] Could not test it: {esc(str(exc))}"
        self.call_from_thread(self._probed, said)

    def _probed(self, said: str) -> None:
        self._prov_note = said
        self._set_busy("")

    def do_signin(self) -> None:
        provider = self._provider_form()["frontier"]
        if provider == "antigravity":
            return
        source = str(Path(openfarm_core.__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": source + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
        with self.suspend():  # the sign-in is the provider's own, in the terminal
            subprocess.run([sys.executable, "-m", "openfarm_core.cli", "--repo", str(self.repo), "acp", "login",
                            "--provider", provider], env=env)
        self._signin = None
        self._check_signin(provider)
        self.refresh_screen()

    # settings ------------------------------------------------------------------

    def _draw_settings(self) -> None:
        cfg = self.facts.get("cfg")
        saved = load_global_providers()
        theme = "light" if self.theme == "openfarm-light" else "dark"
        lines = [f"[b]{esc(self.facts['name'])}[/]", f"Folder       {esc(str(self.repo))}"]
        if cfg is None:
            lines.append("This folder is not a project yet. It becomes one when the spec is written.")
        else:
            never = ", ".join(getattr(cfg, "never_touch", ()) or ()) or "nothing"
            lines += [f"State        {esc(str(cfg.state_dir))}",
                      f"Architect    {esc(str(getattr(cfg, 'micromanager_adapter', '') or saved['frontier']['provider']))}",
                      f"Builders     {esc(str(getattr(cfg, 'endpoint', '') or saved['worker']['endpoint'] or 'not set'))}",
                      f"Never touch  {esc(never)}"]
        lines += [f"Theme        {theme}", f"Tokens       {esc(self.share)}"]
        self.query_one("#set-project", Static).update("\n".join(lines))
        report = self._doctor
        box = self.query_one("#set-doctor", Static)
        if report is None:
            return box.update("[b]Check-up[/]\n[$foreground-muted]Not run yet. It looks at the folder, the sign-ins and the "
                              "settings, and changes nothing.[/]")
        marks = {"ok": "[$success]✓[/]", "warn": "[$warning]![/]", "fail": "[$error]✗[/]"}
        rows = ["[b]Check-up[/]"]
        for check in report.get("checks") or []:
            rows.append(f"{marks.get(check.get('status'), '·')} {esc(str(check.get('label')))}  "
                        f"[$foreground-muted]{esc(str(check.get('detail')))}[/]"
                        f"{'  can be fixed' if check.get('fixable') else ''}")
        rows += [f"[$success]+[/] fixed: {esc(str(item))}" for item in report.get("fixes_applied") or []]
        box.update("\n".join(rows))

    def _actions_settings(self):
        others = [("toggle_theme", "Light or dark")]
        if self._doctor and any(c.get("fixable") for c in self._doctor.get("checks") or []):
            others.insert(0, ("checkup_fix", "Fix what can be fixed"))
        return ("checkup", "Run the check-up"), others, "Esc back"

    def do_toggle_theme(self) -> None:
        self.action_toggle_theme()
        self.refresh_screen()

    def do_checkup(self, fix: bool = False) -> None:
        if not self.busy:
            self._set_busy("Fixing what can be fixed" if fix else "Checking the folder, the sign-ins and the settings")
            self._checkup_bg(fix)

    def do_checkup_fix(self) -> None:
        self.do_checkup(fix=True)

    @work(thread=True, exit_on_error=False)
    def _checkup_bg(self, fix: bool) -> None:
        try:
            report = run_doctor(self.repo, config_path=self.config_path, fix=fix)
        except Exception as exc:  # noqa: BLE001 - shown as a failed check
            report = {"checks": [{"status": "fail", "label": "The check-up itself", "detail": str(exc)}]}
        self.call_from_thread(self._checked, report)

    def _checked(self, report: dict) -> None:
        self._doctor = report
        self._set_busy("")

    # notes ---------------------------------------------------------------------

    def _notes(self) -> list[dict]:
        cfg = self.facts.get("cfg")
        if cfg is None:
            return []
        try:
            return list_findings(self.repo, cfg, state_filter="waiting").get("findings") or []
        except Exception:  # noqa: BLE001
            return []

    def _note_item(self) -> dict | None:
        wanted = self._picked("notes-list")
        return next((n for n in self._notes() if n["id"] == wanted), None)

    def _draw_notes(self) -> None:
        notes = self._notes()
        self._set_list("notes-list", [(n["id"], f"{esc(str(n.get('from_card') or '')):<8} {esc(str(n['title'])[:90])}")
                                      for n in notes])
        item = self._note_item()
        detail = self.query_one("#notes-detail", Static)
        if item is None:
            return detail.update("Nothing is waiting. Builders' notes show up here as they are written.")
        stops = "  [$warning]stops that task until it is answered[/]" if item.get("holds_up") else ""
        detail.update(f"[b]{esc(str(item['title']))}[/]\nFrom {esc(str(item.get('from_card') or 'a builder'))}{stops}\n\n"
                      f"{esc(str(item.get('body') or '').strip() or 'No more was written.')}")

    def _actions_notes(self):
        if self._note_item() is None:
            return ("back", "Back"), [], "Esc back"
        return ("note_keep", "Keep for the next goal"), [("note_dismiss", "Dismiss…")], "↑↓ pick · Esc back"

    def _decide_note(self, decision: str, reason: str, said: str) -> None:
        item = self._note_item()
        if item is None:
            return
        try:
            for finding_id in item.get("duplicate_ids") or [item["id"]]:  # the same note filed again goes with it
                decide_finding(self.repo, self.facts["cfg"], finding_id, decision, reason=reason)
        except Exception as exc:  # noqa: BLE001
            self.note = f"Could not record that: {getattr(exc, 'message', None) or exc}"
        else:
            self.say(f"{said}: {item['title']}")
            self._lists.pop("notes-list", None)
        self.refresh_screen()

    def do_note_keep(self) -> None:
        self._decide_note("next-wave", "", "Kept for the next goal")

    def do_note_dismiss(self) -> None:
        item = self._note_item()
        if item is None:
            return
        from .journey import AskModal

        self.push_screen(AskModal(f"Why dismiss “{item['title'][:60]}”?", "Kept in the history with your reason."),
                         lambda text: self._decide_note("dismissed", text.strip(), "Dismissed") if (text or "").strip() else None)
