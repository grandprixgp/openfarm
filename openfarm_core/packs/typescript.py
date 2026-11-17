"""The TypeScript language pack.

Shaped by the same principles as the Rust and Python packs:

- **Plain output only.** Node v26 runs `.ts` natively and `node --test --test-reporter=tap`
  prints TAP v13 (`ok N - ...`, `not ok N - ...`, `1..N`, `# tests N`), while spec
  mode prints `✔` / `✖` / `ℹ tests N`. A run with no footer died partway: infra,
  never a pass.
- **Never judge a run by its exit code.** Per-test lines decide.
- **No external dependencies.** `tsc` and `prettier` are used only when installed
  (on PATH or in `node_modules/.bin`); otherwise the pack checks syntax with `node`
  and records explicit notes in `self.notes`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from ..errors import Invalid, Refused
from .base import Pack

PURPOSES = ("build", "test", "list", "lint", "ws-lint", "format", "fmt-check",
            "typecheck", "mutation", "extra")

#: Directories discovery and syntax checks never walk into.
_SKIP_DIRS = (
    ".git", ".hg", "node_modules", "dist", "build", "target",
    ".next", ".nuxt", ".turbo", "coverage", ".openfarm", ".cache",
)

_TS_EXTS = (".ts", ".mts", ".cts", ".tsx")
_JS_EXTS = (".js", ".mjs", ".cjs", ".jsx")
_CODE_EXTS = _TS_EXTS + _JS_EXTS

_TEST_FILE_RE = re.compile(
    r"(?:\.(?:test|spec)|_test)\.(?:ts|mts|cts|tsx|js|mjs|cjs|jsx)$"
)
_NODE_TEST_GLOBS = (
    "tests/**/*.test.{ts,mts,js,mjs}",
    "src/**/*.test.{ts,mts,js,mjs}",
)

#: Syntax check script executed via `node --no-warnings --input-type=module -e`.
_CHECK_SCRIPT = (
    'import fs from "node:fs";\n'
    'import path from "node:path";\n'
    'import { execFileSync } from "node:child_process";\n'
    'import { stripTypeScriptTypes } from "node:module";\n'
    'const root = process.argv[1] || ".";\n'
    f'const skip = new Set({json.dumps(list(_SKIP_DIRS))});\n'
    'const tsExts = new Set([".ts", ".mts", ".cts"]);\n'
    'const jsExts = new Set([".js", ".mjs", ".cjs"]);\n'
    'let bad = 0;\n'
    'function walk(dir) {\n'
    '  let entries;\n'
    '  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }\n'
    '  catch { return; }\n'
    '  entries.sort((a, b) => a.name.localeCompare(b.name));\n'
    '  for (const entry of entries) {\n'
    '    const full = path.join(dir, entry.name);\n'
    '    if (entry.isDirectory()) {\n'
    '      if (!skip.has(entry.name)) walk(full);\n'
    '      continue;\n'
    '    }\n'
    '    if (!entry.isFile()) continue;\n'
    '    const ext = path.extname(entry.name);\n'
    '    if (tsExts.has(ext)) {\n'
    '      try {\n'
    '        const src = fs.readFileSync(full, "utf8");\n'
    '        stripTypeScriptTypes(src, { mode: "strip" });\n'
    '      } catch (err) {\n'
    '        bad += 1;\n'
    '        const msg = String(err && err.message ? err.message : err).split(/\\r?\\n/)[0];\n'
    '        console.error(`${full}: SyntaxError: ${msg}`);\n'
    '      }\n'
    '    } else if (jsExts.has(ext)) {\n'
    '      try {\n'
    '        execFileSync(process.execPath, ["--check", full], { stdio: "pipe" });\n'
    '      } catch (err) {\n'
    '        bad += 1;\n'
    '        const text = String((err && err.stderr) || (err && err.message) || err);\n'
    '        const lines = text.split(/\\r?\\n/).map((l) => l.trim()).filter(Boolean);\n'
    '        const reason = lines.find((l) => /SyntaxError/.test(l)) || lines[lines.length - 1] || "syntax error";\n'
    '        console.error(`${full}: ${reason}`);\n'
    '      }\n'
    '    }\n'
    '  }\n'
    '}\n'
    'walk(root);\n'
    'process.exit(bad ? 1 : 0);\n'
)

#: Lists `test(...)` and `it(...)` names from test files without executing them.
_LIST_SCRIPT = (
    'import fs from "node:fs";\n'
    'import path from "node:path";\n'
    'import { execFileSync } from "node:child_process";\n'
    'import { stripTypeScriptTypes } from "node:module";\n'
    'const root = process.argv[1] || ".";\n'
    f'const skip = new Set({json.dumps(list(_SKIP_DIRS))});\n'
    'const testFileRe = /\\.(?:test|spec)\\.(?:ts|mts|cts|tsx|js|mjs|cjs|jsx)$/;\n'
    'const tsExts = new Set([".ts", ".mts", ".cts"]);\n'
    'const jsExts = new Set([".js", ".mjs", ".cjs"]);\n'
    'const files = [];\n'
    'function walk(dir) {\n'
    '  let entries;\n'
    '  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }\n'
    '  catch { return; }\n'
    '  entries.sort((a, b) => a.name.localeCompare(b.name));\n'
    '  for (const entry of entries) {\n'
    '    const full = path.join(dir, entry.name);\n'
    '    if (entry.isDirectory()) {\n'
    '      if (!skip.has(entry.name)) walk(full);\n'
    '      continue;\n'
    '    }\n'
    '    if (entry.isFile() && testFileRe.test(entry.name)) files.push(full);\n'
    '  }\n'
    '}\n'
    'function extractTests(src) {\n'
    '  const names = [];\n'
    '  const n = src.length;\n'
    '  let i = 0;\n'
    '  let prevCode = "";\n'
    '  function skipWsComments(pos) {\n'
    '    while (pos < n) {\n'
    '      const c = src[pos];\n'
    '      if (c === " " || c === "\\t" || c === "\\n" || c === "\\r") { pos++; continue; }\n'
    '      if (src.startsWith("//", pos)) {\n'
    '        const nl = src.indexOf("\\n", pos + 2);\n'
    '        pos = nl === -1 ? n : nl + 1;\n'
    '        continue;\n'
    '      }\n'
    '      if (src.startsWith("/*", pos)) {\n'
    '        const end = src.indexOf("*/", pos + 2);\n'
    '        pos = end === -1 ? n : end + 2;\n'
    '        continue;\n'
    '      }\n'
    '      break;\n'
    '    }\n'
    '    return pos;\n'
    '  }\n'
    '  function readString(pos) {\n'
    '    const q = src[pos];\n'
    '    let j = pos + 1;\n'
    '    let out = "";\n'
    '    while (j < n) {\n'
    '      const ch = src[j];\n'
    '      if (ch === "\\\\") {\n'
    '        if (j + 1 < n) { out += src[j + 1]; j += 2; continue; }\n'
    '        break;\n'
    '      }\n'
    '      if (q === "`" && ch === "$" && src[j + 1] === "{") return { ok: false, end: pos + 1 };\n'
    '      if (ch === q) return { ok: true, value: out, end: j + 1 };\n'
    '      out += ch;\n'
    '      j++;\n'
    '    }\n'
    '    return { ok: false, end: j };\n'
    '  }\n'
    '  while (i < n) {\n'
    '    const ch = src[i];\n'
    '    if (src.startsWith("//", i)) {\n'
    '      const nl = src.indexOf("\\n", i + 2);\n'
    '      i = nl === -1 ? n : nl + 1;\n'
    '      continue;\n'
    '    }\n'
    '    if (src.startsWith("/*", i)) {\n'
    '      const end = src.indexOf("*/", i + 2);\n'
    '      i = end === -1 ? n : end + 2;\n'
    '      continue;\n'
    '    }\n'
    '    if (ch === "\'" || ch === \'"\' || ch === "`") {\n'
    '      let j = i + 1;\n'
    '      while (j < n) {\n'
    '        if (src[j] === "\\\\") { j += 2; continue; }\n'
    '        if (src[j] === ch) { j++; break; }\n'
    '        j++;\n'
    '      }\n'
    '      prevCode = ch;\n'
    '      i = j;\n'
    '      continue;\n'
    '    }\n'
    '    if (/[A-Za-z_$]/.test(ch)) {\n'
    '      let j = i + 1;\n'
    '      while (j < n && /[A-Za-z0-9_$]/.test(src[j])) j++;\n'
    '      const ident = src.slice(i, j);\n'
    '      const canCall = !prevCode || !/[A-Za-z0-9_$]/.test(prevCode);\n'
    '      if (canCall && (ident === "test" || ident === "it")) {\n'
    '        const before = src.slice(Math.max(0, i - 16), i).replace(/\\s+$/, "");\n'
    '        const dotOk = !before.endsWith(".") || /(?:^|[^A-Za-z0-9_$])t\\.$/.test(before);\n'
    '        if (dotOk) {\n'
    '          let k = skipWsComments(j);\n'
    '          if (src[k] === ".") {\n'
    '            k = skipWsComments(k + 1);\n'
    '            let m = k;\n'
    '            while (m < n && /[A-Za-z0-9_$]/.test(src[m])) m++;\n'
    '            const mod = src.slice(k, m);\n'
    '            if (mod === "skip" || mod === "only" || mod === "todo") {\n'
    '              k = skipWsComments(m);\n'
    '            } else {\n'
    '              k = -1;\n'
    '            }\n'
    '          }\n'
    '          if (k !== -1 && src[k] === "(") {\n'
    '            k = skipWsComments(k + 1);\n'
    '            if (k < n && (src[k] === "\'" || src[k] === \'"\' || src[k] === "`")) {\n'
    '              const str = readString(k);\n'
    '              if (str.ok && str.value) names.push(str.value);\n'
    '            }\n'
    '          }\n'
    '        }\n'
    '      }\n'
    '      prevCode = src[j - 1];\n'
    '      i = j;\n'
    '      continue;\n'
    '    }\n'
    '    if (!/\\s/.test(ch)) prevCode = ch;\n'
    '    i++;\n'
    '  }\n'
    '  return names;\n'
    '}\n'
    'walk(root);\n'
    'for (const full of files) {\n'
    '  const rel = path.relative(".", full).replace(/\\\\/g, "/");\n'
    '  const ext = path.extname(full);\n'
    '  let src = "";\n'
    '  try { src = fs.readFileSync(full, "utf8"); }\n'
    '  catch (err) { console.log(`ERROR ${rel}: ${err.message}`); continue; }\n'
    '  if (tsExts.has(ext)) {\n'
    '    try { stripTypeScriptTypes(src, { mode: "strip" }); }\n'
    '    catch (err) {\n'
    '      const msg = String(err && err.message ? err.message : err).split(/\\r?\\n/)[0];\n'
    '      console.log(`ERROR ${rel}: SyntaxError: ${msg}`);\n'
    '      continue;\n'
    '    }\n'
    '  } else if (jsExts.has(ext)) {\n'
    '    try { execFileSync(process.execPath, ["--check", full], { stdio: "pipe" }); }\n'
    '    catch (err) {\n'
    '      const text = String((err && err.stderr) || (err && err.message) || err);\n'
    '      const lines = text.split(/\\r?\\n/).map((l) => l.trim()).filter(Boolean);\n'
    '      const reason = lines.find((l) => /SyntaxError/.test(l)) || lines[lines.length - 1] || "syntax error";\n'
    '      console.log(`ERROR ${rel}: ${reason}`);\n'
    '      continue;\n'
    '    }\n'
    '  }\n'
    '  for (const name of extractTests(src)) {\n'
    '    const safe = name.replace(/[\\t\\r\\n]+/g, " ");\n'
    '    console.log(`TEST ${rel}\\t${safe}`);\n'
    '  }\n'
    '}\n'
)

#: TAP v13 test result line: `[indent](not )ok N - name [# SKIP|TODO ...]`
_TAP_RESULT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<ok>not ok|ok)\s+\d+\s*-\s*(?P<rest>.+?)\s*$"
)
_TAP_DIRECTIVE_RE = re.compile(r"\s+#\s*(?P<kind>SKIP|TODO)(?:\s.*)?$", re.IGNORECASE)
_TAP_PLAN_RE = re.compile(r"^1\.\.(?P<count>\d+)\s*$")
_TAP_STAT_RE = re.compile(
    r"^#\s*(?P<kind>tests|suites|pass|fail|cancelled|skipped|todo)\s+(?P<n>\d+)\s*$"
)
_TAP_YAML_START_RE = re.compile(r"^(?P<indent>[ \t]+)---\s*$")
_TAP_YAML_END_RE = re.compile(r"^(?P<indent>[ \t]+)\.\.\.\s*$")
_TAP_YAML_TYPE_RE = re.compile(r"^\s*type:\s*['\"]?(?P<kind>\w+)['\"]?\s*$")

#: `node --test` spec reporter lines.
_SPEC_LINE_RE = re.compile(
    r"^\s*(?P<mark>[✔✖﹣-])\s+(?P<rest>.+?)\s*$"
)
_SPEC_DUR_RE = re.compile(r"\s+\(\d+(?:\.\d+)?(?:ms|s)\)(?P<tail>(?:\s+#.*)?)$")
_SPEC_STAT_RE = re.compile(
    r"^ℹ\s+(?P<kind>tests|suites|pass|fail|cancelled|skipped|todo)\s+(?P<n>\d+)\s*$"
)
_SPEC_FAIL_HDR_RE = re.compile(r"^✖\s+failing tests:\s*$")

#: Compile / load failure patterns outside test YAML blocks.
_COMPILE_SYNTAX_RE = re.compile(
    r"^(?:#\s*)?(?:SyntaxError(?:\s*\[[^\]]+\])?:|TSError:|"
    r"Error\s*\[ERR_MODULE_NOT_FOUND\]:|"
    r".*?\berror\s+TS\d{4,5}:)",
)
_COMPILE_LOC_RE = re.compile(
    r"^(?:#\s*)?(?:file://)?(?P<path>[^\s:]+\.(?:ts|mts|cts|tsx|js|mjs|cjs|jsx))(?::\d+(?::\d+)?)?\s*$"
)
_IMPORTED_FROM_RE = re.compile(
    r"imported from (?P<path>\S+\.(?:ts|mts|cts|tsx|js|mjs|cjs|jsx))"
)

#: Comment suppressions (scanned on comment text only).
BANNED_IN_COMMENTS = (
    (r"@ts-ignore\b", "suppression-ts-ignore"),
    (r"@ts-nocheck\b", "suppression-ts-nocheck"),
    (r"eslint-disable\b", "suppression-eslint"),
)

#: Banned anywhere in added code (comments stripped).
#: Tuple format: (regex, label, mask_strings)
BANNED_ANYWHERE = (
    (r"\bdebugger\b", "debug-call", True),
    (r"\.skip\s*\(", "test-skipped", True),
    (r"\.only\s*\(", "test-only", True),
    (r"\b(?:test|it)\.todo\s*\(", "test-todo", True),
    (r"\bchild_process\b", "process-call", False),
    (r"\bfetch\s*\(", "network-call", True),
    (r"\beval\s*\(", "dynamic-exec", True),
    (r"\bnew\s+Function\s*\(", "dynamic-exec", True),
)

#: Banned only in production code (strings retained to catch stub messages).
BANNED_PRODUCTION = (
    (r"""\bthrow\s+new\s+Error\s*\(\s*["'`](?:not\s+implemented|todo)\b""",
     "leftover-stub"),
)

_TEST_KIND = "test"
_PRODUCTION_KIND = "production"

_NOTE_NO_TSC = "tsc is not installed, so typecheck and lint check syntax only"
_NOTE_NO_PRETTIER = "prettier is not installed, so formatting is not checked"


def detect_runner(worktree: Path) -> str:
    """`"npm"` when `package.json` defines a custom non-`node --test` script and
    `node_modules` exists, otherwise `"node"`."""
    root = Path(worktree)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(errors="replace"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            scripts = data.get("scripts")
            if isinstance(scripts, dict):
                test_script = scripts.get("test")
                if isinstance(test_script, str):
                    cleaned = test_script.strip()
                    is_placeholder = "no test specified" in cleaned
                    is_node_test = bool(re.search(
                        r"(?:^|\s)node(?:\s+\S+)*\s+--test(?:\s|$)", cleaned))
                    if cleaned and not is_placeholder and not is_node_test:
                        if (root / "node_modules").is_dir():
                            return "npm"
    return "node"


class TypeScriptPack(Pack):
    name = "typescript"
    extension = ".ts"
    test_dir = "tests"
    source_dir = "src"

    def __init__(self, node: str | None = None, runner: str = "",
                 tsc: bool | None = None, prettier: bool | None = None) -> None:
        self.node = str(node or "node")
        self.runner = runner or "node"
        self._runner_fixed = bool(runner)
        self._tsc_arg = tsc
        self._prettier_arg = prettier
        self.has_tsc = (bool(tsc) if tsc is not None
                        else (shutil.which("tsc") is not None))
        self.tsc_cmd = "tsc"
        self.has_prettier = (bool(prettier) if prettier is not None
                             else (shutil.which("prettier") is not None))
        self.prettier_cmd = "prettier"
        self.worktree: Path | None = None
        self.notes: list[str] = []
        self._refresh_notes()

    def _refresh_notes(self) -> None:
        notes: list[str] = []
        if not self.has_tsc:
            notes.append(_NOTE_NO_TSC)
        if not self.has_prettier:
            notes.append(_NOTE_NO_PRETTIER)
        self.notes = notes

    def configure(self, worktree: Path) -> None:
        root = Path(worktree)
        self.worktree = root
        if not self._runner_fixed:
            self.runner = detect_runner(root)
        if self._tsc_arg is None:
            local_tsc = root / "node_modules" / ".bin" / "tsc"
            if shutil.which("tsc") is not None:
                self.has_tsc = True
                self.tsc_cmd = "tsc"
            elif local_tsc.is_file():
                self.has_tsc = True
                self.tsc_cmd = str(local_tsc)
            else:
                self.has_tsc = False
                self.tsc_cmd = "tsc"
        if self._prettier_arg is None:
            local_prettier = root / "node_modules" / ".bin" / "prettier"
            if shutil.which("prettier") is not None:
                self.has_prettier = True
                self.prettier_cmd = "prettier"
            elif local_prettier.is_file():
                self.has_prettier = True
                self.prettier_cmd = str(local_prettier)
            else:
                self.has_prettier = False
                self.prettier_cmd = "prettier"
        self._refresh_notes()

    # -- commands -------------------------------------------------------------

    def _top(self, unit: str) -> str:
        """Unit folder holding `package.json`/`tsconfig.json`, else `.`."""
        top = unit.strip("/") if unit and unit != "." else "."
        worktree = getattr(self, "worktree", None)
        if top != "." and (worktree is None or not any(
                (Path(worktree) / top / manifest).is_file()
                for manifest in ("package.json", "tsconfig.json"))):
            top = "."
        return top

    def _discover_test_files(self, top: str) -> list[str]:
        if self.worktree is None:
            return []
        base = self.worktree if top == "." else (self.worktree / top)
        if not base.is_dir():
            return []
        found: list[str] = []
        skip = set(_SKIP_DIRS)
        for cur, dirs, filenames in _walk_sorted(base, skip):
            for fname in filenames:
                if _TEST_FILE_RE.search(fname):
                    rel = (cur / fname).relative_to(self.worktree).as_posix()
                    found.append(rel)
        return sorted(found)

    def _has_tsconfig(self, root: str) -> bool:
        if self.worktree is None:
            return True
        if root != "." and (self.worktree / root / "tsconfig.json").is_file():
            return True
        return (self.worktree / "tsconfig.json").is_file()

    def commands(self, purpose: str, unit: str = "", tests: list[str] | None = None,
                 files: list[str] | None = None,
                 extra_flags: list[str] | None = None) -> dict:
        self.require(purpose, PURPOSES)
        extra = list(extra_flags or [])
        top = self._top(unit)

        if purpose == "test":
            name_pattern = _test_name_regex(tests)
            if self.runner == "npm":
                argv = ["npm", "test", "--silent"]
                fwd = [str(f) for f in (files or [])]
                if name_pattern:
                    fwd.append(f"--test-name-pattern={name_pattern}")
                fwd.extend(extra)
                if fwd:
                    argv += ["--", *fwd]
                return {"argv": argv}

            argv = [self.node, "--test", "--test-reporter=tap"]
            if name_pattern:
                argv.append(f"--test-name-pattern={name_pattern}")
            if files:
                targets = [str(f) for f in files]
            else:
                discovered = self._discover_test_files(top)
                if discovered:
                    targets = discovered
                elif top == ".":
                    targets = list(_NODE_TEST_GLOBS)
                else:
                    targets = [f"{top}/{g}" for g in _NODE_TEST_GLOBS]
            return {"argv": argv + extra + targets}

        if purpose == "list":
            return {"argv": [self.node, "--no-warnings", "--input-type=module",
                             "-e", _LIST_SCRIPT, top] + extra}

        if purpose in ("build", "typecheck", "lint", "ws-lint"):
            root = "." if purpose == "ws-lint" else top
            if self.has_tsc and self._has_tsconfig(root):
                argv = [self.tsc_cmd, "--noEmit"]
                if (self.worktree is not None and root != "."
                        and (self.worktree / root / "tsconfig.json").is_file()):
                    argv += ["-p", f"{root}/tsconfig.json"]
                return {"argv": argv + extra}
            return {"argv": [self.node, "--no-warnings", "--input-type=module",
                             "-e", _CHECK_SCRIPT, root] + extra}

        if purpose in ("format", "fmt-check"):
            if not self.has_prettier:
                return {"argv": [self.node, "-e", ""]}
            flag = "--check" if purpose == "fmt-check" else "--write"
            targets = [str(f) for f in (files or [])] or ["."]
            return {"argv": [self.prettier_cmd, flag] + targets + extra}

        if purpose == "mutation":
            raise Invalid(
                "no-typescript-mutation",
                'the TypeScript pack has no mutation tool; set [gates] mutation = "off"',
            )

        return {"argv": list(extra)}

    # -- listing and parsing --------------------------------------------------

    def parse_list(self, stdout: str) -> dict:
        tests: list[dict] = []
        errors: list[str] = []
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("ERROR "):
                errors.append(line[len("ERROR "):])
                continue
            if line.startswith("TEST "):
                payload = line[len("TEST "):]
                binary, sep, name = payload.partition("\t")
                if sep and name:
                    tests.append({"name": name, "binary": binary})
                continue
            if "::" in line:
                binary, _, name = line.partition("::")
                if name:
                    tests.append({"name": name.strip(), "binary": binary.strip()})
        return {"tests": tests, "errors": errors} if errors else {"tests": tests}

    def parse_tests(self, stdout: str, stderr: str = "") -> dict:
        text = f"{stdout or ''}\n{stderr or ''}"
        return _parse_node_output(text)

    def parse_mutation(self, stdout: str) -> dict:
        raise Invalid("no-typescript-mutation",
                      "the TypeScript pack has no mutation tool")

    # -- files ----------------------------------------------------------------

    def classify_paths(self, paths: list[str]) -> dict:
        """`test`, `production`, or `other`."""
        from ..globs import normalise

        out: dict[str, str] = {}
        for path in paths:
            text = normalise(str(path))
            parts = [part for part in text.split("/") if part]
            base = parts[-1] if parts else ""
            if not base.endswith(_CODE_EXTS):
                out[path] = "other"
                continue
            in_test_dir = any(seg in ("tests", "test", "__tests__")
                              for seg in parts[:-1])
            is_test_name = bool(_TEST_FILE_RE.search(base))
            out[path] = (_TEST_KIND if (in_test_dir or is_test_name)
                         else _PRODUCTION_KIND)
        return out

    def signatures(self, path: Path) -> dict:
        """TypeScript/JS declarations with function and method bodies stripped."""
        path = Path(path)
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            raise Invalid("unreadable", f"cannot read {path}: {exc}") from exc
        return {"signatures": _extract_ts_signatures(text)}

    def append_stub(self, path: Path, test_name: str, body: str) -> dict:
        """Append a stub that throws `Error(...)` and has balanced `{}` braces."""
        path = Path(path)
        text = path.read_text() if path.exists() else ""
        stub = body or ""
        stripped = _strip_ts_comments_text(stub)
        if (not re.search(r"\bthrow\s+new\s+Error\b", stripped)
                or not _has_balanced_braces(stub)):
            raise Refused(
                "non-panicking-stub",
                "a stub must throw new Error(...) and have balanced braces",
            )
        content = text.rstrip("\n")
        block = stub if stub.endswith("\n") else stub + "\n"
        lead = content + "\n\n" if content.strip() else ""
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
        block_state: dict[str, bool] = {}
        last_line: dict[str, int] = {}

        for entry in added_lines:
            path = str(entry.get("path", ""))
            line_no = int(entry.get("line", 0) or 0)
            text = str(entry.get("text", ""))
            if last_line.get(path) != line_no - 1:
                block_state[path] = False
            in_block = block_state.get(path, False)
            code, comment, next_block = _split_ts_code_and_comments(text, in_block)
            block_state[path] = next_block
            last_line[path] = line_no

            is_production = (path in production or self.classify_paths(
                [path]).get(path) == _PRODUCTION_KIND)

            def hit(label: str) -> None:
                banned.append({"pattern": label, "file": path,
                               "line": line_no, "text": text.strip()})

            if comment:
                for pattern, label in BANNED_IN_COMMENTS:
                    if re.search(pattern, comment):
                        hit(label)

            if code.strip():
                code_no_str = _mask_ts_strings(code)
                for pattern, label, mask_str in BANNED_ANYWHERE:
                    target = code_no_str if mask_str else code
                    for _match in re.finditer(pattern, target):
                        hit(label)
                if is_production:
                    for pattern, label in BANNED_PRODUCTION:
                        for _match in re.finditer(pattern, code, flags=re.IGNORECASE):
                            hit(label)

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
        """Error codes, TS diagnostics, failing test names, and source files."""
        raw = stderr or ""
        failed = re.findall(
            r"^\s*not ok \d+ - (.+?)(?:\s+#\s*(?:SKIP|TODO).*)?$",
            raw, flags=re.MULTILINE | re.IGNORECASE)
        failed += re.findall(
            r"^\s*✖\s+(.+?)\s+\(\d+(?:\.\d+)?(?:ms|s)\)\s*$",
            raw, flags=re.MULTILINE)
        codes = set(re.findall(r"\b(TS\d{4,5})\b", raw))
        codes.update(re.findall(r"\b(ERR_[A-Z0-9_]+)\b", raw))
        codes.update(re.findall(
            r"^(?:#\s*)?(?:\w+\s+)?([A-Za-z_][\w.]*(?:Error|Exception))\b",
            raw, flags=re.MULTILINE))
        codes_sorted = sorted(codes)

        file_matches = re.findall(
            r"(?:file://)?(?:/[^\s:()\"'`]+/)?([^\s/:()\"'`]+\.(?:ts|mts|cts|tsx|js|mjs|cjs|jsx)):\d+",
            raw)
        items: list[str] = []
        for name in file_matches:
            if name and name not in items:
                items.append(name)

        text = re.sub(r"(?:file://)?/(?:tmp|var|home|Users|usr)/[^\s:)\"']+", "<path>", raw)
        text = re.sub(r":\d+(?::\d+)?\b", ":<loc>", text)
        text = re.sub(r"\b\d+(?:\.\d+)?(?:ms|s)\b", "<time>", text)
        text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", text)
        text = re.sub(r"\s+", " ", text)
        failed_sorted = sorted(set(f.strip() for f in failed if f.strip()))
        payload = ("|".join(codes_sorted) + "::" + "|".join(failed_sorted[:12])
                   + "::" + "|".join(items[:12]))
        if not codes_sorted and not failed_sorted and not items:
            payload = text.strip()[:200]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return {"signature": digest, "codes": codes_sorted, "items": items[:12],
                "failed_tests": failed_sorted[:12]}

    def mutation(self, files: list[str], tests: list[str] | None = None) -> dict:
        return self.commands("mutation", files=files)


# -- helpers ------------------------------------------------------------------


def _walk_sorted(root: Path, skip: set[str]):
    import os
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        yield Path(cur), dirs, sorted(files)


def _test_name_regex(tests: list[str] | None) -> str:
    names = [str(t) for t in (tests or []) if str(t)]
    if not names:
        return ""
    return "^(?:" + "|".join(re.escape(n) for n in names) + ")$"


def _add_compile_error(compile_errors: list[dict], target: str) -> None:
    clean = target.strip() or "syntax-or-import"
    if not any(e["binary"] == clean for e in compile_errors):
        compile_errors.append({"pkg": clean, "target": clean, "binary": clean})


def _parse_node_output(text: str) -> dict:
    """Parse TAP v13 and/or `node --test` spec output."""
    results: list[dict] = []
    footers: list[dict] = []
    compile_errors: list[dict] = []

    tap_stats: dict[str, int] = {}
    spec_stats: dict[str, int] = {}
    tap_plan: int | None = None
    tap_plan_after_results = False
    seen_tap_tests = 0
    seen_tap_version = False
    seen_spec_tests = False
    in_spec_failures = False

    yaml_indent: str | None = None
    last_tap_is_file_compile = False
    last_seen_source_file = ""

    for raw_line in text.splitlines():
        if yaml_indent is not None:
            if _TAP_YAML_END_RE.match(raw_line) and raw_line.startswith(yaml_indent):
                yaml_indent = None
                continue
            type_m = _TAP_YAML_TYPE_RE.match(raw_line)
            if type_m and type_m.group("kind") == "suite" and results:
                results.pop()
            continue

        yaml_start = _TAP_YAML_START_RE.match(raw_line)
        if yaml_start:
            yaml_indent = yaml_start.group("indent")
            continue

        stripped = raw_line.strip()
        if stripped == "TAP version 13":
            seen_tap_version = True
            continue

        loc_m = _COMPILE_LOC_RE.match(raw_line)
        if loc_m:
            last_seen_source_file = loc_m.group("path")

        if _COMPILE_SYNTAX_RE.match(stripped):
            imp_m = _IMPORTED_FROM_RE.search(stripped)
            target = (imp_m.group("path") if imp_m
                      else (last_seen_source_file or stripped.split(":", 1)[0]))
            _add_compile_error(compile_errors, target)
            last_tap_is_file_compile = True

        if _SPEC_FAIL_HDR_RE.match(stripped):
            in_spec_failures = True
            continue

        tap_m = _TAP_RESULT_RE.match(raw_line)
        if tap_m:
            rest = tap_m.group("rest")
            dir_m = _TAP_DIRECTIVE_RE.search(rest)
            if dir_m:
                name = rest[:dir_m.start()].strip()
                status = "ignored"
            else:
                name = rest.strip()
                status = "ok" if tap_m.group("ok") == "ok" else "FAILED"
            seen_tap_tests += 1
            if (status == "FAILED" and last_tap_is_file_compile
                    and _TEST_FILE_RE.search(name)):
                _add_compile_error(compile_errors, name)
                last_tap_is_file_compile = False
                continue
            last_tap_is_file_compile = False
            results.append({"name": name, "binary": "", "status": status})
            continue

        if raw_line and not raw_line[0].isspace():
            plan_m = _TAP_PLAN_RE.match(raw_line)
            if plan_m:
                tap_plan = int(plan_m.group("count"))
                tap_plan_after_results = (seen_tap_tests > 0 or tap_plan == 0)
                continue

        tap_stat = _TAP_STAT_RE.match(stripped)
        if tap_stat:
            tap_stats[tap_stat.group("kind")] = int(tap_stat.group("n"))
            continue

        spec_stat = _SPEC_STAT_RE.match(stripped)
        if spec_stat:
            spec_stats[spec_stat.group("kind")] = int(spec_stat.group("n"))
            continue

        if not in_spec_failures:
            spec_m = _SPEC_LINE_RE.match(raw_line)
            if spec_m:
                mark = spec_m.group("mark")
                rest = spec_m.group("rest")
                tail = ""
                dur_m = _SPEC_DUR_RE.search(rest)
                if dur_m:
                    tail = dur_m.group("tail") or ""
                    name = rest[:dur_m.start()].strip()
                else:
                    name = rest.strip()
                if mark in ("﹣", "-") or "# TODO" in tail.upper() or "# SKIP" in tail.upper():
                    status = "ignored"
                elif mark == "✔":
                    status = "ok"
                else:
                    status = "FAILED"
                seen_spec_tests = True
                results.append({"name": name, "binary": "", "status": status})

    has_tap_footer = ("tests" in tap_stats) or (
        tap_plan is not None and tap_plan_after_results
        and len(results) >= tap_plan
    )
    has_spec_footer = "tests" in spec_stats

    if has_tap_footer:
        passed = tap_stats.get(
            "pass", sum(1 for r in results if r["status"] == "ok"))
        raw_failed = tap_stats.get(
            "fail", sum(1 for r in results if r["status"] == "FAILED"))
        cancelled = tap_stats.get("cancelled", 0)
        failed = max(0, raw_failed + cancelled - len(compile_errors))
        ignored = tap_stats.get("skipped", 0) + tap_stats.get("todo", 0)
        if not ignored:
            ignored = sum(1 for r in results if r["status"] == "ignored")
        footers.append({
            "binary": "node:test",
            "status": "FAILED" if failed else "ok",
            "passed": passed,
            "failed": failed,
            "ignored": ignored,
            "filtered": 0,
        })
    elif has_spec_footer:
        passed = spec_stats.get(
            "pass", sum(1 for r in results if r["status"] == "ok"))
        raw_failed = spec_stats.get(
            "fail", sum(1 for r in results if r["status"] == "FAILED"))
        cancelled = spec_stats.get("cancelled", 0)
        failed = max(0, raw_failed + cancelled - len(compile_errors))
        ignored = spec_stats.get("skipped", 0) + spec_stats.get("todo", 0)
        if not ignored:
            ignored = sum(1 for r in results if r["status"] == "ignored")
        footers.append({
            "binary": "node:test",
            "status": "FAILED" if failed else "ok",
            "passed": passed,
            "failed": failed,
            "ignored": ignored,
            "filtered": 0,
        })

    # If the output had any test lines or TAP header or nothing at all without a
    # footer, mark `missing_footers` so aborted/empty-killed runs are INFRA.
    missing: list[str] = [] if footers else ["node:test"]
    if (seen_tap_version or seen_tap_tests or seen_spec_tests) and not footers:
        missing = ["node:test"]
    for error in compile_errors:
        if error["binary"] not in missing:
            missing.append(error["binary"])

    return {
        "results": results,
        "footers": footers,
        "missing_footers": missing,
        "compile_errors": compile_errors,
    }


# -- lexical scanner & signature extraction -----------------------------------


class _TSScanner:
    """Line-by-line TypeScript/JS lexical masker for strings, regexes, and comments."""

    def __init__(self) -> None:
        self.in_block_comment = False
        self.template_quote = False
        self.template_expr_depth = 0
        self.line_entry_blocked: list[bool] = []

    def mask_line(self, line: str) -> str:
        self.line_entry_blocked.append(self.in_block_comment or self.template_quote)
        n = len(line)
        out = list(line)
        i = 0
        last_sig = ""
        while i < n:
            if self.in_block_comment:
                if line.startswith("*/", i):
                    out[i] = out[i + 1] = " "
                    self.in_block_comment = False
                    i += 2
                    continue
                out[i] = " "
                i += 1
                continue

            if self.template_quote:
                ch = line[i]
                if ch == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if ch == "$" and i + 1 < n and line[i + 1] == "{":
                    out[i] = out[i + 1] = " "
                    self.template_quote = False
                    self.template_expr_depth += 1
                    i += 2
                    continue
                if ch == "`":
                    out[i] = " "
                    self.template_quote = False
                    last_sig = "`"
                    i += 1
                    continue
                out[i] = " "
                i += 1
                continue

            ch = line[i]
            if line.startswith("//", i):
                for k in range(i, n):
                    out[k] = " "
                break
            if line.startswith("/*", i):
                self.in_block_comment = True
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if ch in ("'", '"'):
                j = i + 1
                out[i] = " "
                while j < n:
                    if line[j] == "\\" and j + 1 < n:
                        out[j] = out[j + 1] = " "
                        j += 2
                        continue
                    out[j] = " "
                    if line[j] == ch:
                        j += 1
                        break
                    j += 1
                last_sig = ch
                i = j
                continue
            if ch == "`":
                out[i] = " "
                self.template_quote = True
                i += 1
                continue
            if ch == "}" and self.template_expr_depth > 0:
                out[i] = " "
                self.template_expr_depth -= 1
                self.template_quote = True
                i += 1
                continue
            if ch == "/" and _regex_can_start(last_sig):
                j = i + 1
                in_cls = False
                closed = False
                while j < n:
                    if line[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if line[j] == "[":
                        in_cls = True
                    elif line[j] == "]":
                        in_cls = False
                    elif line[j] == "/" and not in_cls:
                        closed = True
                        j += 1
                        break
                    j += 1
                if closed:
                    while j < n and line[j].isalpha():
                        j += 1
                    for k in range(i, j):
                        out[k] = " "
                    last_sig = "/"
                    i = j
                    continue

            if not ch.isspace():
                last_sig = ch
            i += 1
        return "".join(out)


def _regex_can_start(prev: str) -> bool:
    return prev in ("", "=", "(", "[", ",", ":", ";", "!", "&", "|", "?",
                    "{", "}", "~", "+", "-", "*", "%", "^", "<", ">")


def _split_ts_code_and_comments(line: str, in_block: bool = False) -> tuple[str, str, bool]:
    """Split a single TS/JS line into (code, comment_text, next_in_block)."""
    n = len(line)
    code_chars: list[str] = []
    comment_chars: list[str] = []
    i = 0
    quote = ""
    while i < n:
        if in_block:
            end = line.find("*/", i)
            if end == -1:
                comment_chars.append(line[i:])
                return "".join(code_chars), "".join(comment_chars), True
            comment_chars.append(line[i:end + 2])
            in_block = False
            i = end + 2
            continue
        if quote:
            ch = line[i]
            code_chars.append(ch)
            if ch == "\\" and i + 1 < n:
                code_chars.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if line.startswith("//", i):
            comment_chars.append(line[i:])
            break
        if line.startswith("/*", i):
            in_block = True
            comment_chars.append("/*")
            i += 2
            continue
        ch = line[i]
        code_chars.append(ch)
        if ch in ("'", '"', "`"):
            quote = ch
        i += 1
    return "".join(code_chars), "".join(comment_chars), in_block


def _mask_ts_strings(code: str) -> str:
    """Replace `'...'`, `"..."`, and `` `...` `` literals with spaces."""
    out = list(code)
    n = len(code)
    i = 0
    while i < n:
        ch = code[i]
        if ch in ("'", '"', "`"):
            out[i] = " "
            i += 1
            while i < n:
                if code[i] == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                c = code[i]
                out[i] = " "
                i += 1
                if c == ch:
                    break
            continue
        i += 1
    return "".join(out)


def _strip_ts_comments_text(text: str) -> str:
    parts: list[str] = []
    in_block = False
    for line in text.splitlines():
        code, _, in_block = _split_ts_code_and_comments(line, in_block)
        parts.append(code)
    return "\n".join(parts)


def _has_balanced_braces(text: str) -> bool:
    scanner = _TSScanner()
    depth = 0
    for line in text.splitlines():
        masked = scanner.mask_line(line)
        for ch in masked:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return False
    return depth == 0


_DECL_HEADER_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+|async\s+)*"
    r"(interface|type|enum|class|function|const|let|var)\b"
)
_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|readonly|abstract|override|async|get|set)\s+)*"
    r"(?:constructor|[*]?[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("
)


def _extract_ts_signatures(text: str) -> list[str]:
    """Extract TS declarations with all function and method bodies replaced by `{ ... }`."""
    lines = text.splitlines()
    scanner = _TSScanner()
    masks = [scanner.mask_line(line) for line in lines]
    blocked = scanner.line_entry_blocked
    total = len(lines)
    max_decl_lines = 60
    out: list[str] = []

    def skip_braces(cur: int, depth: int) -> int:
        while depth > 0 and cur + 1 < total:
            cur += 1
            m = masks[cur]
            depth += m.count("{") - m.count("}")
        return cur + 1

    def collect_callable(start_idx: int) -> tuple[list[str], int]:
        """Collect a function or method signature up to its body `{`, replacing body with `{ ... }`."""
        block: list[str] = []
        paren_depth = 0
        angle_depth = 0
        seen_params = False
        type_brace_depth = 0
        expect_type_obj = False
        cur = start_idx
        while cur < total and len(block) < 20:
            line = lines[cur]
            mask = masks[cur]
            body_col = -1
            end_semi = False
            for col, ch in enumerate(mask):
                if type_brace_depth > 0:
                    if ch == "{":
                        type_brace_depth += 1
                    elif ch == "}":
                        type_brace_depth -= 1
                    continue
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    if paren_depth > 0:
                        paren_depth -= 1
                        if paren_depth == 0:
                            seen_params = True
                elif seen_params and paren_depth == 0:
                    if ch == "<":
                        angle_depth += 1
                    elif ch == ">" and (col == 0 or mask[col - 1] != "="):
                        angle_depth = max(0, angle_depth - 1)
                    elif ch in (":", "|", "&"):
                        expect_type_obj = True
                    elif ch == "{":
                        if expect_type_obj or angle_depth > 0:
                            type_brace_depth = 1
                            expect_type_obj = False
                        else:
                            body_col = col
                            break
                    elif ch == ";":
                        end_semi = True
                        break
                    elif not ch.isspace() and ch not in ("=", ",", "?"):
                        if ch.isalnum() or ch in ("_", "$", "]"):
                            expect_type_obj = False
            if body_col != -1:
                prefix = line[:body_col].rstrip()
                block.append(f"{prefix} {{ ... }}" if prefix else "{ ... }")
                tail_mask = mask[body_col:]
                depth = tail_mask.count("{") - tail_mask.count("}")
                return block, skip_braces(cur, depth)
            block.append(line.rstrip())
            if end_semi:
                return block, cur + 1
            cur += 1
        return block, cur

    def collect_brace_block(start_idx: int) -> tuple[list[str], int]:
        block: list[str] = []
        depth = 0
        seen_open = False
        cur = start_idx
        while cur < total:
            line = lines[cur]
            mask = masks[cur]
            if "{" in mask:
                seen_open = True
            depth += mask.count("{") - mask.count("}")
            if len(block) < max_decl_lines:
                block.append(line.rstrip())
            cur += 1
            if seen_open and depth <= 0:
                break
        if len(block) >= max_decl_lines and not block[-1].rstrip().endswith("}"):
            block.append("  // ...")
            block.append("}")
        return block, cur

    def collect_arrow_or_const(start_idx: int) -> tuple[str | None, int]:
        """Keep exported/constant bindings, stripping any arrow function body."""
        line = lines[start_idx]
        mask = masks[start_idx]
        is_exported = bool(re.match(r"^\s*export\b", line))
        m_name = re.match(
            r"^\s*(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
            line,
        )
        name = m_name.group(1) if m_name else ""
        # Collect full statement bounds across lines when unbalanced or ending in `=`, `=>`.
        cur = start_idx
        paren = 0
        brace = 0
        bracket = 0
        seen_eq = False
        arrow_line = -1
        arrow_col = -1
        block_lines: list[str] = []

        while cur < total and len(block_lines) < max_decl_lines:
            cur_line = lines[cur]
            cur_mask = masks[cur]
            col = 0
            while col < len(cur_mask):
                ch = cur_mask[col]
                if ch == "(":
                    paren += 1
                elif ch == ")":
                    paren = max(0, paren - 1)
                elif ch == "[":
                    bracket += 1
                elif ch == "]":
                    bracket = max(0, bracket - 1)
                elif ch == "{":
                    if arrow_line != -1 and brace == 0 and paren == 0 and bracket == 0:
                        prefix = cur_line[:col].rstrip()
                        block_lines.append(f"{prefix} {{ ... }};" if prefix else "{ ... };")
                        tail = cur_mask[col:]
                        depth = tail.count("{") - tail.count("}")
                        return "\n".join(block_lines), skip_braces(cur, depth)
                    if seen_eq and brace == 0 and paren == 0 and bracket == 0:
                        # Non-arrow object initializer: strip body to `{ ... };` so no methods leak.
                        prefix = cur_line[:col].rstrip()
                        tail = cur_mask[col:]
                        depth = tail.count("{") - tail.count("}")
                        next_idx = skip_braces(cur, depth)
                        if not (is_exported or name.isupper()):
                            return None, next_idx
                        block_lines.append(f"{prefix} {{ ... }};" if prefix else "{ ... };")
                        return "\n".join(block_lines), next_idx
                    brace += 1
                elif ch == "}":
                    brace = max(0, brace - 1)
                elif paren == 0 and brace == 0 and bracket == 0:
                    if cur_mask.startswith("=>", col):
                        if seen_eq:
                            arrow_line = cur
                            arrow_col = col + 2
                        col += 2
                        continue
                    if ch == "=" and not cur_mask.startswith("==", col):
                        seen_eq = True
                    elif ch == ";" and arrow_line != -1:
                        # Expression arrow function like `export const f = (x: number) => x + 1;`
                        head_lines = block_lines[:arrow_line - start_idx]
                        head_lines.append(lines[arrow_line][:arrow_col].rstrip() + " { ... };")
                        return "\n".join(head_lines), cur + 1
                    elif ch == ";":
                        block_lines.append(cur_line.rstrip())
                        joined = "\n".join(block_lines)
                        if not (is_exported or name.isupper()):
                            return None, cur + 1
                        if len(joined) > 160:
                            joined = joined[:157] + "..."
                        return joined, cur + 1
                col += 1

            if arrow_line == cur:
                # Single-line or header line with `=>` followed on next line or expression
                next_rest = cur_mask[arrow_col:].strip()
                if next_rest and not next_rest.startswith("{"):
                    # Expression arrow body on same line without `;`
                    block_lines.append(cur_line[:arrow_col].rstrip() + " { ... };")
                    return "\n".join(block_lines), cur + 1

            block_lines.append(cur_line.rstrip())
            # ASI check: if balanced and does not end with operator/open delimiter
            if (paren == 0 and brace == 0 and bracket == 0
                    and cur_mask.strip()
                    and cur_mask.rstrip()[-1] not in ("=", "+", "-", "|", "&", ",", ":")
                    and not cur_mask.rstrip().endswith("=>")):
                if arrow_line != -1:
                    head_lines = block_lines[:arrow_line - start_idx]
                    head_lines.append(lines[arrow_line][:arrow_col].rstrip() + " { ... };")
                    return "\n".join(head_lines), cur + 1
                if not (is_exported or name.isupper()):
                    return None, cur + 1
                joined = "\n".join(block_lines)
                if len(joined) > 160:
                    joined = joined[:157] + "..."
                return joined, cur + 1
            cur += 1
        return (("\n".join(block_lines) if (is_exported or name.isupper()) else None),
                max(cur, start_idx + 1))

    index = 0
    while index < total:
        if blocked[index]:
            index += 1
            continue
        line = lines[index]
        match = _DECL_HEADER_RE.match(line)
        if not match:
            index += 1
            continue

        kw = match.group(1)
        if kw == "function":
            block, next_idx = collect_callable(index)
            out.append("\n".join(block))
            index = max(next_idx, index + 1)
            continue

        if kw in ("interface", "enum"):
            block, next_idx = collect_brace_block(index)
            out.append("\n".join(block))
            index = max(next_idx, index + 1)
            continue

        if kw == "type":
            block: list[str] = []
            depth = 0
            cur = index
            while cur < total and len(block) < max_decl_lines:
                cur_line = lines[cur]
                cur_mask = masks[cur]
                block.append(cur_line.rstrip())
                depth += (cur_mask.count("{") - cur_mask.count("}")
                          + cur_mask.count("(") - cur_mask.count(")")
                          + cur_mask.count("[") - cur_mask.count("]"))
                stripped_m = cur_mask.strip()
                cur += 1
                if depth <= 0:
                    if stripped_m.endswith(";"):
                        break
                    if cur < total and not lines[cur].lstrip().startswith(("|", "&")):
                        if stripped_m and stripped_m[-1] not in ("=", "|", "&"):
                            break
            out.append("\n".join(block))
            index = max(cur, index + 1)
            continue

        if kw in ("const", "let", "var"):
            rendered, next_idx = collect_arrow_or_const(index)
            if rendered:
                out.append(rendered)
            index = max(next_idx, index + 1)
            continue

        if kw == "class":
            block = []
            depth = 0
            seen_open = False
            cur = index
            while cur < total and len(block) < max_decl_lines:
                cur_line = lines[cur]
                cur_mask = masks[cur]
                block.append(cur_line.rstrip())
                if "{" in cur_mask:
                    seen_open = True
                depth += cur_mask.count("{") - cur_mask.count("}")
                cur += 1
                if seen_open:
                    break
            if not seen_open or depth <= 0:
                out.append("\n".join(block))
                index = max(cur, index + 1)
                continue

            truncated = False
            while cur < total and depth > 0:
                if blocked[cur]:
                    cur_mask = masks[cur]
                    depth += cur_mask.count("{") - cur_mask.count("}")
                    cur += 1
                    continue
                cur_line = lines[cur]
                cur_mask = masks[cur]
                if depth == 1 and _METHOD_RE.match(cur_line):
                    m_block, next_idx = collect_callable(cur)
                    if len(block) + len(m_block) < max_decl_lines:
                        block.extend(m_block)
                    elif not truncated:
                        block.append("  // ...")
                        truncated = True
                    cur = max(next_idx, cur + 1)
                    continue
                depth += cur_mask.count("{") - cur_mask.count("}")
                if depth <= 0:
                    block.append(cur_line.rstrip())
                    cur += 1
                    break
                if depth == 1 and cur_line.strip() and not cur_line.strip().startswith("//"):
                    # Field declaration inside class: strip `= <expr>` initializer.
                    cleaned_field = _strip_field_initializer(cur_line, cur_mask)
                    if cleaned_field:
                        if len(block) < max_decl_lines - 1:
                            block.append(cleaned_field)
                        elif not truncated:
                            block.append("  // ...")
                            truncated = True
                cur += 1
            out.append("\n".join(block))
            index = max(cur, index + 1)
            continue

        index += 1

    return out


def _strip_field_initializer(line: str, mask: str) -> str:
    """Strip `= <value>` initializers on class fields so no logic leaks."""
    depth = 0
    for col, ch in enumerate(mask):
        if ch in ("(", "[", "{", "<"):
            depth += 1
        elif ch in (")", "]", "}", ">"):
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0 and not mask.startswith("=>", col):
            prefix = line[:col].rstrip()
            return f"{prefix};" if prefix else ""
    return line.rstrip()
