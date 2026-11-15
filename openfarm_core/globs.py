"""Glob matching for never-touch, forbidden and touch paths.

Paths are repository-relative and use `/`. The semantics are the ones the spec
relies on:

- `src/auth/**` matches everything under `src/auth/`, but not `src/authx.rs`.
- `**/Cargo.toml` matches at every depth, including the repository root.
- `*` does not cross a `/`.
"""

from __future__ import annotations

import re

_CACHE: dict[str, re.Pattern[str]] = {}


def normalise(path: str) -> str:
    """Repository-relative, forward slashes, no leading `./` or `/`."""
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


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
        if char in "[]":
            out.append(char)
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
