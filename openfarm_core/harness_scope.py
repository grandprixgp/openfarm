"""Whether a lesson depends on its project, and whether it may go global (v0.7).

Extraction marks each lesson `project` or `general` (the supervisor's reflection
chooses, the deterministic extractor says `general`). `scope_of` is the plain-code
check behind that claim: a lesson that names something of this project is
`project`, whatever it claimed. `global_eligibility` is the second gate, run at
promotion: which kinds of lesson may ever travel to other projects.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCOPES = ("project", "general")
_DEFINITION_RE = re.compile(
    r"^(?:async\s+def|def|class|pub\s+fn|fn|pub\s+struct|struct|"
    r"pub\s+enum|enum|pub\s+trait|trait)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def project_markers(repo_root: Path, project_name: str,
                    tracked_paths: list[str]) -> set[str]:
    """Return lower-cased strings whose presence in a lesson ties it to this project."""
    markers: set[str] = set()

    name = str(project_name).strip().lower()
    if name:
        markers.add(name)

    for raw_path in tracked_paths:
        path = str(raw_path).strip()
        if not path:
            continue

        lower_path = path.lower()
        markers.add(lower_path)

        file_name = Path(path).name.lower()
        if file_name:
            markers.add(file_name)

        stem = Path(path).stem.lower()
        if len(stem) >= 5:
            markers.add(stem)

        if not lower_path.endswith((".py", ".rs")):
            continue

        file_path = repo_root / path
        try:
            if not file_path.is_file():
                continue
            if file_path.stat().st_size >= 1_000_000:
                continue
            text = file_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for match in _DEFINITION_RE.finditer(text):
            defined_name = match.group(1)
            if (
                ("_" in defined_name and len(defined_name) >= 6)
                or len(defined_name) >= 10
                or sum(1 for char in defined_name if char.isupper()) >= 2
            ):
                markers.add(defined_name.lower())

    return markers


def scope_of(entry: dict, markers: set[str]) -> tuple[str, str]:
    """Return `(scope, reason)` for a lesson based on its text and project markers."""
    parts = []
    for key in ("title", "cue", "lesson"):
        value = entry.get(key)
        parts.append(value if isinstance(value, str) else "")
    text = "\n".join(parts)

    patch = entry.get("proposed_patch")
    if "proposed_patch" in entry and patch is not None:
        text += "\n" + json.dumps(patch, default=str)

    text = text.lower()

    if re.search(r"card-[0-9]+", text):
        return "project", "names a card"

    if isinstance(patch, dict):
        for key in ("context_files", "paths", "files"):
            value = patch.get(key)
            if isinstance(value, list) and value:
                return "project", "names files"
        for key in ("path", "file"):
            value = patch.get(key)
            if isinstance(value, str) and value:
                return "project", "names files"

    for marker in sorted(markers):
        marker_text = str(marker).lower()
        if not marker_text:
            continue
        pattern = r"(?<!\w)" + re.escape(marker_text) + r"(?!\w)"
        if re.search(pattern, text):
            return "project", f"names '{marker_text}'"

    reason = entry.get("scope_reason")
    if isinstance(reason, str) and reason:
        return "general", reason
    return "general", "names nothing of this project"


def global_eligibility(entry: dict) -> tuple[bool, str]:
    """`(eligible, reason)`: may this lesson ever go global?"""
    source = str(entry.get("source") or "")
    scope = str(entry.get("scope") or "")
    origin = str(entry.get("origin") or "")
    surface = str(entry.get("surface") or "")
    title = str(entry.get("title") or "")
    cue = str(entry.get("cue") or "")
    lesson = str(entry.get("lesson") or "")
    patch = entry.get("proposed_patch") or {}
    if not isinstance(patch, dict):
        patch = {}

    if source == "deterministic":
        return False, "fixed-rule cue"
    if scope != "general":
        return False, "project scope"
    if origin == "global":
        return False, "already global"

    if surface == "micromanager_context":
        scanned = "\n".join((title, cue, lesson)).lower()
        if any(word in scanned for word in (
            "probe", "overlay", "revision", "harness", "metrics",
            "ledger", "index_md", "by_kind", "by_surface",
        )):
            return False, "harness bookkeeping"

    if len(cue) > 400 or len(lesson) > 400:
        return False, "longer than 400 characters"

    if any("```" in text or "~~~" in text for text in (title, cue, lesson)):
        return False, "holds a code block"

    from .harness_memory import is_assertion_leak, is_gate_bypass_directive

    texts = (title, cue, lesson)
    if any(is_gate_bypass_directive(text) for text in texts):
        return False, "gate bypass"
    if any(is_assertion_leak(text) for text in texts):
        return False, "assertion leak"

    for key in ("context_files", "paths", "files"):
        value = patch.get(key)
        if isinstance(value, list) and value:
            return False, "names files"
    for key in ("path", "file"):
        value = patch.get(key)
        if isinstance(value, str) and value:
            return False, "names files"

    return True, ""
