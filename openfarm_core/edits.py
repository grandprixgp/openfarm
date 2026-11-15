"""Applying model edits, and the checks that run before anything is built.

Two rules decide everything here:

- A search block that doesn't match exactly once is refused before anything runs,
  and spends no attempt and no build.
- The "additions only" check runs on the **raw** edit, before formatting, because
  rustfmt reformats neighbouring untouched lines and would manufacture `-` lines.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from .errors import Invalid, Refused
from .globs import escapes, matches_any, normalise

#: An edit the harness may apply.
FORMAT_SEARCH_REPLACE = "search_replace"
FORMAT_WHOLE_FILE = "whole_file"
#: Only the test writer has this one, and only on cards declaring new symbols.
FORMAT_APPEND_STUB = "append_stub"

FORMATS = (FORMAT_SEARCH_REPLACE, FORMAT_WHOLE_FILE, FORMAT_APPEND_STUB)


class Edit:
    """One edit block from a model's answer."""

    __slots__ = ("format", "path", "search", "replace", "content", "test_name")

    def __init__(self, format: str, path: str, search: str = "",
                 replace: str = "", content: str | None = None,
                 test_name: str = "") -> None:
        self.format = format
        self.path = normalise(path)
        self.search = search
        self.replace = replace
        self.content = content
        self.test_name = test_name

    def as_dict(self) -> dict:
        payload = {"format": self.format, "path": self.path}
        if self.format == FORMAT_SEARCH_REPLACE:
            payload["search"] = self.search
            payload["replace"] = self.replace
        else:
            payload["content"] = self.content
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "Edit":
        return cls(
            format=str(data.get("format", FORMAT_SEARCH_REPLACE)),
            path=str(data.get("path", "")),
            search=str(data.get("search", "")),
            replace=str(data.get("replace", "")),
            content=data.get("content"),
        )


#: Matches the harness accepts in a model answer.
_EDIT_HEADER = re.compile(
    r"^===+\s*(?P<fmt>search_replace|whole_file|append_stub)\s*:\s*(?P<path>\S+)\s*$",
    re.MULTILINE)
_SEARCH_START = re.compile(r"^<{3,}\s*SEARCH\s*$", re.MULTILINE)
_SEARCH_END = re.compile(r"^>{3,}\s*REPLACE\s*$", re.MULTILINE)


def parse_edits(text: str) -> list[Edit]:
    """Read a model's answer into edits.

    Accepted shape:

        === search_replace: src/foo.rs
        <<< SEARCH
        old text
        >>> REPLACE
        new text

        === whole_file: src/bar.rs
        <full file content to the end of the block>
    """
    edits: list[Edit] = []
    headers = list(_EDIT_HEADER.finditer(text))
    if not headers:
        raise Invalid("no-edits", "the answer contains no edit blocks")

    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[start:end]
        fmt = header.group("fmt")
        path = header.group("path").strip()

        if fmt == FORMAT_WHOLE_FILE:
            content = body.lstrip("\n")
            if content.endswith("\n\n"):
                content = content[:-1]
            edits.append(Edit(fmt, path, content=content))
            continue

        if fmt == FORMAT_APPEND_STUB:
            name_match = re.match(r"\s*(?P<name>\S+)\s*\n", body)
            if not name_match:
                raise Invalid("bad-block",
                              f"{path}: append_stub needs the test's name on the "
                              f"first line, then the stub body")
            content = body[name_match.end():]
            if content.endswith("\n\n"):
                content = content[:-1]
            edits.append(Edit(fmt, path, content=content,
                              test_name=name_match.group("name")))
            continue

        search_match = _SEARCH_START.search(body)
        replace_match = _SEARCH_END.search(body)
        if not search_match or not replace_match or replace_match.start() < search_match.end():
            raise Invalid("bad-block",
                          f"{path}: a search_replace block needs <<< SEARCH and >>> REPLACE")
        search = body[search_match.end():replace_match.start()]
        replace = body[replace_match.end():]
        edits.append(Edit(fmt, path,
                          search=_strip_one_newline(search),
                          replace=_strip_one_newline(replace)))
    return edits


def _strip_one_newline(text: str) -> str:
    if text.startswith("\n"):
        text = text[1:]
    if text.endswith("\n"):
        text = text[:-1]
    return text


class ApplyReport:
    """What happened when edits were applied, and what was refused."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.refused: list[dict] = []
        self.added_lines: list[dict] = []
        self.outside_touches: list[str] = []
        self.not_additions_only: list[str] = []
        self.stubs: list[str] = []

    @property
    def ok(self) -> bool:
        return (not self.refused and not self.outside_touches
                and not self.not_additions_only)

    def as_dict(self) -> dict:
        return {"applied": list(self.applied), "refused": list(self.refused),
                "outside_touches": list(self.outside_touches),
                "not_additions_only": list(self.not_additions_only),
                "stubs": list(self.stubs)}


def apply_edits(worktree: Path, edits: list[Edit], allowed_paths: list[str],
                never_touch: list[str], forbidden: list[str],
                additions_only: set[str] | None = None,
                allow_stubs: bool = False,
                stub_inserter=None,
                production_paths: set[str] | None = None,
                forbid_production: bool = False) -> ApplyReport:
    """Apply edits inside the allowed paths, refusing everything else.

    `allowed_paths` is the card's `touches` for that phase. `additions_only` names
    the files where a change must not delete or modify a line (production files the
    test writer may only append a stub to). `allow_stubs` is true only on cards that
    declare new symbols, and only the test writer passes it.

    `production_paths` with `forbid_production` is for the test writer on an ordinary
    card: production code is not its to write, and without this a whole-file edit
    there would land in the frozen commit and be invisible to every diff gate.
    """
    worktree = Path(worktree)
    report = ApplyReport()
    additions = set(additions_only or ())
    production = list(production_paths or ())

    for edit in edits:
        path = edit.path
        if escapes(path):
            report.refused.append({
                "path": path, "code": "path-escape",
                "message": "this path climbs above the repository root"})
            continue
        if matches_any(path, forbidden):
            report.refused.append({"path": path, "code": "forbidden-file",
                                   "message": "the config forbids this path"})
            continue
        if matches_any(path, never_touch):
            report.refused.append({"path": path, "code": "never-touch",
                                   "message": "this path is on the never-touch list"})
            continue
        if edit.format == FORMAT_APPEND_STUB and not allow_stubs:
            report.refused.append({
                "path": path, "code": "stub-not-allowed",
                "message": "append_stub is only for cards that declare new symbols, "
                           "and only for the test writer"})
            continue
        if forbid_production and production and matches_any(path, production):
            report.refused.append({
                "path": path, "code": "production-not-writable",
                "message": "the test writer may not write production code on this "
                           "card: only its named test files"})
            continue
        if allowed_paths and not matches_any(path, allowed_paths):
            report.outside_touches.append(path)
            continue

        target = (worktree / path).resolve()
        root = worktree.resolve()
        if root not in target.parents and target != root:
            report.refused.append({
                "path": path, "code": "path-escape",
                "message": "this path resolves outside the worktree"})
            continue
        # A glob in `additions_only` still means its files are additions-only.
        is_addition = path in additions or (
            additions and matches_any(path, additions))
        if is_addition:
            existing = target.read_text() if target.exists() else ""
            before = existing
        else:
            before = target.read_text() if target.exists() else None

        try:
            if edit.format == FORMAT_APPEND_STUB:
                if stub_inserter is None:
                    raise Invalid("no-stub-inserter",
                                  "this language has no append_stub")
                stub_inserter(target, edit.test_name, edit.content or "")
                report.stubs.append(path)
            else:
                _apply_one(edit, target)
        except Refused as exc:
            report.refused.append({"path": path, "code": exc.code,
                                   "message": exc.message})
            continue
        except Invalid as exc:
            report.refused.append({"path": path, "code": exc.code,
                                   "message": exc.message})
            continue

        after = target.read_text() if target.exists() else ""
        if is_addition and not _is_additions_only(before or "", after):
            report.not_additions_only.append(path)
        report.added_lines.extend(_added_lines(path, before or "", after))
        report.applied.append(path)

    return report


def _apply_one(edit: Edit, target: Path) -> None:
    if edit.format == FORMAT_WHOLE_FILE:
        if edit.content is None:
            raise Invalid("bad-edit", "whole_file needs content")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit.content)
        return

    if edit.format == FORMAT_SEARCH_REPLACE:
        text = target.read_text() if target.exists() else ""
        count = text.count(edit.search)
        if count != 1:
            raise Refused("no-unique-match",
                          f"{edit.path}: search block matched {count} times, "
                          f"not exactly once")
        target.write_text(text.replace(edit.search, edit.replace, 1))
        return

    raise Invalid("bad-format", f"{edit.format!r} is not an edit format")


def _is_additions_only(before: str, after: str) -> bool:
    """True when every line of `before` survives, in order, and nothing else changed."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, _i1, _i2, _j1, _j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            return False
    return True


def _added_lines(path: str, before: str, after: str) -> list[dict]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    out: list[dict] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("insert", "replace"):
            continue
        for offset, text in enumerate(after_lines[j1:j2]):
            out.append({"path": path, "line": j1 + offset + 1, "text": text})
    return out


def diff_text(worktree: Path, paths: list[str], ref: str = "HEAD") -> str:
    """A unified diff against `ref`, plus any new files.

    All git access goes through `Git`, which is the module that holds the no-push
    allowlist. `ref` is HEAD for a per-attempt check and the base commit for the
    card's whole diff once the implementer has committed.
    """
    from .git import Git
    worktree = Path(worktree)
    git = Git(worktree)
    chunks: list[str] = [git.diff(paths, ref=ref)]
    for path in git.untracked_files(paths):
        try:
            content = (worktree / path).read_text()
        except OSError:
            continue
        lines = content.splitlines()
        chunks.append(f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
                      f"--- /dev/null\n+++ b/{path}\n"
                      f"@@ -0,0 +1,{len(lines)} @@\n"
                      + "\n".join("+" + line for line in lines) + "\n")
    return "".join(chunks)


def line_counts(text: str) -> tuple[int, int]:
    """Lines added and removed in a unified diff."""
    added = removed = 0
    for line in text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def touched_paths(text: str) -> list[str]:
    """Every path a unified diff names, added, removed or changed."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            if match:
                for path in (match.group(1), match.group(2)):
                    if path not in out:
                        out.append(path)
        elif line.startswith("+++ ") or line.startswith("--- "):
            target = line[4:].strip()
            if target in ("/dev/null", ""):
                continue
            if target.startswith("b/") or target.startswith("a/"):
                target = target[2:]
            if target not in out:
                out.append(target)
    return out
