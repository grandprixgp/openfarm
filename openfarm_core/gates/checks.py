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
    """Added lines, scanned by the Rust pack.

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
    before_names = {t["name"] for t in before.get("tests", [])} - known
    after_names = {t["name"] for t in after.get("tests", [])} - known

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


def ignored_or_filtered(parsed: dict, named: list[str]) -> GateResult:
    """The named tests must be reported passed, not ignored, filtered or absent."""
    by_name = {r["name"]: r["status"] for r in parsed.get("results", [])}
    failed: list[str] = []
    missing: list[str] = []
    for name in named:
        status = by_name.get(name)
        if status is None:
            missing.append(name)
        elif status != "ok":
            failed.append(f"{name} ({status})")
    if missing:
        return GateResult("pass-after", FAIL,
                          f"named test(s) never reported: {', '.join(missing[:10])}",
                          {"missing": missing, "not-passed": failed})
    if failed:
        return GateResult("pass-after", FAIL,
                          f"named test(s) not passing: {', '.join(failed[:10])}",
                          {"not-passed": failed})
    return GateResult("pass-after", PASS, f"all {len(named)} named test(s) passed")


def suite_green(parsed: dict, known_failing: list[str]) -> GateResult:
    """Every failure must be on `known_failing`, by exact name; no footer is infra."""
    known = set(known_failing)
    unexpected: list[str] = []
    known_red: list[str] = []
    for entry in parsed.get("results", []):
        if entry.get("status") != "FAILED":
            continue
        name = entry.get("name", "")
        if name in known:
            known_red.append(name)
        else:
            unexpected.append(name)

    missing_footers = parsed.get("missing_footers", [])
    if missing_footers:
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
    detail = "suite green"
    if known_red:
        detail += f"; {len(known_red)} known red left out"
    return GateResult("suite", PASS, detail,
                      {"known-red": known_red})


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
                  category: str) -> GateResult:
    """Mutation: blocking for test-backfill, report-only for the rest in the pilot."""
    killed = int(parsed.get("killed", 0))
    survived = int(parsed.get("survived", 0))
    total = int(parsed.get("total", killed + survived))
    capped = bool(parsed.get("capped", False))
    rate = (killed / total) if total else 0.0

    evidence = {"killed": killed, "survived": survived, "total": total,
                "capped": capped, "kill_rate": rate}
    blocking = mode == "required" or (
        category in ("test-backfill",) and mode != "off")

    if mode == "off":
        return GateResult("mutation", PASS, "mutation is off in this config",
                          evidence)
    if capped:
        detail = f"mutation hit its cap after {killed}/{total} mutants"
        return GateResult("mutation", FAIL if blocking else PASS, detail, evidence)
    if rate < min_kill_rate:
        detail = (f"kill rate {rate:.0%} below the required {min_kill_rate:.0%} "
                  f"({killed}/{total})")
        return GateResult("mutation", FAIL if blocking else PASS, detail, evidence)
    return GateResult("mutation", PASS,
                      f"kill rate {rate:.0%} ({killed}/{total})", evidence)


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
