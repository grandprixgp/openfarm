"""Glob matching for never-touch, forbidden and touch paths.

Paths are repository-relative and use `/`. The semantics are the ones the spec
relies on:

- `src/auth/**` matches everything under `src/auth/`, but not `src/authx.rs`.
- `**/Cargo.toml` matches at every depth, including the repository root.
- `*` does not cross a `/`.
"""

from __future__ import annotations

import re
from pathlib import Path

_CACHE: dict[str, re.Pattern[str]] = {}


def normalise(path: str) -> str:
    """Repository-relative, forward slashes, no leading `./` or `/`, no `..`.

    Collapsing `..` matters for security, not tidiness: a model's edit names a path,
    and `a/../../outside.txt` would otherwise pass a glob check against `a/**` and
    then be written outside the worktree.
    """
    text = str(path).replace("\\", "/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                # Above the repository root: keep the `..` so the escape stays
                # visible to the caller instead of being silently absorbed.
                parts.append("..")
                continue
            if parts[-1] == "..":
                parts.append("..")
                continue
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def escapes(path: str) -> bool:
    """True when a path climbs above the repository root."""
    return normalise(path).startswith("../") or normalise(path) == ".."


def resolves_within(root: Path, rel_path: str) -> Path | None:
    """Return the resolved path if `rel_path` stays within `root`, else `None`."""
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        return None
    if escapes(rel_path):
        return None
    candidate_rel = rel_path.replace("\\", "/")
    if Path(candidate_rel).is_absolute():
        return None
    root_resolved = Path(root).resolve()
    try:
        resolved = (root_resolved / candidate_rel).resolve()
    except (OSError, ValueError):
        return None
    if resolved.is_relative_to(root_resolved):
        return resolved
    return None


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate one glob into a regex anchored at both ends."""
    cached = _CACHE.get(pattern)
    if cached is not None:
        return cached

    out = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "/":
            if pattern.startswith("/**", index) and index + 3 == length:
                out.append("(?:/.*)?")
                index += 3
                continue
            out.append("/")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        out.append(re.escape(char))
        index += 1

    compiled = re.compile("^" + "".join(out) + "$")
    _CACHE[pattern] = compiled
    return compiled


def matches(path: str, pattern: str) -> bool:
    return bool(compile_glob(normalise(pattern)).match(normalise(path)))


def matches_any(path: str, patterns) -> bool:
    target = normalise(path)
    return any(matches(target, pattern) for pattern in (patterns or ()))


def first_match(path: str, patterns) -> str | None:
    """The first pattern that matched, so a record can name the rule."""
    target = normalise(path)
    for pattern in (patterns or ()):
        if matches(target, pattern):
            return pattern
    return None
