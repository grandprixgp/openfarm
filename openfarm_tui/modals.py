"""Modal dialogs for Openfarm's Textual Workbench (dismiss, fold, full finding, bounce, hint)."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, Switch, TextArea
from textual.widgets.option_list import Option

from .markup import esc


_MODAL_CSS = """
ModalScreen {
    align: center middle;
    background: rgba(6, 9, 15, 0.80);
}
.modal-box {
    width: 76;
    max-width: 92%;
    height: auto;
    max-height: 85%;
    background: #111824;
    border: round #38bdf8;
    padding: 1 2;
}
.modal-title {
    color: #38bdf8;
    text-style: bold;
    margin-bottom: 1;
}
.modal-sub {
    color: #cbd5e1;
    margin-bottom: 1;
}
.modal-actions {
    height: 1;
    align: right middle;
    margin-top: 1;
}
.modal-actions Button {
    width: auto;
    min-width: 10;
    height: 1;
    border: none;
    padding: 0 2;
    margin-left: 1;
}
.btn-ok {
    background: #38bdf8;
    color: #08101a;
    text-style: bold;
}
.btn-cancel {
    background: #1b2638;
    color: #e2e8f0;
}
"""


class DismissFindingModal(ModalScreen[str | None]):
    """Prompt for an optional one-line reason when dismissing a finding (`x`)."""

    CSS = _MODAL_CSS

    def __init__(self, finding_id: str, title: str) -> None:
        super().__init__()
        self._fid = finding_id
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(f"Dismiss Finding {self._fid}", classes="modal-title", markup=False)
            yield Static(self._title, classes="modal-sub", markup=False)
            yield Input(placeholder="Optional one-line reason (Enter to dismiss)", id="dismiss-reason")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Dismiss \\[Enter]", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#dismiss-reason", Input).focus()

    @on(Input.Submitted, "#dismiss-reason")
    def _submit_input(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#dismiss-reason", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class FoldFindingModal(ModalScreen[str | None]):
    """Select an existing card to fold a finding into (`c`)."""

    CSS = _MODAL_CSS + "\nOptionList { height: 10; background: #0b1019; border: round #2b3b54; }"

    def __init__(self, finding_id: str, title: str, cards: list[tuple[str, str]]) -> None:
        super().__init__()
        self._fid = finding_id
        self._title = title
        self._cards = cards

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(f"Fold {self._fid} Into Existing Card", classes="modal-title", markup=False)
            yield Static(self._title, classes="modal-sub", markup=False)
            opts = [Option(esc(f"{cid}  ·  {ctitle}"), id=cid) for cid, ctitle in self._cards]
            yield OptionList(*opts, id="fold-options")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Fold \\[Enter]", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        ol = self.query_one("#fold-options", OptionList)
        ol.focus()

    def _selected_id(self) -> str | None:
        ol = self.query_one("#fold-options", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._cards):
            return self._cards[idx][0]
        return self._cards[0][0] if self._cards else None

    @on(OptionList.OptionSelected, "#fold-options")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id) if event.option.id else self._selected_id())

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self._selected_id())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class FullFindingModal(ModalScreen[None]):
    """Full-screen scrollable view of one finding (`Enter`)."""

    CSS = _MODAL_CSS + "\n.modal-box { width: 96; height: 32; }\n#full-body { height: 1fr; background: #0b1019; border: round #2b3b54; padding: 1 2; }"

    def __init__(self, finding: dict) -> None:
        super().__init__()
        self._finding = finding

    def compose(self) -> ComposeResult:
        f = self._finding
        with Vertical(classes="modal-box"):
            yield Static(f"{f['id']}  ·  {f['title']}", classes="modal-title", markup=False)
            meta = (
                f"From: {f['from_card']} ({f['from_card_title']})  ·  "
                f"Created: {f.get('created_at') or '—'}  ·  Decision: {f.get('decision') or 'waiting'}"
            )
            yield Static(meta, classes="modal-sub", markup=False)
            with VerticalScroll(id="full-body"):
                yield Static(f.get("body") or "(no explanation body)", markup=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Close \\[Esc]", id="close", classes="btn-ok")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class BounceNoteModal(ModalScreen[str | None]):
    """Multi-line bounce feedback modal (`b` in Review)."""

    CSS = _MODAL_CSS + "\nTextArea { height: 7; background: #0b1019; border: round #38bdf8; }"

    def __init__(self, card_id: str, title: str, *, heading: str = "", sub: str = "",
                 ok_label: str = "Bounce Card") -> None:
        super().__init__()
        self._cid = card_id
        self._title = title
        self._heading = heading or f"Bounce {card_id} With Feedback"
        self._sub = sub or title
        self._ok_label = ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(self._heading, classes="modal-title", markup=False)
            yield Static(self._sub, classes="modal-sub", markup=False)
            yield TextArea("", id="bounce-note")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button(esc(self._ok_label), id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#bounce-note", TextArea).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#bounce-note", TextArea).text.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class HintModal(ModalScreen[str | None]):
    """Send a one-line live hint (`h` during active run)."""

    CSS = _MODAL_CSS

    def __init__(self, card_id: str) -> None:
        super().__init__()
        self._cid = card_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(f"Send Hint for {self._cid or 'Active Run'}", classes="modal-title", markup=False)
            yield Input(placeholder="Short guidance for the active worker/supervisor...", id="hint-input")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Send \\[Enter]", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#hint-input", Input).focus()

    @on(Input.Submitted, "#hint-input")
    def _sub(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#hint-input", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class NewProjectModal(ModalScreen[dict | None]):
    """Add an existing directory or initialize a new project in Openfarm (`n` in Projects)."""

    CSS = _MODAL_CSS + """
    .modal-box { width: 82; }
    .field-lbl { color: #94a3b8; margin-top: 1; }
    .switch-row { height: 3; align: left middle; margin-top: 1; }
    .switch-lbl { width: auto; color: #cbd5e1; margin-left: 1; }
    """

    def __init__(self, default_dir: str = "") -> None:
        super().__init__()
        self._default_dir = default_dir

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static("Add / Register Project in Openfarm", classes="modal-title")
            yield Static("Point to an existing repo folder or a new path to track in Openfarm.", classes="modal-sub")
            yield Label("Project Name", classes="field-lbl")
            yield Input(placeholder="e.g. photo-of-the-day", id="np-name")
            yield Label("Directory Path", classes="field-lbl")
            yield Input(value=self._default_dir, placeholder="/home/user/projects/my-app", id="np-repo")
            yield Label("Language Stack (typescript | python | rust | go | auto)", classes="field-lbl")
            yield Input(value="typescript", id="np-lang")
            yield Label("Short Idea / Notes (optional)", classes="field-lbl")
            yield Input(placeholder="What this project does...", id="np-idea")
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="np-init")
                yield Static("Initialize new Openfarm scaffold if directory is empty", classes="switch-lbl")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Save Project", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#np-name", Input).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss({
            "name": self.query_one("#np-name", Input).value.strip(),
            "repo": self.query_one("#np-repo", Input).value.strip(),
            "language": self.query_one("#np-lang", Input).value.strip() or "typescript",
            "idea": self.query_one("#np-idea", Input).value.strip(),
            "initialize": bool(self.query_one("#np-init", Switch).value),
        })

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class EditProjectModal(ModalScreen[dict | None]):
    """Edit registered Openfarm project metadata (`e` in Projects)."""

    CSS = _MODAL_CSS + """
    .modal-box { width: 82; }
    .field-lbl { color: #94a3b8; margin-top: 1; }
    """

    def __init__(self, project: dict) -> None:
        super().__init__()
        self._project = dict(project)

    def compose(self) -> ComposeResult:
        p = self._project
        with Vertical(classes="modal-box"):
            yield Static(f"Edit Project: {p.get('id', '')}", classes="modal-title", markup=False)
            yield Static("Updates Openfarm registry (and [project] metadata in .openfarm/config.toml if present).", classes="modal-sub", markup=False)
            yield Label("Display Name", classes="field-lbl")
            yield Input(value=str(p.get("name") or ""), id="ep-name")
            yield Label("Repository Path", classes="field-lbl")
            yield Input(value=str(p.get("repo") or ""), id="ep-repo")
            yield Label("Language Stack", classes="field-lbl")
            yield Input(value=str(p.get("language") or ""), id="ep-lang")
            yield Label("Base Branch", classes="field-lbl")
            yield Input(value=str(p.get("base_branch") or "main"), id="ep-branch")
            yield Label("Idea / Notes", classes="field-lbl")
            yield Input(value=str(p.get("idea") or ""), id="ep-idea")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Save Changes", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#ep-name", Input).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss({
            "name": self.query_one("#ep-name", Input).value.strip(),
            "repo": self.query_one("#ep-repo", Input).value.strip(),
            "language": self.query_one("#ep-lang", Input).value.strip(),
            "base_branch": self.query_one("#ep-branch", Input).value.strip(),
            "idea": self.query_one("#ep-idea", Input).value.strip(),
        })

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class DeleteProjectModal(ModalScreen[dict | None]):
    """Confirm removing a project from Openfarm without touching disk files (`d` in Projects)."""

    CSS = _MODAL_CSS + """
    .modal-box { width: 84; border: round #fbbf24; }
    .safe-banner {
        background: #0f2e1f;
        border: round #4ade80;
        color: #bbf7d0;
        padding: 0 2;
        margin-bottom: 1;
    }
    .switch-row { height: 3; align: left middle; margin-top: 1; }
    .switch-lbl { width: auto; color: #cbd5e1; margin-left: 1; }
    .btn-danger { background: #fb7185; color: #0b0f17; text-style: bold; }
    """

    def __init__(self, project: dict) -> None:
        super().__init__()
        self._project = dict(project)

    def compose(self) -> ComposeResult:
        p = self._project
        state_dir = str(p.get("state_dir") or "(none)")
        with Vertical(classes="modal-box"):
            yield Static(f"Remove Project From Openfarm: {p.get('name', p.get('id', ''))}", classes="modal-title", markup=False)
            yield Static(
                "SAFE & NON-DESTRUCTIVE: Your project directory and all files inside\n"
                f"{p.get('repo', '')}\n"
                "will NOT be deleted or modified on disk.",
                classes="safe-banner",
                markup=False,
            )
            yield Static(f"External Openfarm state cache: {state_dir}", classes="modal-sub", markup=False)
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="dp-clear-state")
                yield Static("Also clear Openfarm's external state cache under ~/.openfarm/", classes="switch-lbl")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Remove From Openfarm", id="ok", classes="btn-danger")

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss({
            "confirm": True,
            "clear_state_dir": bool(self.query_one("#dp-clear-state", Switch).value),
        })

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class WorkspaceDiscoveryModal(ModalScreen[dict | None]):
    """Read-only workspace scanner (`[S]` in Projects) to register/adopt discovered repos."""

    CSS = _MODAL_CSS + "\n.modal-box { width: 92; }\nOptionList { height: 12; background: #0b1019; border: round #2b3b54; }"

    def __init__(self, root_label: str, items: list[dict]) -> None:
        super().__init__()
        self._root_label = root_label
        self._items = items

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(f"Discovered Projects in {self._root_label}", classes="modal-title", markup=False)
            yield Static("Read-only scan. Select a folder to Register (or Adopt if git-repo).", classes="modal-sub")
            opts = [
                Option(
                    esc(f"{p['name']:<24} [{p['kind']:<10}] {(p.get('detected_language') or '—'):<10} {'✓ registered' if p['registered'] else ''}"),
                    id=str(idx),
                )
                for idx, p in enumerate(self._items)
            ]
            yield OptionList(*opts, id="disc-options")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Register Selected", id="register", classes="btn-ok")
                yield Button("Adopt Selected", id="adopt", classes="btn-ok")

    def on_mount(self) -> None:
        if self._items:
            self.query_one("#disc-options", OptionList).focus()

    def _selected(self) -> dict | None:
        if not self._items:
            return None
        ol = self.query_one("#disc-options", OptionList)
        idx = ol.highlighted if ol.highlighted is not None else 0
        return self._items[idx] if 0 <= idx < len(self._items) else self._items[0]

    @on(Button.Pressed, "#register")
    def _reg(self) -> None:
        sel = self._selected()
        self.dismiss({"action": "register", "item": sel} if sel else None)

    @on(Button.Pressed, "#adopt")
    def _adopt(self) -> None:
        sel = self._selected()
        self.dismiss({"action": "adopt", "item": sel} if sel else None)

    @on(OptionList.OptionSelected, "#disc-options")
    def _picked(self) -> None:
        sel = self._selected()
        self.dismiss({"action": "register", "item": sel} if sel else None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class CardEditorModal(ModalScreen[dict | None]):
    """In-TUI Card Editor (`[e]` in Plan view) with live eligibility fields."""

    CSS = _MODAL_CSS + "\n.modal-box { width: 92; }\n.field-lbl { color: #94a3b8; margin-top: 1; }\nTextArea { height: 8; background: #0b1019; border: round #334663; }"

    def __init__(self, card_entry: dict) -> None:
        super().__init__()
        self._card = dict(card_entry)

    def compose(self) -> ComposeResult:
        c = self._card
        with Vertical(classes="modal-box"):
            yield Static(f"Edit Card: {c.get('id', '')}", classes="modal-title", markup=False)
            yield Label("Title", classes="field-lbl")
            yield Input(value=str(c.get("title") or ""), id="ce-title")
            yield Label("Category (small-feature | feature | pinned-bugfix | refactor | test-backfill)", classes="field-lbl")
            yield Input(value=str(c.get("category") or "small-feature"), id="ce-cat")
            yield Label("Touches (comma-separated relative file paths)", classes="field-lbl")
            yield Input(value=", ".join(c.get("touches") or []), id="ce-touches")
            yield Label("Depends (comma-separated card IDs)", classes="field-lbl")
            yield Input(value=", ".join(c.get("depends") or []), id="ce-depends")
            yield Label("Done Specification (Markdown)", classes="field-lbl")
            yield TextArea(str(c.get("done_text") or ""), id="ce-done")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Save & Check Eligibility", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#ce-title", Input).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss({
            "title": self.query_one("#ce-title", Input).value.strip(),
            "category": self.query_one("#ce-cat", Input).value.strip(),
            "touches": [t.strip() for t in self.query_one("#ce-touches", Input).value.split(",") if t.strip()],
            "depends": [d.strip() for d in self.query_one("#ce-depends", Input).value.split(",") if d.strip()],
            "done_text": self.query_one("#ce-done", TextArea).text.strip(),
        })

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class SplitCardModal(ModalScreen[dict | None]):
    """Split a large card into two chained child cards (`[x]` in Plan view)."""

    CSS = _MODAL_CSS + "\n.modal-box { width: 86; }\n.field-lbl { color: #94a3b8; margin-top: 1; }"

    def __init__(self, card_entry: dict) -> None:
        super().__init__()
        self._card = dict(card_entry)

    def compose(self) -> ComposeResult:
        c = self._card
        touches = list(c.get("touches") or [])
        t1 = touches[0] if touches else ""
        t2 = touches[1] if len(touches) > 1 else t1
        with Vertical(classes="modal-box"):
            yield Static(f"Split Card {c.get('id', '')} Into Two Chained Cards", classes="modal-title", markup=False)
            yield Static(f"Creates {c.get('id', '')}a and {c.get('id', '')}b (with b depending on a).", classes="modal-sub", markup=False)
            yield Label("Part A Title", classes="field-lbl")
            yield Input(value=f"{c.get('title', '')} (Part A)", id="sc-t1")
            yield Label("Part A Touches (comma-separated)", classes="field-lbl")
            yield Input(value=t1, id="sc-f1")
            yield Label("Part B Title", classes="field-lbl")
            yield Input(value=f"{c.get('title', '')} (Part B)", id="sc-t2")
            yield Label("Part B Touches (comma-separated)", classes="field-lbl")
            yield Input(value=t2, id="sc-f2")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel \\[Esc]", id="cancel", classes="btn-cancel")
                yield Button("Split Card", id="ok", classes="btn-ok")

    def on_mount(self) -> None:
        self.query_one("#sc-t1", Input).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss({
            "first_title": self.query_one("#sc-t1", Input).value.strip(),
            "first_touches": [t.strip() for t in self.query_one("#sc-f1", Input).value.split(",") if t.strip()],
            "second_title": self.query_one("#sc-t2", Input).value.strip(),
            "second_touches": [t.strip() for t in self.query_one("#sc-f2", Input).value.split(",") if t.strip()],
        })

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)
