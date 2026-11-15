"""The reviewer dossier: one page, nine sections, in the spec's order.

The test diff comes first and the production diff second, so the reviewer checks the
contract before the code.
"""

from __future__ import annotations

from pathlib import Path

from .state import write_json


def build_dossier(card_run, final_state: str) -> dict:
    """Every section, as data. `dossier.md` and `dossier.json` carry the same thing."""
    stored_card = {}
    try:
        from .state import read_json
        stored_card = read_json(card_run.card_layout / "card.json", {}) or {}
    except Exception:  # noqa: BLE001 - a dossier must never fail the card
        stored_card = {}

    card_hash_at_claim = stored_card.get("hash", "")
    card_hash_at_end = None
    try:
        if getattr(card_run, "card_source", None):
            card_hash_at_end = card_run.card_source.hash(card_run.card.id)["hash"]
    except Exception:  # noqa: BLE001
        card_hash_at_end = None

    changed = (card_run._changed_since_base()
               if hasattr(card_run, "_changed_since_base") else [])
    touches_check = _touches_check(card_run, changed)
    banned = _evidence(card_run, "banned-patterns")

    gates = [
        {"name": gate.name, "result": gate.result,
         "attempt": card_run.attempt,
         "detail": gate.detail,
         "output": str(gate.detail_path or "")}
        for gate in card_run.gates
    ]

    counter = dict(card_run.counter_summary)
    mutation = dict(card_run.mutation_summary)
    restatement = (card_run.checker_restatement.as_dict()
                   if card_run.checker_restatement else {})

    return {
        "schema_version": 1,
        "card": card_run.card.id,
        "run": card_run.run_id,
        "final_state": final_state,
        "stop_code": card_run.stop_code.value if card_run.stop_code else "",
        "1_risk": {
            "card_hash_at_claim": card_hash_at_claim,
            "card_changed": bool(card_hash_at_end and
                                 card_hash_at_end != card_hash_at_claim),
            "card_hash_at_end": card_hash_at_end or "",
            "files_changed": len(changed),
            "paths": changed,
            "lines_added": _line_counts(card_run)[0],
            "lines_removed": _line_counts(card_run)[1],
            "touches_check": touches_check,
            "existing_tests_changed": list(card_run.existing_tests_changed),
            "banned_patterns": len(banned.get("banned", [])),
            "flagged_patterns": banned.get("flagged", []),
            "unformatted": list(card_run.unformatted),
            "catch_up": card_run.catch_up_result,
        },
        "2_gates": gates,
        "3_known_failing": {
            "excluded": list(card_run.known_failing_excluded),
            "config_list": list(card_run.cfg.known_failing),
        },
        "4_red_green": _red_green(card_run),
        "5_struggle": {
            "attempts": card_run.budget.implementer_attempts,
            "test_writer_attempts": card_run.budget.test_writer_attempts,
            "planner_split": card_run.planner_used,
            "failed_attempts": _failed_attempts(card_run),
        },
        "6_checker": {
            "restatement": restatement,
            "restatement_mismatch": bool(restatement.get("mismatch")),
            "counter_tests": counter,
        },
        "7_mutation": mutation,
        "8_not_checked": _not_checked(card_run),
        "9_diffs": {
            "test_diff": _test_diff(card_run),
            "production_diff": _production_diff(card_run),
        },
    }


def _line_counts(card_run) -> tuple[int, int]:
    from .edits import line_counts
    return line_counts(getattr(card_run, "current_diff", "") or "")


def _touches_check(card_run, changed: list[str]) -> str:
    """Every changed path must be in touches, or a test file the card named.

    A card's named test files are answerable by the test writer even when `touches`
    doesn't list them, so they are not a risk and don't belong in this header.
    """
    from .globs import matches_any
    answerable = list(card_run.card.touches) + [
        test.file for test in card_run.card.tests if test.file]
    answerable += list(card_run.card.test_edits)
    outside = [path for path in changed if not matches_any(path, answerable)]
    if outside:
        return "outside touches: " + ", ".join(outside)
    return f"{len(changed)} path(s), all inside touches"


def _evidence(card_run, name: str) -> dict:
    for gate in card_run.gates:
        if gate.name == name:
            return gate.evidence
    return {}


def _red_green(card_run) -> list[dict]:
    rows = []
    for test in card_run.card.tests:
        rows.append({
            "name": test.name,
            "file": test.file,
            "asserts": test.asserts,
            "failed_on_base": "yes" if card_run.card.category != "test-backfill" else
                              "n/a (backfill)",
            "status_at_head": "pass" if card_run.named_passing else "unknown",
            "frozen_hash": card_run.frozen_hash,
        })
    return rows


def _failed_attempts(card_run) -> list[str]:
    out = []
    for gate in card_run.gates:
        if gate.result in ("fail", "infra"):
            out.append(f"{gate.name} ({gate.result}): {gate.detail[:200]}")
    return out


def _not_checked(card_run) -> list[str]:
    out = ["doc-tests were not run: the suite uses --lib --bins --tests"]
    if card_run.cfg.coverage == "off":
        out.append("coverage is off")
    if card_run.cfg.mutation == "report":
        out.append("mutation is report-only for this category in the pilot")
    if card_run.cfg.counter_tests == "report":
        out.append("counter-tests are report-only in the first pilot batch")
    # A language pack may have had to drop something. Say so rather than let a
    # reader assume the gate ran the way it would elsewhere.
    for note in getattr(card_run.pack, "notes", []) or []:
        out.append(note)
    out.append("visual layout and behaviour across crates are not checked by any gate")
    return out


def _test_diff(card_run) -> str:
    from .edits import diff_text
    try:
        paths = [path for path in card_run._changed_since_base()
                 if card_run._is_test_path(path)]
        return diff_text(card_run.worktree, paths,
                         ref=card_run.base_commit or "HEAD")
    except Exception:  # noqa: BLE001
        return ""


def _production_diff(card_run) -> str:
    from .edits import diff_text
    try:
        paths = [path for path in card_run._changed_since_base()
                 if not card_run._is_test_path(path)]
        return diff_text(card_run.worktree, paths,
                         ref=card_run.base_commit or "HEAD")
    except Exception:  # noqa: BLE001
        return ""


def render_markdown(payload: dict) -> str:
    risk = payload["1_risk"]
    lines = [
        f"# Dossier: {payload['card']}",
        "",
        f"Run `{payload['run']}` · final state **{payload['final_state']}**"
        + (f" · stop code `{payload['stop_code']}`" if payload["stop_code"] else ""),
        "",
        "## 1. Risk",
        "",
        f"- Card hash at claim: `{risk['card_hash_at_claim']}`"
        + ("  **changed by the end**"
           if risk["card_changed"] else "  (unchanged)"),
        f"- Files changed: {risk['files_changed']}"
        f" (+{risk['lines_added']} / -{risk['lines_removed']})",
        f"- Touches check: {risk['touches_check']}",
        f"- Existing tests changed: "
        + (", ".join(risk["existing_tests_changed"]) or "no"),
        f"- Banned patterns: {risk['banned_patterns']}; "
        f"flagged: {len(risk['flagged_patterns'])}",
        f"- Files left unformatted: " + (", ".join(risk["unformatted"]) or "none"),
        f"- Catch-up: {risk['catch_up'] or 'not run'}",
        "",
        "## 2. Gate results",
        "",
        "| Gate | Result | Attempt | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for gate in payload["2_gates"]:
        detail = (gate["detail"] or "").replace("|", "\\|")[:200]
        lines.append(f"| {gate['name']} | {gate['result']} | {gate['attempt']} "
                     f"| {detail} |")

    lines += ["", "## 3. Known-failing tests left out", ""]
    excluded = payload["3_known_failing"]["excluded"]
    lines.append("\n".join(f"- {name}" for name in excluded) if excluded
                 else "None left out of the counts.")

    lines += ["", "## 4. Red-green table", "",
              "| Named test | File | Failed on base | At head | Frozen hash |",
              "| --- | --- | --- | --- | --- |"]
    for row in payload["4_red_green"]:
        lines.append(f"| {row['name']} | {row['file'] or '—'} | "
                     f"{row['failed_on_base']} | {row['status_at_head']} | "
                     f"`{row['frozen_hash'][:12]}` |")

    struggle = payload["5_struggle"]
    lines += ["", "## 5. Struggle line", "",
              f"- Attempts: {struggle['attempts']} implementer, "
              f"{struggle['test_writer_attempts']} test writer",
              f"- Planner split it: {'yes' if struggle['planner_split'] else 'no'}"]
    for failure in struggle["failed_attempts"]:
        lines.append(f"- {failure}")

    checker = payload["6_checker"]
    lines += ["", "## 6. Checker findings", "",
              f"- Restatement mismatch: {'**yes**' if checker['restatement_mismatch'] else 'no'}"]
    if checker["restatement"].get("text"):
        lines.append(f"  > {checker['restatement']['text'][:400]}")
    counters = checker["counter_tests"]
    lines.append(f"- Counter-tests: {counters['written']} written, "
                 f"{counters['dropped_uncompiled']} dropped (did not compile), "
                 f"{counters['passed']} passed, {counters['failed']} failed")

    mutation = payload["7_mutation"]
    lines += ["", "## 7. Mutation and coverage", "",
              f"- Killed {mutation.get('killed', 0)} of {mutation.get('total', 0)}"
              f"{'; hit its cap' if mutation.get('capped') else ''}",
              f"- Coverage: {'not measured' if 'coverage is off' in payload['8_not_checked'] else 'measured'}"]

    lines += ["", "## 8. Not checked", ""]
    lines += [f"- {item}" for item in payload["8_not_checked"]]

    lines += ["", "## 9. Diffs", "", "### Test diff", "", "```diff",
              payload["9_diffs"]["test_diff"].rstrip() or "(none)",
              "```", "", "### Production diff", "", "```diff",
              payload["9_diffs"]["production_diff"].rstrip() or "(none)", "```", ""]
    return "\n".join(lines)


def write_dossier(card_dir: Path, payload: dict) -> tuple[Path, Path]:
    card_dir = Path(card_dir)
    md_path = card_dir / "dossier.md"
    json_path = card_dir / "dossier.json"
    md_path.write_text(render_markdown(payload))
    write_json(json_path, payload)
    return md_path, json_path
