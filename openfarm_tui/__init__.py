"""Optional Textual workbench for Openfarm v0.20.0.

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
    initial_view: str = "overview",
) -> int:
    view_map = {
        "projects": "projects",
        "overview": "auto",  # the default: the app picks the screen from the project's state
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
    repo = Path(repo).expanduser().resolve()
    config_path = Path(config_path).expanduser().resolve() if config_path else None
    # The one screen. Projects, providers, settings and builders' notes are panes of it, behind Ctrl+K.
    panes = {"auto": "", "projects": "projects", "providers": "providers", "engine": "settings", "findings": "notes"}
    if mapped not in panes:  # asked for by name with --view: the older studio and review screens
        from .app import OpenfarmApp

        OpenfarmApp(repo=repo, config_path=config_path, run_id=run_id, initial_view=mapped).run()
        return 0
    from .journey import Journey

    open_at = panes[mapped]
    while True:
        wanted = Journey(repo=repo, config_path=config_path, open_at=open_at).run()
        if not (isinstance(wanted, tuple) and wanted and wanted[0] == "project"):
            return 0
        repo, config_path, open_at = Path(wanted[1]).expanduser().resolve(), None, ""  # another project was opened


__all__ = ["run_textual_tui", "textual_available"]
