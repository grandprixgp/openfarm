"""The Python language pack.

Shaped by the same two facts as the Rust pack:

- **Plain output only.** `unittest -v` prints `name (module.Class.name) ... ok` on
  stderr and one `Ran N tests` footer; `pytest -v` prints `path::name PASSED` and one
  `=== ... in 0.1s ===` footer. A run with no footer died partway: infra, never a pass.
- **Never judge a run by its exit code.** Per-test lines decide, as they do for cargo.

The runner is `unittest` unless the repository configures pytest (`[tool.pytest...]`
in `pyproject.toml`, a `pytest.ini`, or `[tool:pytest]` in `setup.cfg`). openfarm
itself has no dependencies, so `ruff` is used only where it is already installed;
without it formatting is not checked, and the dossier says so.

A module that fails to import is Python's compile error: unittest reports it as a
`unittest.loader._FailedTest`, pytest as `ERROR collecting`. Either way the card goes
back to the implementer with the traceback, exactly as a cargo compile error would.
"""

from __future__ import annotations

import ast
import hashlib
import re
import shutil
import sys
import textwrap
from pathlib import Path

from ..errors import Invalid, Refused
from .base import Pack

PURPOSES = ("build", "test", "list", "lint", "ws-lint", "format", "fmt-check",
            "typecheck", "mutation", "extra")

#: Directories a syntax check never walks into.
_SKIP_DIRS = (".git", ".hg", ".venv", "venv", "env", ".env", "node_modules",
              "__pycache__", "build", "dist", ".tox", ".nox", ".mypy_cache",
              ".ruff_cache", ".pytest_cache", "site-packages", "target")

#: Parses every `.py` file under a root without writing any `.pyc`, then runs
#: `ruff check` when ruff is on PATH. Exit 1 on any finding.
_CHECK_SCRIPT = (
    "import ast, os, shutil, subprocess, sys\n"
    "root = sys.argv[1] if len(sys.argv) > 1 else '.'\n"
    "use_ruff = len(sys.argv) > 2 and sys.argv[2] == 'ruff'\n"
    f"skip = set({list(_SKIP_DIRS)!r})\n"
    "bad = 0\n"
    "for here, dirs, files in os.walk(root):\n"
    "    dirs[:] = sorted(d for d in dirs if d not in skip and not d.endswith('.egg-info'))\n"
    "    for name in sorted(files):\n"
    "        if not name.endswith('.py'):\n"
    "            continue\n"
    "        path = os.path.join(here, name)\n"
    "        try:\n"
    "            with open(path, 'rb') as handle:\n"
    "                ast.parse(handle.read(), filename=path)\n"
    "        except SyntaxError as exc:\n"
    "            bad += 1\n"
    "            print(f'{path}:{exc.lineno}: SyntaxError: {exc.msg}', file=sys.stderr)\n"
    "if use_ruff and shutil.which('ruff'):\n"
    "    bad += subprocess.call(['ruff', 'check', '--quiet', root]) != 0\n"
    "sys.exit(1 if bad else 0)\n"
)

#: Lists unittest ids, one per line, without running anything. Import failures
#: are printed as `ERROR <id>` so a listing never silently shrinks.
_LIST_SCRIPT = (
    "import sys, unittest\n"
    "start, top = sys.argv[1], sys.argv[2]\n"
    "suite = unittest.defaultTestLoader.discover(start, top_level_dir=top)\n"
    "def walk(item):\n"
    "    if isinstance(item, unittest.TestSuite):\n"
    "        for child in item:\n"
    "            yield from walk(child)\n"
    "    else:\n"
    "        yield item\n"
    "for case in walk(suite):\n"
    "    ident = case.id()\n"
    "    if type(case).__name__ == '_FailedTest':\n"
    "        print('ERROR ' + ident.split('._FailedTest.', 1)[-1])\n"
    "    else:\n"
    "        print(ident)\n"
)

#: `test_name (tests.test_mod.TestClass.test_name) ... ok`
_UT_LINE_RE = re.compile(
    r"^(?P<name>\S+) \((?P<ident>[\w.]+)\)"
    r" \.\.\. (?P<status>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b")
#: The id line when the status is not on it: a docstring moves the status to the
#: next line, and a test that prints leaves its output between id and status.
_UT_HEAD_RE = re.compile(r"^(?P<name>\S+) \((?P<ident>[\w.]+)\)(?:\s*$| \.\.\. )")
#: A line that is only a status, after a test printed mid-line.
_UT_BARE_RE = re.compile(
    r"^(?P<status>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b")
#: Any line that ends in a unittest status, for the docstring form.
_UT_TAIL_RE = re.compile(
    r" \.\.\. (?P<status>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b")
#: The status a printing test leaves at the end of its own line.
_UT_END_RE = re.compile(
    r"(?:^|\s)(?P<status>ok|FAIL|ERROR|expected failure|unexpected success)\s*$"
    r"|(?:^|\s)(?P<status2>skipped)\b")
_UT_RAN_RE = re.compile(r"^Ran (?P<count>\d+) tests? in [\d.]+s\s*$")
#: `OK`, `FAILED (failures=1)`, or, from Python 3.12 when a filter selects
#: nothing, `NO TESTS RAN` (exit code 5). An empty run is a finished run.
_UT_RESULT_RE = re.compile(
    r"^(?P<status>OK|FAILED|NO TESTS RAN)\b(?:\s*\((?P<detail>[^)]*)\))?")

_UT_STATUS = {"ok": "ok", "FAIL": "FAILED", "ERROR": "FAILED",
              "skipped": "ignored", "expected failure": "ignored",
              "unexpected success": "FAILED"}

#: `tests/test_mod.py::TestClass::test_name PASSED   [ 50%]`
_PT_LINE_RE = re.compile(
    r"^(?P<nodeid>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
#: `========= 1 failed, 2 passed in 0.12s =========`
_PT_FOOTER_RE = re.compile(r"^=+ (?P<summary>.*?) in [\d.]+s(?: \([^)]*\))? =+\s*$")
_PT_COUNT_RE = re.compile(
    r"(?P<n>\d+) (?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
_PT_COLLECT_ERR_RE = re.compile(r"^(?:_+ )?ERROR collecting (?P<path>\S+)")

_PT_STATUS = {"PASSED": "ok", "FAILED": "FAILED", "ERROR": "FAILED",
              "SKIPPED": "ignored", "XFAIL": "ignored", "XPASS": "FAILED"}

#: Suppressions, leftovers and escapes that fail a card wherever they appear. The
#: `#` patterns look at comments, so they run on the raw line.
BANNED_IN_COMMENTS = (
    (r"#\s*noqa\b", "suppression-noqa"),
    (r"#\s*type:\s*ignore\b", "suppression-type-ignore"),
    (r"#\s*pragma:\s*no\s*cover\b", "suppression-coverage"),
)
BANNED_ANYWHERE = (
    (r"^\s*except\s*:", "bare-except"),
    (r"^\s*except\b[^:]*:\s*pass\s*$", "swallowed-exception"),
    (r"\bbreakpoint\s*\(", "debug-call"),
    (r"\bimport\s+pdb\b|\bpdb\.set_trace\s*\(", "debug-call"),
    (r"@\s*unittest\.skip\w*\b|\.skipTest\s*\(|@\s*pytest\.mark\.skip\w*\b",
     "test-skipped"),
    (r"@\s*unittest\.expectedFailure\b|@\s*pytest\.mark\.xfail\b", "test-skipped"),
    (r"\bimport\s+subprocess\b|\bfrom\s+subprocess\s+import\b|\bsubprocess\.",
     "process-call"),
    (r"\bos\.(?:system|popen|exec\w*|spawn\w*)\s*\(", "process-call"),
    (r"\bimport\s+socket\b|\bfrom\s+socket\s+import\b|\bsocket\.socket\s*\(",
     "network-call"),
    (r"\burllib\.request\b|\bhttp\.client\b|\bimport\s+requests\b|\bhttpx\b",
     "network-call"),
    (r"\beval\s*\(|\bexec\s*\(", "dynamic-exec"),
)
#: Only banned in production code, where a stub left behind is a hidden bug.
BANNED_PRODUCTION = (
    (r"\braise\s+NotImplementedError\b", "leftover-stub"),
)

_TEST_KIND = "test"
_PRODUCTION_KIND = "production"


def detect_runner(worktree: Path) -> str:
    """`pytest` when the repository configures it, otherwise `unittest`."""
    root = Path(worktree)
    if (root / "pytest.ini").is_file():
        return "pytest"
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(errors="replace"))
        except (OSError, ValueError):
            data = {}
        tool = data.get("tool") if isinstance(data, dict) else None
        if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
            return "pytest"
    for name in ("setup.cfg", "tox.ini"):
        cfg = root / name
        if cfg.is_file():
            try:
                text = cfg.read_text(errors="replace")
            except OSError:
                continue
            if re.search(r"^\[(?:tool:)?pytest\]", text, re.MULTILINE):
                return "pytest"
    return "unittest"


class PythonPack(Pack):
    name = "python"
    extension = ".py"
    test_dir = "tests"
    source_dir = ""

    def __init__(self, python: str | None = None, runner: str = "",
                 ruff: bool | None = None) -> None:
        #: The interpreter every command runs under. Defaults to openfarm's own.
        self.python = str(python or sys.executable or "python3")
        #: `unittest` or `pytest`; fixed by `configure` when not given.
        self.runner = runner or "unittest"
        self._runner_fixed = bool(runner)
        self.has_ruff = shutil.which("ruff") is not None if ruff is None else bool(ruff)
        self.worktree: Path | None = None
        self.notes: list[str] = []
        self._note_ruff()

    def _note_ruff(self) -> None:
        note = ("ruff is not installed, so formatting is not checked and lint is a "
                "syntax check only")
        if not self.has_ruff and note not in self.notes:
            self.notes.append(note)

    def configure(self, worktree: Path) -> None:
        self.worktree = Path(worktree)
        if not self._runner_fixed:
            self.runner = detect_runner(Path(worktree))
        if self.runner == "unittest" and not (Path(worktree) / self.test_dir).is_dir():
            note = f"no {self.test_dir}/ folder: unittest discovery finds nothing"
            if note not in self.notes:
                self.notes.append(note)

    # -- commands -------------------------------------------------------------

    def _roots(self, unit: str) -> tuple[str, str]:
        """The start and top-level folders for discovery in this unit.

        A unit is a folder holding its own `pyproject.toml` or `setup.py`. Anything
        else (a package name, `.`, or nothing) means the repository root, because a
        Rust-style package name has no meaning to the Python runner.
        """
        top = unit.strip("/") if unit and unit != "." else "."
        worktree = getattr(self, "worktree", None)
        if top != "." and (worktree is None or not any(
                (Path(worktree) / top / manifest).is_file()
                for manifest in ("pyproject.toml", "setup.py"))):
            top = "."
        start = self.test_dir if top == "." else f"{top}/{self.test_dir}"
        return start, top

    def commands(self, purpose: str, unit: str = "", tests: list[str] | None = None,
                 files: list[str] | None = None,
                 extra_flags: list[str] | None = None) -> dict:
        self.require(purpose, PURPOSES)
        extra = list(extra_flags or [])
        start, top = self._roots(unit)
        py = self.python

        if purpose == "test":
            if self.runner == "pytest":
                argv = [py, "-m", "pytest", "-v", "-p", "no:cacheprovider",
                        "--color=no"]
                if files:
                    argv += [str(f) for f in files]
                elif top != ".":
                    argv.append(top)
                if tests:
                    argv += ["-k", " or ".join(str(t) for t in tests)]
                return {"argv": argv + extra}
            if files:
                modules = [_module_of(str(f), top) for f in files]
                return {"argv": [py, "-m", "unittest", "-v", *modules] + extra}
            argv = [py, "-m", "unittest", "discover", "-s", start, "-t", top, "-v"]
            for name in tests or []:
                argv += ["-k", str(name)]
            return {"argv": argv + extra}

        if purpose == "list":
            if self.runner == "pytest":
                argv = [py, "-m", "pytest", "--collect-only", "-q",
                        "-p", "no:cacheprovider", "--color=no"]
                if top != ".":
                    argv.append(top)
                return {"argv": argv + extra}
            return {"argv": [py, "-c", _LIST_SCRIPT, start, top] + extra}

        if purpose in ("lint", "ws-lint", "typecheck"):
            root = "." if purpose == "ws-lint" else top
            argv = [py, "-c", _CHECK_SCRIPT, root]
            if self.has_ruff and purpose != "typecheck":
                argv.append("ruff")
            return {"argv": argv + extra}

        if purpose == "build":
            return {"argv": [py, "-c", _CHECK_SCRIPT, top] + extra}

        if purpose in ("format", "fmt-check"):
            if not self.has_ruff:
                # Nothing to format with: an honest no-op, named in the dossier.
                return {"argv": [py, "-c", "pass"]}
            argv = ["ruff", "format"]
            if purpose == "fmt-check":
                argv.append("--check")
            return {"argv": argv + [str(f) for f in (files or [])]}

        if purpose == "mutation":
            raise Invalid("no-python-mutation",
                          "the Python pack has no mutation tool in v0.6; set "
                          "[gates] mutation = \"off\"")

        return {"argv": list(extra)}

    # -- listing and parsing --------------------------------------------------

    def parse_list(self, stdout: str) -> dict:
        tests = []
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("ERROR "):
                continue
            if self.runner == "pytest":
                if "::" not in line:
                    continue
                binary, name = _split_nodeid(line)
            else:
                if not re.fullmatch(r"[\w.]+", line) or "." not in line:
                    continue
                binary, _, name = line.rpartition(".")
            tests.append({"name": name, "binary": binary})
        return {"tests": tests}

    def parse_tests(self, stdout: str, stderr: str = "") -> dict:
        text = f"{stdout or ''}\n{stderr or ''}"
        if self.runner == "pytest":
            return _parse_pytest(text)
        return _parse_unittest(text)

    def parse_mutation(self, stdout: str) -> dict:
        raise Invalid("no-python-mutation",
                      "the Python pack has no mutation tool in v0.6")

    # -- files ----------------------------------------------------------------

    def classify_paths(self, paths: list[str]) -> dict:
        """test, production or other.

        A test is anything under a `tests/` folder, a `test_*.py` or `*_test.py`
        file, or a `conftest.py`. Every other `.py` file is production.
        """
        from ..globs import normalise

        out = {}
        for path in paths:
            text = normalise(str(path))
            parts = [part for part in text.split("/") if part]
            base = parts[-1] if parts else ""
            if not base.endswith(".py"):
                out[path] = "other"
                continue
            in_tests = ("tests" in parts[:-1] or "test" in parts[:-1]
                        or base.startswith("test_") or base.endswith("_test.py")
                        or base == "conftest.py")
            out[path] = _TEST_KIND if in_tests else _PRODUCTION_KIND
        return out

    def signatures(self, path: Path) -> dict:
        """Classes, functions and module constants, with every body cut to `...`.

        Phase A must never see a production implementation, so each `def` keeps its
        signature and its docstring's first line, and nothing else.
        """
        path = Path(path)
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            raise Invalid("unreadable", f"cannot read {path}: {exc}") from exc
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # An unparsable file still gets its header lines, never its bodies.
            heads = [line.rstrip() for line in text.splitlines()
                     if re.match(r"^\s*(?:async\s+def|def|class)\s", line)]
            return {"signatures": heads}
        out: list[str] = []
        for node in tree.body:
            rendered = _render_node(node, indent="")
            if rendered:
                out.append(rendered)
        return {"signatures": out}

    def append_stub(self, path: Path, test_name: str, body: str) -> dict:
        """Append a stub that raises `NotImplementedError` at the end of a module.

        Python has no test module inside a source file, so the end is always safe.
        The stub must parse and must raise, so it cannot pass a test by accident.
        """
        path = Path(path)
        text = path.read_text() if path.exists() else ""
        stub = body or ""
        try:
            parsed = ast.parse(stub)
        except SyntaxError as exc:
            raise Refused("non-panicking-stub",
                          f"a stub must be valid Python: {exc.msg}") from exc
        raises = any(
            isinstance(node, ast.Raise) and node.exc is not None
            and "NotImplementedError" in ast.unparse(node.exc)
            for node in ast.walk(parsed))
        if not raises:
            raise Refused("non-panicking-stub",
                          "a stub must raise NotImplementedError")
        content = text.rstrip("\n")
        block = stub if stub.endswith("\n") else stub + "\n"
        lead = content + "\n\n\n" if content.strip() else ""
        path.write_text(lead + block)
        return {"line": lead.count("\n") + 1}

    def format(self, paths: list[Path]) -> dict:
        return self.commands("format", files=[str(p) for p in paths])

    def format_check(self, paths: list[Path]) -> dict:
        return self.commands("fmt-check", files=[str(p) for p in paths])

    # -- patterns -------------------------------------------------------------

    def patterns(self, added_lines: list[dict],
                 flagged_patterns: list[str] | None = None,
                 production_paths: set[str] | None = None) -> dict:
        banned: list[dict] = []
        flagged: list[dict] = []
        production = production_paths or set()
        previous: dict[str, tuple[int, str]] = {}

        for entry in added_lines:
            path = str(entry.get("path", ""))
            line_no = entry.get("line", 0)
            text = str(entry.get("text", ""))
            code = _strip_comment(text)
            is_production = path in production or self.classify_paths(
                [path]).get(path) == _PRODUCTION_KIND

            def hit(label: str) -> None:
                banned.append({"pattern": label, "file": path,
                               "line": line_no, "text": text.strip()})

            for pattern, label in BANNED_IN_COMMENTS:
                if re.search(pattern, text[len(code):]):
                    hit(label)
            if code.strip():
                for pattern, label in BANNED_ANYWHERE:
                    for _match in re.finditer(pattern, code):
                        hit(label)
                if is_production:
                    for pattern, label in BANNED_PRODUCTION:
                        for _match in re.finditer(pattern, code):
                            hit(label)
                # `except X:` then `pass` alone on the next line swallows too.
                prev = previous.get(path)
                if (code.strip() == "pass" and prev is not None
                        and prev[0] == line_no - 1
                        and re.match(r"^\s*except\b[^:]*:\s*$", prev[1])):
                    hit("swallowed-exception")
                previous[path] = (line_no, code)

            for pattern in (flagged_patterns or []):
                try:
                    hits = list(re.finditer(pattern, code))
                except re.error:
                    hits = list(re.finditer(re.escape(pattern), code))
                for _hit in hits:
                    flagged.append({"pattern": pattern, "file": path,
                                    "line": line_no, "text": text.strip()})

        return {"banned": banned, "flagged": flagged}

    # -- errors ---------------------------------------------------------------

    def error_signature(self, stderr: str) -> dict:
        """Exception types, failing test ids and files, with volatile parts gone."""
        raw = stderr or ""
        failed = re.findall(r"^(?:FAIL|ERROR): (\S+)", raw, flags=re.MULTILINE)
        failed += re.findall(r"^FAILED (\S+)", raw, flags=re.MULTILINE)
        failed += re.findall(r"^(\S+::\S+) (?:FAILED|ERROR)\b", raw, flags=re.MULTILINE)
        codes = sorted(set(re.findall(
            r"^\s*(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b",
            raw, flags=re.MULTILINE)))
        files = re.findall(r'File "([^"]+)"', raw)
        items = []
        for name in files:
            base = Path(name).name
            if base and base not in items and "/unittest/" not in name:
                items.append(base)
        text = re.sub(r"/(?:tmp|var|home|Users|usr)/[^\s:)\"']+", "<path>", raw)
        text = re.sub(r"line \d+", "line <n>", text)
        text = re.sub(r"\b\d+\.\d+s\b", "<time>", text)
        text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", text)
        text = re.sub(r"\s+", " ", text)
        failed_sorted = sorted(set(failed))
        payload = ("|".join(codes) + "::" + "|".join(failed_sorted[:12])
                   + "::" + "|".join(items[:12]))
        if not codes and not failed_sorted and not items:
            payload = text.strip()[:200]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return {"signature": digest, "codes": codes, "items": items[:12],
                "failed_tests": failed_sorted[:12]}

    def mutation(self, files: list[str], tests: list[str] | None = None) -> dict:
        return self.commands("mutation", files=files)


# -- helpers -------------------------------------------------------------------


def _module_of(path: str, top: str = ".") -> str:
    """`tests/test_x.py` becomes `tests.test_x`, relative to the top folder."""
    from ..globs import normalise

    text = normalise(path)
    if top not in ("", ".") and text.startswith(top.strip("/") + "/"):
        text = text[len(top.strip("/")) + 1:]
    if text.endswith(".py"):
        text = text[:-3]
    return text.replace("/", ".")


def _split_nodeid(nodeid: str) -> tuple[str, str]:
    """`tests/test_x.py::TestA::test_b[1]` -> (`tests/test_x.py::TestA`, `test_b[1]`)."""
    head, _, name = nodeid.rpartition("::")
    return head, name


def _parse_unittest(text: str) -> dict:
    results: list[dict] = []
    footers: list[dict] = []
    compile_errors: list[dict] = []
    pending: tuple[str, str] | None = None
    ran: int | None = None

    def record(name: str, ident: str, status: str) -> None:
        if ".loader._FailedTest." in ident or ident.startswith("unittest.loader."):
            module = ident.split("._FailedTest.", 1)[-1]
            compile_errors.append({"pkg": module, "target": module, "binary": module})
            return
        binary = ident.rpartition(".")[0]
        results.append({"name": name, "binary": binary,
                        "status": _UT_STATUS.get(status, "FAILED")})

    for line in text.splitlines():
        match = _UT_LINE_RE.match(line)
        if match:
            pending = None
            record(match.group("name"), match.group("ident"), match.group("status"))
            continue
        head = _UT_HEAD_RE.match(line)
        if head:
            pending = (head.group("name"), head.group("ident"))
            # A test that printed to stderr leaves `... noise ok` on one line.
            rest = line[head.end():]
            same = _UT_END_RE.search(rest)
            if same:
                record(pending[0], pending[1],
                       same.group("status") or same.group("status2"))
                pending = None
            continue
        if pending is not None:
            tail = _UT_TAIL_RE.search(line) or _UT_BARE_RE.match(line.strip())
            if tail:
                record(pending[0], pending[1], tail.group("status"))
                pending = None
                continue
        ran_match = _UT_RAN_RE.match(line.strip())
        if ran_match:
            ran = int(ran_match.group("count"))
            continue
        result = _UT_RESULT_RE.match(line.strip())
        if result and ran is not None and not footers:
            counts = dict(re.findall(r"([\w ]+?)=(\d+)", result.group("detail") or ""))
            counts = {key.strip(): int(value) for key, value in counts.items()}
            failed = (counts.get("failures", 0) + counts.get("errors", 0)
                      + counts.get("unexpected successes", 0))
            # An import failure counts as an error in unittest's footer; it is
            # reported as a compile error here, so it leaves the footer count.
            failed = max(0, failed - len(compile_errors))
            footers.append({
                "binary": "unittest",
                "status": "FAILED" if result.group("status") == "FAILED" else "ok",
                "passed": max(0, ran - failed - counts.get("skipped", 0)
                              - counts.get("expected failures", 0)
                              - len(compile_errors)),
                "failed": failed,
                "ignored": counts.get("skipped", 0) + counts.get("expected failures", 0),
                "filtered": 0,
            })
            if not failed and footers[-1]["status"] == "FAILED" and compile_errors:
                footers[-1]["status"] = "ok"

    missing = [] if footers else ["unittest"]
    for error in compile_errors:
        if error["binary"] not in missing:
            missing.append(error["binary"])
    return {"results": results, "footers": footers,
            "missing_footers": missing, "compile_errors": compile_errors}


def _parse_pytest(text: str) -> dict:
    results: list[dict] = []
    footers: list[dict] = []
    compile_errors: list[dict] = []
    for line in text.splitlines():
        match = _PT_LINE_RE.match(line)
        if match:
            binary, name = _split_nodeid(match.group("nodeid"))
            results.append({"name": name, "binary": binary,
                            "status": _PT_STATUS[match.group("status")]})
            continue
        collect = _PT_COLLECT_ERR_RE.match(line.strip())
        if collect:
            path = collect.group("path").rstrip("_ ").strip()
            if not any(e["binary"] == path for e in compile_errors):
                compile_errors.append({"pkg": path, "target": path, "binary": path})
            continue
        footer = _PT_FOOTER_RE.match(line.strip())
        if footer and _PT_COUNT_RE.search(footer.group("summary")):
            counts: dict[str, int] = {}
            for count in _PT_COUNT_RE.finditer(footer.group("summary")):
                kind = count.group("kind")
                kind = "errors" if kind in ("error", "errors") else kind
                counts[kind] = counts.get(kind, 0) + int(count.group("n"))
            errors = max(0, counts.get("errors", 0) - len(compile_errors))
            failed = counts.get("failed", 0) + errors + counts.get("xpassed", 0)
            footers.append({
                "binary": "pytest",
                "status": "FAILED" if failed else "ok",
                "passed": counts.get("passed", 0),
                "failed": failed,
                "ignored": counts.get("skipped", 0) + counts.get("xfailed", 0),
                "filtered": counts.get("deselected", 0),
            })
    missing = [] if footers else ["pytest"]
    for error in compile_errors:
        if error["binary"] not in missing:
            missing.append(error["binary"])
    return {"results": results, "footers": footers,
            "missing_footers": missing, "compile_errors": compile_errors}


def _strip_comment(line: str) -> str:
    """`line` without a trailing `# ...` comment outside string literals."""
    quote = ""
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if ch == "#":
            return line[:i]
        if ch in ("'", '"'):
            quote = ch * 3 if line.startswith(ch * 3, i) else ch
            i += len(quote)
            continue
        i += 1
    return line


def _first_doc_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True) if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) else None
    return doc.strip().splitlines()[0] if doc else ""


def _render_def(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> str:
    import copy

    shell = copy.copy(node)
    shell.body = [ast.Expr(ast.Constant(...))]
    lines = ast.unparse(shell).splitlines()
    doc = _first_doc_line(node)
    if doc:
        lines.insert(len(lines) - 1, f'    """{doc}"""')
    return "\n".join(indent + line for line in lines)


def _render_node(node: ast.AST, indent: str) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _render_def(node, indent)
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases] + [
            ast.unparse(k) for k in node.keywords]
        decorators = [f"{indent}@{ast.unparse(d)}" for d in node.decorator_list]
        head = f"{indent}class {node.name}" + (f"({', '.join(bases)})" if bases else "") + ":"
        body: list[str] = []
        doc = _first_doc_line(node)
        if doc:
            body.append(f'{indent}    """{doc}"""')
        for child in node.body:
            rendered = _render_node(child, indent + "    ")
            if rendered:
                body.append(rendered)
        if not body:
            body.append(f"{indent}    ...")
        return "\n".join(decorators + [head] + body)
    if isinstance(node, ast.AnnAssign) and node.simple:
        target = ast.unparse(node.target)
        if node.value is not None and target.isupper():
            return indent + ast.unparse(node)
        return f"{indent}{target}: {ast.unparse(node.annotation)}"
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name) and node.targets[0].id.isupper():
        rendered = ast.unparse(node)
        if len(rendered) > 160:
            rendered = rendered[:157] + "..."
        return indent + rendered
    return ""


def _raises_not_implemented(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Raise) and stmt.exc is not None
            and "NotImplementedError" in ast.unparse(stmt.exc))


def _is_docstring(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def _stub_function(node: ast.AST) -> bool:
    body = [stmt for stmt in node.body if not _is_docstring(stmt)]
    return len(body) == 1 and _raises_not_implemented(body[0])


def _declaration_only(stmts: list[ast.stmt], in_class: bool) -> bool:
    for stmt in stmts:
        if _is_docstring(stmt) or isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                and stmt.value.value is Ellipsis:
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _stub_function(stmt):
                return False
            continue
        if isinstance(stmt, ast.ClassDef):
            if not _declaration_only(stmt.body, in_class=True):
                return False
            continue
        if isinstance(stmt, ast.AnnAssign):
            # A field or a typed name. In a class it may carry a plain default.
            if stmt.value is None or (in_class and isinstance(stmt.value, ast.Constant)):
                continue
            return False
        if isinstance(stmt, ast.Assign) and not in_class and all(
                isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
            continue
        return False
    return True


def valid_python_scaffold(body: str) -> bool:
    """True when `body` declares contracts and has no behaviour of its own.

    Imports, classes, typed fields and functions whose only statement is
    `raise NotImplementedError`. The scaffold sets the shape a worker fills in;
    any real logic in it would be frontier code nobody tested.
    """
    text = textwrap.dedent(str(body or ""))
    if not text.strip():
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return _declaration_only(tree.body, in_class=False)
