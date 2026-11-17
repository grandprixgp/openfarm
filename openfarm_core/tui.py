"""Stdlib curses TUI and deterministic headless snapshot renderer for openfarm.

Pure Python 3.11+ standard library only. Never invokes git directly (always
uses ``openfarm_core.git.Git``) and never reads or exposes ``key_file`` or
``auth_file`` contents.
"""

from __future__ import annotations

import argparse
import contextlib
import curses
import dataclasses
import importlib
import io
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from .acp_setup import ACP_ACTIONS, inspect_acp_status
from .adapters.files import parse_card_file
from .bench import load_task, render_report
from .cards import Card, extract_fenced_fields
from .config import CONFIG_PATH, Config, ConfigError, load_config
from .dossier import render_markdown
from .errors import AdapterError, ConfigError as _CfgErr, Infra, Invalid, Refused, UsageError
from .git import Git
from .globs import matches, normalise
from .harness_global import GlobalLibrary, global_dir_for
from .harness_memory import HarnessLibrary, sanitize_lesson_text
from .state import StateDir, _safe_id, append_jsonl, read_json, utc_now, write_json

VIEWS = ("overview", "cards", "dossier", "board", "swarm", "harness", "ops")
VIEW_LABELS = {
    "overview": "Overview",
    "cards": "Cards",
    "dossier": "Dossier",
    "board": "Board",
    "swarm": "Swarm",
    "harness": "Harness",
    "ops": "Ops",
}

VALID_MEMBER_KINDS = ("api", "acp", "code", "external", "scripted")
VALID_ROLES = (
    "coordinator",
    "supervisor",
    "planner",
    "test-writer",
    "implementer",
    "checker",
    "gates",
    "monitor",
)
VALID_POST_KINDS = (
    "brief",
    "result",
    "gate",
    "defect",
    "question",
    "answer",
    "handback",
    "action",
    "done",
    "blocked",
    "stall",
    "roster",
    "note",
)
EFFORT_STEPS = ("off", "low", "medium", "high", "xhigh", "max")


@dataclasses.dataclass
class TuiContext:
    """Resolved repository, config, and state handles (supports pre-config dirs)."""

    repo: Path
    config_path: Path | None
    configured: bool
    config_error: str
    cfg: Config | None
    state: StateDir | None


def _optional_module(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _load_tui_context(repo: Path | str, config_path: Path | str | None = None) -> TuiContext:
    resolved_repo = Path(repo).resolve()
    explicit_cfg = Path(config_path) if config_path else None
    target_cfg = explicit_cfg if explicit_cfg is not None else (resolved_repo / CONFIG_PATH)
    if explicit_cfg is None and not target_cfg.exists():
        return TuiContext(
            repo=resolved_repo,
            config_path=None,
            configured=False,
            config_error="",
            cfg=None,
            state=None,
        )
    try:
        cfg = load_config(resolved_repo, explicit_cfg)
    except (ConfigError, OSError) as exc:
        return TuiContext(
            repo=resolved_repo,
            config_path=explicit_cfg,
            configured=False,
            config_error=str(exc),
            cfg=None,
            state=None,
        )
    return TuiContext(
        repo=resolved_repo,
        config_path=explicit_cfg,
        configured=True,
        config_error="",
        cfg=cfg,
        state=StateDir(cfg.state_dir),
    )


def _normalize_role(role: str) -> str:
    text = str(role or "").strip().lower()
    if text == "test_writer":
        return "test-writer"
    if text == "micromanager":
        return "supervisor"
    return text


def _is_secret_file(ctx: TuiContext, candidate: Path) -> bool:
    if ctx.cfg is None:
        return False
    resolved = candidate.resolve()
    secrets: list[Path] = []
    if getattr(ctx.cfg, "key_file", None) is not None:
        secrets.append(Path(ctx.cfg.key_file))
    mm_cfg = getattr(ctx.cfg, "micromanager", None)
    if mm_cfg is not None and getattr(mm_cfg, "auth_file", None) is not None:
        secrets.append(Path(mm_cfg.auth_file))
    for secret in secrets:
        try:
            if resolved == secret.resolve():
                return True
        except OSError:
            continue
    return False


def _globs_overlap(pat1: str, pat2: str) -> bool:
    """Return True if two repo-relative globs could match a common path."""
    globs_mod = _optional_module("openfarm_core.globs")
    mod_fn = getattr(globs_mod, "globs_overlap", None) if globs_mod else None
    if callable(mod_fn):
        return bool(mod_fn(pat1, pat2))

    p1 = normalise(pat1)
    p2 = normalise(pat2)
    if not p1 or not p2:
        return False
    if p1 == p2:
        return True
    if not any(ch in p1 for ch in "*?"):
        return matches(p1, p2)
    if not any(ch in p2 for ch in "*?"):
        return matches(p2, p1)
    return _nfa_globs_overlap(p1, p2)


def _compile_glob_tokens(pattern: str) -> list[str]:
    tokens: list[str] = []
    idx = 0
    length = len(pattern)
    while idx < length:
        if pattern.startswith("**/", idx):
            tokens.append("**/")
            idx += 3
        elif pattern.startswith("/**", idx) and idx + 3 == length:
            tokens.append("/**")
            idx += 3
        elif pattern.startswith("**", idx):
            tokens.append("**")
            idx += 2
        else:
            tokens.append(pattern[idx])
            idx += 1
    return tokens


def _nfa_globs_overlap(pat1: str, pat2: str) -> bool:
    """Exact BFS over the product of two glob token streams."""
    t1 = _compile_glob_tokens(pat1)
    t2 = _compile_glob_tokens(pat2)
    n1, n2 = len(t1), len(t2)

    def _expand(tokens: list[str], pos: int) -> list[int]:
        out = [pos]
        seen = {pos}
        stack = [pos]
        while stack:
            cur = stack.pop()
            if cur >= len(tokens):
                continue
            tok = tokens[cur]
            if tok in ("**/", "/**", "**", "*"):
                nxt = cur + 1
                if nxt not in seen:
                    seen.add(nxt)
                    out.append(nxt)
                    stack.append(nxt)
        return out

    def _step(tok: str, ch: str) -> bool:
        if tok == "**/":
            return True
        if tok == "/**":
            return True
        if tok == "**":
            return True
        if tok == "*":
            return ch != "/"
        if tok == "?":
            return ch != "/"
        return tok == ch

    def _alphabet(i: int, j: int) -> list[str]:
        chars = {"a", "/"}
        for tok in (t1[i] if i < n1 else "", t2[j] if j < n2 else ""):
            if len(tok) == 1 and tok not in "*?":
                chars.add(tok)
        return sorted(chars)

    queue: list[tuple[int, int]] = []
    visited: set[tuple[int, int]] = set()
    for s1 in _expand(t1, 0):
        for s2 in _expand(t2, 0):
            pair = (s1, s2)
            if pair not in visited:
                visited.add(pair)
                queue.append(pair)

    while queue:
        i, j = queue.pop(0)
        if i == n1 and j == n2:
            return True
        if i == n1 or j == n2:
            continue
        tok1, tok2 = t1[i], t2[j]
        for ch in _alphabet(i, j):
            if not (_step(tok1, ch) and _step(tok2, ch)):
                continue
            next_i = i if tok1 in ("**/", "/**", "**", "*") else i + 1
            next_j = j if tok2 in ("**/", "/**", "**", "*") else j + 1
            for ni in _expand(t1, next_i):
                for nj in _expand(t2, next_j):
                    pair = (ni, nj)
                    if pair not in visited:
                        visited.add(pair)
                        queue.append(pair)
    return False


# -- Run, board, and swarm readers -------------------------------------------


def _resolve_run_id(
    state: StateDir | None, explicit_run_id: str | None = None
) -> tuple[str, str]:
    if state is None:
        return "", "none"
    if explicit_run_id:
        safe = _safe_id(explicit_run_id, "run_id")
        return safe, "explicit"
    holder = state.lock_holder()
    if isinstance(holder, dict) and holder.get("run_id"):
        return str(holder["run_id"]), "active-lock"
    if state.runs.is_dir():
        dirs = sorted(p for p in state.runs.iterdir() if p.is_dir())
        with_board = [
            p for p in dirs if (p / "board" / "board.jsonl").is_file() or (p / "board" / "members.json").is_file()
        ]
        if with_board:
            return with_board[-1].name, "latest"
        with_run = [p for p in dirs if (p / "run.json").is_file()]
        if with_run:
            return with_run[-1].name, "latest"
        if dirs:
            return dirs[-1].name, "latest"
    return "", "none"


def _read_board_posts_raw(board_dir: Path) -> list[dict]:
    jsonl = board_dir / "board.jsonl"
    if not jsonl.is_file():
        return []
    posts: list[dict] = []
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(item, dict):
            posts.append(item)
    posts.sort(key=lambda p: int(p.get("seq", 0) or 0))
    return posts


def _read_board_data(
    ctx: TuiContext,
    run_id: str | None = None,
    *,
    board_filter: dict[str, str] | None = None,
) -> dict:
    resolved_run, run_source = _resolve_run_id(ctx.state, run_id)
    empty = {
        "run_id": resolved_run,
        "run_source": run_source,
        "exists": False,
        "board_dir": "",
        "ended": False,
        "ended_at": "",
        "reason": "",
        "total_posts": 0,
        "last_seq": 0,
        "posts": [],
        "members": {},
        "unanswered_questions": [],
        "waiting_members": [],
        "filter": dict(board_filter or {}),
    }
    if ctx.state is None or not resolved_run:
        return empty
    board_dir = ctx.state.run_dir(resolved_run) / "board"
    empty["board_dir"] = str(board_dir)
    if not board_dir.exists():
        return empty

    state_meta = read_json(board_dir / "state.json", {}) or {}
    members_raw = read_json(board_dir / "members.json", {}) or {}
    members = members_raw if isinstance(members_raw, dict) else {}
    all_posts = _read_board_posts_raw(board_dir)

    answered_refs = {
        p.get("ref")
        for p in all_posts
        if p.get("kind") == "answer" and p.get("ref") is not None
    }
    unanswered = [
        {
            "seq": p.get("seq"),
            "from": p.get("from", ""),
            "to": list(p.get("to") or []),
            "card": p.get("card", ""),
            "expects": p.get("expects"),
            "body_preview": str(p.get("body", "")).replace("\n", " ")[:100],
        }
        for p in all_posts
        if p.get("kind") == "question" and p.get("seq") not in answered_refs
    ]
    waiting_members = [
        {
            "name": m.get("name", name),
            "role": m.get("role", ""),
            "kind": m.get("kind", ""),
            "card": m.get("card", ""),
            "wait_since": m.get("wait_since", ""),
            "last_read_seq": int(m.get("last_read_seq", 0) or 0),
        }
        for name, m in sorted(members.items())
        if isinstance(m, dict) and m.get("state") == "waiting"
    ]

    flt = board_filter or {}
    f_kind = str(flt.get("kind", "") or "").strip()
    f_member = str(flt.get("member", "") or "").strip()
    f_card = str(flt.get("card", "") or "").strip()

    filtered: list[dict] = []
    for post in all_posts:
        if f_kind and post.get("kind") != f_kind:
            continue
        if f_card and post.get("card") != f_card:
            continue
        if f_member:
            to_list = [str(x) for x in (post.get("to") or [])]
            if post.get("from") != f_member and f_member not in to_list:
                continue
        body = str(post.get("body", "") or "")
        preview = " ".join(body.split())[:120]
        filtered.append(
            {
                "seq": int(post.get("seq", 0) or 0),
                "at": str(post.get("at", "")),
                "from": str(post.get("from", "")),
                "to": [str(x) for x in (post.get("to") or [])],
                "kind": str(post.get("kind", "")),
                "card": str(post.get("card", "") or ""),
                "attempt": int(post.get("attempt", 0) or 0),
                "ref": post.get("ref"),
                "body": body,
                "body_preview": preview,
                "evidence": [str(e) for e in (post.get("evidence") or [])],
                "expects": post.get("expects"),
            }
        )

    return {
        "run_id": resolved_run,
        "run_source": run_source,
        "exists": True,
        "board_dir": str(board_dir),
        "ended": bool(state_meta.get("ended", False)),
        "ended_at": str(state_meta.get("ended_at", "")),
        "reason": str(state_meta.get("reason", "")),
        "total_posts": len(all_posts),
        "last_seq": int(all_posts[-1].get("seq", 0)) if all_posts else 0,
        "posts": filtered,
        "members": members,
        "unanswered_questions": unanswered,
        "waiting_members": waiting_members,
        "filter": dict(flt),
    }


def _read_swarm_summary(cfg: Config | None) -> dict:
    if cfg is None:
        return {
            "configured": False,
            "enabled": False,
            "coordinator": "code",
            "supervisor": "config",
            "coordinator_effort": "max",
            "supervisor_effort": "max",
            "default_tier": "default",
            "escalate_after": 2,
            "handbacks_per_card": 2,
            "max_members": 24,
            "coordinator_calls_per_run": 60,
            "coordinator_tokens_per_run": 600_000,
            "external_wait_s": 1800.0,
            "on_external_timeout": "fallback",
            "stall_after_s": 300.0,
        }
    sw = getattr(cfg, "swarm", None)
    if sw is None:
        return {
            "configured": False,
            "enabled": False,
            "coordinator": "code",
            "supervisor": "config",
            "coordinator_effort": "max",
            "supervisor_effort": "max",
            "default_tier": "default",
            "escalate_after": 2,
            "handbacks_per_card": 2,
            "max_members": 24,
            "coordinator_calls_per_run": 60,
            "coordinator_tokens_per_run": 600_000,
            "external_wait_s": 1800.0,
            "on_external_timeout": "fallback",
            "stall_after_s": 300.0,
        }
    return {
        "configured": True,
        "enabled": bool(getattr(sw, "enabled", False)),
        "coordinator": str(getattr(sw, "coordinator", "code")),
        "supervisor": str(getattr(sw, "supervisor", "config")),
        "coordinator_effort": str(getattr(sw, "coordinator_effort", "max")),
        "supervisor_effort": str(getattr(sw, "supervisor_effort", "max")),
        "default_tier": str(getattr(sw, "default_tier", "default")),
        "escalate_after": int(getattr(sw, "escalate_after", 2)),
        "handbacks_per_card": int(getattr(sw, "handbacks_per_card", 2)),
        "max_members": int(getattr(sw, "max_members", 24)),
        "coordinator_calls_per_run": int(getattr(sw, "coordinator_calls_per_run", 60)),
        "coordinator_tokens_per_run": int(getattr(sw, "coordinator_tokens_per_run", 600_000)),
        "external_wait_s": float(getattr(sw, "external_wait_s", getattr(sw, "external_wait", 1800.0))),
        "on_external_timeout": str(getattr(sw, "on_external_timeout", "fallback")),
        "stall_after_s": float(getattr(sw, "stall_after_s", getattr(sw, "stall_after", 300.0))),
    }


def _read_tiers_summary(cfg: Config | None) -> list[dict]:
    if cfg is None:
        return []
    raw_tiers = getattr(cfg, "tiers", None)
    if isinstance(raw_tiers, dict) and raw_tiers:
        out: list[dict] = []
        for name, tier in raw_tiers.items():
            out.append(
                {
                    "name": str(getattr(tier, "name", name) or name),
                    "model": str(getattr(tier, "model", "")),
                    "efforts": [str(e) for e in (getattr(tier, "efforts", ()) or ())],
                    "effort_param": str(getattr(tier, "effort_param", "")),
                }
            )
        return out
    impl = cfg.role_models.get("implementer") if getattr(cfg, "role_models", None) else None
    model_name = str(getattr(impl, "model", "") or "")
    effort_val = str(getattr(impl, "effort", "") or "")
    if not model_name:
        return []
    return [
        {
            "name": "default (synthesized)",
            "model": model_name,
            "efforts": [effort_val] if effort_val else [],
            "effort_param": "reasoning_effort",
        }
    ]


# -- Snapshot collectors for each view ---------------------------------------


def _collect_overview(ctx: TuiContext, run_id: str | None = None) -> dict:
    swarm = _read_swarm_summary(ctx.cfg)
    tiers = _read_tiers_summary(ctx.cfg)
    if not ctx.configured or ctx.cfg is None or ctx.state is None:
        return {
            "configured": False,
            "config_error": ctx.config_error,
            "repo": str(ctx.repo),
            "run": {},
            "paused": False,
            "kill_switch": False,
            "breaker": {"tripped": False, "cards": [], "reason": ""},
            "swarm": swarm,
            "tiers": tiers,
            "last_run": {},
            "harness": {"enabled": False},
        }

    cfg = ctx.cfg
    state = ctx.state
    holder = state.lock_holder() or {}
    pause_info = read_json(state.paused, {}) if state.is_paused() else {}
    kill_path = state.kill_switch(cfg.kill_switch)
    kill_info = read_json(kill_path, {}) if kill_path.exists() else {}

    last_run_payload: dict[str, Any] = {}
    resolved_run, _ = _resolve_run_id(state, run_id)
    run_file = state.run_dir(resolved_run) / "run.json" if resolved_run else None
    if run_file is None or not run_file.is_file():
        runs = sorted(state.runs.glob("*/run.json")) if state.runs.is_dir() else []
        run_file = runs[-1] if runs else None
    if run_file is not None and run_file.is_file():
        raw_run = read_json(run_file, {}) or {}
        counts: dict[str, int] = {}
        card_outcomes: list[dict] = []
        for entry in raw_run.get("outcomes", []) or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("final_state") or "skipped")
            counts[key] = counts.get(key, 0) + 1
            card_outcomes.append(
                {
                    "card": entry.get("card", ""),
                    "final_state": entry.get("final_state", ""),
                    "stop_code": entry.get("stop_code", ""),
                    "skipped": entry.get("skipped", ""),
                }
            )
        tokens = dict(raw_run.get("tokens") or {})
        last_run_payload = {
            "run_id": raw_run.get("run_id", ""),
            "started_at": raw_run.get("started_at", ""),
            "finished_at": raw_run.get("finished_at", ""),
            "dry_run": bool(raw_run.get("dry_run", False)),
            "spend": float(raw_run.get("spend", 0.0) or 0.0),
            "micromanager_spend": float(raw_run.get("micromanager_spend", 0.0) or 0.0),
            "counts": counts,
            "outcomes": card_outcomes,
            "tokens": tokens,
        }

    harness_summary = _read_local_harness_snapshot(ctx, include_entries=False)
    role_summary = {
        role: {
            "model": rm.model,
            "effort": str(getattr(rm, "effort", "") or ""),
        }
        for role, rm in (cfg.role_models or {}).items()
    }
    return {
        "configured": True,
        "config_error": "",
        "repo": str(ctx.repo),
        "project": cfg.name,
        "lane": cfg.lane,
        "base_branch": cfg.base_branch,
        "state_dir": str(cfg.state_dir),
        "run": holder,
        "active_run_id": str(holder.get("run_id", "") or ""),
        "paused": state.is_paused(),
        "pause_reason": str((pause_info or {}).get("reason", "")),
        "kill_switch": state.kill_requested(cfg.kill_switch),
        "kill_reason": str((kill_info or {}).get("reason", "")),
        "breaker": state.read_breaker(),
        "spend_cap": cfg.spend_per_night,
        "concurrent_cards": cfg.concurrent_cards,
        "concurrent_builds": cfg.concurrent_builds,
        "model": {
            "adapter": cfg.model_adapter,
            "endpoint": cfg.endpoint,
            "key_file": str(cfg.key_file) if cfg.key_file is not None else None,
            "key_file_present": bool(cfg.key_file and cfg.key_file.is_file()),
            "header_names": sorted(cfg.model_headers.keys()),
            "roles": role_summary,
        },
        "swarm": swarm,
        "tiers": tiers,
        "last_run": last_run_payload,
        "harness": {
            "enabled": harness_summary.get("enabled", False),
            "revision": harness_summary.get("revision", 0),
            "state": harness_summary.get("state", ""),
            "active_count": harness_summary.get("active_count", 0),
            "total_entries": harness_summary.get("total_entries", 0),
        },
    }


def _card_from_header_and_body(header: dict, body: str, path: Path, fields_block: str = "") -> Card:
    merged = dict(header)
    if fields_block == "toml":
        block_fields, body = extract_fenced_fields(body, fields_block)
        if block_fields:
            merged = {**block_fields, **header}
    fields = {
        k: merged[k]
        for k in (
            "category",
            "reference",
            "tests",
            "test_edits",
            "frontier_estimate",
            "new_symbols",
            "unit",
            "context_files",
            "type_changes",
            "replan_if",
        )
        if k in merged
    }
    deps = [str(d) for d in (merged.get("depends", merged.get("deps", [])) or [])]
    touches = [str(t) for t in (merged.get("touches", []) or [])]
    done_body = body.strip()
    return Card(
        id=str(merged.get("id", path.stem)),
        title=str(merged.get("title", "")),
        state=str(merged.get("state", "backlog")),
        done_text=done_body or str(merged.get("done", "")),
        touches=touches,
        deps=deps,
        fields=fields,
    )


def _read_cards_readonly(ctx: TuiContext) -> tuple[list[Card], str]:
    if not ctx.configured or ctx.cfg is None or ctx.state is None:
        return [], "unconfigured"
    spec = ctx.cfg.cards_adapter
    if spec.startswith("cmd:"):
        return [], "cmd"
    if not spec.startswith("files:"):
        return [], "unknown"
    folder = (spec[len("files:") :].strip() or "cards").rstrip("/")
    source = ctx.state.root / "cards-source"
    cards_dir = source / folder
    git = Git(ctx.repo)
    cards_branch = getattr(ctx.cfg, "cards_branch", "openfarm-cards")

    if git.ref_exists(f"refs/heads/{cards_branch}"):
        from .adapters.files import open_file_source

        fsource = open_file_source(ctx.cfg, ctx.state.root)
        fsource.prepare()
        return fsource.all_cards(), "cards-source"
    if cards_dir.is_dir():
        source_mode = "cards-source"
        scan_dir = cards_dir
    else:
        source_mode = "repo-files"
        scan_dir = ctx.repo / folder

    if not scan_dir.is_dir():
        return [], source_mode
    cards: list[Card] = []
    for path in sorted(scan_dir.glob("*.md")):
        try:
            header, body = parse_card_file(path.read_text(encoding="utf-8", errors="replace"))
        except (Invalid, OSError):
            continue
        if not header.get("id"):
            header["id"] = path.stem
        cards.append(
            _card_from_header_and_body(
                header,
                body,
                path,
                fields_block=getattr(ctx.cfg, "fields_block", ""),
            )
        )
    return cards, source_mode


def _cards_collide(a: Card, b: Card) -> bool:
    if a.id == b.id:
        return False
    if b.id in a.deps or a.id in b.deps:
        return True
    for pat1 in a.touches:
        for pat2 in b.touches:
            if _globs_overlap(pat1, pat2):
                return True
    return False


def _collect_cards(ctx: TuiContext) -> dict:
    if not ctx.configured or ctx.cfg is None or ctx.state is None:
        return {"configured": False, "source_mode": "unconfigured", "cards": []}
    cards, source_mode = _read_cards_readonly(ctx)
    git = Git(ctx.repo)
    base_branch = ctx.cfg.base_branch
    has_head = False
    base_ref = "HEAD"
    try:
        has_head = git.ref_exists("HEAD")
        if git.ref_exists(f"refs/heads/{base_branch}"):
            base_ref = f"refs/heads/{base_branch}"
    except (Infra, OSError):
        has_head = False

    active_candidates = [c for c in cards if c.state not in ("done", "dropped")]
    out: list[dict] = []
    for card in cards:
        collides = [
            other.id
            for other in active_candidates
            if other.id != card.id and _cards_collide(card, other)
        ]
        branches: list[str] = []
        unmerged = 0
        diff_preview = ""
        if has_head:
            try:
                branches = git.branches(f"openfarm/{card.id}-*")
                if branches:
                    br = branches[0]
                    unmerged = git.count_commits(f"refs/heads/{br}", base_ref)
                    diff_preview = git.diff(ref=f"{base_ref}...refs/heads/{br}")[:6000]
            except (Infra, OSError):
                branches = []

        checks = sorted(ctx.state.check_dir(card.id).glob("*.json")) if ctx.state.checks.is_dir() else []
        latest_check = read_json(checks[-1], {}) if checks else {}
        records = sorted(ctx.state.cards.glob(f"{card.id}/*/card-record.json")) if ctx.state.cards.is_dir() else []
        latest_record = read_json(records[-1], {}) if records else {}
        verdict_data = read_json(records[-1].parent / "verdict.json", {}) if records else {}
        escapes_path = records[-1].parent / "escapes.jsonl" if records else None
        escapes_count = 0
        if escapes_path and escapes_path.is_file():
            escapes_count = sum(1 for ln in escapes_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())

        replan_if = list(getattr(card, "replan_if", None) or card.fields.get("replan_if") or [])
        out.append(
            {
                "id": card.id,
                "title": card.title,
                "state": card.state,
                "category": card.category,
                "unit": card.unit,
                "deps": list(card.deps),
                "touches": list(card.touches),
                "replan_if": replan_if,
                "collides_with": collides,
                "branches": branches,
                "unmerged_commits": unmerged,
                "diff_preview": diff_preview,
                "latest_check": latest_check or {},
                "latest_record": latest_record or {},
                "verdict": verdict_data or {},
                "escapes_count": escapes_count,
            }
        )
    return {
        "configured": True,
        "source_mode": source_mode,
        "base_branch": base_branch,
        "cards": out,
    }


def _discover_dossiers(ctx: TuiContext) -> list[tuple[str, str, Path]]:
    if ctx.state is None or not ctx.state.cards.is_dir():
        return []
    found: list[tuple[str, str, Path]] = []
    for path in sorted(ctx.state.cards.glob("*/*/dossier.json")):
        run_name = path.parent.name
        card_name = path.parent.parent.name
        found.append((card_name, run_name, path))
    return found


def _render_team_and_board_section(
    payload: dict, board_data: dict, card_id: str, run_id: str
) -> str:
    team_rows = payload.get("team") or []
    board_meta = payload.get("board") or {}
    handbacks = payload.get("handbacks") or []
    set_events = payload.get("team_sets") or []

    if not team_rows and board_data.get("exists"):
        for name, m in sorted((board_data.get("members") or {}).items()):
            if not isinstance(m, dict):
                continue
            if m.get("card") and m.get("card") != card_id:
                continue
            if not m.get("card") and m.get("role") not in ("coordinator", "supervisor", "monitor"):
                continue
            tok = m.get("tokens")
            if isinstance(tok, dict):
                tok_total = int(tok.get("in", 0) or 0) + int(tok.get("out", 0) or 0)
            else:
                tok_total = int(tok or 0) + int(m.get("tokens_in", 0) or 0) + int(m.get("tokens_out", 0) or 0)
            team_rows.append(
                {
                    "member": m.get("name", name),
                    "role": m.get("role", ""),
                    "tier": m.get("tier", ""),
                    "effort": m.get("effort", ""),
                    "attempts": m.get("attempts", 0),
                    "tokens": tok_total,
                }
            )
    if not handbacks and board_data.get("exists"):
        for post in board_data.get("posts") or []:
            if post.get("kind") == "handback" and (not card_id or post.get("card") == card_id):
                handbacks.append({"seq": post.get("seq"), "from": post.get("from"), "body": post.get("body")})
            if post.get("kind") == "action" and "set" in str(post.get("body", "")).lower():
                if not card_id or post.get("card") == card_id:
                    set_events.append(post.get("body", ""))

    board_path = str(board_meta.get("path") or board_data.get("board_dir") or "")
    lines = ["## Team and board", ""]
    if board_path:
        lines.append(f"- Board: `{board_path}` (run `{run_id}`)")
    if team_rows:
        lines += [
            "",
            "| Member | Role | Tier | Effort | Attempts | Tokens |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in team_rows:
            lines.append(
                f"| {row.get('member', row.get('name', ''))} | {row.get('role', '')} | "
                f"{row.get('tier', '') or '—'} | {row.get('effort', '') or '—'} | "
                f"{row.get('attempts', 0)} | {row.get('tokens', 0)} |"
            )
    else:
        lines.append("- Team: (no per-member rows recorded)")
    if set_events:
        lines += ["", "- Escalations / `set` actions:"]
        for evt in set_events:
            lines.append(f"  - {evt}")
    if handbacks:
        lines += ["", "- Handbacks:"]
        for hb in handbacks:
            if isinstance(hb, dict):
                lines.append(f"  - #{hb.get('seq', '?')} {hb.get('from', '')}: {str(hb.get('body', ''))[:200]}")
            else:
                lines.append(f"  - {str(hb)[:200]}")
    lines.append("")
    return "\n".join(lines)


def _collect_dossier(
    ctx: TuiContext,
    *,
    card_id: str | None = None,
    run_id: str | None = None,
    attempt: int | None = None,
) -> dict:
    if not ctx.configured or ctx.state is None:
        return {"configured": False, "available": [], "error": "repository not configured"}
    entries = _discover_dossiers(ctx)
    available = [{"card": c, "run": r, "path": str(p)} for c, r, p in entries]
    if not entries:
        return {"configured": True, "available": [], "error": "no dossiers found yet"}

    chosen_card = card_id
    if not chosen_card:
        chosen_card = entries[-1][0]
    card_runs = [(c, r, p) for c, r, p in entries if c == chosen_card]
    if not card_runs:
        return {
            "configured": True,
            "available": available,
            "card": chosen_card,
            "error": f"no dossier yet for {chosen_card}",
        }
    if run_id:
        matches_run = [(c, r, p) for c, r, p in card_runs if r == run_id]
        if not matches_run:
            return {
                "configured": True,
                "available": available,
                "card": chosen_card,
                "run": run_id,
                "error": f"no dossier for {chosen_card} in run {run_id}",
            }
        chosen_c, chosen_r, chosen_p = matches_run[-1]
    else:
        chosen_c, chosen_r, chosen_p = card_runs[-1]

    payload = dict(read_json(chosen_p, {}) or {})
    if attempt is not None:
        gates = [
            g for g in (payload.get("2_gates") or [])
            if g.get("attempt") == attempt
        ]
        if not gates:
            return {
                "configured": True,
                "available": available,
                "card": chosen_c,
                "run": chosen_r,
                "attempt": attempt,
                "error": f"no gate ran on attempt {attempt}",
            }
        payload["2_gates"] = gates
        payload["attempt"] = attempt
        diff_file = chosen_p.parent / "edits" / f"{attempt}.diff"
        if diff_file.is_file():
            attempt_diff = diff_file.read_text(encoding="utf-8", errors="replace")
            diffs = dict(payload.get("9_diffs") or {})
            diffs["attempt"] = attempt
            diffs["attempt_diff"] = attempt_diff
            diffs["production_diff"] = attempt_diff
            payload["9_diffs"] = diffs

    md = render_markdown(payload)
    board_data = _read_board_data(ctx, chosen_r)
    if "## Team and board" not in md:
        md = md.rstrip() + "\n\n" + _render_team_and_board_section(payload, board_data, chosen_c, chosen_r)

    return {
        "configured": True,
        "available": available,
        "card": chosen_c,
        "run": chosen_r,
        "attempt": attempt,
        "payload": payload,
        "markdown": md,
        "error": "",
    }


def _collect_swarm(ctx: TuiContext, run_id: str | None = None) -> dict:
    swarm_cfg = _read_swarm_summary(ctx.cfg)
    tiers = _read_tiers_summary(ctx.cfg)
    board = _read_board_data(ctx, run_id)
    roster: list[dict] = []
    for name, member in sorted((board.get("members") or {}).items()):
        if not isinstance(member, dict):
            continue
        tok = member.get("tokens")
        if isinstance(tok, dict):
            tokens_in = int(tok.get("in", 0) or 0)
            tokens_out = int(tok.get("out", 0) or 0)
        else:
            tokens_in = int(member.get("tokens_in", 0) or 0)
            tokens_out = int(member.get("tokens_out", 0) or 0)
        roster.append(
            {
                "name": str(member.get("name", name)),
                "kind": str(member.get("kind", "")),
                "role": str(member.get("role", "")),
                "card": str(member.get("card", "") or ""),
                "tier": str(member.get("tier", "") or ""),
                "effort": str(member.get("effort", "") or ""),
                "state": str(member.get("state", "")),
                "wait_since": str(member.get("wait_since", "") or ""),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tokens_total": tokens_in + tokens_out,
                "attempts": int(member.get("attempts", 0) or 0),
                "last_read_seq": int(member.get("last_read_seq", 0) or 0),
                "owner_kind": str(member.get("owner_kind", "") or ""),
            }
        )
    return {
        "configured": ctx.configured,
        "run_id": board["run_id"],
        "run_source": board["run_source"],
        "swarm": swarm_cfg,
        "tiers": tiers,
        "members": roster,
    }


def _harness_dir_for(ctx: TuiContext) -> Path | None:
    if not ctx.configured or ctx.cfg is None or ctx.state is None:
        return None
    hcfg = getattr(ctx.cfg, "harness", None)
    raw = str(getattr(hcfg, "dir", "") or "").strip() if hcfg else ""
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (ctx.repo / p).resolve()
    return ctx.state.harness()


def _read_local_harness_snapshot(ctx: TuiContext, *, include_entries: bool = True) -> dict:
    if not ctx.configured or ctx.cfg is None or ctx.state is None:
        return {"enabled": False, "exists": False, "entries": [], "index": ""}
    hdir = _harness_dir_for(ctx)
    hcfg = getattr(ctx.cfg, "harness", None)
    enabled = bool(getattr(hcfg, "enabled", False))
    if hdir is None or not hdir.exists():
        return {
            "enabled": enabled,
            "exists": False,
            "dir": str(hdir or ""),
            "revision": 0,
            "parent_revision": 0,
            "state": "uninitialized",
            "probe_remaining": 0,
            "active_entry_ids": [],
            "active_count": 0,
            "probing_count": 0,
            "candidate_count": 0,
            "demoted_count": 0,
            "total_entries": 0,
            "metrics": {},
            "entries": [],
            "index": "",
        }
    lib = HarnessLibrary(hdir, config=ctx.cfg)
    st = lib.status()
    entries = lib.list_entries() if include_entries else []
    index_md = lib.format_index() if include_entries else ""
    return {**st, "exists": True, "dir": str(hdir), "entries": entries, "index": index_md}


def _collect_harness(
    ctx: TuiContext,
    *,
    global_mode: bool = False,
    entry_id: str | None = None,
) -> dict:
    if not ctx.configured or ctx.cfg is None:
        return {
            "configured": False,
            "global_mode": global_mode,
            "local": {"enabled": False, "exists": False, "entries": [], "index": ""},
            "global": {"dir": "", "active": 0, "retired": 0, "entries": []},
            "selected_entry": None,
        }
    local = _read_local_harness_snapshot(ctx, include_entries=True)
    gdir = global_dir_for(ctx.cfg.harness)
    glib = GlobalLibrary(gdir)
    global_status = glib.status()

    selected_detail = None
    if entry_id:
        if global_mode:
            with contextlib.suppress(Invalid):
                selected_detail = glib.get(entry_id)
        else:
            hdir = _harness_dir_for(ctx)
            if hdir and hdir.exists():
                lib = HarnessLibrary(hdir, config=ctx.cfg)
                with contextlib.suppress(Invalid):
                    selected_detail = lib.show_entry(entry_id)

    return {
        "configured": True,
        "global_mode": global_mode,
        "local": local,
        "global": global_status,
        "selected_entry": selected_detail,
    }


def _collect_ops(
    ctx: TuiContext,
    *,
    acp_home: Path | None = None,
    claude_bin: str | None = None,
    acp_bin: str | None = None,
    include_cli_auth: bool = False,
    bench_dir: Path | None = None,
    bench_out: Path | None = None,
) -> dict:
    if include_cli_auth:
        acp_info = inspect_acp_status(
            ctx.repo if ctx.configured else None,
            home=acp_home,
            claude_bin=claude_bin,
            acp_bin=acp_bin,
        )
    else:
        resolved_claude = claude_bin if claude_bin is not None else (shutil.which("claude") or "")
        resolved_acp = acp_bin if acp_bin is not None else (shutil.which("claude-agent-acp") or "")
        transport = (
            "claude-agent-acp"
            if resolved_acp
            else ("claude-native-acp" if resolved_claude else "missing")
        )
        acp_info = {
            "ok": True,
            "resolved_transport": transport,
            "claude_cli": resolved_claude or None,
            "claude_agent_acp": resolved_acp or None,
            "oauth_token_env": bool(str(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()),
            "cli_auth_checked": False,
        }
        if ctx.configured and ctx.cfg is not None:
            cfg = ctx.cfg
            acp_info["repo_frontier"] = {
                "adapter": cfg.micromanager_adapter,
                "mode": cfg.micromanager.mode,
                "enabled": cfg.micromanager_enabled,
                "auth": cfg.micromanager.auth,
                "binary": cfg.micromanager.binary,
                "model": cfg.micromanager.model,
                "effort": cfg.micromanager.effort,
                "tool_policy": cfg.micromanager.tool_policy,
                "auth_file_present": bool(
                    cfg.micromanager.auth_file and cfg.micromanager.auth_file.is_file()
                ),
            }
            acp_info["repo_workers"] = {
                "adapter": cfg.model_adapter,
                "endpoint": cfg.endpoint,
                "key_file_present": bool(cfg.key_file and cfg.key_file.is_file()),
            }

    supervision_reports: list[dict] = []
    if ctx.state is not None:
        sup_dir = ctx.state.root / "supervision"
        if sup_dir.is_dir():
            for path in sorted(sup_dir.glob("*-supervise.json"))[-5:]:
                data = read_json(path, {}) or {}
                supervision_reports.append(
                    {
                        "file": path.name,
                        "path": str(path),
                        "summary": str(data.get("summary", ""))[:160],
                    }
                )

    tasks_root = Path(bench_dir) if bench_dir is not None else (ctx.repo / "bench")
    bench_tasks: list[dict] = []
    if tasks_root.is_dir():
        for child in sorted(tasks_root.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or not (child / "task.toml").is_file():
                continue
            try:
                task = load_task(child)
            except Invalid as exc:
                bench_tasks.append({"id": child.name, "error": exc.message})
                continue
            bench_tasks.append(
                {
                    "id": task.id,
                    "language": task.language,
                    "category": task.category,
                    "has_script": task.script is not None,
                }
            )

    latest_bench: dict[str, Any] = {}
    candidate_bench_roots: list[Path] = []
    if bench_out is not None:
        candidate_bench_roots.append(Path(bench_out))
    if ctx.state is not None:
        candidate_bench_roots.append(ctx.state.root / "bench")
    candidate_bench_roots.append(ctx.repo / ".openfarm" / "bench")
    for root in candidate_bench_roots:
        if not root.is_dir():
            continue
        reports = sorted(root.glob("**/bench-report.json"))
        if reports:
            rep = read_json(reports[-1], {}) or {}
            md_path = reports[-1].with_name("bench-report.md")
            md_text = (
                md_path.read_text(encoding="utf-8", errors="replace")
                if md_path.is_file()
                else (render_report(rep.get("summary", {})) if rep.get("summary") else "")
            )
            latest_bench = {
                "path": str(reports[-1]),
                "dry_run": bool(rep.get("dry_run", False)),
                "summary": rep.get("summary", {}),
                "markdown": md_text,
            }
            break

    return {
        "configured": ctx.configured,
        "config_error": ctx.config_error,
        "repo": str(ctx.repo),
        "acp": acp_info,
        "supervision_reports": supervision_reports,
        "bench_tasks": bench_tasks,
        "latest_bench": latest_bench,
        "pre_config_helpers": ["init", "adopt", "install-skill"],
    }


def collect_snapshot(
    repo: Path | str,
    config_path: Path | str | None = None,
    view: str = "overview",
    run_id: str | None = None,
    *,
    card_id: str | None = None,
    attempt: int | None = None,
    board_filter: dict[str, str] | None = None,
    harness_global: bool = False,
    harness_entry: str | None = None,
    acp_home: Path | None = None,
    claude_bin: str | None = None,
    acp_bin: str | None = None,
    include_cli_auth: bool = False,
    bench_dir: Path | None = None,
    bench_out: Path | None = None,
) -> dict:
    """Collect structured snapshot state for one TUI view."""
    if view not in VIEWS:
        raise UsageError(f"unknown view {view!r}; choose from {', '.join(VIEWS)}")
    ctx = _load_tui_context(repo, config_path)
    overview = _collect_overview(ctx, run_id=run_id)
    if view == "overview":
        data = overview
    elif view == "cards":
        data = _collect_cards(ctx)
    elif view == "dossier":
        data = _collect_dossier(ctx, card_id=card_id, run_id=run_id, attempt=attempt)
    elif view == "board":
        data = _read_board_data(ctx, run_id=run_id, board_filter=board_filter)
    elif view == "swarm":
        data = _collect_swarm(ctx, run_id=run_id)
    elif view == "harness":
        data = _collect_harness(ctx, global_mode=harness_global, entry_id=harness_entry)
    else:
        data = _collect_ops(
            ctx,
            acp_home=acp_home,
            claude_bin=claude_bin,
            acp_bin=acp_bin,
            include_cli_auth=include_cli_auth,
            bench_dir=bench_dir,
            bench_out=bench_out,
        )

    return {
        "view": view,
        "repo": str(ctx.repo),
        "configured": ctx.configured,
        "config_error": ctx.config_error,
        "status_bar": {
            "project": overview.get("project", Path(ctx.repo).name),
            "active_run_id": overview.get("active_run_id", ""),
            "paused": overview.get("paused", False),
            "kill_switch": overview.get("kill_switch", False),
            "breaker_tripped": bool((overview.get("breaker") or {}).get("tripped", False)),
            "swarm_enabled": bool((overview.get("swarm") or {}).get("enabled", False)),
        },
        "data": data,
    }


# -- Plain-text snapshot formatting ------------------------------------------


def _format_overview_lines(snap: dict) -> list[str]:
    d = snap["data"]
    if not d.get("configured"):
        lines = [
            f"Overview — {d['repo']} (not configured)",
            "No .openfarm/config.toml found." if not d.get("config_error") else f"Config error: {d['config_error']}",
            "Switch to [7] ops to run `init`, `adopt`, or `install-skill`, or press `:` for command bar.",
        ]
        return lines

    sw = d["swarm"]
    br = d["breaker"]
    lines = [
        f"Project: {d['project']}  lane={d['lane']}  base={d['base_branch']}  state_dir={d['state_dir']}",
        f"Live run: {d['active_run_id'] or '(none)'}  paused={d['paused']}  kill_switch={d['kill_switch']}  breaker_tripped={bool(br.get('tripped'))}",
        f"Caps: spend_per_night={d['spend_cap']}  concurrent_cards={d['concurrent_cards']}  concurrent_builds={d['concurrent_builds']}",
        f"Model: adapter={d['model']['adapter']}  endpoint={d['model']['endpoint'] or '—'}  key_file_present={d['model']['key_file_present']}",
        f"Swarm: enabled={sw['enabled']}  coordinator={sw['coordinator']} ({sw['coordinator_effort']})  supervisor={sw['supervisor']} ({sw['supervisor_effort']})  default_tier={sw['default_tier']}",
    ]
    if d["tiers"]:
        lines.append("Tiers:")
        for t in d["tiers"]:
            lines.append(
                f"  - {t['name']}: model={t['model']}  efforts={','.join(t['efforts']) or 'none'}  param={t['effort_param'] or '—'}"
            )
    lr = d.get("last_run") or {}
    if lr:
        lines.append(
            f"Last run: {lr.get('run_id')}  finished={lr.get('finished_at')}  spend={lr.get('spend')}  outcomes={json.dumps(lr.get('counts', {}), sort_keys=True)}"
        )
        tok = lr.get("tokens") or {}
        if tok:
            lines.append(
                f"Tokens: total={tok.get('total', 0)}  worker={tok.get('worker', 0)}  supervisor={tok.get('supervisor', 0)}"
            )
            if tok.get("by_member"):
                lines.append(f"  by_member: {json.dumps(tok['by_member'], sort_keys=True)}")
            if tok.get("by_effort"):
                lines.append(f"  by_effort: {json.dumps(tok['by_effort'], sort_keys=True)}")
    else:
        lines.append("Last run: (none)")
    h = d.get("harness") or {}
    lines.append(
        f"Harness: enabled={h.get('enabled', False)}  rev={h.get('revision', 0)}  state={h.get('state', 'n/a')}  active={h.get('active_count', 0)}/{h.get('total_entries', 0)}"
    )
    lines.append("Actions: [p]ause  [u]resume  [x]stop  [n]run")
    return lines


def _format_cards_lines(snap: dict) -> list[str]:
    d = snap["data"]
    if not d.get("configured"):
        return ["Cards — repository not configured."]
    cards = d.get("cards") or []
    lines = [f"Cards ({len(cards)})  source={d.get('source_mode')}  base={d.get('base_branch')}"]
    if not cards:
        lines.append("  (no cards)")
    for c in cards:
        deps = ",".join(c["deps"]) or "—"
        col = ",".join(c["collides_with"]) or "none"
        br = c["branches"][0] if c["branches"] else "—"
        lines.append(
            f"  [{c['state']}] {c['id']}  {c['title']}  cat={c['category'] or '—'}  unit={c['unit'] or '.'}  deps={deps}  collides={col}  branch={br} (unmerged={c['unmerged_commits']})"
        )
        if c["diff_preview"]:
            preview_head = c["diff_preview"].splitlines()[:6]
            for diff_ln in preview_head:
                lines.append(f"    diff: {diff_ln[:110]}")
    lines.append("Actions: [c]heck  [b]aseline check  [d]one  [l]release  [v]erdict  [e]scape  [Enter]dossier")
    return lines


def _format_dossier_lines(snap: dict) -> list[str]:
    d = snap["data"]
    if d.get("error"):
        return [f"Dossier: {d['error']}"]
    lines = [f"Dossier: {d.get('card')}  run={d.get('run')}" + (f"  attempt={d.get('attempt')}" if d.get("attempt") is not None else "")]
    lines.extend((d.get("markdown") or "").splitlines())
    lines.append("Actions: [[]/[]]card  [{/}]run  [a]attempt  [m]toggle md/json")
    return lines


def _format_board_lines(snap: dict) -> list[str]:
    d = snap["data"]
    lines = [
        f"Board — run={d['run_id'] or '(none)'} ({d['run_source']})  ended={d['ended']}  posts={d['total_posts']}  last_seq={d['last_seq']}"
    ]
    if d.get("waiting_members"):
        waits = ", ".join(
            f"{m['name']}({m['role']} since {m['wait_since'] or '?'})"
            for m in d["waiting_members"]
        )
        lines.append(f"Waiting: {waits}")
    else:
        lines.append("Waiting: (none)")
    if d.get("unanswered_questions"):
        qs = ", ".join(f"#{q['seq']}->{','.join(q['to'])}" for q in d["unanswered_questions"])
        lines.append(f"Unanswered questions: {qs}")
    posts = d.get("posts") or []
    if not posts:
        lines.append("  (no board posts)")
    for p in posts:
        to_txt = ",".join(p["to"]) or "—"
        card_txt = f" [{p['card']}]" if p["card"] else ""
        ref_txt = f" ref=#{p['ref']}" if p["ref"] is not None else ""
        ev_txt = f" ev={','.join(p['evidence'])}" if p["evidence"] else ""
        lines.append(
            f"  #{p['seq']} {p['at']} {p['from']} -> {to_txt} ({p['kind']}{card_txt}{ref_txt}): {p['body_preview']}{ev_txt}"
        )
    lines.append("Actions: [f]ilter  [v]iew evidence  [n]post  [a]nswer  [w]ait check")
    return lines


def _format_swarm_lines(snap: dict) -> list[str]:
    d = snap["data"]
    sw = d["swarm"]
    lines = [
        f"Swarm — run={d['run_id'] or '(none)'}  enabled={sw['enabled']}  coordinator={sw['coordinator']}  supervisor={sw['supervisor']}",
        f"Policy: on_external_timeout={sw['on_external_timeout']}  external_wait_s={sw['external_wait_s']}  stall_after_s={sw['stall_after_s']}  escalate_after={sw['escalate_after']}  max_members={sw['max_members']}",
        "Tiers:",
    ]
    if d["tiers"]:
        for t in d["tiers"]:
            lines.append(f"  {t['name']}: model={t['model']}  efforts={','.join(t['efforts']) or 'none'}  param={t['effort_param'] or '—'}")
    else:
        lines.append("  (no tiers configured)")
    lines.append(f"Roster ({len(d['members'])}):")
    if not d["members"]:
        lines.append("  (no members)")
    for m in d["members"]:
        lines.append(
            f"  {m['name']}  kind={m['kind']}  role={m['role']}  card={m['card'] or '—'}  tier={m['tier'] or '—'}  effort={m['effort'] or '—'}  state={m['state']}  wait_since={m['wait_since'] or '—'}  tokens={m['tokens_total']}  attempts={m['attempts']}"
        )
    lines.append("Actions: [J]oin  [S]tart  [E]set  [H]int  [B]lock  [X]remove")
    return lines


def _format_harness_lines(snap: dict) -> list[str]:
    d = snap["data"]
    if not d.get("configured"):
        return ["Harness — repository not configured."]
    mode = "GLOBAL" if d["global_mode"] else "LOCAL"
    lines = [f"Harness ({mode})"]
    if d["global_mode"]:
        g = d["global"]
        lines.append(f"Global dir: {g.get('dir')}  active={g.get('active', 0)}  retired={g.get('retired', 0)}")
        for e in g.get("entries") or []:
            lines.append(f"  [{e.get('status')}] {e.get('id')} ({e.get('surface')}/{e.get('scope')}): {str(e.get('cue', ''))[:90]}")
    else:
        loc = d["local"]
        m = loc.get("metrics") or {}
        lines.append(
            f"Local dir: {loc.get('dir', '—')}  enabled={loc.get('enabled')}  rev={loc.get('revision', 0)}  state={loc.get('state')}  pass_rate={m.get('pass_rate', 0.0)}  efficiency={m.get('harness_efficiency', 0.0)}"
        )
        for e in loc.get("entries") or []:
            st = e.get("stats") or {}
            lines.append(
                f"  [{e.get('status')}] {e.get('id')} ({e.get('surface')}/{e.get('role', '*')}) +{st.get('helped', 0)}/-{st.get('hurt', 0)}: {str(e.get('cue', ''))[:80]}"
            )
    if d.get("selected_entry"):
        lines.append("Selected entry detail:")
        lines.append(json.dumps(d["selected_entry"], indent=2, ensure_ascii=False))
    lines.append("Actions: [g]lobal toggle  [i]ndex  [Enter]show  [+]promote  [-]demote  [R]eset --yes")
    return lines


def _format_ops_lines(snap: dict) -> list[str]:
    d = snap["data"]
    acp = d.get("acp") or {}
    lines = [
        f"Ops — repo={d['repo']}  configured={d['configured']}",
        f"ACP: transport={acp.get('resolved_transport', 'missing')}  claude_cli={acp.get('claude_cli') or 'none'}  agent_acp={acp.get('claude_agent_acp') or 'none'}  oauth_token_env={acp.get('oauth_token_env', False)}",
    ]
    if "repo_frontier" in acp:
        rf = acp["repo_frontier"]
        rw = acp["repo_workers"]
        lines.append(
            f"  Frontier: adapter={rf['adapter']}  mode={rf['mode']}  auth={rf['auth']}  model={rf['model'] or 'default'}  effort={rf['effort']}"
        )
        lines.append(
            f"  Workers: adapter={rw['adapter']}  endpoint={rw['endpoint'] or '—'}  key_file_present={rw['key_file_present']}"
        )
    lines.append(f"Bench tasks ({len(d.get('bench_tasks') or [])}):")
    for task in d.get("bench_tasks") or []:
        if "error" in task:
            lines.append(f"  ! {task['id']}: {task['error']}")
        else:
            lines.append(f"  - {task['id']} ({task['language']}/{task['category']}) script={task['has_script']}")
    if d.get("latest_bench"):
        lb = d["latest_bench"]
        lines.append(f"Latest bench report: {lb.get('path')}")
        for ln in str(lb.get("markdown", "")).splitlines():
            lines.append(f"  {ln}")
    if d.get("supervision_reports"):
        lines.append("Recent supervise reports:")
        for rep in d["supervision_reports"]:
            lines.append(f"  - {rep['file']}: {rep['summary']}")
    lines.append("Pre-config helpers: init, adopt, install-skill")
    lines.append("Actions: [a]cp status/probe/configure  [s]upervise  [b]ench list/dry-run  [I]nit  [A]dopt  [K]install-skill")
    return lines


_FORMATTERS = {
    "overview": _format_overview_lines,
    "cards": _format_cards_lines,
    "dossier": _format_dossier_lines,
    "board": _format_board_lines,
    "swarm": _format_swarm_lines,
    "harness": _format_harness_lines,
    "ops": _format_ops_lines,
}


def render_snapshot(
    repo: Path | str,
    config_path: Path | str | None = None,
    view: str = "overview",
    run_id: str | None = None,
    as_json: bool = False,
    **kwargs,
) -> str:
    """Render a deterministic headless snapshot for one view (text or JSON)."""
    snap = collect_snapshot(repo, config_path=config_path, view=view, run_id=run_id, **kwargs)
    if as_json:
        return json.dumps(snap, indent=2, ensure_ascii=False)
    sb = snap["status_bar"]
    tabs = "  ".join(
        f"[{idx}] {VIEW_LABELS[v].upper() if v == view else VIEW_LABELS[v]}"
        for idx, v in enumerate(VIEWS, start=1)
    )
    badges = []
    if sb["active_run_id"]:
        badges.append(f"run:{sb['active_run_id']}")
    if sb["paused"]:
        badges.append("PAUSED")
    if sb["kill_switch"]:
        badges.append("KILL")
    if sb["breaker_tripped"]:
        badges.append("BREAKER")
    if sb["swarm_enabled"]:
        badges.append("SWARM")
    header = f"openfarm tui | {sb['project']}" + (f" [{' '.join(badges)}]" if badges else "")
    lines = [header, tabs, "-" * min(100, max(len(header), len(tabs))), *_FORMATTERS[view](snap)]
    return "\n".join(lines)


# -- Evidence reader & checked fallback board/swarm actions ------------------


def read_evidence_file(
    repo: Path | str,
    config_path: Path | str | None = None,
    path: str = "",
    run_id: str | None = None,
) -> dict:
    """Safely read an evidence file strictly inside ``state_dir``."""
    ctx = _load_tui_context(repo, config_path)
    if not ctx.configured or ctx.state is None:
        return {"ok": False, "refused": True, "reason": "repository is not configured"}
    if not path or "\x00" in path:
        return {"ok": False, "refused": True, "reason": "invalid evidence path"}
    resolved_run, _ = _resolve_run_id(ctx.state, run_id)
    raw = Path(path)
    if raw.is_absolute():
        candidate = raw.resolve()
    elif resolved_run:
        candidate = (ctx.state.run_dir(resolved_run) / "board" / raw).resolve()
        if not candidate.exists():
            candidate = (ctx.state.root / raw).resolve()
    else:
        candidate = (ctx.state.root / raw).resolve()

    state_root = ctx.state.root.resolve()
    if not candidate.is_relative_to(state_root):
        return {"ok": False, "refused": True, "reason": f"evidence path escapes state_dir: {path}"}
    if _is_secret_file(ctx, candidate):
        return {"ok": False, "refused": True, "reason": "cannot read secret key_file"}
    if not candidate.is_file():
        return {"ok": False, "refused": True, "reason": f"evidence file not found: {path}"}
    return {"ok": True, "path": str(candidate), "text": candidate.read_text(encoding="utf-8", errors="replace")}


def _fallback_validate_expects(expects: dict | None, data: object) -> dict:
    if not isinstance(data, dict):
        raise Refused("invalid-answer", "answer payload must be a JSON object")
    if not expects or not isinstance(expects, dict):
        return data
    required = expects.get("required") or ()
    for key in required:
        if key not in data:
            raise Refused("invalid-answer", f"missing required field {key!r}")
    allowed_verdicts = expects.get("verdicts") or expects.get("allowed_verdicts")
    if allowed_verdicts and data.get("verdict") not in allowed_verdicts:
        raise Refused(
            "invalid-answer",
            f"verdict {data.get('verdict')!r} not in {list(allowed_verdicts)}",
        )
    props = expects.get("properties") or expects.get("fields") or {}
    if isinstance(props, dict):
        for key, spec in props.items():
            if key not in data:
                continue
            val = data[key]
            if isinstance(spec, dict):
                enum = spec.get("enum") or spec.get("choices")
                if enum and val not in enum:
                    raise Refused("invalid-answer", f"field {key!r}: {val!r} not in {list(enum)}")
                expected_type = spec.get("type")
                type_map = {
                    "str": str,
                    "string": str,
                    "int": int,
                    "integer": int,
                    "float": (int, float),
                    "number": (int, float),
                    "bool": bool,
                    "boolean": bool,
                    "list": list,
                    "array": list,
                    "dict": dict,
                    "object": dict,
                }
                if expected_type in type_map:
                    py_type = type_map[expected_type]
                    if expected_type in ("int", "integer", "float", "number") and isinstance(val, bool):
                        raise Refused("invalid-answer", f"field {key!r} cannot be boolean")
                    if not isinstance(val, py_type):
                        raise Refused("invalid-answer", f"field {key!r} must be {expected_type}")
    return data


def _ensure_board_dir(state: StateDir, run_id: str) -> Path:
    board_dir = state.run_dir(run_id) / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "evidence").mkdir(parents=True, exist_ok=True)
    if not (board_dir / "state.json").exists():
        write_json(board_dir / "state.json", {"ended": False, "ended_at": "", "reason": ""})
    if not (board_dir / "members.json").exists():
        write_json(board_dir / "members.json", {})
    return board_dir


def _fallback_board_post(
    ctx: TuiContext,
    *,
    run_id: str,
    author: str,
    to: list[str],
    kind: str,
    body: str,
    card: str = "",
    ref: int | None = None,
) -> dict:
    board_mod = _optional_module("openfarm_core.board")
    board_dir = _ensure_board_dir(ctx.state, run_id)
    if board_mod is not None and hasattr(board_mod, "Board"):
        board = board_mod.Board(board_dir, state_dir=ctx.state.root)
        board.ensure()
        return board.post(author=author, to=to, kind=kind, body=body, card=card, ref=ref)

    if kind not in VALID_POST_KINDS:
        raise Refused("bad-post-kind", f"invalid post kind {kind!r}")
    members = read_json(board_dir / "members.json", {}) or {}
    author_rec = members.get(author) if isinstance(members, dict) else None
    author_kind = str((author_rec or {}).get("kind", "external"))
    if kind in ("done", "gate") and author_kind != "code":
        raise Refused("reserved-kind", f"post kind {kind!r} is reserved for code members")

    cleaned_body = body if author_kind == "code" else sanitize_lesson_text(body, max_len=8000)
    existing = _read_board_posts_raw(board_dir)
    next_seq = (int(existing[-1]["seq"]) + 1) if existing else 1
    evidence: list[str] = []
    if len(cleaned_body) > 8000:
        ev_path = board_dir / "evidence" / f"post-{next_seq:04d}.txt"
        ev_path.write_text(cleaned_body, encoding="utf-8")
        evidence.append(str(ev_path))
        cleaned_body = cleaned_body[:8000]
    record = {
        "seq": next_seq,
        "at": utc_now(),
        "from": author,
        "to": list(dict.fromkeys([t for t in to if t])),
        "kind": kind,
        "card": card,
        "attempt": 0,
        "ref": ref,
        "body": cleaned_body,
        "evidence": evidence,
    }
    append_jsonl(board_dir / "board.jsonl", record)
    return record


def _fallback_board_answer(
    ctx: TuiContext,
    *,
    run_id: str,
    question_seq: int,
    as_name: str,
    data: object,
    body: str = "",
) -> dict:
    board_mod = _optional_module("openfarm_core.board")
    board_dir = _ensure_board_dir(ctx.state, run_id)
    if board_mod is not None and hasattr(board_mod, "Board"):
        board = board_mod.Board(board_dir, state_dir=ctx.state.root)
        board.ensure()
        return board.answer(question_seq, as_name=as_name, data=data, body=body)

    posts = _read_board_posts_raw(board_dir)
    question = next((p for p in posts if int(p.get("seq", 0)) == question_seq), None)
    if question is None or question.get("kind") != "question":
        raise Refused("bad-question", f"post #{question_seq} is not a question")
    if any(p.get("kind") == "answer" and p.get("ref") == question_seq for p in posts):
        raise Refused("already-answered", f"question #{question_seq} is already answered")

    members = read_json(board_dir / "members.json", {}) or {}
    member = members.get(as_name) if isinstance(members, dict) else None
    member_role = _normalize_role((member or {}).get("role", ""))
    to_norm = {_normalize_role(x) for x in (question.get("to") or [])}
    if as_name not in (question.get("to") or []) and "all" not in to_norm and (not member_role or member_role not in to_norm):
        raise Refused("not-addressee", f"{as_name!r} is not addressed on question #{question_seq}")

    validated = _fallback_validate_expects(question.get("expects"), data)
    ans_path = board_dir / "evidence" / f"answer-q{question_seq}.json"
    write_json(ans_path, validated)
    next_seq = (int(posts[-1]["seq"]) + 1) if posts else 1
    record = {
        "seq": next_seq,
        "at": utc_now(),
        "from": as_name,
        "to": [str(question.get("from", "supervisor"))],
        "kind": "answer",
        "card": str(question.get("card", "")),
        "attempt": int(question.get("attempt", 0) or 0),
        "ref": question_seq,
        "body": sanitize_lesson_text(body or json.dumps(validated, ensure_ascii=False), max_len=8000),
        "evidence": [str(ans_path)],
    }
    append_jsonl(board_dir / "board.jsonl", record)
    return record


def _dispatch_fallback_board(ctx: TuiContext, tokens: list[str]) -> dict:
    if not ctx.configured or ctx.state is None:
        return _cmd_result(2, "", "repository is not configured")
    parser = argparse.ArgumentParser(prog="openfarm board", add_help=False)
    sub = parser.add_subparsers(dest="board_op")
    p_read = sub.add_parser("read")
    p_read.add_argument("--as", dest="as_name", default="*")
    p_read.add_argument("--since", type=int, default=0)
    p_read.add_argument("--limit", type=int, default=20)
    p_read.add_argument("--seqs", default=None)
    p_read.add_argument("--status", action="store_true")
    p_read.add_argument("--run", default=None)

    p_post = sub.add_parser("post")
    p_post.add_argument("--as", dest="as_name", required=True)
    p_post.add_argument("--to", required=True)
    p_post.add_argument("--kind", default="note")
    p_post.add_argument("--card", default="")
    p_post.add_argument("--ref", type=int, default=None)
    p_post.add_argument("--body", default=None)
    p_post.add_argument("--file", default=None)
    p_post.add_argument("--run", default=None)

    p_ans = sub.add_parser("answer")
    p_ans.add_argument("seq", type=int)
    p_ans.add_argument("--as", dest="as_name", required=True)
    p_ans.add_argument("--file", default=None)
    p_ans.add_argument("--json-data", default=None)
    p_ans.add_argument("--body", default="")
    p_ans.add_argument("--run", default=None)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("--as", dest="as_name", required=True)
    p_wait.add_argument("--since", type=int, default=0)
    p_wait.add_argument("--timeout", default="0")
    p_wait.add_argument("--run", default=None)

    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(err_buf):
            args = parser.parse_args(tokens)
    except SystemExit:
        return _cmd_result(2, "", err_buf.getvalue().strip() or "invalid board command")

    run_id, _ = _resolve_run_id(ctx.state, getattr(args, "run", None))
    if not run_id:
        run_id = f"run-{utc_now().replace('-', '').replace(':', '')}"

    try:
        if args.board_op == "read":
            board_data = _read_board_data(ctx, run_id)
            if args.status:
                payload = {
                    "run_id": board_data["run_id"],
                    "posts": board_data["total_posts"],
                    "last_seq": board_data["last_seq"],
                    "ended": board_data["ended"],
                    "members": len(board_data["members"]),
                    "unanswered_questions": len(board_data["unanswered_questions"]),
                }
            else:
                posts = board_data["posts"]
                if args.seqs:
                    wanted = [int(s.strip()) for s in args.seqs.split(",") if s.strip()]
                    by_seq = {p["seq"]: p for p in posts}
                    posts = [by_seq[s] for s in wanted if s in by_seq]
                else:
                    posts = [p for p in posts if p["seq"] > args.since][: args.limit]
                payload = {"run_id": board_data["run_id"], "posts": posts}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

        if args.board_op == "post":
            if args.file:
                res = read_evidence_file(ctx.repo, ctx.config_path, args.file, run_id=run_id)
                if not res["ok"]:
                    return _cmd_result(1, "", res["reason"], data={"refused": True, "reason": res["reason"]})
                body = res["text"]
            else:
                body = args.body or ""
            to_list = [x.strip() for x in args.to.split(",") if x.strip()]
            rec = _fallback_board_post(
                ctx,
                run_id=run_id,
                author=args.as_name,
                to=to_list,
                kind=args.kind,
                body=body,
                card=args.card,
                ref=args.ref,
            )
            return _cmd_result(0, json.dumps(rec, indent=2, ensure_ascii=False), "", data=rec)

        if args.board_op == "answer":
            if args.file:
                f = Path(args.file)
                if _is_secret_file(ctx, f):
                    return _cmd_result(1, "", "refused: cannot read secret key_file")
                raw_json = f.read_text(encoding="utf-8")
            elif args.json_data is not None:
                raw_json = args.json_data
            else:
                return _cmd_result(2, "", "board answer requires --file or --json-data")
            parsed = json.loads(raw_json)
            rec = _fallback_board_answer(
                ctx,
                run_id=run_id,
                question_seq=args.seq,
                as_name=args.as_name,
                data=parsed,
                body=args.body,
            )
            return _cmd_result(0, json.dumps(rec, indent=2, ensure_ascii=False), "", data=rec)

        if args.board_op == "wait":
            board_data = _read_board_data(ctx, run_id)
            posts = [p for p in board_data["posts"] if p["seq"] > args.since]
            if posts:
                payload = {"woke": True, "posts": posts, "last_seq": posts[-1]["seq"]}
            elif board_data["ended"]:
                payload = {"woke": False, "reason": "run-ended", "posts": []}
            else:
                payload = {"woke": False, "reason": "timeout", "posts": []}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)
    except (Refused, Invalid, ValueError, OSError) as exc:
        msg = getattr(exc, "message", str(exc))
        return _cmd_result(1, json.dumps({"refused": True, "reason": msg}), msg, data={"refused": True, "reason": msg})

    return _cmd_result(2, "", "unknown board subcommand")


def _require_coordinator_seat(ctx: TuiContext, members: dict, caller: str) -> None:
    if not caller:
        raise Refused("not-coordinator", "caller (--as) is required and must hold the coordinator seat")
    member = members.get(caller) if isinstance(members, dict) else None
    if not isinstance(member, dict) or member.get("state") == "removed":
        raise Refused("not-coordinator", f"{caller!r} is not an active member holding the coordinator seat")
    if _normalize_role(member.get("role", "")) != "coordinator":
        raise Refused("not-coordinator", f"{caller!r} has role {member.get('role')!r}, not coordinator")


def _validate_tier_effort(cfg: Config, tier_name: str, effort: str) -> tuple[str, str]:
    tiers = getattr(cfg, "tiers", None) or {}
    if tiers:
        if tier_name not in tiers:
            raise Refused("bad-tier", f"unknown tier {tier_name!r}")
        tier = tiers[tier_name]
        allowed = tuple(getattr(tier, "efforts", ()) or ())
        if effort and allowed and effort not in allowed:
            raise Refused("bad-effort", f"effort {effort!r} is not allowed for tier {tier_name!r}")
        return str(getattr(tier, "model", "")), effort
    if tier_name and tier_name != "default":
        raise Refused("bad-tier", f"unknown tier {tier_name!r}")
    if effort and effort not in EFFORT_STEPS:
        raise Refused("bad-effort", f"invalid effort {effort!r}")
    impl = cfg.role_models.get("implementer") if cfg.role_models else None
    return str(getattr(impl, "model", "") or ""), effort


def _dispatch_fallback_swarm(ctx: TuiContext, tokens: list[str]) -> dict:
    if not ctx.configured or ctx.cfg is None or ctx.state is None:
        return _cmd_result(2, "", "repository is not configured")
    parser = argparse.ArgumentParser(prog="openfarm swarm", add_help=False)
    sub = parser.add_subparsers(dest="swarm_op")

    p_st = sub.add_parser("status")
    p_st.add_argument("--run", default=None)

    sub.add_parser("tiers")

    p_join = sub.add_parser("join")
    p_join.add_argument("--name", required=True)
    p_join.add_argument("--seat", required=True, choices=("coordinator", "supervisor"))
    p_join.add_argument("--kind", default="agent", choices=("human", "agent"))
    p_join.add_argument("--run", default=None)

    p_start = sub.add_parser("start")
    p_start.add_argument("card")
    p_start.add_argument("--team", default="")
    p_start.add_argument("--as", dest="as_name", default="")
    p_start.add_argument("--run", default=None)

    p_set = sub.add_parser("set")
    p_set.add_argument("member")
    p_set.add_argument("--tier", default="")
    p_set.add_argument("--effort", default="")
    p_set.add_argument("--as", dest="as_name", default="")
    p_set.add_argument("--run", default=None)

    p_hint = sub.add_parser("hint")
    p_hint.add_argument("member")
    p_hint.add_argument("--body", required=True)
    p_hint.add_argument("--as", dest="as_name", default="")
    p_hint.add_argument("--run", default=None)

    p_block = sub.add_parser("block")
    p_block.add_argument("card")
    p_block.add_argument("--reason", required=True)
    p_block.add_argument("--as", dest="as_name", default="")
    p_block.add_argument("--run", default=None)

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("member")
    p_rm.add_argument("--as", dest="as_name", default="")
    p_rm.add_argument("--run", default=None)

    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(err_buf):
            args = parser.parse_args(tokens)
    except SystemExit:
        return _cmd_result(2, "", err_buf.getvalue().strip() or "invalid swarm command")

    if args.swarm_op == "tiers":
        payload = {"tiers": _read_tiers_summary(ctx.cfg)}
        return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

    run_id, _ = _resolve_run_id(ctx.state, getattr(args, "run", None))
    if args.swarm_op == "status":
        payload = _collect_swarm(ctx, run_id=run_id)
        return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

    if not run_id:
        run_id = f"run-{utc_now().replace('-', '').replace(':', '')}"
    board_dir = _ensure_board_dir(ctx.state, run_id)
    members = read_json(board_dir / "members.json", {}) or {}
    swarm_cfg = _read_swarm_summary(ctx.cfg)

    try:
        if args.swarm_op == "join":
            seat_val = swarm_cfg.get(args.seat, "")
            if seat_val != "external":
                raise Refused(
                    "seat-not-external",
                    f"[swarm] {args.seat} is {seat_val!r}, not 'external'",
                )
            members[args.name] = {
                "name": args.name,
                "kind": "external",
                "role": args.seat,
                "card": "",
                "tier": "",
                "effort": "",
                "state": "active",
                "added_by": "external",
                "last_read_seq": 0,
                "wait_since": "",
                "tokens": {"in": 0, "out": 0},
                "attempts": 0,
                "owner_kind": args.kind,
            }
            write_json(board_dir / "members.json", members)
            _fallback_board_post(
                ctx,
                run_id=run_id,
                author=args.name,
                to=["all"],
                kind="roster",
                body=f"{args.name} joined seat {args.seat} ({args.kind})",
            )
            payload = {"joined": True, "member": members[args.name], "run_id": run_id}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

        _require_coordinator_seat(ctx, members, getattr(args, "as_name", ""))

        if args.swarm_op == "start":
            cards, _ = _read_cards_readonly(ctx)
            card = next((c for c in cards if c.id == args.card), None)
            if card is None:
                raise Refused("unknown-card", f"no card {args.card!r}")
            if card.state != "ready":
                raise Refused("not-ready", f"{card.id} is {card.state}, not ready")
            running_cards = {
                c.id: c for c in cards if c.state == "in_progress"
            }
            for running in running_cards.values():
                if _cards_collide(card, running):
                    raise Refused("card-collision", f"{card.id} collides with {running.id}")
            team_spec: dict[str, dict[str, str]] = {}
            if args.team:
                for item in args.team.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    role_part, sep, spec_part = item.partition("=")
                    if not sep:
                        raise Refused("bad-team", f"expected role=tier:effort, got {item!r}")
                    tier_n, _, eff_n = spec_part.partition(":")
                    model_n, eff_checked = _validate_tier_effort(
                        ctx.cfg, tier_n.strip() or swarm_cfg["default_tier"], eff_n.strip()
                    )
                    team_spec[_normalize_role(role_part)] = {
                        "tier": tier_n.strip() or swarm_cfg["default_tier"],
                        "effort": eff_checked,
                        "model": model_n,
                    }
            rec = _fallback_board_post(
                ctx,
                run_id=run_id,
                author=args.as_name,
                to=["all"],
                kind="action",
                card=card.id,
                body=f"start {card.id} team={json.dumps(team_spec, sort_keys=True)}",
            )
            payload = {"action": "start", "card": card.id, "team": team_spec, "post": rec}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

        if args.swarm_op == "set":
            target = members.get(args.member)
            if not isinstance(target, dict) or target.get("state") == "removed":
                raise Refused("unknown-member", f"member {args.member!r} not found")
            new_tier = args.tier or str(target.get("tier") or swarm_cfg["default_tier"])
            new_effort = args.effort or str(target.get("effort") or "")
            model_n, eff_checked = _validate_tier_effort(ctx.cfg, new_tier, new_effort)
            target["tier"] = new_tier
            target["effort"] = eff_checked
            if model_n:
                target["model"] = model_n
            write_json(board_dir / "members.json", members)
            rec = _fallback_board_post(
                ctx,
                run_id=run_id,
                author=args.as_name,
                to=[args.member],
                kind="action",
                card=str(target.get("card", "")),
                body=f"set {args.member} tier={new_tier} effort={eff_checked}",
            )
            payload = {"action": "set", "member": target, "post": rec}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

        if args.swarm_op == "hint":
            target = members.get(args.member)
            if not isinstance(target, dict) or target.get("state") == "removed":
                raise Refused("unknown-member", f"member {args.member!r} not found")
            rec = _fallback_board_post(
                ctx,
                run_id=run_id,
                author=args.as_name,
                to=[args.member],
                kind="note",
                card=str(target.get("card", "")),
                body=args.body,
            )
            payload = {"action": "hint", "member": args.member, "post": rec}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

        if args.swarm_op == "block":
            rec = _fallback_board_post(
                ctx,
                run_id=run_id,
                author=args.as_name,
                to=["all"],
                kind="blocked",
                card=args.card,
                body=f"blocked {args.card}: {args.reason}",
            )
            payload = {"action": "block", "card": args.card, "reason": args.reason, "post": rec}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)

        if args.swarm_op == "remove":
            target = members.get(args.member)
            if not isinstance(target, dict):
                raise Refused("unknown-member", f"member {args.member!r} not found")
            target["state"] = "removed"
            write_json(board_dir / "members.json", members)
            payload = {"action": "remove", "member": args.member, "state": "removed"}
            return _cmd_result(0, json.dumps(payload, indent=2, ensure_ascii=False), "", data=payload)
    except (Refused, Invalid, _CfgErr, ValueError) as exc:
        msg = getattr(exc, "message", str(exc))
        return _cmd_result(1, json.dumps({"refused": True, "reason": msg}), msg, data={"refused": True, "reason": msg})

    return _cmd_result(2, "", "unknown swarm subcommand")


def _cmd_result(exit_code: int, stdout: str, stderr: str, data: object = None) -> dict:
    combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part).strip()
    if data is None and stdout.strip():
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None
    return {
        "ok": exit_code == 0,
        "exit_code": int(exit_code),
        "stdout": stdout,
        "stderr": stderr,
        "output": combined,
        "data": data,
    }


def dispatch_cli_command(
    repo: Path | str,
    config_path: Path | str | None = None,
    tokens: list[str] | str | None = None,
) -> dict:
    """Execute an ``openfarm`` CLI command in-process and capture its output."""
    if tokens is None:
        return _cmd_result(2, "", "empty command")
    if isinstance(tokens, str):
        try:
            argv_tokens = shlex.split(tokens)
        except ValueError as exc:
            return _cmd_result(2, "", f"parse error: {exc}")
    else:
        argv_tokens = [str(t) for t in tokens if str(t) != ""]

    if argv_tokens and argv_tokens[0] == "openfarm":
        argv_tokens = argv_tokens[1:]
    if not argv_tokens:
        return _cmd_result(2, "", "empty command")

    cmd_name = argv_tokens[0]
    if cmd_name == "tui" and "--once" not in argv_tokens:
        return _cmd_result(1, "", "cannot launch nested interactive tui from command bar")

    from . import cli

    known_cli = set(getattr(cli, "COMMANDS", {})) | set(getattr(cli, "PRE_CONFIG_COMMANDS", {}))
    ctx = _load_tui_context(repo, config_path)
    if cmd_name == "board" and "board" not in known_cli:
        return _dispatch_fallback_board(ctx, argv_tokens[1:])
    if cmd_name == "swarm" and "swarm" not in known_cli:
        return _dispatch_fallback_swarm(ctx, argv_tokens[1:])

    resolved_repo = Path(repo).resolve()
    full_argv: list[str] = []
    if "--repo" not in argv_tokens and not any(t.startswith("--repo=") for t in argv_tokens):
        full_argv.extend(["--repo", str(resolved_repo)])
    if (
        config_path is not None
        and "--config" not in argv_tokens
        and not any(t.startswith("--config=") for t in argv_tokens)
    ):
        full_argv.extend(["--config", str(config_path)])
    full_argv.extend(argv_tokens)

    if cmd_name == "adopt" and "--dir" not in argv_tokens and not any(t.startswith("--dir=") for t in argv_tokens):
        full_argv.extend(["--dir", str(resolved_repo)])
    if (
        cmd_name == "bench"
        and "--dir" not in argv_tokens
        and not any(t.startswith("--dir=") for t in argv_tokens)
        and (resolved_repo / "bench").is_dir()
    ):
        full_argv.extend(["--dir", str(resolved_repo / "bench")])

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 0
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            exit_code = int(cli.main(full_argv))
    except SystemExit as exc:
        code = exc.code
        exit_code = int(code) if isinstance(code, int) else (0 if code is None else 2)
    except Exception as exc:  # captured so interactive TUI never crashes
        err_buf.write(f"error: {exc}\n")
        exit_code = 3
    return _cmd_result(exit_code, out_buf.getvalue(), err_buf.getvalue())


# -- Headless & curses interactive controller --------------------------------


class TuiController:
    """Stateful controller for navigation, forms, command bar, and modals."""

    def __init__(
        self,
        repo: Path | str,
        config_path: Path | str | None = None,
        *,
        run_id: str | None = None,
        view: str = "overview",
        refresh_s: float = 1.0,
    ) -> None:
        if view not in VIEWS:
            raise UsageError(f"unknown view {view!r}; choose from {', '.join(VIEWS)}")
        self.repo = Path(repo).resolve()
        self.config_path = Path(config_path) if config_path else None
        self.run_id = run_id
        self.view = view
        self.refresh_s = float(refresh_s)
        self.scroll = 0
        self.selected_index = 0
        self.card_id: str | None = None
        self.attempt: int | None = None
        self.dossier_json_mode = False
        self.board_filter: dict[str, str] = {}
        self.harness_global = False
        self.harness_entry: str | None = None
        self.help_open = False
        self.modal_title = ""
        self.modal_lines: list[str] = []
        self.modal_scroll = 0
        self.status_message = ""
        self.last_command_result: dict | None = None
        self.snapshot: dict = {}
        self.refresh()

    def refresh(self) -> dict:
        self.snapshot = collect_snapshot(
            self.repo,
            config_path=self.config_path,
            view=self.view,
            run_id=self.run_id,
            card_id=self.card_id,
            attempt=self.attempt,
            board_filter=self.board_filter,
            harness_global=self.harness_global,
            harness_entry=self.harness_entry,
        )
        return self.snapshot

    def set_view(self, view: str) -> None:
        if view not in VIEWS:
            raise UsageError(f"unknown view {view!r}")
        self.view = view
        self.scroll = 0
        self.selected_index = 0
        self.refresh()

    def cycle_view(self, step: int = 1) -> None:
        idx = (VIEWS.index(self.view) + step) % len(VIEWS)
        self.set_view(VIEWS[idx])

    def execute_command(self, line: str) -> dict:
        res = dispatch_cli_command(self.repo, self.config_path, line)
        self.last_command_result = res
        title = f": {line} (exit {res['exit_code']})"
        body = res["output"] or "(no output)"
        self.open_modal(title, body.splitlines())
        self.status_message = f"cmd exit={res['exit_code']}"
        self.refresh()
        return res

    def open_modal(self, title: str, lines: list[str]) -> None:
        self.modal_title = title
        self.modal_lines = list(lines) or ["(empty)"]
        self.modal_scroll = 0

    def close_modal(self) -> None:
        self.modal_title = ""
        self.modal_lines = []
        self.modal_scroll = 0

    def view_evidence(self, path: str) -> dict:
        res = read_evidence_file(self.repo, self.config_path, path, run_id=self.run_id)
        if res["ok"]:
            self.open_modal(f"Evidence: {path}", res["text"].splitlines())
        else:
            self.open_modal(f"Evidence refused: {path}", [res["reason"]])
        return res

    # -- View action helpers --------------------------------------------------

    def _selected_card_id(self, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        if self.view == "cards":
            cards = (self.snapshot.get("data") or {}).get("cards") or []
            if cards:
                idx = min(max(0, self.selected_index), len(cards) - 1)
                return str(cards[idx].get("id", ""))
        return self.card_id or ""

    def action_pause(self, reason: str = "") -> dict:
        argv = ["pause"] + (["--reason", reason] if reason else [])
        return self.execute_command(shlex.join(argv))

    def action_resume(self, *, clear_breaker: bool = False, clear_kill: bool = False) -> dict:
        argv = ["resume"]
        if clear_breaker:
            argv.append("--clear-breaker")
        if clear_kill:
            argv.append("--clear-kill")
        return self.execute_command(shlex.join(argv))

    def action_stop(self, reason: str = "") -> dict:
        argv = ["stop"] + (["--reason", reason] if reason else [])
        return self.execute_command(shlex.join(argv))

    def action_launch_run(
        self,
        *,
        dry_run: bool = False,
        card: str = "",
        cards: int = 3,
        spend: float | None = None,
        script: str = "",
        mm_script: str = "",
    ) -> dict:
        argv = ["run", "--cards", str(int(cards))]
        if dry_run:
            argv.append("--dry-run")
        if card:
            argv.extend(["--card", card])
        if spend is not None:
            argv.extend(["--spend", str(spend)])
        if script:
            argv.extend(["--script", script])
        if mm_script:
            argv.extend(["--mm-script", mm_script])
        return self.execute_command(shlex.join(argv))

    def action_check(self, card_id: str | None = None, *, baseline: bool = False) -> dict:
        cid = self._selected_card_id(card_id)
        if not cid:
            return _cmd_result(2, "", "no card selected")
        argv = ["check", cid] + (["--baseline"] if baseline else [])
        return self.execute_command(shlex.join(argv))

    def action_done(self, card_id: str | None = None, *, note: str = "") -> dict:
        cid = self._selected_card_id(card_id)
        if not cid:
            return _cmd_result(2, "", "no card selected")
        argv = ["done", cid] + (["--note", note] if note else [])
        return self.execute_command(shlex.join(argv))

    def action_release(self, card_id: str | None = None, *, reason: str = "manual") -> dict:
        cid = self._selected_card_id(card_id)
        if not cid:
            return _cmd_result(2, "", "no card selected")
        return self.execute_command(shlex.join(["release", cid, "--reason", reason or "manual"]))

    def action_verdict(
        self,
        verdict: str,
        *,
        card_id: str | None = None,
        review_tokens: int = 0,
        minutes: float | None = None,
        note: str = "",
    ) -> dict:
        cid = self._selected_card_id(card_id)
        if not cid:
            return _cmd_result(2, "", "no card selected")
        argv = ["verdict", cid, verdict, "--review-tokens", str(int(review_tokens))]
        if minutes is not None:
            argv.extend(["--minutes", str(minutes)])
        if note:
            argv.extend(["--note", note])
        return self.execute_command(shlex.join(argv))

    def action_escape(self, note: str, *, card_id: str | None = None) -> dict:
        cid = self._selected_card_id(card_id)
        if not cid:
            return _cmd_result(2, "", "no card selected")
        return self.execute_command(shlex.join(["escape", cid, "--note", note]))

    def action_board_post(
        self,
        *,
        as_name: str,
        to: str,
        body: str,
        kind: str = "note",
        card: str = "",
        ref: int | None = None,
    ) -> dict:
        argv = ["board", "post", "--as", as_name, "--to", to, "--kind", kind, "--body", body]
        if card:
            argv.extend(["--card", card])
        if ref is not None:
            argv.extend(["--ref", str(ref)])
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_board_answer(
        self,
        seq: int,
        *,
        as_name: str,
        json_data: str | dict,
        body: str = "",
    ) -> dict:
        import tempfile

        payload_text = (
            json_data if isinstance(json_data, str) else json.dumps(json_data, ensure_ascii=False)
        )
        tmp_dir = Path(tempfile.mkdtemp(prefix="openfarm-tui-ans-"))
        ans_path = tmp_dir / "answer.json"
        try:
            ans_path.write_text(payload_text, encoding="utf-8")
            argv = [
                "board",
                "answer",
                str(int(seq)),
                "--as",
                as_name,
                "--file",
                str(ans_path),
            ]
            if body:
                argv.extend(["--body", body])
            if self.run_id:
                argv.extend(["--run", self.run_id])
            return self.execute_command(shlex.join(argv))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def action_board_wait(self, *, as_name: str, since: int = 0, timeout: str = "0") -> dict:
        argv = ["board", "wait", "--as", as_name, "--since", str(int(since)), "--timeout", timeout]
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_swarm_join(self, *, name: str, seat: str, kind: str = "agent") -> dict:
        argv = ["swarm", "join", "--name", name, "--seat", seat, "--kind", kind]
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_swarm_start(self, card: str, *, as_name: str = "", team: str = "") -> dict:
        argv = ["swarm", "start", card]
        if team:
            argv.extend(["--team", team])
        if as_name:
            argv.extend(["--as", as_name])
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_swarm_set(self, member: str, *, as_name: str = "", tier: str = "", effort: str = "") -> dict:
        argv = ["swarm", "set", member]
        if tier:
            argv.extend(["--tier", tier])
        if effort:
            argv.extend(["--effort", effort])
        if as_name:
            argv.extend(["--as", as_name])
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_swarm_hint(self, member: str, *, body: str, as_name: str = "") -> dict:
        argv = ["swarm", "hint", member, "--body", body]
        if as_name:
            argv.extend(["--as", as_name])
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_swarm_block(self, card: str, *, reason: str, as_name: str = "") -> dict:
        argv = ["swarm", "block", card, "--reason", reason]
        if as_name:
            argv.extend(["--as", as_name])
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_swarm_remove(self, member: str, *, as_name: str = "") -> dict:
        argv = ["swarm", "remove", member]
        if as_name:
            argv.extend(["--as", as_name])
        if self.run_id:
            argv.extend(["--run", self.run_id])
        return self.execute_command(shlex.join(argv))

    def action_harness(self, action: str, target: str = "", *, reason: str = "manual", yes: bool = False) -> dict:
        argv = ["harness", action]
        if target:
            argv.append(target)
        if self.harness_global and action in ("status", "promote", "demote"):
            argv.append("--global")
        if action in ("promote", "demote") and reason:
            argv.extend(["--reason", reason])
        if action == "reset" and yes:
            argv.append("--yes")
        return self.execute_command(shlex.join(argv))

    def lines(self) -> list[str]:
        if self.view == "dossier" and self.dossier_json_mode:
            payload = (self.snapshot.get("data") or {}).get("payload")
            if payload is not None:
                return json.dumps(payload, indent=2, ensure_ascii=False).splitlines()
        return _FORMATTERS[self.view](self.snapshot)

    def handle_key(self, key: int | str) -> bool:
        """Process one key event. Return False when the user requests quit."""
        if isinstance(key, str) and len(key) == 1:
            code = ord(key)
        elif isinstance(key, int):
            code = key
        else:
            return True

        if code == 27:  # Escape
            if self.help_open:
                self.help_open = False
            elif self.modal_lines:
                self.close_modal()
            return True

        if self.help_open:
            if code in (ord("?"), ord("q")):
                self.help_open = False
            return True

        if self.modal_lines:
            if code in (ord("q"), ord("\n"), ord("\r")):
                self.close_modal()
            elif code in (curses.KEY_DOWN, ord("j")):
                self.modal_scroll = min(max(0, len(self.modal_lines) - 1), self.modal_scroll + 1)
            elif code in (curses.KEY_UP, ord("k")):
                self.modal_scroll = max(0, self.modal_scroll - 1)
            elif code == curses.KEY_NPAGE:
                self.modal_scroll = min(max(0, len(self.modal_lines) - 1), self.modal_scroll + 10)
            elif code == curses.KEY_PPAGE:
                self.modal_scroll = max(0, self.modal_scroll - 10)
            return True

        if code == ord("q"):
            return False
        if code == ord("?"):
            self.help_open = True
            return True
        if code == ord("r"):
            self.refresh()
            self.status_message = "refreshed"
            return True
        if ord("1") <= code <= ord("7"):
            self.set_view(VIEWS[code - ord("1")])
            return True
        if code == ord("\t"):
            self.cycle_view(1)
            return True
        if code == getattr(curses, "KEY_BTAB", 353):
            self.cycle_view(-1)
            return True
        if code in (curses.KEY_DOWN, ord("j")):
            self.scroll += 1
            return True
        if code in (curses.KEY_UP, ord("k")):
            self.scroll = max(0, self.scroll - 1)
            return True
        if code == curses.KEY_NPAGE:
            self.scroll += 10
            return True
        if code == curses.KEY_PPAGE:
            self.scroll = max(0, self.scroll - 10)
            return True
        if self.view == "harness" and code == ord("g"):
            self.harness_global = not self.harness_global
            self.refresh()
            return True
        if self.view == "dossier" and code == ord("m"):
            self.dossier_json_mode = not self.dossier_json_mode
            self.scroll = 0
            return True
        return True


def _help_lines() -> list[str]:
    return [
        "openfarm TUI Help",
        "",
        "Global keys:",
        "  1..7         Switch view (overview, cards, dossier, board, swarm, harness, ops)",
        "  Tab / S-Tab  Next / previous view",
        "  j/k, Up/Down Scroll view or modal",
        "  r            Refresh",
        "  :            Command bar (run any `openfarm ...` command in-process)",
        "  ?            Toggle this help overlay",
        "  q / Esc      Close modal/help or quit",
        "",
        "Quick commands via `:` command bar:",
        "  status --json | pause --reason ... | resume [--clear-breaker] [--clear-kill] | stop",
        "  run --dry-run | check CARD [--baseline] | done CARD | release CARD --reason ...",
        "  verdict CARD clean --review-tokens 1000 | escape CARD --note ... | dossier CARD",
        "  board read | board post --as A --to B --body ... | board answer SEQ --as A --file ...",
        "  swarm join --name N --seat coordinator|supervisor | swarm start CARD --as N",
        "  harness status [--global] | harness index | harness show ID | harness reset --yes",
        "  acp status | acp probe | supervise --triage-blocked | bench list | init NAME | adopt",
    ]


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return
    avail = max_x - x - 1
    if avail <= 0:
        return
    with contextlib.suppress(curses.error):
        win.addnstr(y, x, text, avail, attr)


def _prompt_line(stdscr, prompt: str) -> str:
    max_y, max_x = stdscr.getmaxyx()
    y = max(0, max_y - 1)
    _safe_addstr(stdscr, y, 0, " " * max(1, max_x - 1))
    _safe_addstr(stdscr, y, 0, prompt)
    stdscr.refresh()
    curses.echo()
    stdscr.timeout(-1)
    try:
        raw = stdscr.getstr(y, min(len(prompt), max(0, max_x - 2)), max(1, max_x - len(prompt) - 2))
        return raw.decode("utf-8", errors="replace").strip()
    except curses.error:
        return ""
    finally:
        curses.noecho()


def _render_curses_frame(stdscr, ctrl: TuiController) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    sb = ctrl.snapshot.get("status_bar") or {}
    header = f" openfarm tui | {sb.get('project', '')} | view={ctrl.view}"
    if sb.get("active_run_id"):
        header += f" | run={sb['active_run_id']}"
    if sb.get("paused"):
        header += " [PAUSED]"
    if sb.get("kill_switch"):
        header += " [KILL]"
    if sb.get("breaker_tripped"):
        header += " [BREAKER]"
    _safe_addstr(stdscr, 0, 0, header.ljust(max(1, max_x - 1)), curses.A_REVERSE)

    tabs = " ".join(
        f"[{idx}:{VIEW_LABELS[v]}]" if v == ctrl.view else f" {idx}:{VIEW_LABELS[v]} "
        for idx, v in enumerate(VIEWS, start=1)
    )
    _safe_addstr(stdscr, 1, 0, tabs, curses.A_BOLD)

    body_top = 3
    body_height = max(1, max_y - 4)
    if ctrl.help_open:
        display_lines = _help_lines()
        start = 0
    elif ctrl.modal_lines:
        display_lines = [f"=== {ctrl.modal_title} ===", ""] + ctrl.modal_lines
        start = min(ctrl.modal_scroll, max(0, len(display_lines) - 1))
    else:
        display_lines = ctrl.lines()
        start = min(ctrl.scroll, max(0, len(display_lines) - 1))

    for row in range(body_height):
        idx = start + row
        if idx >= len(display_lines):
            break
        _safe_addstr(stdscr, body_top + row, 0, display_lines[idx])

    footer = " [1-7] View  [Tab] Next  [r] Refresh  [:] Cmd  [?] Help  [q] Quit"
    if ctrl.status_message:
        footer += f" | {ctrl.status_message}"
    _safe_addstr(stdscr, max(0, max_y - 1), 0, footer.ljust(max(1, max_x - 1)), curses.A_REVERSE)
    stdscr.refresh()


def _handle_view_hotkey(stdscr, ctrl: TuiController, ch: int) -> bool:
    """Handle view-specific interactive prompts; return True if key was consumed."""
    v = ctrl.view
    if v == "overview":
        if ch == ord("p"):
            reason = _prompt_line(stdscr, "pause reason (optional): ")
            ctrl.action_pause(reason=reason)
            return True
        if ch == ord("u"):
            flags = _prompt_line(stdscr, "resume flags (--clear-breaker / --clear-kill): ")
            ctrl.action_resume(
                clear_breaker="--clear-breaker" in flags,
                clear_kill="--clear-kill" in flags,
            )
            return True
        if ch == ord("x"):
            reason = _prompt_line(stdscr, "stop (kill switch) reason: ")
            ctrl.action_stop(reason=reason)
            return True
        if ch == ord("n"):
            spec = _prompt_line(stdscr, "run args (e.g. --dry-run --cards 1): ")
            ctrl.execute_command("run " + spec if spec else "run --dry-run")
            return True
    elif v == "cards":
        if ch == ord("c"):
            ctrl.action_check(baseline=False)
            return True
        if ch == ord("b"):
            ctrl.action_check(baseline=True)
            return True
        if ch == ord("d"):
            note = _prompt_line(stdscr, "done note (optional): ")
            ctrl.action_done(note=note)
            return True
        if ch == ord("l"):
            reason = _prompt_line(stdscr, "release reason: ")
            if reason:
                ctrl.action_release(reason=reason)
            return True
        if ch == ord("v"):
            verdict = _prompt_line(stdscr, "verdict (clean|minor-fix|bounced|scrapped): ")
            tokens_raw = _prompt_line(stdscr, "review tokens: ")
            if verdict and tokens_raw.isdigit():
                ctrl.action_verdict(verdict, review_tokens=int(tokens_raw))
            return True
        if ch == ord("e"):
            note = _prompt_line(stdscr, "escape note: ")
            if note:
                ctrl.action_escape(note=note)
            return True
        if ch in (ord("\n"), ord("\r")):
            cid = ctrl._selected_card_id()
            if cid:
                ctrl.card_id = cid
                ctrl.set_view("dossier")
            return True
    elif v == "dossier":
        if ch == ord("a"):
            raw = _prompt_line(stdscr, "attempt # (blank to clear): ")
            ctrl.attempt = int(raw) if raw.isdigit() else None
            ctrl.refresh()
            return True
    elif v == "board":
        if ch == ord("f"):
            flt = _prompt_line(stdscr, "filter kind=<k> member=<m> card=<c> (blank clears): ")
            parsed: dict[str, str] = {}
            for tok in shlex.split(flt):
                k, sep, val = tok.partition("=")
                if sep and k in ("kind", "member", "card"):
                    parsed[k] = val
            ctrl.board_filter = parsed
            ctrl.refresh()
            return True
        if ch == ord("v"):
            ev = _prompt_line(stdscr, "evidence path (inside state_dir): ")
            if ev:
                ctrl.view_evidence(ev)
            return True
        if ch == ord("n"):
            as_n = _prompt_line(stdscr, "post --as: ")
            to_n = _prompt_line(stdscr, "post --to: ")
            body = _prompt_line(stdscr, "post body: ")
            if as_n and to_n and body:
                ctrl.action_board_post(as_name=as_n, to=to_n, body=body)
            return True
        if ch == ord("a"):
            seq_s = _prompt_line(stdscr, "answer question seq: ")
            as_n = _prompt_line(stdscr, "answer --as: ")
            js_s = _prompt_line(stdscr, "answer JSON: ")
            if seq_s.isdigit() and as_n and js_s:
                ctrl.action_board_answer(int(seq_s), as_name=as_n, json_data=js_s)
            return True
        if ch == ord("w"):
            as_n = _prompt_line(stdscr, "board wait --as: ")
            if as_n:
                ctrl.action_board_wait(as_name=as_n)
            return True
    elif v == "swarm":
        if ch == ord("J"):
            args = _prompt_line(stdscr, "swarm join --name N --seat coordinator|supervisor: ")
            if args:
                ctrl.execute_command("swarm join " + args)
            return True
        if ch == ord("S"):
            args = _prompt_line(stdscr, "swarm start CARD --as CALLER [--team ...]: ")
            if args:
                ctrl.execute_command("swarm start " + args)
            return True
        if ch == ord("E"):
            args = _prompt_line(stdscr, "swarm set MEMBER --as CALLER [--tier T] [--effort E]: ")
            if args:
                ctrl.execute_command("swarm set " + args)
            return True
        if ch == ord("H"):
            args = _prompt_line(stdscr, "swarm hint MEMBER --as CALLER --body ...: ")
            if args:
                ctrl.execute_command("swarm hint " + args)
            return True
        if ch == ord("B"):
            args = _prompt_line(stdscr, "swarm block CARD --as CALLER --reason ...: ")
            if args:
                ctrl.execute_command("swarm block " + args)
            return True
        if ch == ord("X"):
            args = _prompt_line(stdscr, "swarm remove MEMBER --as CALLER: ")
            if args:
                ctrl.execute_command("swarm remove " + args)
            return True
    elif v == "harness":
        if ch == ord("i"):
            ctrl.action_harness("index")
            return True
        if ch == ord("s"):
            eid = _prompt_line(stdscr, "harness show <id>: ")
            if eid:
                ctrl.action_harness("show", eid)
            return True
        if ch in (ord("+"), ord("P")):
            eid = _prompt_line(stdscr, "harness promote <id>: ")
            if eid:
                ctrl.action_harness("promote", eid)
            return True
        if ch in (ord("-"), ord("D")):
            eid = _prompt_line(stdscr, "harness demote <id_or_rev>: ")
            if eid:
                ctrl.action_harness("demote", eid)
            return True
        if ch == ord("R"):
            confirm = _prompt_line(stdscr, "type YES to confirm harness reset: ")
            if confirm == "YES":
                ctrl.action_harness("reset", yes=True)
            return True
    elif v == "ops":
        if ch == ord("a"):
            ctrl.execute_command("acp status --json")
            return True
        if ch == ord("P"):
            ctrl.execute_command("acp probe --json")
            return True
        if ch == ord("C"):
            args = _prompt_line(stdscr, "acp configure args: ")
            ctrl.execute_command("acp configure " + args)
            return True
        if ch == ord("s"):
            args = _prompt_line(stdscr, "supervise flags (--triage-blocked / --prepare-queue): ")
            if args:
                ctrl.execute_command("supervise " + args)
            return True
        if ch == ord("b"):
            ctrl.execute_command("bench list --json")
            return True
        if ch == ord("B"):
            args = _prompt_line(stdscr, "bench run --dry-run args: ")
            ctrl.execute_command("bench run --dry-run " + args)
            return True
        if ch == ord("I"):
            args = _prompt_line(stdscr, "init <name> [flags]: ")
            if args:
                ctrl.execute_command("init " + args)
            return True
        if ch == ord("A"):
            args = _prompt_line(stdscr, "adopt [flags]: ")
            ctrl.execute_command("adopt " + args)
            return True
        if ch == ord("K"):
            args = _prompt_line(stdscr, "install-skill [flags]: ")
            ctrl.execute_command("install-skill " + args)
            return True
    return False


def _curses_main(stdscr, ctrl: TuiController) -> int:
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    stdscr.keypad(True)
    timeout_ms = max(50, int(ctrl.refresh_s * 1000))
    while True:
        _render_curses_frame(stdscr, ctrl)
        stdscr.timeout(timeout_ms)
        ch = stdscr.getch()
        if ch == curses.ERR:
            ctrl.refresh()
            continue
        if ch == ord(":") and not ctrl.help_open and not ctrl.modal_lines:
            cmd_line = _prompt_line(stdscr, ":")
            if cmd_line:
                ctrl.execute_command(cmd_line)
            continue
        if not ctrl.help_open and not ctrl.modal_lines and _handle_view_hotkey(stdscr, ctrl, ch):
            continue
        if not ctrl.handle_key(ch):
            return 0


def run_tui(
    repo: Path | str = ".",
    config_path: Path | str | None = None,
    run_id: str | None = None,
    refresh_s: float = 1.0,
    once: bool = False,
    view: str = "overview",
    json_output: bool = False,
    *,
    out=None,
    **kwargs,
) -> int:
    """Launch interactive curses TUI, or render a deterministic snapshot with ``once=True``."""
    if view not in VIEWS:
        raise UsageError(f"unknown view {view!r}; choose from {', '.join(VIEWS)}")
    if once:
        rendered = render_snapshot(
            repo,
            config_path=config_path,
            view=view,
            run_id=run_id,
            as_json=json_output,
            **kwargs,
        )
        stream = out if out is not None else sys.stdout
        print(rendered, file=stream)
        return 0

    ctrl = TuiController(
        repo,
        config_path=config_path,
        run_id=run_id,
        view=view,
        refresh_s=refresh_s,
    )
    return int(curses.wrapper(lambda stdscr: _curses_main(stdscr, ctrl)))
