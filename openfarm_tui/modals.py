"""Modal dialogs for Openfarm's Textual Workbench (dismiss, fold, full finding, bounce, hint)."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option


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
            yield Static(f"Dismiss Finding {self._fid}", classes="modal-title")
            yield Static(self._title, classes="modal-sub")
            yield Input(placeholder="Optional one-line reason (Enter to dismiss)", id="dismiss-reason")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel [Esc]", id="cancel", classes="btn-cancel")
                yield Button("Dismiss [Enter]", id="ok", classes="btn-ok")

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
            yield Static(f"Fold {self._fid} Into Existing Card", classes="modal-title")
            yield Static(self._title, classes="modal-sub")
            opts = [Option(f"{cid}  ·  {ctitle}", id=cid) for cid, ctitle in self._cards]
            yield OptionList(*opts, id="fold-options")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel [Esc]", id="cancel", classes="btn-cancel")
                yield Button("Fold [Enter]", id="ok", classes="btn-ok")

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
            yield Static(f"{f['id']}  ·  {f['title']}", classes="modal-title")
            meta = (
                f"From: {f['from_card']} ({f['from_card_title']})  ·  "
                f"Created: {f.get('created_at') or '—'}  ·  Decision: {f.get('decision') or 'waiting'}"
            )
            yield Static(meta, classes="modal-sub")
            with VerticalScroll(id="full-body"):
                yield Static(f.get("body") or "(no explanation body)", markup=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Close [Esc]", id="close", classes="btn-ok")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class BounceNoteModal(ModalScreen[str | None]):
    """Multi-line bounce feedback modal (`b` in Review)."""

    CSS = _MODAL_CSS + "\nTextArea { height: 7; background: #0b1019; border: round #38bdf8; }"

    def __init__(self, card_id: str, title: str) -> None:
        super().__init__()
        self._cid = card_id
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(f"Bounce {self._cid} With Feedback", classes="modal-title")
            yield Static(self._title, classes="modal-sub")
            yield TextArea("", id="bounce-note")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel [Esc]", id="cancel", classes="btn-cancel")
                yield Button("Bounce Card", id="ok", classes="btn-ok")

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
            yield Static(f"Send Hint for {self._cid or 'Active Run'}", classes="modal-title")
            yield Input(placeholder="Short guidance for the active worker/supervisor...", id="hint-input")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel [Esc]", id="cancel", classes="btn-cancel")
                yield Button("Send [Enter]", id="ok", classes="btn-ok")

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
