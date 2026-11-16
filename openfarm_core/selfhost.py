"""Which openfarm is running, and whether it may run on the repository it was given.

openfarm can work on its own source. It must never run *from* that source while
doing so: a card that edits a gate would then be judged by the gate it edited. The
rule is general and needs no idea of which repository is openfarm: the running
code must not live inside the repository it farms, nor inside its state folder,
where the card worktrees are.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__

#: The folder holding the running `openfarm_core` package.
CORE_ROOT = Path(__file__).resolve().parent.parent


def orchestrator_identity(core_root: Path | None = None) -> dict:
    """Version, location and (where it is a git checkout) commit of this openfarm."""
    root = Path(core_root) if core_root is not None else CORE_ROOT
    commit = ""
    dirty = False
    if (root / ".git").exists():
        # git.py is the only place that runs git; this goes through it too.
        from .errors import AdapterError
        from .git import Git
        try:
            checkout = Git(root)
            commit = checkout.head()
            dirty = any(path.startswith("openfarm_core/")
                        for path in checkout.changed_files())
        except (AdapterError, OSError):
            commit, dirty = "", False
    return {"version": __version__, "path": str(root), "commit": commit,
            "dirty": dirty}


_CACHED: dict | None = None


def cached_identity() -> dict:
    """`orchestrator_identity()` for this process, worked out once."""
    global _CACHED
    if _CACHED is None:
        _CACHED = orchestrator_identity()
    return dict(_CACHED)


def describe(identity: dict) -> str:
    text = f"openfarm {identity.get('version', '?')} at {identity.get('path', '?')}"
    if identity.get("commit"):
        text += f" (commit {identity['commit'][:12]}"
        text += ", with uncommitted core changes)" if identity.get("dirty") else ")"
    return text


def unpinned_reason(repo: Path, state_dir: Path | None,
                    core_root: Path | None = None) -> str | None:
    """Why this openfarm may not run on `repo`, or None when it may.

    Refused when the running core is inside the repository or its state folder.
    The fix is a pinned copy outside both, such as a detached git worktree of a
    tagged commit.
    """
    root = (Path(core_root) if core_root is not None else CORE_ROOT).resolve()
    repo_path = Path(repo).resolve()
    places = [("the repository it would edit", repo_path)]
    if state_dir is not None:
        places.append(("its state folder, where card worktrees live",
                       Path(state_dir).resolve()))
    for label, place in places:
        if root == place or root.is_relative_to(place):
            return (f"openfarm is running from {root}, inside {label} ({place}). "
                    f"Run a pinned copy instead, for example: git -C {repo_path} "
                    f"worktree add --detach ~/.openfarm/runners/<tag> <tag>, then "
                    f"python3 ~/.openfarm/runners/<tag>/openfarm --repo {repo_path} "
                    f"run ...")
    return None
