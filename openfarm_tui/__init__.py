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
    initial_view: str = "overview",
) -> int:
    from .app import OpenfarmApp

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
    while True:
        if mapped == "auto":
            # The one screen. Projects, providers and settings are behind Ctrl+K: choosing
            # one there opens the older screen for it, and closing that comes back here.
            from .journey import Journey

            wanted = Journey(repo=repo, config_path=config_path).run()
            if not (isinstance(wanted, tuple) and wanted and wanted[0] == "classic"):
                return 0
            classic = OpenfarmApp(repo=repo, config_path=config_path, run_id=run_id, initial_view=wanted[1])
            classic.run()
            repo = Path(getattr(classic, "repo", repo))  # a project switched to there is the one shown here
            continue
        OpenfarmApp(repo=repo, config_path=config_path, run_id=run_id, initial_view=mapped).run()
        return 0


__all__ = ["run_textual_tui", "textual_available"]
