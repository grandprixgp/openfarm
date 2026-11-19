"""The per-attempt path checks: diff within touches, and banned patterns."""

from __future__ import annotations

import re
from pathlib import Path

from ..edits import touched_paths
from ..globs import matches_any, normalise


class GateResult:
    """One gate's answer, and where its evidence sits."""

    def __init__(self, name: str, result: str, detail: str = "",
                 evidence: dict | None = None,
                 detail_path: Path | None = None) -> None:
        self.name = name
        self.result = result  # pass | fail | infra
        self.detail = detail
        self.evidence = dict(evidence or {})
        self.detail_path = detail_path

    @property
    def ok(self) -> bool:
        return self.result == "pass"

    def as_dict(self) -> dict:
        return {"name": self.name, "result": self.result, "detail": self.detail,
                "evidence": self.evidence,
                "detail_path": str(self.detail_path) if self.detail_path else ""}

    def __repr__(self) -> str:
        return f"GateResult({self.name!r}, {self.result})"


PASS = "pass"
FAIL = "fail"
INFRA = "infra"


def diff_within_touches(changed: list[str], touches: list[str], cfg) -> GateResult:
    """Every changed path must be in touches, and not never-touch or forbidden."""
    outside: list[dict] = []
    for path in changed:
        target = normalise(path)
        reason = None
        if touches and not matches_any(target, touches):
            reason = "not in the card's touches"
        else:
            blocked = _blocked(target, cfg)
            if blocked:
                reason = blocked
        if reason:
            outside.append({"path": target, "reason": reason})

    if outside:
        return GateResult(
            "diff-within-touches", FAIL,
            "; ".join(f"{entry['path']}: {entry['reason']}" for entry in outside),
            {"outside": outside})
    return GateResult("diff-within-touches", PASS,
                      f"{len(changed)} changed path(s), all inside touches")


def _blocked(path: str, cfg) -> str | None:
    from ..globs import first_match
    pattern = first_match(path, cfg.never_touch)
    if pattern:
        return f"matches never-touch glob {pattern!r}"
    if matches_any(path, cfg.forbidden_files):
        return "is a forbidden path"
    return None


def _code_of(line: str) -> str:
    """A line's code, without indentation or trailing comment.

    Whitespace differs between a base file and a re-indented diff line, so comparing
    the raw text would miss a pattern that merely moved.
    """
    text = str(line).strip()
    for marker in ("//", "#"):
        index = text.find(marker)
        if index > 0:
            text = text[:index].strip()
    return text


def banned_patterns_rust(added_lines: list[dict], cfg,
                         production_paths: set[str],
                         base_counts: dict[str, dict[str, int]] | None = None,
                         head_counts: dict[str, dict[str, int]] | None = None
                         ) -> GateResult:
    """`banned_patterns` with the Rust pack, as before v0.6."""
    return banned_patterns(added_lines, cfg, production_paths,
                           base_counts=base_counts, head_counts=head_counts)


def banned_patterns(added_lines: list[dict], cfg,
                    production_paths: set[str],
                    base_counts: dict[str, dict[str, int]] | None = None,
                    head_counts: dict[str, dict[str, int]] | None = None,
                    pack=None) -> GateResult:
    """Added lines, scanned by the card's language pack (Rust when none is given).

    The decision is a **count comparison per file**, not a search for new-looking
    lines. `base_counts` and `head_counts` hold how many times each banned label
    appeared in each file at the base commit and at head. A label whose count rose
    was introduced by this card and is banned; one whose count did not rise was
    already there and is flagged for the reviewer.

    Counting is what makes a moved line safe to allow: a `mechanical` edit that
    renames the call before an existing `.unwrap()` keeps the count at one, so it is
    flagged, while a file with three `.unwrap()`s that gains a fourth goes to four
    and is banned. Comparing *added lines* against a base allowance instead would let
    a file with three swallow a fourth for free.

    When `head_counts` is absent the gate falls back to added lines alone, and every
    hit is banned: fewer allowances is the safe direction.
    """
    if pack is None:
        from ..packs.rust import RustPack
        pack = RustPack()
    found = pack.patterns(added_lines, cfg.flagged_patterns, production_paths)
    evidence_counts = {"base": base_counts or {}, "head": head_counts or {}}

    if head_counts is None:
        banned = list(found.get("banned", []))
        flagged = list(found.get("flagged", []))
        evidence = {"banned": banned, "flagged": flagged,
                    "flagged_count": len(flagged), "pre_existing": [],
                    "counts": evidence_counts}
        if banned:
            lines = "; ".join(
                f"{entry['file']}:{entry['line']} {entry['pattern']}"
                for entry in banned[:12])
            return GateResult("banned-patterns", FAIL,
                              f"{len(banned)} banned pattern(s): {lines}", evidence)
        detail = "no banned patterns"
        if flagged:
            detail += f"; {len(flagged)} flagged for the reviewer"
        return GateResult("banned-patterns", PASS, detail, evidence)

    introduced: list[dict] = []
    pre_existing: list[dict] = []
    for path, labels in (head_counts or {}).items():
        for label, count in labels.items():
            base = (base_counts or {}).get(path, {}).get(label, 0)
            if count > base:
                introduced.append({"file": path, "pattern": label,
                                   "base": base, "head": count,
                                   "added": count - base})
            elif count:
                pre_existing.append({"file": path, "pattern": label,
                                     "count": count})

    # Where a line is known, name it: a reviewer wants the line, not just the count.
    located: dict[tuple[str, str], list[dict]] = {}
    for entry in found.get("banned", []):
        located.setdefault((entry.get("file", ""), entry.get("pattern", "")),
                           []).append(entry)
    for entry in introduced:
        hits = located.get((entry["file"], entry["pattern"]), [])
        entry["line"] = hits[-1]["line"] if hits else 0

    flagged = list(found.get("flagged", [])) + pre_existing
    evidence = {"banned": introduced, "flagged": flagged,
                "flagged_count": len(flagged), "pre_existing": pre_existing,
                "counts": evidence_counts}

    if introduced:
        lines = "; ".join(
            f"{entry['file']}"
            + (f":{entry['line']}" if entry.get("line") else "")
            + f" {entry['pattern']} ({entry['base']} -> {entry['head']})"
            for entry in introduced[:12])
        return GateResult("banned-patterns", FAIL,
                          f"{len(introduced)} banned pattern(s) introduced: {lines}",
                          evidence)
    detail = "no banned patterns introduced"
    if pre_existing:
        detail += (f"; {len(pre_existing)} already present at base, flagged for the "
                   f"reviewer")
    if flagged and len(flagged) > len(pre_existing):
        detail += f"; {len(flagged) - len(pre_existing)} flagged"
    return GateResult("banned-patterns", PASS, detail, evidence)


def banned_patterns_any(added_lines: list[dict], cfg,
                        production_paths: set[str]) -> GateResult:
    """Non-Rust fallback: the generic pattern list, no language rules."""
    banned: list[dict] = []
    generic = (
        (r"#\s*\[\s*allow\b", "suppression-allow"),
        (r"eslint-disable", "suppression-eslint"),
        (r"@ts-ignore", "suppression-ts"),
        (r"\.skip\s*\(", "test-skip"),
        (r"\.only\s*\(", "test-only"),
        (r"\btodo!\s*\(", "leftover-stub"),
        (r"\bunimplemented!\s*\(", "leftover-stub"),
        (r"\bdbg!\s*\(", "debug-macro"),
    )
    for entry in added_lines:
        for pattern, label in generic:
            if re.search(pattern, str(entry.get("text", ""))):
                banned.append({"pattern": label, "file": entry.get("path", ""),
                               "line": entry.get("line", 0)})
    return GateResult("banned-patterns", FAIL if banned else PASS,
                      f"{len(banned)} banned pattern(s)" if banned else "none",
                      {"banned": banned})


def test_loss(before: dict, after: dict,
              known_failing: list[str] | None = None) -> GateResult:
    """No test may vanish, be skipped, or be newly ignored.

    `before` and `after` are `parse_list` answers taken before phase A and at the
    end. Counts alone are not enough: a renamed test would hide inside one.
    """
    known = set(known_failing or [])
    # A test is its binary and its name: the same name moved to another binary
    # (a Python test moved to another class) is a loss too. Packs that list no
    # binary, like Rust's, compare by name alone, as before v0.6.
    before_names = {_listed(t) for t in before.get("tests", [])
                    if t["name"] not in known}
    after_names = {_listed(t) for t in after.get("tests", [])
                   if t["name"] not in known}

    lost = sorted(before_names - after_names)
    if lost:
        return GateResult("no-test-loss", FAIL,
                          f"{len(lost)} test(s) gone: " + ", ".join(lost[:10]),
                          {"lost": lost})
    if len(after_names) < len(before_names):
        return GateResult("no-test-loss", FAIL,
                          f"test count fell from {len(before_names)} to "
                          f"{len(after_names)}")
    return GateResult("no-test-loss", PASS,
                      f"{len(after_names)} test(s), none lost")


def _listed(test: dict) -> str:
    binary = str(test.get("binary", "") or "")
    return f"{binary}::{test['name']}" if binary else str(test["name"])


def rows_for_test(rows: list[dict], name: str, file: str = "",
                  row_file=None) -> list[dict]:
    """The result (or listing) rows that belong to one named test.

    A row belongs when its name is the test's name, or when the test names a
    function and the row is one of its parameter cases (`test_param[1-2]` for
    `test_param`, as pytest reports them). A name that carries its own parameter
    id matches that row only. Names are never matched as substrings: a runner's
    `-k test_adds` also runs `test_adds_more`, and that row is another test's.

    `row_file` is the pack's `row_file`: the source file a row came from, or None
    when its runner does not say. With the card's `file`, rows from another file
    are another test's too, even with the same name.
    """
    name = str(name)
    out = []
    for row in rows:
        row_name = str(row.get("name", ""))
        if row_name != name and not _is_parameter_row(row_name, name):
            continue
        if file and row_file is not None:
            where = row_file(row)
            if where and not same_test_file(where, file):
                continue
        out.append(row)
    return out


def _is_parameter_row(row_name: str, name: str) -> bool:
    return ("[" not in name and row_name.startswith(name + "[")
            and row_name.endswith("]"))


def same_test_file(a: str, b: str) -> bool:
    """True when two spellings of a test file are the same file.

    A runner may report paths from its own root (a unit folder) while a card names
    them from the repository root, so one may be a suffix of the other, by whole
    path parts.
    """
    left, right = _plain_path(a), _plain_path(b)
    if not left or not right:
        return False
    return (left == right or left.endswith("/" + right)
            or right.endswith("/" + left))


def _plain_path(path: str) -> str:
    text = normalise(str(path or ""))
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def combined_status(rows: list[dict]) -> str:
    """One status for every row of a named test.

    `not-run` with no rows; the shared status when every row agrees; otherwise
    `mixed`. A mixed test is neither failing nor passing: it proves nothing.
    """
    if not rows:
        return "not-run"
    statuses = {str(row.get("status", "unknown")) for row in rows}
    if len(statuses) == 1:
        return statuses.pop()
    return "mixed"


def ignored_or_filtered(parsed: dict, named: list,
                        run_cls: str = "pass", exit_code: int = 0,
                        files: dict[str, str] | None = None,
                        row_file=None) -> GateResult:
    """The named tests must be reported passed, not ignored, filtered or absent.

    Every row of a named test must pass: its parameter cases, and every binary
    that reported it. `files` maps a name to the card's file for it, so rows of a
    same-named test elsewhere are left out (see `rows_for_test`).

    Only a finished run can say so. A run the runner classed as infra (timed out,
    killed, a machine error) is infra here whatever rows it printed before it
    stopped, and so is one with a binary that printed no footer; a run that does
    not compile, or that crashed on a signal, is a failure.
    """
    unjudged = _unjudged_run(parsed, run_cls, exit_code, "pass-after")
    if unjudged is not None:
        return unjudged
    results = parsed.get("results", [])
    files = files or {}

    failed: list[str] = []
    missing: list[str] = []
    rows_of: dict[str, list[dict]] = {}
    for item in named:
        if isinstance(item, tuple) and len(item) == 2:
            binary, name = str(item[0]), str(item[1])
        elif isinstance(item, dict):
            binary = str(item.get("binary", ""))
            name = str(item.get("name", ""))
        else:
            binary, name = "", str(item)
        matches = rows_for_test(results, name, files.get(name, ""), row_file)
        if binary:
            matches = [m for m in matches if m.get("binary", "") == binary]
        label = f"{binary}::{name}" if binary else name

        if not matches:
            missing.append(label)
            continue
        bad = [m.get("status", "unknown") for m in matches if m.get("status") != "ok"]
        if bad:
            failed.append(f"{label} ({bad[0]})")
            rows_of[label] = [dict(m) for m in matches]

    if missing:
        return GateResult("pass-after", FAIL,
                          f"named test(s) never reported: {', '.join(missing[:10])}",
                          {"missing": missing, "not-passed": failed,
                           "rows": rows_of})
    if failed:
        return GateResult("pass-after", FAIL,
                          f"named test(s) not passing: {', '.join(failed[:10])}",
                          {"not-passed": failed, "rows": rows_of})
    return GateResult("pass-after", PASS, f"all {len(named)} named test(s) passed")


#: The exit code pytest and unittest give a finished run that collected no test.
_EMPTY_RUN_EXIT = 5


def suite_green(parsed: dict, known_failing: list[str],
                run_cls: str = "pass", exit_code: int = 0) -> GateResult:
    """Every failure must be on `known_failing`, by exact name.

    A missing footer is infra **only when nothing compiled** and the binary did
    not die from a non-SIGKILL crash signal (such as SIGABRT/SIGSEGV). A test
    binary that failed to compile has a compile error and no footer, and that is
    the model's edit being wrong, not the machine: the card must go back to the
    implementer with the compiler output, which is the single most useful thing
    it can be told.
    """
    known = set(known_failing)
    unexpected: list[str] = []
    known_red: list[str] = []
    failed_results: list[dict] = []
    for entry in parsed.get("results", []):
        if entry.get("status") != "FAILED":
            continue
        failed_results.append(entry)
        name = entry.get("name", "")
        if name in known:
            known_red.append(name)
        else:
            unexpected.append(name)

    compile_errors = parsed.get("compile_errors", [])
    missing_footers = parsed.get("missing_footers", [])
    footers = parsed.get("footers", [])

    if compile_errors:
        targets = sorted({_compile_name(entry) for entry in compile_errors})
        return GateResult(
            "suite", FAIL,
            f"the code did not compile ({len(targets)} target(s)): "
            + ", ".join(targets[:8]),
            {"compile_errors": compile_errors, "unexpected": unexpected,
             "known-red": known_red})

    if run_cls == INFRA:
        # The runner decides infra, and its word wins over anything the run printed
        # before it stopped: a killed run's partial output can carry a footer-shaped
        # line from the code under test, or simply stop before the failing test.
        return GateResult(
            "suite", INFRA,
            "the runner classed the run as infra (timed out, killed or a machine "
            "error), so its output cannot be judged",
            {"run_class": run_cls, "exit_code": exit_code,
             "missing_footers": missing_footers, "unexpected": unexpected})

    if missing_footers:
        if run_cls == FAIL and exit_code < 0 and exit_code != -9:
            return GateResult(
                "suite", FAIL,
                f"test binary crashed with signal {-exit_code}: "
                + ", ".join(missing_footers[:8]),
                {"missing_footers": missing_footers, "unexpected": unexpected,
                 "exit_code": exit_code})
        return GateResult(
            "suite", INFRA,
            "a test binary printed no footer, so the run died partway: "
            + ", ".join(missing_footers[:8]),
            {"missing_footers": missing_footers, "unexpected": unexpected})

    if unexpected:
        return GateResult("suite", FAIL,
                          f"{len(unexpected)} red test(s) not on known_failing: "
                          + ", ".join(unexpected[:10]),
                          {"unexpected": unexpected, "known-red": known_red})

    footer_failed = sum(int(f.get("failed", 0)) for f in footers)
    if footer_failed > len(failed_results):
        return GateResult(
            "suite", FAIL,
            "test footer reported failures not matched cleanly",
            {"footers": footers, "unexpected": unexpected, "known-red": known_red})
    if any(f.get("status") == "FAILED" for f in footers) and not failed_results:
        return GateResult(
            "suite", FAIL,
            "test footer reported failures not matched cleanly",
            {"footers": footers, "unexpected": unexpected, "known-red": known_red})

    if run_cls == FAIL and not failed_results:
        # The run failed and nothing it printed says which test did: cargo that
        # stopped before building (`--locked` with a stale lockfile, a manifest
        # that does not parse), pytest that exits 1 after "N passed" over a
        # coverage floor. Whatever it was, it is not a green suite. The one
        # failed exit that is a finished run is the empty one (`NO TESTS RAN`,
        # exit 5): a footer that counted no test at all.
        counted = sum(int(f.get("passed", 0)) + int(f.get("failed", 0))
                      + int(f.get("ignored", 0)) for f in footers)
        # Only that exit code is the empty run: pytest stopped by Ctrl-C prints
        # "no tests ran" too, with exit code 2, and that is not a finished run.
        if not footers or counted or exit_code != _EMPTY_RUN_EXIT:
            return GateResult(
                "suite", FAIL,
                f"the test run failed (exit code {exit_code}) and nothing it printed "
                f"names a failing test, so it cannot be read as green",
                {"run_class": run_cls, "exit_code": exit_code, "footers": footers,
                 "unexpected": unexpected, "known-red": known_red})

    detail = "suite green"
    if known_red:
        detail += f"; {len(known_red)} known red left out"
    return GateResult("suite", PASS, detail,
                      {"known-red": known_red})


def _unjudged_run(parsed: dict, run_cls: str, exit_code: int,
                  gate: str) -> GateResult | None:
    """The answer for a run that cannot be read as green, or None if it can.

    The same order as `suite_green`: a compile error is the edit's fault, the
    runner's infra class wins over the output, a binary without a footer died
    partway (a crash on a signal other than SIGKILL is the code's fault).
    """
    compile_errors = parsed.get("compile_errors", [])
    missing = parsed.get("missing_footers", [])
    if compile_errors:
        targets = sorted({_compile_name(entry) for entry in compile_errors})
        return GateResult(gate, FAIL,
                          f"the code did not compile ({len(targets)} target(s)): "
                          + ", ".join(targets[:8]),
                          {"compile_errors": compile_errors})
    if run_cls == INFRA:
        return GateResult(gate, INFRA,
                          "the runner classed the run as infra (timed out, killed or "
                          "a machine error), so its output cannot be judged",
                          {"run_class": run_cls, "exit_code": exit_code})
    if missing:
        if run_cls == FAIL and exit_code < 0 and exit_code != -9:
            return GateResult(gate, FAIL,
                              f"test binary crashed with signal {-exit_code}: "
                              + ", ".join(missing[:8]),
                              {"missing_footers": missing, "exit_code": exit_code})
        return GateResult(gate, INFRA,
                          "a test binary printed no footer, so the run died partway: "
                          + ", ".join(missing[:8]),
                          {"missing_footers": missing})
    return None


def _compile_name(entry: dict) -> str:
    return str(entry.get("target") or entry.get("pkg") or entry.get("binary") or "?")


def frozen_check(expected: dict[str, str], actual: dict[str, str]) -> GateResult:
    """Test files and stub lines must hash-match what phase A froze."""
    changed = [path for path in expected if expected[path] != actual.get(path)]
    added = [path for path in actual if path not in expected]
    if changed or added:
        detail = []
        if changed:
            detail.append("changed: " + ", ".join(sorted(changed)[:8]))
        if added:
            detail.append("appeared: " + ", ".join(sorted(added)[:8]))
        return GateResult("tests-frozen", FAIL, "; ".join(detail),
                          {"changed": changed, "added": added})
    return GateResult("tests-frozen", PASS,
                      f"{len(expected)} frozen file(s) unchanged")


def revert_check(expected_hash: str, current_hash: str,
                 refrozen: bool = False) -> GateResult:
    """The final test files must be the set that failed on base after phase A."""
    if expected_hash == current_hash and not refrozen:
        return GateResult("revert-check", PASS,
                          "final tests hash-match the set that failed on base")
    if refrozen:
        return GateResult("revert-check", PASS,
                          "tests were rewritten and re-frozen; fail-before ran again "
                          "on the new set")
    return GateResult("revert-check", FAIL,
                      "the final tests are not the set that failed on base",
                      {"expected": expected_hash, "actual": current_hash})


def mutation_gate(parsed: dict, min_kill_rate: float, mode: str,
                  category: str,
                  required_for: list[str] | tuple[str, ...] = ("test-backfill",)
                  ) -> GateResult:
    """Mutation: blocking when mode is required or category is in `required_for`.

    The kill rate divides by the mutants that could be judged. A pack that counts
    timed-out and unviable mutants (cargo-mutants does) says so with `judged`,
    caught plus missed: a mutant that did not build, or whose tests never finished,
    proves nothing either way. Without `judged` every mutant counts, as before.
    """
    killed = int(parsed.get("killed", 0))
    survived = int(parsed.get("survived", 0))
    total = int(parsed.get("total", killed + survived))
    timeout = int(parsed.get("timeout", 0) or 0)
    unviable = int(parsed.get("unviable", 0) or 0)
    judged = int(parsed["judged"]) if "judged" in parsed else total
    capped = bool(parsed.get("capped", False))
    rate = (killed / judged) if judged else 0.0

    evidence = {"killed": killed, "survived": survived, "total": total,
                "capped": capped, "kill_rate": rate}
    if "judged" in parsed:
        evidence.update({"judged": judged, "timeout": timeout, "unviable": unviable})
    blocking = mode == "required" or (
        category in tuple(required_for) and mode != "off")
    unjudged = []
    if timeout:
        unjudged.append(f"{timeout} timed out")
    if unviable:
        unjudged.append(f"{unviable} unviable")
    tail = f"; not judged: {', '.join(unjudged)}" if unjudged else ""

    if mode == "off":
        return GateResult("mutation", PASS, "mutation is off in this config",
                          evidence)
    if capped:
        detail = f"mutation hit its cap after {killed}/{judged} mutants"
        return GateResult("mutation", FAIL if blocking else PASS, detail, evidence)
    if not judged:
        if parsed.get("scoped") and not timeout and category not in tuple(required_for):
            detail = f"no mutable expressions in the card's diff ({total} found{tail})"
            return GateResult("mutation", PASS, detail, evidence)
        detail = f"no mutant could be judged ({total} found{tail})"
        return GateResult("mutation", FAIL if blocking else PASS, detail, evidence)
    if rate < min_kill_rate:
        detail = (f"kill rate {rate:.0%} below the required {min_kill_rate:.0%} "
                  f"({killed}/{judged}{tail})")
        return GateResult("mutation", FAIL if blocking else PASS, detail, evidence)
    return GateResult("mutation", PASS,
                      f"kill rate {rate:.0%} ({killed}/{judged}{tail})", evidence)


def extra_gate_result(name: str, passed: bool, on_fail: str,
                      detail: str = "") -> GateResult:
    """An `[[gates.extra]]` command's answer."""
    if passed:
        return GateResult(f"extra:{name}", PASS, detail or "passed")
    return GateResult(f"extra:{name}", FAIL,
                      (detail or "failed") + f" (on_fail = {on_fail})",
                      {"on_fail": on_fail})


def paths_touched_by_extra_gate(changed: list[str], paths: list[str]) -> bool:
    """An extra gate runs only when the diff touches its paths."""
    if not paths:
        return True
    return any(matches_any(normalise(path), paths) for path in changed)
