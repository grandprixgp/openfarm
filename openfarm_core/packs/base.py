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

    def row_file(self, row: dict) -> str | None:
        """The source file a result or listing row came from, or None if unknown.

        The gates use it to keep a same-named test in another file from counting
        for a card's named test. None (the default) leaves every row in, as
        before; a pack whose rows name their file says so here.
        """
        return None

    def prepare_worktree(self, worktree: Path) -> str:
        """Ready another checkout of this project for a run, as `configure` does.

        The flow calls it on the trial checkouts it makes beside a card's worktree
        (revert-check, catch-up), so a run there sees what the card's own runs see.
        It never changes the pack's own settings. Returns a note, or "" (the
        default: nothing to do).
        """
        return ""

    def test_writer_note(self, worktree: Path, test_paths: list[str]) -> str:
        """What the test writer must know to write tests this project can build.

        For instance how a test file reaches the code under test. "" (the
        default) adds nothing to the brief.
        """
        return ""

    def build_errors(self, stdout: str, stderr: str) -> str:
        """The errors of a build that failed, a few lines, or "" when none are found.

        The flow adds it where a gate would otherwise only say the tests were not
        listed, so the role asked to try again sees why.
        """
        return ""

    def gate_evidence(self, gate: str) -> dict:
        """What a gate's record should say about how this pack ran it.

        For instance, that a lint gate was a syntax check only, so nobody reading
        the record takes it for a type check; a `note` key says it in a sentence.
        Empty (the default) adds nothing.
        """
        return {}

    def tooling_summary(self) -> dict:
        """What this pack will run a card with, for `check` and a card record.

        `pack`, `runner` (when the pack has one: unittest or pytest, node
        --test), `lint` and `format` (when the pack says which tool), and
        `notes`, the pack's own sentences on what it could not use. A pack that
        knows more says it by overriding this.
        """
        out: dict = {"pack": str(self.name)}
        runner = str(getattr(self, "runner", "") or "")
        if runner:
            out["runner"] = runner
        # A pack whose lint and formatting are ruff only where configured.
        use_ruff = getattr(self, "use_ruff", None)
        if isinstance(use_ruff, bool):
            out["lint"] = "ruff" if use_ruff else "syntax"
            out["format"] = "ruff" if use_ruff else "off"
        out["notes"] = [str(note) for note in getattr(self, "notes", []) or []]
        return out

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
        from .typescript import TypeScriptPack
        return TypeScriptPack()
    return UnsupportedPack(language)


class OwnMutationMixin:
    """`[gates] mutation` through openfarm's own runner (`openfarm_core/mutate.py`,
    v0.8.5), for packs whose language has no mutation tool openfarm can call.

    The flow sets `mutation_output` (the card's evidence folder) and
    `mutation_lines` (`{path: [line, ...]}` changed since the card's base, so only
    those lines are mutated). The runner's result is read from its `outcomes.json`.
    """

    mutation_language = ""
    mutation_output = None
    mutation_lines = None
    _mutation_dir = None
    _mutation_runs = 0

    def _own_mutation_argv(self, files, tests) -> list[str]:
        import json
        import shutil
        import sys
        import tempfile

        from .. import mutate

        self._mutation_runs = int(self._mutation_runs or 0) + 1
        if self.mutation_output is not None:
            out = Path(self.mutation_output) / str(self._mutation_runs)
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True, exist_ok=True)
        else:
            out = Path(tempfile.mkdtemp(prefix="openfarm-mutants-")).resolve()
        self._mutation_dir = out
        test_argv = self.commands("test", tests=list(tests or []))["argv"]
        argv = [sys.executable, "-I", str(Path(mutate.__file__)), "--lang", self.mutation_language,
                "--out", str(out), "--test-argv", json.dumps(test_argv)]
        if self.mutation_lines is not None:
            argv += ["--lines", json.dumps({str(k): sorted(v) for k, v in
                                            self.mutation_lines.items()})]
        return argv + [str(f) for f in (files or [])]

    def read_mutation(self, stdout: str, stderr: str = "", exit_code=None) -> dict:
        from ..mutate import read_outcomes

        return read_outcomes(self._mutation_dir, stdout, stderr, exit_code)

    def parse_mutation(self, stdout: str) -> dict:
        return self.read_mutation(stdout, "", None)

