"""Attempt Timeline & Phase-B Resume Inspection for Openfarm v0.13.0 (100% stdlib-only).

Reconstructs a card's run-by-run phase timeline (attempts, gates, stop codes, token
counts) from `<state_dir>/cards/<card>/<run>/` and identifies whether a card branch
already holds a frozen-tests commit eligible for Phase-B resume without re-running
Phase A (test-writer).
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import CONFIG_PATH, load_config
from .errors import ConfigError
from .git import Git


def find_resumable_frozen_commit(repo: Path, card_id: str) -> dict:
    """Check if an existing card branch already has a `freeze the tests` commit for Phase-B resume."""
    root = Path(repo).expanduser().resolve()
    if not (root / ".git").exists():
        return {"resumable": False, "branch": "", "commit": ""}
    git = Git(root)
    matches = git.branches(f"openfarm/{card_id}-*")
    if not matches:
        return {"resumable": False, "branch": "", "commit": ""}
    branch = matches[0]
    res = git._run(["log", "--format=%H %s", "-n", "12", branch], check=False)
    if res.returncode != 0:
        return {"resumable": False, "branch": branch, "commit": ""}
    for line in res.stdout.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and "freeze the tests" in parts[1].lower():
            return {"resumable": True, "branch": branch, "commit": parts[0]}
    return {"resumable": False, "branch": branch, "commit": ""}


def build_card_timeline(repo: Path, card_id: str, *, config_path: Path | None = None) -> dict:
    """Reconstruct attempt-by-attempt & gate-by-gate timeline for `card_id`."""
    root = Path(repo).expanduser().resolve()
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    if not cfg_p.is_file():
        return {"card_id": card_id, "runs": [], "resume": {"resumable": False}}
    try:
        cfg = load_config(root, cfg_p)
    except (ConfigError, OSError):
        return {"card_id": card_id, "runs": [], "resume": {"resumable": False}}

    card_dir = Path(cfg.state_dir) / "cards" / card_id
    runs: list[dict] = []
    if card_dir.is_dir():
        for run_dir in sorted(p for p in card_dir.iterdir() if p.is_dir()):
            rec_path = run_dir / "card-record.json"
            if not rec_path.is_file():
                continue
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            raw_attempts = rec.get("attempts")
            if isinstance(raw_attempts, list):
                att_dicts = [a for a in raw_attempts if isinstance(a, dict)]
            else:
                att_dicts = []
                att_dir = run_dir / "attempts"
                if att_dir.is_dir():
                    for p in sorted(att_dir.glob("*.json")):
                        try:
                            d = json.loads(p.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        if isinstance(d, dict):
                            att_dicts.append(d)
            attempts = [
                {
                    "role": str(a.get("role") or a.get("phase") or ""),
                    "attempt": int(a.get("attempt") or 0),
                    "model": str(a.get("model") or ""),
                    "effort": str(a.get("effort") or ""),
                    "tokens_in": int(a.get("tokens_in") or 0),
                    "tokens_out": int(a.get("tokens_out") or 0),
                    "ok": bool(
                        a["ok"]
                        if "ok" in a
                        else str(a.get("outcome") or a.get("status") or "").lower() in ("ok", "pass", "passed", "in-review", "in_review")
                    ),
                }
                for a in att_dicts
            ]
            raw_gates = rec.get("gates") if isinstance(rec.get("gates"), list) else []
            if not raw_gates and att_dicts:
                raw_gates = [g for g in (att_dicts[-1].get("gates") or []) if isinstance(g, dict)]
            gates = [
                {
                    "gate": str(g.get("gate") or g.get("name") or ""),
                    "result": str(g.get("result") or ""),
                    "detail": str(g.get("detail") or "")[:160],
                }
                for g in raw_gates
                if isinstance(g, dict)
            ]
            runs.append({
                "run_id": str(rec.get("run_id") or run_dir.name),
                "final_state": str(rec.get("final_state") or rec.get("state") or ""),
                "stop_code": str(rec.get("stop_code") or ""),
                "attempts": attempts,
                "gates": gates,
            })

    return {
        "card_id": card_id,
        "runs": runs,
        "resume": find_resumable_frozen_commit(root, card_id),
    }


__all__ = ["build_card_timeline", "find_resumable_frozen_commit"]
