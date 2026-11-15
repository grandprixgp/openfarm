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
#: `test test_name ... ok` (also `test test_name - should panic ... ok`)
_TEST_LINE_RE = re.compile(
    r"^test\s+(?P<name>\S+)(?:\s+-\s+should\s+panic(?:\s*\([^)]*\))?)?"
    r"\s+\.\.\.\s+(?P<status>ok|FAILED|ignored|bench)\b")
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
        #: Cleared when the worktree has no lock file, because `--locked` means
        #: "use the lock as it is" and cargo refuses to make one under it.
        self.has_lockfile = True
        self.notes: list[str] = []

    def configure(self, worktree: Path) -> None:
        """Adapt the flags to this worktree.

        A library often commits no lock file, and Atlas-style repositories always
        do. Dropping `--locked` where there is no lock is the only honest choice:
        keeping it would fail every command, and pretending the lock existed would
        hide that the resolution was free.
        """
        lockfile = Path(worktree) / "Cargo.lock"
        if "--locked" in self.cargo_flags and not lockfile.exists():
            self.has_lockfile = False
            self.cargo_flags = [flag for flag in self.cargo_flags if flag != "--locked"]
            self.notes.append(
                "Cargo.lock is not committed, so --locked is not passed: the "
                "dependency resolution is not pinned")

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

        stderr_bins: list[str] = []
        for err_line in (stderr or "").splitlines():
            stripped_err = err_line.lstrip()
            if stripped_err.startswith(("Compiling ", "Finished ")):
                continue
            running = _RUNNING_RE.match(err_line) or _RUNNING_BARE_RE.match(err_line)
            if running:
                stderr_bins.append(_binary_name(running.group("spec")))

        current_bin = ""
        seen_footer = True  # no binary open yet
        pending_results: list[dict] = []
        stderr_idx = 0

        for line in (stdout or "").splitlines():
            running = _RUNNING_RE.match(line) or _RUNNING_BARE_RE.match(line)
            if running:
                if pending_results:
                    results.extend(pending_results)
                    pending_results.clear()
                if not seen_footer and current_bin:
                    if current_bin not in missing:
                        missing.append(current_bin)
                current_bin = _binary_name(running.group("spec"))
                seen_footer = False
                continue

            test_line = _TEST_LINE_RE.match(line)
            if test_line:
                entry = {
                    "name": test_line.group("name"),
                    "binary": current_bin,
                    "status": test_line.group("status"),
                }
                if current_bin:
                    results.append(entry)
                else:
                    pending_results.append(entry)
                continue

            footer = _FOOTER_RE.match(line.strip())
            if footer:
                footer_bin = current_bin
                if not footer_bin:
                    if stderr_idx < len(stderr_bins):
                        footer_bin = stderr_bins[stderr_idx]
                        stderr_idx += 1
                else:
                    if footer_bin in stderr_bins[stderr_idx:]:
                        pos = stderr_bins.index(footer_bin, stderr_idx)
                        for skipped_bin in stderr_bins[stderr_idx:pos]:
                            if skipped_bin not in missing:
                                missing.append(skipped_bin)
                        stderr_idx = pos + 1

                for entry in pending_results:
                    entry["binary"] = footer_bin
                    results.append(entry)
                pending_results.clear()

                footers.append({
                    "binary": footer_bin,
                    "status": footer.group("status"),
                    "passed": int(footer.group("passed")),
                    "failed": int(footer.group("failed")),
                    "ignored": int(footer.group("ignored")),
                    "filtered": int(footer.group("filtered")),
                })
                seen_footer = True
                current_bin = ""

        if pending_results:
            assign_bin = current_bin or (
                stderr_bins[stderr_idx] if stderr_idx < len(stderr_bins) else "")
            for entry in pending_results:
                entry["binary"] = assign_bin
                results.append(entry)
            pending_results.clear()

        if not seen_footer and current_bin and current_bin not in missing:
            missing.append(current_bin)

        for rem_bin in stderr_bins[stderr_idx:]:
            if rem_bin not in missing:
                missing.append(rem_bin)

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
        """test, production or other.

        A crate can sit at the repository root or under a workspace folder, so the
        source directory is looked for at any depth. Getting this wrong is not
        cosmetic: production-only patterns and the mutation gate both key off it, so
        a workspace crate classified as `other` would silently skip both.
        """
        from ..globs import normalise

        out = {}
        for path in paths:
            text = normalise(str(path))
            parts = [part for part in text.split("/") if part]
            in_tests = ("/tests/" in f"/{text}" or parts[:1] == ["tests"]
                        or text.endswith("_test.rs") or "_tests/" in text)
            in_source = "src" in parts[:-1]
            if in_tests:
                out[path] = "test"
            elif in_source and text.endswith(".rs"):
                out[path] = "production"
            elif parts[:1] in (["benches"], ["examples"], ["docs"]) or \
                    text.endswith(".md"):
                out[path] = "other"
            else:
                out[path] = "other"
        return out

    def signatures(self, path: Path) -> dict:
        """The type and signature lines a brief may show.

        Extracts declarations (`struct`, `enum`, `trait`, `type`, `const`, `static`,
        `union`, `mod`, `impl`, `fn`) while stripping function bodies so Phase A
        never sees production implementations.
        """
        path = Path(path)
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            raise Invalid("unreadable", f"cannot read {path}: {exc}") from exc

        header = re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:(?:unsafe|async|const)\s+|extern\s+\"[^\"]*\"\s+)*"
            r"(fn|struct|enum|trait|type|const|static|mod|union|impl)\b")
        inner_fn_header = re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:(?:unsafe|async|const)\s+|extern\s+\"[^\"]*\"\s+)*"
            r"fn\b")

        lines = text.splitlines()
        scanner = _RustScanner()
        masks = [scanner.mask_line(line) for line in lines]
        in_comment_or_str = scanner.line_entry_blocked

        out: list[str] = []
        index = 0
        total_lines = len(lines)
        max_decl_lines = 60

        def _skip_fn_body(start_idx: int, initial_depth: int) -> int:
            depth = initial_depth
            cur = start_idx
            while depth > 0 and cur + 1 < total_lines:
                cur += 1
                m = masks[cur]
                depth += m.count("{") - m.count("}")
            return cur + 1

        def _collect_fn_signature(start_idx: int) -> tuple[list[str], int]:
            """Collect a `fn` signature, stripping any `{ ... }` body."""
            block: list[str] = []
            cur = start_idx
            steps = 0
            while cur < total_lines and steps < 12:
                line = lines[cur]
                mask = masks[cur]
                open_col = mask.find("{")
                if open_col != -1:
                    prefix = line[:open_col].rstrip()
                    block.append(f"{prefix} {{ ... }}" if prefix else "{ ... }")
                    tail_mask = mask[open_col:]
                    depth = tail_mask.count("{") - tail_mask.count("}")
                    next_idx = _skip_fn_body(cur, depth)
                    return block, next_idx
                block.append(line.rstrip())
                if _strip_line_comment(line).rstrip().endswith(";"):
                    return block, cur + 1
                cur += 1
                steps += 1
            return block, cur

        while index < total_lines:
            if in_comment_or_str[index]:
                index += 1
                continue
            line = lines[index]
            match = header.match(line)
            if not match:
                index += 1
                continue

            kw = match.group(1)

            if kw == "fn":
                block, next_idx = _collect_fn_signature(index)
                out.append("\n".join(block))
                index = max(next_idx, index + 1)
                continue

            if kw in ("type", "const", "static"):
                block = []
                depth = 0
                cur = index
                while cur < total_lines and len(block) < max_decl_lines:
                    cur_line = lines[cur]
                    mask = masks[cur]
                    block.append(cur_line.rstrip())
                    depth += mask.count("{") - mask.count("}")
                    if depth <= 0 and _strip_line_comment(cur_line).rstrip().endswith(";"):
                        cur += 1
                        break
                    cur += 1
                out.append("\n".join(block))
                index = max(cur, index + 1)
                continue

            if kw in ("struct", "enum", "union"):
                block = []
                depth = 0
                seen_open = False
                cur = index
                while cur < total_lines and len(block) < max_decl_lines:
                    cur_line = lines[cur]
                    mask = masks[cur]
                    block.append(cur_line.rstrip())
                    if "{" in mask:
                        seen_open = True
                    depth += mask.count("{") - mask.count("}")
                    if seen_open and depth <= 0:
                        cur += 1
                        break
                    if not seen_open and _strip_line_comment(cur_line).rstrip().endswith(";"):
                        cur += 1
                        break
                    cur += 1
                out.append("\n".join(block))
                index = max(cur, index + 1)
                continue

            if kw in ("trait", "impl", "mod"):
                if kw == "mod" and _strip_line_comment(line).rstrip().endswith(";"):
                    out.append(line.rstrip())
                    index += 1
                    continue

                block: list[str] = []
                depth = 0
                seen_open = False
                cur = index
                # Collect header up to opening `{`.
                while cur < total_lines and len(block) < max_decl_lines:
                    cur_line = lines[cur]
                    mask = masks[cur]
                    block.append(cur_line.rstrip())
                    if "{" in mask:
                        seen_open = True
                    depth += mask.count("{") - mask.count("}")
                    cur += 1
                    if seen_open:
                        break
                    if _strip_line_comment(cur_line).rstrip().endswith(";"):
                        break

                if not seen_open or depth <= 0:
                    out.append("\n".join(block))
                    index = max(cur, index + 1)
                    continue

                truncated = False
                while cur < total_lines and depth > 0:
                    if in_comment_or_str[cur]:
                        mask = masks[cur]
                        depth += mask.count("{") - mask.count("}")
                        if len(block) < max_decl_lines - 1:
                            block.append(lines[cur].rstrip())
                        cur += 1
                        continue

                    cur_line = lines[cur]
                    mask = masks[cur]
                    if inner_fn_header.match(cur_line):
                        fn_block, next_idx = _collect_fn_signature(cur)
                        if len(block) + len(fn_block) < max_decl_lines:
                            block.extend(fn_block)
                        elif not truncated:
                            block.append("    // ...")
                            truncated = True
                        # _collect_fn_signature already consumed the fn body at depth >= 1
                        cur = max(next_idx, cur + 1)
                        continue

                    depth += mask.count("{") - mask.count("}")
                    if depth <= 0:
                        block.append(cur_line.rstrip())
                        cur += 1
                        break
                    if len(block) < max_decl_lines - 1:
                        block.append(cur_line.rstrip())
                    elif not truncated:
                        block.append("    // ...")
                        truncated = True
                    cur += 1

                out.append("\n".join(block))
                index = max(cur, index + 1)
                continue

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

        stripped_body = "\n".join(
            _strip_line_comment(line) for line in (body or "").splitlines()
        )
        has_panic_macro = bool(
            re.search(r"\b(?:todo|unimplemented)!\s*\(", stripped_body))
        if not has_panic_macro or not _has_balanced_braces(body or ""):
            raise Refused(
                "non-panicking-stub",
                "a stub must panic with todo!() or unimplemented!() and have balanced braces")

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
            if text.lstrip().startswith("//"):
                continue
            code = _strip_line_comment(text)
            if not code.strip():
                continue

            is_production = path in production or self.classify_paths(
                [path]).get(path) == _PRODUCTION_KIND

            for pattern, label in BANNED_ANYWHERE:
                for _match in re.finditer(pattern, code):
                    banned.append({"pattern": label, "file": path,
                                   "line": line_no, "text": text.strip()})
            if is_production:
                for pattern, label in BANNED_PRODUCTION:
                    for _match in re.finditer(pattern, code):
                        banned.append({"pattern": label, "file": path,
                                       "line": line_no, "text": text.strip()})

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
        """Error codes and item names, with the volatile parts stripped."""
        raw = stderr or ""
        failed_tests = re.findall(
            r"^test\s+(\S+)(?:\s+-\s+should\s+panic(?:\s*\([^)]*\))?)?"
            r"\s+\.\.\.\s+FAILED\b",
            raw,
            flags=re.MULTILINE,
        )
        text = re.sub(r"^\s*--> .*$", "", raw, flags=re.MULTILINE)
        text = re.sub(r"panicked at (?:/[^\s:]+/)?(src/|tests/|crates/)",
                      r"panicked at \1", text)
        text = re.sub(r"/(?:tmp|var|home|Users)/[^\s:)]+", "<path>", text)
        text = re.sub(r"\.rs:\d+(?::\d+)?", ".rs", text)
        text = re.sub(r"\b\d+\.\d+(?:ms|s|µs|us|ns)\b", "<time>", text)
        text = re.sub(r"\b\d+:\d+\b", "<line:col>", text)
        text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", text)
        text = re.sub(r"\s+", " ", text)
        codes = sorted(set(re.findall(r"\b(E\d{4})\b", text)))
        panicked = re.findall(r"thread '([^']+)' panicked at", text)
        failed = sorted(set(failed_tests) | set(panicked))
        names = re.findall(r"`([^`]{1,120})`", text)
        payload = (
            "|".join(codes)
            + "::" + "|".join(failed[:12])
            + "::" + "|".join(names[:12])
        )
        if not codes and not failed and not names:
            payload = text.strip()[:200]
        import hashlib
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return {"signature": digest, "codes": codes,
                "items": names[:12], "failed_tests": failed[:12]}

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


def _is_ident(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


class _RustScanner:
    """Line-by-line Rust lexical masker for strings, chars, and comments."""

    def __init__(self) -> None:
        self.block_comment_depth = 0
        self.in_string = False
        self.raw_hashes: int | None = None
        self.line_entry_blocked: list[bool] = []

    def mask_line(self, line: str) -> str:
        """Return a same-length string with strings, chars, and comments masked."""
        self.line_entry_blocked.append(
            self.block_comment_depth > 0 or self.in_string or self.raw_hashes is not None
        )
        n = len(line)
        out = list(line)
        i = 0
        while i < n:
            if self.block_comment_depth > 0:
                if line.startswith("/*", i):
                    self.block_comment_depth += 1
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                    continue
                if line.startswith("*/", i):
                    self.block_comment_depth -= 1
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                    continue
                out[i] = " "
                i += 1
                continue

            if self.raw_hashes is not None:
                end_tok = '"' + ("#" * self.raw_hashes)
                if line.startswith(end_tok, i):
                    for k in range(len(end_tok)):
                        out[i + k] = " "
                    i += len(end_tok)
                    self.raw_hashes = None
                    continue
                out[i] = " "
                i += 1
                continue

            if self.in_string:
                if line[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                    continue
                if line[i] == '"':
                    self.in_string = False
                out[i] = " "
                i += 1
                continue

            if line.startswith("//", i):
                for k in range(i, n):
                    out[k] = " "
                break

            if line.startswith("/*", i):
                self.block_comment_depth = 1
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue

            # Raw string literal: r"...", r#"..."#, br#"..."#, cr#"..."#
            if line[i] == "r" and (
                i == 0
                or not _is_ident(line[i - 1])
                or (line[i - 1] in ("b", "c") and (i == 1 or not _is_ident(line[i - 2])))
            ):
                j = i + 1
                while j < n and line[j] == "#":
                    j += 1
                if j < n and line[j] == '"':
                    self.raw_hashes = j - (i + 1)
                    for k in range(i, j + 1):
                        out[k] = " "
                    i = j + 1
                    continue

            if line[i] == '"':
                self.in_string = True
                out[i] = " "
                i += 1
                continue

            if line[i] == "'":
                if i + 2 < n and line[i + 1] not in ("\\", "'") and line[i + 2] == "'":
                    out[i] = out[i + 1] = out[i + 2] = " "
                    i += 3
                    continue
                if i + 2 < n and line[i + 1] == "\\":
                    j = i + 2
                    if j < n:
                        j += 1
                    while j < n and line[j] != "'":
                        j += 1
                    if j < n and line[j] == "'":
                        for k in range(i, j + 1):
                            out[k] = " "
                        i = j + 1
                        continue

            i += 1
        return "".join(out)


def _strip_line_comment(line: str) -> str:
    """Return `line` without any trailing `// ...` comment outside quoted strings."""
    n = len(line)
    in_string = False
    raw_hashes: int | None = None
    i = 0
    while i < n:
        if raw_hashes is not None:
            end_tok = '"' + ("#" * raw_hashes)
            if line.startswith(end_tok, i):
                i += len(end_tok)
                raw_hashes = None
                continue
            i += 1
            continue
        if in_string:
            if line[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if line[i] == '"':
                in_string = False
            i += 1
            continue
        if line.startswith("//", i):
            return line[:i]
        if line[i] == "r" and (
            i == 0
            or not _is_ident(line[i - 1])
            or (line[i - 1] in ("b", "c") and (i == 1 or not _is_ident(line[i - 2])))
        ):
            j = i + 1
            while j < n and line[j] == "#":
                j += 1
            if j < n and line[j] == '"':
                raw_hashes = j - (i + 1)
                i = j + 1
                continue
        if line[i] == '"':
            in_string = True
            i += 1
            continue
        if line[i] == "'":
            if i + 2 < n and line[i + 1] not in ("\\", "'") and line[i + 2] == "'":
                i += 3
                continue
            if i + 2 < n and line[i + 1] == "\\":
                j = i + 3
                while j < n and line[j] != "'":
                    j += 1
                if j < n and line[j] == "'":
                    i = j + 1
                    continue
        i += 1
    return line


def _has_balanced_braces(text: str) -> bool:
    """True when `{` and `}` outside strings/comments are balanced."""
    scanner = _RustScanner()
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


def _terminates(line: str) -> bool:
    stripped = _strip_line_comment(line).rstrip()
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
