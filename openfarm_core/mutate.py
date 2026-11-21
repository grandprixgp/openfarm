"""openfarm's own mutation runner for Python and TypeScript (v0.8.5).

Rust has cargo-mutants; Python and TypeScript had no mutation tool, so
`[gates] mutation` could only be "off" for them. This runner makes small
behaviour changes ("mutants") to the production files a card changed, runs the
card's named tests against each one, and counts a mutant `caught` when the tests
fail and `missed` when they still pass. It is run as a script (standard library
only), so it needs nothing installed in the project:

    python mutate.py --lang python --out DIR --test-argv JSON
                     [--lines JSON] [--max N] [--timeout S] FILE...

Python mutants come from the syntax tree (comparison and arithmetic operators,
`and`/`or`, `not`, `True`/`False`, number constants, returned values). TypeScript
mutants are token swaps outside strings and comments; `<` and `>` are swapped only
with spaces round them, so generics (`Array<string>`) are left alone. With
`--lines` (`{"path": [line, ...]}`, the lines the card changed, which the flow
reads from git), only those lines are mutated; a file with no changed line (a
test-backfill card's unit) is mutated whole.

Before any mutant the named tests run once on the code as it is: if they fail
there, nothing is judged (exit 4, as cargo-mutants does). A mutant whose test run
takes longer than `--timeout` is `timeout`; one that cannot be written (a syntax
error) is `unviable`. Every file is put back exactly as it was, whatever happens.

The result is `outcomes.json` in `--out` and a closing line in cargo-mutants' form,
`N mutants tested in 0s: A missed, B caught, C unviable, D timeouts`, so the packs
read it the way the Rust pack reads cargo-mutants. Exit codes: 0 all caught, 2
some missed, 3 some timed out (and none missed), 4 the baseline failed, 1 usage.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

EXIT_ALL_CAUGHT, EXIT_USAGE, EXIT_MISSED, EXIT_TIMEOUT, EXIT_BASELINE = 0, 1, 2, 3, 4
DEFAULT_MAX_MUTANTS = 40

# -- Python ---------------------------------------------------------------------

_CMP_SWAPS = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
              ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
              ast.Is: ast.IsNot, ast.IsNot: ast.Is}
_BIN_SWAPS = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv,
              ast.FloorDiv: ast.Mult, ast.Div: ast.Mult, ast.Mod: ast.FloorDiv}


def _annotation_ids(tree: ast.AST) -> set[int]:
    """Nodes inside type annotations: changing them changes no behaviour."""
    out: set[int] = set()
    for node in ast.walk(tree):
        parts = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(node.returns)
            args = node.args
            for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if a is not None:
                    parts.append(a.annotation)
        elif isinstance(node, ast.AnnAssign):
            parts.append(node.annotation)
        for part in parts:
            if part is not None:
                out.update(id(n) for n in ast.walk(part))
    return out


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def python_mutants(source: str, lines: set[int] | None) -> list[dict]:
    """Every mutant of one Python file: `{"line", "what", "text"}`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    skip = _annotation_ids(tree) | _docstring_ids(tree)
    sites: list[tuple[int, str, object]] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        line = getattr(node, "lineno", 0)
        if lines is not None and line not in lines:
            continue
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP_SWAPS:
                    sites.append((line, f"{type(op).__name__} -> {_CMP_SWAPS[type(op)].__name__}",
                                  ("cmp", node, i)))
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN_SWAPS:
            sites.append((line, f"{type(node.op).__name__} -> {_BIN_SWAPS[type(node.op)].__name__}",
                          ("bin", node)))
        elif isinstance(node, ast.AugAssign) and type(node.op) in _BIN_SWAPS:
            sites.append((line, f"{type(node.op).__name__}= -> {_BIN_SWAPS[type(node.op)].__name__}=",
                          ("bin", node)))
        elif isinstance(node, ast.BoolOp):
            other = "Or" if isinstance(node.op, ast.And) else "And"
            sites.append((line, f"{type(node.op).__name__} -> {other}", ("bool", node)))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append((line, "drop not", ("not", node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            sites.append((line, f"{node.value} -> {not node.value}", ("const", node)))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, int)
              and not isinstance(node.value, bool)):
            sites.append((line, f"{node.value} -> {node.value + 1}", ("const", node)))
        elif (isinstance(node, ast.Return) and node.value is not None
              and not (isinstance(node.value, ast.Constant) and node.value.value is None)):
            sites.append((line, "return None", ("ret", node)))

    original = ast.unparse(tree)
    out: list[dict] = []
    for line, what, site in sites:
        mutated = copy.deepcopy(tree)
        # The same node in the copy, found by its place in the walk.
        index = [id(n) for n in ast.walk(tree)].index(id(site[1]))
        target = list(ast.walk(mutated))[index]
        kind = site[0]
        if kind == "cmp":
            target.ops[site[2]] = _CMP_SWAPS[type(target.ops[site[2]])]()
        elif kind == "bin":
            target.op = _BIN_SWAPS[type(target.op)]()
        elif kind == "bool":
            target.op = ast.Or() if isinstance(target.op, ast.And) else ast.And()
        elif kind == "not":
            _replace(mutated, target, target.operand)
        elif kind == "const":
            target.value = (not target.value) if isinstance(target.value, bool) else target.value + 1
        elif kind == "ret":
            target.value = ast.Constant(value=None)
        text = ast.unparse(ast.fix_missing_locations(mutated))
        if text != original:
            out.append({"line": line, "what": what, "text": text + "\n"})
    return out


def _replace(tree: ast.AST, old: ast.AST, new: ast.AST) -> None:
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if value is old:
                setattr(parent, field, new)
                return
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if item is old:
                        value[i] = new
                        return


# -- TypeScript -----------------------------------------------------------------

_TS_SWAPS = [("===", "!=="), ("!==", "==="), ("==", "!="), ("!=", "=="),
             (" <= ", " < "), (" >= ", " > "), (" < ", " <= "), (" > ", " >= "),
             ("&&", "||"), ("||", "&&"), (" + ", " - "), (" - ", " + "),
             (" * ", " / "), ("true", "false"), ("false", "true")]
_WORD = re.compile(r"[A-Za-z0-9_$]")


_REGEX_PRE_WORDS = frozenset({
    "return", "case", "throw", "typeof", "void", "delete",
    "in", "instanceof", "new", "yield", "await", "of", "else", "do",
})
_REGEX_PRE_CHARS = set("(,=[!&|?:;{}~^%+-*<>")


def _can_start_regex(text: str, slash_idx: int) -> bool:
    """True when `/` at `slash_idx` can begin a JS/TS regex literal rather than division."""
    j = slash_idx - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    ch = text[j]
    if ch in _REGEX_PRE_CHARS:
        if ch in "+-" and j > 0 and text[j - 1] == ch:
            return False  # postfix ++ or -- followed by division
        return True
    if ch.isalnum() or ch in "_$":
        k = j
        while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
            k -= 1
        return text[k + 1:j + 1] in _REGEX_PRE_WORDS
    return False


def _scan_regex_literal(text: str, i: int) -> int:
    """Return index past `/pattern/flags` starting at `i`, or -1 if not a regex literal."""
    n = len(text)
    j = i + 1
    in_class = False
    while j < n:
        c = text[j]
        if c in "\r\n":
            return -1
        if c == "\\":
            if j + 1 >= n or text[j + 1] in "\r\n":
                return -1
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]" and in_class:
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            return j
        j += 1
    return -1


def _code_spans(text: str, *, raw_backtick: bool = False,
                regex_literals: bool | None = None) -> list[tuple[int, int]]:
    """The parts of a TS/Go file outside strings, templates/raw strings, regexes, and comments."""
    if regex_literals is None:
        regex_literals = not raw_backtick
    spans, i, start, n = [], 0, 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two in ("//", "/*") or ch in "'\"`":
            spans.append((start, i))
            if two == "//":
                end = text.find("\n", i)
                i = n if end < 0 else end
            elif two == "/*":
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
            else:
                i += 1
                while i < n and text[i] != ch:
                    if ch == "`" and raw_backtick:
                        i += 1
                    else:
                        i += 2 if text[i] == "\\" else 1
                i += 1
            start = i
            continue
        if regex_literals and ch == "/" and _can_start_regex(text, i):
            end = _scan_regex_literal(text, i)
            if end > 0:
                spans.append((start, i))
                i = end
                start = i
                continue
        i += 1
    spans.append((start, n))
    return [(a, b) for a, b in spans if b > a]


def _token_mutants(source: str, lines: set[int] | None, swaps: list[tuple[str, str]],
                   *, raw_backtick: bool = False, check_triple_eq: bool = False) -> list[dict]:
    out: list[dict] = []
    taken: set[int] = set()
    for a, b in _code_spans(source, raw_backtick=raw_backtick):
        chunk = source[a:b]
        for old, new in swaps:
            at = chunk.find(old)
            while at >= 0:
                pos = a + at
                end = pos + len(old)
                word = old.isalpha()
                ok = pos not in taken
                if word:
                    ok = ok and not (pos > 0 and _WORD.match(source[pos - 1])) \
                        and not (end < len(source) and _WORD.match(source[end]))
                if check_triple_eq and old in ("==", "!=") and (
                        source[end:end + 1] == "=" or source[pos - 1:pos] in "=!"):
                    ok = False  # part of === or !==
                if old in (" < ", " > ") and (source[end:end + 1] == "=" or source[pos + 1:pos + 3] in ("<=", ">=")):
                    ok = False
                line = source.count("\n", 0, pos) + 1
                if ok and (lines is None or line in lines):
                    taken.add(pos)
                    out.append({"line": line, "what": f"{old.strip()} -> {new.strip()}",
                                "text": source[:pos] + new + source[end:]})
                at = chunk.find(old, at + 1)
    out.sort(key=lambda m: m["line"])
    return out


def typescript_mutants(source: str, lines: set[int] | None) -> list[dict]:
    return _token_mutants(source, lines, _TS_SWAPS, raw_backtick=False, check_triple_eq=True)


_GO_SWAPS = [("==", "!="), ("!=", "=="),
             (" <= ", " < "), (" >= ", " > "), (" < ", " <= "), (" > ", " >= "),
             ("&&", "||"), ("||", "&&"), (" + ", " - "), (" - ", " + "),
             (" * ", " / "), ("true", "false"), ("false", "true")]


def go_mutants(source: str, lines: set[int] | None) -> list[dict]:
    """Token-swap mutants for Go outside strings, raw strings, and comments."""
    return _token_mutants(source, lines, _GO_SWAPS, raw_backtick=True, check_triple_eq=False)


# -- running --------------------------------------------------------------------

def changed_lines_from_diff(diff: str) -> dict[str, set[int]]:
    """`{path: lines}` added or changed, read from a unified diff (any context)."""
    out: dict[str, set[int]] = {}
    path, line = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            path = None if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
            if path is not None:
                out.setdefault(path, set())
            continue
        if raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        hunk = re.match(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            line = int(hunk.group(1))
            continue
        if path is None or not raw or raw.startswith("\\"):
            continue
        if raw[0] == "+":
            out[path].add(line)
            line += 1
        elif raw[0] == " ":
            line += 1
    return out


def spread(items: list, limit: int) -> list:
    """At most `limit` of `items`, evenly spread, always the same ones."""
    if limit <= 0 or len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def _drop_bytecode(path: Path) -> None:
    """Remove a Python file's cached bytecode. Python checks a `.pyc` by the
    source's size and its mtime in whole seconds, so two mutants of one size
    written in one second would otherwise run the earlier one's code."""
    import importlib.util

    try:
        cached = Path(importlib.util.cache_from_source(str(path)))
    except (NotImplementedError, ValueError):
        return
    folder = cached.parent
    stem = path.stem + "."
    try:
        for entry in folder.iterdir():
            if entry.name.startswith(stem) and entry.suffix == ".pyc":
                entry.unlink()
    except OSError:
        pass


def run_tests(argv: list[str], cwd: Path, timeout: float) -> str:
    """`pass`, `fail` or `timeout` for one run of the named tests."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except OSError:
        return "fail"
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
        return "timeout"
    return "pass" if code == 0 else "fail"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfarm-mutate")
    parser.add_argument("--lang", choices=("python", "typescript", "go"), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--test-argv", required=True, help="JSON list: the named tests' command")
    parser.add_argument("--lines", default="", help='JSON {"path": [line, ...]}')
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_MUTANTS)
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="seconds per mutant (default: 3x the baseline run + 10)")
    parser.add_argument("files", nargs="+")
    try:
        args = parser.parse_args(argv)
        test_argv = json.loads(args.test_argv)
        if not isinstance(test_argv, list) or not test_argv:
            raise ValueError("--test-argv must be a non-empty JSON list")
        wanted = json.loads(args.lines) if args.lines else None
        if wanted is not None and not isinstance(wanted, dict):
            raise ValueError("--lines must be a JSON object")
    except (SystemExit, ValueError) as exc:
        print(f"openfarm-mutate: usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    root = Path.cwd()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    baseline = run_tests(test_argv, root, timeout=max(args.timeout, 600.0))
    base_secs = time.monotonic() - started
    if baseline != "pass":
        print(f"openfarm-mutate: the named tests do not pass on the code as it is "
              f"({baseline}); no mutant is judged", file=sys.stderr)
        return EXIT_BASELINE
    per_mutant = args.timeout or (base_secs * 3 + 10)

    make = {"python": python_mutants, "typescript": typescript_mutants, "go": go_mutants}[args.lang]
    plan: list[tuple[str, dict]] = []
    originals: dict[str, bytes] = {}
    mtimes: dict[str, tuple[int, int]] = {}
    for rel in args.files:
        path = root / rel
        try:
            data = path.read_bytes()
            info = path.stat()
        except OSError:
            continue
        originals[rel] = data
        mtimes[rel] = (info.st_atime_ns, info.st_mtime_ns)
        if wanted is not None:
            if rel in wanted and not wanted[rel]:
                # Delete-only diff: 0 added/changed lines in this file -> 0 mutants.
                continue
            lines = set(wanted.get(rel) or ()) or None
        else:
            lines = None
        mutants = make(data.decode("utf-8", errors="replace"), lines)
        plan.extend((rel, m) for m in mutants)
    total_found = len(plan)
    plan = spread(plan, args.max)

    def put_back(*_):
        for rel, data in originals.items():
            try:
                (root / rel).write_bytes(data)
                os.utime(root / rel, ns=mtimes[rel])
                if args.lang == "python":
                    _drop_bytecode(root / rel)
            except OSError:
                pass

    previous = signal.signal(signal.SIGTERM, lambda *_: (put_back(), sys.exit(EXIT_USAGE)))
    results: list[dict] = []
    try:
        for rel, mutant in plan:
            path = root / rel
            if args.lang == "python":
                try:
                    compile(mutant["text"], rel, "exec")
                except SyntaxError:
                    results.append({"file": rel, "line": mutant["line"], "what": mutant["what"],
                                    "outcome": "unviable"})
                    continue
            path.write_text(mutant["text"], encoding="utf-8")
            # Each mutant its own whole-second mtime, and no cached bytecode.
            stamp = mtimes[rel][1] + (len(results) + 1) * 1_000_000_000
            os.utime(path, ns=(stamp, stamp))
            if args.lang == "python":
                _drop_bytecode(path)
            outcome = run_tests(test_argv, root, per_mutant)
            path.write_bytes(originals[rel])
            os.utime(path, ns=mtimes[rel])
            if args.lang == "python":
                _drop_bytecode(path)
            results.append({"file": rel, "line": mutant["line"], "what": mutant["what"],
                            "outcome": {"fail": "caught", "pass": "missed",
                                        "timeout": "timeout"}[outcome]})
    finally:
        put_back()
        signal.signal(signal.SIGTERM, previous)

    counts = {k: sum(1 for r in results if r["outcome"] == k)
              for k in ("caught", "missed", "timeout", "unviable")}
    payload = {"tool": "openfarm-mutate", "lang": args.lang, "total": len(results),
               "found": total_found, "capped": total_found > len(results),
               "scoped": wanted is not None, **counts,
               "outcomes": results}
    (out_dir / "outcomes.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for r in results:
        if r["outcome"] == "missed":
            print(f"MISSED {r['file']}:{r['line']}: {r['what']}")
    print(f"{len(results)} mutants tested in {round(time.monotonic() - started)}s: "
          f"{counts['missed']} missed, {counts['caught']} caught, "
          f"{counts['unviable']} unviable, {counts['timeout']} timeouts")
    if counts["missed"]:
        return EXIT_MISSED
    if counts["timeout"]:
        return EXIT_TIMEOUT
    return EXIT_ALL_CAUGHT


def read_outcomes(out_dir: Path | None, stdout: str, stderr: str, exit_code: int | None) -> dict:
    """The gate's counts from one run (used by the Python and TypeScript packs)."""
    if exit_code is not None and exit_code not in (EXIT_ALL_CAUGHT, EXIT_MISSED, EXIT_TIMEOUT):
        meaning = {EXIT_USAGE: "a usage error", EXIT_BASELINE: "the named tests fail on the code "
                   "as it is, so no mutant could be judged"}.get(exit_code, "an unknown exit code")
        tail = (stderr or stdout or "").strip()[-300:]
        from .errors import Invalid
        raise Invalid("mutation-not-run", f"openfarm-mutate exited {exit_code}, {meaning}"
                      + (f": {tail}" if tail else ""))
    payload = None
    if out_dir is not None:
        try:
            payload = json.loads((Path(out_dir) / "outcomes.json").read_text())
        except (OSError, ValueError):
            payload = None
    if payload is None:
        from .errors import Invalid
        raise Invalid("unreadable-mutation", "openfarm-mutate left no outcomes.json: "
                      + (stdout or stderr or "").strip()[-200:])
    caught, missed = int(payload.get("caught", 0)), int(payload.get("missed", 0))
    return {"killed": caught, "survived": missed, "timeout": int(payload.get("timeout", 0)),
            "unviable": int(payload.get("unviable", 0)), "total": int(payload.get("total", 0)),
            "judged": caught + missed, "capped": bool(payload.get("capped", False)),
            "scoped": bool(payload.get("scoped", False)),
            "source": "openfarm-mutate"}


if __name__ == "__main__":
    raise SystemExit(main())
