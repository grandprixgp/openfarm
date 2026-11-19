"""The reviewer dossier: one page, nine sections, in the spec's order.

The test diff comes first and the production diff second, so the reviewer checks the
contract before the code.
"""

from __future__ import annotations

from pathlib import Path

from .budget import describe_split, token_split
from .state import write_json


def build_dossier(card_run, final_state: str,
                  harness: dict | None = None) -> dict:
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
         "attempt": getattr(gate, "attempt", card_run.attempt),
         "detail": gate.detail,
         "output": str(gate.detail_path or "")}
        for gate in card_run.gates
    ]

    counter = dict(card_run.counter_summary)
    counter.setdefault("not_run", 0)
    mutation = dict(card_run.mutation_summary)
    restatement = (card_run.checker_restatement.as_dict()
                   if card_run.checker_restatement else {})
    pass1 = dict(getattr(card_run, "checker_pass1", {}) or {})
    pass2 = dict(getattr(card_run, "checker_pass2", {}) or {})
    adjudication = dict(getattr(card_run, "checker_adjudication", {}) or {})
    mm_summary = dict(getattr(card_run, "micromanager_summary", {}) or {})
    if not mm_summary and getattr(card_run, "micromanager", None) is not None:
        mm_budget = getattr(card_run.micromanager, "budget", None)
        if mm_budget is not None and hasattr(mm_budget, "as_dict"):
            mm_summary = {
                **mm_budget.as_dict(),
                "mode": getattr(card_run.micromanager, "mode", "off"),
                "reviewer_brief": getattr(card_run, "reviewer_brief", ""),
            }

    return {
        "schema_version": 1,
        "card": card_run.card.id,
        "run": card_run.run_id,
        "final_state": final_state,
        "stop_code": card_run.stop_code.value if card_run.stop_code else "",
        "stop_detail": (getattr(card_run, "detail", "") or "")[-2000:]
        if card_run.stop_code else "",
        "orchestrator": _orchestrator(),
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
            "banned_lines": banned.get("banned", []),
            "flagged_patterns": banned.get("flagged", []),
            "flagged_lines": banned.get("flagged", []),
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
            "tokens": {
                **token_split(
                    int(getattr(card_run.budget, "tokens_in", 0) or 0)
                    + int(getattr(card_run.budget, "tokens_out", 0) or 0),
                    int(mm_summary.get("tokens_in", 0) or 0)
                    + int(mm_summary.get("tokens_out", 0) or 0)),
                "tokens_in_fresh": (
                    int(getattr(card_run.budget, "tokens_in_fresh",
                                getattr(card_run.budget, "tokens_in", 0)) or 0)
                    + int(mm_summary.get("tokens_in_fresh",
                                         mm_summary.get("tokens_in", 0)) or 0)
                ),
                "tokens_in_cached": (
                    int(getattr(card_run.budget, "tokens_in_cached", 0) or 0)
                    + int(mm_summary.get("tokens_in_cached", 0) or 0)
                ),
            },
        },
        "6_checker": {
            "restatement": restatement,
            "restatement_mismatch": bool(restatement.get("mismatch")),
            "pass1": pass1,
            "counter_tests": counter,
            "pass2": pass2,
            "said": checker_said(pass2, counter),
            "checker_adjudication": adjudication,
            "micromanager": mm_summary,
        },
        "7_mutation": mutation,
        "8_not_checked": _not_checked(card_run),
        "9_diffs": {
            "test_diff": _test_diff(card_run),
            "production_diff": _production_diff(card_run),
        },
        "harness": _harness_summary(card_run, harness),
        "team": list(
            card_run.team_summary()
            if hasattr(card_run, "team_summary")
            else (getattr(card_run, "team", []) or [])
        ),
        "sets": list(
            card_run.board_sets_summary()
            if hasattr(card_run, "board_sets_summary")
            else (getattr(card_run, "team_sets", []) or [])
        ),
        "handbacks": list(getattr(card_run, "handbacks", []) or []),
        "board_path": (
            str(card_run.board.board_dir)
            if getattr(card_run, "board", None) is not None
            else ""
        ),
    }


def _harness_summary(card_run, harness: dict | None) -> dict:
    raw = harness if harness is not None else getattr(card_run, "harness_summary", None)
    if not isinstance(raw, dict) or not raw:
        return {}
    metrics = raw.get("metrics") or {}
    efficiency_score = raw.get("efficiency_score")
    if efficiency_score is None and isinstance(metrics, dict):
        efficiency_score = metrics.get(
            "harness_efficiency", metrics.get("efficiency_score", 0.0)
        )
    probe_tr = raw.get("probe_transition")
    probe_val: dict | str
    if isinstance(probe_tr, dict):
        probe_val = dict(probe_tr)
    else:
        probe_val = str(probe_tr or "")
    return {
        "revision": int(raw.get("revision", 0) or 0),
        "state": str(raw.get("state", "") or ""),
        "cues_applied": list(raw.get("cues_applied") or []),
        "lessons_added": list(raw.get("lessons_added") or []),
        "probe_transition": probe_val,
        "efficiency_score": float(efficiency_score or 0.0),
        "skipped_reason": str(raw.get("skipped_reason", "") or ""),
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
    failed_map = getattr(card_run, "failed_on_base_by_test", {}) or {}
    head_map = getattr(card_run, "head_status_by_test", {}) or {}
    rows = []
    for test in card_run.card.tests:
        if card_run.card.category == "test-backfill":
            failed_on_base = "n/a (backfill)"
        elif test.name in failed_map:
            status = failed_map[test.name]
            failed_on_base = "yes" if status == "FAILED" else f"no ({status})"
        else:
            failed_on_base = "unknown"

        if test.name in head_map:
            head_status = head_map[test.name]
            status_at_head = "pass" if head_status == "ok" else head_status
        else:
            status_at_head = "unknown"

        rows.append({
            "name": test.name,
            "file": test.file,
            "asserts": test.asserts,
            "failed_on_base": failed_on_base,
            "status_at_head": status_at_head,
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
    out = []
    if str(getattr(card_run.pack, "name", "rust")) == "rust":
        out.append("doc-tests were not run: the suite uses --lib --bins --tests")
    if card_run.cfg.coverage == "off":
        out.append("coverage is off")
    required_for = getattr(card_run.cfg, "mutation_required_for",
                           ("test-backfill",))
    if card_run.cfg.mutation == "off":
        out.append("mutation testing is off")
    elif (card_run.cfg.mutation == "report"
          and card_run.card.category not in required_for):
        out.append("mutation is report-only for this category")
    if card_run.cfg.counter_tests == "report":
        out.append("counter-tests are report-only")
    pass1 = getattr(card_run, "checker_pass1", None) or {}
    if pass1 and not pass1.get("readable", True):
        out.append("the checker's first pass could not be read, so its restatement "
                   "was not compared with the tests")
    pass2 = getattr(card_run, "checker_pass2", None) or {}
    if pass2 and not pass2.get("readable", True):
        out.append("the checker's second pass could not be read, so no counter-test "
                   "ran and nothing was checked by it")
    # A language pack may have had to drop something. Say so rather than let a
    # reader assume the gate ran the way it would elsewhere.
    for note in getattr(card_run.pack, "notes", []) or []:
        out.append(note)
    out.append("visual layout and behaviour across units are not checked by any gate")
    return out


def checker_said(pass2: dict, counter: dict) -> str:
    """One line on what the checker's second pass concluded, for the dossier's top.

    The verdict is a model's claim and decides nothing; the counter-tests that
    failed on the diff are what count, so the line always says both.
    """
    if not pass2:
        return ""
    if not pass2.get("readable", True):
        return ("could not be read, even when asked again "
                f"({pass2.get('problem') or 'no answer in the form'}): no counter-test "
                "ran, nothing was checked")
    failed = int(counter.get("failed", 0) or 0)
    passed = int(counter.get("passed", 0) or 0)
    dropped = (int(counter.get("dropped_uncompiled", 0) or 0)
               + int(counter.get("not_run", 0) or 0))
    verdict = pass2.get("verdict", "")
    if verdict == "changes-requested":
        head = "changes requested"
        if not failed:
            head += ", but no counter-test fails on the diff"
    elif verdict == "accept":
        head = "accepts the diff"
    else:
        head = "no verdict stated (old answer form)"
    tests = []
    if failed:
        tests.append(f"{failed} failed on the diff")
    if passed:
        tests.append(f"{passed} passed")
    if dropped:
        tests.append(f"{dropped} dropped")
    return head + "; counter-tests: " + (", ".join(tests) if tests else "none written")


def _quoted_block(text: str, limit: int) -> list[str]:
    """Model text shown as text, never as markdown the dossier would render."""
    body = (text or "").replace("```", "` ` `").strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n[... cut]"
    return ["  ```text", *("  " + line for line in body.splitlines()), "  ```"]


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


def _orchestrator() -> dict:
    """Which openfarm judged this card. Never fails the dossier."""
    try:
        from .selfhost import cached_identity
        return cached_identity()
    except Exception:  # a dossier must never fail the card
        return {}


def render_markdown(payload: dict) -> str:
    risk = payload["1_risk"]
    lines = [
        f"# Dossier: {payload['card']}",
        "",
        f"Run `{payload['run']}` · final state **{payload['final_state']}**"
        + (f" · stop code `{payload['stop_code']}`" if payload["stop_code"] else ""),
        "",
    ]
    if payload.get("stop_detail"):
        lines += [f"Why it stopped: {payload['stop_detail']}", ""]
    said = (payload.get("6_checker") or {}).get("said", "")
    if said:
        lines += [f"**Checker, second pass: {said}.** Details in section 6.", ""]
    if payload.get("orchestrator"):
        from .selfhost import describe
        lines += [f"Judged by {describe(payload['orchestrator'])}", ""]
    lines += [
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
        f"- Banned patterns: {risk['banned_patterns']}",
        f"- Flagged: {len(risk['flagged_patterns'])}",
        f"- Files left unformatted: " + (", ".join(risk["unformatted"]) or "none"),
        f"- Catch-up: {risk['catch_up'] or 'not run'}",
        "",
        "## 2. Gate results",
        "",
        "| Gate | Result | Attempt | Detail | Output |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in payload["2_gates"]:
        detail = (gate["detail"] or "").replace("|", "\\|")[:200]
        output = (gate.get("output") or "").replace("|", "\\|")
        lines.append(f"| {gate['name']} | {gate['result']} | {gate['attempt']} "
                     f"| {detail} | {output} |")

    banned = risk.get("banned_lines") or []
    flagged = risk.get("flagged_lines") or []
    if banned or flagged:
        lines += ["", "### Patterns, with their lines", ""]
        for entry in banned:
            lines.append(f"- **{entry.get('pattern')}** "
                         f"{entry.get('file')}:{entry.get('line')} "
                         f"`{(entry.get('text') or '').strip()[:160]}`")
        for entry in flagged:
            lines.append(f"- flagged `{entry.get('pattern')}` "
                         f"{entry.get('file')}:{entry.get('line')}")

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
    if struggle.get("tokens"):
        lines.append(f"- Tokens: {describe_split(struggle['tokens'])}")
    for failure in struggle["failed_attempts"]:
        lines.append(f"- {failure}")

    checker = payload["6_checker"]
    lines += ["", "## 6. Checker findings", "",
              f"- Restatement mismatch: {'**yes**' if checker['restatement_mismatch'] else 'no'}"]
    if checker["restatement"].get("text"):
        lines.append(f"  > {checker['restatement']['text'][:400]}")
    pass1 = checker.get("pass1") or {}
    if pass1:
        if not pass1.get("readable", True):
            lines.append("- First pass: **could not be read**, even when asked again "
                         f"({pass1.get('problem', '')}). No restatement was compared "
                         "with the tests.")
        else:
            catches = {"yes": "yes", "partly": "partly", "no": "**no**"}.get(
                pass1.get("catches", ""), "not stated")
            lines.append("- First pass, would the tests catch a bad implementation: "
                         f"{catches}")
        if pass1.get("asked_again"):
            lines.append("- First pass asked again once: the first answer could not "
                         f"be read ({pass1.get('first_problem', '')})")
        if pass1.get("misses"):
            lines.append("- What the first pass says the tests miss (a model's "
                         "opinion; it blocks nothing):")
            lines += _quoted_block(pass1["misses"], 1500)
    counters = checker["counter_tests"]
    pass2 = checker.get("pass2") or {}
    if pass2:
        if not pass2.get("readable", True):
            lines.append("- Second pass: **could not be read**, even when asked again "
                         f"({pass2.get('problem', '')}). No counter-test ran; the "
                         "checker checked nothing.")
        else:
            verdict = {"accept": "accepts the diff",
                       "changes-requested": "**changes requested**"}.get(
                           pass2.get("verdict", ""), "no verdict stated (old answer form)")
            lines.append(f"- Second pass verdict: {verdict}")
        if pass2.get("asked_again"):
            lines.append("- Asked again once: the first answer could not be read "
                         f"({pass2.get('first_problem', '')})")
        if pass2.get("reasons"):
            lines.append("- Reasons:")
            lines += _quoted_block(pass2["reasons"], 1500)
    lines.append(f"- Counter-tests: {counters['written']} written, "
                 f"{counters['dropped_uncompiled']} dropped (did not compile), "
                 f"{counters['passed']} passed, {counters['failed']} failed, "
                 f"{counters.get('not_run', 0)} not-run")
    for result in pass2.get("counter_results") or []:
        name = result.get("name") or "(no name)"
        where = f" ({result['file']})" if result.get("file") else ""
        outcome = result.get("outcome", "")
        if outcome == "failed":
            lines.append(f"  - `{name}`{where}: **failed on the diff**. First lines "
                         "of its output:")
            lines += ["  " + line for line in
                      _quoted_block(result.get("output_head", ""), 1500)]
        elif outcome == "passed":
            lines.append(f"  - `{name}`{where}: passed")
        else:
            lines.append(f"  - `{name}`{where}: {outcome or 'dropped'}, "
                         f"{result.get('why', '')}")
            if result.get("output_head"):
                lines += ["  " + line for line in
                          _quoted_block(result["output_head"], 800)]
    if pass2.get("readable", False):
        for key, title in (("traceability", "Traceability"),
                           ("edge_cases", "Traced edge cases")):
            if pass2.get(key):
                lines.append(f"- {title}:")
                lines += _quoted_block(pass2[key], 2500)
            elif key.replace("_", " ") in (pass2.get("missing") or []):
                lines.append(f"- {title}: not given")
    adj = checker.get("checker_adjudication") or {}
    if adj:
        lines.append(f"- Checker adjudication: {adj.get('verdict', '')} — "
                     f"{adj.get('rationale', '')[:200]}")
    mm = checker.get("micromanager") or {}
    if mm and (mm.get("calls", 0) or mm.get("reviewer_brief")):
        lines.append(
            f"- Micromanager: mode={mm.get('mode', 'off')}, "
            f"calls={mm.get('calls', 0)}, "
            f"spend={mm.get('spend', 0.0):.4f}, "
            f"false_rejects_dismissed={mm.get('false_rejects_dismissed', 0)}"
        )
        if mm.get("reviewer_brief"):
            lines.append(f"  > {mm['reviewer_brief'][:400]}")

    mutation = payload["7_mutation"]
    lines += ["", "## 7. Mutation and coverage", "",
              f"- Killed {mutation.get('killed', 0)} of {mutation.get('total', 0)}"
              f"{'; hit its cap' if mutation.get('capped') else ''}",
              f"- Survived: {mutation.get('survived', 0)}",
              "- Coverage: not measured (no coverage gate is implemented)"]

    lines += ["", "## 8. Not checked", ""]
    lines += [f"- {item}" for item in payload["8_not_checked"]]

    lines += ["", "## 9. Diffs", "", "### Test diff", "", "```diff",
              payload["9_diffs"]["test_diff"].rstrip() or "(none)",
              "```", "", "### Production diff", "", "```diff",
              payload["9_diffs"]["production_diff"].rstrip() or "(none)", "```", ""]

    harness = payload.get("harness") or {}
    if harness:
        cues = ", ".join(harness.get("cues_applied") or []) or "(none)"
        lessons = ", ".join(harness.get("lessons_added") or []) or "(none)"
        probe = harness.get("probe_transition")
        if isinstance(probe, dict):
            probe_text = str(probe.get("action") or probe)
        else:
            probe_text = str(probe or "none")
        lines += [
            "## Harness efficiency",
            "",
            f"- Revision: {harness.get('revision', 0)} ({harness.get('state', '') or 'n/a'})",
            f"- Cues applied: {cues}",
            f"- Lessons added: {lessons}",
            f"- Probe transition: {probe_text}",
            f"- Efficiency score: {float(harness.get('efficiency_score', 0.0) or 0.0):.4f}",
            "",
        ]
    team = payload.get("team") or []
    sets = payload.get("sets") or []
    handbacks = payload.get("handbacks") or []
    board_path = str(payload.get("board_path", "") or "")
    if team or sets or handbacks or board_path:
        lines += ["## Team and board", ""]
        if board_path:
            lines.append(f"- Board: `{board_path}`")
        if team:
            lines += [
                "",
                "| Member | Role | Tier | Effort | Attempts | Tokens |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for row in team:
                lines.append(
                    f"| {row.get('member', '')} | {row.get('role', '')} | "
                    f"{row.get('tier', '')} | {row.get('effort', '') or 'default'} | "
                    f"{row.get('attempts', 0)} | {row.get('tokens', 0)} |"
                )
        if sets:
            lines += ["", "### Tier and effort changes", ""]
            for entry in sets:
                if isinstance(entry, dict):
                    lines.append(f"- {entry.get('detail') or entry}")
                else:
                    lines.append(f"- {entry}")
        if handbacks:
            lines += ["", "### Hand-backs", ""]
            for hb in handbacks:
                if isinstance(hb, dict):
                    lines.append(
                        f"- {hb.get('role', 'worker')} (attempt {hb.get('attempt', 0)}): "
                        f"{hb.get('reason', '')}"
                    )
                else:
                    lines.append(f"- {hb}")
        lines.append("")
    return "\n".join(lines)


def write_dossier(card_dir: Path, payload: dict) -> tuple[Path, Path]:
    card_dir = Path(card_dir)
    md_path = card_dir / "dossier.md"
    json_path = card_dir / "dossier.json"
    md_path.write_text(render_markdown(payload))
    write_json(json_path, payload)
    return md_path, json_path
