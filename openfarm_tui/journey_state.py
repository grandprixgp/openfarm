"""Which screen the app shows, worked out from the project's state and nothing else.

`gather` reads the facts, `screen_for` is the pure rule. The last screen the user
looked at is never an input: reopening the app lands where the project is.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

STEPS = ["Idea", "Questions", "Spec", "Plan", "Build", "Try it", "Accept"]
STEP_OF = {"idea": 0, "questions": 1, "spec": 2, "plan": 3, "build": 4, "try": 5, "accept": 6}
UNBUILT = ("ready", "backlog")


def screen_for(facts: dict) -> dict:
    """`{"screen", "flag"}` for a project's facts. `flag` is True when a task needs the user.

    Facts: `spec` (bool), `asking` (questions are open), `goal` (the current goal or
    None), `tasks` (the current goal's tasks, each with a `state`), `running` (a
    build is running), `questions` (builders' questions waiting), `try_needed`,
    `tried` (a try-it result for this code or None; `looks_right` once a person
    has said so).
    """
    if not facts.get("spec"):
        return {"screen": "questions" if facts.get("asking") else "idea", "flag": False}
    if facts.get("goal") is None:  # every goal is accepted
        return {"screen": "accept", "flag": False}
    states = [t["state"] for t in facts.get("tasks") or [] if t["state"] != "dropped"]
    if not states:
        return {"screen": "spec", "flag": False}
    needs_you = any(s in ("blocked", "in_review") for s in states) or bool(facts.get("questions"))
    if needs_you:
        return {"screen": "build", "flag": True}
    if facts.get("running") or any(s == "in_progress" for s in states):
        return {"screen": "build", "flag": False}
    if all(s == "done" for s in states):
        tried = facts.get("tried")
        # Passing the checks is not enough: a person looks and says it is right.
        if facts.get("try_needed") and not (tried and tried.get("looks_right")):
            return {"screen": "try", "flag": False}
        return {"screen": "accept", "flag": False}
    if all(s in UNBUILT for s in states):
        return {"screen": "plan", "flag": False}
    return {"screen": "build", "flag": False}  # some built, some still to build


def session_path(repo: Path) -> Path:
    """Where the idea and the questions so far are kept before the folder is a project."""
    from openfarm_core.projects import default_projects_path

    key = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
    return default_projects_path().parent / "idea-drafts" / f"{key}.json"


def load_session(repo: Path) -> dict:
    try:
        data = json.loads(session_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"history": list(data.get("history") or []), "pending": data.get("pending") or None,
            "stack": str(data.get("stack") or "auto"), "asking": bool(data.get("asking"))}


def save_session(repo: Path, session: dict) -> None:
    path = session_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")


def _task(card, cards_by_id: dict, state_dir: Path, header: dict) -> dict:
    from openfarm_core.blocked import records, triage

    state = str(card.state).lower().replace("-", "_")
    runs = records(state_dir, card.id)
    waits = [d for d in card.deps if str(getattr(cards_by_id.get(d), "state", "")).lower() != "done"]
    return {
        "id": card.id, "title": card.title, "state": state, "files": list(card.touches), "deps": list(card.deps),
        "waits": waits, "tries": len(runs),
        "parked": [str(r) for r in header.get("preflight") or []],
        "repairs": [str(r) for r in header.get("repairs") or []],
        "triage": triage(runs[-1], runs[:-1]) if runs and state == "blocked" else None,
        "last": runs[-1] if runs else None,
    }


def gather(repo: Path, config_path: Path | None = None) -> dict:
    """Read the facts `screen_for` needs, plus what the screens show. Never writes."""
    from openfarm_core.automode import load_project_chunks
    from openfarm_core.config import CONFIG_PATH, load_config
    from openfarm_core.daemon import daemon_status
    from openfarm_core.findings import list_findings
    from openfarm_core.store import read_doc, store_or_none
    from openfarm_core.tryit import how_to_start, last_result

    repo = Path(repo)
    session = load_session(repo)
    facts = {"repo": repo, "name": repo.name, "cfg": None, "spec": False, "spec_text": "", "goals": [],
             "goal": None, "goal_no": 0, "tasks": [], "all_tasks": [], "running": False, "questions": [],
             "try_needed": False, "try_plan": {}, "tried": None, "session": session,
             "asking": bool(session["asking"] and (session["pending"] or session["history"]))}
    cfg_file = Path(config_path) if config_path else repo / CONFIG_PATH
    if not cfg_file.is_file():
        return facts
    try:
        cfg = load_config(repo, cfg_file)
    except Exception:  # noqa: BLE001 - a broken config is shown as no project, with the palette's doctor to fix it
        return facts
    facts["cfg"] = cfg
    facts["name"] = cfg.name
    facts["spec_text"] = read_doc(repo, cfg, "spec").strip()
    facts["goals"] = load_project_chunks(repo)
    facts["spec"] = bool(facts["spec_text"] and facts["goals"])
    goal = next((g for g in facts["goals"] if g["status"] == "active"), None) or next(
        (g for g in facts["goals"] if g["status"] != "done"), None)
    facts["goal"] = goal
    facts["goal_no"] = 1 + facts["goals"].index(goal) if goal else 0
    store = store_or_none(repo, cfg)
    cards = [c for c in (store.cards() if store else []) if not c.id.upper().startswith("FIND")]
    by_id = {c.id: c for c in cards}
    facts["all_tasks"] = [dict(_task(c, by_id, cfg.state_dir, store.read(c.id)[0]), goal_no=int(c.fields.get("wave") or 1))
                          for c in sorted(cards, key=lambda c: c.id)]
    facts["tasks"] = [t for t in facts["all_tasks"] if t["goal_no"] == facts["goal_no"]]
    try:
        facts["running"] = bool(daemon_status(repo, config_path=config_path).get("running"))
    except Exception:  # noqa: BLE001 - no status means nothing is known to be running
        facts["running"] = False
    try:
        waiting = list_findings(repo, cfg, state_filter="waiting").get("findings") or []
    except Exception:  # noqa: BLE001
        waiting = []
    mine = {t["id"] for t in facts["tasks"]}
    facts["questions"] = [f for f in waiting if f.get("from_card") in mine and f.get("holds_up")]
    facts["try_plan"] = how_to_start(repo, cfg)
    facts["try_needed"] = facts["try_plan"].get("kind") == "web"
    if goal:
        facts["tried"] = last_result(repo, cfg, goal["id"])
    return facts
