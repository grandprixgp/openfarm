"""Optional Textual 3-View + Findings Inbox Workbench for Openfarm v0.10.0.

`openfarm_core` remains 100% Python 3.11+ stdlib-only. This package is loaded
lazily by `openfarm_core.tui.run_tui` when `textual` is installed; otherwise
Openfarm falls back seamlessly to the stdlib `curses` TUI.
"""

from __future__ import annotations

from pathlib import Path


def textual_available() -> bool:
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False


def run_textual_tui(
    repo: Path | str = ".",
    config_path: Path | str | None = None,
    *,
    run_id: str | None = None,
    initial_view: str = "studio",
) -> int:
    from .app import OpenfarmApp

    view_map = {
        "projects": "projects",
        "overview": "studio",
        "studio": "studio",
        "cards": "review",
        "dossier": "review",
        "review": "review",
        "findings": "findings",
        "board": "engine",
        "swarm": "engine",
        "harness": "engine",
        "ops": "engine",
        "engine": "engine",
        "providers": "providers",
    }
    mapped = view_map.get(str(initial_view or "studio").strip().lower(), "studio")
    app = OpenfarmApp(
        repo=Path(repo).expanduser().resolve(),
        config_path=Path(config_path).expanduser().resolve() if config_path else None,
        run_id=run_id,
        initial_view=mapped,
    )
    app.run()
    return 0


__all__ = ["run_textual_tui", "textual_available"]
