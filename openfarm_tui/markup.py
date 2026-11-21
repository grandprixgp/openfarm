"""Escaping for text shown in Textual widgets."""

from __future__ import annotations


def esc(text: object) -> str:
    """Make any text safe to put inside Textual markup.

    `rich.markup.escape` is not enough for Static, Label, Button and Option text:
    Textual's own parser still raises on `[?date=YYYY-MM-DD]` and swallows `[$name]`.
    RichLog and DataTable render through Rich, so they keep `rich.markup.escape`
    or take a `rich.text.Text`.
    """
    out = str(text).replace("[", "\\[")
    # A trailing backslash would escape the tag that follows the text.
    return out + " " if out.endswith("\\") else out
