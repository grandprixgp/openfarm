"""Bare `openfarm` Automode: context-aware landing state & command palette (v0.9.1).

Detects where a user is starting from (empty folder, unadopted repo, or adopted
openfarm repo), recommends the next step, computes live economics stats from
recent card records, and exposes both guided A->B flows and direct power tools.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import adopt
from .config import CONFIG_PATH, load_config
from .errors import ConfigError, Refused
from .state import StateDir

AUTOMODE_PALETTE: tuple[dict, ...] = (
    # Guided A->B flows
    {"id": "init", "group": "guided", "label": "New App (empty folder -> full app)", "command": "init"},
    {"id": "adopt", "group": "guided", "label": "Adopt existing repository", "command": "adopt"},
    {"id": "new-wave", "group": "guided", "label": "Plan & grill a new feature wave", "command": "supervise --prepare-queue"},
    {"id": "run", "group": "guided", "label": "Run ready cards", "command": "run"},
    {"id": "review", "group": "guided", "label": "Review in-review cards", "command": "review"},
    {"id": "land", "group": "guided", "label": "Local serial merge queue", "command": "land"},
    # Direct power tools
    {"id": "triage", "group": "power", "label": "Frontier card-complexity triage", "command": "triage"},
    {"id": "frontier-review", "group": "power", "label": "Mixture-of-frontiers review (card/diff/repo)", "command": "frontier-review"},
    {"id": "grill", "group": "power", "label": "Interactive design grill session", "command": "grill"},
    {"id": "stats", "group": "power", "label": "Economics & token-share statistics", "command": "status"},
    {"id": "dossiers", "group": "power", "label": "Inspect card dossiers", "command": "dossier"},
    {"id": "harness", "group": "power", "label": "Recursive harness memory", "command": "harness"},
    {"id": "acp", "group": "power", "label": "Provider OAuth & probe status", "command": "acp status"},
    {"id": "bench", "group": "power", "label": "Run efficacy bench", "command": "bench list"},
)


def _recent_economics(state: StateDir) -> dict:
    """Aggregate worker/supervisor token shares & fresh/cached counts from recent records."""
    worker_tokens = 0
    supervisor_tokens = 0
    tokens_in_fresh = 0
    tokens_in_cached = 0
    cards_seen = 0
    passed = 0
    if state.cards.is_dir():
        for rec_path in sorted(state.cards.glob("*/*/card-record.json"))[-50:]:
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            cards_seen += 1
            if rec.get("final_state") == "in-review":
                passed += 1
            worker_tokens += int(rec.get("tokens_in", 0) or 0) + int(rec.get("tokens_out", 0) or 0)
            supervisor_tokens += (
                int(rec.get("micromanager_tokens_in", 0) or 0)
                + int(rec.get("micromanager_tokens_out", 0) or 0)
            )
            tokens_in_fresh += int(rec.get("tokens_in_fresh", 0) or 0)
            tokens_in_cached += int(rec.get("tokens_in_cached", 0) or 0)
    total = worker_tokens + supervisor_tokens
    worker_share = round(worker_tokens / total, 4) if total > 0 else 0.0
    return {
        "cards_seen": cards_seen,
        "passed": passed,
        "total_tokens": total,
        "worker_tokens": worker_tokens,
        "supervisor_tokens": supervisor_tokens,
        "worker_share": worker_share,
        "tokens_in_fresh": tokens_in_fresh,
        "tokens_in_cached": tokens_in_cached,
    }


def detect_workspace_state(root: Path, config_path: Path | None = None) -> dict:
    """Detect whether `root` is empty, an unadopted repo, or an adopted openfarm repo."""
    root = Path(root).expanduser().resolve()
    cfg_file = Path(config_path) if config_path else (root / CONFIG_PATH)

    if cfg_file.is_file():
        counts = {"ready": 0, "in_progress": 0, "in_review": 0, "blocked": 0, "done": 0}
        economics: dict = {}
        try:
            from types import SimpleNamespace
            from .tui import _read_cards_readonly

            cfg = load_config(root, cfg_file)
            state = StateDir(cfg.state_dir)
            tui_ctx = SimpleNamespace(configured=True, repo=root, cfg=cfg, state=state)
            cards, _mode = _read_cards_readonly(tui_ctx)
            for c in cards:
                st = str(getattr(c, "state", "ready")).lower().replace("-", "_")
                if st in counts:
                    counts[st] += 1
            economics = _recent_economics(state)
        except (ConfigError, OSError):
            pass

        if counts["in_review"] > 0:
            next_action = "review"
            summary = f"{counts['in_review']} card(s) in-review — run `openfarm review` or `openfarm land`"
        elif counts["ready"] > 0:
            next_action = "run"
            summary = f"{counts['ready']} ready card(s) — run `openfarm triage` or `openfarm run`"
        elif counts["blocked"] > 0:
            next_action = "unblock"
            summary = f"{counts['blocked']} blocked card(s) — inspect dossiers or run `openfarm frontier-review`"
        else:
            next_action = "new-wave"
            summary = "Queue clear — start a new feature wave or run `openfarm triage`"
        return {
            "stage": "adopted",
            "root": str(root),
            "next_action": next_action,
            "summary": summary,
            "counts": counts,
            "economics": economics,
            "palette": list(AUTOMODE_PALETTE),
        }

    entries = [p for p in root.iterdir() if not p.name.startswith(".")] if root.is_dir() else []
    has_git = (root / ".git").exists()
    if not has_git and not entries:
        return {
            "stage": "empty",
            "root": str(root),
            "next_action": "init",
            "summary": "Empty directory — start a new app with Grill & Initialize (`openfarm init`)",
            "detected_language": "",
            "palette": list(AUTOMODE_PALETTE),
        }

    detected_lang = ""
    try:
        detected_lang = adopt.detect_language(root)
    except Refused:
        detected_lang = ""
    return {
        "stage": "unadopted",
        "root": str(root),
        "next_action": "adopt",
        "summary": (
            f"Existing repository ({detected_lang or 'unknown language'}) — adopt with `openfarm adopt`"
        ),
        "detected_language": detected_lang,
        "palette": list(AUTOMODE_PALETTE),
    }


def render_automode_summary(state: dict) -> str:
    """Compact human-readable Automode landing summary."""
    lines = [
        f"openfarm automode [{state.get('stage', 'unknown')}] — {state.get('root', '.')}",
        f"  Next: {state.get('summary', '')}",
    ]
    counts = state.get("counts")
    if isinstance(counts, dict):
        lines.append(
            f"  Queue: ready={counts.get('ready', 0)}  in-review={counts.get('in_review', 0)}  "
            f"blocked={counts.get('blocked', 0)}  done={counts.get('done', 0)}"
        )
    econ = state.get("economics")
    if isinstance(econ, dict) and econ.get("cards_seen"):
        lines.append(
            f"  Recent: {econ['passed']}/{econ['cards_seen']} passed  "
            f"tokens={econ['total_tokens']} (worker {econ['worker_share']:.0%}, "
            f"fresh={econ['tokens_in_fresh']} cached={econ['tokens_in_cached']})"
        )
    lines.append("  Palette: init | adopt | triage | run | review | land | frontier-review | grill | stats | acp")
    return "\n".join(lines)
