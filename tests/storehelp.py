"""Where a test finds a project's cards now: on the cards branch, not in the repository's `cards/`."""

from __future__ import annotations

from pathlib import Path

from openfarm_core.config import load_config


def cards_dir(root: Path) -> Path:
    return Path(load_config(root).state_dir) / "cards-source" / "cards"


def card_path(root: Path, card_id: str) -> Path:
    return cards_dir(root) / f"{card_id}.md"


def doc(root: Path, name: str) -> Path:
    """`spec`, `chunks` or `decisions`, wherever the project keeps it now."""
    from openfarm_core.store import doc_path

    return doc_path(root, load_config(root), name)


def add_card(root: Path, text: str) -> dict:
    """Add a card file's text to the project the way `openfarm card add` does."""
    from openfarm_core.adapters.files import parse_card_file
    from openfarm_core.store import Store

    header, body = parse_card_file(text)
    return Store(root, load_config(root)).change_card(str(header["id"]), "create", header=header, body=body)
