"""Whether a lesson depends on its project, and whether it may go global (v0.7).

Extraction marks each lesson `project` or `general` (the supervisor's reflection
chooses, the deterministic extractor says `general`). `scope_of` is the plain-code
check behind that claim: a lesson that names something of this project is
`project`, whatever it claimed. `global_eligibility` is the second gate, run at
promotion: which kinds of lesson may ever travel to other projects.
"""

from __future__ import annotations

from pathlib import Path

SCOPES = ("project", "general")


def project_markers(repo_root: Path, project_name: str,
                    tracked_paths: list[str]) -> set[str]:
    """The strings whose presence in a lesson ties it to this project.

    Filled by a v0.7 card. Until then: the project name and every tracked path.
    """
    markers = {str(project_name).strip().lower()} if str(project_name).strip() else set()
    markers.update(p.strip().lower() for p in tracked_paths if p.strip())
    return markers


def scope_of(entry: dict, markers: set[str]) -> tuple[str, str]:
    """`(scope, reason)` for a lesson. Until the v0.7 card lands, always `project`."""
    return "project", "scope check not built yet"


def global_eligibility(entry: dict) -> tuple[bool, str]:
    """`(eligible, reason)`: may this lesson ever go global? Until the card lands, no."""
    return False, "eligibility check not built yet"
