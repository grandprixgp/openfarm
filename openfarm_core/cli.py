"""The `openfarm` command: one entry point, fifteen subcommands, shared exit codes.

Exit codes are the same for every command:

- `0` done, or for `check`, eligible.
- `1` refused, or for `check`, ineligible. A refusal is never retried.
- `2` usage or config error.
- `3` trouble: an adapter failed, the state folder can't be read, or the runner
  reported infra.

Every command takes `--json` and then prints its result as one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as _dt
import difflib
import functools
import inspect
import io
import json
import re
import sys
import threading
import time as _time
from pathlib import Path

from . import __version__
from .acp_setup import (
    ACP_ACTIONS,
    configure_acp_repo,
    inspect_acp_status,
    probe_acp,
    run_acp_login,
)
from .adopt import adopt_project
from .bench import load_task
from .board import Board, VALID_POST_KINDS, board_dir_for
from .adapters.acp import (
    ClaudeAcpAdapter,
    acp_adapter_for_provider,
    is_acp_adapter,
    normalize_acp_provider,
)
from .adapters.files import FileCardSource, open_card_source
from .adapters.jsonl import TelemetrySink, open_telemetry
from .adapters.openai_compat import open_backend
from .adapters.direct import BuildLimiter, open_runner
from .bootstrap import (
    INSTALL_TARGETS,
    MUTATION_MODES,
    PROFILES,
    initialize_project,
    install_skill_bundle,
)
from .briefs import BriefBook
from .budget import NightBudget, token_split
from .cards import Card
from .config import load_config, parse_duration
from .coordinator import SwarmRunner
from .dossier import build_dossier, render_markdown
from .errors import (AdapterError, ConfigError, GateFailure, Infra, Invalid,
                     Refused, StopCode, UsageError)
from .flow import CardRun
from .gates.checks import suite_green
from .gates.eligibility import check_eligibility, derive_cargo_unit
from .git import Git, GitRefused
from .globs import matches_any, normalise, resolves_within
from .harness_global import GlobalLibrary, global_dir_for
from .harness_memory import HarnessLibrary
from .harness_scope import project_markers
from .human_cli import Style, error_hint, format_human
from .micromanager import ClaudeAcpMicromanager, MicromanagerBudget, open_micromanager
from .packs.base import pack_for
from .state import (StateDir, _safe_id, baseline_lock_for, read_json, utc_now,
                    utc_after, write_json)
from .swarm import (
    parse_team_spec,
    swarm_block,
    swarm_hint,
    swarm_join,
    swarm_remove,
    swarm_set,
    swarm_start,
    swarm_status,
    swarm_tiers,
)
from .tui import VIEWS as TUI_VIEWS, run_tui

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_TROUBLE = 3


class Context:
    """Everything a command needs, opened once."""

    def __init__(self, repo: Path, config_path: Path | None = None,
                 json_output: bool = False) -> None:
        self.repo = Path(repo).resolve()
        self.json = json_output
        self.cfg = load_config(self.repo, config_path)
        self.state = StateDir(self.cfg.state_dir)

    def emit(self, payload: dict, *, kind: str | None = None) -> None:
        if self.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        print(format_human(kind or "", payload, style=Style(sys.stdout)))

    def cards(self, worktree: Path | None = None) -> FileCardSource:
        source = open_card_source(self.cfg, self.state.root)
        if worktree is not None and hasattr(source, "worktree"):
            source.worktree = worktree
        return source

    def runner(self, worktree: Path, output_dir: Path | None = None,
               limiter: BuildLimiter | None = None, budget=None):
        return open_runner(
            self.cfg, worktree, output_dir, limiter=limiter, budget=budget
        )


def _emit_ctx(ctx, payload: dict, kind: str) -> None:
    if isinstance(ctx, Context):
        ctx.emit(payload, kind=kind)
        return
    emit_fn = getattr(ctx, "emit", None)
    if not callable(emit_fn):
        return
    try:
        params = inspect.signature(emit_fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "kind" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        emit_fn(payload, kind=kind)
    else:
        emit_fn(payload)


def _check_id(value: str, what: str = "card id") -> str:
    try:
        return _safe_id(value, what)
    except Invalid as exc:
        raise Refused("bad-id", exc.message) from exc


def _open_harness_library(
    ctx: Context, *, require_enabled: bool = True
) -> HarnessLibrary | None:
    hcfg = getattr(ctx.cfg, "harness", None)
    enabled = bool(getattr(hcfg, "enabled", False))
    if require_enabled and not enabled:
        return None
    hdir_raw = str(getattr(hcfg, "dir", "") or "").strip() if hcfg else ""
    if hdir_raw:
        hdir_path = Path(hdir_raw).expanduser()
        if not hdir_path.is_absolute():
            hdir_path = (ctx.repo / hdir_path).resolve()
        hdir = hdir_path
    else:
        hdir = ctx.state.harness()
    return HarnessLibrary(hdir, config=ctx.cfg)


def _build_night_mm_budget(cfg) -> MicromanagerBudget:
    mm_cfg = getattr(cfg, "micromanager", None)
    max_calls = int(
        getattr(
            mm_cfg,
            "max_calls_per_card",
            getattr(cfg, "micromanager_max_calls_per_card", 8),
        )
    )
    tokens_cap = int(
        getattr(
            mm_cfg,
            "tokens_per_card",
            getattr(cfg, "micromanager_tokens_per_card", 200_000),
        )
    )
    spend_cap = float(
        getattr(
            mm_cfg,
            "spend_per_night",
            getattr(
                cfg,
                "micromanager_spend_per_night",
                getattr(cfg, "spend_per_night", 10.0),
            ),
        )
    )
    return MicromanagerBudget(
        max_calls_per_card=max_calls,
        tokens_per_card=tokens_cap,
        spend_per_night=spend_cap,
    )


# -- commands ------------------------------------------------------------------

def cmd_check(ctx: Context, args) -> int:
    """Eligibility on one card, without claiming it."""
    card_id = _check_id(args.card, "card id")
    source = ctx.cards()
    source.prepare()
    try:
        card = Card.from_dict(source.read(card_id)["card"])
    except Refused as exc:
        _emit_ctx(ctx, {"card": card_id, "eligible": False,
                        "failures": [{"rule": "read", "detail": exc.message}]}, "check")
        return EXIT_REFUSED

    deps_done = _deps_done(source, card)
    eligibility = check_eligibility(card, ctx.cfg, deps_done=deps_done,
                                    worktree=ctx.repo)
    payload = {"card": card.id, "eligible": eligibility.ok,
               "card_hash": card.hash(),
               "failures": eligibility.failures}

    if args.baseline and eligibility.ok:
        try:
            baseline = _baseline_check(ctx, source, card)
            payload["baseline"] = baseline
            if not baseline.get("ok", False):
                payload["eligible"] = False
        except (AdapterError, Infra) as exc:
            payload["baseline"] = {"ok": False, "error": exc.message}
            payload["eligible"] = False

    write_json(ctx.state.check_dir(card.id) / f"{utc_now().replace(':', '')}.json",
               payload)
    _emit_ctx(ctx, payload, "check")
    return EXIT_OK if payload["eligible"] else EXIT_REFUSED


def _deps_done(source: FileCardSource, card: Card) -> bool:
    if not card.deps:
        return True
    done = {other.id for other in source.all_cards() if other.state == "done"}
    return all(dep in done for dep in card.deps)


def _baseline_check(ctx: Context, source: FileCardSource, card: Card) -> dict:
    """Build base, verify whole-suite health (with cache), and check named tests."""
    worktree = ctx.state.root / "checks" / "worktree" / card.id
    git = Git(ctx.repo)
    base = (f"refs/heads/{ctx.cfg.base_branch}"
            if git.ref_exists(f"refs/heads/{ctx.cfg.base_branch}") else "HEAD")
    base_commit = git.head(base)
    if worktree.exists():
        git.worktree_remove(worktree)
    git.worktree_add(worktree, base)
    try:
        pack = pack_for(
            ctx.cfg.languages[0], ctx.cfg.cargo_flags,
            spec=ctx.cfg.pack_adapter, cwd=worktree,
            python=ctx.cfg.runner_python,
        )
        pack.configure(worktree)
        unit = card.unit or derive_cargo_unit(card.touches, worktree)
        runner = ctx.runner(worktree)
        target = ctx.state.check_dir(card.id) / "target"
        env = ctx.cfg.runner_env.get(unit, {})
        empty_named = {name: "not-run" for name in card.test_names}

        cache_path = ctx.state.cache_path(
            base_commit or "unknown", unit or "workspace",
            ctx.cfg.known_failing_hash(), ctx.cfg.env_hash(unit),
        )
        cache_lock = (
            baseline_lock_for(cache_path) if ctx.cfg.baseline_cache else None
        )
        lock_held = False
        if cache_lock is not None:
            cache_lock.acquire()
            lock_held = True
        cached = read_json(cache_path) if ctx.cfg.baseline_cache else None
        if not (isinstance(cached, dict)
                and cached.get("known_failing_hash") == ctx.cfg.known_failing_hash()
                and cached.get("env_hash") == ctx.cfg.env_hash(unit)):
            runs = []
            for _ in (1, 2):
                res = runner.exec(
                    kind="test", argv=pack.commands("test", unit=unit)["argv"],
                    cwd=worktree, target_dir=target, unit=unit, env=env,
                )
                parsed_suite = pack.parse_tests(res.stdout, res.stderr)
                sg = suite_green(parsed_suite, ctx.cfg.known_failing,
                                 run_cls=res.cls, exit_code=res.exit_code)
                runs.append((res, parsed_suite, sg))
            first, second = runs
            if (first[1].get("compile_errors") or second[1].get("compile_errors")):
                return {"ok": False, "class": first[0].cls, "named": empty_named,
                        "error": "the base does not compile",
                        "compile_errors": first[1].get("compile_errors") or second[1].get("compile_errors")}
            if (first[0].cls == "infra" or second[0].cls == "infra"
                    or first[2].result == "infra" or second[2].result == "infra"):
                return {"ok": False, "class": "infra", "named": empty_named,
                        "error": "the base suite run did not finish"}
            if not first[2].ok or not second[2].ok:
                return {"ok": False, "class": "fail", "named": empty_named,
                        "error": first[2].detail or second[2].detail}
            list_res = runner.exec(
                kind="list", argv=pack.commands("list", unit=unit)["argv"],
                cwd=worktree, target_dir=target, unit=unit, env=env,
            )
            listing = pack.parse_list(list_res.stdout)
            if ctx.cfg.baseline_cache:
                write_json(cache_path, {
                    "commit": base_commit, "unit": unit,
                    "known_failing_hash": ctx.cfg.known_failing_hash(),
                    "env_hash": ctx.cfg.env_hash(unit),
                    "agreed": True, "list": listing, "at": utc_now(),
                })
        if lock_held and cache_lock is not None:
            cache_lock.release()
            lock_held = False

        if not card.test_names:
            return {"ok": True, "class": "pass", "named": {},
                    "failed_on_base": [], "passing_on_base": []}

        argv = pack.commands("test", unit=unit, tests=card.test_names)["argv"]
        result = runner.exec(
            kind="test", argv=argv, cwd=worktree, target_dir=target,
            unit=unit, env=env)
        parsed = pack.parse_tests(result.stdout, result.stderr)

        if result.cls == "infra" or (
                parsed.get("missing_footers") and not parsed.get("compile_errors")):
            return {"ok": False, "class": result.cls, "named": empty_named,
                    "error": "the base run did not finish"}
        if parsed.get("compile_errors"):
            return {"ok": False, "class": result.cls, "named": empty_named,
                    "error": "the frozen tests do not compile on base, so a failure "
                             "would prove nothing",
                    "compile_errors": parsed.get("compile_errors", [])}

        statuses = {entry["name"]: entry["status"]
                    for entry in parsed.get("results", [])}
        named = {name: statuses.get(name, "not-run") for name in card.test_names}
        passing = [name for name, status in named.items() if status == "ok"]
        return {
            "ok": not passing,
            "class": result.cls,
            "named": named,
            "failed_on_base": [name for name, status in named.items()
                               if status == "FAILED"],
            "passing_on_base": passing,
        }
    finally:
        if lock_held and cache_lock is not None:
            cache_lock.release()
        git.worktree_remove(worktree)


def cmd_run(ctx: Context, args) -> int:
    """Start a run: claim, drive, and move each card."""
    if ctx.state.kill_requested(ctx.cfg.kill_switch):
        _emit_ctx(ctx, {"refused": True, "reason": "the kill switch exists"}, "run")
        return EXIT_REFUSED
    if ctx.state.is_paused():
        _emit_ctx(ctx, {"refused": True, "reason": "the queue is paused"}, "run")
        return EXIT_REFUSED
    breaker = ctx.state.read_breaker()
    if breaker.get("tripped"):
        _emit_ctx(ctx, {"refused": True, "reason": "the circuit breaker is tripped"}, "run")
        return EXIT_REFUSED

    if not args.dry_run:
        # Never judge a card with code the card could have edited.
        from .selfhost import unpinned_reason
        reason = unpinned_reason(ctx.repo, getattr(ctx.cfg, "state_dir", None))
        if reason:
            _emit_ctx(ctx, {"refused": True, "reason": reason,
                            "code": "self-host-unpinned"}, "run")
            return EXIT_REFUSED

    if getattr(args, "learn_from", None):
        import dataclasses
        ctx.cfg.harness = dataclasses.replace(ctx.cfg.harness, learn_from=args.learn_from)

    run_id = args.run_id or f"run-{utc_now().replace('-', '').replace(':', '')}"
    if not ctx.state.acquire_run_lock(run_id):
        holder = ctx.state.lock_holder() or {}
        _emit_ctx(ctx, {"refused": True,
                        "reason": f"another run holds the lock: {holder.get('run_id')}"}, "run")
        return EXIT_REFUSED
    stale = getattr(ctx.state, "last_lock_takeover", None)
    if stale:
        print(f"note: took over run.lock from run {stale.get('run_id')!r} "
              f"(pid {stale.get('pid')} on {stale.get('host')}, started "
              f"{stale.get('started_at')}), which is no longer running",
              file=sys.stderr)

    try:
        return _do_run(ctx, args, run_id)
    finally:
        ctx.state.release_run_lock(run_id)


def _do_run(ctx: Context, args, run_id: str) -> int:
    ctx.state.check_disk_space(getattr(ctx.cfg, "min_free_mb", 0))
    source = ctx.cards()
    source.prepare()
    run_dir = ctx.state.create_run(run_id)
    telemetry_raw = open_telemetry(ctx.cfg, run_dir)
    telemetry = TelemetrySink(telemetry_raw)

    cfg = ctx.cfg
    handshake = _handshake(ctx, source, args, telemetry_raw=telemetry_raw)
    if handshake["refused"]:
        _emit_ctx(ctx, {"refused": True, "reason": handshake["reason"],
                        "adapters": handshake["adapters"]}, "run")
        return EXIT_REFUSED

    if cfg.state_dir.exists():
        try:
            (ctx.state.config_lock).write_text(
                (cfg.path.read_text() if cfg.path else "") +
                f"\n# hash: {cfg.config_hash()}\n")
        except OSError:
            pass

    identifiers = ([_check_id(args.card, "card id")]
                   if args.card else source.next(cfg.lane,
                                                 limit=args.cards)["cards"])
    # `--spend` may lower the night's cap but never raise the config's, because the
    # config is the operator's standing limit and the flag is one night's. A zero or
    # negative flag is a usage error rather than "no cap": silently turning a limit
    # off is the one thing a spend flag must never do.
    cap = cfg.spend_per_night
    if args.spend is not None:
        if args.spend < 0:
            raise UsageError("--spend must be zero or more")
        if args.spend == 0:
            raise UsageError(
                "--spend 0 would remove the cap; set spend_per_night in the config "
                "and pass a positive --spend to lower it")
        cap = (min(args.spend, cfg.spend_per_night) if cfg.spend_per_night > 0
               else args.spend)
    night = NightBudget(cap, cards_cap=args.cards)
    night_mm_budget = _build_night_mm_budget(cfg)
    deadline = _deadline(args)
    outcomes: list[dict] = []
    consecutive: list[tuple[str, str, str]] = []
    script_path = Path(args.script) if getattr(args, "script", None) else None
    mm_script_path = (Path(args.mm_script)
                      if getattr(args, "mm_script", None) else None)
    harness_lib = _open_harness_library(ctx, require_enabled=True)
    swarm_cfg = getattr(cfg, "swarm", None)
    board: Board | None = None
    if swarm_cfg is not None and swarm_cfg.enabled and not args.dry_run:
        board = Board(board_dir_for(ctx.state, run_id), state_dir=ctx.state)
        board.ensure()
        board.upsert_member("gates", kind="code", role="gates", added_by="system")
        board.upsert_member("monitor", kind="code", role="monitor", added_by="system")
        if swarm_cfg.coordinator == "code":
            board.upsert_member(
                "coordinator", kind="code", role="coordinator", added_by="system"
            )
        if swarm_cfg.supervisor.startswith("tier:"):
            sup_tier = cfg.tiers[swarm_cfg.supervisor[5:]]
            from .swarm import worker_kind_for_config
            board.upsert_member(
                "supervisor",
                kind=worker_kind_for_config(cfg),
                role="supervisor",
                tier=sup_tier.name,
                model=sup_tier.model,
                effort=swarm_cfg.supervisor_effort,
                added_by="system",
            )
        elif is_acp_adapter(swarm_cfg.supervisor):
            sup_prov = normalize_acp_provider(swarm_cfg.supervisor)
            board.upsert_member(
                "supervisor",
                kind="acp",
                role="supervisor",
                tier=acp_adapter_for_provider(sup_prov),
                model=str(getattr(getattr(cfg, "micromanager", None), "model", "") or sup_prov),
                effort=swarm_cfg.supervisor_effort,
                added_by="system",
            )

    swarm_parallel = bool(
        swarm_cfg is not None and swarm_cfg.enabled and not args.dry_run
    )
    shared_mm_backend = None
    mm_seat: _SharedAcpSupervisor | None = None
    coord_backend = None
    swarm_sup = swarm_cfg.supervisor if swarm_cfg is not None else "config"
    uses_acp_sup = is_acp_adapter(swarm_sup) or (
        swarm_sup == "config"
        and is_acp_adapter(cfg.micromanager_adapter)
        and getattr(getattr(cfg, "micromanager", None), "mode", "active") != "off"
    )
    if (
        swarm_parallel
        and mm_script_path is None
        and getattr(cfg, "concurrent_cards", 1) > 1
        and uses_acp_sup
    ):
        shared_mm = open_micromanager(
            cfg, ctx.state.root, ctx.repo, worktree=ctx.repo, budget=night_mm_budget
        )
        shared_mm_backend = getattr(shared_mm, "backend", None)
        if isinstance(shared_mm_backend, ClaudeAcpMicromanager):
            # Share the process, not the conversation: see _SharedAcpSupervisor.
            mm_seat = _SharedAcpSupervisor(shared_mm_backend)
            shared_mm_backend = None
    coord_spec = str(getattr(swarm_cfg, "coordinator", "code") or "")
    if swarm_parallel and coord_spec.startswith("tier:"):
        coord_backend = open_backend(cfg, script_path)
    elif swarm_parallel and is_acp_adapter(coord_spec):
        if script_path is not None:
            coord_backend = open_backend(cfg, script_path)
        else:
            from .coordinator import AcpCoordinatorBackend
            coord_backend = AcpCoordinatorBackend(
                cfg,
                provider=normalize_acp_provider(coord_spec),
                effort=str(getattr(swarm_cfg, "coordinator_effort", "") or ""),
            )
    if coord_backend is not None:
        coord_backend = _MeteredCoordinatorBackend(coord_backend, night)
    build_limiter = (
        BuildLimiter(getattr(cfg, "concurrent_builds", 2))
        if swarm_parallel
        else None
    )

    try:
        if not swarm_parallel:
            for card_id in identifiers:
                if ctx.state.kill_requested(cfg.kill_switch):
                    # Cards in flight were stopped by CardRun itself, which blocks them with
                    # `killed`. A card not yet claimed is simply left alone.
                    outcomes.append({"card": card_id,
                                     "skipped": "the kill switch was set"})
                    break
                # A pause stops new claims, and cards already in flight finish their attempt:
                # that is the spec's rule, and it is checked here rather than only at entry so
                # `openfarm pause` means something during a night.
                if ctx.state.is_paused():
                    outcomes.append({"card": card_id,
                                     "skipped": "the queue was paused"})
                    continue
                # `_deadline` answers in `time.monotonic()`, so it must be compared to the
                # same clock. Comparing it to `time.time()` would skip every card at once.
                if deadline is not None and _time.monotonic() >= deadline:
                    outcomes.append({"card": card_id,
                                     "skipped": "the run's time ran out"})
                    continue
                reason = night.exhausted()
                if reason:
                    outcomes.append({"card": card_id, "skipped": reason})
                    continue
                ctx.state.check_disk_space(getattr(cfg, "min_free_mb", 0))
                if args.dry_run:
                    outcomes.append(_dry_run(ctx, source, card_id))
                    continue

                outcome = _run_card(ctx, source, card_id, run_id, telemetry,
                                    run_dir, script_path, mm_script=mm_script_path,
                                    harness_library=harness_lib,
                                    mm_budget=night_mm_budget, board=board)
                outcomes.append(outcome)
                final_state = outcome.get("final_state")
                if final_state == "blocked":
                    code = outcome.get("stop_code", "")
                    consecutive.append((card_id, code, outcome.get("detail", "")))
                    if code == StopCode.INFRA.value:
                        ctx.state.pause(
                            f"infra error on {card_id}: {outcome.get('detail', '')}")
                        night.note_card()
                        night.note_spend(outcome.get("spend", 0.0) or 0.0)
                        night.note_micromanager(
                            outcome.get("micromanager_spend", 0.0) or 0.0)
                        _update_breaker(ctx, consecutive)
                        break
                elif final_state != "released":
                    consecutive = []
                night.note_card()
                night.note_spend(outcome.get("spend", 0.0) or 0.0)
                night.note_micromanager(outcome.get("micromanager_spend", 0.0) or 0.0)
                _update_breaker(ctx, consecutive)
        else:
            rec_lock = threading.Lock()

            def _run_swarm_card(cid: str, *, on_handback=None, on_struggling=None) -> dict:
                return _run_card(
                    ctx,
                    source,
                    cid,
                    run_id,
                    telemetry,
                    run_dir,
                    script_path,
                    mm_script=mm_script_path,
                    harness_library=harness_lib,
                    mm_budget=night_mm_budget,
                    board=board,
                    build_limiter=build_limiter,
                    mm_backend=shared_mm_backend,
                    on_handback=on_handback,
                    on_struggling=on_struggling,
                    mm_seat=mm_seat,
                )

            def _record_swarm_outcome(cid: str, outcome: dict) -> bool:
                nonlocal consecutive
                with rec_lock:
                    final_state = outcome.get("final_state")
                    infra_stop = False
                    if final_state == "blocked":
                        code = outcome.get("stop_code", "")
                        consecutive.append((cid, code, outcome.get("detail", "")))
                        if code == StopCode.INFRA.value:
                            ctx.state.pause(
                                f"infra error on {cid}: {outcome.get('detail', '')}"
                            )
                            infra_stop = True
                    elif final_state != "released":
                        consecutive = []
                    night.note_spend(outcome.get("spend", 0.0) or 0.0)
                    night.note_micromanager(
                        outcome.get("micromanager_spend", 0.0) or 0.0
                    )
                    _update_breaker(ctx, consecutive)
                    return infra_stop

            swarm_runner = SwarmRunner(
                cfg=cfg,
                state=ctx.state,
                source=source,
                board=board,
                identifiers=identifiers,
                night=night,
                deadline=deadline,
                run_card_fn=_run_swarm_card,
                record_outcome_fn=_record_swarm_outcome,
                backend=coord_backend,
            )
            outcomes = swarm_runner.run()
    finally:
        if coord_backend is not None and hasattr(coord_backend, "close"):
            try:
                coord_backend.close()
            except Exception as exc:
                print(f"coordinator backend close error ignored: {exc}", file=sys.stderr)
        if shared_mm_backend is not None and hasattr(shared_mm_backend, "close"):
            try:
                shared_mm_backend.close()
            except Exception as exc:
                print(f"micromanager close error ignored: {exc}", file=sys.stderr)
        if mm_seat is not None:
            try:
                mm_seat.close()
            except Exception as exc:
                print(f"micromanager close error ignored: {exc}", file=sys.stderr)
        if board is not None:
            board.end_run("finished")

    notes: list[str] = []
    if not _backend_prices_spend(cfg, script_path):
        notes.append(
            "the backend reports no prices, so every spend is zero and the spend "
            "caps cannot fire: set [model] cost_per_1k_in and cost_per_1k_out")
    if cfg.concurrent_cards > 1 and not swarm_parallel:
        notes.append(
            f"concurrent_cards is {cfg.concurrent_cards} but cards run one at a "
            f"time: the per-card state is ready for a pool, the loop is not")

    tokens_payload = token_split(
        sum(int(o.get("tokens_in", 0) or 0) + int(o.get("tokens_out", 0) or 0)
            for o in outcomes),
        sum(int(o.get("micromanager_tokens_in", 0) or 0)
            + int(o.get("micromanager_tokens_out", 0) or 0) for o in outcomes),
        coord_backend.tokens if coord_backend is not None else 0,
    )
    if board is not None:
        tokens_payload.update(_member_token_breakdown(board, tokens_payload))

    record = {"run_id": run_id, "started_at": utc_now(),
              "finished_at": utc_now(), "caps": {"spend": night.spend_cap,
                                                "cards": args.cards},
              "tokens": tokens_payload,
              # Dollars at API list prices: what the spend caps count, not a cost.
              "spend": round(night.total_spend(), 6),
              "worker_spend": round(night.spend, 6),
              "micromanager_spend": round(night.micromanager_spend, 6),
              "coordinator_spend": round(night.coordinator_spend, 6),
              "spend_measured": _backend_prices_spend(cfg, script_path),
              "notes": notes,
              "outcomes": outcomes,
              "dry_run": bool(args.dry_run),
              "telemetry_problems": telemetry.problems,
              "harness": (harness_lib.status()
                          if harness_lib is not None else {"enabled": False})}
    write_json(run_dir / "run.json", record)
    telemetry.write("run", record)
    telemetry.flush()
    _emit_ctx(ctx, record, "run")
    return EXIT_OK if not any(o.get("stop_code") == "infra" for o in outcomes) \
        else EXIT_TROUBLE


def _dry_run(ctx: Context, source: FileCardSource, card_id: str) -> dict:
    """Eligibility and baselines only: no model calls, no claims.

    The baseline half is the point of the flag: a dry run exists so an operator can
    find out that the base is red before a night starts, without spending a card.
    """
    try:
        card = Card.from_dict(source.read(card_id)["card"])
    except AdapterError as exc:
        return {"card": card_id, "error": exc.message}
    eligibility = check_eligibility(card, ctx.cfg,
                                    deps_done=_deps_done(source, card),
                                    worktree=ctx.repo)
    payload = {"card": card_id, "dry_run": True, "eligible": eligibility.ok,
               "failures": eligibility.failures}
    if eligibility.ok:
        try:
            payload["baseline"] = _baseline_check(ctx, source, card)
            if not payload["baseline"].get("ok", False):
                payload["eligible"] = False
        except (AdapterError, Infra, OSError) as exc:
            payload["baseline"] = {"ok": False,
                                   "error": getattr(exc, "message", str(exc))}
            payload["eligible"] = False
    return payload


def _load_test_conventions(cfg, worktree: Path) -> str:
    """Load `[brief] test_conventions` safely, preferring the card worktree."""
    if not cfg.test_conventions_path:
        return ""
    conv_rel = normalise(str(cfg.test_conventions_path))
    if conv_rel == ".openfarm/config.toml":
        return ""
    allowed_extra = {conv_rel} if conv_rel.startswith(".openfarm/") else set()
    conv_book = BriefBook(
        worktree, cfg.never_touch, cfg.forbidden_files,
        allowed_extra_reads=allowed_extra,
    )
    if conv_book.is_hidden(conv_rel):
        return ""
    if (worktree / conv_rel).is_file():
        return conv_book.guarded_read(conv_rel)
    repo_note = resolves_within(cfg.repo, conv_rel)
    if repo_note is not None and repo_note.is_file():
        return repo_note.read_text()
    return ""


class _SharedAcpSupervisor:
    """The run's one ACP supervisor process, with one session per card.

    Parallel cards share the ACP process (the spec's "one Claude ACP process
    when the supervisor is `acp`"), but not a conversation: v0.8 sent every
    card's supervisor prompts into one session rooted at the repository, so a
    card's supervisor saw the other cards' prompts and answers. Each card now
    gets its own session, rooted at its own worktree, and its own artifact
    numbering; the night's supervisor budget stays shared. The adapter takes one
    prompt at a time, so the cards' calls take turns on one lock. A process that
    dies (a timed-out turn kills it) is replaced once for everyone, and every
    card opens a fresh session on the new one.
    """

    def __init__(self, proto: ClaudeAcpMicromanager) -> None:
        self.proto = proto
        self.lock = threading.RLock()
        self.adapter = None

    def live_adapter(self):
        """The one live adapter, started or replaced as needed (call under `lock`)."""
        proc = getattr(self.adapter, "_proc", None) if self.adapter is not None else None
        if self.adapter is not None and proc is not None and proc.poll() is not None:
            with contextlib.suppress(Exception):
                self.adapter.close()
            self.adapter = None
        if self.adapter is None:
            p = self.proto
            self.adapter = ClaudeAcpAdapter(
                command=p.command, cwd=p.cwd, provider=p.provider,
                isolate_home=p.isolate_home, auth_file=p.auth_file, auth_mode=p.auth_mode,
                never_touch=p.never_touch, forbidden_files=p.forbidden_files,
                allowed_root=p.cwd,
            )
        return self.adapter

    def for_card(self, worktree: Path, card_dir: Path) -> "_CardAcpSupervisor":
        p = self.proto
        return _CardAcpSupervisor(
            self, command=p.command, cwd=worktree, provider=p.provider, model=p.model,
            effort=p.effort, tool_policy=p.tool_policy, timeout_s=p.timeout_s,
            isolate_home=p.isolate_home, auth_file=p.auth_file, auth_mode=p.auth_mode,
            never_touch=p.never_touch, forbidden_files=p.forbidden_files,
            cost_per_1k_in=p.cost_per_1k_in, cost_per_1k_out=p.cost_per_1k_out,
            state_dir=p.state_dir, card_dir=card_dir,
        )

    def close(self) -> None:
        with self.lock:
            if self.adapter is not None:
                self.adapter.close()
                self.adapter = None


class _CardAcpSupervisor(ClaudeAcpMicromanager):
    """One card's supervisor: its own session on the run's shared ACP process."""

    def __init__(self, seat: _SharedAcpSupervisor, **kwargs) -> None:
        super().__init__(**kwargs)
        self._seat = seat
        # One prompt at a time on the one process, whichever card asks.
        self._lock = seat.lock

    def _ensure_session(self):
        adapter = self._seat.live_adapter()
        if self._adapter is not adapter:
            self._adapter = adapter
            self._session_id = None
        return super()._ensure_session()

    def close(self) -> None:
        # The card is done with its session; the run closes the shared process.
        with self._lock:
            self._session_id = None
            self._adapter = None


def _run_card(ctx: Context, source: FileCardSource, card_id: str, run_id: str,
              telemetry, run_dir: Path, script: Path | None,
              backend=None, mm_script: Path | None = None,
              micromanager=None,
              harness_library: HarnessLibrary | None = None,
              mm_budget: MicromanagerBudget | None = None,
              board: Board | None = None,
              build_limiter: BuildLimiter | None = None,
              mm_backend=None,
              on_handback=None,
              on_struggling=None,
              mm_seat: _SharedAcpSupervisor | None = None) -> dict:
    """Claim one card and drive it. `backend` overrides the configured one."""
    cfg = ctx.cfg
    holder = f"openfarm:{run_id}"
    try:
        claim = source.claim(card_id, holder, cfg.lease_ttl_s)
    except Refused as exc:
        return {"card": card_id, "final_state": "released",
                "stop_code": StopCode.REFUSED.value, "detail": exc.message}

    card = Card.from_dict(source.read(card_id)["card"])
    card_hash = card.hash()
    worktree = ctx.state.worktrees / f"{card.id}-{run_id}"
    if worktree.exists():
        Git(ctx.repo).worktree_remove(worktree)

    base_ref = (f"refs/heads/{cfg.base_branch}"
                if Git(ctx.repo).ref_exists(f"refs/heads/{cfg.base_branch}") else "HEAD")
    Git(ctx.repo).worktree_add(worktree, base_ref)

    card_dir = ctx.state.create_card(card.id, run_id)
    pack = pack_for(cfg.languages[0], cfg.cargo_flags,
                    spec=cfg.pack_adapter, cwd=worktree,
                    python=cfg.runner_python)
    pack.configure(worktree)
    owns_backend = backend is None
    backend = backend if backend is not None else open_backend(cfg, script)
    runner = ctx.runner(worktree, output_dir=card_dir / "gates" / "output")
    if build_limiter is not None and hasattr(runner, "limiter") and runner.limiter is None:
        runner.limiter = build_limiter
    book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
    book.pack = pack
    conventions = _load_test_conventions(cfg, worktree)

    if mm_seat is not None and micromanager is None and mm_backend is None:
        # Its own session on the run's shared supervisor process, at its worktree.
        mm_backend = mm_seat.for_card(worktree, card_dir)
        owns_mm = True  # closing it ends the card's session, not the process
    else:
        owns_mm = micromanager is None and mm_backend is None
    mm = (micromanager if micromanager is not None else open_micromanager(
        cfg, card_dir, ctx.repo, script=mm_script,
        worktree=worktree, card_dir=card_dir, budget=mm_budget,
        board=board, worker_backend=backend, backend_override=mm_backend,
    ))
    if mm_budget is not None and hasattr(mm, "budget"):
        mm.budget = mm_budget
    resolved_harness = (
        harness_library
        if harness_library is not None
        else _open_harness_library(ctx, require_enabled=True)
    )
    run = CardRun(
        card=card, cfg=cfg, state=ctx.state, card_dir=card_dir,
        worktree=worktree, runner=runner, pack=pack, backend=backend,
        book=book, run_id=run_id, telemetry=telemetry,
        deps_done=_deps_done(source, card), conventions=conventions,
        log=lambda message: print(message, file=sys.stderr),
        micromanager=mm, harness_library=resolved_harness,
        board=board, swarm=getattr(cfg, "swarm", None),
        on_handback=on_handback, on_struggling=on_struggling)
    run.card_source = source
    try:
        record = run.run(claim=claim)
    finally:
        if owns_mm:
            try:
                mm.close()
            except Exception:
                pass
        if owns_backend and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:
                pass
        try:
            Git(ctx.repo).worktree_remove(worktree)
        except (Infra, OSError):
            pass
    record["card_hash"] = card_hash
    record["detail"] = run.detail
    return record


def _deadline(args) -> float | None:
    """When this run must stop claiming, as a `time.monotonic()` deadline.

    Either flag bounds a night. The check runs between cards, not inside one: a card
    is finished or blocked, never abandoned mid-way, because its partial work and its
    lease would be left behind.
    """
    wall_start = _dt.datetime.now(_dt.timezone.utc)
    mono_start = _time.monotonic()

    def monotonic_at(target: _dt.datetime) -> float:
        """A wall-clock moment as a monotonic deadline.

        The two clocks must not be mixed: `time.monotonic()` has an arbitrary epoch,
        so a wall-clock instant is converted by the offset between them here.
        """
        return mono_start + (target - wall_start).total_seconds()

    if getattr(args, "for_duration", None):
        try:
            seconds = parse_duration(args.for_duration, "--for")
        except ConfigError as exc:
            raise UsageError(str(exc)) from exc
        return mono_start + seconds

    if getattr(args, "until", None):
        text = str(args.until).strip()
        try:
            # `HH:MM` is a time of day; a date-time carries a date. Checking for `:`
            # alone would send every `HH:MM` down the ISO branch.
            is_time_of_day = re.fullmatch(r"\d{1,2}:\d{2}", text) is not None
            if is_time_of_day:
                hour, minute = (int(part) for part in text.split(":"))
                if not (0 <= hour < 24 and 0 <= minute < 60):
                    raise ValueError(f"{hour}:{minute} is not a time of day")
                target = wall_start.replace(hour=hour, minute=minute, second=0,
                                            microsecond=0)
                # "The next time the clock shows HH:MM": if that instant has already
                # passed today, it means tomorrow. Comparing to the start of this
                # minute instead would make the current minute's deadline negative.
                if target <= wall_start:
                    target += _dt.timedelta(days=1)
            else:
                target = _dt.datetime.fromisoformat(text)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=_dt.timezone.utc)
        except (ValueError, TypeError) as exc:
            raise UsageError(f"--until: cannot read {text!r} as a time") from exc
        return monotonic_at(target)
    return None


def _handshake(ctx: Context, source, args, telemetry_raw=None) -> dict:
    """Ask every adapter `hello`, and refuse to start if one is incompatible.

    The spec is explicit: "A run refuses to start if no version is shared or an op it
    needs is missing." Doing it here, before any card is claimed, turns version skew
    into an honest refusal instead of a mid-run adapter failure.
    """
    from .protocol import REQUIRED_OPS, check_hello

    names = {}
    try:
        hello = source.hello() if hasattr(source, "hello") else {"versions": [1],
                                                                "ops": []}
        check_hello(hello, "cards", set(REQUIRED_OPS["cards"]))
        names["cards"] = hello.get("name", "")
    except Invalid as exc:
        return {"refused": True,
                "reason": f"card source: {exc.message}",
                "adapters": names}

    runner = ctx.runner(ctx.repo)
    try:
        hello = runner.hello()
        check_hello(hello, "runner", set(REQUIRED_OPS["runner"]))
        names["runner"] = hello.get("name", "")
    except Invalid as exc:
        return {"refused": True,
                "reason": f"runner: {exc.message}",
                "adapters": names}

    try:
        pack = pack_for(ctx.cfg.languages[0], ctx.cfg.cargo_flags,
                        spec=ctx.cfg.pack_adapter, cwd=ctx.repo,
                        python=ctx.cfg.runner_python)
        hello = pack.hello()
        needed = set(REQUIRED_OPS["pack"])
        offered = set(hello.get("ops") or [])
        missing = needed - offered
        if missing:
            return {"refused": True,
                    "reason": f"language pack: it does not offer "
                              f"{', '.join(sorted(missing))}",
                    "adapters": names}
        names["pack"] = hello.get("name", "")
    except Invalid as exc:
        return {"refused": True, "reason": f"language pack: {exc.message}",
                "adapters": names}

    script_path = Path(args.script) if getattr(args, "script", None) else None
    try:
        backend = open_backend(ctx.cfg, script_path)
        hello = backend.hello() if hasattr(backend, "hello") else {
            "versions": [1], "ops": ["hello", "complete"]}
        check_hello(hello, "model", set(REQUIRED_OPS["model"]))
        names["model"] = hello.get("name", "")
    except Invalid as exc:
        return {"refused": True, "reason": f"model: {exc.message}",
                "adapters": names}

    if telemetry_raw is not None:
        try:
            hello = (telemetry_raw.hello()
                     if hasattr(telemetry_raw, "hello") else {"versions": [1],
                                                             "ops": []})
            check_hello(hello, "telemetry", set(REQUIRED_OPS["telemetry"]))
            names["telemetry"] = hello.get("name", "")
        except Invalid as exc:
            return {"refused": True, "reason": f"telemetry: {exc.message}",
                    "adapters": names}

    mm_script_raw = getattr(args, "mm_script", None)
    mm_cfg = getattr(ctx.cfg, "micromanager", None)
    swarm_cfg = getattr(ctx.cfg, "swarm", None)
    swarm_sup = (
        swarm_cfg.supervisor
        if (swarm_cfg is not None and swarm_cfg.enabled)
        else "config"
    )
    should_check_mm = bool(mm_script_raw) or is_acp_adapter(swarm_sup) or (
        swarm_sup == "config"
        and mm_cfg is not None
        and getattr(mm_cfg, "mode", "off") != "off"
    )
    if should_check_mm:
        mm_script = Path(mm_script_raw) if mm_script_raw else None
        mm = None
        try:
            mm = open_micromanager(ctx.cfg, ctx.state.root, ctx.repo,
                                   script=mm_script, worktree=ctx.repo)
            hello = mm.hello() if hasattr(mm, "hello") else {
                "versions": [1], "ops": ["hello", "assist"]}
            check_hello(hello, "micromanager", set(REQUIRED_OPS["micromanager"]))
            names["micromanager"] = hello.get("name", "")
        except Invalid as exc:
            return {"refused": True, "reason": f"micromanager: {exc.message}",
                    "adapters": names}
        finally:
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
    elif swarm_sup != "config":
        names["micromanager"] = swarm_sup

    if (
        swarm_cfg is not None
        and swarm_cfg.enabled
        and is_acp_adapter(str(getattr(swarm_cfg, "coordinator", "") or ""))
    ):
        try:
            if script_path is not None:
                coord_check = open_backend(ctx.cfg, script_path)
            else:
                from .coordinator import AcpCoordinatorBackend
                coord_check = AcpCoordinatorBackend(
                    ctx.cfg,
                    provider=normalize_acp_provider(swarm_cfg.coordinator),
                )
            hello = coord_check.hello()
            check_hello(hello, "model", set(REQUIRED_OPS["model"]))
            names["coordinator"] = hello.get("name", "")
        except Invalid as exc:
            return {"refused": True, "reason": f"coordinator: {exc.message}",
                    "adapters": names}

    return {"refused": False, "reason": "", "adapters": names}


def _backend_prices_spend(cfg, script: Path | None) -> bool:
    """Whether this configuration can report a cost at all."""
    if script is not None or cfg.model_adapter in ("scripted", "stub"):
        return False
    if is_acp_adapter(cfg.model_adapter):
        return True  # ACP providers report cost per turn or support fallback pricing.
    return bool(getattr(cfg, "cost_per_1k_in", 0.0)
                or getattr(cfg, "cost_per_1k_out", 0.0))


class _MeteredCoordinatorBackend:
    """The coordinator's backend, counted: its tokens and its spend.

    A model coordinator's calls are the run's spend like any card's, so each
    call's cost goes to the night budget as it happens (the spend cap sees it
    before the next card starts), and its tokens become the coordinator's part
    of the run's token split. Everything else passes through to the backend.
    """

    def __init__(self, inner, night: NightBudget) -> None:
        self._inner = inner
        self._night = night
        self._lock = threading.Lock()
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.spend = 0.0
        inner_complete = inner.complete

        @functools.wraps(inner_complete)
        def complete(*args, **kwargs):
            comp = inner_complete(*args, **kwargs)
            self._note(comp)
            return comp

        # `functools.wraps` keeps the inner signature visible to the callers that
        # inspect it to choose which optional arguments to pass.
        self.complete = complete

    def _note(self, comp) -> None:
        tok_in = int(getattr(comp, "tokens_in", 0) or 0)
        tok_out = int(getattr(comp, "tokens_out", 0) or 0)
        try:
            cost = float(getattr(comp, "cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        with self._lock:
            self.calls += 1
            self.tokens_in += tok_in
            self.tokens_out += tok_out
            self.spend += cost
        if cost:
            self._night.note_coordinator(cost)

    @property
    def tokens(self) -> int:
        with self._lock:
            return self.tokens_in + self.tokens_out

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


_WORKER_ROLES = ("planner", "test-writer", "implementer", "checker")


def _member_token_breakdown(board: Board, split: dict) -> dict:
    """`by_member` and `by_effort` for a swarm run, adding up to `split["total"]`.

    The board records usage per member as calls happen, but not every token the
    run counts lands on a member: a supervisor that is not a tier (the config's
    micromanager, an ACP supervisor) keeps its usage in the card records only.
    Such tokens are listed as `<part> (not on the board)` and under the effort
    `unrecorded`, so both maps still add up to the total. Members held by an
    outside agent (`kind = "external"`, as `swarm join` and `openfarm drive`
    make them) spend on their own account, not through this run's backends; any
    usage they report goes to `external_by_member` and is not in the total.
    """
    by_member: dict[str, int] = {}
    by_effort: dict[str, int] = {}
    external: dict[str, int] = {}
    recorded = {"worker": 0, "supervisor": 0, "coordinator": 0}
    for m in board.list_members(include_removed=True):
        mtokens = int(m.get("tokens_in", 0) or 0) + int(m.get("tokens_out", 0) or 0)
        role = str(m.get("role") or "")
        if str(m.get("kind") or "") == "external":
            if mtokens > 0:
                external[m["name"]] = mtokens
            continue
        if mtokens > 0 or role in _WORKER_ROLES:
            by_member[m["name"]] = mtokens
        part = ("worker" if role in _WORKER_ROLES else
                role if role in ("supervisor", "coordinator") else "")
        if part:
            recorded[part] += mtokens
        for eff_key, eff_tok in (m.get("tokens_by_effort") or {}).items():
            by_effort[eff_key] = by_effort.get(eff_key, 0) + int(eff_tok or 0)
    for part in ("worker", "supervisor", "coordinator"):
        missing = int(split.get(part, 0) or 0) - recorded[part]
        if missing > 0:
            by_member[f"{part} (not on the board)"] = missing
            by_effort["unrecorded"] = by_effort.get("unrecorded", 0) + missing
    result = {"by_member": by_member, "by_effort": by_effort}
    if external:
        result["external_by_member"] = external
    return result


def _update_breaker(ctx: Context, consecutive: list[tuple[str, str, str]]) -> None:
    limit = ctx.cfg.circuit_breaker
    if not limit:
        return
    if len(consecutive) < limit:
        ctx.state.write_breaker(False, [])
        return
    window = consecutive[-limit:]
    reasons = {code for _cid, code, _detail in window}
    if len(reasons) == 1:
        code = reasons.pop()
        ctx.state.write_breaker(
            True, [cid for cid, _c, _d in window],
            f"{limit} cards blocked in a row for {code}")


def cmd_status(ctx: Context, args) -> int:
    """Read-only: the live run, the caps, and the farm's own state."""
    holder = ctx.state.lock_holder()
    harness_lib = _open_harness_library(ctx, require_enabled=True)
    payload = {
        "run": holder or {},
        "paused": ctx.state.is_paused(),
        "kill_switch": ctx.state.kill_requested(ctx.cfg.kill_switch),
        "breaker": ctx.state.read_breaker(),
        "spend_cap": ctx.cfg.spend_per_night,
        "concurrent_cards": ctx.cfg.concurrent_cards,
        "last_run": _last_run(ctx),
        "harness": (harness_lib.status()
                    if harness_lib is not None else {"enabled": False}),
    }
    if args.card:
        card_id = _check_id(args.card, "card id")
        payload["card"] = str(ctx.state.card_dir(card_id, ""))
        payload["history"] = [
            {"run": path.parent.name,
             "state": (read_json(path) or {}).get("final_state", ""),
             "stop_code": (read_json(path) or {}).get("stop_code", "")}
            for path in sorted(ctx.state.cards.glob(f"{card_id}/*/card-record.json"))
        ]
    _emit_ctx(ctx, payload, "status")
    return EXIT_OK


def _last_run(ctx: Context) -> dict:
    runs = sorted(ctx.state.runs.glob("*/run.json"))
    if not runs:
        return {}
    data = read_json(runs[-1]) or {}
    counts: dict[str, int] = {}
    for outcome in data.get("outcomes", []):
        key = outcome.get("final_state") or "skipped"
        counts[key] = counts.get(key, 0) + 1
    return {"run_id": data.get("run_id", ""),
            "finished_at": data.get("finished_at", ""), "outcomes": counts}


def cmd_pause(ctx: Context, args) -> int:
    ctx.state.pause(args.reason or "")
    _emit_ctx(ctx, {"paused": True, "reason": args.reason or ""}, "pause")
    return EXIT_OK


def cmd_resume(ctx: Context, args) -> int:
    breaker = ctx.state.read_breaker()
    if breaker.get("tripped") and not args.clear_breaker:
        _emit_ctx(ctx, {"refused": True,
                        "reason": "the circuit breaker is tripped; pass --clear-breaker "
                                  "to clear it"}, "resume")
        return EXIT_REFUSED
    if ctx.state.kill_requested(ctx.cfg.kill_switch) and not args.clear_kill:
        _emit_ctx(ctx, {"refused": True,
                        "reason": "the kill switch exists; pass --clear-kill to clear it"}, "resume")
        return EXIT_REFUSED
    if args.clear_breaker:
        ctx.state.write_breaker(False, [])
    if args.clear_kill:
        ctx.state.clear_kill(ctx.cfg.kill_switch)
    ctx.state.resume()
    _emit_ctx(ctx, {"paused": False, "breaker_cleared": bool(args.clear_breaker),
                    "kill_cleared": bool(args.clear_kill)}, "resume")
    return EXIT_OK


def cmd_stop(ctx: Context, args) -> int:
    path = ctx.state.request_kill(ctx.cfg.kill_switch, args.reason or "")
    _emit_ctx(ctx, {"kill_switch": str(path), "reason": args.reason or ""}, "stop")
    return EXIT_OK


def cmd_release(ctx: Context, args) -> int:
    """Free a stuck card via card source first, then archive partial work."""
    card_id = _check_id(args.card, "card id")
    source = ctx.cards()
    source.prepare()
    try:
        source.release(card_id, reason=args.reason,
                       stop_code=StopCode.RELEASED.value)
    except Refused as exc:
        _emit_ctx(ctx, {"released": False, "reason": exc.message,
                        "archived": []}, "release")
        return EXIT_REFUSED

    card_dir = ctx.state.cards / card_id
    archived = []
    if card_dir.exists():
        partial = card_dir / "partial"
        partial.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().replace(":", "")
        for path in sorted(card_dir.iterdir()):
            if path.name == "partial":
                continue
            target = partial / f"{stamp}-{path.name}"
            try:
                path.rename(target)
                archived.append(str(target))
            except OSError:
                pass
    _emit_ctx(ctx, {"released": True, "card": card_id, "archived": archived}, "release")
    return EXIT_OK


def cmd_done(ctx: Context, args) -> int:
    """Mark a reviewed card done once its branch is merged into the base branch.

    A card waiting on others is claimed only when they are done, so without this
    step a wave never moves past its first cards.
    """
    card_id = _check_id(args.card, "card id")
    source = ctx.cards()
    source.prepare()
    card = next((c for c in source.all_cards() if c.id == card_id), None)
    if card is None:
        _emit_ctx(ctx, {"card": card_id, "done": False, "reason": "no such card"}, "done")
        return EXIT_REFUSED
    git = Git(ctx.repo)
    base = ctx.cfg.base_branch
    branch = f"openfarm/{card.id}-{card.slug()}"
    reason = ""
    if not git.ref_exists(f"refs/heads/{base}"):
        reason = f"the base branch {base} does not exist"
    elif not git.branch_exists(branch):
        reason = f"there is no branch {branch} to check"
    else:
        unmerged = git.count_commits(f"refs/heads/{branch}", f"refs/heads/{base}")
        if unmerged:
            reason = f"{branch} has {unmerged} commit(s) not merged into {base}"
    if reason:
        _emit_ctx(ctx, {"card": card_id, "done": False, "reason": reason}, "done")
        return EXIT_REFUSED
    try:
        source.mark_done(card_id, note=args.note or "")
    except Refused as exc:
        _emit_ctx(ctx, {"card": card_id, "done": False, "reason": exc.message}, "done")
        return EXIT_REFUSED
    _emit_ctx(ctx, {"card": card_id, "done": True, "branch": branch, "base": base}, "done")
    return EXIT_OK


def cmd_dossier(ctx: Context, args) -> int:
    """Read-only: the dossier from the card's latest run, or a named one.

    There is one dossier per run, not one per attempt, so `--attempt` names which
    attempt's gate results to show rather than choosing a different file. Asking for
    an attempt the card never reached is a refusal, not a silent fallback.
    """
    card_id = _check_id(args.card, "card id")
    if args.run:
        run_id = _check_id(args.run, "run id")
        path = ctx.state.cards / card_id / run_id / "dossier.json"
    else:
        candidates = sorted(ctx.state.cards.glob(f"{card_id}/*/dossier.json"))
        path = candidates[-1] if candidates else None
    if not path or not Path(path).exists():
        _emit_ctx(ctx, {"card": card_id, "error": "no dossier yet"}, "dossier")
        return EXIT_REFUSED
    payload = read_json(path) or {}

    if args.attempt is not None:
        gates = [entry for entry in payload.get("2_gates", [])
                 if entry.get("attempt") == args.attempt]
        if not gates:
            _emit_ctx(ctx, {"card": card_id,
                            "error": f"no gate ran on attempt {args.attempt}",
                            "attempts_run": sorted({entry.get("attempt")
                                                    for entry in payload.get("2_gates", [])})},
                      "dossier")
            return EXIT_REFUSED
        payload["2_gates"] = gates
        payload["attempt"] = args.attempt
        diff_file = Path(path).parent / "edits" / f"{args.attempt}.diff"
        if diff_file.exists():
            attempt_diff = diff_file.read_text()
            diffs = dict(payload.get("9_diffs") or {})
            diffs["attempt"] = args.attempt
            diffs["attempt_diff"] = attempt_diff
            diffs["production_diff"] = attempt_diff
            payload["9_diffs"] = diffs

    if args.format == "json" or ctx.json:
        if ctx.json:
            _emit_ctx(ctx, payload, "dossier")
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(payload))
    return EXIT_OK


def cmd_verdict(ctx: Context, args) -> int:
    """Records the reviewing lane's verdict. Writes the state folder only."""
    card_id = _check_id(args.card, "card id")
    candidates = sorted(ctx.state.cards.glob(f"{card_id}/*/card-record.json"))
    if not candidates:
        _emit_ctx(ctx, {"card": card_id, "error": "no run for this card"}, "verdict")
        return EXIT_REFUSED
    card_dir = candidates[-1].parent
    payload = {"card": card_id, "verdict": args.verdict,
               "review_tokens": args.review_tokens,
               "minutes": args.minutes, "note": args.note or "",
               "at": utc_now()}
    write_json(card_dir / "verdict.json", payload)
    _emit_ctx(ctx, payload, "verdict")
    return EXIT_OK


def cmd_escape(ctx: Context, args) -> int:
    """Records a defect found later in farm work. Writes the state folder only."""
    card_id = _check_id(args.card, "card id")
    candidates = sorted(ctx.state.cards.glob(f"{card_id}/*/card-record.json"))
    if not candidates:
        _emit_ctx(ctx, {"card": card_id, "error": "no run for this card"}, "escape")
        return EXIT_REFUSED
    from .state import append_jsonl
    path = candidates[-1].parent / "escapes.jsonl"
    payload = {"card": card_id, "note": args.note, "at": utc_now()}
    append_jsonl(path, payload)
    _emit_ctx(ctx, payload, "escape")
    return EXIT_OK


def cmd_supervise(ctx: Context, args) -> int:
    """Supervisor triage for blocked cards and ready/backlog queue preparation."""
    if not args.triage_blocked and not args.prepare_queue:
        raise UsageError("supervise requires --triage-blocked or --prepare-queue")

    card_id = _check_id(args.card, "card id") if args.card else None
    source = ctx.cards()
    source.prepare()
    mm_script = Path(args.mm_script) if getattr(args, "mm_script", None) else None
    supervision_dir = ctx.state.root / "supervision"
    supervision_dir.mkdir(parents=True, exist_ok=True)
    base_stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_stamp = base_stamp
    idx = 2
    while (
        (supervision_dir / run_stamp).exists()
        or (supervision_dir / f"{run_stamp}-supervise.json").exists()
    ):
        run_stamp = f"{base_stamp}-{idx}"
        idx += 1
    sup_card_dir = supervision_dir / run_stamp
    sup_card_dir.mkdir(parents=True, exist_ok=True)
    mm = open_micromanager(
        ctx.cfg, ctx.state.root, ctx.repo,
        script=mm_script, worktree=ctx.repo,
        card_dir=sup_card_dir,
    )
    harness_lib = _open_harness_library(ctx, require_enabled=True)
    all_cards = source.all_cards()
    if harness_lib is not None:
        langs = getattr(ctx.cfg, "languages", None) or ("rust",)
        lang = str(langs[0] or "rust")
        relevant_cards = [
            c for c in all_cards
            if (card_id is None or c.id == card_id)
            and (
                (args.triage_blocked and c.state == "blocked")
                or (args.prepare_queue and c.state in ("ready", "backlog"))
            )
        ]
        cats = list(dict.fromkeys([c.category for c in relevant_cards if c.category] + ["*"]))
        merged_cues: list[dict] = []
        seen_cues: set[str] = set()
        rev_val = 0
        state_val = "confirmed"
        for cat in cats:
            sel = harness_lib.select_guidance(
                language=lang,
                kind=cat,
                role="micromanager",
            )
            ctx_part = sel.micromanager_context or {}
            if ctx_part:
                rev_val = ctx_part.get("revision", rev_val)
                state_val = ctx_part.get("state", state_val)
                for cue in ctx_part.get("cues") or ():
                    if not isinstance(cue, dict):
                        continue
                    cid = str(cue.get("id", ""))
                    if cid and cid in seen_cues:
                        continue
                    if cid:
                        seen_cues.add(cid)
                    merged_cues.append(cue)
        if merged_cues:
            mm.harness_context = {
                "revision": rev_val,
                "state": state_val,
                "cues": merged_cues,
            }

    def _file_supervise_findings(findings: list[dict], default_card: str) -> list[str]:
        filed: list[str] = []
        limit = getattr(ctx.cfg, "findings_per_card", 2)
        for entry in findings:
            if len(filed) >= limit:
                break
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            from_card = str(entry.get("from_card") or default_card or "SUPERVISE")
            res = source.finding(from_card, title, str(entry.get("body", "")))
            filed.append(str(res.get("id", "") or title))
        return filed

    results: dict[str, dict] = {}
    applied: dict[str, object] = {"released": [], "findings": []}
    try:
        if args.triage_blocked:
            blocked = [c for c in all_cards
                       if c.state == "blocked"
                       and (card_id is None or c.id == card_id)]
            records = {}
            for c in blocked:
                rec_files = sorted(ctx.state.cards.glob(f"{c.id}/*/card-record.json"))
                if rec_files:
                    records[c.id] = read_json(rec_files[-1], {}) or {}
            report = mm.triage_and_review("triage_blocked", {
                "card_id": card_id or "",
                "cards": [c.as_dict() for c in blocked],
                "records": records,
            })
            results["triage_blocked"] = report.as_dict()
            if args.apply:
                for rec in report.recommendations:
                    if isinstance(rec, dict) and rec.get("action") == "release":
                        cid = rec.get("card_id") or rec.get("card")
                        if cid and any(c.id == cid for c in blocked):
                            source.release(
                                cid,
                                reason=str(rec.get("reason") or report.summary or "supervisor triage release"),
                                stop_code=StopCode.RELEASED.value,
                            )
                            applied["released"].append(cid)
                default_c = card_id or (blocked[0].id if blocked else "TRIAGE")
                applied["findings"].extend(
                    _file_supervise_findings(report.findings, default_c))

        if args.prepare_queue:
            queued = [c for c in all_cards
                      if c.state in ("ready", "backlog")
                      and (card_id is None or c.id == card_id)]
            report = mm.triage_and_review("prepare_queue", {
                "card_id": card_id or "",
                "cards": [c.as_dict() for c in queued],
            })
            results["prepare_queue"] = report.as_dict()
            if args.apply:
                default_c = card_id or (queued[0].id if queued else "QUEUE")
                applied["findings"].extend(
                    _file_supervise_findings(report.findings, default_c))

        mm_budget = mm.budget.as_dict() if hasattr(mm, "budget") else {}
    finally:
        mm.close()

    if len(results) == 1:
        only = next(iter(results.values()))
        payload = {**only, "applied": applied if args.apply else None,
                   "micromanager": mm_budget}
    else:
        payload = {"reports": results, "applied": applied if args.apply else None,
                   "micromanager": mm_budget}
    report_path = supervision_dir / f"{run_stamp}-supervise.json"
    write_json(report_path, payload)
    payload["report_path"] = str(report_path)
    _emit_ctx(ctx, payload, "supervise")
    return EXIT_OK


_GLOBAL_ID_RE = re.compile(r"^g-[0-9a-f]{10}$")


def _project_label(ctx: Context) -> str:
    return str(getattr(ctx.cfg, "name", "") or "").strip() or Path(ctx.repo).name


def _project_markers(ctx: Context) -> set[str] | None:
    """What ties a lesson to this project, or None when git can't list its files."""
    try:
        tracked = Git(ctx.repo).tracked_files()
    except (GitRefused, Infra, OSError):
        return None
    return project_markers(Path(ctx.repo), _project_label(ctx), tracked)


def cmd_harness(ctx: Context, args) -> int:
    """Inspect and manage the adaptive harness cue library."""
    action = getattr(args, "action", "status") or "status"
    target = getattr(args, "target", None)
    reason = getattr(args, "reason", None) or "manual"
    use_global = bool(getattr(args, "global_", False))

    if use_global and action not in ("status", "promote", "demote"):
        raise UsageError(f"harness {action} does not take --global")

    if use_global:
        global_lib = GlobalLibrary(global_dir_for(ctx.cfg.harness))
        if action == "status":
            _emit_ctx(ctx, global_lib.status(), "harness_global_status")
            return EXIT_OK
        if action == "promote":
            if not target:
                raise UsageError("harness promote requires <id>")
            try:
                if _GLOBAL_ID_RE.match(target):
                    res = global_lib.restore(target, reason=reason)
                else:
                    res = _open_harness_library(ctx, require_enabled=False).force_to_global(
                        global_lib, target, _project_label(ctx), _project_markers(ctx))
            except (Refused, Invalid) as exc:
                _emit_ctx(ctx, {"error": exc.message, "kind": "refused",
                                "code": exc.code}, "harness_promote")
                return EXIT_REFUSED
            _emit_ctx(ctx, res, "harness_promote")
            return EXIT_OK
        if action == "demote":
            if not target:
                raise UsageError("harness demote requires <id>")
            try:
                gid = target
                if not _GLOBAL_ID_RE.match(target):
                    gid = _open_harness_library(ctx, require_enabled=False).global_id_of(target)
                    if not gid:
                        raise Refused("not-global", f"{target} is not in the global library")
                res = global_lib.retire(gid, reason=reason)
            except (Refused, Invalid) as exc:
                _emit_ctx(ctx, {"error": exc.message, "kind": "refused",
                                "code": exc.code}, "harness_demote")
                return EXIT_REFUSED
            _emit_ctx(ctx, res, "harness_demote")
            return EXIT_OK

    harness_lib = _open_harness_library(ctx, require_enabled=False)
    assert harness_lib is not None

    if action == "status":
        _emit_ctx(ctx, harness_lib.status(), "harness_status")
        return EXIT_OK

    if action == "index":
        _emit_ctx(ctx, {"index": harness_lib.format_index(), "status": harness_lib.status()},
                  "harness_index")
        return EXIT_OK

    if action == "show":
        if not target:
            raise UsageError("harness show requires <id>")
        try:
            safe = _check_id(target, "entry id")
            entry = harness_lib.show_entry(safe)
            if entry is None:
                raise Refused("unknown-harness-entry",
                              f"harness entry {safe!r} does not exist")
        except (Refused, Invalid) as exc:
            _emit_ctx(ctx, {"error": exc.message, "kind": "refused", "code": exc.code},
                      "harness_show")
            return EXIT_REFUSED
        _emit_ctx(ctx, entry, "harness_show")
        return EXIT_OK

    if action == "promote":
        if not target:
            raise UsageError("harness promote requires <id>")
        try:
            res = harness_lib.promote_entry(target, reason=reason)
        except (Refused, Invalid) as exc:
            _emit_ctx(ctx, {"error": exc.message, "kind": "refused", "code": exc.code},
                      "harness_promote")
            return EXIT_REFUSED
        _emit_ctx(ctx, res, "harness_promote")
        return EXIT_OK

    if action == "demote":
        if not target:
            raise UsageError("harness demote requires <id_or_rev>")
        try:
            res = harness_lib.demote_entry_or_revision(target, reason=reason)
        except (Refused, Invalid) as exc:
            _emit_ctx(ctx, {"error": exc.message, "kind": "refused", "code": exc.code},
                      "harness_demote")
            return EXIT_REFUSED
        _emit_ctx(ctx, res, "harness_demote")
        return EXIT_OK

    if action == "reset":
        if not getattr(args, "yes", False):
            raise UsageError("harness reset requires --yes to confirm")
        res = harness_lib.reset()
        _emit_ctx(ctx, res, "harness_reset")
        return EXIT_OK

    raise UsageError(f"unknown harness action: {action}")


def _resolve_board(ctx: Context, run_id: str | None) -> tuple[str, Board]:
    if run_id:
        rid = _check_id(run_id, "run id")
    else:
        holder = ctx.state.lock_holder()
        if not holder or not holder.get("run_id"):
            raise Refused(
                "no-active-run",
                "no active run holds the lock; pass --run ID",
            )
        rid = _check_id(str(holder["run_id"]), "run id")
    bdir = board_dir_for(ctx.state, rid)
    if run_id and not bdir.exists() and not ctx.state.run_dir(rid).exists():
        raise Refused("unknown-run", f"run {rid!r} does not exist")
    return rid, Board(bdir, state_dir=ctx.state)


def _parse_seqs_arg(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise UsageError("--seqs expects comma-separated integers")
    out: list[int] = []
    for part in parts:
        try:
            seq = int(part)
        except ValueError as exc:
            raise UsageError(f"--seqs: {part!r} is not an integer") from exc
        if seq < 1:
            raise UsageError(f"--seqs: sequence numbers must be >= 1, got {seq}")
        out.append(seq)
    return out


def cmd_board(ctx: Context, args) -> int:
    """Read, post, answer, or wait on a run's board."""
    action = getattr(args, "board_command", None)
    if not action:
        if ctx.json:
            raise UsageError("board requires a subcommand: read, post, answer, or wait")
        action = "read"
    rid, board = _resolve_board(ctx, getattr(args, "run", None))

    if action == "read":
        if getattr(args, "status", False):
            _emit_ctx(ctx, {
                "run_id": rid,
                **board.status(),
                "roster": board.list_members(),
            }, "board_status")
            return EXIT_OK
        seqs = _parse_seqs_arg(getattr(args, "seqs", None))
        posts = board.read(
            as_name=getattr(args, "as_name", None),
            since=getattr(args, "since", None),
            limit=getattr(args, "limit", 20),
            seqs=seqs,
        )
        _emit_ctx(ctx, {"run_id": rid, "posts": posts, "count": len(posts)}, "board_read")
        return EXIT_OK

    if action == "post":
        if args.file is not None:
            try:
                body = Path(args.file).read_text(encoding="utf-8")
            except OSError as exc:
                raise Refused(
                    "unreadable-file", f"cannot read {args.file}: {exc}"
                ) from exc
        else:
            body = args.body if args.body is not None else ""
        # An outside voice: a registered, non-code member writing a note,
        # question, defect or result. Steering kinds go through `swarm`,
        # answers through `board answer`, and code's kinds are code's alone.
        post = board.outside_post(
            author=args.as_name,
            to=args.to,
            kind=args.kind,
            body=body,
            card=args.card,
            attempt=args.attempt,
            ref=args.ref,
        )
        _emit_ctx(ctx, {"run_id": rid, "post": post}, "board_post")
        return EXIT_OK

    if action == "answer":
        try:
            raw_text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise Refused(
                "unreadable-file", f"cannot read {args.file}: {exc}"
            ) from exc
        try:
            data = json.loads(raw_text)
        except ValueError as exc:
            raise Refused(
                "bad-json", f"{args.file} is not valid JSON: {exc}"
            ) from exc
        post = board.answer(
            args.seq,
            as_name=args.as_name,
            data=data,
            body=args.body,
        )
        _emit_ctx(ctx, {"run_id": rid, "post": post}, "board_answer")
        return EXIT_OK

    if action == "wait":
        try:
            timeout_s = parse_duration(args.timeout, "--timeout")
        except ConfigError as exc:
            raise UsageError(str(exc)) from exc
        if timeout_s < 0:
            raise UsageError("--timeout must be >= 0")
        result = board.wait(
            args.as_name,
            since=args.since,
            timeout_s=timeout_s,
            limit=args.limit,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK

    raise UsageError(f"unknown board subcommand: {action}")


def _maybe_prepare_source(ctx: Context):
    source = ctx.cards()
    if getattr(source, "worktree", None) is not None and not (ctx.repo / ".git").exists():
        return None
    source.prepare()
    return source


def cmd_swarm(ctx: Context, args) -> int:
    """Inspect tiers/roster and execute checked swarm actions."""
    action = getattr(args, "swarm_command", None)
    if not action:
        if ctx.json:
            raise UsageError(
                "swarm requires a subcommand: status, tiers, join, start, set, hint, block, or remove"
            )
        holder = ctx.state.lock_holder()
        if holder and holder.get("run_id"):
            rid, board = _resolve_board(ctx, None)
            _emit_ctx(ctx, {"run_id": rid, **swarm_status(board, ctx.cfg)}, "swarm_status")
            return EXIT_OK
        _emit_ctx(ctx, {"swarm": ctx.cfg.swarm.as_dict(), **swarm_tiers(ctx.cfg)}, "swarm_overview")
        return EXIT_OK
    if action == "tiers":
        _emit_ctx(ctx, swarm_tiers(ctx.cfg), "swarm_tiers")
        return EXIT_OK

    rid, board = _resolve_board(ctx, getattr(args, "run", None))

    if action == "status":
        _emit_ctx(ctx, {"run_id": rid, **swarm_status(board, ctx.cfg)}, "swarm_status")
        return EXIT_OK

    if action == "join":
        member = swarm_join(
            board,
            ctx.cfg,
            name=args.name,
            seat=args.seat,
            owner_kind=args.kind,
        )
        _emit_ctx(ctx, {"run_id": rid, "member": member}, "swarm_join")
        return EXIT_OK

    # A name typed after --as proves nothing: from the command line only a
    # member that joined the coordinator seat may steer (outside=True).
    if action == "start":
        card_id = _check_id(args.card, "card id")
        team = parse_team_spec(args.team)
        source = _maybe_prepare_source(ctx)
        res = swarm_start(
            board,
            ctx.cfg,
            caller=args.as_name,
            outside=True,
            card_id=card_id,
            team=team,
            source=source,
        )
        _emit_ctx(ctx, {"run_id": rid, **res}, "swarm_start")
        return EXIT_OK

    if action == "set":
        res = swarm_set(
            board,
            ctx.cfg,
            caller=args.as_name,
            outside=True,
            member_name=args.member,
            tier=args.tier,
            effort=args.effort,
            reason=args.reason,
        )
        _emit_ctx(ctx, {"run_id": rid, **res}, "swarm_set")
        return EXIT_OK

    if action == "hint":
        res = swarm_hint(
            board,
            ctx.cfg,
            caller=args.as_name,
            outside=True,
            member_name=args.member,
            body=args.body,
        )
        _emit_ctx(ctx, {"run_id": rid, **res}, "swarm_hint")
        return EXIT_OK

    if action == "block":
        card_id = _check_id(args.card, "card id")
        source = _maybe_prepare_source(ctx)
        res = swarm_block(
            board,
            ctx.cfg,
            caller=args.as_name,
            outside=True,
            card_id=card_id,
            reason=args.reason,
            source=source,
        )
        _emit_ctx(ctx, {"run_id": rid, **res}, "swarm_block")
        return EXIT_OK

    if action == "remove":
        source = _maybe_prepare_source(ctx)
        res = swarm_remove(
            board,
            ctx.cfg,
            caller=args.as_name,
            outside=True,
            member_name=args.member,
            source=source,
        )
        _emit_ctx(ctx, {"run_id": rid, **res}, "swarm_remove")
        return EXIT_OK

    raise UsageError(f"unknown swarm subcommand: {action}")


# -- the parser ----------------------------------------------------------------

_CLI_EPILOG = """\
Command groups (22 commands):
  Setup & Auth         init, adopt, install-skill, acp
  Run & Monitor        check, run, status, pause, resume, stop, tui
  Review & Harness     dossier, verdict, done, release, escape, supervise, harness
  Swarm, Board & Bench swarm, board, drive, bench

Common workflows:
  Bootstrap & auth     openfarm init my-app --idea "track habits"
                       openfarm adopt --dir .
                       openfarm acp status && openfarm acp login
  Check & run          openfarm check CARD-1 --baseline
                       openfarm run --dry-run --cards 3
                       openfarm run --cards 3 --spend 1.00
  Review & merge       openfarm dossier CARD-1
                       openfarm verdict CARD-1 clean --review-tokens 1200
                       git merge --no-ff openfarm/CARD-1-<slug> && openfarm done CARD-1
  Swarm, board & TUI   openfarm swarm && openfarm board && openfarm tui

Pass --json with any command for machine-readable JSON output.
"""


def build_parser() -> argparse.ArgumentParser:
    raw_fmt = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="openfarm",
        description="A plain-code orchestrator for narrowly scoped model work.",
        formatter_class=raw_fmt,
        epilog=_CLI_EPILOG,
    )
    parser.add_argument("--version", action="version",
                        version=f"openfarm {__version__}")
    parser.add_argument("--repo", default=".", help="the repository root")
    parser.add_argument("--config", default=None, help="an explicit config path")
    parser.add_argument("--json", action="store_true",
                        help="print the result as one JSON object")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="start a run (a night)",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm run --dry-run --cards 3\n  openfarm run --card CARD-1 --spend 1.00\n  openfarm run --cards 4 --for 2h",
    )
    run.add_argument("--cards", type=int, default=3,
                     help="most cards to claim")
    run.add_argument("--spend", type=float, default=None,
                     help="cap for this run, never above the config's")
    run.add_argument("--for", dest="for_duration", default=None,
                     help="stop after this long")
    run.add_argument("--until", default=None, help="stop at this UTC time")
    run.add_argument("--card", default=None, help="one named card")
    run.add_argument("--dry-run", action="store_true",
                     help="eligibility and baselines only: no model calls, no claims")
    run.add_argument("--script", default=None,
                     help="a scripted model backend, for tests and dry runs")
    run.add_argument("--mm-script", dest="mm_script", default=None,
                     help="a scripted micromanager backend, for tests")
    run.add_argument("--run-id", default=None, help="name this run")
    run.add_argument("--learn-from", dest="learn_from", default=None,
                     choices=("both", "project", "global"),
                     help="which lessons this run applies, over [harness] learn_from")

    status = sub.add_parser(
        "status",
        help="read-only state",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm status\n  openfarm status --card CARD-1",
    )
    status.add_argument("--card", default=None)

    pause = sub.add_parser("pause", help="stop new claims")
    pause.add_argument("--reason", default=None)

    resume = sub.add_parser("resume", help="remove the pause")
    resume.add_argument("--clear-breaker", action="store_true")
    resume.add_argument("--clear-kill", action="store_true")

    stop = sub.add_parser("stop", help="the kill switch")
    stop.add_argument("--reason", default=None)

    release = sub.add_parser(
        "release",
        help="free a stuck card",
        formatter_class=raw_fmt,
        epilog='Example:\n  openfarm release CARD-1 --reason "needs revised spec"',
    )
    release.add_argument("card")
    release.add_argument("--reason", required=True)

    done = sub.add_parser(
        "done",
        help="mark a reviewed, merged card done",
        formatter_class=raw_fmt,
        epilog='Example:\n  git merge --no-ff openfarm/CARD-1-slug && openfarm done CARD-1 --note "lgtm"',
    )
    done.add_argument("card")
    done.add_argument("--note", default=None)

    dossier = sub.add_parser(
        "dossier",
        help="print a card's dossier",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm dossier CARD-1\n  openfarm dossier CARD-1 --attempt 1 --format json",
    )
    dossier.add_argument("card")
    dossier.add_argument("--run", default=None)
    dossier.add_argument("--attempt", type=int, default=None)
    dossier.add_argument("--format", choices=("md", "json"), default="md")

    check = sub.add_parser(
        "check",
        help="eligibility on one card",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm check CARD-1\n  openfarm check CARD-1 --baseline",
    )
    check.add_argument("card")
    check.add_argument("--baseline", action="store_true")

    verdict = sub.add_parser(
        "verdict",
        help="record the reviewer's verdict",
        formatter_class=raw_fmt,
        epilog='Example:\n  openfarm verdict CARD-1 clean --review-tokens 1200 --note "verified locally"',
    )
    verdict.add_argument("card")
    verdict.add_argument("verdict",
                         choices=("clean", "minor-fix", "bounced", "scrapped"))
    verdict.add_argument("--review-tokens", type=int, required=True)
    verdict.add_argument("--minutes", type=float, default=None)
    verdict.add_argument("--note", default=None)

    escape = sub.add_parser("escape", help="record a later defect")
    escape.add_argument("card")
    escape.add_argument("--note", required=True)

    supervise = sub.add_parser(
        "supervise",
        help="run micromanager triage/queue prep",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm supervise --triage-blocked\n  openfarm supervise --prepare-queue --apply",
    )
    supervise.add_argument("--triage-blocked", action="store_true")
    supervise.add_argument("--prepare-queue", action="store_true")
    supervise.add_argument("--card", default=None)
    supervise.add_argument("--apply", action="store_true")
    supervise.add_argument("--mm-script", dest="mm_script", default=None)

    harness = sub.add_parser(
        "harness",
        help="inspect and manage harness adaptations",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm harness status\n  openfarm harness index\n  openfarm harness status --global\n  openfarm harness show exp-0001",
    )
    harness.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "index", "show", "promote", "demote", "reset"),
    )
    harness.add_argument("target", nargs="?", default=None)
    harness.add_argument("--global", dest="global_", action="store_true",
                         help="use the global harness library")
    harness.add_argument("--reason", default="manual")
    harness.add_argument("--yes", action="store_true")

    board = sub.add_parser(
        "board",
        help="read, post, answer, or wait on a run's board",
        formatter_class=raw_fmt,
        epilog='Examples:\n  openfarm board\n  openfarm board read --status\n  openfarm board post --as alice --to all --kind note --body "checking status"',
    )
    board_sub = board.add_subparsers(dest="board_command")

    board_read = board_sub.add_parser("read", help="read posts or status from the board")
    board_read.add_argument("--as", dest="as_name", default=None)
    board_read.add_argument("--since", type=int, default=None)
    board_read.add_argument("--limit", type=int, default=20)
    board_read.add_argument("--seqs", default=None)
    board_read.add_argument("--status", action="store_true")
    board_read.add_argument("--run", default=None)

    board_post = board_sub.add_parser("post", help="post a message to the board")
    board_post.add_argument("--as", dest="as_name", required=True)
    board_post.add_argument("--to", required=True)
    board_post.add_argument("--kind", choices=VALID_POST_KINDS, default="note")
    board_post.add_argument("--card", default="")
    board_post.add_argument("--attempt", type=int, default=0)
    board_post.add_argument("--ref", type=int, default=None)
    body_group = board_post.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", default=None)
    body_group.add_argument("--file", default=None)
    board_post.add_argument("--run", default=None)

    board_answer = board_sub.add_parser("answer", help="answer a board question")
    board_answer.add_argument("seq", type=int)
    board_answer.add_argument("--as", dest="as_name", required=True)
    board_answer.add_argument("--file", required=True)
    board_answer.add_argument("--body", default="")
    board_answer.add_argument("--run", default=None)

    board_wait = board_sub.add_parser("wait", help="wait for matching posts on the board")
    board_wait.add_argument("--as", dest="as_name", required=True)
    board_wait.add_argument("--since", type=int, default=None)
    board_wait.add_argument("--timeout", default="30m")
    board_wait.add_argument("--limit", type=int, default=None)
    board_wait.add_argument("--run", default=None)

    swarm = sub.add_parser(
        "swarm",
        help="inspect and control swarm roster and seats",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm swarm\n  openfarm swarm tiers\n  openfarm swarm status --run RUN_ID\n  openfarm swarm join --name alice --seat coordinator --kind human",
    )
    swarm_sub = swarm.add_subparsers(dest="swarm_command")

    swarm_status_p = swarm_sub.add_parser("status", help="show roster, tiers, and board status")
    swarm_status_p.add_argument("--run", default=None)

    swarm_sub.add_parser("tiers", help="list configured tiers and efforts")

    swarm_join_p = swarm_sub.add_parser("join", help="join an external seat")
    swarm_join_p.add_argument("--name", required=True)
    swarm_join_p.add_argument("--seat", choices=("coordinator", "supervisor"), required=True)
    swarm_join_p.add_argument("--kind", choices=("human", "agent"), default="human")
    swarm_join_p.add_argument("--run", default=None)

    swarm_start_p = swarm_sub.add_parser("start", help="start a ready card with a team")
    swarm_start_p.add_argument("card")
    swarm_start_p.add_argument("--team", default=None, help="role=tier:effort,...")
    swarm_start_p.add_argument("--as", dest="as_name", default="")
    swarm_start_p.add_argument("--run", default=None)

    swarm_set_p = swarm_sub.add_parser("set", help="change a member's tier or effort")
    swarm_set_p.add_argument("member")
    swarm_set_p.add_argument("--tier", default=None)
    swarm_set_p.add_argument("--effort", default=None)
    swarm_set_p.add_argument("--reason", default="")
    swarm_set_p.add_argument("--as", dest="as_name", default="")
    swarm_set_p.add_argument("--run", default=None)

    swarm_hint_p = swarm_sub.add_parser("hint", help="send a hint note to a member")
    swarm_hint_p.add_argument("member")
    swarm_hint_p.add_argument("--body", required=True)
    swarm_hint_p.add_argument("--as", dest="as_name", default="")
    swarm_hint_p.add_argument("--run", default=None)

    swarm_block_p = swarm_sub.add_parser("block", help="block a card with a reason")
    swarm_block_p.add_argument("card")
    swarm_block_p.add_argument("--reason", required=True)
    swarm_block_p.add_argument("--as", dest="as_name", default="")
    swarm_block_p.add_argument("--run", default=None)

    swarm_remove_p = swarm_sub.add_parser("remove", help="remove a finished card's member")
    swarm_remove_p.add_argument("member")
    swarm_remove_p.add_argument("--as", dest="as_name", default="")
    swarm_remove_p.add_argument("--run", default=None)

    bench = sub.add_parser(
        "bench",
        help="run the efficacy bench",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm bench\n  openfarm bench list --tasks bench\n  openfarm bench run --dry-run --task py-bounded-full",
    )
    bench_sub = bench.add_subparsers(dest="bench_command")
    bench_list = bench_sub.add_parser("list", help="list bench tasks")
    bench_list.add_argument("--tasks", "--dir", dest="dir", default="bench",
                            help="folder of bench tasks, one folder per task with a task.toml "
                            "(default: ./bench; the openfarm checkout ships one)")
    bench_run = bench_sub.add_parser(
        "run", help="run bench tasks in each arm and write a local report")
    bench_run.add_argument("--tasks", "--dir", dest="dir", default="bench",
                           help="folder of bench tasks, one folder per task with a task.toml "
                           "(default: ./bench; the openfarm checkout ships one)")
    bench_run.add_argument("--task", action="append", default=[],
                           help="task id to run (repeatable; default: all)")
    bench_run.add_argument("--arm", action="append", default=[],
                           metavar="NAME=FILE",
                           help="an arm (farm or solo) and its config file; repeatable")
    bench_run.add_argument("--repeats", type=int, default=None,
                           help="runs per task and arm (default 3; 1 with --dry-run)")
    bench_run.add_argument("--out", default=None,
                           help="report folder (default ~/.openfarm/bench/<time>)")
    bench_run.add_argument("--dry-run", action="store_true",
                           help="scripted answers from each task's script.json; costs nothing")

    init = sub.add_parser(
        "init",
        help="scaffold a new openfarm Rust project",
        formatter_class=raw_fmt,
        epilog='Example:\n  openfarm init calorie-log --idea "track daily meals" --commit',
    )
    init.add_argument("name", help="project name or slug (e.g. calorie-log)")
    init.add_argument("--dir", default=None,
                      help="target directory (default: <repo>/<project_slug> or current repo)")
    init.add_argument("--idea", default="", help="plain-English project idea")
    init.add_argument("--profile", choices=PROFILES, default="auto",
                      help="micromanager profile (default: auto)")
    init.add_argument("--acp-provider", choices=("claude", "antigravity", "codex"),
                      default="claude",
                      help="Frontier ACP provider for micromanager (default: claude)")
    init.add_argument("--state-dir", default=None, help="external state directory")
    init.add_argument("--secrets-dir", default=None, help="external secrets directory")
    init.add_argument("--endpoint", default="https://api.example.invalid/v1",
                      help="ZDR OpenAI-compatible base URL")
    init.add_argument("--model", default="lesser-coder",
                      help="default lesser coding model name")
    init.add_argument("--key-file", default=None, help="external API key file path")
    init.add_argument("--base-branch", default="main", help="git base branch name")
    init.add_argument("--mutation", choices=MUTATION_MODES, default="auto",
                      help="mutation gate mode (default: auto)")
    init.add_argument("--no-git", action="store_true", help="skip git init")
    init.add_argument("--commit", action="store_true",
                      help="commit initial scaffold on base branch")
    init.add_argument("--force", action="store_true",
                      help="overwrite scaffold files in non-empty directory")

    adopt = sub.add_parser(
        "adopt",
        help="add openfarm to an existing repository",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm adopt --dir .\n  openfarm adopt --dir . --lang python --no-suite",
    )
    adopt.add_argument("--dir", default=None,
                       help="repository directory (default: the current directory)")
    adopt.add_argument("--lang", choices=("rust", "python", "typescript"), default=None,
                       help="language pack to use (default: detect from manifests)")
    adopt.add_argument("--state-dir", default=None, help="external state directory")
    adopt.add_argument("--key-file", default=None, help="external API key file path")
    adopt.add_argument("--no-suite", action="store_true",
                       help="skip running the suite once to propose known_failing")

    install_skill = sub.add_parser(
        "install-skill", help="install bundled agent skills, commands, and optional bin shim"
    )
    install_skill.add_argument("--target", choices=INSTALL_TARGETS, default="all",
                               help="runtime destination (default: all)")
    install_skill.add_argument("--dest", default=None,
                               help="override base home or workspace directory")
    install_skill.add_argument("--install-bin", default=None,
                               help="install executable openfarm shim into this folder")
    install_skill.add_argument("--dry-run", action="store_true",
                               help="plan installation without writing files")

    acp = sub.add_parser(
        "acp",
        help="inspect, log in via OAuth, configure, or probe Frontier ACP providers",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm acp status\n  openfarm acp login --provider antigravity\n  openfarm acp configure --provider codex --mode active --auth oauth\n  openfarm acp probe",
    )
    acp.add_argument("action", nargs="?", default="status", choices=ACP_ACTIONS)
    acp.add_argument("--provider", choices=("claude", "antigravity", "codex", "all"),
                     default=None,
                     help="ACP provider (`claude`, `antigravity`, `codex`, or `all`)")
    acp.add_argument("--mode", choices=("active", "advisory", "off"), default=None)
    acp.add_argument("--auth", choices=("oauth", "api-key", "auto"), default=None)
    acp.add_argument("--binary", default=None,
                     help="ACP binary or resolution mode (`auto`, `claude`, `claude-agent-acp`, `agy`, `agy-print`, `antigravity-acp`, `codex`, `codex-acp`)")
    acp.add_argument("--model", default=None)
    acp.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default=None)
    acp.add_argument("--tool-policy", choices=("none", "read-only"), default=None)
    acp.add_argument("--auth-file", default=None)
    acp.add_argument("--clear-auth-file", action="store_true")
    login_grp = acp.add_mutually_exclusive_group()
    login_grp.add_argument("--claudeai", action="store_true",
                           help="authenticate using Claude subscription OAuth (default)")
    login_grp.add_argument("--console", action="store_true",
                           help="authenticate via Anthropic Console login")
    acp.add_argument("--sso", action="store_true", help="force SSO login flow")
    acp.add_argument("--email", default="", help="pre-populate login email")
    acp.add_argument("--setup-token", action="store_true",
                     help="run `claude setup-token` for long-lived CLAUDE_CODE_OAUTH_TOKEN")

    tui_p = sub.add_parser(
        "tui",
        help="open interactive terminal UI or render one snapshot",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm tui\n  openfarm tui --once --view overview\n  openfarm tui --once --view board --run RUN_ID",
    )
    tui_p.add_argument("--run", dest="run_id", default=None,
                       help="pin TUI to a specific run id")
    tui_p.add_argument("--refresh", type=float, default=1.0,
                       help="refresh interval in seconds (default: 1.0)")
    tui_p.add_argument("--once", action="store_true",
                       help="render a single view snapshot and exit")
    tui_p.add_argument("--view", choices=TUI_VIEWS, default="overview",
                       help="initial TUI view (default: overview)")

    drive_p = sub.add_parser(
        "drive",
        help="drive external coordinator/supervisor seats with claude, agy, or codex",
        formatter_class=raw_fmt,
        epilog="Examples:\n  openfarm drive --agent claude --seat both\n  openfarm drive --agent codex --seat both --cards 3 --wave\n  openfarm drive --agent antigravity --run RUN_ID --seat supervisor",
    )
    drive_p.add_argument(
        "--agent",
        choices=("claude", "antigravity", "agy", "codex"),
        default="claude",
        help="external driver CLI provider (default: claude)",
    )
    drive_p.add_argument(
        "--seat",
        choices=("both", "coordinator", "supervisor"),
        default="both",
        help="board seat(s) to drive (default: both)",
    )
    drive_p.add_argument("--run", dest="run_id", default=None,
                         help="attach to an active run id (omit to start a managed run)")
    drive_p.add_argument("--cards", type=int, default=None,
                         help="maximum cards to run")
    drive_p.add_argument("--card", action="append", default=[],
                         help="specific card id to run (repeatable)")
    drive_p.add_argument("--spend", type=float, default=None,
                         help="spend cap for a managed run")
    drive_p.add_argument("--driver-name", default="driver",
                         help="board member name prefix (default: driver)")
    drive_p.add_argument("--driver-bin", default=None,
                         help="path to driver CLI executable")
    drive_p.add_argument("--model", default=None,
                         help="model override for the driver agent")
    drive_p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"),
                         default=None, help="reasoning effort for the driver agent")
    drive_p.add_argument("--max-turns", type=int, default=40,
                         help="maximum driver turns (default: 40)")
    drive_p.add_argument("--wave", action="store_true",
                         help="inspect in-review dossiers and diffs after the run")
    drive_p.add_argument("--record-verdict", action="store_true",
                         help="record wave review verdicts on ledger (never merges or pushes)")
    drive_p.add_argument("--script", default=None,
                         help="JSON scripted responses for offline tests")

    return parser


def _emit_standalone(payload: dict, want_json: bool, *, kind: str | None = None) -> None:
    if want_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(format_human(kind or "", payload, style=Style(sys.stdout)))


def _emit_bench_error(message: str, want_json: bool) -> int:
    _emit_standalone({"error": message, "kind": "invalid"}, want_json, kind="error")
    return EXIT_TROUBLE


BENCH_NEEDS = (
    "bench needs a folder of bench tasks: one folder per task, each with a task.toml, "
    "the card, a repo/ to start from and hidden tests (the openfarm checkout ships "
    "one in bench/). Point it at one with --tasks PATH, for example "
    "'openfarm bench list --tasks ~/openfarm/bench' or "
    "'openfarm bench run --dry-run --tasks ~/openfarm/bench'."
)


def _emit_no_tasks_folder(tasks_dir: Path, want_json: bool, *, bare: bool = False) -> int:
    """No tasks folder: a usage problem, said plainly, with what bench needs."""
    if bare:
        message = f"no bench tasks folder here ({tasks_dir}). {BENCH_NEEDS}"
    elif tasks_dir.exists():
        message = f"{tasks_dir} is not a folder. {BENCH_NEEDS}"
    else:
        message = f"no bench tasks folder at {tasks_dir}. {BENCH_NEEDS}"
    if want_json:
        print(json.dumps({"error": message, "kind": "usage error"}, indent=2,
                         ensure_ascii=False))
    else:
        print(f"openfarm: bench: {message}", file=sys.stderr)
    return EXIT_USAGE


def cmd_bench(args) -> int:
    action = getattr(args, "bench_command", None)
    want_json = bool(getattr(args, "json", False))
    if action == "list":
        return cmd_bench_list(args)
    if action == "run":
        return cmd_bench_run(args)
    if not action and not want_json:
        if not hasattr(args, "dir") or not args.dir:
            repo_arg = getattr(args, "repo", ".") or "."
            repo_bench = Path(repo_arg) / "bench"
            args.dir = str(repo_bench) if repo_arg != "." and repo_bench.is_dir() else "bench"
        if not Path(args.dir).is_dir():
            return _emit_no_tasks_folder(Path(args.dir), want_json, bare=True)
        return cmd_bench_list(args)
    raise UsageError("bench requires a subcommand: list or run (" + BENCH_NEEDS + ")")


def cmd_bench_list(args) -> int:
    want_json = bool(getattr(args, "json", False))
    tasks_dir = Path(args.dir)
    if not tasks_dir.is_dir():
        return _emit_no_tasks_folder(tasks_dir, want_json)

    tasks: list[dict[str, str]] = []
    for child in sorted(tasks_dir.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if not (child / "task.toml").is_file():
            continue
        task_dir = child
        try:
            task = load_task(task_dir)
        except Invalid as exc:
            return _emit_bench_error(f"{task_dir}: {exc.message}", want_json)
        tasks.append({
            "id": task.id,
            "language": task.language,
            "category": task.category,
        })

    tasks.sort(key=lambda task: task["id"])
    _emit_standalone({"tasks": tasks}, want_json, kind="bench_list")
    return EXIT_OK


def cmd_bench_run(args) -> int:
    from .bench import load_arm, run_bench
    want_json = bool(getattr(args, "json", False))
    tasks_dir = Path(args.dir)
    if not tasks_dir.is_dir():
        return _emit_no_tasks_folder(tasks_dir, want_json)
    try:
        tasks = [load_task(child) for child in sorted(tasks_dir.iterdir())
                 if child.is_dir() and (child / "task.toml").is_file()]
        if args.task:
            wanted = set(args.task)
            unknown = sorted(wanted - {task.id for task in tasks})
            if unknown:
                return _emit_bench_error(f"unknown task(s): {unknown}", want_json)
            tasks = [task for task in tasks if task.id in wanted]
        arms: dict[str, str | None] = {}
        for spec in args.arm:
            name, sep, path = spec.partition("=")
            if not sep or name not in ("farm", "solo"):
                return _emit_bench_error(
                    f"--arm takes farm=FILE or solo=FILE, not {spec!r}", want_json)
            arms[name] = None if args.dry_run else load_arm(Path(path))
        if not arms:
            if not args.dry_run:
                return _emit_bench_error(
                    "name at least one arm: --arm farm=FILE and/or --arm solo=FILE "
                    "(examples in bench/arms/), or pass --dry-run", want_json)
            arms = {"farm": None}
        repeats = args.repeats if args.repeats is not None else (1 if args.dry_run else 3)
        if repeats < 1:
            return _emit_bench_error("--repeats must be at least 1", want_json)
        out = (Path(args.out) if args.out else
               Path.home() / ".openfarm" / "bench"
               / _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime()))
        report = run_bench(tasks, arms, repeats, out, dry_run=args.dry_run,
                           log=lambda message: print(message, file=sys.stderr))
    except Invalid as exc:
        return _emit_bench_error(exc.message, want_json)
    payload = {"out": str(out), "summary": report["summary"],
               "runs": len(report["records"])}
    if want_json:
        _emit_standalone(payload, True)
    else:
        print((out / "bench-report.md").read_text(encoding="utf-8"))
        print(f"report: {out}")
    return EXIT_OK


def cmd_init(args) -> int:
    directory = Path(args.dir) if args.dir else None
    repo_base = Path(args.repo) if getattr(args, "repo", None) else Path.cwd()
    payload = initialize_project(
        args.name,
        directory=directory,
        repo_base=repo_base,
        idea=args.idea,
        profile=args.profile,
        acp_provider=args.acp_provider,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        secrets_dir=Path(args.secrets_dir) if args.secrets_dir else None,
        endpoint=args.endpoint,
        model=args.model,
        key_file=Path(args.key_file) if args.key_file else None,
        base_branch=args.base_branch,
        mutation=args.mutation,
        no_git=bool(args.no_git),
        commit=bool(args.commit),
        force=bool(args.force),
    )
    _emit_standalone(payload, bool(getattr(args, "json", False)), kind="init")
    return EXIT_OK


def cmd_adopt(args) -> int:
    directory = Path(args.dir) if args.dir else Path.cwd()
    try:
        payload = adopt_project(
            directory,
            language=args.lang,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            key_file=Path(args.key_file) if args.key_file else None,
            run_suite=not args.no_suite,
        )
    except Refused as exc:
        _emit_standalone({"error": exc.message, "kind": "refused"},
                         bool(getattr(args, "json", False)), kind="adopt")
        return EXIT_REFUSED
    if payload.get("known_failing_proposed"):
        payload["next"] = ("the suite has failing tests; they are proposed in a "
                           "comment above [policy] known_failing in "
                           ".openfarm/config.toml. Copy them in if you agree, then "
                           "commit .openfarm/ and cards/")
    else:
        payload["next"] = "commit .openfarm/ and cards/, then write cards"
    _emit_standalone(payload, bool(getattr(args, "json", False)), kind="adopt")
    return EXIT_OK


def cmd_install_skill(args) -> int:
    payload = install_skill_bundle(
        target=args.target,
        dest=Path(args.dest) if args.dest else None,
        install_bin=Path(args.install_bin) if args.install_bin else None,
        dry_run=bool(args.dry_run),
    )
    _emit_standalone(payload, bool(getattr(args, "json", False)), kind="install_skill")
    return EXIT_OK


def cmd_acp(args) -> int:
    repo = Path(args.repo) if getattr(args, "repo", None) else Path.cwd()
    action = args.action
    provider = getattr(args, "provider", None)
    if action == "status":
        payload = inspect_acp_status(repo, provider=provider or "all")
    elif action == "login":
        login_mode = "console" if args.console else "claudeai"
        payload = run_acp_login(
            provider=provider or "claude",
            mode=login_mode,
            sso=bool(args.sso),
            email=args.email,
            setup_token=bool(args.setup_token),
        )
    elif action == "configure":
        payload = configure_acp_repo(
            repo,
            provider=provider,
            mode=args.mode,
            auth=args.auth,
            binary=args.binary,
            model=args.model,
            effort=args.effort,
            tool_policy=args.tool_policy,
            auth_file=args.auth_file,
            clear_auth_file=bool(args.clear_auth_file),
        )
    elif action == "probe":
        payload = probe_acp(
            repo,
            provider=provider,
            command=args.binary,
            auth_mode=args.auth,
        )
    else:
        raise UsageError(f"unknown acp action: {action}")
    _emit_standalone(payload, bool(getattr(args, "json", False)), kind=f"acp_{action}")
    return EXIT_OK


def cmd_tui(args) -> int:
    if args.refresh is not None and args.refresh <= 0:
        raise UsageError("--refresh must be positive")
    repo = Path(args.repo) if getattr(args, "repo", None) else Path.cwd()
    config_path = Path(args.config) if getattr(args, "config", None) else None
    return run_tui(
        repo=repo,
        config_path=config_path,
        run_id=args.run_id,
        refresh_s=float(args.refresh or 1.0),
        once=bool(args.once),
        view=args.view,
        json_output=bool(getattr(args, "json", False)),
    )


def cmd_drive(ctx: Context, args) -> int:
    """Drive external coordinator/supervisor board seats with a headless agent."""
    from .driver import drive_run

    payload = drive_run(
        ctx.repo,
        ctx.cfg,
        ctx.state,
        agent=args.agent,
        seat=args.seat,
        run_id=args.run_id,
        cards=args.cards,
        card_ids=list(getattr(args, "card", None) or ()),
        spend=args.spend,
        driver_name=args.driver_name,
        driver_bin=args.driver_bin,
        model=args.model,
        effort=args.effort,
        max_turns=args.max_turns,
        wave=bool(args.wave),
        record_verdict=bool(args.record_verdict),
        script=Path(args.script) if getattr(args, "script", None) else None,
    )
    _emit_ctx(ctx, payload, "drive")
    if payload.get("refused"):
        return EXIT_REFUSED
    return EXIT_OK if payload.get("ok") else EXIT_TROUBLE


PRE_CONFIG_COMMANDS = {
    "adopt": cmd_adopt,
    "bench": cmd_bench,
    "init": cmd_init,
    "install-skill": cmd_install_skill,
    "acp": cmd_acp,
    "tui": cmd_tui,
}


COMMANDS = {
    "run": cmd_run,
    "status": cmd_status,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "stop": cmd_stop,
    "release": cmd_release,
    "done": cmd_done,
    "dossier": cmd_dossier,
    "check": cmd_check,
    "verdict": cmd_verdict,
    "escape": cmd_escape,
    "supervise": cmd_supervise,
    "harness": cmd_harness,
    "board": cmd_board,
    "swarm": cmd_swarm,
    "drive": cmd_drive,
}


def _normalise_argv(argv: list[str]) -> list[str]:
    """Let `--json`, `--repo` and `--config` sit before or after the subcommand.

    Every command takes `--json`, and a person typing `openfarm check CARD --json`
    should not have to know that argparse wanted it first.
    """
    taking_value = ("--repo", "--config")
    early: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            rest.extend(argv[index:])
            break
        if token == "--json":
            early.append(token)
            index += 1
            continue
        if token in taking_value:
            if index + 1 >= len(argv) or argv[index + 1] in ("--", "--json"):
                raise UsageError(f"{token} requires a value")
            early.append(token)
            early.append(argv[index + 1])
            index += 2
            continue
        if any(token.startswith(name + "=") for name in taking_value):
            early.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return early + rest


_CHOICE_RE = re.compile(
    r"invalid choice:\s*'([^']+)'\s*\(choose from ([^)]+)\)",
    re.DOTALL,
)


def _suggest_subcommand(err_text: str) -> str:
    match = _CHOICE_RE.search(err_text or "")
    if not match:
        return ""
    bad = match.group(1).strip()
    raw_choices = [c.strip().strip("'\"") for c in match.group(2).split(",")]
    choices = [c for c in raw_choices if c]
    if not bad or not choices:
        return ""
    matches = difflib.get_close_matches(bad, choices, n=3, cutoff=0.5)
    if not matches and len(bad) >= 2:
        matches = [c for c in choices if c.startswith(bad)][:3]
    if not matches:
        matches = difflib.get_close_matches(bad, choices, n=2, cutoff=0.35)
    return ", ".join(matches)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    want_json = False
    for token in raw_argv:
        if token == "--":
            break
        if token == "--json":
            want_json = True
            break

    def _emit_error(kind: str, message: str, code: int) -> int:
        if want_json:
            print(json.dumps({"error": message, "kind": kind},
                             indent=2, ensure_ascii=False))
        else:
            print(f"openfarm: {kind}: {message}", file=sys.stderr)
            hint = error_hint(kind, message)
            if hint:
                st_err = Style(sys.stderr)
                print(st_err.dim(f"  hint: {hint}"), file=sys.stderr)
        return code

    parser = build_parser()
    try:
        normalised = _normalise_argv(raw_argv)
    except UsageError as exc:
        return _emit_error("usage error", str(exc), EXIT_USAGE)

    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(err_buf):
            args = parser.parse_args(normalised)
    except SystemExit as exc:
        if want_json:
            msg = err_buf.getvalue().strip().splitlines()[-1] if err_buf.getvalue().strip() else "invalid arguments"
            return _emit_error("usage error", msg, int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE)
        code = int(exc.code) if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)
        if code == 0:
            raise
        err_text = err_buf.getvalue()
        if err_text:
            sys.stderr.write(err_text)
        suggestion = _suggest_subcommand(err_text)
        if suggestion:
            st_err = Style(sys.stderr)
            print(st_err.yellow(f"Did you mean: {suggestion}?"), file=sys.stderr)
        return code
    if not args.command:
        if want_json:
            return _emit_error("usage error", "no subcommand specified", EXIT_USAGE)
        parser.print_help()
        return EXIT_USAGE

    if args.command in PRE_CONFIG_COMMANDS:
        try:
            return PRE_CONFIG_COMMANDS[args.command](args)
        except UsageError as exc:
            return _emit_error("usage error", str(exc), EXIT_USAGE)
        except ConfigError as exc:
            return _emit_error("config error", str(exc), EXIT_USAGE)
        except GitRefused as exc:
            return _emit_error("refused", str(exc), EXIT_REFUSED)
        except Refused as exc:
            return _emit_error("refused", exc.message, EXIT_REFUSED)
        except Invalid as exc:
            return _emit_error("adapter trouble", exc.message, EXIT_TROUBLE)
        except Infra as exc:
            return _emit_error("trouble", exc.message, EXIT_TROUBLE)
        except AdapterError as exc:
            return _emit_error("trouble", exc.message, EXIT_TROUBLE)
        except OSError as exc:
            return _emit_error("trouble", str(exc), EXIT_TROUBLE)

    try:
        ctx = Context(Path(args.repo), Path(args.config) if args.config else None,
                      json_output=args.json)
    except ConfigError as exc:
        return _emit_error("config error", str(exc), EXIT_USAGE)
    except OSError as exc:
        return _emit_error("cannot read the repository", str(exc), EXIT_USAGE)

    try:
        return COMMANDS[args.command](ctx, args)
    except UsageError as exc:
        return _emit_error("usage error", str(exc), EXIT_USAGE)
    except GitRefused as exc:
        return _emit_error("refused", str(exc), EXIT_REFUSED)
    except Refused as exc:
        return _emit_error("refused", exc.message, EXIT_REFUSED)
    except Invalid as exc:
        return _emit_error("adapter trouble", exc.message, EXIT_TROUBLE)
    except Infra as exc:
        return _emit_error("trouble", exc.message, EXIT_TROUBLE)
    except AdapterError as exc:
        return _emit_error("trouble", exc.message, EXIT_TROUBLE)
    except KeyboardInterrupt:
        return _emit_error("interrupted", "interrupted", EXIT_TROUBLE)


if __name__ == "__main__":
    raise SystemExit(main())
