"""Preflight Touches Conflict Matrix & `openfarm doctor [--fix]` (v0.10.2).

`run_doctor()` inspects git cleanliness, config, Frontier ACP OAuth status (without
ever invoking `agy` unattended), Worker endpoint/key permissions, `bwrap` confinement,
installed language toolchains, stale `run.lock`, pause/breaker flags, orphaned
Openfarm worktrees, and foreign `cards-source` worktrees.

Safety invariant: `--fix` performs strictly non-destructive Openfarm housekeeping
(stale lock removal, orphaned worktree pruning under `<state_dir>/worktrees`,
foreign `cards-source` rebuild, `0600` key chmod) and NEVER modifies user source
files or git history.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from .acp_setup import inspect_acp_status
from .adapters.files import FileCardSource
from .config import CONFIG_PATH, load_config
from .errors import ConfigError, Refused
from .git import Git
from .providers import is_placeholder_endpoint, load_global_providers, probe_worker_endpoint
from .state import StateDir
from .swarm import collision_reason


def compute_touches_conflict_matrix(cards: list) -> dict:
    """Compute pairwise `touches`/dependency conflicts and greedy parallel execution lanes."""
    conflicts: list[dict] = []
    adj: dict[str, set[str]] = {c.id: set() for c in cards}
    for i, card_a in enumerate(cards):
        for card_b in cards[i + 1 :]:
            reason = collision_reason(card_a, card_b)
            if reason:
                conflicts.append({"a": card_a.id, "b": card_b.id, "reason": reason})
                adj[card_a.id].add(card_b.id)
                adj[card_b.id].add(card_a.id)

    # Greedy graph coloring into parallel lanes (cards in the same lane never collide).
    lanes: list[list[str]] = []
    for card in cards:
        placed = False
        for lane in lanes:
            if all(other not in adj[card.id] for other in lane):
                lane.append(card.id)
                placed = True
                break
        if not placed:
            lanes.append([card.id])

    return {
        "card_count": len(cards),
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "parallel_lanes": lanes,
        "max_parallelism": max((len(lane) for lane in lanes), default=0),
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def run_doctor(
    repo: Path | str = ".",
    *,
    config_path: Path | None = None,
    fix: bool = False,
    probe_worker: bool = False,
) -> dict:
    """Inspect repo, providers, toolchains, and Openfarm state; optionally apply safe fixes."""
    root = Path(repo).expanduser().resolve()
    checks: list[dict] = []
    fixes_applied: list[str] = []

    def _add(cid: str, label: str, status: str, detail: str, fixable: bool = False) -> None:
        checks.append({
            "id": cid,
            "label": label,
            "status": status,
            "detail": detail,
            "fixable": fixable,
        })

    # 1. Git repository & cleanliness
    if (root / ".git").exists():
        try:
            git = Git(root)
            branch = git.current_branch() or "(detached)"
            clean = git.is_clean()
            _add(
                "git",
                "Git repository",
                "ok" if clean else "warn",
                f"branch={branch}, {'clean' if clean else 'uncommitted changes'}",
            )
        except Exception as exc:
            _add("git", "Git repository", "fail", str(exc))
    else:
        _add("git", "Git repository", "warn", f"no .git directory in {root}")

    # 2. Config
    cfg = None
    cfg_p = Path(config_path).expanduser().resolve() if config_path else (root / CONFIG_PATH)
    if cfg_p.is_file():
        try:
            cfg = load_config(root, cfg_p)
            _add("config", "Openfarm config", "ok", f"{cfg.name} ({', '.join(cfg.languages)})")
        except (ConfigError, OSError) as exc:
            _add("config", "Openfarm config", "fail", str(exc))
    else:
        _add("config", "Openfarm config", "warn", f"not initialized ({CONFIG_PATH} missing)")

    # 3. Global & repo Worker provider + 0600 key permissions
    gp = load_global_providers()
    ep = str(getattr(cfg, "endpoint", "") or gp["worker"].get("endpoint") or "")
    kf = getattr(cfg, "key_file", None)
    if kf is None and gp["worker"].get("key_file"):
        kf = Path(gp["worker"]["key_file"]).expanduser()

    if is_placeholder_endpoint(ep):
        _add("worker_endpoint", "Worker endpoint", "warn", "unset or placeholder (configure via `openfarm providers set`)")
    else:
        _add("worker_endpoint", "Worker endpoint", "ok", ep)

    if kf is not None and kf.is_file() and not kf.is_symlink():
        mode = stat.S_IMODE(kf.stat().st_mode)
        if mode != 0o600:
            if fix:
                os.chmod(kf, 0o600)
                fixes_applied.append(f"chmod 0600 {kf}")
                _add("key_permissions", "Worker key permissions", "ok", f"fixed to 0600 ({kf})")
            else:
                _add(
                    "key_permissions",
                    "Worker key permissions",
                    "warn",
                    f"{kf} has mode {oct(mode)}, expected 0o600",
                    fixable=True,
                )
        else:
            _add("key_permissions", "Worker key permissions", "ok", f"0600 ({kf})")
    elif kf is not None:
        _add("key_permissions", "Worker key permissions", "warn", f"key file missing ({kf})")

    if probe_worker and not is_placeholder_endpoint(ep):
        pr = probe_worker_endpoint(ep, key_file=kf, zdr=bool(gp["worker"].get("zdr", True)))
        _add(
            "worker_probe",
            "Worker live probe",
            "ok" if pr["ok"] else "fail",
            f"HTTP {pr['status']} ({pr['latency_ms']}ms)" if pr["ok"] else str(pr["error"]),
        )

    # 4. Frontier ACP OAuth (read-only status check; never executes agy)
    fr_prov = str(gp.get("frontier", {}).get("provider") or "claude")
    try:
        acp = inspect_acp_status(root, provider=fr_prov)
        ready = bool(acp.get("oauth_ready") or acp.get("ok"))
        _add(
            "frontier_acp",
            f"Frontier ACP ({fr_prov})",
            "ok" if ready else "warn",
            "OAuth ready" if ready else f"login required (`openfarm acp login --provider {fr_prov}`)",
        )
    except Exception as exc:
        _add("frontier_acp", f"Frontier ACP ({fr_prov})", "warn", str(exc))

    # 5. Bubblewrap confinement
    bwrap_path = shutil.which("bwrap")
    _add(
        "confinement",
        "Bubblewrap confinement",
        "ok" if bwrap_path else "warn",
        bwrap_path or "bwrap not found on PATH (using non-bwrap confinement fallback)",
    )

    # 6. Language toolchains
    langs = list(cfg.languages) if cfg is not None else ["python", "typescript", "rust", "go"]
    tool_map = {
        "python": ("python3", "pytest", "ruff"),
        "typescript": ("node", "tsc", "prettier"),
        "rust": ("cargo", "rustc", "cargo-mutants"),
        "go": ("go", "gofmt"),
    }
    found_tools: list[str] = []
    missing_primary: list[str] = []
    for lang in langs:
        bins = tool_map.get(lang, ())
        for idx, b in enumerate(bins):
            if shutil.which(b):
                found_tools.append(b)
            elif idx == 0:
                missing_primary.append(f"{lang}:{b}")
    _add(
        "toolchains",
        "Language toolchains",
        "warn" if missing_primary else "ok",
        f"available=[{', '.join(dict.fromkeys(found_tools))}]"
        + (f"; missing primary=[{', '.join(missing_primary)}]" if missing_primary else ""),
    )

    # 7. StateDir checks (stale run.lock, paused/breaker, foreign cards-source, orphaned worktrees)
    if cfg is not None:
        st = StateDir(cfg.state_dir)
        lock_file = st.root / "run.lock"
        if lock_file.is_file():
            pid = 0
            try:
                lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
                pid = int(lock_data.get("pid") or 0) if isinstance(lock_data, dict) else 0
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                _add("run_lock", "Run lock", "ok", f"active run lock held by pid {pid}")
            else:
                if fix:
                    lock_file.unlink(missing_ok=True)
                    fixes_applied.append(f"removed stale {lock_file}")
                    _add("run_lock", "Run lock", "ok", "stale run.lock removed")
                else:
                    _add("run_lock", "Run lock", "warn", f"stale run.lock (pid {pid} not running)", fixable=True)
        else:
            _add("run_lock", "Run lock", "ok", "no stale lock")

        if st.is_paused():
            _add("queue_pause", "Queue pause", "warn", "queue is paused (`openfarm resume` to clear)")
        else:
            _add("queue_pause", "Queue pause", "ok", "not paused")

        breaker = st.read_breaker()
        if breaker.get("tripped"):
            _add("breaker", "Circuit breaker", "warn", f"tripped: {breaker.get('reason', '')}")
        else:
            _add("breaker", "Circuit breaker", "ok", "healthy")

        cs_dir = st.root / "cards-source"
        if cs_dir.exists() and (root / ".git").exists():
            if not FileCardSource._worktree_belongs_to_repo(cs_dir, root):
                if fix:
                    shutil.rmtree(cs_dir)
                    fixes_applied.append(f"removed foreign cards-source {cs_dir}")
                    _add("cards_source", "Cards worktree", "ok", "rebuilt foreign cards-source worktree")
                else:
                    _add(
                        "cards_source",
                        "Cards worktree",
                        "warn",
                        f"{cs_dir} points to a different repository",
                        fixable=True,
                    )
            else:
                _add("cards_source", "Cards worktree", "ok", "bound to current repo")

        wt_root = st.root / "worktrees"
        if wt_root.is_dir():
            stray_wts = [p for p in wt_root.iterdir() if p.is_dir()]
            if stray_wts and not lock_file.is_file():
                if fix:
                    for wt in stray_wts:
                        shutil.rmtree(wt, ignore_errors=True)
                    fixes_applied.append(f"pruned {len(stray_wts)} orphaned worktree(s)")
                    _add("worktrees", "Orphaned worktrees", "ok", f"pruned {len(stray_wts)} orphaned worktree(s)")
                else:
                    _add(
                        "worktrees",
                        "Orphaned worktrees",
                        "warn",
                        f"{len(stray_wts)} leftover worktree(s) under {wt_root}",
                        fixable=True,
                    )
            else:
                _add("worktrees", "Orphaned worktrees", "ok", "clean")

    fail_count = sum(1 for c in checks if c["status"] == "fail")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    return {
        "ok": fail_count == 0,
        "repo": str(root),
        "checks": checks,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "fixes_applied": fixes_applied,
    }


__all__ = ["compute_touches_conflict_matrix", "run_doctor"]
