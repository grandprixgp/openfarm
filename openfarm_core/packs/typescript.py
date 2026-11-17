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

import base64
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from ..errors import Invalid, Refused
from .base import Pack

PURPOSES = ("build", "test", "list", "lint", "ws-lint", "format", "fmt-check",
            "typecheck", "mutation", "extra")

#: Directories discovery and syntax checks never walk into, at any depth.
_SKIP_DIRS = (".git", ".hg", "node_modules", ".openfarm")
#: Build output, skipped only at a unit root (the walk's root, or a folder with
#: a package.json or tsconfig.json): `src/build/` is source, `dist/` is not.
_BUILD_DIRS = ("dist", "build", "target", ".next", ".nuxt", ".turbo", "coverage",
               ".cache")
_UNIT_MANIFESTS = ("package.json", "tsconfig.json")

_TS_EXTS = (".ts", ".mts", ".cts", ".tsx")
_JS_EXTS = (".js", ".mjs", ".cjs", ".jsx")
_CODE_EXTS = _TS_EXTS + _JS_EXTS

#: What a test file is, for eligibility, `classify_paths`, discovery and the list
#: script alike: named `*.test.*`, `*.spec.*` or `*_test.*` with an extension node
#: runs. A file that a card may name as a test is one node is given and lists.
_RUNNABLE_EXTS = (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs")
_TEST_FILE_PATTERN = r"(?:\.(?:test|spec)|_test)\.(?:ts|mts|cts|js|mjs|cjs)$"
_TEST_FILE_RE = re.compile(_TEST_FILE_PATTERN)
#: Test-named `.tsx`/`.jsx`: node --test cannot run them, so never a test file.
_UNRUNNABLE_TEST_RE = re.compile(r"(?:\.(?:test|spec)|_test)\.(?:tsx|jsx)$")
_TEST_DIRS = ("tests", "test", "__tests__")


def is_test_file(path: str) -> bool:
    """The one rule for a TypeScript/JavaScript test file (see `_TEST_FILE_PATTERN`)."""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_TEST_FILE_RE.search(name))


def test_file_problem(path: str) -> str:
    """Why `path` cannot hold a card's named test, or "" when it can."""
    if is_test_file(path):
        return ""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    if _UNRUNNABLE_TEST_RE.search(name):
        return (f"{path!r} is a .tsx/.jsx file, which node --test cannot run; "
                "put the test in a .test.ts or .test.js file")
    return (f"{path!r} is not a test file: node --test is given, and the list reads, "
            "only files named *.test.*, *.spec.* or *_test.* ending in "
            + ", ".join(_RUNNABLE_EXTS))
_NODE_TEST_GLOBS = (
    "tests/**/*.test.{ts,mts,js,mjs}",
    "src/**/*.test.{ts,mts,js,mjs}",
)

#: Lines the pack's own reporter writes to stderr. Code under test cannot write
#: there: `node --test` turns a test file's stdout and stderr into `# ` comments.
_REPORT_MARK = "openfarm-node-test "


def _js(template: str) -> str:
    """A JS source with the pack's shared constants filled in."""
    return (template
            .replace("__SKIP_DIRS__", json.dumps(list(_SKIP_DIRS)))
            .replace("__BUILD_DIRS__", json.dumps(list(_BUILD_DIRS)))
            .replace("__UNIT_MANIFESTS__", json.dumps(list(_UNIT_MANIFESTS)))
            .replace("__TEST_FILE_RE__", json.dumps(_TEST_FILE_PATTERN))
            .replace("__REPORT_MARK__", json.dumps(_REPORT_MARK)))


#: The walk every script shares: which folders are skipped, which files are tests.
_JS_WALK = r'''
const skip = new Set(__SKIP_DIRS__);
const testFileRe = new RegExp(__TEST_FILE_RE__);
function isTestFile(rel) {
  const name = rel.split("/").pop();
  return testFileRe.test(name);
}
const buildDirs = new Set(__BUILD_DIRS__);
const unitManifests = __UNIT_MANIFESTS__;
function walk(dir, onFile, atRoot = true) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
  catch { return; }
  entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const unitRoot = atRoot || unitManifests.some((m) => entries.some(
    (e) => e.name === m && e.isFile()));
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (skip.has(entry.name)) continue;
      if (unitRoot && buildDirs.has(entry.name)) continue;
      walk(full, onFile, false);
      continue;
    }
    if (entry.isFile()) onFile(full);
  }
}
'''

#: Static test names in one source text: `test(...)`/`it(...)` calls with a
#: literal first argument, and how many such calls there are at all.
_JS_EXTRACT = r'''
function extractTests(src) {
  const names = [];
  let calls = 0;
  const n = src.length;
  let i = 0;
  let prevCode = "";
  function skipWsComments(pos) {
    while (pos < n) {
      const c = src[pos];
      if (c === " " || c === "\t" || c === "\n" || c === "\r") { pos++; continue; }
      if (src.startsWith("//", pos)) {
        const nl = src.indexOf("\n", pos + 2);
        pos = nl === -1 ? n : nl + 1;
        continue;
      }
      if (src.startsWith("/*", pos)) {
        const end = src.indexOf("*/", pos + 2);
        pos = end === -1 ? n : end + 2;
        continue;
      }
      break;
    }
    return pos;
  }
  const escapes = { n: "\n", t: "\t", r: "\r" };
  function readString(pos) {
    const q = src[pos];
    let j = pos + 1;
    let out = "";
    while (j < n) {
      const ch = src[j];
      if (ch === "\\") {
        if (j + 1 < n) { out += escapes[src[j + 1]] || src[j + 1]; j += 2; continue; }
        break;
      }
      if (q === "`" && ch === "$" && src[j + 1] === "{") return { ok: false, end: pos + 1 };
      if (ch === q) return { ok: true, value: out, end: j + 1 };
      out += ch;
      j++;
    }
    return { ok: false, end: j };
  }
  while (i < n) {
    const ch = src[i];
    if (src.startsWith("//", i)) {
      const nl = src.indexOf("\n", i + 2);
      i = nl === -1 ? n : nl + 1;
      continue;
    }
    if (src.startsWith("/*", i)) {
      const end = src.indexOf("*/", i + 2);
      i = end === -1 ? n : end + 2;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      let j = i + 1;
      while (j < n) {
        if (src[j] === "\\") { j += 2; continue; }
        if (src[j] === ch) { j++; break; }
        j++;
      }
      prevCode = ch;
      i = j;
      continue;
    }
    if (/[A-Za-z_$]/.test(ch)) {
      let j = i + 1;
      while (j < n && /[A-Za-z0-9_$]/.test(src[j])) j++;
      const ident = src.slice(i, j);
      const canCall = !prevCode || !/[A-Za-z0-9_$]/.test(prevCode);
      if (canCall && (ident === "test" || ident === "it")) {
        const before = src.slice(Math.max(0, i - 16), i).replace(/\s+$/, "");
        const dotOk = !before.endsWith(".") || /(?:^|[^A-Za-z0-9_$])t\.$/.test(before);
        if (dotOk) {
          let k = skipWsComments(j);
          if (src[k] === ".") {
            k = skipWsComments(k + 1);
            let m = k;
            while (m < n && /[A-Za-z0-9_$]/.test(src[m])) m++;
            const mod = src.slice(k, m);
            if (mod === "skip" || mod === "only" || mod === "todo") {
              k = skipWsComments(m);
            } else {
              k = -1;
            }
          }
          if (k !== -1 && src[k] === "(") {
            calls++;
            k = skipWsComments(k + 1);
            if (k < n && (src[k] === "'" || src[k] === '"' || src[k] === "`")) {
              const str = readString(k);
              if (str.ok && str.value) names.push(str.value);
            }
          }
        }
      }
      prevCode = src[j - 1];
      i = j;
      continue;
    }
    if (!/\s/.test(ch)) prevCode = ch;
    i++;
  }
  return { names, calls };
}
function cleanName(name) {
  return String(name).replace(/[\t\r\n]+/g, " ");
}
'''

_JS_SYNTAX = r'''
const tsExts = new Set([".ts", ".mts", ".cts"]);
const jsExts = new Set([".js", ".mjs", ".cjs"]);
function syntaxError(full) {
  const ext = path.extname(full);
  if (tsExts.has(ext)) {
    try {
      stripTypeScriptTypes(fs.readFileSync(full, "utf8"), { mode: "strip" });
    } catch (err) {
      const msg = String(err && err.message ? err.message : err).split(/\r?\n/)[0];
      return `SyntaxError: ${msg}`;
    }
  } else if (jsExts.has(ext)) {
    try {
      execFileSync(process.execPath, ["--check", full], { stdio: "pipe" });
    } catch (err) {
      const text = String((err && err.stderr) || (err && err.message) || err);
      const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
      return lines.find((l) => /SyntaxError/.test(l)) || lines[lines.length - 1] || "syntax error";
    }
  }
  return "";
}
'''

_JS_IMPORTS = r'''
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { stripTypeScriptTypes } from "node:module";
'''

#: Syntax check script executed via `node --no-warnings --input-type=module -e`.
_CHECK_SCRIPT = _js(_JS_IMPORTS + _JS_WALK + _JS_SYNTAX + r'''
const root = process.argv[1] || ".";
let bad = 0;
walk(root, (full) => {
  const reason = syntaxError(full);
  if (reason) {
    bad += 1;
    console.error(`${full}: ${reason}`);
  }
});
process.exit(bad ? 1 : 0);
''')

#: Lists `test(...)` and `it(...)` names from test files without executing them.
_LIST_SCRIPT = _js(_JS_IMPORTS + _JS_WALK + _JS_SYNTAX + _JS_EXTRACT + r'''
const root = process.argv[1] || ".";
const files = [];
walk(root, (full) => {
  const rel = path.relative(".", full).replace(/\\/g, "/");
  if (isTestFile(rel)) files.push(full);
});
for (const full of files) {
  const rel = path.relative(".", full).replace(/\\/g, "/");
  let src = "";
  try { src = fs.readFileSync(full, "utf8"); }
  catch (err) { console.log(`ERROR ${rel}: ${err.message}`); continue; }
  const reason = syntaxError(full);
  if (reason) { console.log(`ERROR ${rel}: ${reason}`); continue; }
  for (const name of extractTests(src).names) {
    console.log(`TEST ${rel}\t${cleanName(name)}`);
  }
}
''')

#: A second `node --test` reporter, beside TAP, writing to stderr: every result
#: with the file it came from, each test file's static listing (unless the run
#: is filtered by name), and the exit code. The parser compares the two, so a
#: test file that stops part way, or never registers its tests, is a failure.
_REPORTER_SCRIPT = _js(r'''
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
''' + _JS_EXTRACT + r'''
const MARK = __REPORT_MARK__;
const filtered = process.execArgv.some(
  (a) => /^--test-(?:name|skip)-pattern/.test(a) || a === "--test-only");
function emit(obj) { return MARK + JSON.stringify(obj) + "\n"; }
function rel(file) {
  let full = String(file);
  if (full.startsWith("file://")) full = fileURLToPath(full);
  return path.relative(process.cwd(), full).split(path.sep).join("/");
}
process.on("exit", (code) => {
  try { fs.writeSync(2, emit({ e: "exit", code })); } catch {}
});
export default async function* openfarmReporter(source) {
  const seen = [];
  const known = new Set();
  for await (const event of source) {
    if (event.type !== "test:pass" && event.type !== "test:fail") continue;
    const data = event.data || {};
    const file = data.file ? rel(data.file) : "";
    if (file && !known.has(file)) { known.add(file); seen.push(file); }
    const details = data.details || {};
    yield emit({
      e: event.type === "test:pass" ? "pass" : "fail",
      file,
      name: String(data.name ?? ""),
      nesting: Number(data.nesting) || 0,
      type: details.type === "suite" ? "suite" : "test",
      skip: data.skip !== undefined && data.skip !== false,
      todo: data.todo !== undefined && data.todo !== false,
    });
  }
  if (!filtered) {
    for (const file of seen) {
      let src;
      try { src = fs.readFileSync(path.resolve(file), "utf8"); } catch { continue; }
      const found = extractTests(src);
      yield emit({ e: "listed", file, names: found.names.map(cleanName), calls: found.calls });
    }
  }
  yield emit({ e: "end", filtered });
}
''')

_REPORTER_URL = ("data:text/javascript;base64,"
                 + base64.b64encode(_REPORTER_SCRIPT.encode("utf-8")).decode("ascii"))

#: TAP v13 test result line: `[indent](not )ok N - name [# SKIP|TODO ...]`
_TAP_RESULT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<ok>not ok|ok)\s+\d+\s*-\s*(?P<rest>.+?)\s*$"
)
_TAP_DIRECTIVE_AT_RE = re.compile(r"#\s*(?:SKIP|TODO)\b", re.IGNORECASE)
_TAP_PLAN_RE = re.compile(r"^1\.\.(?P<count>\d+)\s*$")
_TAP_STAT_RE = re.compile(
    r"^#\s*(?P<kind>tests|suites|pass|fail|cancelled|skipped|todo)\s+(?P<n>\d+)\s*$"
)
_TAP_YAML_START_RE = re.compile(r"^(?P<indent>[ \t]+)---\s*$")

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

#: Comment suppressions (scanned on comment text only). Python's `# type: ignore`
#: is `@ts-ignore`/`@ts-expect-error`/`@ts-nocheck`, its `# noqa` is
#: `eslint-disable`, and its `# pragma: no cover` is a c8/istanbul/v8 ignore.
BANNED_IN_COMMENTS = (
    (r"@ts-ignore\b", "suppression-ts-ignore"),
    (r"@ts-expect-error\b", "suppression-ts-expect-error"),
    (r"@ts-nocheck\b", "suppression-ts-nocheck"),
    (r"eslint-disable\b", "suppression-eslint"),
    (r"\b(?:c8|istanbul|v8|node:coverage)\s+(?:ignore|disable)\b", "suppression-coverage"),
)

#: Bare names; the `node:` spellings are caught anywhere by the rule before.
_NETWORK_MODULES = r"(?:http|https|http2|net|dgram|tls|dns)"

#: Banned anywhere in added code (comments stripped).
#: Tuple format: (regex, label, mask_strings)
BANNED_ANYWHERE = (
    (r"\bdebugger\b", "debug-call", True),
    (r"""["'](?:node:)?inspector(?:/promises)?["']""", "debug-call", False),
    # `test.skip(`, `t.skip(`, `describe.skip(`; the option forms are below.
    (r"\.skip\s*\(", "test-skipped", True),
    (r"\.only\s*\(", "test-only", True),
    (r"\.todo\s*\(", "test-todo", True),
    (r"\bchild_process\b", "process-call", False),
    (r"\bprocess\s*\.\s*(?:binding|_linkedBinding|dlopen)\s*\(", "process-call", True),
    (r"\bfetch\s*\(", "network-call", True),
    (r"""["']node:(?:http|https|http2|net|dgram|tls|dns)(?:/\w+)?["']""", "network-call",
     False),
    (r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+)["']"""
     + _NETWORK_MODULES + r"""(?:/\w+)?["']""", "network-call", False),
    (r"""["']undici["']""", "network-call", False),
    (r"\b(?:XMLHttpRequest|WebSocket|EventSource)\b|\bsendBeacon\s*\(", "network-call",
     True),
    # Any reference to eval, so `(0, eval)(...)` and `globalThis.eval` count too.
    (r"\beval\b", "dynamic-exec", True),
    (r"\bFunction\s*\(", "dynamic-exec", True),
    (r"""["'](?:node:)?vm["']""", "dynamic-exec", False),
    # A module chosen at run time can be anything, child_process included.
    (r"""\b(?:import|require)\s*\(\s*(?!["'][^"'`]*["']\s*[,)])""", "dynamic-exec", False),
    # `globalThis["fetch"]` and the like reach anything by a computed name.
    (r"\b(?:globalThis|global)\s*(?:\?\.\s*)?\[", "dynamic-exec", True),
    # An empty catch, as a statement or a promise handler.
    (r"\bcatch\s*(?:\([^)]*\))?\s*\{\s*\}", "swallowed-exception", True),
    (r"\.catch\s*\(\s*(?:\(\s*\w*\s*\)|\w+)?\s*=>\s*(?:\{\s*\}|undefined\b|null\b|void\s+0\b)",
     "swallowed-exception", True),
)

#: `node:test` options that skip, park or single out a test: `{ skip: true }`,
#: `{ todo: "later" }`, `{ only: true }`, `{ expectFailure: true }`. On the line of
#: a test/it/describe/suite call any value but `false` counts; on a line of its
#: own (a multi-line options object) in a test file, `true` or a string does.
_TEST_CALL_RE = re.compile(r"\b(?:test|it|describe|suite)(?:\.\w+)?\s*\(")
_TEST_OPTION_RE = re.compile(r"(?<![\w$.])(skip|todo|only|expectFailure)\s*:")
_TEST_OPTION_LABELS = {"skip": "test-skipped", "todo": "test-todo", "only": "test-only",
                       "expectFailure": "test-skipped"}

#: A leftover stub in production: any error thrown with a not-implemented message.
#: Python bans `raise NotImplementedError`; the TypeScript spelling has no type of
#: its own, so the message is what marks it.
_STUB_MESSAGE = (r"""["'`]\s*(?:not\s+(?:yet\s+)?implemented|unimplemented|todo|"""
                 r"""stub)\b""")
BANNED_PRODUCTION = (
    (r"\bthrow\s*\(?\s*(?:new\s+)?[\w$.]*\s*\(\s*" + _STUB_MESSAGE, "leftover-stub"),
)
_STUB_RE = re.compile(BANNED_PRODUCTION[0][0], re.IGNORECASE)
#: A line whose throw goes on to the next line.
_OPEN_THROW_RE = re.compile(r"\bthrow\b(?:\s+new)?(?:\s+[\w$.]+)?\s*\(?\s*$")
_OPEN_CATCH_RE = re.compile(r"\bcatch\s*(?:\([^)]*\))?\s*\{\s*$")

_TEST_KIND = "test"
_PRODUCTION_KIND = "production"

_NOTE_NO_TSC = "tsc is not installed, so typecheck and lint check syntax only"
_NOTE_NO_TSCONFIG = "there is no tsconfig.json, so typecheck and lint check syntax only"
_NOTE_SYNTAX_SET = ('[runner] typescript_typecheck is "syntax", so typecheck and lint '
                    "check syntax only")
_NOTE_PRETTIER_UNCONFIGURED = ("prettier is not configured in this project, so "
                               "formatting is not checked")
_NOTE_NO_PRETTIER = "prettier is configured but not installed, so formatting is not checked"
_NOTE_FORMAT_OFF = '[runner] typescript_format is "off", so formatting is not checked'

#: Where prettier reads its settings; one of these, or a `prettier` key in
#: package.json, is what "the project configures prettier" means.
PRETTIER_CONFIG_FILES = (
    ".prettierrc", ".prettierrc.json", ".prettierrc.yaml", ".prettierrc.yml",
    ".prettierrc.json5", ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs",
    ".prettierrc.ts", ".prettierrc.cts", ".prettierrc.mts", ".prettierrc.toml",
    "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs",
    "prettier.config.ts", "prettier.config.cts", "prettier.config.mts",
)

#: `[runner]` settings this pack reads, with the value used when one is absent.
DEFAULT_SETTINGS = {"typescript": "node", "typescript_typecheck": "auto",
                    "typescript_format": "auto", "link_node_modules": True}

_NODE_TEST_SCRIPT_RE = re.compile(r"(?:^|\s|&&|;)node(?:\s+\S+)*\s+--test(?:\s|$)")
_LINK_MARKER = ".openfarm-linked"


def _package_json(root: Path) -> dict:
    try:
        data = json.loads((Path(root) / "package.json").read_text(errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def test_script_problem(worktree: Path) -> str:
    """Why `package.json`'s test script can't be served, or "" when it can.

    Only `node --test` runs are understood: the parser reads node's TAP, not
    jest, vitest or mocha output. A missing script, npm's placeholder and any
    script that runs `node --test` are fine (the pack runs node itself).
    """
    scripts = _package_json(worktree).get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(script, str):
        return ""
    cleaned = script.strip()
    if not cleaned or "no test specified" in cleaned:
        return ""
    if _NODE_TEST_SCRIPT_RE.search(cleaned):
        return ""
    return (f"package.json's test script is {cleaned!r}, which is not a node --test "
            "command. This version of openfarm runs TypeScript and JavaScript tests "
            "only with node --test (node:test); it does not understand jest, vitest "
            "or mocha. If the tests use node:test, set the script to a node --test "
            "command; otherwise this project cannot be run by openfarm yet")


def detect_runner(worktree: Path) -> str:
    """Always `"node"`: the only runner this version has. See `test_script_problem`."""
    return "node"


def base_checkout(worktree: Path) -> Path:
    """The main checkout a `git worktree` folder was made from, or `worktree` itself.

    Read from the worktree's `.git` file and its `commondir`, without running git.
    """
    root = Path(worktree)
    dotgit = root / ".git"
    if not dotgit.is_file():
        return root
    try:
        text = dotgit.read_text(errors="replace").strip()
    except OSError:
        return root
    match = re.match(r"gitdir:\s*(.+)", text)
    if not match:
        return root
    gitdir = Path(match.group(1).strip())
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()
    common = gitdir.parent.parent
    try:
        rel = (gitdir / "commondir").read_text(errors="replace").strip()
        if rel:
            common = (gitdir / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    except OSError:
        pass
    if common.name == ".git" and common.parent.is_dir():
        return common.parent
    return root


def runner_settings(base: Path) -> dict:
    """`[runner]` keys this pack reads, from the base checkout's own config."""
    settings = dict(DEFAULT_SETTINGS)
    path = Path(base) / ".openfarm" / "config.toml"
    try:
        import tomllib
        data = tomllib.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return settings
    runner = data.get("runner") if isinstance(data, dict) else None
    if isinstance(runner, dict):
        for key in DEFAULT_SETTINGS:
            if key in runner:
                settings[key] = runner[key]
    return settings


def settings_from_config(cfg) -> dict:
    """The same settings from a loaded `Config`, for code that has one."""
    return {
        "typescript": getattr(cfg, "runner_typescript", "node"),
        "typescript_typecheck": getattr(cfg, "typescript_typecheck", "auto"),
        "typescript_format": getattr(cfg, "typescript_format", "auto"),
        "link_node_modules": getattr(cfg, "link_node_modules", True),
    }


def prettier_configured(root: Path) -> bool:
    root = Path(root)
    if "prettier" in _package_json(root):
        return True
    return any((root / name).is_file() for name in PRETTIER_CONFIG_FILES)


def _tool_bin(base: Path, name: str) -> str:
    """The base checkout's `node_modules/.bin/<name>`, else `<name>` on PATH, else ""."""
    local = Path(base) / "node_modules" / ".bin" / name
    if local.exists():
        return str(local)
    return name if shutil.which(name) is not None else ""


def resolve_tooling(worktree: Path, settings: dict | None = None) -> dict:
    """One decision, from the base checkout, on the tools every gate uses.

    adopt makes it in the main checkout and records it; `check` and every card
    worktree make the same one, because a worktree's tools are looked up in the
    checkout it came from. Nothing is run.
    """
    root = Path(worktree)
    base = base_checkout(root)
    chosen = dict(DEFAULT_SETTINGS)
    chosen.update(settings if settings is not None else runner_settings(base))
    tsc = _tool_bin(base, "tsc")
    prettier = _tool_bin(base, "prettier")
    has_tsconfig = (root / "tsconfig.json").is_file()
    configured = prettier_configured(root)
    check_notes: list[str] = []
    format_notes: list[str] = []
    problems: list[str] = []

    want_check = str(chosen.get("typescript_typecheck") or "auto")
    if want_check == "tsc":
        typecheck = "tsc"
        if not tsc:
            problems.append('[runner] typescript_typecheck is "tsc" but no tsc was '
                            "found in node_modules/.bin or on PATH")
        if not has_tsconfig:
            problems.append('[runner] typescript_typecheck is "tsc" but there is no '
                            "tsconfig.json")
    elif want_check == "syntax":
        typecheck = "syntax"
        check_notes.append(_NOTE_SYNTAX_SET)
    else:
        typecheck = "tsc" if (tsc and has_tsconfig) else "syntax"
        if not tsc:
            check_notes.append(_NOTE_NO_TSC)
        elif not has_tsconfig:
            check_notes.append(_NOTE_NO_TSCONFIG)

    jsx = jsx_files(root) if typecheck == "syntax" else []
    if jsx:
        check_notes.append(
            f"{len(jsx)} .tsx/.jsx file(s) are not checked (first: {jsx[0]}): node "
            "cannot parse JSX, so only tsc can check them; cards that touch them are "
            "refused until tsc and a tsconfig.json are set up")

    want_format = str(chosen.get("typescript_format") or "auto")
    if want_format == "prettier":
        fmt = "prettier"
        if not prettier:
            problems.append('[runner] typescript_format is "prettier" but no prettier '
                            "was found in node_modules/.bin or on PATH")
    elif want_format == "off":
        fmt = "off"
        format_notes.append(_NOTE_FORMAT_OFF)
    else:
        fmt = "prettier" if (configured and prettier) else "off"
        if not configured:
            format_notes.append(_NOTE_PRETTIER_UNCONFIGURED)
        elif not prettier:
            format_notes.append(_NOTE_NO_PRETTIER)

    return {
        "runner": "node --test",
        "typecheck": typecheck,
        "tsc": tsc if typecheck == "tsc" else "",
        "format": fmt,
        "prettier": prettier if fmt == "prettier" else "",
        "link_node_modules": bool(chosen.get("link_node_modules", True)),
        "base": str(base),
        "test_script": test_script_problem(root),
        "syntax_only": typecheck == "syntax",
        "jsx_files": len(jsx),
        "typecheck_notes": check_notes,
        "format_notes": format_notes,
        "notes": check_notes + format_notes,
        "problems": problems,
    }


_JSX_EXTS = (".tsx", ".jsx")


def jsx_files(root: Path, limit: int = 1000) -> list[str]:
    """`.tsx`/`.jsx` files under `root`, by the shared walk, repository-relative."""
    root = Path(root)
    found: list[str] = []
    if not root.is_dir():
        return found
    for cur, _dirs, files in _walk_sorted(root):
        for name in files:
            if name.endswith(_JSX_EXTS):
                found.append((cur / name).relative_to(root).as_posix())
                if len(found) >= limit:
                    return found
    return found


#: What node's type stripping refuses, and what the owner can do about it.
_STRIP_ADVICE = (
    ("enum", "enums", "replace each enum with a union of literals or an `as const` "
     "object"),
    ("namespace", "namespaces", "turn each namespace into a module of its own, or "
     "`declare` it when it only holds types"),
    ("parameter propert", "constructor parameter properties", "declare the field in "
     "the class and assign it in the constructor"),
)
_RELATIVE_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+|\brequire\s*\(\s*)(["'])(\.{1,2}/[^"'\n]*)\1""")


def adopt_findings(root: Path, node: str = "node", timeout_s: float = 300.0) -> list[str]:
    """What node --test cannot run in this repo, and what to do about each.

    Node strips types only: enums, namespaces and parameter properties are
    refused, and an import of a relative path without its extension is not
    found at run time. Each finding is one line naming the first file.
    """
    import subprocess

    root = Path(root)
    notes: list[str] = []
    try:
        done = subprocess.run([node, "--no-warnings", "--input-type=module", "-e",
                               _CHECK_SCRIPT, "."], cwd=root, capture_output=True,
                              text=True, timeout=timeout_s, check=False)
        errors = done.stderr.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        errors = []
    for needle, what, advice in _STRIP_ADVICE:
        hits = [line.split(": SyntaxError", 1)[0] for line in errors
                if needle in line.lower() and ": SyntaxError" in line]
        if hits:
            first = Path(hits[0])
            try:
                first = first.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                pass
            notes.append(f"node cannot run TypeScript {what} ({len(hits)} file(s), first: "
                         f"{first.as_posix()}): {advice}")
    bare: list[tuple[str, str]] = []
    for cur, _dirs, files in _walk_sorted(root):
        for name in files:
            if not name.endswith(_CODE_EXTS) or name.endswith(".d.ts"):
                continue
            path = cur / name
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for match in _RELATIVE_IMPORT_RE.finditer(text):
                spec = match.group(2)
                last = spec.rsplit("/", 1)[-1]
                if "." not in last.lstrip(".") and not spec.endswith("/"):
                    bare.append((path.relative_to(root).as_posix(), spec))
    if bare:
        files = sorted({file for file, _ in bare})
        file, spec = bare[0]
        notes.append(
            f"{len(files)} file(s) import a relative path without its extension (first: "
            f"{file} imports {spec!r}): node finds only full names, so write "
            f"'{spec}.ts' and set allowImportingTsExtensions (or "
            "rewriteRelativeImportExtensions) in tsconfig.json")
    return notes


def link_node_modules(worktree: Path, base: Path) -> str:
    """Give a card worktree the base checkout's `node_modules`, or say why not.

    The worktree gets a real `node_modules/` folder holding one link per entry
    of the base's folder. A real folder, not one link, because git's common
    `node_modules/` ignore rule matches folders only: a link named
    `node_modules` would be an untracked file in every diff. It is made only
    when git ignores the base's folder, and checked to be ignored in the
    worktree too, or removed. `git worktree remove` deletes the links, never
    what they point at. Returns "" when linked (or nothing to link).
    """
    root = Path(worktree)
    base = Path(base)
    source = base / "node_modules"
    target = root / "node_modules"
    if root.resolve() == base.resolve() or not source.is_dir():
        return ""
    if target.is_symlink() or (target.exists() and not (target / _LINK_MARKER).exists()):
        return ""
    try:
        from ..git import Git
        if Git(base).untracked_files(["node_modules"]):
            return ("node_modules is not linked into card worktrees: git does not "
                    "ignore it in the base checkout")
        target.mkdir(exist_ok=True)
        (target / _LINK_MARKER).write_text("links made by openfarm\n")
        for entry in sorted(source.iterdir()):
            link = target / entry.name
            if not (link.exists() or link.is_symlink()):
                os.symlink(entry, link)
        if Git(root).untracked_files(["node_modules"]):
            shutil.rmtree(target)
            return ("node_modules is not linked into card worktrees: git does not "
                    "ignore it in the worktree")
    except Exception as exc:  # noqa: BLE001 - a link that can't be made is a note
        if target.exists() and (target / _LINK_MARKER).exists():
            shutil.rmtree(target, ignore_errors=True)
        return f"node_modules is not linked into card worktrees: {exc}"
    return ""


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
        self._settings: dict | None = None
        self.has_tsc = (bool(tsc) if tsc is not None
                        else (shutil.which("tsc") is not None))
        self.tsc_cmd = "tsc"
        # prettier only when a project configures it, which configure() reads.
        self.has_prettier = bool(prettier)
        self.prettier_cmd = "prettier"
        self.worktree: Path | None = None
        self.tooling: dict = {}
        self.notes: list[str] = []
        self._refresh_notes()

    def _refresh_notes(self) -> None:
        notes: list[str] = []
        if not self.has_tsc:
            notes.append(_NOTE_NO_TSC)
        if not self.has_prettier:
            notes.append(_NOTE_PRETTIER_UNCONFIGURED)
        self.notes = notes

    def apply_settings(self, cfg) -> None:
        """Use a loaded config's `[runner]` settings instead of reading the file."""
        self._settings = settings_from_config(cfg)

    def configure(self, worktree: Path) -> None:
        root = Path(worktree)
        self.worktree = root
        tooling = resolve_tooling(root, self._settings)
        self.tooling = tooling
        notes: list[str] = []
        if self._tsc_arg is None:
            self.has_tsc = tooling["typecheck"] == "tsc"
            self.tsc_cmd = tooling["tsc"] or "tsc"
            notes += tooling["typecheck_notes"]
        elif not self.has_tsc:
            notes.append(_NOTE_NO_TSC)
        if self._prettier_arg is None:
            self.has_prettier = tooling["format"] == "prettier"
            self.prettier_cmd = tooling["prettier"] or "prettier"
            notes += tooling["format_notes"]
        elif not self.has_prettier:
            notes.append(_NOTE_PRETTIER_UNCONFIGURED)
        if tooling["link_node_modules"]:
            why = link_node_modules(root, Path(tooling["base"]))
            if why:
                notes.append(why)
        elif (Path(tooling["base"]) / "node_modules").is_dir() and \
                Path(tooling["base"]).resolve() != root.resolve():
            notes.append("node_modules is not linked into card worktrees: "
                         "[runner] link_node_modules is false")
        if tooling["test_script"]:
            notes.append(tooling["test_script"])
        self.notes = notes

    def gate_evidence(self, gate: str) -> dict:
        """The lint gates say whether types were checked or only syntax."""
        if gate not in ("lint", "workspace-lint", "ws-lint", "typecheck", "build"):
            return {}
        if self.has_tsc:
            return {"typecheck": "tsc", "syntax_only": False}
        return {"typecheck": "syntax", "syntax_only": True,
                "note": ("syntax only: node's type stripping parsed the code, no "
                         "types were checked (no tsc)")}

    def tooling_summary(self) -> dict:
        """The runner, the type check and the formatter this pack decided on."""
        tooling = self.tooling or {}
        return {
            "pack": self.name,
            "runner": str(tooling.get("runner") or "node --test"),
            "typecheck": "tsc" if self.has_tsc else "syntax",
            "syntax_only": not self.has_tsc,
            "tsc": str(tooling.get("tsc") or "") if self.has_tsc else "",
            "format": "prettier" if self.has_prettier else "off",
            "prettier": (str(tooling.get("prettier") or "") if self.has_prettier
                         else ""),
            "notes": list(self.notes),
        }

    def prepare_worktree(self, worktree: Path) -> str:
        """Link the base checkout's `node_modules` into a trial checkout too.

        The same rule as a card worktree's (`link_node_modules`), so a revert-check
        or catch-up run imports the same packages the card's own runs did. The
        pack's tools and notes stay those of the card's worktree.
        """
        root = Path(worktree)
        settings = (self._settings if self._settings is not None
                    else runner_settings(base_checkout(root)))
        chosen = dict(DEFAULT_SETTINGS)
        chosen.update(settings or {})
        if not bool(chosen.get("link_node_modules", True)):
            return ""
        return link_node_modules(root, base_checkout(root))

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
                if is_test_file(fname):
                    rel = (cur / fname).relative_to(self.worktree).as_posix()
                    found.append(rel)
        return sorted(found)

    def test_files(self, unit: str = "") -> list[str]:
        """The test files a full run of `unit` passes to node, repository-relative."""
        return self._discover_test_files(self._top(unit))

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
            if self.runner != "node":
                raise Invalid(
                    "unsupported-test-runner",
                    f"the TypeScript pack runs tests only with node --test, not "
                    f"{self.runner!r}")
            argv = [self.node, "--test",
                    "--test-reporter=tap", "--test-reporter-destination=stdout",
                    f"--test-reporter={_REPORTER_URL}",
                    "--test-reporter-destination=stderr"]
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
        return _parse_node_output(stdout, stderr)

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
            # A test file (the shared rule), a test-named .tsx/.jsx that node cannot
            # run, or a helper inside a test folder: all the test writer's, frozen
            # after phase A, never production the implementer may rewrite.
            in_test_dir = any(seg in _TEST_DIRS for seg in parts[:-1])
            is_test = is_test_file(base) or bool(_UNRUNNABLE_TEST_RE.search(base))
            out[path] = (_TEST_KIND if (in_test_dir or is_test)
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
        #: The previous added line's code per path, when it left a throw or a
        #: catch open for the next line to finish.
        open_throw: dict[str, str] = {}
        open_catch: set[str] = set()

        for entry in added_lines:
            path = str(entry.get("path", ""))
            line_no = int(entry.get("line", 0) or 0)
            text = str(entry.get("text", ""))
            if last_line.get(path) != line_no - 1:
                block_state[path] = False
                open_throw.pop(path, None)
                open_catch.discard(path)
            in_block = block_state.get(path, False)
            code, comment, next_block = _split_ts_code_and_comments(text, in_block)
            block_state[path] = next_block
            last_line[path] = line_no

            kind = (_PRODUCTION_KIND if path in production
                    else self.classify_paths([path]).get(path))
            is_production = kind == _PRODUCTION_KIND

            def hit(label: str) -> None:
                banned.append({"pattern": label, "file": path,
                               "line": line_no, "text": text.strip()})

            if comment:
                for pattern, label in BANNED_IN_COMMENTS:
                    if re.search(pattern, comment):
                        hit(label)

            if not code.strip():
                continue
            code_no_str = _mask_ts_strings(code)
            for pattern, label, mask_str in BANNED_ANYWHERE:
                target = code_no_str if mask_str else code
                for _match in re.finditer(pattern, target):
                    hit(label)
            on_test_call = bool(_TEST_CALL_RE.search(code_no_str))
            for match in _TEST_OPTION_RE.finditer(code_no_str):
                value = code[match.end():].lstrip()
                if on_test_call:
                    if not re.match(r"false\b", value):
                        hit(_TEST_OPTION_LABELS[match.group(1)])
                elif kind == _TEST_KIND and re.match(r"""true\b|["'`]""", value):
                    hit(_TEST_OPTION_LABELS[match.group(1)])
            if path in open_catch and code_no_str.lstrip().startswith("}"):
                hit("swallowed-exception")
            open_catch.discard(path)
            if _OPEN_CATCH_RE.search(code_no_str):
                open_catch.add(path)
            if is_production:
                own = list(_STUB_RE.finditer(code))
                for _match in own:
                    hit("leftover-stub")
                started = open_throw.pop(path, None)
                if started is not None and not own:
                    joined = started + " " + code.strip()
                    if _STUB_RE.search(joined):
                        hit("leftover-stub")
                    elif len(joined) < 400 and not code.rstrip().endswith((";", "}")):
                        open_throw[path] = joined
                if not own and _OPEN_THROW_RE.search(code_no_str):
                    open_throw[path] = code.strip()

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


def _walk_sorted(root: Path, skip: set[str] | None = None):
    """The same walk as the scripts' `walk`: `_SKIP_DIRS` anywhere, `_BUILD_DIRS`
    only at a unit root."""
    skip = set(_SKIP_DIRS) if skip is None else set(skip)
    root = Path(root)
    for cur, dirs, files in os.walk(root):
        here = Path(cur)
        unit_root = here == root or any(name in files for name in _UNIT_MANIFESTS)
        dirs[:] = sorted(d for d in dirs if d not in skip
                         and not (unit_root and d in _BUILD_DIRS))
        yield here, dirs, sorted(files)


def _test_name_regex(tests: list[str] | None) -> str:
    names = [str(t) for t in (tests or []) if str(t)]
    if not names:
        return ""
    return "^(?:" + "|".join(re.escape(n) for n in names) + ")$"


def _add_compile_error(compile_errors: list[dict], target: str) -> None:
    clean = target.strip() or "syntax-or-import"
    if not any(e["binary"] == clean for e in compile_errors):
        compile_errors.append({"pkg": clean, "target": clean, "binary": clean})


def _parse_node_output(stdout: str, stderr: str = "") -> dict:
    """Parse TAP v13 and/or `node --test` spec output.

    TAP is what the pack's own argv prints. Only node writes raw TAP lines:
    whatever a test file prints, on stdout or stderr, reaches the stream as a
    `# ` comment. So a result row, a plan or a stat line is read only where
    node itself puts one, and nothing inside a YAML block (an error message,
    a stack) is read as anything at all.
    """
    events, err_rest = _split_reporter_lines(stderr)
    text = f"{stdout or ''}\n{err_rest}"
    results: list[dict] = []
    footers: list[dict] = []
    compile_errors: list[dict] = []

    rows: list[dict] = []
    stat_lines: list[tuple[int, str, int]] = []
    top_plans: list[tuple[int, int]] = []
    spec_stats: dict[str, int] = {}
    spec_results: list[dict] = []
    seen_tap_version = False
    seen_spec_tests = False
    in_spec_failures = False

    yaml_indent: str | None = None
    yaml_row: dict | None = None
    last_row_line = -2
    pending_compile: list[str] = []
    last_seen_source_file = ""

    for index, raw_line in enumerate(text.splitlines()):
        line = raw_line.rstrip("\r")
        if yaml_indent is not None:
            # A block ends only at `...` on exactly its own indent. Every other
            # line is the block's, and only its own top-level keys are read.
            if line.rstrip() == yaml_indent + "...":
                yaml_indent = None
                yaml_row = None
                continue
            if (yaml_row is not None and line.startswith(yaml_indent)
                    and len(line) > len(yaml_indent)
                    and not line[len(yaml_indent)].isspace()):
                key, sep, value = line[len(yaml_indent):].partition(":")
                if sep:
                    yaml_row["yaml"].setdefault(key.strip(), _yaml_scalar(value))
            continue

        yaml_start = _TAP_YAML_START_RE.match(line)
        if yaml_start:
            yaml_indent = yaml_start.group("indent")
            yaml_row = None
            if rows and last_row_line == index - 1 and \
                    len(yaml_indent) == len(rows[-1]["indent"]) + 2:
                yaml_row = rows[-1]
            continue

        stripped = line.strip()
        if stripped == "TAP version 13" and not line[:1].isspace():
            seen_tap_version = True
            continue

        loc_m = _COMPILE_LOC_RE.match(line)
        if loc_m:
            last_seen_source_file = loc_m.group("path")

        if _COMPILE_SYNTAX_RE.match(stripped):
            imp_m = _IMPORTED_FROM_RE.search(stripped)
            target = (imp_m.group("path") if imp_m
                      else (last_seen_source_file or stripped.split(":", 1)[0]))
            if seen_tap_version and line.startswith("#"):
                # Something a test file printed. It is a compile error only if
                # the file then failed to load, which its own row will say.
                pending_compile.append(target)
            else:
                _add_compile_error(compile_errors, target)

        tap_m = _TAP_RESULT_RE.match(line)
        if tap_m:
            name, status = _tap_name_status(tap_m.group("rest"), tap_m.group("ok"))
            indent = tap_m.group("indent")
            row = {"name": name, "status": status, "indent": indent,
                   "line": index, "yaml": {}, "compile": []}
            if indent == "":
                row["compile"] = pending_compile
                pending_compile = []
            rows.append(row)
            last_row_line = index
            continue

        if line and not line[0].isspace():
            plan_m = _TAP_PLAN_RE.match(line)
            if plan_m:
                top_plans.append((index, int(plan_m.group("count"))))
                continue
            tap_stat = _TAP_STAT_RE.match(line)
            if tap_stat:
                stat_lines.append((index, tap_stat.group("kind"), int(tap_stat.group("n"))))
                continue

        if seen_tap_version:
            continue

        if _SPEC_FAIL_HDR_RE.match(stripped):
            in_spec_failures = True
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
                spec_results.append({"name": name, "binary": "", "status": status})

    report = _reporter_summary(events)
    file_rows = report["file_rows"]

    for row in rows:
        kind = str(row["yaml"].get("type", "")).strip("'\"")
        if kind == "suite":
            # A suite is a container: passing or skipped, it is not a test. Failing
            # on its own (its body threw, a hook failed) it is a red row; failing
            # only because a test in it failed, that test is already the red row.
            if row["status"] != "FAILED":
                continue
            if str(row["yaml"].get("failureType", "")).strip("'\"") == "subtestsFailed":
                continue
            results.append({"name": row["name"], "binary": "", "status": "FAILED"})
            continue
        if row["indent"] == "" and (row["name"] in file_rows
                                    or _looks_like_file_row(row)):
            # Node's own row for a whole test file: the file failed to load or
            # exited badly (red), or it ran no test at all (not a pass).
            if row["status"] == "FAILED":
                if row["compile"]:
                    for target in row["compile"]:
                        _add_compile_error(compile_errors, target)
                    _add_compile_error(compile_errors, row["name"])
                    continue
                results.append({"name": row["name"], "binary": row["name"],
                                "status": "FAILED"})
            continue
        results.append({"name": row["name"], "binary": "", "status": row["status"]})

    if not seen_tap_version:
        results.extend(spec_results)

    # The footer is node's: the last top-level plan, with nothing after it but
    # its stat lines, numbering exactly the top-level rows.
    final_plan = top_plans[-1] if top_plans else None
    top_rows = [row for row in rows if row["indent"] == ""]
    tap_stats: dict[str, int] = {}
    has_tap_footer = False
    if final_plan is not None:
        plan_line, plan_count = final_plan
        has_tap_footer = (plan_count == len(top_rows)
                          and not any(row["line"] > plan_line for row in top_rows))
        if has_tap_footer:
            tap_stats = {kind: n for line_no, kind, n in stat_lines if line_no > plan_line}
    has_spec_footer = not seen_tap_version and "tests" in spec_stats

    if has_tap_footer or has_spec_footer:
        stats = tap_stats if has_tap_footer else spec_stats
        passed = stats.get("pass", sum(1 for r in results if r["status"] == "ok"))
        raw_failed = stats.get(
            "fail", sum(1 for r in results if r["status"] == "FAILED"))
        cancelled = stats.get("cancelled", 0)
        failed = max(0, raw_failed + cancelled - len(compile_errors))
        ignored = stats.get("skipped", 0) + stats.get("todo", 0)
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

    if report["seen"]:
        results.extend(_lost_tests(report))
        exit_code = report["exit"]
        if (footers and exit_code not in (None, 0) and not compile_errors
                and not any(r["status"] == "FAILED" for r in results)):
            results.append({"name": f"node --test exited with code {exit_code}",
                            "binary": "node:test", "status": "FAILED"})

    # No footer means the run died partway (or never started): infra, never a pass.
    missing: list[str] = [] if footers else ["node:test"]
    if report["seen"] and not report["end"] and "node:test" not in missing:
        missing.append("node:test")
    for error in compile_errors:
        if error["binary"] not in missing:
            missing.append(error["binary"])

    parsed = {
        "results": results,
        "footers": footers,
        "missing_footers": missing,
        "compile_errors": compile_errors,
    }
    if report["seen"]:
        parsed["files"] = {file: sorted(names)
                           for file, names in sorted(report["reported"].items())}
        parsed["file_status"] = {
            file: dict(report["status"].get(file, {"ok": 0, "failed": 0, "ignored": 0}))
            for file in parsed["files"]}
    return parsed


def _split_reporter_lines(stderr: str) -> tuple[list[dict], str]:
    """The pack reporter's lines out of stderr, and everything else."""
    events: list[dict] = []
    rest: list[str] = []
    for line in (stderr or "").splitlines():
        if line.startswith(_REPORT_MARK):
            try:
                payload = json.loads(line[len(_REPORT_MARK):])
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                events.append(payload)
                continue
        rest.append(line)
    return events, "\n".join(rest)


def _reporter_summary(events: list[dict]) -> dict:
    reported: dict[str, set[str]] = {}
    status: dict[str, dict[str, int]] = {}
    skipped_in: set[str] = set()
    file_rows: set[str] = set()
    listed: dict[str, tuple[list[str], int]] = {}
    exit_code = None
    end = False
    for event in events:
        kind = event.get("e")
        file = str(event.get("file", "") or "")
        if kind in ("pass", "fail"):
            name = _clean_name(event.get("name", ""))
            if int(event.get("nesting", 0) or 0) == 0 and name == file:
                file_rows.add(file)
                reported.setdefault(file, set())
                continue
            if event.get("skip"):
                skipped_in.add(file)
            if event.get("type") == "suite":
                reported.setdefault(file, set())
                continue
            reported.setdefault(file, set()).add(name)
            counts = status.setdefault(file, {"ok": 0, "failed": 0, "ignored": 0})
            if event.get("skip") or event.get("todo"):
                counts["ignored"] += 1
            elif kind == "pass":
                counts["ok"] += 1
            else:
                counts["failed"] += 1
        elif kind == "listed":
            names = [_clean_name(n) for n in (event.get("names") or [])]
            listed[file] = (names, int(event.get("calls", 0) or 0))
        elif kind == "exit":
            try:
                exit_code = int(event.get("code"))
            except (TypeError, ValueError):
                exit_code = None
        elif kind == "end":
            end = True
    return {"seen": bool(events), "reported": reported, "status": status,
            "skipped_in": skipped_in,
            "file_rows": file_rows, "listed": listed, "exit": exit_code, "end": end}


def _lost_tests(report: dict) -> list[dict]:
    """Red rows for tests a file's static listing has and the runner never reported.

    A file that reported nothing lost every listed test (it exited, or never
    registered them). A file with a skipped test or suite can hide subtests the
    listing sees, so a partial gap there is not counted.
    """
    out: list[dict] = []
    for file, (names, calls) in sorted(report["listed"].items()):
        got = report["reported"].get(file, set())
        if not got:
            lost = names
            if not names and calls:
                out.append({"name": f"{file}: registered no tests", "binary": file,
                            "status": "FAILED"})
        elif file in report["skipped_in"]:
            lost = []
        else:
            lost = [name for name in names if name not in got]
        seen: set[str] = set()
        for name in lost:
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "binary": file, "status": "FAILED",
                        "reason": "never-reported"})
    return out


def _clean_name(name) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(name))


def _tap_name_status(rest: str, ok: str) -> tuple[str, str]:
    """A TAP row's name with node's escapes (`\\#`, `\\\\`) undone, and its status."""
    out: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if ch == "\\" and i + 1 < n and rest[i + 1] in ("\\", "#"):
            out.append(rest[i + 1])
            i += 2
            continue
        if ch == "#" and (i == 0 or rest[i - 1].isspace()) and \
                _TAP_DIRECTIVE_AT_RE.match(rest, i):
            return "".join(out).strip(), "ignored"
        out.append(ch)
        i += 1
    return "".join(out).strip(), ("ok" if ok == "ok" else "FAILED")


def _yaml_scalar(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _looks_like_file_row(row: dict) -> bool:
    """Node's row for a test file is named by its path and has no subtests."""
    name = row["name"]
    if not name or any(ch.isspace() for ch in name):
        return False
    if not name.endswith(_CODE_EXTS):
        return False
    return (is_test_file(name) or bool(_UNRUNNABLE_TEST_RE.search(name))
            or "exitCode" in row["yaml"])


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
        #: The word that ends at `last_sig`, and whether it follows a `.` (a
        #: property such as `x.return` is not the keyword).
        last_word = ""
        word_is_property = False
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
            if ch == "/" and (_regex_can_start(last_sig) or (
                    last_word in _REGEX_AFTER_WORDS and not word_is_property
                    and (last_sig.isalnum() or last_sig in "_$"))):
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
                if ch.isalnum() or ch in "_$":
                    if i > 0 and (line[i - 1].isalnum() or line[i - 1] in "_$") \
                            and last_word:
                        last_word += ch
                    else:
                        word_is_property = last_sig == "."
                        last_word = ch
                else:
                    last_word = ""
                last_sig = ch
            i += 1
        return "".join(out)


#: Keywords after which a `/` starts a regex literal, not a division.
_REGEX_AFTER_WORDS = frozenset({
    "return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
    "throw", "yield", "await", "instanceof",
})


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
    decorators: list[str] = []
    while index < total:
        if blocked[index]:
            index += 1
            continue
        line = lines[index]
        if masks[index].lstrip().startswith("@"):
            # Decorators belong to the class below them: keep them with it.
            cur, depth, found = index, 0, []
            while cur < total and len(found) < 10:
                found.append(lines[cur].rstrip())
                depth += sum(masks[cur].count(c) for c in "({[") \
                    - sum(masks[cur].count(c) for c in ")}]")
                cur += 1
                if depth <= 0 and not (cur < total and masks[cur].lstrip().startswith("@")):
                    break
            following = _DECL_HEADER_RE.match(lines[cur]) if cur < total else None
            decorators = found if following and following.group(1) == "class" else []
            index = max(cur, index + 1)
            continue
        match = _DECL_HEADER_RE.match(line)
        if not match:
            decorators = []
            index += 1
            continue

        kw = match.group(1)
        pending, decorators = decorators, []
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
            block = list(pending)
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


# -- scaffold contracts --------------------------------------------------------
#
# A scaffold sets the shape a worker fills in, so, as for Rust and Python, it may
# only declare: imports, re-exports, types, interfaces, ambient `declare`s, and
# functions, methods, accessors and arrows whose whole body is one
# `throw new Error(...)`. Anything this grammar does not know is refused, and a
# type is skipped only where node's type stripping erases it too.


class _ScaffoldRefused(Exception):
    pass


_SCAFFOLD_PUNCT = ("...", "=>", "?.")
#: Words after which a type, a declaration or a statement must go on.
_PREFIX_WORDS = frozenset({
    "export", "default", "declare", "abstract", "async", "const", "let", "var",
    "function", "class", "interface", "type", "enum", "namespace", "module", "global",
    "extends", "implements", "keyof", "typeof", "infer", "is", "as", "readonly",
    "unique", "new", "asserts", "import", "from", "static", "public", "private",
    "protected", "override", "accessor", "get", "set",
})
#: Tokens that cannot start a statement, so a line starting with one goes on.
_CONTINUES = frozenset({"|", "&", ".", "=>", "extends", "?", ":"})
#: Before these a `{` opens an object type, not a body.
_TYPE_OPERAND_BEFORE = frozenset({
    "", ":", "|", "&", "=>", "<", ",", "(", "[", "?", "keyof", "typeof", "readonly",
    "extends", "is", "infer", "=", "unique", "asserts",
})
_PARAM_MODIFIERS = frozenset({"public", "private", "protected", "readonly", "override"})
_MEMBER_MODIFIERS = frozenset({"public", "private", "protected", "static", "readonly",
                               "abstract", "override", "declare", "async", "accessor"})
_LITERAL_WORDS = frozenset({"true", "false", "null", "undefined"})


def _scaffold_tokens(text: str) -> list[tuple[str, str, bool]]:
    """(kind, text, newline before) for `text`; comments dropped.

    Kinds: id, str, tpl (a template with no substitution), num, punct. A template
    substitution or an identifier escape is refused outright, so the tokens never
    drift from what node reads.
    """
    out: list[tuple[str, str, bool]] = []
    i, n = 0, len(text)
    newline = False
    while i < n:
        ch = text[i]
        if ch in " \t\r\f\v﻿":
            i += 1
            continue
        if ch == "\n" or ch in "  ":
            newline = True
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise _ScaffoldRefused("an unclosed /* comment")
            if "\n" in text[i:end]:
                newline = True
            i = end + 2
            continue
        start = i
        if ch in ("'", '"'):
            i += 1
            while i < n and text[i] != ch:
                if text[i] == "\n":
                    raise _ScaffoldRefused("a string that runs past its line")
                i += 2 if text[i] == "\\" else 1
            if i >= n:
                raise _ScaffoldRefused("an unclosed string")
            i += 1
            out.append(("str", text[start:i], newline))
        elif ch == "`":
            i += 1
            while i < n and text[i] != "`":
                if text.startswith("${", i):
                    raise _ScaffoldRefused("a template with a ${...} substitution")
                i += 2 if text[i] == "\\" else 1
            if i >= n:
                raise _ScaffoldRefused("an unclosed template")
            i += 1
            out.append(("tpl", text[start:i], newline))
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            i += 1
            while i < n and (text[i].isalnum() or text[i] in "._"):
                i += 1
            out.append(("num", text[start:i], newline))
        elif ch.isalpha() or ch in "_$#" or ord(ch) > 127:
            i += 1
            while i < n and (text[i].isalnum() or text[i] in "_$" or ord(text[i]) > 127):
                i += 1
            if not text[start:i].lstrip("#").replace("$", "a").replace("_", "a").isidentifier():
                raise _ScaffoldRefused(f"an odd name {text[start:i]!r}")
            out.append(("id", text[start:i], newline))
        elif ch == "\\":
            raise _ScaffoldRefused("an escape outside a string")
        else:
            for punct in _SCAFFOLD_PUNCT:
                if text.startswith(punct, i):
                    i += len(punct)
                    break
            else:
                i += 1
            out.append(("punct", text[start:i], newline))
        newline = False
    return out


class _ScaffoldParser:
    """Declarations and throwing stubs only; raises `_ScaffoldRefused` otherwise."""

    def __init__(self, tokens: list[tuple[str, str, bool]]) -> None:
        self.toks = tokens
        self.i = 0

    # -- token helpers --------------------------------------------------------

    def peek(self, ahead: int = 0) -> str:
        j = self.i + ahead
        return self.toks[j][1] if j < len(self.toks) else ""

    def kind(self, ahead: int = 0) -> str:
        j = self.i + ahead
        return self.toks[j][0] if j < len(self.toks) else ""

    def newline_before(self, ahead: int = 0) -> bool:
        j = self.i + ahead
        return self.toks[j][2] if j < len(self.toks) else True

    def at_end(self) -> bool:
        return self.i >= len(self.toks)

    def take(self, want: str | None = None) -> str:
        if self.at_end():
            raise _ScaffoldRefused(f"the text ends where {want or 'more'} was expected")
        value = self.toks[self.i][1]
        if want is not None and value != want:
            raise _ScaffoldRefused(f"{value!r} where {want!r} was expected")
        self.i += 1
        return value

    def take_name(self) -> str:
        if self.kind() != "id":
            raise _ScaffoldRefused(f"{self.peek() or 'the end'!r} where a name was expected")
        return self.take()

    def optional(self, want: str) -> bool:
        if self.peek() == want and not self.at_end():
            self.i += 1
            return True
        return False

    def prev(self) -> tuple[str, str]:
        if self.i == 0:
            return "", ""
        kind, value, _ = self.toks[self.i - 1]
        return kind, value

    def prev_ends_value(self) -> bool:
        kind, value = self.prev()
        if kind in ("str", "tpl", "num"):
            return True
        if kind == "id":
            return value not in _PREFIX_WORDS
        return value in (")", "]", "}", ">")

    def statement_ends_here(self) -> bool:
        """At depth 0: an explicit `;`, the end, or a line break node would end on."""
        if self.at_end():
            return True
        return (self.newline_before() and self.prev_ends_value()
                and self.peek() not in _CONTINUES)

    def end_statement(self) -> None:
        if self.optional(";"):
            return
        if self.at_end() or self.peek() == "}" or self.newline_before():
            return
        raise _ScaffoldRefused(f"{self.peek()!r} after a declaration")

    def skip_balanced(self) -> None:
        """Skip a bracketed group starting at an opener; its contents are types."""
        pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
        stack = [pairs[self.take()]]
        while stack:
            if self.at_end():
                raise _ScaffoldRefused("an unclosed bracket")
            value = self.take()
            if value in pairs and (value != "<" or stack[-1] == ">"):
                stack.append(pairs[value])
            elif value == "<" and stack[-1] != ">":
                continue
            elif value == stack[-1]:
                stack.pop()
            elif value in (")", "]", "}") and value != stack[-1]:
                if stack[-1] == ">":
                    raise _ScaffoldRefused("an odd bracket inside < >")
                raise _ScaffoldRefused("unbalanced brackets")

    def skip_type(self, stops: tuple[str, ...], body_may_follow: bool = False) -> None:
        """Skip a type: it is erased by node, so its contents never run.

        Ends before a depth-0 token in `stops`, before a `{` that opens a body
        (when `body_may_follow`), or at a line break node would end the type on.
        """
        started = False
        while not self.at_end():
            value = self.peek()
            if started and self.statement_ends_here():
                return
            if value in stops:
                return
            if value == "{":
                if body_may_follow and self.prev()[1] not in _TYPE_OPERAND_BEFORE \
                        and started:
                    return
                self.skip_balanced()
            elif value in ("(", "[", "<"):
                self.skip_balanced()
            elif value in (")", "]", "}", ">", ";"):
                return
            elif value == "@":
                raise _ScaffoldRefused("a decorator")
            else:
                self.take()
            started = True

    # -- grammar --------------------------------------------------------------

    def statements(self) -> None:
        while not self.at_end():
            if self.optional(";"):
                continue
            self.statement()

    def statement(self) -> None:
        word = self.peek()
        if word == "import":
            self.import_decl()
        elif word == "export":
            self.take()
            if self.optional("default"):
                if self.peek() in ("function", "class", "abstract", "async", "interface"):
                    self.declaration(allow_anonymous=True)
                    return
                raise _ScaffoldRefused("export default of a value")
            if self.peek() in ("{", "*") or (self.peek() == "type"
                                              and self.peek(1) in ("{", "*")):
                self.reexport()
                return
            if self.peek() == "=":
                raise _ScaffoldRefused("export =")
            self.declaration()
        else:
            self.declaration()

    def import_decl(self) -> None:
        self.take("import")
        if self.kind() == "str":
            raise _ScaffoldRefused("an import run only for its side effects")
        self.optional("type")
        while not self.at_end() and self.peek() != "from":
            value = self.peek()
            if value == "{":
                self.skip_balanced()
            elif value in ("*", ",", "as") or self.kind() == "id":
                self.take()
                if self.peek() == "=":
                    raise _ScaffoldRefused("import x = require(...)")
            else:
                raise _ScaffoldRefused(f"{value!r} in an import")
        self.take("from")
        if self.kind() != "str":
            raise _ScaffoldRefused("an import from something other than a string")
        self.take()
        if self.peek() in ("with", "assert") and not self.newline_before():
            self.take()
            if self.peek() != "{":
                raise _ScaffoldRefused("odd import attributes")
            self.skip_balanced()
        self.end_statement()

    def reexport(self) -> None:
        self.optional("type")
        if self.optional("*"):
            if self.optional("as"):
                self.take_name()
            self.take("from")
        else:
            self.take("{")
            while not self.optional("}"):
                if self.kind() == "id" or self.peek() == ",":
                    self.take()
                elif self.kind() == "str":
                    self.take()
                else:
                    raise _ScaffoldRefused(f"{self.peek()!r} in an export list")
            if not self.optional("from"):
                self.end_statement()
                return
        if self.kind() != "str":
            raise _ScaffoldRefused("a re-export from something other than a string")
        self.take()
        self.end_statement()

    def declaration(self, allow_anonymous: bool = False) -> None:
        word = self.peek()
        if word == "declare":
            self.take()
            self.ambient()
        elif word == "interface":
            self.take()
            self.take_name()
            self.skip_type(("{",))
            if self.peek() != "{":
                raise _ScaffoldRefused("an interface with no body")
            self.skip_balanced()
        elif word == "type" and self.kind(1) == "id":
            self.take()
            self.take_name()
            if self.peek() == "<":
                self.skip_balanced()
            self.take("=")
            self.skip_type((";",))
            self.end_statement()
        elif word in ("async", "function"):
            self.function(allow_anonymous=allow_anonymous)
        elif word in ("abstract", "class"):
            self.optional("abstract")
            self.class_decl(allow_anonymous=allow_anonymous)
        elif word in ("const", "let", "var"):
            self.variable()
        elif word == "enum" or (word == "const" and self.peek(1) == "enum"):
            raise _ScaffoldRefused("an enum (node cannot run enums)")
        elif word in ("namespace", "module"):
            raise _ScaffoldRefused("a namespace (node cannot run namespaces)")
        elif word == "@":
            raise _ScaffoldRefused("a decorator")
        else:
            raise _ScaffoldRefused(f"a statement starting {word!r}")

    def ambient(self) -> None:
        """A `declare` declaration: erased by node, so skipped up to where it ends."""
        if self.peek() in ("const", "let", "var", "type"):
            self.take()
            self.skip_type((";",))
            self.end_statement()
            return
        if self.peek() in ("function", "async"):
            self.optional("async")
            self.take("function")
            self.skip_type((";",), body_may_follow=True)
            if self.peek() == "{":
                raise _ScaffoldRefused("a declared function with a body")
            self.end_statement()
            return
        if self.peek() in ("abstract", "class", "module", "namespace", "global", "enum",
                           "interface"):
            self.skip_type(("{",))
            if self.peek() != "{":
                raise _ScaffoldRefused("an ambient declaration with no body")
            self.skip_balanced()
            return
        raise _ScaffoldRefused(f"declare {self.peek()!r}")

    def params(self) -> None:
        """A parameter list: names and types only, no defaults, no parameter properties."""
        start = self.i
        self.skip_balanced()
        inner = self.toks[start + 1:self.i - 1]
        for pos, (kind, value, _) in enumerate(inner):
            if value == "=":
                raise _ScaffoldRefused("a parameter default (code that runs)")
            if value == "@":
                raise _ScaffoldRefused("a parameter decorator")
            before = inner[pos - 1][1] if pos else ","
            if kind == "id" and value in _PARAM_MODIFIERS and before == "," \
                    and pos + 1 < len(inner) and inner[pos + 1][0] == "id":
                raise _ScaffoldRefused("a parameter property (node cannot run it)")

    def signature_rest(self) -> bool:
        """After a name: generics, parameters, return type. True when a body follows."""
        if self.peek() == "<":
            self.skip_balanced()
        if self.peek() != "(":
            raise _ScaffoldRefused(f"{self.peek()!r} where parameters were expected")
        self.params()
        if self.optional(":"):
            self.skip_type((";", "}"), body_may_follow=True)
        return self.peek() == "{" and not self.statement_ends_here()

    def throw_body(self) -> None:
        """Exactly `{ throw new Error(<string>?); }`."""
        self.take("{")
        if self.peek() != "throw":
            raise _ScaffoldRefused("a body that is not exactly one throw new Error(...)")
        self.take("throw")
        self.take("new")
        self.take("Error")
        self.take("(")
        if self.kind() in ("str", "tpl"):
            self.take()
        self.take(")")
        self.optional(";")
        if self.peek() != "}":
            raise _ScaffoldRefused("a body with more than one throw new Error(...)")
        self.take("}")

    def function(self, allow_anonymous: bool = False) -> None:
        self.optional("async")
        self.take("function")
        self.optional("*")
        if self.kind() == "id" and self.peek() not in ("<",):
            self.take()
        elif not allow_anonymous:
            raise _ScaffoldRefused("a function with no name")
        if self.signature_rest():
            self.throw_body()
        else:
            self.end_statement()  # an overload signature

    def class_decl(self, allow_anonymous: bool = False) -> None:
        self.take("class")
        if self.kind() == "id" and self.peek() not in ("extends", "implements"):
            self.take()
        elif not allow_anonymous:
            raise _ScaffoldRefused("a class with no name")
        if self.peek() == "<":
            self.skip_balanced()
        if self.optional("extends"):
            # A plain, possibly dotted name: anything else is an expression that runs.
            self.take_name()
            while self.optional("."):
                self.take_name()
            if self.peek() == "<":
                self.skip_balanced()
        if self.optional("implements"):
            self.skip_type(("{",), body_may_follow=True)
        self.take("{")
        self.members(closing="}")
        self.take("}")

    def members(self, closing: str | None) -> None:
        while not self.at_end() and self.peek() != closing:
            if self.optional(";"):
                continue
            self.member()

    def member(self) -> None:
        while (self.peek() in _MEMBER_MODIFIERS and self.peek(1) not in
               ("(", "<", ":", "=", ";", "?", "!", "{", "}")
               and not self.newline_before(1)):
            self.take()
        if self.peek() == "static" and self.peek(1) == "{":
            raise _ScaffoldRefused("a static block (code that runs)")
        if self.peek() in ("get", "set") and self.kind(1) in ("id", "str", "num") \
                and not self.newline_before(1):
            self.take()
        if self.peek() == "@":
            raise _ScaffoldRefused("a decorator")
        if self.peek() == "[":
            # An index signature `[key: K]: V`; a computed key is code that runs.
            if not (self.kind(1) == "id" and self.peek(2) == ":"):
                raise _ScaffoldRefused("a computed member name")
            self.skip_balanced()
            self.take(":")
            self.skip_type((";", "}"))
            self.end_statement()
            return
        self.optional("*")
        if self.kind() not in ("id", "str", "num"):
            raise _ScaffoldRefused(f"{self.peek() or 'the end'!r} where a member was expected")
        self.take()
        self.optional("?") or self.optional("!")
        if self.peek() in ("(", "<"):
            if self.signature_rest():
                self.throw_body()
            else:
                self.end_statement()  # abstract, declared or an overload
            return
        if self.optional(":"):
            self.skip_type((";", "=", "}"))
        if self.optional("="):
            self.initializer(in_class=True)
        self.end_statement()

    def initializer(self, in_class: bool) -> None:
        """A stub function, or (in a class) one literal."""
        if self.peek() in ("async", "function") or self.peek() == "(" or (
                self.kind() == "id" and self.peek(1) == "=>") or self.peek() == "<":
            self.arrow_or_function()
            return
        if in_class:
            self.optional("-")
            if self.kind() in ("str", "tpl", "num") or self.peek() in _LITERAL_WORDS:
                self.take()
                return
        raise _ScaffoldRefused("a value that is not a throwing stub")

    def arrow_or_function(self) -> None:
        if self.peek() == "function" or (self.peek() == "async" and self.peek(1) == "function"):
            self.function(allow_anonymous=True)
            return
        if self.peek() == "async" and not self.newline_before(1):
            self.take()
        if self.kind() == "id" and self.peek(1) == "=>":
            self.take()
        else:
            if self.peek() == "<":
                self.skip_balanced()
            if self.peek() != "(":
                raise _ScaffoldRefused("a value that is not a throwing stub")
            self.params()
            if self.optional(":"):
                self.skip_type(("=>",))
        self.take("=>")
        if self.peek() != "{":
            raise _ScaffoldRefused("an arrow whose body is an expression")
        self.throw_body()

    def variable(self) -> None:
        keyword = self.take()  # const / let / var
        self.take_name()
        if self.optional(":"):
            self.skip_type(("=", ";", ","))
        if self.peek() == ",":
            raise _ScaffoldRefused("several names in one declaration")
        if keyword != "const" and self.peek() != "=":
            self.end_statement()  # a typed name with no value, like Python's `x: int`
            return
        self.take("=")
        self.initializer(in_class=False)
        if self.peek() == ",":
            raise _ScaffoldRefused("several names in one declaration")
        self.end_statement()


def typescript_scaffold_problem(body: str, in_class: bool = False) -> str:
    """Why `body` is not a TypeScript scaffold, or "" when it is one.

    It may hold only declarations and failing stubs: imports, re-exports, types,
    interfaces, `declare`s, classes whose members are typed fields (a literal
    default at most) and stub methods, and functions, methods, accessors and
    arrows whose whole body is one `throw new Error(...)`. With `in_class`, the
    body is a run of class members (an edit adding to a class it is inside).
    """
    if not str(body or "").strip():
        return "it is empty"
    try:
        tokens = _scaffold_tokens(str(body))
        if not tokens:
            return "it holds only comments"
        parser = _ScaffoldParser(tokens)
        if in_class:
            parser.members(closing=None)
        else:
            parser.statements()
    except _ScaffoldRefused as exc:
        return str(exc)
    return ""


def typescript_scaffold_edit_problem(fmt: str, search: str, replace: str,
                                     content: str | None) -> str:
    """Why a scaffold edit is not declarations and stubs only, or "".

    A `whole_file` or `append_stub` is judged on its content. A `search_replace`
    that only inserts lines is judged on what it adds, as class members when the
    lines before it open a class body, and only when it lands between complete
    statements; one that changes or drops existing lines is judged whole, so it
    cannot rewrite logic and call the result a scaffold.
    """
    if fmt != "search_replace":
        return typescript_scaffold_problem(content or "")
    old = (search or "").splitlines(keepends=True)
    new = (replace or "").splitlines(keepends=True)
    head = 0
    while head < len(old) and head < len(new) and old[head] == new[head]:
        head += 1
    tail = 0
    while (tail < len(old) - head and tail < len(new) - head
           and old[len(old) - 1 - tail] == new[len(new) - 1 - tail]):
        tail += 1
    removed = old[head:len(old) - tail]
    if not old or any(line.strip() for line in removed):
        return typescript_scaffold_problem(replace or "")
    added = "".join(new[head:len(new) - tail])
    scanner = _TSScanner()
    masked = [scanner.mask_line(line.rstrip("\n")) for line in old]
    before = "\n".join(masked[:head]).rstrip()
    after = "\n".join(masked[len(old) - tail:]).lstrip()
    if before and before[-1] not in "{};":
        return "it is inserted in the middle of a statement"
    if after and not (after[0].isalnum() or after[0] in "_$#}@;"):
        return "it is inserted in the middle of a statement"
    return typescript_scaffold_problem(added, in_class=_opens_class_body(before))


def _opens_class_body(masked: str) -> bool:
    """True when the innermost `{` still open at the end of `masked` is a class's."""
    stack: list[bool] = []
    header = ""
    for ch in masked:
        if ch == "{":
            stack.append(bool(re.search(r"\bclass\b", header)))
            header = ""
        elif ch == "}":
            if not stack:
                return False
            stack.pop()
            header = ""
        elif ch == ";":
            header = ""
        else:
            header += ch
    return bool(stack) and stack[-1]
