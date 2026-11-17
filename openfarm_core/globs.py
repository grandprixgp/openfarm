"""Glob matching for never-touch, forbidden and touch paths.

Paths are repository-relative and use `/`. The semantics are the ones the spec
relies on:

- `src/auth/**` matches everything under `src/auth/`, but not `src/authx.rs`.
- `**/Cargo.toml` matches at every depth, including the repository root.
- `*` does not cross a `/`.
"""

from __future__ import annotations

from collections import deque
from functools import lru_cache
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


_SEG_START = 0
_SEG_DOT1 = 1
_SEG_DOT2 = 2
_SEG_OK = 3
_SEG_DEAD = -1


def _path_dfa_step(state: int, char: str) -> int:
    if state == _SEG_START:
        if char == "/":
            return _SEG_DEAD
        if char == ".":
            return _SEG_DOT1
        return _SEG_OK
    if state == _SEG_DOT1:
        if char == "/":
            return _SEG_DEAD
        if char == ".":
            return _SEG_DOT2
        return _SEG_OK
    if state == _SEG_DOT2:
        return _SEG_DEAD if char == "/" else _SEG_OK
    return _SEG_START if char == "/" else _SEG_OK


def _compile_glob_nfa(pattern: str):
    eps: list[list[int]] = [[]]
    edges: list[list[tuple[str, str, int]]] = [[]]
    literals: set[str] = set()

    def new_state() -> int:
        idx = len(eps)
        eps.append([])
        edges.append([])
        return idx

    cur = 0
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                nxt = new_state()
                loop = new_state()
                eps[cur].extend((nxt, loop))
                edges[loop].append(("any", "", loop))
                edges[loop].append(("char", "/", nxt))
                cur = nxt
                index += 3
                continue
            if pattern.startswith("**", index):
                nxt = new_state()
                edges[cur].append(("any", "", cur))
                eps[cur].append(nxt)
                cur = nxt
                index += 2
                continue
            nxt = new_state()
            edges[cur].append(("not_slash", "", cur))
            eps[cur].append(nxt)
            cur = nxt
            index += 1
            continue
        if char == "/":
            if pattern.startswith("/**", index) and index + 3 == length:
                nxt = new_state()
                tail = new_state()
                eps[cur].append(nxt)
                edges[cur].append(("char", "/", tail))
                edges[tail].append(("any", "", tail))
                eps[tail].append(nxt)
                cur = nxt
                index += 3
                continue
            nxt = new_state()
            edges[cur].append(("char", "/", nxt))
            cur = nxt
            index += 1
            continue
        if char == "?":
            nxt = new_state()
            edges[cur].append(("not_slash", "", nxt))
            cur = nxt
            index += 1
            continue
        nxt = new_state()
        edges[cur].append(("char", char, nxt))
        literals.add(char)
        cur = nxt
        index += 1

    return cur, eps, edges, literals


def _eps_closure(starts: list[int], eps: list[list[int]]) -> tuple[int, ...]:
    seen = set(starts)
    stack = list(starts)
    while stack:
        state = stack.pop()
        for nxt in eps[state]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return tuple(sorted(seen))


def _build_trans(
    eps: list[list[int]],
    edges: list[list[tuple[str, str, int]]],
    alphabet: tuple[str, ...],
) -> list[dict[str, tuple[int, ...]]]:
    table: list[dict[str, tuple[int, ...]]] = []
    for state in range(len(eps)):
        row: dict[str, tuple[int, ...]] = {}
        for char in alphabet:
            targets = [
                tgt
                for kind, val, tgt in edges[state]
                if kind == "any"
                or (kind == "not_slash" and char != "/")
                or (kind == "char" and val == char)
            ]
            row[char] = _eps_closure(targets, eps) if targets else ()
        table.append(row)
    return table


def _pick_wildcard(forbidden: set[str]) -> str:
    code = 1
    while True:
        ch = chr(code)
        if ch not in forbidden:
            return ch
        code += 1


@lru_cache(maxsize=1024)
def _globs_overlap_norm(pat1: str, pat2: str) -> bool:
    accept1, eps1, edges1, lit1 = _compile_glob_nfa(pat1)
    accept2, eps2, edges2, lit2 = _compile_glob_nfa(pat2)

    special = {"/", "."}
    literals = lit1 | lit2
    wildcard = _pick_wildcard(literals | special)
    alphabet = tuple(sorted(literals | special | {wildcard}))

    trans1 = _build_trans(eps1, edges1, alphabet)
    trans2 = _build_trans(eps2, edges2, alphabet)

    init1 = _eps_closure([0], eps1)
    init2 = _eps_closure([0], eps2)
    queue: deque[tuple[int, int, int]] = deque()
    visited: set[tuple[int, int, int]] = set()
    for s1 in init1:
        for s2 in init2:
            node = (s1, s2, _SEG_START)
            visited.add(node)
            queue.append(node)

    while queue:
        s1, s2, dfa = queue.popleft()
        if s1 == accept1 and s2 == accept2 and dfa == _SEG_OK:
            return True
        row1 = trans1[s1]
        row2 = trans2[s2]
        for char in alphabet:
            next_dfa = _path_dfa_step(dfa, char)
            if next_dfa == _SEG_DEAD:
                continue
            ns1_tuple = row1[char]
            if not ns1_tuple:
                continue
            ns2_tuple = row2[char]
            if not ns2_tuple:
                continue
            for ns1 in ns1_tuple:
                for ns2 in ns2_tuple:
                    nxt = (ns1, ns2, next_dfa)
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append(nxt)
    return False


def globs_overlap(pat1: str, pat2: str) -> bool:
    """True when `pat1` and `pat2` can match at least one common valid repo-relative path."""
    p1 = normalise(pat1)
    p2 = normalise(pat2)
    if not p1 or not p2 or escapes(p1) or escapes(p2):
        return False
    a, b = (p1, p2) if p1 <= p2 else (p2, p1)
    return _globs_overlap_norm(a, b)
