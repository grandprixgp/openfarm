"""The Go language pack.

Stdlib-only pack following the same contracts as Rust, Python, and TypeScript:
- `go test -json -count=1` emits NDJSON events; never judge by exit code alone.
- `go vet` for lint, `gofmt` for format / format-check, `go build` for build /
  typecheck, and openfarm's own diff-scoped mutation runner (`mutate.py`).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from ..errors import Invalid, Refused
from .base import OwnMutationMixin, Pack

PURPOSES = ("build", "test", "list", "lint", "ws-lint", "format", "fmt-check",
            "typecheck", "mutation", "extra")

_GOFMT_CHECK_SCRIPT = (
    "import subprocess, sys\n"
    "files = sys.argv[1:] or ['.']\n"
    "res = subprocess.run(['gofmt', '-l', *files], capture_output=True, text=True)\n"
    "if res.stdout.strip():\n"
    "    sys.stdout.write(res.stdout)\n"
    "    sys.exit(1)\n"
    "if res.stderr:\n"
    "    sys.stderr.write(res.stderr)\n"
    "sys.exit(res.returncode)\n"
)

BANNED_IN_COMMENTS = (
    (r"//\s*nolint\b", "suppression-nolint"),
)

BANNED_ANYWHERE = (
    (r'"os/exec"', "process-call"),
    (r'"unsafe"', "unsafe-code"),
    (r'"net(?:/http)?"', "network-call"),
    (r"\bt\s*\.\s*(?:Skip|SkipNow|Skipf)\s*\(", "test-skipped"),
)

BANNED_PRODUCTION = (
    (r"\bpanic\s*\(", "leftover-stub"),
)

_TEST_KIND = "test"
_PRODUCTION_KIND = "production"

_LIST_OK_RE = re.compile(r"^ok\s+(\S+)\b")
_LIST_NO_TESTS_RE = re.compile(r"^\?\s+(\S+)\b")
_LIST_TEST_RE = re.compile(r"^(?:Test|Benchmark|Example|Fuzz)[A-Za-z0-9_]*$")
_FAIL_LINE_RE = re.compile(r"^---\s+FAIL:\s+(\S+)")
_GO_ERR_RE = re.compile(r"^([^\s:]+\.go):(\d+)(?::\d+)?:\s*(.+)$")


def _strip_line_comment(line: str) -> str:
    """`line` without `// ...` or single-line `/* ... */` comments outside strings."""
    out: list[str] = []
    quote = ""
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            out.append(ch)
            if quote != "`" and ch == "\\" and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if line.startswith("/*", i):
            end = line.find("*/", i + 2)
            if end < 0:
                break
            out.append(" ")
            i = end + 2
            continue
        if line.startswith("//", i):
            break
        if ch in ('"', "'", "`"):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


class GoPack(OwnMutationMixin, Pack):
    name = "go"
    extension = ".go"
    test_dir = "."
    source_dir = "."
    mutation_language = "go"

    def __init__(self) -> None:
        self.worktree: Path | None = None
        self.runner = "go test"
        self.has_go = shutil.which("go") is not None
        self.has_gofmt = shutil.which("gofmt") is not None
        self.notes: list[str] = []
        self._update_notes()

    def _update_notes(self) -> None:
        self.notes = []
        if not self.has_go:
            self.notes.append("go is not installed on PATH")
        if not self.has_gofmt:
            self.notes.append("gofmt is not installed on PATH, so formatting is not checked")

    def configure(self, worktree: Path) -> None:
        self.worktree = Path(worktree)
        self.has_go = shutil.which("go") is not None
        self.has_gofmt = shutil.which("gofmt") is not None
        self._update_notes()

    def tooling_summary(self) -> dict:
        return {
            "pack": self.name,
            "runner": "go test",
            "lint": "go vet",
            "format": "gofmt" if self.has_gofmt else "off",
            "notes": list(self.notes),
        }

    def _pkg_pattern(self, unit: str) -> str:
        cleaned = (unit or "").strip().strip("/")
        if cleaned and cleaned != "." and self.worktree is not None:
            if (self.worktree / cleaned).is_dir():
                return f"./{cleaned}/..."
        return "./..."

    # -- commands -------------------------------------------------------------

    def commands(self, purpose: str, unit: str = "", tests: list[str] | None = None,
                 files: list[str] | None = None,
                 extra_flags: list[str] | None = None) -> dict:
        self.require(purpose, PURPOSES)
        extra = list(extra_flags or [])
        pkg = self._pkg_pattern(unit)

        if purpose in ("build", "typecheck"):
            return {"argv": ["go", "build", pkg] + extra}

        if purpose == "test":
            argv = ["go", "test", "-json", "-count=1"]
            if tests:
                if len(tests) == 1 and "/" in str(tests[0]):
                    pattern = "/".join(f"^{re.escape(seg)}$" for seg in str(tests[0]).split("/"))
                else:
                    pattern = "^(?:" + "|".join(re.escape(str(t)) for t in tests) + ")$"
                argv += ["-run", pattern]
            argv.append(pkg)
            return {"argv": argv + extra}

        if purpose == "list":
            return {"argv": ["go", "test", "-list", ".", pkg] + extra}

        if purpose in ("lint", "ws-lint"):
            target = "./..." if purpose == "ws-lint" else pkg
            return {"argv": ["go", "vet", target] + extra}

        if purpose == "format":
            if not self.has_gofmt:
                return {"argv": [sys.executable, "-c", "pass"]}
            targets = [str(f) for f in (files or ["."])]
            return {"argv": ["gofmt", "-w", *targets] + extra}

        if purpose == "fmt-check":
            if not self.has_gofmt:
                return {"argv": [sys.executable, "-c", "pass"]}
            targets = [str(f) for f in (files or ["."])]
            return {"argv": [sys.executable, "-c", _GOFMT_CHECK_SCRIPT, *targets] + extra}

        if purpose == "mutation":
            return {"argv": self._own_mutation_argv(files, tests) + extra}

        return {"argv": list(extra)}

    # -- output ---------------------------------------------------------------

    def parse_list(self, stdout: str) -> dict:
        tests: list[dict] = []
        errors: list[str] = []
        pending: list[str] = []
        in_error_pkg = ""
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("# "):
                in_error_pkg = line[2:].strip()
                continue
            ok = _LIST_OK_RE.match(line)
            if ok:
                pkg = ok.group(1)
                for name in pending:
                    tests.append({"name": name, "binary": pkg})
                pending = []
                in_error_pkg = ""
                continue
            if _LIST_NO_TESTS_RE.match(line):
                pending = []
                in_error_pkg = ""
                continue
            if line.startswith("FAIL") and "[build failed]" in line:
                errors.append(line)
                in_error_pkg = ""
                continue
            if in_error_pkg:
                errors.append(f"{in_error_pkg}: {line}")
                continue
            if _LIST_TEST_RE.match(line):
                pending.append(line)
        for name in pending:
            tests.append({"name": name, "binary": "."})
        return {"tests": tests, "errors": errors} if errors else {"tests": tests}

    def parse_tests(self, stdout: str, stderr: str = "") -> dict:
        results: list[dict] = []
        footers: list[dict] = []
        compile_errors: list[dict] = []
        pkg_counts: dict[str, dict[str, int]] = {}
        seen_compile: set[str] = set()

        def add_compile(pkg: str) -> None:
            key = pkg or "go"
            if key not in seen_compile:
                seen_compile.add(key)
                compile_errors.append({"pkg": key, "target": key, "binary": key})

        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            action = str(ev.get("Action") or "")
            pkg = str(ev.get("Package") or ev.get("ImportPath") or ".")
            test_name = ev.get("Test")

            if action == "build-fail" or ev.get("FailedBuild"):
                add_compile(str(ev.get("FailedBuild") or pkg))
                continue

            if test_name:
                if action in ("pass", "fail", "skip"):
                    status = {"pass": "ok", "fail": "FAILED", "skip": "ignored"}[action]
                    results.append({"name": str(test_name), "binary": pkg, "status": status})
                    counts = pkg_counts.setdefault(pkg, {"passed": 0, "failed": 0, "ignored": 0})
                    if status == "ok":
                        counts["passed"] += 1
                    elif status == "FAILED":
                        counts["failed"] += 1
                    else:
                        counts["ignored"] += 1
            elif action in ("pass", "fail", "skip"):
                counts = pkg_counts.get(pkg, {"passed": 0, "failed": 0, "ignored": 0})
                footers.append({
                    "binary": pkg,
                    "status": "FAILED" if action == "fail" else "ok",
                    "passed": counts["passed"],
                    "failed": counts["failed"],
                    "ignored": counts["ignored"],
                    "filtered": 0,
                })

        for raw in (stderr or "").splitlines():
            line = raw.strip()
            if line.startswith("# "):
                add_compile(line[2:].strip())

        missing: list[str] = [] if footers else ["go test"]
        for err in compile_errors:
            if err["binary"] not in missing:
                missing.append(err["binary"])
        return {
            "results": results,
            "footers": footers,
            "missing_footers": missing,
            "compile_errors": compile_errors,
        }

    # -- files ----------------------------------------------------------------

    def classify_paths(self, paths: list[str]) -> dict:
        from ..globs import normalise

        out: dict[str, str] = {}
        for path in paths:
            text = normalise(str(path))
            base = text.rsplit("/", 1)[-1]
            if not base.endswith(".go"):
                out[path] = "other"
            elif base.endswith("_test.go"):
                out[path] = _TEST_KIND
            else:
                out[path] = _PRODUCTION_KIND
        return out

    def signatures(self, path: Path) -> dict:
        path = Path(path)
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            raise Invalid("unreadable", f"cannot read {path}: {exc}") from exc
        sigs: list[str] = []
        for raw in text.splitlines():
            stripped = raw.strip()
            if raw.startswith((" ", "\t")):
                continue
            if stripped.startswith("package "):
                sigs.append(stripped)
            elif stripped.startswith(("func ", "type ", "const ", "var ")):
                head = stripped.split("{", 1)[0].rstrip()
                if head:
                    sigs.append(head)
        return {"signatures": sigs}

    def append_stub(self, path: Path, test_name: str, body: str) -> dict:
        path = Path(path)
        stub = str(body or "")
        if "panic(" not in stub:
            raise Refused("non-panicking-stub",
                          "a Go stub must call panic(...) so it cannot pass a test by accident")
        text = path.read_text() if path.exists() else ""
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

        for entry in added_lines:
            path = str(entry.get("path", ""))
            line_no = entry.get("line", 0)
            text = str(entry.get("text", ""))
            code = _strip_line_comment(text)
            comment = text[len(code):]
            is_production = (path in production
                             or self.classify_paths([path]).get(path) == _PRODUCTION_KIND)

            def hit(label: str) -> None:
                banned.append({"pattern": label, "file": path,
                               "line": line_no, "text": text.strip()})

            for pattern, label in BANNED_IN_COMMENTS:
                if re.search(pattern, comment):
                    hit(label)
            if code.strip():
                for pattern, label in BANNED_ANYWHERE:
                    for _match in re.finditer(pattern, code):
                        hit(label)
                if is_production:
                    for pattern, label in BANNED_PRODUCTION:
                        for _match in re.finditer(pattern, code):
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

    # -- errors & mutation ----------------------------------------------------

    def error_signature(self, stderr: str) -> dict:
        raw = stderr or ""
        failed = sorted(set(_FAIL_LINE_RE.findall(raw)))
        items: list[str] = []
        codes: list[str] = []
        for line in raw.splitlines():
            m = _GO_ERR_RE.match(line.strip())
            if m:
                fname = Path(m.group(1)).name
                if fname not in items:
                    items.append(fname)
                msg = m.group(3).split(":", 1)[0].strip()
                if msg and msg not in codes:
                    codes.append(msg)
        cleaned = re.sub(r"/(?:tmp|var|home|Users|usr)/[^\s:)\"']+", "<path>", raw)
        cleaned = re.sub(r"\b\d+\.\d+s\b", "<time>", cleaned)
        cleaned = re.sub(r"0x[0-9a-fA-F]+", "<addr>", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        payload = ("|".join(sorted(codes)[:12]) + "::" + "|".join(failed[:12])
                   + "::" + "|".join(items[:12]))
        if not codes and not failed and not items:
            payload = cleaned.strip()[:200]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return {
            "signature": digest,
            "codes": sorted(codes)[:12],
            "items": items[:12],
            "failed_tests": failed[:12],
        }

    def mutation(self, files: list[str], tests: list[str] | None = None) -> dict:
        return self.commands("mutation", files=files, tests=tests)
