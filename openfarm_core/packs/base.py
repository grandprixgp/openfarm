"""The language pack interface.

A pack knows one language's commands and output formats. It never runs anything: it
answers with argv, and the runner runs it.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import Invalid


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
        raise NotImplementedError

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


def pack_for(language: str, cargo_flags: list[str] | None = None,
             spec: str = "", cwd: Path | None = None) -> Pack:
    """The pack for a language. `cargo_flags` comes from `[runner] cargo_flags`."""
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
    if language in ("typescript", "ts", "javascript", "js"):
        raise Invalid(
            "no-typescript-pack",
            "the TypeScript pack comes after the pilot and is not implemented")
    return UnsupportedPack(language)
