"""Performance-Aware Adaptive Model Router for Openfarm v0.13.0 (100% stdlib-only).

Aggregates historical card outcomes, attempts, token splits, and verdicts from
`<state_dir>/cards/**` by `(language, category)` and recommends the lowest-cost
Worker model + starting effort + supervision profile with high empirical first-try
success rate.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import CONFIG_PATH, load_config
from .errors import ConfigError


def _load_attempt_records(run_dir: Path, rec: dict) -> list[dict]:
    """Load attempt dicts from either `attempts/*.json` (live runs) or inline list (fixtures)."""
    raw_attempts = rec.get("attempts")
    if isinstance(raw_attempts, list):
        return [a for a in raw_attempts if isinstance(a, dict)]
    att_dir = run_dir / "attempts"
    out: list[dict] = []
    if att_dir.is_dir():
        for p in sorted(att_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                out.append(data)
    return out


def compute_routing_recommendations(repo: Path, *, config_path: Path | None = None) -> dict:
    """Analyze `<state_dir>/cards/**` records and recommend optimal model/effort/profile per category."""
    root = Path(repo).expanduser().resolve()
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    if not cfg_p.is_file():
        return {"samples": 0, "recommendations": [], "note": "repository not configured"}
    try:
        cfg = load_config(root, cfg_p)
    except (ConfigError, OSError) as exc:
        return {"samples": 0, "recommendations": [], "note": str(exc)}

    cards_root = Path(cfg.state_dir) / "cards"
    if not cards_root.is_dir():
        return {"samples": 0, "recommendations": [], "note": "no completed runs recorded yet"}

    buckets: dict[tuple[str, str], dict] = {}
    samples = 0
    default_lang = (cfg.languages[0] if cfg.languages else "unknown").lower()

    for rec_path in sorted(cards_root.glob("*/*/card-record.json")):
        try:
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        samples += 1
        tooling = rec.get("tooling") if isinstance(rec.get("tooling"), dict) else {}
        lang = str(rec.get("language") or tooling.get("pack") or default_lang).lower()
        cat = str(rec.get("category") or "small-feature").lower()
        final_state = str(rec.get("final_state") or rec.get("state") or "").lower().replace("-", "_")
        passed = final_state in ("in_review", "done")
        attempts = _load_attempt_records(rec_path.parent, rec)
        impl_attempts = [a for a in attempts if str(a.get("role") or "") == "implementer"]
        if impl_attempts:
            first_try = passed and len(impl_attempts) <= 1
        else:
            raw_n = rec.get("attempts")
            first_try = passed and (int(raw_n) <= 1 if isinstance(raw_n, int) else True)
        model_src = impl_attempts or attempts
        models = [str(a.get("model") or "") for a in model_src if str(a.get("model") or "").strip()]
        efforts = [str(a.get("effort") or "") for a in model_src if str(a.get("effort") or "").strip()]

        key = (lang, cat)
        b = buckets.setdefault(key, {
            "language": lang,
            "category": cat,
            "total": 0,
            "passed": 0,
            "first_try": 0,
            "models": {},
            "efforts": {},
        })
        b["total"] += 1
        if passed:
            b["passed"] += 1
        if first_try:
            b["first_try"] += 1
        for m in models:
            b["models"][m] = b["models"].get(m, 0) + (2 if passed else 1)
        for e in efforts:
            b["efforts"][e] = b["efforts"].get(e, 0) + (2 if passed else 1)

    recs: list[dict] = []
    for (lang, cat), b in sorted(buckets.items()):
        total = max(1, b["total"])
        pass_rate = round(b["passed"] / total, 3)
        first_try_rate = round(b["first_try"] / total, 3)
        top_model = max(b["models"].items(), key=lambda kv: kv[1])[0] if b["models"] else cfg.role_models["implementer"].model
        if first_try_rate >= 0.75:
            effort = "medium"
            profile = "lean"
        elif pass_rate >= 0.6:
            effort = "high"
            profile = "balanced"
        else:
            effort = "max"
            profile = "strict"
        confidence = round(min(1.0, total / 5.0), 2)
        recs.append({
            "language": lang,
            "category": cat,
            "samples": total,
            "pass_rate": pass_rate,
            "first_try_rate": first_try_rate,
            "recommended_model": top_model,
            "recommended_effort": effort,
            "recommended_profile": profile,
            "confidence": confidence,
        })

    return {
        "samples": samples,
        "recommendations": recs,
        "note": "" if samples else "no card records found yet",
    }


__all__ = ["compute_routing_recommendations"]
