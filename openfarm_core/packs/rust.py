"""The Rust language pack.

Everything here is shaped by two facts:

- **Plain output only.** `--format json` is nightly-only for libtest and nextest
  gates its JSON behind an env var, so the pack parses the two stable shapes:
  `<name>: test` from `--list`, and `test <name> ... ok|FAILED|ignored`.
- **Never judge a run by its exit code.** A `known_failing` test still makes cargo
  exit 101. The pack parses per-test lines plus one footer per test binary, and a
  binary with no footer means the run died partway: infra, never a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import Invalid, Refused
from .base import Pack

PURPOSES = ("build", "test", "list", "lint", "ws-lint", "format", "fmt-check",
            "typecheck", "mutation", "extra")

#: `    Running unittests src/lib.rs (/path/target/debug/deps/foo-abc)`
#: `     Running tests/existing.rs (/path/target/debug/deps/existing-def)`
#: Cargo puts a kind word in front of unit tests and none in front of an
#: integration test file, so everything between `Running` and the parenthesised
#: target is the spec, and the kind word is stripped afterwards.
_RUNNING_RE = re.compile(
    r"^\s*Running\s+(?P<spec>.+?)\s+\((?P<target>[^)]*)\)\s*$")
#: A `Running` line whose target has no parentheses, on some cargo versions.
_RUNNING_BARE_RE = re.compile(r"^\s*Running\s+(?P<spec>\S+)\s*$")
#: `test test_name ... ok`
_TEST_LINE_RE = re.compile(
    r"^test\s+(?P<name>\S+)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored|bench)\b")
#: `test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s`
_FOOTER_RE = re.compile(
    r"^test result:\s+(?P<status>\w+)\.\s+(?P<passed>\d+)\s+passed;\s+"
    r"(?P<failed>\d+)\s+failed;\s+(?P<ignored>\d+)\s+ignored;"
    r"(?:\s+(?P<measured>\d+)\s+measured;)?\s+(?P<filtered>\d+)\s+filtered out")
#: `foo: test` or `foo: bench`
_LIST_RE = re.compile(r"^(?P<name>.+):\s+(?P<kind>test|bench)\s*$")
#: `error: could not compile `pkg` (test "integration") due to 1 previous error`
_COMPILE_FAIL_RE = re.compile(
    r"^error: could not compile `(?P<pkg>[^`]+)`"
    r"(?:\s+\((?P<target>[^)]+)\))?", re.MULTILINE)

#: Suppressions and leftovers that fail a card wherever they appear.
BANNED_ANYWHERE = (
    (r"#\s*!?\[\s*allow\s*\(", "suppression-allow"),
    (r"#\s*\[\s*ignore\b", "test-ignored"),
    (r"\btodo!\s*\(", "leftover-stub"),
    (r"\bunimplemented!\s*\(", "leftover-stub"),
    (r"\bdbg!\s*\(", "debug-macro"),
    (r"\bunsafe\b", "unsafe"),
    (r"\bstd::process::Command\b", "process-call"),
    (r"\bCommand::new\s*\(", "process-call"),
    (r"\bstd::net::", "network-call"),
    (r"\bTcpStream\b", "network-call"),
    (r"\bUdpSocket\b", "network-call"),
    (r"\breqwest::", "network-call"),
    (r"eslint-disable", "suppression-eslint"),
    (r"@ts-ignore", "suppression-ts"),
    (r"\.skip\s*\(", "test-skip"),
    (r"\.only\s*\(", "test-only"),
)

#: Only banned in production code, where a hidden failure is a hidden bug.
BANNED_PRODUCTION = (
    (r"\.unwrap\s*\(\s*\)", "production-unwrap"),
    (r"\.expect\s*\(", "production-expect"),
    (r"\bpanic!\s*\(", "production-panic"),
    (r"\bunreachable!\s*\(", "production-unreachable"),
    (r"\bunchecked_", "unchecked"),
)

_TEST_KIND = "test"
_PRODUCTION_KIND = "production"


class RustPack(Pack):
    name = "rust"
    extension = ".rs"
    test_dir = "tests"
    source_dir = "src"
    edition = "2021"

    def __init__(self, cargo_flags: list[str] | None = None) -> None:
        #: Always placed on cargo commands, always before the `--` separator.
        self.cargo_flags = list(cargo_flags if cargo_flags is not None else ["--locked"])

    # -- commands -------------------------------------------------------------

    def commands(self, purpose: str, unit: str = "", tests: list[str] | None = None,
                 files: list[str] | None = None,
                 extra_flags: list[str] | None = None) -> dict:
        self.require(purpose, PURPOSES)
        flags = list(self.cargo_flags)
        extra = list(extra_flags or [])

        if purpose == "build":
            argv = ["cargo", "build", *flags]
            if unit:
                argv[2:2] = ["-p", unit]
            return {"argv": argv + extra}

        if purpose == "test":
            argv = ["cargo", "test"]
            if unit:
                argv += ["-p", unit]
            # `--lib --bins --tests` keeps doc-tests out; their names have a
            # different shape and the dossier says they did not run.
            return {"argv": argv + flags + ["--no-fail-fast",
                                            "--lib", "--bins", "--tests"] + extra}

        if purpose == "list":
            argv = ["cargo", "test"]
            if unit:
                argv += ["-p", unit]
            return {"argv": argv + flags + ["--lib", "--bins", "--tests",
                                            "--", "--list"] + extra}

        if purpose == "lint":
            argv = ["cargo", "clippy"]
            if unit:
                argv += ["-p", unit]
            return {"argv": argv + ["--all-targets"] + flags + ["--",
                                                               "-D", "warnings"] + extra}

        if purpose == "ws-lint":
            return {"argv": ["cargo", "clippy", "--workspace", "--all-targets"]
                    + flags + ["--", "-D", "warnings"] + extra}

        if purpose in ("format", "fmt-check"):
            argv = ["rustfmt", "--edition", self.edition]
            if purpose == "fmt-check":
                argv.insert(1, "--check")
            return {"argv": argv + [str(f) for f in (files or [])]}

        if purpose == "typecheck":
            # Clippy already typechecks the targets it sees; a separate pass is
            # dropped where lint covers it.
            return {"argv": ["cargo", "check", "--all-targets"] + flags + extra}

        if purpose == "mutation":
            return {"argv": ["cargo", "mutants", "--no-times", "--json",
                             "--file", *[str(f) for f in (files or [])]] + extra}

        return {"argv": list(extra)}

    # -- listing and parsing --------------------------------------------------

    def parse_list(self, stdout: str) -> dict:
        tests = []
        for line in stdout.splitlines():
            match = _LIST_RE.match(line.strip())
            if not match or match.group("kind") != "test":
                continue
            tests.append({"name": match.group("name").strip(), "binary": ""})
        return {"tests": tests}

    def parse_tests(self, stdout: str, stderr: str = "") -> dict:
        results: list[dict] = []
        footers: list[dict] = []
        missing: list[str] = []
        compile_errors: list[dict] = []

        current_bin = ""
        seen_footer = True  # no binary open yet
        for line in stdout.splitlines():
            running = _RUNNING_RE.match(line) or _RUNNING_BARE_RE.match(line)
            if running:
                if not seen_footer and current_bin:
                    missing.append(current_bin)
                current_bin = _binary_name(running.group("spec"))
                seen_footer = False
                continue
            test_line = _TEST_LINE_RE.match(line)
            if test_line:
                results.append({"name": test_line.group("name"),
                                "binary": current_bin,
                                "status": test_line.group("status")})
                continue
            footer = _FOOTER_RE.match(line.strip())
            if footer:
                footers.append({
                    "binary": current_bin,
                    "passed": int(footer.group("passed")),
                    "failed": int(footer.group("failed")),
                    "ignored": int(footer.group("ignored")),
                    "filtered": int(footer.group("filtered")),
                })
                seen_footer = True
        if not seen_footer and current_bin:
            missing.append(current_bin)

        for match in _COMPILE_FAIL_RE.finditer(f"{stdout}\n{stderr}"):
            target = _target_name(match.group("target") or "")
            compile_errors.append({
                "pkg": match.group("pkg"),
                "target": target,
                "binary": target or match.group("pkg"),
            })
        for error in compile_errors:
            if error["binary"] not in missing:
                missing.append(error["binary"])

        return {"results": results, "footers": footers,
                "missing_footers": missing, "compile_errors": compile_errors}

    def parse_mutation(self, stdout: str) -> dict:
        """cargo-mutants answers one JSON object, or one per line.

        The exact shape is not verified on a real tool yet; the pilot measures it.
        """
        import json

        killed = survived = total = 0
        capped = False
        text = stdout.strip()
        if not text:
            raise Invalid("empty-mutation", "the mutation tool printed nothing")

        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                total = int(payload.get("total", 0))
                killed = int(payload.get("killed", payload.get("caught", 0)))
                survived = int(payload.get("survived", payload.get("missed", 0)))
                capped = bool(payload.get("capped", False))
                if not total:
                    total = killed + survived
                return {"killed": killed, "survived": survived,
                        "total": total, "capped": capped}

        for line in text.splitlines():
            lowered = line.lower()
            if "killed" in lowered or "caught" in lowered:
                killed += 1
            elif "survived" in lowered or "missed" in lowered:
                survived += 1
            elif "mutant" in lowered and "total" in lowered:
                digits = re.findall(r"\d+", line)
                if digits:
                    total = int(digits[-1])
            if "timeout" in lowered and "mutant" in lowered:
                capped = True
        return {"killed": killed, "survived": survived,
                "total": total or (killed + survived), "capped": capped}

    # -- files ----------------------------------------------------------------

    def classify_paths(self, paths: list[str]) -> dict:
        out = {}
        for path in paths:
            text = str(path).replace("\\", "/")
            parts = text.split("/")
            if text.endswith("_test.rs") or "_tests/" in text or "/tests/" in f"/{text}":
                out[path] = "test"
            elif parts[0] == "tests":
                out[path] = "test"
            elif parts[0] == "src" and text.endswith(".rs"):
                out[path] = "production"
            elif parts[0] in ("benches", "examples", "docs") or text.endswith(".md"):
                out[path] = "other"
            else:
                out[path] = "other"
        return out

    def signatures(self, path: Path) -> dict:
        """The type and signature lines a brief may show.

        Bounded on purpose: a line that opens a signature is followed until its
        terminator, at most eight lines. This is not a Rust parser and does not try
        to be one.
        """
        path = Path(path)
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            raise Invalid("unreadable", f"cannot read {path}: {exc}") from exc

        header = re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:async\s+|const\s+|extern\s+\"[^\"]*\"\s+)?"
            r"(fn|struct|enum|trait|type|const|static|mod|union|impl)\b")
        lines = text.splitlines()
        out: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if not header.match(line):
                index += 1
                continue
            block = [line.rstrip()]
            terminator = _terminates(line)
            steps = 0
            while not terminator and steps < 8 and index + 1 < len(lines):
                index += 1
                steps += 1
                block.append(lines[index].rstrip())
                terminator = _terminates(lines[index])
            out.append("\n".join(block))
            index += 1
        return {"signatures": out}

    def edit_formats(self) -> dict:
        return {"formats": ["search_replace", "whole_file"]}

    def apply_edit(self, fmt: str, path: Path, edit: dict) -> dict:
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

    def append_stub(self, path: Path, test_name: str, body: str) -> dict:
        """Append a panicking stub above the first `#[cfg(test)]` module.

        Clippy's `items_after_test_module` warns by default, so a stub below a test
        module fails a lint run with warnings as errors. A file with more than one
        such module refuses the card rather than guess.
        """
        path = Path(path)
        text = path.read_text()
        lines = text.splitlines(keepends=True)

        module_lines = [i for i, line in enumerate(lines)
                        if re.match(r"^\s*#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]", line)]
        if len(module_lines) > 1:
            raise Refused("multiple-test-modules",
                          f"{path}: {len(module_lines)} #[cfg(test)] modules; "
                          f"openfarm will not guess where a stub goes")
        if len(module_lines) == 1:
            insert_at = module_lines[0]
            while insert_at > 0 and re.match(r"^\s*#\s*\[", lines[insert_at - 1]):
                insert_at -= 1
        else:
            insert_at = len(lines)

        if insert_at == len(lines) and lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"

        block = body if body.endswith("\n") else body + "\n"
        if insert_at == len(lines) and lines and lines[-1].strip():
            block = "\n" + block
        lines.insert(insert_at, block)
        path.write_text("".join(lines))
        return {"line": insert_at + 1}

    def format(self, paths: list[Path]) -> dict:
        """Answers the argv; the runner does the formatting."""
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
            is_production = path in production or self.classify_paths(
                [path]).get(path) == _PRODUCTION_KIND

            for pattern, label in BANNED_ANYWHERE:
                if re.search(pattern, text):
                    banned.append({"pattern": label, "file": path,
                                   "line": line_no, "text": text.strip()})
            if is_production:
                for pattern, label in BANNED_PRODUCTION:
                    if re.search(pattern, text):
                        banned.append({"pattern": label, "file": path,
                                       "line": line_no, "text": text.strip()})

            for pattern in (flagged_patterns or []):
                try:
                    hit = re.search(pattern, text)
                except re.error:
                    hit = pattern in text
                if hit:
                    flagged.append({"pattern": pattern, "file": path,
                                    "line": line_no, "text": text.strip()})

        return {"banned": banned, "flagged": flagged}

    # -- errors ---------------------------------------------------------------

    def error_signature(self, stderr: str) -> dict:
        """Error codes and item names, with the volatile parts stripped."""
        text = stderr or ""
        text = re.sub(r"^\s*--> .*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\.rs:\d+:\d+", ".rs", text)
        text = re.sub(r"\b\d+\.\d+(?:ms|s|µs)\b", "<time>", text)
        text = re.sub(r"\b\d+:\d+\b", "<line:col>", text)
        text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", text)
        text = re.sub(r"\s+", " ", text)
        codes = re.findall(r"\b(E\d{4})\b", text)
        names = re.findall(r"`([^`]{1,120})`", text)
        payload = "|".join(sorted(set(codes))) + "::" + "|".join(names[:12])
        if not codes and not names:
            payload = text.strip()[:200]
        import hashlib
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return {"signature": digest, "codes": sorted(set(codes)),
                "items": names[:12]}

    def mutation(self, files: list[str], tests: list[str] | None = None) -> dict:
        return self.commands("mutation", files=files)


def _target_name(spec: str) -> str:
    """`test "full"` becomes `full`; a bare name is left alone."""
    words = spec.strip().split()
    if len(words) >= 2 and words[0] in ("test", "bin", "example", "bench"):
        words = words[1:]
    return " ".join(words).strip().strip('"').strip("'")


def _binary_name(spec: str) -> str:
    """The test binary's own name, from a `Running` line's spec.

    `unittests src/lib.rs` names the unit-test binary by its source file, while an
    integration test is named by its own path. Either way the spec, minus cargo's
    kind word, is the most useful name a record can carry.
    """
    words = spec.split()
    if words and words[0] in ("unittests", "tests"):
        words = words[1:]
    return " ".join(words).strip() or spec.strip()


def _terminates(line: str) -> bool:
    stripped = line.rstrip()
    return stripped.endswith("{") or stripped.endswith(";") or stripped.endswith("}")


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
