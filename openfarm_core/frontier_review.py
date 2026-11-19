"""Mixture-of-frontiers review across multiple frontier providers (v0.9.1).

Runs multiple frontier providers over a card spec, a diff, or a repository
snapshot and aggregates their verdicts, findings, and disagreements into one
structured report.
"""

from __future__ import annotations

import json

SCOPES = ("card", "diff", "repo")
VERDICTS = ("approve", "minor-fix", "changes-requested", "reject")


def _extract_json_dict(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return None


def build_frontier_review_prompt(scope: str, target_text: str) -> str:
    return (
        f"You are performing a senior engineering review (scope={scope}).\n"
        "Return ONLY a JSON object with keys:\n"
        '  verdict ("approve"|"minor-fix"|"changes-requested"|"reject"),\n'
        "  summary (short string), findings (list of concrete issue strings).\n\n"
        f"--- BEGIN {scope.upper()} DATA ---\n"
        f"{target_text[:24000]}\n"
        f"--- END {scope.upper()} DATA ---\n"
    )


def review_with_frontiers(
    scope: str,
    target_text: str,
    providers: list[str],
    *,
    backends: dict | None = None,
) -> dict:
    """Run each named provider backend on `target_text` and aggregate findings."""
    if scope not in SCOPES:
        raise ValueError(f"scope {scope!r} must be one of {SCOPES!r}")
    prompt = build_frontier_review_prompt(scope, target_text)
    reviews: list[dict] = []
    all_findings: list[dict] = []
    backend_map = backends or {}

    for prov in providers:
        backend = backend_map.get(prov)
        if backend is None:
            reviews.append({
                "provider": prov,
                "verdict": "unavailable",
                "summary": f"provider {prov!r} not configured",
                "findings": [],
            })
            continue
        raw = str(backend.ask(prompt) if hasattr(backend, "ask") else backend(prompt))
        parsed = _extract_json_dict(raw) or {}
        verdict = str(parsed.get("verdict") or "changes-requested").lower()
        if verdict not in VERDICTS:
            verdict = "changes-requested"
        summary = str(parsed.get("summary") or raw.strip()[:200])
        findings = [str(f) for f in (parsed.get("findings") or []) if str(f).strip()]
        reviews.append({
            "provider": prov,
            "verdict": verdict,
            "summary": summary,
            "findings": findings,
        })
        for f in findings:
            all_findings.append({"provider": prov, "finding": f})

    valid_verdicts = {r["verdict"] for r in reviews if r["verdict"] in VERDICTS}
    consensus = len(valid_verdicts) == 1
    agreed_verdict = next(iter(valid_verdicts)) if consensus else "disagreed"
    return {
        "scope": scope,
        "providers": list(providers),
        "consensus": consensus,
        "agreed_verdict": agreed_verdict,
        "reviews": reviews,
        "all_findings": all_findings,
    }
