"""Factory Autopilot (v0.11.0): deterministic trial-rebase & opt-in local auto-land.

Safety invariants:
- `trial_rebase_branch()` runs inside a throwaway worktree; on ANY git conflict it
  aborts cleanly, preserves the original card branch SHA untouched, and returns
  `StopCode.REBASE_CONFLICT`.
- `auto_land_card()` is strictly opt-in (`[factory] auto_land = false` by default),
  lands ONLY onto the local `integration_branch` (default `openfarm/integration`),
  never modifies `base_branch` (`main`), and NEVER pushes to any remote.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .errors import Refused, StopCode
from .git import Git
from .triage import triage_card_heuristic


def trial_rebase_branch(
    repo: Path,
    branch: str,
    onto_ref: str,
    state_dir: Path,
) -> dict:
    """Deterministically trial-rebase `branch` onto `onto_ref` in a throwaway worktree."""
    root = Path(repo).expanduser().resolve()
    git = Git(root)
    if not git.branch_exists(branch):
        raise Refused("branch-not-found", f"branch {branch!r} does not exist")
    old_sha = git.branch_head(branch)
    wt = Path(state_dir).expanduser().resolve() / "worktrees" / f"rebase-{int(time.time() * 1000)}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    try:
        git.worktree_add(wt, old_sha)
        wt_git = Git(wt)
        ok, out = wt_git.rebase(onto_ref)
        if not ok:
            return {
                "ok": False,
                "rebased": False,
                "branch": branch,
                "onto": onto_ref,
                "stop_code": StopCode.REBASE_CONFLICT.value,
                "preserved_sha": old_sha,
                "detail": out[:400],
            }
        new_sha = wt_git.head()
        git.create_branch(branch, new_sha)
        return {
            "ok": True,
            "rebased": new_sha != old_sha,
            "branch": branch,
            "onto": onto_ref,
            "old_sha": old_sha,
            "new_sha": new_sha,
            "detail": "rebased cleanly",
        }
    finally:
        git.worktree_remove(wt)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)


def evaluate_auto_land(
    card,
    cfg,
    *,
    record: dict | None = None,
    findings: list[dict] | None = None,
    frontier_report: dict | None = None,
) -> dict:
    """Evaluate whether `card` meets every `[factory]` opt-in auto-land safety bar."""
    fcfg = getattr(cfg, "factory", None)
    if fcfg is None or not getattr(fcfg, "auto_land", False):
        return {"eligible": False, "reasons": ["[factory] auto_land is disabled"]}

    reasons: list[str] = []
    state = str(getattr(card, "state", "") or "").lower().replace("-", "_")
    if state != "in_review":
        reasons.append(f"state is {state!r}, not 'in_review'")

    fields = getattr(card, "fields", None) or {}
    profile = str(fields.get("profile") or "").strip().lower()
    if not profile:
        profile = str(triage_card_heuristic(card).get("recommended_profile") or "balanced").lower()
    allowed_profiles = tuple(getattr(fcfg, "auto_land_profiles", ("lean",)) or ("lean",))
    if profile not in allowed_profiles:
        reasons.append(f"profile {profile!r} not in auto_land_profiles {list(allowed_profiles)}")

    gates = (record or {}).get("gates") if isinstance(record, dict) else None
    if not isinstance(gates, list) or not gates:
        reasons.append("missing deterministic gate record")
    else:
        failed = [str(g.get("gate") or "?") for g in gates if isinstance(g, dict) and g.get("result") != "pass"]
        if failed:
            reasons.append(f"non-passing gate(s): {', '.join(failed)}")

    if getattr(fcfg, "require_zero_findings", True) and findings:
        reasons.append(f"{len(findings)} worker finding(s) require human review")

    if getattr(fcfg, "require_frontier_consensus", False):
        if not isinstance(frontier_report, dict) or not frontier_report.get("consensus") or frontier_report.get("agreed_verdict") != "approve":
            reasons.append("mixture-of-frontiers consensus 'approve' required")

    return {
        "eligible": not reasons,
        "profile": profile,
        "reasons": reasons,
    }


def auto_land_card(
    repo: Path,
    cfg,
    source,
    card,
    *,
    branch: str,
    record: dict | None = None,
    findings: list[dict] | None = None,
    frontier_report: dict | None = None,
) -> dict:
    """Merge an eligible card locally onto `cfg.factory.integration_branch` (never touching `main` or pushing)."""
    verdict = evaluate_auto_land(
        card,
        cfg,
        record=record,
        findings=findings,
        frontier_report=frontier_report,
    )
    if not verdict["eligible"]:
        return {"landed": False, "reasons": verdict["reasons"]}

    root = Path(repo).expanduser().resolve()
    git = Git(root)
    int_branch = str(cfg.factory.integration_branch or "openfarm/integration")
    if not git.branch_exists(int_branch):
        git.create_branch(int_branch, cfg.base_branch)

    wt = Path(cfg.state_dir).expanduser().resolve() / "worktrees" / f"autoland-{card.id}-{int(time.time() * 1000)}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    try:
        git.worktree_add(wt, git.branch_head(int_branch))
        wt_git = Git(wt)
        ok, out = wt_git.merge_no_ff(branch, message=f"openfarm(auto-land): merge {branch} ({card.id})")
        if not ok:
            return {
                "landed": False,
                "stop_code": StopCode.REBASE_CONFLICT.value,
                "reasons": [f"merge conflict onto {int_branch}: {out[:200]}"],
            }
        new_sha = wt_git.head()
        git.create_branch(int_branch, new_sha)
    finally:
        git.worktree_remove(wt)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)

    # Mark card done on openfarm-cards
    try:
        source.move(card.id, "", "done")
    except (Refused, OSError) as exc:
        move_err = str(exc)
        _ = move_err
    return {
        "landed": True,
        "card": card.id,
        "branch": branch,
        "integration_branch": int_branch,
        "commit": new_sha,
    }


def promote_integration_branch(repo: Path, cfg) -> dict:
    """Local-only `--no-ff` merge of `cfg.factory.integration_branch` into `cfg.base_branch` (zero push)."""
    root = Path(repo).expanduser().resolve()
    git = Git(root)
    int_branch = str(cfg.factory.integration_branch or "openfarm/integration")
    base = cfg.base_branch
    if not git.branch_exists(int_branch):
        raise Refused("no-integration-branch", f"integration branch {int_branch!r} does not exist yet")
    if not git.is_clean():
        raise Refused("dirty-worktree", "working tree is not clean; commit or stash before promoting")
    ahead = git.count_commits(int_branch, base=base)
    if ahead == 0:
        return {"promoted": False, "ahead": 0, "reason": f"{int_branch} has no new commits ahead of {base}"}
    git.checkout(base)
    ok, out = git.merge_no_ff(int_branch, message=f"openfarm: merge {int_branch} into {base}")
    if not ok:
        raise Refused("merge-conflict", f"conflict merging {int_branch} into {base}: {out[:300]}")
    return {
        "promoted": True,
        "integration_branch": int_branch,
        "base_branch": base,
        "ahead": ahead,
        "commit": git.head(),
    }


__all__ = [
    "auto_land_card",
    "evaluate_auto_land",
    "promote_integration_branch",
    "trial_rebase_branch",
]
