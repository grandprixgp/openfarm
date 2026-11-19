"""Frontier card-complexity triage (v0.9.1).

Scores cards (`low | medium | high | split`) either deterministically via
heuristics or in a single batched frontier call, recommending supervision
profile (`lean | balanced | strict`), starting worker effort, and whether a
card should be split before spending worker tokens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .cards import Card
from .config import SUPERVISION_PROFILES

COMPLEXITY_LEVELS = ("low", "medium", "high", "split")
_HIGH_RISK_CATEGORIES = ("feature", "refactor", "integration", "port")
_EFFORTS = ("medium", "high", "max")


def triage_card_heuristic(card: Card) -> dict:
    """Fast, deterministic complexity score when no frontier model is called."""
    reasons: list[str] = []
    score = 0
    touches = len(card.touches or [])
    if touches >= 5:
        score += 5
        reasons.append(f"touches {touches} files")
    elif touches >= 4:
        score += 3
        reasons.append(f"touches {touches} files")
    elif touches >= 2:
        score += 1
        reasons.append(f"touches {touches} files")

    fields = getattr(card, "fields", {}) or {}
    cat = (card.category or str(fields.get("Category") or fields.get("category") or "")).lower()
    new_sym = bool(card.new_symbols or str(fields.get("New symbols") or "").lower() in ("yes", "true"))
    type_chg = bool(card.type_changes or str(fields.get("Type changes") or "").lower() in ("yes", "true"))
    if new_sym:
        score += 1
        reasons.append("declares new symbols")
    if type_chg:
        score += 1
        reasons.append("changes types")
    if cat in _HIGH_RISK_CATEGORIES:
        score += 2
        reasons.append(f"category={cat}")
    if len(card.done_text or "") > 400:
        score += 1
        reasons.append("long Done specification")

    if score >= 5 or (touches >= 4 and (new_sym or type_chg)):
        complexity = "split"
        profile = "strict"
        effort = "high"
        split = True
    elif score >= 3:
        complexity = "high"
        profile = "strict"
        effort = "high"
        split = False
    elif score >= 1:
        complexity = "medium"
        profile = "balanced"
        effort = "medium"
        split = False
    else:
        complexity = "low"
        profile = "lean"
        effort = "medium"
        split = False

    if not reasons:
        reasons.append("small single-file bounded card")

    return {
        "card": card.id,
        "title": card.title,
        "complexity": complexity,
        "recommended_profile": profile,
        "starting_effort": effort,
        "split_suggested": split,
        "reasons": reasons,
        "source": "heuristic",
    }


def _extract_json_array(text: str) -> list | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except ValueError:
        pass
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    return None


def build_triage_prompt(cards: list[Card]) -> str:
    """Single batched prompt asking a frontier model to triage all cards at once."""
    items = []
    for c in cards:
        items.append({
            "card": c.id,
            "title": c.title,
            "category": c.category,
            "unit": c.unit,
            "touches": list(c.touches or []),
            "new_symbols": bool(c.new_symbols),
            "type_changes": bool(c.type_changes),
            "done": (c.done_text or "")[:600],
        })
    return (
        "Score each openfarm card's implementation complexity in one JSON array.\n"
        "Return ONLY a JSON array of objects with keys:\n"
        '  card (string), complexity ("low"|"medium"|"high"|"split"),\n'
        '  recommended_profile ("lean"|"balanced"|"strict"),\n'
        '  starting_effort ("medium"|"high"|"max"), reasons (list of short strings).\n'
        "Use `split` when a card is really 2+ independent cards.\n\n"
        "CARDS:\n" + json.dumps(items, indent=2)
    )


def triage_cards_with_frontier(cards: list[Card], backend=None) -> list[dict]:
    """Triage `cards` in one batched frontier call, falling back to heuristics per card."""
    if not cards:
        return []
    if backend is None:
        return [triage_card_heuristic(c) for c in cards]

    prompt = build_triage_prompt(cards)
    raw = ""
    if hasattr(backend, "ask"):
        raw = str(backend.ask(prompt) or "")
    elif callable(backend):
        raw = str(backend(prompt) or "")
    parsed = _extract_json_array(raw) or []
    by_id: dict[str, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("card") or "")
        comp = str(item.get("complexity") or "").lower()
        prof = str(item.get("recommended_profile") or "").lower()
        eff = str(item.get("starting_effort") or "medium").lower()
        if cid and comp in COMPLEXITY_LEVELS and prof in SUPERVISION_PROFILES:
            if eff not in _EFFORTS:
                eff = "high" if comp in ("high", "split") else "medium"
            reasons = item.get("reasons")
            by_id[cid] = {
                "card": cid,
                "complexity": comp,
                "recommended_profile": prof,
                "starting_effort": eff,
                "split_suggested": comp == "split",
                "reasons": [str(r) for r in reasons] if isinstance(reasons, list) else [],
                "source": "frontier",
            }

    out: list[dict] = []
    for c in cards:
        if c.id in by_id:
            entry = dict(by_id[c.id])
            entry["title"] = c.title
            out.append(entry)
        else:
            out.append(triage_card_heuristic(c))
    return out


def apply_triage_to_card_file(card_path: Path, entry: dict) -> bool:
    """Record `Complexity:` and `Profile:` lines in a card markdown file."""
    path = Path(card_path)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    comp = str(entry.get("complexity") or "")
    prof = str(entry.get("recommended_profile") or "")
    if not comp or not prof:
        return False
    for key, val in (("Complexity", comp), ("Profile", prof)):
        pat = re.compile(rf"^(?P<k>{re.escape(key)}\s*:\s*).*$", re.MULTILINE)
        if pat.search(text):
            text = pat.sub(rf"\g<k>{val}", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n{key}: {val}\n"
    path.write_text(text, encoding="utf-8")
    return True
