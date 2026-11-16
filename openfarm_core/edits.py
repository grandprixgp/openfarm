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
import tempfile
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
        detail = "the answer contains no edit blocks"
        if re.search(r"^(---|\+\+\+) ", text, re.MULTILINE):
            detail += (" (it looks like a diff; openfarm reads only "
                       "`=== whole_file: <path>` and `=== search_replace: <path>` blocks)")
        raise Invalid("no-edits", detail)

    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[start:end]
        fmt = header.group("fmt")
        path = header.group("path").strip()

        body = _unfence(body)
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


_FENCE_LINE = re.compile(r"^\s*```[\w.+-]*\s*$")
_TRAILING_JUNK = re.compile(
    r"^\s*(```|(>{3,}|<{3,}|={3,})\s*(END|REPLACE|SEARCH)?"
    r"|</?[｜|]+\s*DSML[^>]*>"
    r"|</(parameter|invoke|function_calls|tool_calls?|calls)>)\s*$")


def _unfence(body: str) -> str:
    """Drop a Markdown code fence a model wrapped around a block.

    A leading fence line right under the header, and a closing fence as the
    block's last non-blank line, are never file content: no source file this
    reads starts with "```lang" or ends on a bare "```".
    """
    lines = body.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return body
    opened = bool(_FENCE_LINE.match(lines[first]))
    if opened:
        del lines[first]
    # Trailing junk a model adds after its last real line: a closing fence, a
    # closing marker the format does not have (`>>>`, `>>> END`), or a model's own
    # tool-call tags leaking through (DeepSeek's `</｜｜DSML｜｜ invoke>`). None of
    # these is ever source code.
    trimmed = False
    while True:
        last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
        if last is None or not _TRAILING_JUNK.match(lines[last]):
            break
        del lines[last:]
        trimmed = True
    if trimmed:
        lines.append("")  # keep the block's last real line ending in a newline
    return "\n".join(lines)


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

    All edits in a batch are validated and staged in memory first so either the
    whole batch lands or nothing on disk changes (`report.ok` is all-or-nothing).
    `allowed_paths` is the card's `touches` for that phase. `additions_only` names
    the files where a change must not delete or modify a line (production files the
    test writer may only append a stub to). `allow_stubs` is true only on cards that
    declare new symbols, and only the test writer passes it.

    `production_paths` with `forbid_production` is for the test writer on an ordinary
    card: production code is not its to write, and without this a whole-file edit
    there would land in the frozen commit and be invisible to every diff gate.
    """
    worktree = Path(worktree)
    root = worktree.resolve()
    report = ApplyReport()
    additions = set(additions_only or ())
    production = list(production_paths or ())

    pending: dict[Path, str] = {}
    orig_disk: dict[Path, str | None] = {}
    addition_targets: set[Path] = set()

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
        if not target.is_relative_to(root):
            report.refused.append({
                "path": path, "code": "path-escape",
                "message": "this path resolves outside the worktree"})
            continue
        if target == root or target.is_dir():
            report.refused.append({
                "path": path, "code": "not-a-file",
                "message": "this path is a directory, not a file"})
            continue

        resolved_rel = normalise(target.relative_to(root).as_posix())
        if matches_any(resolved_rel, forbidden):
            report.refused.append({"path": path, "code": "forbidden-file",
                                   "message": "this path resolves to a forbidden path"})
            continue
        if matches_any(resolved_rel, never_touch):
            report.refused.append({"path": path, "code": "never-touch",
                                   "message": "this path resolves to a never-touch path"})
            continue
        if forbid_production and production and matches_any(resolved_rel, production):
            report.refused.append({
                "path": path, "code": "production-not-writable",
                "message": "this path resolves to production code, which is not writable"})
            continue
        if allowed_paths and not matches_any(resolved_rel, allowed_paths):
            report.outside_touches.append(path)
            continue

        if target not in orig_disk:
            orig_disk[target] = target.read_text() if target.exists() else None
        current = pending[target] if target in pending else orig_disk[target]

        # A glob in `additions_only` still means its files are additions-only.
        is_addition = (
            path in additions
            or (bool(additions) and matches_any(path, additions))
            or resolved_rel in additions
            or (bool(additions) and matches_any(resolved_rel, additions))
        )
        if is_addition:
            addition_targets.add(target)
            # A file that does not exist yet is *not* additions-only by construction:
            # every line of a brand-new file is an "addition", so a whole-file edit
            # would pass. It is refused instead, because the only additions the test
            # writer may make to production code are stubs.
            if current is None:
                report.refused.append({
                    "path": path, "code": "additions-only-new-file",
                    "message": "the test writer may not create a production file: "
                               "only append a stub to one that exists"})
                continue

        try:
            new_content = _stage_one(edit, target, current, stub_inserter)
            if edit.format == FORMAT_APPEND_STUB:
                report.stubs.append(path)
        except Refused as exc:
            report.refused.append({"path": path, "code": exc.code,
                                   "message": exc.message})
            continue
        except Invalid as exc:
            report.refused.append({"path": path, "code": exc.code,
                                   "message": exc.message})
            continue

        pending[target] = new_content
        if is_addition and not _is_additions_only(current or "", new_content):
            if path not in report.not_additions_only:
                report.not_additions_only.append(path)
        report.added_lines.extend(_added_lines(path, current or "", new_content))
        report.applied.append(path)

    for target in addition_targets:
        if target in pending and not _is_additions_only(orig_disk[target] or "", pending[target]):
            rel = normalise(target.relative_to(root).as_posix())
            if rel not in report.not_additions_only:
                report.not_additions_only.append(rel)

    if not report.ok:
        report.applied.clear()
        report.stubs.clear()
        return report

    written: list[tuple[Path, str | None]] = []
    try:
        for target, content in pending.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            prior = orig_disk[target]
            target.write_text(content)
            written.append((target, prior))
    except OSError as exc:
        for target, prior in reversed(written):
            if prior is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(prior)
        raise Invalid("write-failed", f"cannot write edit batch: {exc}") from exc

    return report


def _stage_one(edit: Edit, target: Path, current: str | None,
               stub_inserter) -> str:
    if edit.format == FORMAT_APPEND_STUB:
        if stub_inserter is None:
            raise Invalid("no-stub-inserter", "this language has no append_stub")
        with tempfile.TemporaryDirectory() as tmp_dir:
            staged = Path(tmp_dir) / target.name
            if current is not None:
                staged.write_text(current)
            stub_inserter(staged, edit.test_name, edit.content or "")
            return staged.read_text() if staged.exists() else ""

    if edit.format == FORMAT_WHOLE_FILE:
        if edit.content is None:
            raise Invalid("bad-edit", "whole_file needs content")
        return edit.content

    if edit.format == FORMAT_SEARCH_REPLACE:
        base_text = current or ""
        count = base_text.count(edit.search)
        if count != 1:
            raise Refused("no-unique-match",
                          f"{edit.path}: search block matched {count} times, "
                          f"not exactly once")
        return base_text.replace(edit.search, edit.replace, 1)

    raise Invalid("bad-format", f"{edit.format!r} is not an edit format")


def _apply_one(edit: Edit, target: Path) -> None:
    current = target.read_text() if target.exists() else None
    content = _stage_one(edit, target, current, None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


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
