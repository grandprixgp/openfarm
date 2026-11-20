"""Why a task stopped, in one of three kinds, each with the action that fits it.

- `task`: the task itself is wrong (it can't be built as written). Main action:
  apply a fix to the task and retry.
- `builder`: the task is sound and the builder fell short. Main action: retry.
- `check`: one check disagrees with the others and may be wrong. Main action:
  run that check by hand and look at its raw output.

"Release" alone is never the main action: the same task with the same scaffold
fails the same way again.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import tempfile
from pathlib import Path

KINDS = {
    "task": {"title": "The task is wrong", "main": "fix", "others": ["edit", "skip"]},
    "builder": {"title": "The builder fell short", "main": "retry", "others": ["edit", "skip"]},
    "check": {"title": "A check may be wrong", "main": "run-check", "others": ["accept", "retry"]},
}
ACTIONS = {
    "fix": "Apply fix and retry",
    "retry": "Retry",
    "run-check": "Run it myself",
    "edit": "Edit task",
    "skip": "Skip this task",
    "accept": "Accept anyway",
}

_TASK_CODES = {"ineligible", "refused", "cannot-split", "card-changed"}
_CHECK_CODES = {"tests-unprovable"}
_TASK_WORDS = ("card.touches", "touches", "scaffold", "left the allowed paths", "cannot be completed as scoped",
               "shape", "not eligible", "parameter default")
_PLAIN = {
    "ineligible": "The task can't be built as written",
    "refused": "The builder was not allowed to do what the task needs",
    "cannot-split": "The task is too big and could not be split",
    "card-changed": "The task's text changed while it was being built",
    "tests-unprovable": "The check that the tests really test the new code disagreed with the other checks",
    "attempts": "The builder ran out of tries",
    "no-progress": "The builder stopped making progress",
    "checker-reject": "The reviewer turned the work down",
    "supervisor-reject": "The reviewer turned the work down",
    "extra-gate": "One of the project's own checks failed",
    "build-cap": "The build limit was reached",
    "budget": "The spending limit was reached",
    "infra": "Something outside the task failed (a timeout or a lost connection)",
    "lease-expired": "The builder went quiet for too long",
    "rebase-conflict": "The work no longer fits on top of the newest code",
    "stale-conflict": "The work no longer fits on top of the newest code",
}


#: What the build refuses in new code, said to whoever writes a task so the task doesn't ask for it.
BUILD_RULES = (
    "The build refuses new code that calls the network directly (a bare `fetch(` call, `XMLHttpRequest`, `WebSocket`), "
    "starts a process, or runs code built at run time (`eval`, `Function(`, a computed `import(`), in any file, browser "
    "code and code inside strings included. A task that needs the network says how: the function is passed in or held "
    "in a value first (`const net = globalThis.fetch; await net(url)`), never called by its bare name."
)


def _same_way(record: dict, earlier: list[dict]) -> bool:
    key = (record.get("stop_code"), str(record.get("detail") or "")[:160])
    return any((r.get("stop_code"), str(r.get("detail") or "")[:160]) == key for r in earlier)


def triage(record: dict, earlier: list[dict] | None = None) -> dict:
    """Sort one stopped run into a kind.

    `record` is the run's `card-record.json`; `earlier` holds the same task's
    previous records. Returns `{"kind", "title", "why", "main", "main_label",
    "others": [{"id", "label"}], "repeated"}`. `why` is the builder's own
    diagnosis when there is one.
    """
    code = str(record.get("stop_code") or "")
    detail = str(record.get("detail") or "").strip()
    low = detail.lower()
    counter = record.get("counter_tests") or {}
    repeated = _same_way(record, list(earlier or []))
    # The reviewer's answer was lost (cut off, unreadable): the work was never judged, so no blame falls on it.
    unjudged = bool(counter.get("unreadable")) or "could not be read" in low
    if code in _CHECK_CODES or unjudged or (counter.get("failed") and code in ("checker-reject", "attempts")):
        kind = "check"
    elif code in _TASK_CODES or any(word in low for word in _TASK_WORDS) or repeated:
        kind = "task"
    else:
        kind = "builder"
    spec = KINDS[kind]
    plain = ("The reviewer's answer could not be read, so the work was never judged" if unjudged
             else _PLAIN.get(code, "The task stopped"))
    if repeated and kind == "task" and code not in _TASK_CODES:
        plain += ", the same way as last time"
    return {
        "kind": kind,
        "title": spec["title"],
        "why": f"{plain}. {detail}" if detail else f"{plain}.",
        "main": spec["main"],
        "main_label": ACTIONS[spec["main"]],
        "others": [{"id": a, "label": ACTIONS[a]} for a in spec["others"]],
        "repeated": repeated,
    }


def records(state_dir: Path, card_id: str) -> list[dict]:
    """Every run record kept for a task, oldest first (released tries are under `partial/`)."""
    folder = Path(state_dir) / "cards" / card_id
    found = []
    for path in [*folder.glob("run-*/card-record.json"), *folder.glob("partial/*/card-record.json")]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found.append(data)
    return sorted(found, key=lambda r: str(r.get("claimed_at") or ""))


_RULE_WORDS = {
    "network-call": "the code calls the network directly",
    "process-call": "the code starts another program",
    "dynamic-exec": "the code builds and runs code at run time",
    "swallowed-exception": "the code hides an error",
    "leftover-stub": "a placeholder was left in",
}


def failed_checks(state_dir: Path, record: dict) -> list[str]:
    """The checks that failed in a run, one plain line each, repeats folded (the stop reason often hides them)."""
    folder = Path(state_dir) / "cards" / str(record.get("card_id")) / str(record.get("run_id")) / "gates"
    lines: list[str] = []
    for path in sorted(folder.glob("*/*.json")):
        try:
            gate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if gate.get("result") != "fail" or str(gate.get("name", "")).startswith("micromanager"):
            continue
        labels = {str(b.get("pattern")) for b in (gate.get("evidence") or {}).get("banned") or [] if isinstance(b, dict)}
        said = "; ".join(sorted(_RULE_WORDS.get(label, label) for label in labels))
        line = (f"a build rule was broken: {said}" if said else f"{gate.get('name')}: {str(gate.get('detail') or '')[:200]}")
        if line not in lines:
            lines.append(line)
    return lines


def triage_card(state_dir: Path, card_id: str) -> dict | None:
    """`triage` for a task's latest run, or None when it has never run."""
    runs = records(state_dir, card_id)
    if not runs:
        return None
    got = triage(runs[-1], runs[:-1])
    checks = failed_checks(state_dir, runs[-1])
    if checks and got["kind"] != "check":
        if runs[-1].get("stop_code") == "cannot-split":  # "too big to split" is how it ended, not why
            got["why"] = "The builder could not get past the checks and gave up."
        got["why"] += " What failed on the way: " + "; ".join(checks) + "."
        # Every try broke a rule the task's own text asks for: retrying the same task can't end differently.
        if got["kind"] == "builder" and all(c.startswith("a build rule") for c in checks) and int(runs[-1].get("attempts") or 0) > 1:
            got.update(triage(dict(runs[-1], stop_code="cannot-split")), why=got["why"])
    got["checks"] = checks
    return got


def propose_fix(root: Path, cfg, store, card_id: str, why: str = "", backend=None) -> dict | None:
    """A fix for a task that is wrong, shown as a diff before anything is saved.

    The plan checks' own fixes come first. With a `backend`, the Architect is asked
    next and given the builder's diagnosis. Returns `{"header", "body", "notes",
    "diff"}`, or None when nobody has a fix.
    """
    from .adapters.files import parse_card_file, render_card_file
    from .frontier_review import _extract_json_dict
    from .preflight import apply_fixes, check_card
    from .store import card_from

    header, body = store.read(card_id)
    before = render_card_file(header, body)
    new_header, new_body, notes = apply_fixes(header, body, check_card(card_from(header, body), cfg, root, store.cards()))
    if not notes and backend is not None:
        prompt = (
            "This openfarm task card could not be built. Rewrite the card so it can be, keeping what it delivers.\n"
            f"What the builder reported: {why or 'no diagnosis'}\n"
            "Rules: every file the text names is in `touches`; no parameter defaults in a signature.\n"
            f"{BUILD_RULES}\n"
            'Return ONLY JSON: {"card": "<the whole card file, +++ TOML header and body>", "change": "<one sentence>"}\n\n'
            + before
        )
        try:
            parsed = _extract_json_dict(str(backend.ask(prompt) if hasattr(backend, "ask") else backend(prompt))) or {}
            got_header, new_body = parse_card_file(str(parsed.get("card") or ""))
        except Exception:  # noqa: BLE001 - no usable answer means no fix, which the caller shows
            return None
        new_header = {**header, **{k: v for k, v in got_header.items() if k not in ("id", "state")}}
        notes = [f"the Architect rewrote it: {str(parsed.get('change') or 'to fit what the builder reported').strip()}"]
    if not notes:
        return None
    after = render_card_file(new_header, new_body)
    diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), "task now", "task with the fix",
                                          lineterm="", n=1))
    return {"header": new_header, "body": new_body, "notes": notes, "diff": diff}


def apply_fix_and_retry(store, card_id: str, fix: dict, by: str = "") -> dict:
    """Save a proposed fix to the task and put it back in the queue."""
    header = dict(fix["header"], repairs=[*(store.read(card_id)[0].get("repairs") or []), *fix["notes"]])
    store.change_card(card_id, "edit", by=by, why="; ".join(fix["notes"]),
                      apply=lambda h, _b: (dict(header, state=h.get("state", "ready")), fix["body"]))
    return store.change_card(card_id, "retry", by=by, why="retry with the fix")


def check_command(cfg, card, root: Path) -> list[str]:
    """The exact command the test checks run for this task's named tests."""
    from .packs.base import pack_for

    lang = cfg.language_for_unit(card.unit) if hasattr(cfg, "language_for_unit") else cfg.languages[0]
    pack = pack_for(lang, cfg.cargo_flags, spec=cfg.pack_adapter, cwd=root, python=cfg.runner_python)
    if getattr(pack, "apply_settings", None) is not None:
        pack.apply_settings(cfg)
    files = sorted({t.file for t in card.tests if t.file})
    return list(pack.commands("test", card.unit, tests=[t.name for t in card.tests], files=files or None)["argv"])


def run_check(root: Path, cfg, card, timeout_s: float = 300) -> dict:
    """Run the task's tests on its own branch and hand back the raw output.

    Returns `{"command", "exit", "output", "where"}`. The branch is checked out
    in a throwaway folder, so nothing in the project moves.
    """
    from .git import Git

    git = Git(root)
    branch = f"openfarm/{card.id}-{card.slug()}"
    if not git.branch_exists(branch):
        return {"command": "", "exit": None, "output": f"The task's branch {branch} does not exist, so there is nothing to run.",
                "where": branch}
    argv = check_command(cfg, card, root)
    with tempfile.TemporaryDirectory(prefix="openfarm-check-") as tmp:
        work = Path(tmp) / "work"
        git.worktree_add(work, branch)
        try:
            done = subprocess.run(argv, cwd=work, capture_output=True, text=True, timeout=timeout_s, check=False)
            output, code = (done.stdout + done.stderr).strip(), done.returncode
        except subprocess.TimeoutExpired:
            output, code = f"The command did not finish in {int(timeout_s)} seconds.", None
        except OSError as exc:
            output, code = f"The command could not start: {exc}", None
        finally:
            git.worktree_remove(work)
    return {"command": " ".join(argv), "exit": code, "output": output, "where": branch}
