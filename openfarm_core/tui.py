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
import re
import shlex
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any

from . import __version__
from .acp_setup import ACP_ACTIONS, inspect_acp_status
from .adapters.acp import is_acp_adapter, normalize_acp_provider
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
        raw_tests = card.fields.get("tests") or []
        tests_meta = [
            {
                "name": str(getattr(t, "name", t)),
                "file": str(getattr(t, "file", "")),
                "asserts": str(getattr(t, "asserts", "")),
            }
            for t in raw_tests
        ]
        out.append(
            {
                "id": card.id,
                "title": card.title,
                "state": card.state,
                "category": card.category,
                "unit": card.unit,
                "done_text": card.done_text,
                "tests": tests_meta,
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
    agy_bin: str | None = None,
    codex_bin: str | None = None,
    include_cli_auth: bool = False,
    bench_dir: Path | None = None,
    bench_out: Path | None = None,
) -> dict:
    if include_cli_auth:
        acp_info = inspect_acp_status(
            ctx.repo if ctx.configured else None,
            provider="all",
            home=acp_home,
            claude_bin=claude_bin,
            acp_bin=acp_bin,
            agy_bin=agy_bin,
            codex_bin=codex_bin,
        )
    else:
        resolved_home = (
            Path(acp_home).expanduser().resolve()
            if acp_home is not None
            else Path.home().resolve()
        )
        resolved_claude = claude_bin if claude_bin is not None else (shutil.which("claude") or "")
        resolved_acp = acp_bin if acp_bin is not None else (shutil.which("claude-agent-acp") or "")
        claude_transport = (
            "claude-agent-acp"
            if resolved_acp
            else ("claude-native-acp" if resolved_claude else "missing")
        )
        resolved_agy = agy_bin if agy_bin is not None else (shutil.which("agy") or "")
        resolved_agy_acp = shutil.which("antigravity-acp") or ""
        agy_transport = (
            "antigravity-acp"
            if resolved_agy_acp
            else ("antigravity-native-acp" if resolved_agy else "missing")
        )
        resolved_codex = codex_bin if codex_bin is not None else (shutil.which("codex") or "")
        resolved_codex_acp = shutil.which("codex-acp") or ""
        codex_transport = (
            "codex-acp"
            if resolved_codex_acp
            else ("codex-native-acp" if resolved_codex else "missing")
        )
        claude_creds = (resolved_home / ".claude" / ".credentials.json").is_file()
        agy_creds = (
            resolved_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        ).is_file()
        codex_creds = (resolved_home / ".codex" / "auth.json").is_file()
        providers_map = {
            "claude": {
                "provider": "claude",
                "resolved_transport": claude_transport,
                "cli_path": resolved_claude or None,
                "acp_bin": resolved_acp or None,
                "oauth_ready": bool(
                    claude_creds
                    or str(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
                ),
                "credentials_file_present": claude_creds,
            },
            "antigravity": {
                "provider": "antigravity",
                "resolved_transport": agy_transport,
                "cli_path": resolved_agy or None,
                "acp_bin": resolved_agy_acp or None,
                "oauth_ready": agy_creds,
                "credentials_file_present": agy_creds,
            },
            "codex": {
                "provider": "codex",
                "resolved_transport": codex_transport,
                "cli_path": resolved_codex or None,
                "acp_bin": resolved_codex_acp or None,
                "oauth_ready": codex_creds,
                "credentials_file_present": codex_creds,
            },
        }
        primary_provider = "claude"
        if ctx.configured and ctx.cfg is not None:
            for spec in (
                ctx.cfg.micromanager_adapter,
                ctx.cfg.model_adapter,
                getattr(getattr(ctx.cfg, "swarm", None), "coordinator", ""),
                getattr(getattr(ctx.cfg, "swarm", None), "supervisor", ""),
            ):
                if is_acp_adapter(spec):
                    primary_provider = normalize_acp_provider(spec)
                    break

        acp_info = {
            "ok": True,
            "provider": "all",
            "primary_provider": primary_provider,
            "resolved_transport": providers_map[primary_provider]["resolved_transport"],
            "claude_cli": resolved_claude or None,
            "claude_agent_acp": resolved_acp or None,
            "agy_cli": resolved_agy or None,
            "antigravity_acp": resolved_agy_acp or None,
            "codex_cli": resolved_codex or None,
            "codex_acp": resolved_codex_acp or None,
            "oauth_token_env": bool(str(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()),
            "cli_auth_checked": False,
            "providers": providers_map,
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
            swarm_cfg = getattr(cfg, "swarm", None)
            if swarm_cfg is not None:
                acp_info["repo_swarm"] = {
                    "enabled": swarm_cfg.enabled,
                    "coordinator": swarm_cfg.coordinator,
                    "supervisor": swarm_cfg.supervisor,
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
    agy_bin: str | None = None,
    codex_bin: str | None = None,
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
            agy_bin=agy_bin,
            codex_bin=codex_bin,
            include_cli_auth=include_cli_auth,
            bench_dir=bench_dir,
            bench_out=bench_out,
        )

    if view == "cards":
        cards_cnt = len(data.get("cards") or [])
    elif ctx.configured:
        cards_list, _ = _read_cards_readonly(ctx)
        cards_cnt = len(cards_list)
    else:
        cards_cnt = 0

    if view == "dossier":
        dossiers_cnt = len(data.get("available") or [])
    elif ctx.configured:
        dossiers_cnt = len(_discover_dossiers(ctx))
    else:
        dossiers_cnt = 0

    if view == "board":
        board_meta = data
    elif ctx.configured:
        board_meta = _read_board_data(ctx, run_id=run_id)
    else:
        board_meta = {}
    posts_cnt = int(board_meta.get("total_posts", 0) or 0)
    unanswered_q_cnt = len(board_meta.get("unanswered_questions") or [])

    if view == "swarm":
        members_cnt = len(data.get("members") or [])
    else:
        members_cnt = len(board_meta.get("members") or {})

    if view == "harness":
        if harness_global:
            harness_cnt = int((data.get("global") or {}).get("active", 0) or 0)
        else:
            harness_cnt = int((data.get("local") or {}).get("total_entries", 0) or 0)
    else:
        harness_cnt = int((overview.get("harness") or {}).get("total_entries", 0) or 0)

    if view == "ops":
        bench_cnt = len(data.get("bench_tasks") or [])
    else:
        bdir = Path(bench_dir) if bench_dir is not None else (ctx.repo / "bench")
        bench_cnt = (
            sum(1 for p in bdir.iterdir() if p.is_dir() and (p / "task.toml").is_file())
            if bdir.is_dir()
            else 0
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
            "unanswered_questions": unanswered_q_cnt,
            "counts": {
                "cards": cards_cnt,
                "dossiers": dossiers_cnt,
                "posts": posts_cnt,
                "members": members_cnt,
                "harness": harness_cnt,
                "bench": bench_cnt,
            },
        },
        "data": data,
    }


# -- Visual primitives, icons, theme, and plain-text formatting --------------


SELECTION_VIEWS = ("cards", "board", "swarm", "harness")

_STATE_STYLE_MAP: dict[str, tuple[str, str]] = {
    "pass": ("✓", "ok"),
    "done": ("✓", "ok"),
    "ready": ("✓", "ok"),
    "ok": ("✓", "ok"),
    "clean": ("✓", "ok"),
    "confirmed": ("✓", "ok"),
    "answered": ("✓", "ok"),
    "in_progress": ("●", "info"),
    "active": ("●", "info"),
    "probing": ("●", "info"),
    "running": ("●", "info"),
    "in_review": ("◉", "accent"),
    "in-review": ("◉", "accent"),
    "minor-fix": ("◉", "accent"),
    "waiting": ("⏸", "warn"),
    "paused": ("⏸", "warn"),
    "candidate": ("⏸", "warn"),
    "blocked": ("✗", "err"),
    "fail": ("✗", "err"),
    "failed": ("✗", "err"),
    "refused": ("✗", "err"),
    "bounced": ("✗", "err"),
    "scrapped": ("✗", "err"),
    "demoted": ("✗", "err"),
    "retired": ("✗", "err"),
    "removed": ("✗", "err"),
    "error": ("✗", "err"),
    "handback": ("↩", "warn"),
    "stall": ("⚠", "warn"),
    "collision": ("⚠", "warn"),
    "defect": ("⚠", "err"),
}

_POST_STYLE_MAP: dict[str, tuple[str, str]] = {
    "question": ("?", "warn"),
    "answer": ("✓", "ok"),
    "done": ("✓", "ok"),
    "gate": ("✓", "info"),
    "handback": ("↩", "warn"),
    "stall": ("⚠", "warn"),
    "blocked": ("✗", "err"),
    "defect": ("✗", "err"),
    "brief": ("●", "info"),
    "result": ("●", "info"),
    "action": ("●", "info"),
    "roster": ("●", "info"),
    "note": ("○", "muted"),
}


def _state_icon(state: str) -> tuple[str, str]:
    key = str(state or "").strip().lower()
    return _STATE_STYLE_MAP.get(key, ("○", "muted"))


def _post_icon(kind: str) -> tuple[str, str]:
    key = str(kind or "").strip().lower()
    return _POST_STYLE_MAP.get(key, ("○", "muted"))


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _truncate(text: str, max_cols: int) -> str:
    clean = " ".join(str(text or "").replace("\t", " ").splitlines()).strip()
    if max_cols <= 0:
        return ""
    if _display_width(clean) <= max_cols:
        return clean
    if max_cols == 1:
        for ch in clean:
            if _display_width(ch) <= 1:
                return ch
        return "…"
    out: list[str] = []
    used = 0
    limit = max_cols - 1
    for ch in clean:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _pad_right(text: str, width: int) -> str:
    if width <= 0:
        return ""
    trunc = _truncate(text, width)
    pad = max(0, width - _display_width(trunc))
    return trunc + (" " * pad)


def _wrap_text(text: str, width: int) -> list[str]:
    lim = max(1, int(width))
    raw_lines = str(text or "").splitlines()
    if not raw_lines:
        return [""]
    out: list[str] = []
    for raw in raw_lines:
        if not raw.strip():
            out.append("")
            continue
        words = raw.split(" ")
        cur = ""
        for word in words:
            candidate = word if not cur else f"{cur} {word}"
            if _display_width(candidate) <= lim:
                cur = candidate
                continue
            if cur:
                out.append(cur)
                cur = ""
            if _display_width(word) <= lim:
                cur = word
                continue
            chunk = ""
            for ch in word:
                w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                if _display_width(chunk) + w > lim and chunk:
                    out.append(chunk)
                    chunk = ch
                else:
                    chunk += ch
            cur = chunk
        if cur:
            out.append(cur)
    return out or [""]


def _bar(ratio: float, width: int = 10, *, show_pct: bool = True) -> str:
    r = max(0.0, min(1.0, float(ratio)))
    filled = max(0, min(width, int(round(r * width))))
    blocks = ("█" * filled) + ("░" * (width - filled))
    return f"{blocks} {round(r * 100)}%" if show_pct else blocks


def _classify_line_theme(line: str, default_key: str = "") -> str:
    s = line.lstrip()
    if s.startswith(("diff --git ", "index ", "+++ ", "--- ")):
        return "info_bold"
    if s.startswith("@@"):
        return "info"
    if s.startswith("+"):
        return "ok"
    if s.startswith("-"):
        return "err"
    if s.startswith("#"):
        return "info_bold"
    low = line.lower()
    if "✗" in line or re.search(r"\b(fail|failed|blocked|refused|error|scrapped|bounced)\b", low):
        return "err"
    if "✓" in line or re.search(r"\b(pass|passed|clean|confirmed)\b", low):
        return "ok"
    if "⚠" in line or "⏸" in line or "↩" in line or re.search(r"\b(warn|warning|paused|waiting|handback|stall|collision)\b", low):
        return "warn"
    if "◉" in line or "in_review" in low or "in-review" in low:
        return "accent"
    if "●" in line or "in_progress" in low:
        return "info"
    return default_key


def _init_curses_theme() -> dict[str, int]:
    """Build semantic curses attribute map, honoring NO_COLOR and terminal color support."""
    no_color = bool(str(os.environ.get("NO_COLOR", "")).strip())
    has_colors = False
    with contextlib.suppress(curses.error):
        has_colors = bool(curses.has_colors())

    fallback: dict[str, int] = {
        "brand": curses.A_BOLD | curses.A_REVERSE,
        "header": curses.A_REVERSE,
        "tab_active": curses.A_BOLD | curses.A_REVERSE,
        "tab_idle": curses.A_DIM,
        "ok": curses.A_BOLD,
        "warn": curses.A_BOLD,
        "err": curses.A_BOLD | curses.A_REVERSE,
        "info": curses.A_BOLD,
        "accent": curses.A_BOLD,
        "border": curses.A_DIM,
        "selected": curses.A_BOLD | curses.A_REVERSE,
        "key": curses.A_BOLD,
        "muted": curses.A_DIM,
    }
    if no_color or not has_colors:
        return fallback

    with contextlib.suppress(curses.error):
        curses.start_color()
    default_bg = curses.COLOR_BLACK
    try:
        curses.use_default_colors()
        default_bg = -1
    except curses.error:
        default_bg = curses.COLOR_BLACK

    def _pair(pair_id: int, fg: int, bg: int, extra: int = 0, fb: int = 0) -> int:
        try:
            curses.init_pair(pair_id, fg, bg)
            return curses.color_pair(pair_id) | extra
        except curses.error:
            return fb

    return {
        "brand": _pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN, curses.A_BOLD, fallback["brand"]),
        "header": _pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE, curses.A_BOLD, fallback["header"]),
        "tab_active": _pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN, curses.A_BOLD, fallback["tab_active"]),
        "tab_idle": _pair(
            4,
            -1 if default_bg == -1 else curses.COLOR_WHITE,
            default_bg,
            curses.A_DIM,
            fallback["tab_idle"],
        ),
        "ok": _pair(5, curses.COLOR_GREEN, default_bg, curses.A_BOLD, fallback["ok"]),
        "warn": _pair(6, curses.COLOR_YELLOW, default_bg, curses.A_BOLD, fallback["warn"]),
        "err": _pair(7, curses.COLOR_RED, default_bg, curses.A_BOLD, fallback["err"]),
        "info": _pair(8, curses.COLOR_CYAN, default_bg, 0, fallback["info"]),
        "accent": _pair(9, curses.COLOR_MAGENTA, default_bg, curses.A_BOLD, fallback["accent"]),
        "border": _pair(10, curses.COLOR_BLUE, default_bg, curses.A_DIM, fallback["border"]),
        "selected": _pair(
            11,
            curses.COLOR_CYAN,
            default_bg,
            curses.A_BOLD | curses.A_REVERSE,
            fallback["selected"],
        ),
        "key": _pair(12, curses.COLOR_BLACK, curses.COLOR_WHITE, curses.A_BOLD, fallback["key"]),
        "muted": curses.A_DIM,
    }


def _box_lines(title: str, inner_lines: list[str], width: int = 96) -> list[str]:
    w = max(24, int(width))
    iw = w - 4
    t = _truncate(title, max(4, iw - 4))
    fill = max(1, w - 5 - _display_width(t))
    out = [f"╭─ {t} {'─' * fill}╮"]
    body = inner_lines or ["(none)"]
    for raw in body:
        for wrapped in _wrap_text(raw, iw):
            out.append(f"│ {_pad_right(wrapped, iw)} │")
    out.append(f"╰{'─' * (w - 2)}╯")
    return out


def _table_lines(headers: list[str], rows: list[list[str]], col_widths: list[int]) -> list[str]:
    head = "  ".join(_pad_right(h, w) for h, w in zip(headers, col_widths))
    sep = "  ".join("─" * w for w in col_widths)
    out = [head, sep]
    for row in rows:
        out.append("  ".join(_pad_right(cell, w) for cell, w in zip(row, col_widths)))
    return out


def _status_pills(sb: dict, *, include_brand: bool = True) -> list[str]:
    pills: list[str] = []
    if include_brand:
        pills.append(f"openfarm tui v{__version__}")
    pills.append(str(sb.get("project") or ""))
    run_id = str(sb.get("active_run_id") or "")
    pills.append(f"● {run_id}" if run_id else "○ idle")
    if sb.get("paused"):
        pills.append("⏸ PAUSED")
    if sb.get("kill_switch"):
        pills.append("■ KILL")
    if sb.get("breaker_tripped"):
        pills.append("⚡ BREAKER")
    if sb.get("swarm_enabled"):
        pills.append("⇄ SWARM")
    uq = int(sb.get("unanswered_questions", 0) or 0)
    if uq > 0:
        pills.append(f"? {uq} question" if uq == 1 else f"? {uq} questions")
    return [p for p in pills if p]


def _tab_label(idx: int, view_name: str, counts: dict, *, include_count: bool = True) -> str:
    label = VIEW_LABELS[view_name]
    count_keys = {
        "cards": "cards",
        "dossier": "dossiers",
        "board": "posts",
        "swarm": "members",
        "harness": "harness",
        "ops": "bench",
    }
    cnt = int(counts.get(count_keys.get(view_name, ""), 0) or 0) if include_count else 0
    if include_count and cnt > 0:
        return f" {idx} {label} ({cnt}) "
    return f" {idx} {label} "


def _format_overview_lines(snap: dict, *, width: int = 96) -> list[str]:
    d = snap["data"]
    if not d.get("configured"):
        msg = "No .openfarm/config.toml found." if not d.get("config_error") else f"Config error: {d['config_error']}"
        return _box_lines(
            f"Overview — {d['repo']} (not configured)",
            [
                msg,
                "Switch to [7] Ops to run `init`, `adopt`, or `install-skill`, or press `:` for command bar.",
            ],
            width=width,
        )

    sw = d["swarm"]
    br = d["breaker"]
    run_icon = "●" if d["active_run_id"] else "○"
    pause_icon = "⏸ yes" if d["paused"] else "✓ no"
    kill_icon = "✗ ACTIVE" if d["kill_switch"] else "✓ clear"
    brk_icon = "⚡ TRIPPED" if br.get("tripped") else "✓ ok"
    proj_lines = [
        f"Project: {d['project']}  lane={d['lane']}  base={d['base_branch']}",
        f"State dir: {d['state_dir']}",
        f"Live run: {run_icon} {d['active_run_id'] or '(none)'}   Paused: {pause_icon}   Kill: {kill_icon}   Breaker: {brk_icon}",
        f"Caps: spend_per_night={d['spend_cap']}   concurrent_cards={d['concurrent_cards']}   concurrent_builds={d['concurrent_builds']}",
        f"Model: adapter={d['model']['adapter']}   endpoint={d['model']['endpoint'] or '—'}   key_file_present={d['model']['key_file_present']}",
    ]
    out = _box_lines("Project & Safety", proj_lines, width=width)

    swarm_lines = [
        f"Swarm: {'⇄ enabled' if sw['enabled'] else '○ off'}   coordinator={sw['coordinator']} ({sw['coordinator_effort']})   supervisor={sw['supervisor']} ({sw['supervisor_effort']})   default_tier={sw['default_tier']}"
    ]
    if d["tiers"]:
        rows = [
            [t["name"], t["model"], ",".join(t["efforts"]) or "none", t["effort_param"] or "—"]
            for t in d["tiers"]
        ]
        tw = max(44, width - 4)
        c1, c2, c3 = max(8, tw // 6), max(12, tw // 3), max(10, tw // 4)
        c4 = max(8, tw - c1 - c2 - c3 - 6)
        swarm_lines.extend(_table_lines(["Tier", "Model", "Efforts", "Param"], rows, [c1, c2, c3, c4]))
    out.extend(_box_lines("Swarm & Tiers", swarm_lines, width=width))

    lr = d.get("last_run") or {}
    if lr:
        counts = lr.get("counts") or {}
        total_cards = sum(int(v or 0) for v in counts.values()) or 1
        outcome_bits = [f"{_state_icon(k)[0]} {k}:{v}" for k, v in sorted(counts.items())]
        run_lines = [
            f"Run: {lr.get('run_id')}   finished={lr.get('finished_at') or '—'}   spend=${lr.get('spend', 0.0):.4f}",
            "Outcomes: " + ("  ".join(outcome_bits) if outcome_bits else "(none)"),
        ]
        for k, v in sorted(counts.items()):
            ic, _ = _state_icon(k)
            run_lines.append(f"  {ic} {_pad_right(k, 12)} {_bar(int(v or 0) / total_cards, 12)} ({v}/{total_cards})")
        tok = lr.get("tokens") or {}
        if tok:
            total_tok = int(tok.get("total", 0) or 0)
            w_tok = int(tok.get("worker", 0) or 0)
            s_tok = int(tok.get("supervisor", 0) or 0)
            denom = total_tok or (w_tok + s_tok) or 1
            w_share = float(tok.get("worker_share", w_tok / denom))
            s_share = float(tok.get("supervisor_share", s_tok / denom))
            run_lines.append(f"Tokens: total={total_tok}   worker={w_tok}   supervisor={s_tok}")
            run_lines.append(f"  worker     {_bar(w_share, 12)} ({w_tok})")
            run_lines.append(f"  supervisor {_bar(s_share, 12)} ({s_tok})")
            if tok.get("by_member"):
                run_lines.append("  by_member:")
                for mk, mv in sorted(tok["by_member"].items()):
                    run_lines.append(f"    {_pad_right(mk, 14)} {_bar(int(mv or 0) / denom, 10)} ({mv})")
            if tok.get("by_effort"):
                run_lines.append("  by_effort:")
                for ek, ev in sorted(tok["by_effort"].items()):
                    run_lines.append(f"    {_pad_right(ek, 14)} {_bar(int(ev or 0) / denom, 10)} ({ev})")
    else:
        run_lines = ["Last run: (none)"]
    out.extend(_box_lines("Last Run & Token Split", run_lines, width=width))

    h = d.get("harness") or {}
    h_tot = int(h.get("total_entries", 0) or 0)
    h_act = int(h.get("active_count", 0) or 0)
    h_ratio = (h_act / h_tot) if h_tot > 0 else 0.0
    h_ic, _ = _state_icon(str(h.get("state", "")))
    out.extend(
        _box_lines(
            "Harness",
            [
                f"enabled={h.get('enabled', False)}   rev={h.get('revision', 0)}   state={h_ic} {h.get('state', 'n/a')}   active {_bar(h_ratio, 10)} ({h_act}/{h_tot})"
            ],
            width=width,
        )
    )
    out.append("Actions: [p]ause  [u]resume  [x]stop  [n]run")
    return out


def _format_cards_lines(snap: dict) -> list[str]:
    d = snap["data"]
    if not d.get("configured"):
        return _box_lines("Cards", ["Cards — repository not configured."])
    cards = d.get("cards") or []
    inner: list[str] = []
    if not cards:
        inner.append("(no cards)")
    else:
        state_counts: dict[str, int] = {}
        for c in cards:
            st = str(c.get("state", ""))
            state_counts[st] = state_counts.get(st, 0) + 1
        summary_bits = [f"{_state_icon(k)[0]} {k}:{v}" for k, v in sorted(state_counts.items())]
        inner.append("States: " + "  ".join(summary_bits))
        inner.append("")
        rows: list[list[str]] = []
        for c in cards:
            ic, _ = _state_icon(c["state"])
            col_txt = f"⚠ {','.join(c['collides_with'])}" if c["collides_with"] else "✓ none"
            br = c["branches"][0] if c["branches"] else "—"
            unm = f"{br} (+{c['unmerged_commits']})" if c["unmerged_commits"] else br
            rows.append([
                f"{ic} {c['state']}",
                c["id"],
                c["title"],
                c["category"] or "—",
                col_txt,
                unm,
            ])
        inner.extend(_table_lines(["State", "ID", "Title", "Category", "Collisions", "Branch"], rows, [12, 8, 22, 13, 12, 15]))
        for c in cards:
            if c["diff_preview"]:
                inner.append("")
                inner.append(f"Diff preview ({c['id']}):")
                for diff_ln in c["diff_preview"].splitlines()[:6]:
                    inner.append(f"  {diff_ln[:86]}")
    out = _box_lines(f"Cards ({len(cards)}) · source={d.get('source_mode')} · base={d.get('base_branch')}", inner)
    out.append("Actions: [c]heck  [b]aseline check  [d]one  [l]release  [v]erdict  [e]scape  [Enter]dossier")
    return out


def _format_dossier_lines(snap: dict, *, width: int = 96) -> list[str]:
    d = snap["data"]
    if d.get("error"):
        return _box_lines("Dossier", [f"✗ Dossier: {d['error']}"], width=width)
    payload = d.get("payload") or {}
    gates = payload.get("2_gates") or []
    passed = sum(1 for g in gates if str(g.get("result", "")).lower() == "pass")
    gate_summary = f"Gates: {_bar(passed / len(gates), 10)} ({passed}/{len(gates)} pass)" if gates else "Gates: (none)"
    att = f"  attempt={d.get('attempt')}" if d.get("attempt") is not None else ""
    st_ic, _ = _state_icon(str(payload.get("final_state", "")))
    out = _box_lines(
        f"Dossier: {d.get('card')} · run={d.get('run')}{att}",
        [
            f"Final state: {st_ic} {payload.get('final_state', '—')}   {gate_summary}",
        ],
        width=width,
    )
    out.extend((d.get("markdown") or "").splitlines())
    out.append("Actions: [[]/[]]card  [{/}]run  [a]attempt  [m]toggle md/json")
    return out


def _format_board_lines(snap: dict) -> list[str]:
    d = snap["data"]
    inner: list[str] = []
    if d.get("waiting_members"):
        waits = ", ".join(
            f"⏸ {m['name']}({m['role']} since {m['wait_since'] or '?'})"
            for m in d["waiting_members"]
        )
        inner.append(f"Waiting: {waits}")
    else:
        inner.append("Waiting: (none)")
    if d.get("unanswered_questions"):
        qs = ", ".join(f"? #{q['seq']}->{','.join(q['to'])}" for q in d["unanswered_questions"])
        inner.append(f"Unanswered questions: {qs}")

    posts = d.get("posts") or []
    inner.append("")
    if not posts:
        inner.append("(no board posts)")
    else:
        rows: list[list[str]] = []
        for p in posts:
            ic, _ = _post_icon(p["kind"])
            to_txt = ",".join(p["to"]) or "—"
            ev_txt = f" ({len(p['evidence'])} ev)" if p["evidence"] else ""
            rows.append([
                f"#{p['seq']}",
                f"{ic} {p['kind']}",
                f"{p['from']}->{to_txt}",
                p["card"] or "—",
                f"{p['body_preview']}{ev_txt}",
            ])
        inner.extend(_table_lines(["Seq", "Kind", "Route", "Card", "Preview"], rows, [5, 11, 19, 9, 38]))
    ended_txt = "ended" if d["ended"] else "live"
    out = _box_lines(
        f"Board — run={d['run_id'] or '(none)'} ({d['run_source']}) · {ended_txt} · posts={d['total_posts']} · last_seq={d['last_seq']}",
        inner,
    )
    out.append("Actions: [f]ilter  [v]iew evidence  [n]post  [a]nswer  [w]ait check  [Enter]evidence")
    return out


def _format_swarm_lines(snap: dict) -> list[str]:
    d = snap["data"]
    sw = d["swarm"]
    cfg_lines = [
        f"Enabled: {'⇄ yes' if sw['enabled'] else '○ no'}   coordinator={sw['coordinator']}   supervisor={sw['supervisor']}   default_tier={sw['default_tier']}",
        f"Policy: timeout={sw['on_external_timeout']}   ext_wait={sw['external_wait_s']}s   stall={sw['stall_after_s']}s   escalate={sw['escalate_after']}   max={sw['max_members']}",
    ]
    out = _box_lines(f"Swarm — run={d['run_id'] or '(none)'}", cfg_lines)

    if d["tiers"]:
        t_rows = [
            [t["name"], t["model"], ",".join(t["efforts"]) or "none", t["effort_param"] or "—"]
            for t in d["tiers"]
        ]
        out.extend(_box_lines("Tiers", _table_lines(["Tier", "Model", "Efforts", "Param"], t_rows, [16, 28, 24, 16])))
    else:
        out.extend(_box_lines("Tiers", ["(no tiers configured)"]))

    members = d.get("members") or []
    if not members:
        m_inner = ["(no members)"]
    else:
        tot_tok = sum(int(m.get("tokens_total", 0) or 0) for m in members) or 1
        m_rows: list[list[str]] = []
        for m in members:
            ic, _ = _state_icon(m["state"])
            te = f"{m['tier'] or '—'}:{m['effort'] or '—'}"
            share = _bar(int(m.get("tokens_total", 0) or 0) / tot_tok, 8, show_pct=False)
            m_rows.append([
                f"{ic} {m['state']}",
                m["name"],
                m["role"],
                m["kind"],
                m["card"] or "—",
                te,
                f"{m['tokens_total']} {share}",
            ])
        m_inner = _table_lines(["State", "Name", "Role", "Kind", "Card", "Tier:Effort", "Tokens"], m_rows, [11, 11, 11, 8, 8, 13, 16])
    out.extend(_box_lines(f"Roster ({len(members)})", m_inner))
    out.append("Actions: [J]oin  [S]tart  [E]set  [H]int  [B]lock  [X]remove")
    return out


def _format_harness_lines(snap: dict) -> list[str]:
    d = snap["data"]
    if not d.get("configured"):
        return _box_lines("Harness", ["Harness — repository not configured."])
    mode = "GLOBAL" if d["global_mode"] else "LOCAL"
    inner: list[str] = []
    if d["global_mode"]:
        g = d["global"]
        tot = int(g.get("active", 0) or 0) + int(g.get("retired", 0) or 0)
        act_bar = _bar(int(g.get("active", 0) or 0) / (tot or 1), 10)
        inner.append(f"Global dir: {g.get('dir')}   active={g.get('active', 0)}  retired={g.get('retired', 0)}   {act_bar}")
        entries = g.get("entries") or []
        if entries:
            inner.append("")
            rows = [
                [
                    f"{_state_icon(str(e.get('status', '')))[0]} {e.get('status')}",
                    str(e.get("id", "")),
                    f"{e.get('surface')}/{e.get('scope')}",
                    str(e.get("cue", "")),
                ]
                for e in entries
            ]
            inner.extend(_table_lines(["Status", "ID", "Surface/Scope", "Cue"], rows, [12, 14, 24, 34]))
    else:
        loc = d["local"]
        m = loc.get("metrics") or {}
        pr = float(m.get("pass_rate", 0.0) or 0.0)
        eff = float(m.get("harness_efficiency", 0.0) or 0.0)
        inner.append(f"Local dir: {loc.get('dir', '—')}   enabled={loc.get('enabled')}   rev={loc.get('revision', 0)}   state={loc.get('state')}")
        inner.append(f"Pass rate: {_bar(pr, 10)}   Efficiency: {_bar(min(1.0, max(0.0, eff)), 10)}")
        entries = loc.get("entries") or []
        if entries:
            inner.append("")
            rows = []
            for e in entries:
                st = e.get("stats") or {}
                ic, _ = _state_icon(str(e.get("status", "")))
                rows.append([
                    f"{ic} {e.get('status')}",
                    str(e.get("id", "")),
                    f"{e.get('surface')}/{e.get('role', '*')}",
                    f"+{st.get('helped', 0)}/-{st.get('hurt', 0)}",
                    str(e.get("cue", "")),
                ])
            inner.extend(_table_lines(["Status", "ID", "Surface/Role", "Help", "Cue"], rows, [12, 12, 22, 7, 29]))
    if d.get("selected_entry"):
        inner.append("")
        inner.append("Selected entry detail:")
        inner.extend(json.dumps(d["selected_entry"], indent=2, ensure_ascii=False).splitlines())
    out = _box_lines(f"Harness ({mode})", inner)
    out.append("Actions: [g]lobal toggle  [i]ndex  [Enter]show  [+]promote  [-]demote  [R]eset --yes")
    return out


def _format_ops_lines(snap: dict, *, width: int = 96) -> list[str]:
    d = snap["data"]
    acp = d.get("acp") or {}
    tr = acp.get("resolved_transport", "missing")
    tr_ic = "✗" if tr == "missing" else "✓"
    primary_prov = acp.get("primary_provider") or "claude"
    acp_lines = [
        f"ACP ({primary_prov}): {tr_ic} transport={tr}   claude_cli={acp.get('claude_cli') or 'none'}   agent_acp={acp.get('claude_agent_acp') or 'none'}   oauth_env={acp.get('oauth_token_env', False)}"
    ]
    providers = acp.get("providers")
    if isinstance(providers, dict):
        prov_parts = []
        for pname in ("claude", "antigravity", "codex"):
            pinfo = providers.get(pname)
            if not isinstance(pinfo, dict):
                continue
            ptr = pinfo.get("resolved_transport") or "missing"
            p_ic = "✓" if ptr != "missing" else "✗"
            auth_tag = "oauth" if pinfo.get("oauth_ready") else "no-auth"
            prov_parts.append(f"{p_ic} {pname}={ptr}({auth_tag})")
        if prov_parts:
            acp_lines.append("Providers: " + "  ".join(prov_parts))
    if "repo_frontier" in acp:
        rf = acp["repo_frontier"]
        rw = acp["repo_workers"]
        acp_lines.append(
            f"Frontier: adapter={rf['adapter']}  mode={rf['mode']}  auth={rf['auth']}  model={rf['model'] or 'default'}  effort={rf['effort']}"
        )
        acp_lines.append(
            f"Workers: adapter={rw['adapter']}  endpoint={rw['endpoint'] or '—'}  key_file_present={rw['key_file_present']}"
        )
    out = _box_lines(f"Ops — repo={d['repo']} · configured={d['configured']}", acp_lines, width=width)

    tasks = d.get("bench_tasks") or []
    if tasks:
        t_rows = []
        for task in tasks:
            if "error" in task:
                t_rows.append(["✗ " + task["id"], "error", task["error"], "—"])
            else:
                t_rows.append(["✓ " + task["id"], task["language"], task["category"], "yes" if task["has_script"] else "no"])
        tw = max(44, width - 4)
        c1, c2, c4 = max(12, tw // 4), max(8, tw // 7), 7
        c3 = max(12, tw - c1 - c2 - c4 - 6)
        t_inner = _table_lines(["Task ID", "Language", "Category", "Script"], t_rows, [c1, c2, c3, c4])
    else:
        t_inner = ["(no bench tasks found)"]
    out.extend(_box_lines(f"Bench Tasks ({len(tasks)})", t_inner, width=width))

    if d.get("latest_bench"):
        lb = d["latest_bench"]
        lb_lines = [f"Report: {lb.get('path')}"]
        arms = ((lb.get("summary") or {}).get("arms") or {})
        for arm_name, arm_stats in sorted(arms.items()):
            pr = float(arm_stats.get("pass_rate", 0.0) or 0.0)
            ws = float(arm_stats.get("worker_share", 0.0) or 0.0)
            lb_lines.append(
                f"  {_pad_right(arm_name, 8)} pass {_bar(pr, 10)} ({arm_stats.get('passed', 0)}/{arm_stats.get('runs', 0)})  worker {_bar(ws, 8)}  tok={arm_stats.get('tokens', 0)}"
            )
        for ln in str(lb.get("markdown", "")).splitlines():
            lb_lines.append(ln)
        out.extend(_box_lines("Latest Bench Report", lb_lines, width=width))

    if d.get("supervision_reports"):
        sup_lines = [f"• {rep['file']}: {rep['summary']}" for rep in d["supervision_reports"]]
        out.extend(_box_lines("Recent Supervise Reports", sup_lines, width=width))

    out.append("Pre-config helpers: init, adopt, install-skill")
    out.append("Actions: [a]cp status  [P]robe  [C]onfigure  [s]upervise  [b]ench list  [B]ench dry-run  [I]nit  [A]dopt  [K]install-skill")
    return out


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
    counts = sb.get("counts") or {}
    tabs = "  ".join(
        f"[{_tab_label(idx, v, counts).strip()}]" if v == view else _tab_label(idx, v, counts).strip()
        for idx, v in enumerate(VIEWS, start=1)
    )
    header = " │ ".join(_status_pills(sb))
    rule = "─" * min(96, max(len(header), len(tabs)))
    lines = [header, tabs, rule, *_FORMATTERS[view](snap)]
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
        if len(argv_tokens) == 1:
            full_argv.extend(["list", "--dir", str(resolved_repo / "bench")])
        elif argv_tokens[1] in ("list", "run"):
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
        self.detail_scroll = 0
        self.search_query = ""
        self.card_id: str | None = None
        self.attempt: int | None = None
        self.dossier_json_mode = False
        self.board_filter: dict[str, str] = {}
        self.harness_global = False
        self.harness_entry: str | None = None
        self.help_open = False
        self.help_scroll = 0
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
        self._clamp_selection()
        return self.snapshot

    def set_search(self, query: str) -> None:
        self.search_query = str(query or "").strip()
        self.scroll = 0
        self.selected_index = 0
        self.detail_scroll = 0
        self._clamp_selection()
        self.status_message = f"filter: /{self.search_query}" if self.search_query else "filter cleared"

    def set_view(self, view: str) -> None:
        if view not in VIEWS:
            raise UsageError(f"unknown view {view!r}")
        self.view = view
        self.scroll = 0
        self.selected_index = 0
        self.detail_scroll = 0
        self.refresh()

    def cycle_view(self, step: int = 1) -> None:
        idx = (VIEWS.index(self.view) + step) % len(VIEWS)
        self.set_view(VIEWS[idx])

    def _raw_items_for_view(self) -> list[dict]:
        data = self.snapshot.get("data") or {}
        if self.view == "cards":
            return list(data.get("cards") or [])
        if self.view == "board":
            return list(data.get("posts") or [])
        if self.view == "swarm":
            return list(data.get("members") or [])
        if self.view == "harness":
            if data.get("global_mode"):
                return list((data.get("global") or {}).get("entries") or [])
            return list((data.get("local") or {}).get("entries") or [])
        return []

    @staticmethod
    def _item_search_blob(view: str, item: dict) -> str:
        if view == "cards":
            parts = [
                str(item.get("id", "")),
                str(item.get("title", "")),
                str(item.get("state", "")),
                str(item.get("category", "")),
                str(item.get("unit", "")),
                " ".join(item.get("touches") or []),
                " ".join(item.get("deps") or []),
                " ".join(item.get("collides_with") or []),
                " ".join(item.get("branches") or []),
                str(item.get("done_text", "")),
            ]
            return " ".join(parts).lower()
        if view == "board":
            parts = [
                f"#{item.get('seq', '')}",
                str(item.get("from", "")),
                " ".join(item.get("to") or []),
                str(item.get("kind", "")),
                str(item.get("card", "")),
                str(item.get("body", "")),
                " ".join(item.get("evidence") or []),
            ]
            return " ".join(parts).lower()
        if view == "swarm":
            parts = [
                str(item.get("name", "")),
                str(item.get("role", "")),
                str(item.get("kind", "")),
                str(item.get("card", "")),
                str(item.get("tier", "")),
                str(item.get("effort", "")),
                str(item.get("state", "")),
            ]
            return " ".join(parts).lower()
        if view == "harness":
            parts = [
                str(item.get("id", "")),
                str(item.get("status", "")),
                str(item.get("surface", "")),
                str(item.get("role", "")),
                str(item.get("scope", "")),
                str(item.get("language", "")),
                str(item.get("kind", "")),
                str(item.get("failure_class", "")),
                str(item.get("cue", "")),
                str(item.get("lesson", "")),
                str(item.get("project", "")),
            ]
            return " ".join(parts).lower()
        return ""

    def filtered_items(self) -> list[dict]:
        raw = self._raw_items_for_view()
        q = self.search_query.lower().strip()
        items = [it for it in raw if q in self._item_search_blob(self.view, it)] if q else raw
        if items:
            self.selected_index = min(max(0, self.selected_index), len(items) - 1)
        else:
            self.selected_index = 0
        return items

    def _clamp_selection(self) -> None:
        items = self._raw_items_for_view()
        q = self.search_query.lower().strip()
        if q and self.view in SELECTION_VIEWS:
            items = [it for it in items if q in self._item_search_blob(self.view, it)]
        if items:
            self.selected_index = min(max(0, self.selected_index), len(items) - 1)
        else:
            self.selected_index = 0

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
            cards = self.filtered_items()
            if cards:
                idx = min(max(0, self.selected_index), len(cards) - 1)
                return str(cards[idx].get("id", ""))
        return self.card_id or ""

    def _selected_board_post(self) -> dict | None:
        if self.view != "board":
            return None
        posts = self.filtered_items()
        if not posts:
            return None
        idx = min(max(0, self.selected_index), len(posts) - 1)
        return posts[idx]

    def _selected_swarm_member(self) -> dict | None:
        if self.view != "swarm":
            return None
        members = self.filtered_items()
        if not members:
            return None
        idx = min(max(0, self.selected_index), len(members) - 1)
        return members[idx]

    def _selected_harness_entry(self) -> dict | None:
        if self.view != "harness":
            return None
        entries = self.filtered_items()
        if not entries:
            return None
        idx = min(max(0, self.selected_index), len(entries) - 1)
        return entries[idx]

    def _cycle_dossier_card(self, step: int) -> None:
        if self.view != "dossier":
            return
        avail = (self.snapshot.get("data") or {}).get("available") or []
        cards_seen = list(dict.fromkeys(str(a.get("card", "")) for a in avail if a.get("card")))
        if not cards_seen:
            return
        cur = self.card_id or str((self.snapshot.get("data") or {}).get("card", ""))
        pos = cards_seen.index(cur) if cur in cards_seen else 0
        self.card_id = cards_seen[(pos + step) % len(cards_seen)]
        self.scroll = 0
        self.refresh()

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
                raw = json.dumps(payload, indent=2, ensure_ascii=False).splitlines()
            else:
                raw = _FORMATTERS[self.view](self.snapshot)
        else:
            raw = _FORMATTERS[self.view](self.snapshot)
        q = self.search_query.lower().strip()
        if q and self.view not in SELECTION_VIEWS:
            matched = [ln for ln in raw if q in ln.lower()]
            return matched or [f"No matches for '/{self.search_query}' (press Esc to clear)."]
        return raw

    def handle_key(self, key: int | str) -> bool:
        """Process one key event. Return False when the user requests quit."""
        if isinstance(key, str) and len(key) == 1:
            code = ord(key)
        elif isinstance(key, int):
            code = key
        else:
            return True

        if code == getattr(curses, "KEY_RESIZE", 410):
            return True

        if code == 27:  # Escape
            if self.help_open:
                self.help_open = False
                self.help_scroll = 0
            elif self.modal_lines:
                self.close_modal()
            elif self.search_query:
                self.set_search("")
            return True

        if self.help_open:
            help_len = len(_help_lines())
            if code in (ord("?"), ord("q"), ord("\n"), ord("\r")):
                self.help_open = False
                self.help_scroll = 0
            elif code in (curses.KEY_DOWN, ord("j")):
                self.help_scroll = min(max(0, help_len - 1), self.help_scroll + 1)
            elif code in (curses.KEY_UP, ord("k")):
                self.help_scroll = max(0, self.help_scroll - 1)
            elif code == curses.KEY_NPAGE:
                self.help_scroll = min(max(0, help_len - 1), self.help_scroll + 8)
            elif code == curses.KEY_PPAGE:
                self.help_scroll = max(0, self.help_scroll - 8)
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
            self.help_scroll = 0
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

        if self.view in SELECTION_VIEWS:
            items = self.filtered_items()
            limit = max(0, len(items) - 1)
            if code in (curses.KEY_DOWN, ord("j")):
                if items:
                    self.selected_index = min(limit, self.selected_index + 1)
                    self.detail_scroll = 0
                return True
            if code in (curses.KEY_UP, ord("k")):
                if items:
                    self.selected_index = max(0, self.selected_index - 1)
                    self.detail_scroll = 0
                return True
            if code == curses.KEY_NPAGE:
                if items:
                    self.selected_index = min(limit, self.selected_index + 8)
                    self.detail_scroll = 0
                return True
            if code == curses.KEY_PPAGE:
                if items:
                    self.selected_index = max(0, self.selected_index - 8)
                    self.detail_scroll = 0
                return True
            if code == ord("["):
                self.detail_scroll = max(0, self.detail_scroll - 3)
                return True
            if code == ord("]"):
                self.detail_scroll += 3
                return True
        else:
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
            if self.view == "dossier" and code == ord("["):
                self._cycle_dossier_card(-1)
                return True
            if self.view == "dossier" and code == ord("]"):
                self._cycle_dossier_card(1)
                return True

        if self.view == "harness" and code == ord("g"):
            self.harness_global = not self.harness_global
            self.selected_index = 0
            self.detail_scroll = 0
            self.scroll = 0
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
        "  j/k, Up/Down Move selection (cards/board/swarm/harness) or scroll view/modal",
        "  PgUp / PgDn  Page move or page scroll",
        "  [ / ]        Scroll detail inspector pane (or cycle card in dossier)",
        "  /            Live filter items or lines (Esc clears active filter)",
        "  r            Refresh snapshot",
        "  :            Command bar (run any `openfarm ...` command in-process)",
        "  ?            Toggle this help overlay",
        "  q / Esc      Close help/modal, clear filter, or quit",
        "",
        "View shortcuts:",
        "  Overview:    [p]ause  [u]resume  [x]stop  [n]run",
        "  Cards:       [Enter]dossier  [c]heck  [b]aseline  [d]one  [l]release  [v]erdict  [e]scape",
        "  Dossier:     [m]d/json toggle  [a]ttempt #  [[/]] cycle card",
        "  Board:       [Enter]open 1st evidence  [f]ilter  [v]iew evidence  [n]post  [a]nswer  [w]ait",
        "  Swarm:       [J]oin  [S]tart  [E]set  [H]int  [B]lock  [X]remove",
        "  Harness:     [g]lobal toggle  [i]ndex  [Enter]show  [+]promote  [-]demote  [R]eset",
        "  Ops:         [a]cp status  [P]robe  [C]onfigure  [s]upervise  [b]ench list  [B]ench dry-run",
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


def _theme_attr(theme: dict[str, int], key: str) -> int:
    if not key:
        return 0
    if key == "info_bold":
        return theme.get("info", 0) | curses.A_BOLD
    return theme.get(key, 0)


def _draw_segments(
    win,
    y: int,
    x: int,
    max_width: int,
    segments: list[tuple[str, str]],
    theme: dict[str, int],
    *,
    fill_attr: int = 0,
) -> None:
    if max_width <= 0:
        return
    cur_x = x
    rem = max_width
    for text, key in segments:
        if rem <= 0:
            break
        if not text:
            continue
        chunk = _truncate(text, rem) if _display_width(text) > rem else text
        if not chunk:
            continue
        attr = fill_attr if fill_attr else _theme_attr(theme, key)
        _safe_addstr(win, y, cur_x, chunk, attr)
        used = _display_width(chunk)
        cur_x += used
        rem -= used


def _draw_box_frame(
    win,
    y: int,
    x: int,
    h: int,
    w: int,
    title: str,
    theme: dict[str, int],
    *,
    footer_hint: str = "",
    clear: bool = False,
) -> tuple[int, int, int, int]:
    """Draw a Unicode rounded box frame and return (inner_y, inner_x, inner_h, inner_w)."""
    max_y, max_x = win.getmaxyx()
    w = min(w, max_x - x - 1)
    h = min(h, max_y - y)
    if w < 6 or h < 3:
        return (y, x, max(1, h), max(1, w))

    border_attr = theme.get("border", 0)
    title_attr = theme.get("info", 0) | curses.A_BOLD
    inner_w = max(1, w - 4)
    if clear:
        blank = " " * (w - 2)
        for row in range(y + 1, y + h - 1):
            _safe_addstr(win, row, x + 1, blank, 0)

    t_text = _truncate(title, max(2, w - 6))
    _safe_addstr(win, y, x, "╭─ ", border_attr)
    _safe_addstr(win, y, x + 3, t_text, title_attr)
    after_x = x + 3 + _display_width(t_text)
    rem = max(0, (x + w - 1) - after_x)
    if rem > 0:
        fill = " " + ("─" * max(0, rem - 1)) if rem >= 1 else ""
        _safe_addstr(win, y, after_x, fill, border_attr)
    _safe_addstr(win, y, x + w - 1, "╮", border_attr)

    for row in range(y + 1, y + h - 1):
        _safe_addstr(win, row, x, "│", border_attr)
        _safe_addstr(win, row, x + w - 1, "│", border_attr)

    hint = _truncate(footer_hint, max(0, w - 6)) if footer_hint else ""
    if hint and w >= _display_width(hint) + 6:
        left_dashes = max(1, (w - 4) - _display_width(hint))
        _safe_addstr(win, y + h - 1, x, "╰" + ("─" * left_dashes) + " ", border_attr)
        hx = x + 1 + left_dashes + 1
        _safe_addstr(win, y + h - 1, hx, hint, theme.get("muted", border_attr))
        rx = hx + _display_width(hint)
        tail = max(0, (x + w - 1) - rx)
        if tail > 0:
            _safe_addstr(win, y + h - 1, rx, "─" * tail, border_attr)
        _safe_addstr(win, y + h - 1, x + w - 1, "╯", border_attr)
    else:
        _safe_addstr(win, y + h - 1, x, "╰" + ("─" * max(0, w - 2)) + "╯", border_attr)

    return (y + 1, x + 2, max(1, h - 2), inner_w)


def _styled_text_lines(
    raw_lines: list[str],
    width: int,
    *,
    default_key: str = "",
) -> list[list[tuple[str, str]]]:
    out: list[list[tuple[str, str]]] = []
    for raw in raw_lines:
        for wrapped in _wrap_text(raw, width):
            key = _classify_line_theme(wrapped, default_key=default_key)
            out.append([(wrapped, key)])
    return out


def _render_styled_pane(
    win,
    iy: int,
    ix: int,
    ih: int,
    iw: int,
    styled_lines: list[list[tuple[str, str]]],
    scroll: int,
    theme: dict[str, int],
) -> tuple[int, int, int]:
    total = len(styled_lines)
    start = min(max(0, scroll), max(0, total - ih)) if total > ih else 0
    for r in range(ih):
        idx = start + r
        if idx >= total:
            break
        _draw_segments(win, iy + r, ix, iw, styled_lines[idx], theme)
    return (start + 1 if total else 0, min(total, start + ih), total)


def _build_card_detail_lines(card: dict, width: int) -> list[list[tuple[str, str]]]:
    ic, st_key = _state_icon(card.get("state", ""))
    lines: list[list[tuple[str, str]]] = [
        [(f"{ic} {card.get('id', '')}", "info_bold"), (" · ", "muted"), (str(card.get("title", "")), "")],
        [
            ("State: ", "muted"),
            (f"{ic} {card.get('state', '')}", st_key),
            ("   Cat: ", "muted"),
            (str(card.get("category") or "—"), ""),
            ("   Unit: ", "muted"),
            (str(card.get("unit") or "."), ""),
        ],
    ]
    touches = ", ".join(card.get("touches") or []) or "—"
    deps = ", ".join(card.get("deps") or []) or "—"
    cols = card.get("collides_with") or []
    lines.append([("Touches: ", "muted"), (touches, "info")])
    lines.append([("Deps: ", "muted"), (deps, "")])
    lines.append([
        ("Collisions: ", "muted"),
        (f"⚠ {', '.join(cols)}" if cols else "✓ none", "warn" if cols else "ok"),
    ])
    br = (card.get("branches") or ["—"])[0]
    unm = int(card.get("unmerged_commits", 0) or 0)
    lines.append([
        ("Branch: ", "muted"),
        (br, "info"),
        (f"  (+{unm} unmerged)" if unm else "  (0 unmerged)", "warn" if unm else "muted"),
    ])
    verdict = card.get("verdict") or {}
    esc = int(card.get("escapes_count", 0) or 0)
    if verdict or esc:
        v_txt = str(verdict.get("verdict") or "—")
        _, vk = _state_icon(v_txt)
        lines.append([("Verdict: ", "muted"), (v_txt, vk), (f"   Escapes: {esc}", "warn" if esc else "muted")])

    done_txt = str(card.get("done_text") or "").strip()
    if done_txt:
        lines.append([("", "")])
        lines.append([("## Done criteria", "info_bold")])
        lines.extend(_styled_text_lines(done_txt.splitlines(), width))

    diff_txt = str(card.get("diff_preview") or "").strip()
    if diff_txt:
        lines.append([("", "")])
        lines.append([("## Diff preview", "info_bold")])
        for raw_d in diff_txt.splitlines()[:40]:
            dk = _classify_line_theme(raw_d)
            lines.append([(_truncate(raw_d, width), dk)])
    return lines


def _build_board_detail_lines(post: dict, width: int) -> list[list[tuple[str, str]]]:
    ic, kk = _post_icon(post.get("kind", ""))
    to_txt = ", ".join(post.get("to") or []) or "—"
    lines: list[list[tuple[str, str]]] = [
        [
            (f"Post #{post.get('seq', '')}", "info_bold"),
            ("  ", ""),
            (f"{ic} {post.get('kind', '')}", kk),
            ("  at ", "muted"),
            (str(post.get("at", "")), "muted"),
        ],
        [
            ("Route: ", "muted"),
            (str(post.get("from", "")), "info"),
            (" → ", "muted"),
            (to_txt, "accent"),
        ],
        [
            ("Card: ", "muted"),
            (str(post.get("card") or "—"), ""),
            ("   Attempt: ", "muted"),
            (str(post.get("attempt") or 0), ""),
            ("   Ref: ", "muted"),
            (f"#{post['ref']}" if post.get("ref") is not None else "—", ""),
        ],
    ]
    body = str(post.get("body") or "")
    lines.append([("", "")])
    lines.append([("## Body", "info_bold")])
    lines.extend(_styled_text_lines(body.splitlines(), width))

    expects = post.get("expects")
    if expects:
        lines.append([("", "")])
        lines.append([("## Expects", "info_bold")])
        lines.extend(_styled_text_lines(json.dumps(expects, indent=2, ensure_ascii=False).splitlines(), width, default_key="warn"))

    evs = post.get("evidence") or []
    lines.append([("", "")])
    lines.append([(f"## Evidence ({len(evs)})", "info_bold")])
    if evs:
        for i, ev in enumerate(evs, start=1):
            lines.append([(f"[{i}] ", "key"), (str(ev), "info")])
        lines.append([("Press [Enter] to open first evidence file in modal", "muted")])
    else:
        lines.append([("(none)", "muted")])
    return lines


def _build_swarm_detail_lines(
    member: dict | None,
    data: dict,
    width: int,
) -> list[list[tuple[str, str]]]:
    lines: list[list[tuple[str, str]]] = []
    if member is not None:
        ic, sk = _state_icon(member.get("state", ""))
        members = data.get("members") or []
        tot_roster = sum(int(m.get("tokens_total", 0) or 0) for m in members) or 1
        m_tot = int(member.get("tokens_total", 0) or 0)
        m_in = int(member.get("tokens_in", 0) or 0)
        m_out = int(member.get("tokens_out", 0) or 0)
        in_den = m_tot or 1
        lines.extend([
            [(f"{ic} {member.get('name', '')}", "info_bold"), (f"  [{member.get('kind', '')} / {member.get('role', '')}]", "accent")],
            [
                ("State: ", "muted"),
                (f"{ic} {member.get('state', '')}", sk),
                ("   Owner: ", "muted"),
                (str(member.get("owner_kind") or "—"), ""),
                ("   Card: ", "muted"),
                (str(member.get("card") or "—"), "info"),
            ],
            [
                ("Tier: ", "muted"),
                (str(member.get("tier") or "—"), ""),
                ("   Effort: ", "muted"),
                (str(member.get("effort") or "—"), ""),
                ("   Attempts: ", "muted"),
                (str(member.get("attempts", 0)), ""),
            ],
            [
                ("Wait since: ", "muted"),
                (str(member.get("wait_since") or "—"), "warn" if member.get("wait_since") else "muted"),
                ("   Last seq: ", "muted"),
                (str(member.get("last_read_seq", 0)), ""),
            ],
            [("", "")],
            [("## Token share", "info_bold")],
            [("Roster ", "muted"), (_bar(m_tot / tot_roster, 12), "info"), (f" ({m_tot} tok)", "muted")],
            [("Input  ", "muted"), (_bar(m_in / in_den, 12), "ok"), (f" ({m_in})", "muted")],
            [("Output ", "muted"), (_bar(m_out / in_den, 12), "accent"), (f" ({m_out})", "muted")],
        ])
    else:
        lines.append([("No member selected.", "muted")])

    sw = data.get("swarm") or {}
    lines.extend([
        [("", "")],
        [("## Seat config", "info_bold")],
        [
            ("Enabled: ", "muted"),
            ("yes" if sw.get("enabled") else "no", "ok" if sw.get("enabled") else "muted"),
            ("   Coord: ", "muted"),
            (f"{sw.get('coordinator')} ({sw.get('coordinator_effort')})", ""),
            ("   Sup: ", "muted"),
            (f"{sw.get('supervisor')} ({sw.get('supervisor_effort')})", ""),
        ],
        [
            ("Default tier: ", "muted"),
            (str(sw.get("default_tier", "default")), "info"),
            ("   Escalate: ", "muted"),
            (str(sw.get("escalate_after", 2)), ""),
            ("   Stall: ", "muted"),
            (f"{sw.get('stall_after_s', 300)}s", ""),
        ],
    ])
    tiers = data.get("tiers") or []
    lines.append([("", "")])
    lines.append([("## Tiers", "info_bold")])
    if not tiers:
        lines.append([("(no tiers configured)", "muted")])
    for t in tiers:
        lines.append([
            (f"• {t['name']}: ", "info"),
            (str(t["model"]), ""),
            (f"  [{','.join(t['efforts']) or 'none'}]", "muted"),
        ])
    return lines


def _build_harness_detail_lines(entry: dict | None, data: dict, width: int) -> list[list[tuple[str, str]]]:
    if entry is None:
        return [[("No harness entry selected.", "muted")]]
    st_txt = str(entry.get("status", ""))
    ic, sk = _state_icon(st_txt)
    scope_or_role = entry.get("scope") if data.get("global_mode") else entry.get("role", "*")
    lines: list[list[tuple[str, str]]] = [
        [(f"{ic} {entry.get('id', '')}", "info_bold"), (f"  [{entry.get('surface', '')}/{scope_or_role}]", "accent")],
        [
            ("Status: ", "muted"),
            (f"{ic} {st_txt}", sk),
            ("   Lang: ", "muted"),
            (str(entry.get("language") or "*"), ""),
            ("   Kind: ", "muted"),
            (str(entry.get("kind") or "*"), ""),
        ],
        [("Failure class: ", "muted"), (str(entry.get("failure_class") or "—"), "warn")],
    ]
    stats = entry.get("stats") or {}
    helped = int(stats.get("helped", 0) or 0)
    hurt = int(stats.get("hurt", 0) or 0)
    conf = entry.get("confidence")
    if helped or hurt:
        ratio = helped / (helped + hurt)
        lines.append([("Signal: ", "muted"), (_bar(ratio, 12), "ok" if ratio >= 0.5 else "err"), (f" (+{helped}/-{hurt})", "muted")])
    if conf is not None:
        lines.append([("Confidence: ", "muted"), (_bar(float(conf), 12), "info")])
    if entry.get("project"):
        lines.append([("Project: ", "muted"), (str(entry.get("project")), "")])

    cue = str(entry.get("cue") or "").strip()
    if cue:
        lines.append([("", "")])
        lines.append([("## Cue", "info_bold")])
        lines.extend(_styled_text_lines(cue.splitlines(), width))

    lesson = str(entry.get("lesson") or "").strip()
    if lesson:
        lines.append([("", "")])
        lines.append([("## Lesson", "info_bold")])
        lines.extend(_styled_text_lines(lesson.splitlines(), width))

    sources = entry.get("sources") or entry.get("applied_cards") or []
    if sources:
        lines.append([("", "")])
        lines.append([("## Sources", "info_bold")])
        for src in list(sources)[:12]:
            lines.append([(f"• {src}", "muted")])
    return lines


def _render_master_list(
    win,
    ctrl: TuiController,
    items: list[dict],
    iy: int,
    ix: int,
    ih: int,
    iw: int,
    theme: dict[str, int],
) -> None:
    if ih <= 0 or iw <= 0:
        return
    if ctrl.view == "cards":
        head = "  " + _pad_right("St", 2) + " " + _pad_right("ID", 9) + " " + _pad_right("Title", max(8, iw - 23)) + " " + _pad_right("Git", 8)
    elif ctrl.view == "board":
        head = "  " + _pad_right("Seq", 4) + " " + _pad_right("Kind", 10) + " " + _pad_right("Route", 14) + " " + _pad_right("Preview", max(6, iw - 33))
    elif ctrl.view == "swarm":
        head = "  " + _pad_right("St", 2) + " " + _pad_right("Name", 11) + " " + _pad_right("Role", 11) + " " + _pad_right("Card", 8) + " " + _pad_right("Tok", max(4, iw - 37))
    else:
        head = "  " + _pad_right("St", 2) + " " + _pad_right("ID", 10) + " " + _pad_right("Surface", 14) + " " + _pad_right("Cue", max(6, iw - 31))

    _safe_addstr(win, iy, ix, _truncate(head, iw), theme.get("muted", 0) | curses.A_BOLD)
    list_top = iy + 1
    list_h = max(1, ih - 1)
    if ih >= 3:
        _safe_addstr(win, iy + 1, ix, "─" * iw, theme.get("border", 0))
        list_top = iy + 2
        list_h = max(1, ih - 2)

    if not items:
        msg = f"(no matches for '/{ctrl.search_query}')" if ctrl.search_query else "(empty)"
        _safe_addstr(win, list_top, ix, _truncate(msg, iw), theme.get("muted", 0))
        return

    sel = min(max(0, ctrl.selected_index), len(items) - 1)
    if sel < ctrl.scroll:
        ctrl.scroll = sel
    elif sel >= ctrl.scroll + list_h:
        ctrl.scroll = sel - list_h + 1
    ctrl.scroll = max(0, min(ctrl.scroll, max(0, len(items) - list_h)))

    for row in range(list_h):
        idx = ctrl.scroll + row
        if idx >= len(items):
            break
        it = items[idx]
        is_sel = idx == sel
        prefix = "▸ " if is_sel else "  "
        if ctrl.view == "cards":
            ic, sk = _state_icon(it.get("state", ""))
            br_unm = f"+{it['unmerged_commits']}" if it.get("unmerged_commits") else ("⚠" if it.get("collides_with") else "—")
            title_w = max(8, iw - 23)
            segs = [
                (prefix, "selected" if is_sel else "muted"),
                (f"{ic} ", sk),
                (_pad_right(str(it.get("id", "")), 9), "info_bold"),
                (" ", ""),
                (_pad_right(str(it.get("title", "")), title_w), ""),
                (" ", ""),
                (_pad_right(br_unm, 8), "warn" if it.get("unmerged_commits") or it.get("collides_with") else "muted"),
            ]
        elif ctrl.view == "board":
            ic, sk = _post_icon(it.get("kind", ""))
            to_txt = ",".join(it.get("to") or []) or "—"
            route = f"{it.get('from', '')}→{to_txt}"
            prev_w = max(6, iw - 33)
            segs = [
                (prefix, "selected" if is_sel else "muted"),
                (_pad_right(f"#{it.get('seq', '')}", 4), "info_bold"),
                (" ", ""),
                (f"{ic} {_pad_right(str(it.get('kind', '')), 8)}", sk),
                (" ", ""),
                (_pad_right(route, 14), ""),
                (" ", ""),
                (_pad_right(str(it.get("body_preview", "")), prev_w), "muted"),
            ]
        elif ctrl.view == "swarm":
            ic, sk = _state_icon(it.get("state", ""))
            tok_w = max(4, iw - 37)
            segs = [
                (prefix, "selected" if is_sel else "muted"),
                (f"{ic} ", sk),
                (_pad_right(str(it.get("name", "")), 11), "info_bold"),
                (" ", ""),
                (_pad_right(str(it.get("role", "")), 11), "accent"),
                (" ", ""),
                (_pad_right(str(it.get("card") or "—"), 8), ""),
                (" ", ""),
                (_pad_right(str(it.get("tokens_total", 0)), tok_w), "info"),
            ]
        else:
            ic, sk = _state_icon(it.get("status", ""))
            cue_w = max(6, iw - 31)
            surf = str(it.get("surface", ""))
            segs = [
                (prefix, "selected" if is_sel else "muted"),
                (f"{ic} ", sk),
                (_pad_right(str(it.get("id", "")), 10), "info_bold"),
                (" ", ""),
                (_pad_right(surf, 14), "accent"),
                (" ", ""),
                (_pad_right(str(it.get("cue", "")), cue_w), ""),
            ]

        if is_sel:
            _safe_addstr(win, list_top + row, ix, " " * iw, theme.get("selected", 0))
            _draw_segments(win, list_top + row, ix, iw, segs, theme, fill_attr=theme.get("selected", 0))
        else:
            _draw_segments(win, list_top + row, ix, iw, segs, theme)


def _render_split_master_detail(
    win,
    ctrl: TuiController,
    body_top: int,
    body_height: int,
    max_x: int,
    theme: dict[str, int],
) -> None:
    items = ctrl.filtered_items()
    data = ctrl.snapshot.get("data") or {}
    sel = min(max(0, ctrl.selected_index), len(items) - 1) if items else 0
    selected_item = items[sel] if items else None

    if ctrl.view == "cards":
        left_title = f"Cards ({len(items)})"
        right_title = f"Card: {selected_item['id']}" if selected_item else "Card Inspector"
    elif ctrl.view == "board":
        uq = len(data.get("unanswered_questions") or [])
        extra = f" · ?{uq}" if uq else ""
        left_title = f"Board ({len(items)}){extra}"
        right_title = f"Post #{selected_item['seq']}" if selected_item else "Post Inspector"
    elif ctrl.view == "swarm":
        left_title = f"Swarm Roster ({len(items)})"
        right_title = f"Member: {selected_item['name']}" if selected_item else "Swarm Inspector"
    else:
        mode = "GLOBAL" if data.get("global_mode") else "LOCAL"
        left_title = f"Harness [{mode}] ({len(items)})"
        right_title = f"Entry: {selected_item.get('id', '')}" if selected_item else "Harness Inspector"

    left_hint = f"[{sel + 1}/{len(items)}]" if items else "[0/0]"

    if max_x >= 108:
        left_w = max(44, int(max_x * 0.48))
        right_x = left_w + 1
        right_w = max(24, max_x - right_x - 1)
        iy1, ix1, ih1, iw1 = _draw_box_frame(win, body_top, 0, body_height, left_w, left_title, theme, footer_hint=left_hint)
        _render_master_list(win, ctrl, items, iy1, ix1, ih1, iw1, theme)
        d_top, d_left, d_h, d_w = body_top, right_x, body_height, right_w
    else:
        master_h = max(4, min(body_height - 4, body_height // 2))
        detail_h = max(4, body_height - master_h)
        full_w = max(20, max_x - 1)
        iy1, ix1, ih1, iw1 = _draw_box_frame(win, body_top, 0, master_h, full_w, left_title, theme, footer_hint=left_hint)
        _render_master_list(win, ctrl, items, iy1, ix1, ih1, iw1, theme)
        d_top, d_left, d_h, d_w = body_top + master_h, 0, detail_h, full_w

    detail_iw = max(1, d_w - 4)
    detail_ih = max(1, d_h - 2)
    if ctrl.view == "cards":
        detail_styled = _build_card_detail_lines(selected_item, detail_iw) if selected_item else [[("No card selected.", "muted")]]
    elif ctrl.view == "board":
        detail_styled = _build_board_detail_lines(selected_item, detail_iw) if selected_item else [[("No board post selected.", "muted")]]
    elif ctrl.view == "swarm":
        detail_styled = _build_swarm_detail_lines(selected_item, data, detail_iw)
    else:
        detail_styled = _build_harness_detail_lines(selected_item, data, detail_iw)

    tot_d = len(detail_styled)
    ctrl.detail_scroll = min(ctrl.detail_scroll, max(0, tot_d - detail_ih))
    start_d = min(max(0, ctrl.detail_scroll), max(0, tot_d - detail_ih)) if tot_d > detail_ih else 0
    hint = f"[{start_d + 1 if tot_d else 0}-{min(tot_d, start_d + detail_ih)}/{tot_d}] [/]" if tot_d > detail_ih else ""
    iy2, ix2, ih2, iw2 = _draw_box_frame(win, d_top, d_left, d_h, d_w, right_title, theme, footer_hint=hint)
    _render_styled_pane(win, iy2, ix2, ih2, iw2, detail_styled, ctrl.detail_scroll, theme)


def _styled_box_segment_line(line: str, width: int) -> list[tuple[str, str]]:
    """Colorize rounded box borders (`╭`, `│`, `╰`) separately from their inner text."""
    clipped = _truncate(line, width)
    if clipped.startswith(("╭", "╰")):
        if clipped.startswith("╭─ ") and " ─" in clipped[3:]:
            end_title = clipped.find(" ─", 3)
            return [
                ("╭─ ", "border"),
                (clipped[3:end_title], "info_bold"),
                (clipped[end_title:], "border"),
            ]
        return [(clipped, "border")]
    if clipped.startswith("│ ") and clipped.endswith(" │") and len(clipped) >= 4:
        inner = clipped[2:-2]
        return [("│ ", "border"), (inner, _classify_line_theme(inner)), (" │", "border")]
    return [(clipped, _classify_line_theme(clipped))]


def _render_scrollable_view(
    win,
    ctrl: TuiController,
    body_top: int,
    body_height: int,
    max_x: int,
    theme: dict[str, int],
) -> None:
    full_w = max(24, max_x - 1)
    q = ctrl.search_query.lower().strip()

    if ctrl.view in ("overview", "ops"):
        if ctrl.view == "overview":
            raw_lines = _format_overview_lines(ctrl.snapshot, width=full_w)
        else:
            raw_lines = _format_ops_lines(ctrl.snapshot, width=full_w)
        if q:
            matched = [ln for ln in raw_lines if q in ln.lower()]
            inner = matched or [f"No matches for '/{ctrl.search_query}' (press Esc to clear)."]
            raw_lines = _box_lines(f"{VIEW_LABELS[ctrl.view]} · Filter /{ctrl.search_query}", inner, width=full_w)
        styled = [_styled_box_segment_line(ln, full_w) for ln in raw_lines]
        ctrl.scroll = min(ctrl.scroll, max(0, len(styled) - body_height))
        _render_styled_pane(win, body_top, 0, body_height, full_w, styled, ctrl.scroll, theme)
        return

    title = f"Dossier: {(ctrl.snapshot.get('data') or {}).get('card') or 'none'}"
    if ctrl.dossier_json_mode:
        title += " [JSON]"
    if ctrl.search_query:
        title += f" (/{ctrl.search_query})"

    if ctrl.dossier_json_mode:
        payload = (ctrl.snapshot.get("data") or {}).get("payload")
        raw_lines = (
            json.dumps(payload, indent=2, ensure_ascii=False).splitlines()
            if payload is not None
            else _format_dossier_lines(ctrl.snapshot, width=max(24, full_w - 4))
        )
    else:
        raw_lines = _format_dossier_lines(ctrl.snapshot, width=max(24, full_w - 4))
    if q:
        matched = [ln for ln in raw_lines if q in ln.lower()]
        raw_lines = matched or [f"No matches for '/{ctrl.search_query}' (press Esc to clear)."]

    inner_w = max(12, full_w - 4)
    styled = [
        _styled_box_segment_line(w, inner_w)
        if w.startswith(("╭", "╰", "│ "))
        else [(w, _classify_line_theme(w))]
        for ln in raw_lines
        for w in _wrap_text(ln, inner_w)
    ]
    inner_h = max(1, body_height - 2)
    ctrl.scroll = min(ctrl.scroll, max(0, len(styled) - inner_h))
    total = len(styled)
    start = min(max(0, ctrl.scroll), max(0, total - inner_h)) if total > inner_h else 0
    hint = f"[{start + 1 if total else 0}-{min(total, start + inner_h)}/{total}]" if total > inner_h else ""
    iy, ix, ih, iw = _draw_box_frame(win, body_top, 0, body_height, full_w, title, theme, footer_hint=hint)
    _render_styled_pane(win, iy, ix, ih, iw, styled, ctrl.scroll, theme)


def _render_modal_box(
    win,
    title: str,
    raw_lines: list[str],
    scroll: int,
    max_y: int,
    max_x: int,
    theme: dict[str, int],
) -> int:
    box_w = max(12, min(max_x - 2, max(54, int(max_x * 0.88))))
    inner_w = max(8, box_w - 4)
    styled = _styled_text_lines(raw_lines, inner_w)
    total = len(styled)
    box_h = max(4, min(max_y - 2, max(8, total + 3)))
    top = max(1, (max_y - box_h) // 2)
    left = max(0, (max_x - box_w) // 2)
    inner_h = max(1, box_h - 2)
    clamped_scroll = min(max(0, scroll), max(0, total - inner_h))
    top_i = clamped_scroll + 1 if total else 0
    bot_i = min(total, clamped_scroll + inner_h)
    modal_title = f"{title} ── [j/k scroll · Esc/q close]"
    if _display_width(modal_title) > box_w - 6:
        modal_title = title
    iy, ix, ih, iw = _draw_box_frame(
        win,
        top,
        left,
        box_h,
        box_w,
        modal_title,
        theme,
        footer_hint=f"[{top_i}-{bot_i}/{total}]",
        clear=True,
    )
    _render_styled_pane(win, iy, ix, ih, iw, styled, clamped_scroll, theme)
    return clamped_scroll


def _prompt_line(
    stdscr,
    prompt: str,
    *,
    ctrl: TuiController | None = None,
    theme: dict[str, int] | None = None,
    on_change=None,
) -> str | None:
    if theme is None:
        theme = _init_curses_theme()
    buf = ""
    with contextlib.suppress(curses.error):
        curses.curs_set(1)
    stdscr.timeout(-1)
    try:
        while True:
            if ctrl is not None and on_change is not None:
                _render_curses_frame(stdscr, ctrl, theme=theme)
            max_y, max_x = stdscr.getmaxyx()
            y = max(0, max_y - 1)
            line_text = f"{prompt}{buf}"
            _safe_addstr(stdscr, y, 0, " " * max(1, max_x - 1), theme.get("header", curses.A_REVERSE))
            _safe_addstr(stdscr, y, 0, _truncate(line_text, max(1, max_x - 2)), theme.get("header", curses.A_REVERSE))
            if hasattr(stdscr, "move"):
                with contextlib.suppress(curses.error):
                    stdscr.move(y, min(max(0, max_x - 2), _display_width(line_text)))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch == curses.ERR:
                continue
            if ch == getattr(curses, "KEY_RESIZE", 410):
                with contextlib.suppress(curses.error):
                    curses.update_lines_cols()
                if ctrl is not None:
                    _render_curses_frame(stdscr, ctrl, theme=theme)
                continue
            if ch == 27:  # Escape
                return None
            if ch in (10, 13, getattr(curses, "KEY_ENTER", 343)):
                return buf.strip()
            if ch in (getattr(curses, "KEY_BACKSPACE", 263), 127, 8):
                buf = buf[:-1]
                if on_change is not None:
                    on_change(buf)
                continue
            if 32 <= ch <= 126:
                buf += chr(ch)
                if on_change is not None:
                    on_change(buf)
    finally:
        with contextlib.suppress(curses.error):
            curses.curs_set(0)


def _render_curses_frame(
    stdscr,
    ctrl: TuiController,
    theme: dict[str, int] | None = None,
) -> None:
    if theme is None:
        theme = _init_curses_theme()
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    sb = ctrl.snapshot.get("status_bar") or {}
    avail_w = max(1, max_x - 1)

    # Row 0: Top status ribbon with brand pill + project + run + alert pills.
    brand_pill = f" openfarm tui v{__version__} "
    ribbon_segs: list[tuple[str, str]] = [(brand_pill, "brand"), (" ", "")]
    proj = str(sb.get("project") or "")
    if proj and avail_w >= 48:
        ribbon_segs.extend([(proj, "info_bold"), (" │ ", "border")])
    run_id = str(sb.get("active_run_id") or "")
    ribbon_segs.append((f"● {run_id}" if run_id else "○ idle", "ok" if run_id else "muted"))
    if sb.get("paused"):
        ribbon_segs.extend([("  ", ""), (" ⏸ PAUSED ", "warn")])
    if sb.get("kill_switch"):
        ribbon_segs.extend([("  ", ""), (" ■ KILL ", "err")])
    if sb.get("breaker_tripped"):
        ribbon_segs.extend([("  ", ""), (" ⚡ BREAKER ", "err")])
    if sb.get("swarm_enabled"):
        ribbon_segs.extend([("  ", ""), (" ⇄ SWARM ", "accent")])
    uq = int(sb.get("unanswered_questions", 0) or 0)
    if uq > 0:
        q_txt = f" ? {uq} question " if uq == 1 else f" ? {uq} questions "
        ribbon_segs.extend([("  ", ""), (q_txt, "warn")])
    _draw_segments(stdscr, 0, 0, avail_w, ribbon_segs, theme)

    # Row 1: Pill tab bar + right-aligned mode/search indicators.
    counts = sb.get("counts") or {}
    ind_segs: list[tuple[str, str]] = []
    if ctrl.view == "harness" and ctrl.harness_global:
        ind_segs.append(("[GLOBAL]", "accent"))
    if ctrl.view == "dossier" and ctrl.dossier_json_mode:
        ind_segs.append(("[JSON]", "info"))
    if ctrl.search_query:
        ind_segs.append((f"/{ctrl.search_query}", "warn"))
    ind_str = " ".join(s for s, _ in ind_segs)
    ind_w = _display_width(ind_str) + (1 if ind_str else 0)

    full_tabs = [_tab_label(idx, v, counts, include_count=True) for idx, v in enumerate(VIEWS, start=1)]
    plain_tabs = [_tab_label(idx, v, counts, include_count=False) for idx, v in enumerate(VIEWS, start=1)]
    compact_tabs = [f"{idx}:{VIEW_LABELS[v][:4]}" for idx, v in enumerate(VIEWS, start=1)]

    tab_budget = max(20, avail_w - ind_w)
    if sum(_display_width(t) for t in full_tabs) + 6 <= tab_budget:
        chosen_tabs = full_tabs
    elif sum(_display_width(t) for t in plain_tabs) + 6 <= tab_budget:
        chosen_tabs = plain_tabs
    else:
        chosen_tabs = compact_tabs

    tab_segs: list[tuple[str, str]] = []
    for idx, (v, label) in enumerate(zip(VIEWS, chosen_tabs)):
        if idx > 0:
            tab_segs.append((" ", ""))
        tab_segs.append((label, "tab_active" if v == ctrl.view else "tab_idle"))
    _draw_segments(stdscr, 1, 0, tab_budget, tab_segs, theme)
    if ind_str and avail_w > ind_w:
        rx = max(0, avail_w - _display_width(ind_str))
        spaced_ind: list[tuple[str, str]] = []
        for idx, seg in enumerate(ind_segs):
            if idx > 0:
                spaced_ind.append((" ", ""))
            spaced_ind.append(seg)
        _draw_segments(stdscr, 1, rx, avail_w - rx, spaced_ind, theme)

    # Body area (rows 2 .. max_y - 2).
    body_top = 2
    body_height = max(1, max_y - 3)
    if ctrl.view in SELECTION_VIEWS:
        _render_split_master_detail(stdscr, ctrl, body_top, body_height, max_x, theme)
    else:
        _render_scrollable_view(stdscr, ctrl, body_top, body_height, max_x, theme)

    # Centered floating modals on top of background frame.
    if ctrl.help_open:
        ctrl.help_scroll = _render_modal_box(
            stdscr, "openfarm TUI Help", _help_lines(), ctrl.help_scroll, max_y, max_x, theme
        )
    elif ctrl.modal_lines:
        ctrl.modal_scroll = _render_modal_box(
            stdscr, ctrl.modal_title or "Output", ctrl.modal_lines, ctrl.modal_scroll, max_y, max_x, theme
        )

    # Bottom footer bar with keycap pills + right-aligned status message.
    footer_y = max(0, max_y - 1)
    hdr_attr = theme.get("header", curses.A_REVERSE)
    _safe_addstr(stdscr, footer_y, 0, " " * avail_w, hdr_attr)
    key_pairs = [
        ("1-7", "View"),
        ("Tab", "Next"),
        ("j/k", "Move"),
        ("/", "Filter"),
        (":", "Cmd"),
        ("?", "Help"),
        ("q", "Quit"),
    ]
    status_txt = f" {ctrl.status_message} " if ctrl.status_message else ""
    status_w = _display_width(status_txt)
    caps_budget = max(10, avail_w - status_w)
    cur_x = 0
    for cap, desc in key_pairs:
        pill = f" {cap} "
        tail = f" {desc} "
        need = _display_width(pill) + _display_width(tail)
        if cur_x + need > caps_budget:
            break
        _safe_addstr(stdscr, footer_y, cur_x, pill, theme.get("key", curses.A_BOLD))
        cur_x += _display_width(pill)
        _safe_addstr(stdscr, footer_y, cur_x, tail, hdr_attr)
        cur_x += _display_width(tail)
    if status_txt and avail_w > status_w:
        _safe_addstr(stdscr, footer_y, max(cur_x, avail_w - status_w), _truncate(status_txt, avail_w - cur_x), hdr_attr)
    stdscr.refresh()


def _handle_view_hotkey(stdscr, ctrl: TuiController, ch: int, *, theme: dict[str, int] | None = None) -> bool:
    """Handle view-specific interactive prompts; return True if key was consumed."""
    v = ctrl.view
    if v == "overview":
        if ch == ord("p"):
            reason = _prompt_line(stdscr, "pause reason (optional): ", theme=theme)
            if reason is not None:
                ctrl.action_pause(reason=reason)
            return True
        if ch == ord("u"):
            flags = _prompt_line(stdscr, "resume flags (--clear-breaker / --clear-kill): ", theme=theme)
            if flags is not None:
                ctrl.action_resume(
                    clear_breaker="--clear-breaker" in flags,
                    clear_kill="--clear-kill" in flags,
                )
            return True
        if ch == ord("x"):
            reason = _prompt_line(stdscr, "stop (kill switch) reason: ", theme=theme)
            if reason is not None:
                ctrl.action_stop(reason=reason)
            return True
        if ch == ord("n"):
            spec = _prompt_line(stdscr, "run args (e.g. --dry-run --cards 1): ", theme=theme)
            if spec is not None:
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
            note = _prompt_line(stdscr, "done note (optional): ", theme=theme)
            if note is not None:
                ctrl.action_done(note=note)
            return True
        if ch == ord("l"):
            reason = _prompt_line(stdscr, "release reason: ", theme=theme)
            if reason:
                ctrl.action_release(reason=reason)
            return True
        if ch == ord("v"):
            verdict = _prompt_line(stdscr, "verdict (clean|minor-fix|bounced|scrapped): ", theme=theme)
            if verdict:
                tokens_raw = _prompt_line(stdscr, "review tokens: ", theme=theme)
                if tokens_raw and tokens_raw.isdigit():
                    ctrl.action_verdict(verdict, review_tokens=int(tokens_raw))
            return True
        if ch == ord("e"):
            note = _prompt_line(stdscr, "escape note: ", theme=theme)
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
            raw = _prompt_line(stdscr, "attempt # (blank to clear): ", theme=theme)
            if raw is not None:
                ctrl.attempt = int(raw) if raw.isdigit() else None
                ctrl.refresh()
            return True
    elif v == "board":
        if ch in (ord("\n"), ord("\r")):
            post = ctrl._selected_board_post()
            evs = (post or {}).get("evidence") or []
            if evs:
                ctrl.view_evidence(str(evs[0]))
            else:
                ctrl.status_message = "no evidence on selected post"
            return True
        if ch == ord("f"):
            flt = _prompt_line(stdscr, "filter kind=<k> member=<m> card=<c> (blank clears): ", theme=theme)
            if flt is not None:
                parsed: dict[str, str] = {}
                for tok in shlex.split(flt):
                    k, sep, val = tok.partition("=")
                    if sep and k in ("kind", "member", "card"):
                        parsed[k] = val
                ctrl.board_filter = parsed
                ctrl.refresh()
            return True
        if ch == ord("v"):
            ev = _prompt_line(stdscr, "evidence path (inside state_dir): ", theme=theme)
            if ev:
                ctrl.view_evidence(ev)
            return True
        if ch == ord("n"):
            as_n = _prompt_line(stdscr, "post --as: ", theme=theme)
            to_n = _prompt_line(stdscr, "post --to: ", theme=theme) if as_n else None
            body = _prompt_line(stdscr, "post body: ", theme=theme) if to_n else None
            if as_n and to_n and body:
                ctrl.action_board_post(as_name=as_n, to=to_n, body=body)
            return True
        if ch == ord("a"):
            seq_s = _prompt_line(stdscr, "answer question seq: ", theme=theme)
            as_n = _prompt_line(stdscr, "answer --as: ", theme=theme) if seq_s and seq_s.isdigit() else None
            js_s = _prompt_line(stdscr, "answer JSON: ", theme=theme) if as_n else None
            if seq_s and seq_s.isdigit() and as_n and js_s:
                ctrl.action_board_answer(int(seq_s), as_name=as_n, json_data=js_s)
            return True
        if ch == ord("w"):
            as_n = _prompt_line(stdscr, "board wait --as: ", theme=theme)
            if as_n:
                ctrl.action_board_wait(as_name=as_n)
            return True
    elif v == "swarm":
        if ch == ord("J"):
            args = _prompt_line(stdscr, "swarm join --name N --seat coordinator|supervisor: ", theme=theme)
            if args:
                ctrl.execute_command("swarm join " + args)
            return True
        if ch == ord("S"):
            args = _prompt_line(stdscr, "swarm start CARD --as CALLER [--team ...]: ", theme=theme)
            if args:
                ctrl.execute_command("swarm start " + args)
            return True
        if ch == ord("E"):
            args = _prompt_line(stdscr, "swarm set MEMBER --as CALLER [--tier T] [--effort E]: ", theme=theme)
            if args:
                ctrl.execute_command("swarm set " + args)
            return True
        if ch == ord("H"):
            args = _prompt_line(stdscr, "swarm hint MEMBER --as CALLER --body ...: ", theme=theme)
            if args:
                ctrl.execute_command("swarm hint " + args)
            return True
        if ch == ord("B"):
            args = _prompt_line(stdscr, "swarm block CARD --as CALLER --reason ...: ", theme=theme)
            if args:
                ctrl.execute_command("swarm block " + args)
            return True
        if ch == ord("X"):
            args = _prompt_line(stdscr, "swarm remove MEMBER --as CALLER: ", theme=theme)
            if args:
                ctrl.execute_command("swarm remove " + args)
            return True
    elif v == "harness":
        if ch in (ord("\n"), ord("\r")):
            entry = ctrl._selected_harness_entry()
            if entry:
                eid = str(entry.get("id", ""))
                if not ctrl.harness_global and eid:
                    ctrl.action_harness("show", eid)
                elif eid:
                    ctrl.open_modal(
                        f"Global Harness: {eid}",
                        json.dumps(entry, indent=2, ensure_ascii=False).splitlines(),
                    )
            return True
        if ch == ord("i"):
            ctrl.action_harness("index")
            return True
        if ch == ord("s"):
            eid = _prompt_line(stdscr, "harness show <id>: ", theme=theme)
            if eid:
                ctrl.action_harness("show", eid)
            return True
        if ch in (ord("+"), ord("P")):
            eid = _prompt_line(stdscr, "harness promote <id>: ", theme=theme)
            if eid:
                ctrl.action_harness("promote", eid)
            return True
        if ch in (ord("-"), ord("D")):
            eid = _prompt_line(stdscr, "harness demote <id_or_rev>: ", theme=theme)
            if eid:
                ctrl.action_harness("demote", eid)
            return True
        if ch == ord("R"):
            confirm = _prompt_line(stdscr, "type YES to confirm harness reset: ", theme=theme)
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
            args = _prompt_line(stdscr, "acp configure args: ", theme=theme)
            if args is not None:
                ctrl.execute_command("acp configure " + args)
            return True
        if ch == ord("s"):
            args = _prompt_line(stdscr, "supervise flags (--triage-blocked / --prepare-queue): ", theme=theme)
            if args:
                ctrl.execute_command("supervise " + args)
            return True
        if ch == ord("b"):
            ctrl.execute_command("bench list --json")
            return True
        if ch == ord("B"):
            args = _prompt_line(stdscr, "bench run --dry-run args: ", theme=theme)
            if args is not None:
                ctrl.execute_command("bench run --dry-run " + args)
            return True
        if ch == ord("I"):
            args = _prompt_line(stdscr, "init <name> [flags]: ", theme=theme)
            if args:
                ctrl.execute_command("init " + args)
            return True
        if ch == ord("A"):
            args = _prompt_line(stdscr, "adopt [flags]: ", theme=theme)
            if args is not None:
                ctrl.execute_command("adopt " + args)
            return True
        if ch == ord("K"):
            args = _prompt_line(stdscr, "install-skill [flags]: ", theme=theme)
            if args is not None:
                ctrl.execute_command("install-skill " + args)
            return True
    return False


def _curses_main(stdscr, ctrl: TuiController) -> int:
    with contextlib.suppress(curses.error, AttributeError):
        curses.set_escdelay(25)
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    stdscr.keypad(True)
    theme = _init_curses_theme()
    timeout_ms = max(50, int(ctrl.refresh_s * 1000))
    while True:
        _render_curses_frame(stdscr, ctrl, theme=theme)
        stdscr.timeout(timeout_ms)
        ch = stdscr.getch()
        if ch == curses.ERR:
            ctrl.refresh()
            continue
        if ch == getattr(curses, "KEY_RESIZE", 410):
            with contextlib.suppress(curses.error):
                curses.update_lines_cols()
            continue
        if ch == ord(":") and not ctrl.help_open and not ctrl.modal_lines:
            cmd_line = _prompt_line(stdscr, ":", ctrl=ctrl, theme=theme)
            if cmd_line:
                ctrl.execute_command(cmd_line)
            continue
        if ch == ord("/") and not ctrl.help_open and not ctrl.modal_lines:
            prev_query = ctrl.search_query
            ctrl.set_search("")
            res = _prompt_line(stdscr, "/", ctrl=ctrl, theme=theme, on_change=ctrl.set_search)
            if res is None:
                ctrl.set_search(prev_query)
            else:
                ctrl.set_search(res)
            continue
        if not ctrl.help_open and not ctrl.modal_lines and _handle_view_hotkey(stdscr, ctrl, ch, theme=theme):
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

    os.environ.setdefault("ESCDELAY", "25")
    ctrl = TuiController(
        repo,
        config_path=config_path,
        run_id=run_id,
        view=view,
        refresh_s=refresh_s,
    )
    return int(curses.wrapper(lambda stdscr: _curses_main(stdscr, ctrl)))
