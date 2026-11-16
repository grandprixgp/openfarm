"""The language pack interface.

A pack knows one language's commands and output formats. It never runs anything: it
answers with argv, and the runner runs it.
"""

from __future__ import annotations

from pathlib import Path

import re

from ..errors import Invalid, Refused


class Pack:
    """What a language pack must provide."""

    name = "base"
    extension = ""
    test_dir = "tests"
    source_dir = "src"

    def hello(self) -> dict:
        return {"name": self.name,
                "versions": [1],
                "ops": ["hello", "commands", "parse_list", "parse_tests",
                        "classify_paths", "signatures", "edit_formats",
                        "apply_edit", "append_stub", "format", "format_check",
                        "patterns", "error_signature", "mutation",
                        "parse_mutation"]}

    # -- commands -------------------------------------------------------------

    def commands(self, purpose: str, unit: str = "", tests: list[str] | None = None,
                 files: list[str] | None = None,
                 extra_flags: list[str] | None = None) -> dict:
        raise NotImplementedError

    # -- output ---------------------------------------------------------------

    def parse_list(self, stdout: str) -> dict:
        raise NotImplementedError

    def parse_tests(self, stdout: str, stderr: str = "") -> dict:
        raise NotImplementedError

    def parse_mutation(self, stdout: str) -> dict:
        raise NotImplementedError

    def error_signature(self, stderr: str) -> dict:
        raise NotImplementedError

    # -- files ----------------------------------------------------------------

    def classify_paths(self, paths: list[str]) -> dict:
        raise NotImplementedError

    def signatures(self, path: Path) -> dict:
        raise NotImplementedError

    def edit_formats(self) -> dict:
        return {"formats": ["search_replace", "whole_file"]}

    def apply_edit(self, fmt: str, path: Path, edit: dict) -> dict:
        """Plain-text edits, the same in every language."""
        return apply_text_edit(fmt, path, edit)

    def append_stub(self, path: Path, test_name: str, body: str) -> dict:
        raise NotImplementedError

    def format(self, paths: list[Path]) -> dict:
        raise NotImplementedError

    def format_check(self, paths: list[Path]) -> dict:
        raise NotImplementedError

    def patterns(self, added_lines: list[dict], flagged_patterns: list[str] | None = None,
                 production_paths: set[str] | None = None) -> dict:
        raise NotImplementedError

    def mutation(self, files: list[str], tests: list[str] | None = None) -> dict:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def require(purpose: str, known: tuple[str, ...]) -> None:
        if purpose not in known:
            raise Invalid("bad-purpose",
                          f"{purpose!r} is not one of {', '.join(known)}")


class UnsupportedPack(Pack):
    """A language with no pack yet. Every op refuses, so a card can't silently pass."""

    def __init__(self, language: str) -> None:
        self.name = f"unsupported:{language}"
        self.language = language

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": []}

    def _refuse(self, op: str):
        raise Invalid("unsupported-language",
                      f"no language pack for {self.language!r} ({op})")

    def commands(self, *args, **kwargs) -> dict:
        self._refuse("commands")

    def parse_list(self, *args, **kwargs) -> dict:
        self._refuse("parse_list")

    def parse_tests(self, *args, **kwargs) -> dict:
        self._refuse("parse_tests")


def apply_text_edit(fmt: str, path: Path, edit: dict) -> dict:
    """`whole_file` or `search_replace` on one file; a search must match once."""
    path = Path(path)
    if fmt == "whole_file":
        content = edit.get("content")
        if content is None:
            raise Invalid("bad-edit", "whole_file needs `content`")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content))
        return {"ok": True}

    if fmt == "search_replace":
        search = edit.get("search")
        replace = edit.get("replace", "")
        if search is None:
            raise Invalid("bad-edit", "search_replace needs `search`")
        text = path.read_text() if path.exists() else ""
        count = text.count(str(search))
        if count != 1:
            raise Refused(
                "no-unique-match",
                f"{path}: search block matched {count} times, not exactly once")
        path.write_text(text.replace(str(search), str(replace), 1))
        return {"ok": True}

    raise Invalid("bad-format", f"{fmt!r} is not an edit format")


def added_lines_from_diff(diff_text: str) -> list[dict]:
    """Added lines from a unified diff, with their new-file line numbers."""
    out: list[dict] = []
    path = ""
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = "" if target == "/dev/null" else target
            if path.startswith("b/"):
                path = path[2:]
            continue
        if line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            new_line = int(match.group(1)) if match else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.append({"path": path, "line": new_line, "text": line[1:]})
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return out


def pack_for(language: str, cargo_flags: list[str] | None = None,
             spec: str = "", cwd: Path | None = None,
             python: str = "") -> Pack:
    """The pack for a language.

    `cargo_flags` comes from `[runner] cargo_flags` and `python` from
    `[runner] python`; each pack ignores the other's setting.
    """
    cmd_spec = spec if str(spec).startswith("cmd:") else (
        language if str(language).startswith("cmd:") else "")
    if cmd_spec:
        try:
            from ..protocol import CmdPack
        except ImportError:
            CmdPack = None
        if CmdPack is not None:
            if cwd is not None:
                return CmdPack(cmd_spec, cwd=cwd, cargo_flags=cargo_flags)
            if cargo_flags is not None:
                return CmdPack(cmd_spec, cargo_flags=cargo_flags)
            return CmdPack(cmd_spec)
        return UnsupportedPack(cmd_spec)
    if language == "rust":
        from .rust import RustPack
        return RustPack(cargo_flags)
    if language in ("python", "py"):
        from .python import PythonPack
        return PythonPack(python or None)
    if language in ("typescript", "ts", "javascript", "js"):
        raise Invalid(
            "no-typescript-pack",
            "the TypeScript pack comes after the pilot and is not implemented")
    return UnsupportedPack(language)
