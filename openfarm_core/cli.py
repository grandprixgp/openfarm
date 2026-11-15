"""The `openfarm` command: one entry point, ten subcommands, shared exit codes.

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
import json
import sys
from pathlib import Path

from .adapters.files import FileCardSource, open_file_source
from .adapters.jsonl import JsonlTelemetry, TelemetrySink
from .adapters.openai_compat import open_backend
from .adapters.direct import open_runner
from .briefs import BriefBook
from .budget import NightBudget
from .cards import Card
from .config import load_config
from .dossier import build_dossier, render_markdown
from .errors import (AdapterError, ConfigError, GateFailure, Infra, Invalid,
                     Refused, StopCode, UsageError)
from .flow import CardRun
from .git import Git, GitRefused
from .gates.eligibility import check_eligibility
from .globs import matches_any
from .packs.base import pack_for
from .state import (StateDir, read_json, utc_now, utc_after, write_json)

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

    def emit(self, payload: dict) -> None:
        if self.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                print(f"{key}: {value}")

    def cards(self, worktree: Path | None = None) -> FileCardSource:
        source = open_file_source(self.cfg, self.state.root)
        if worktree is not None:
            source.worktree = worktree
        return source

    def runner(self, worktree: Path, output_dir: Path | None = None):
        return open_runner(self.cfg, worktree, output_dir)


# -- commands ------------------------------------------------------------------

def cmd_check(ctx: Context, args) -> int:
    """Eligibility on one card, without claiming it."""
    source = ctx.cards()
    source.prepare()
    try:
        card = Card.from_dict(source.read(args.card)["card"])
    except Refused as exc:
        ctx.emit({"card": args.card, "eligible": False,
                  "failures": [{"rule": "read", "detail": exc.message}]})
        return EXIT_REFUSED

    deps_done = _deps_done(source, card)
    eligibility = check_eligibility(card, ctx.cfg, deps_done=deps_done)
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
    ctx.emit(payload)
    return EXIT_OK if payload["eligible"] else EXIT_REFUSED


def _deps_done(source: FileCardSource, card: Card) -> bool:
    if not card.deps:
        return True
    done = {other.id for other in source.all_cards() if other.state == "done"}
    return all(dep in done for dep in card.deps)


def _baseline_check(ctx: Context, source: FileCardSource, card: Card) -> dict:
    """Build base and confirm the named tests fail at runtime."""
    pack = pack_for(ctx.cfg.languages[0])
    worktree = ctx.state.root / "checks" / "worktree" / card.id
    git = Git(ctx.repo)
    base = (f"refs/heads/{ctx.cfg.base_branch}"
            if git.ref_exists(f"refs/heads/{ctx.cfg.base_branch}") else "HEAD")
    if worktree.exists():
        git.worktree_remove(worktree)
    git.worktree_add(worktree, base)
    try:
        runner = ctx.runner(worktree)
        target = ctx.state.check_dir(card.id) / "target"
        result = runner.exec(
            kind="test", argv=pack.commands("test", unit=card.unit)["argv"],
            cwd=worktree, target_dir=target, unit=card.unit,
            env=ctx.cfg.runner_env.get(card.unit, {}))
        parsed = pack.parse_tests(result.stdout, result.stderr)
        return {"ok": not parsed.get("compile_errors"),
                "compile_errors": parsed.get("compile_errors", []),
                "class": result.cls}
    finally:
        git.worktree_remove(worktree)


def cmd_run(ctx: Context, args) -> int:
    """Start a run: claim, drive, and move each card."""
    if ctx.state.kill_requested(ctx.cfg.kill_switch):
        ctx.emit({"refused": True, "reason": "the kill switch exists"})
        return EXIT_REFUSED
    if ctx.state.is_paused():
        ctx.emit({"refused": True, "reason": "the queue is paused"})
        return EXIT_REFUSED
    breaker = ctx.state.read_breaker()
    if breaker.get("tripped"):
        ctx.emit({"refused": True, "reason": "the circuit breaker is tripped"})
        return EXIT_REFUSED

    run_id = args.run_id or f"run-{utc_now().replace('-', '').replace(':', '')}"
    if not ctx.state.acquire_run_lock(run_id):
        holder = ctx.state.lock_holder() or {}
        ctx.emit({"refused": True,
                  "reason": f"another run holds the lock: {holder.get('run_id')}"})
        return EXIT_REFUSED

    try:
        return _do_run(ctx, args, run_id)
    finally:
        ctx.state.release_run_lock(run_id)


def _do_run(ctx: Context, args, run_id: str) -> int:
    source = ctx.cards()
    source.prepare()
    run_dir = ctx.state.create_run(run_id)
    telemetry = TelemetrySink(JsonlTelemetry(run_dir))

    cfg = ctx.cfg
    if cfg.state_dir.exists():
        try:
            (ctx.state.config_lock).write_text(
                (cfg.path.read_text() if cfg.path else "") +
                f"\n# hash: {cfg.config_hash()}\n")
        except OSError:
            pass

    identifiers = [args.card] if args.card else source.next(cfg.lane,
                                                           limit=args.cards)["cards"]
    night = NightBudget(args.spend or cfg.spend_per_night, cards_cap=args.cards)
    outcomes: list[dict] = []
    consecutive: list[tuple[str, str]] = []

    for card_id in identifiers:
        if ctx.state.kill_requested(cfg.kill_switch):
            outcomes.append({"card": card_id, "final_state": "blocked",
                             "stop_code": StopCode.KILLED.value})
            break
        reason = night.exhausted()
        if reason:
            outcomes.append({"card": card_id, "skipped": reason})
            continue
        if args.dry_run:
            outcomes.append(_dry_run(ctx, source, card_id))
            continue

        outcome = _run_card(ctx, source, card_id, run_id, telemetry,
                            run_dir, args.script)
        outcomes.append(outcome)
        if outcome.get("final_state") in ("blocked", "released"):
            code = outcome.get("stop_code", "")
            consecutive.append((code, outcome.get("detail", "")))
        else:
            consecutive = []
        night.note_card()
        night.note_spend(outcome.get("spend", 0.0) or 0.0)
        _update_breaker(ctx, consecutive)

    record = {"run_id": run_id, "started_at": utc_now(),
              "finished_at": utc_now(), "caps": {"spend": night.spend_cap,
                                                "cards": args.cards},
              "spend": night.spend,
              "outcomes": outcomes,
              "dry_run": bool(args.dry_run),
              "telemetry_problems": telemetry.problems}
    write_json(run_dir / "run.json", record)
    telemetry.write("run", record)
    telemetry.flush()
    ctx.emit(record)
    return EXIT_OK if not any(o.get("stop_code") == "infra" for o in outcomes) \
        else EXIT_TROUBLE


def _dry_run(ctx: Context, source: FileCardSource, card_id: str) -> dict:
    """Eligibility and baselines only: no model calls, no claims."""
    try:
        card = Card.from_dict(source.read(card_id)["card"])
    except AdapterError as exc:
        return {"card": card_id, "error": exc.message}
    eligibility = check_eligibility(card, ctx.cfg,
                                    deps_done=_deps_done(source, card))
    return {"card": card_id, "dry_run": True, "eligible": eligibility.ok,
            "failures": eligibility.failures}


def _run_card(ctx: Context, source: FileCardSource, card_id: str, run_id: str,
              telemetry, run_dir: Path, script: Path | None,
              backend=None) -> dict:
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
    pack = pack_for(cfg.languages[0])
    backend = backend if backend is not None else open_backend(cfg, script)
    runner = ctx.runner(worktree, output_dir=card_dir / "gates" / "output")
    book = BriefBook(worktree, cfg.never_touch, cfg.forbidden_files)
    conventions = ""
    if cfg.test_conventions_path:
        note = cfg.repo / cfg.test_conventions_path
        if note.exists() and not book.is_hidden(str(cfg.test_conventions_path)):
            conventions = note.read_text()

    run = CardRun(
        card=card, cfg=cfg, state=ctx.state, card_dir=card_dir,
        worktree=worktree, runner=runner, pack=pack, backend=backend,
        book=book, run_id=run_id, telemetry=telemetry,
        deps_done=_deps_done(source, card), conventions=conventions,
        log=lambda message: print(message, file=sys.stderr))
    run.card_source = source
    try:
        record = run.run(claim=claim)
    finally:
        try:
            Git(ctx.repo).worktree_remove(worktree)
        except (Infra, OSError):
            pass
    record["card_hash"] = card_hash
    record["detail"] = run.detail
    return record


def _update_breaker(ctx: Context, consecutive: list[tuple[str, str]]) -> None:
    limit = ctx.cfg.circuit_breaker
    if not limit:
        return
    if len(consecutive) < limit:
        ctx.state.write_breaker(False, [])
        return
    reasons = {code for code, _detail in consecutive[-limit:]}
    if len(reasons) == 1:
        code = reasons.pop()
        ctx.state.write_breaker(
            True, [f"card-{index}" for index in range(len(consecutive))],
            f"{limit} cards blocked in a row for {code}")


def cmd_status(ctx: Context, args) -> int:
    """Read-only: the live run, the caps, and the farm's own state."""
    holder = ctx.state.lock_holder()
    payload = {
        "run": holder or {},
        "paused": ctx.state.is_paused(),
        "kill_switch": ctx.state.kill_requested(ctx.cfg.kill_switch),
        "breaker": ctx.state.read_breaker(),
        "spend_cap": ctx.cfg.spend_per_night,
        "concurrent_cards": ctx.cfg.concurrent_cards,
        "last_run": _last_run(ctx),
    }
    if args.card:
        payload["card"] = str(ctx.state.card_dir(args.card, ""))
        payload["history"] = [
            {"run": path.parent.name,
             "state": (read_json(path) or {}).get("final_state", ""),
             "stop_code": (read_json(path) or {}).get("stop_code", "")}
            for path in sorted(ctx.state.cards.glob(f"{args.card}/*/card-record.json"))
        ]
    ctx.emit(payload)
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
    ctx.emit({"paused": True, "reason": args.reason or ""})
    return EXIT_OK


def cmd_resume(ctx: Context, args) -> int:
    breaker = ctx.state.read_breaker()
    if breaker.get("tripped") and not args.clear_breaker:
        ctx.emit({"refused": True,
                  "reason": "the circuit breaker is tripped; pass --clear-breaker "
                            "to clear it"})
        return EXIT_REFUSED
    if ctx.state.kill_requested(ctx.cfg.kill_switch) and not args.clear_kill:
        ctx.emit({"refused": True,
                  "reason": "the kill switch exists; pass --clear-kill to clear it"})
        return EXIT_REFUSED
    if args.clear_breaker:
        ctx.state.write_breaker(False, [])
    if args.clear_kill:
        ctx.state.clear_kill(ctx.cfg.kill_switch)
    ctx.state.resume()
    ctx.emit({"paused": False, "breaker_cleared": bool(args.clear_breaker),
              "kill_cleared": bool(args.clear_kill)})
    return EXIT_OK


def cmd_stop(ctx: Context, args) -> int:
    path = ctx.state.request_kill(ctx.cfg.kill_switch, args.reason or "")
    ctx.emit({"kill_switch": str(path), "reason": args.reason or ""})
    return EXIT_OK


def cmd_release(ctx: Context, args) -> int:
    """Free a stuck card, archiving its partial work first."""
    source = ctx.cards()
    source.prepare()
    card_dir = ctx.state.cards / args.card
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
    try:
        source.release(args.card, reason=args.reason,
                       stop_code=StopCode.RELEASED.value)
    except Refused as exc:
        ctx.emit({"released": False, "reason": exc.message,
                  "archived": archived})
        return EXIT_REFUSED
    ctx.emit({"released": True, "card": args.card, "archived": archived})
    return EXIT_OK


def cmd_dossier(ctx: Context, args) -> int:
    """Read-only: the dossier from the card's latest run, or a named one."""
    if args.run:
        path = ctx.state.cards / args.card / args.run / "dossier.json"
    else:
        candidates = sorted(ctx.state.cards.glob(f"{args.card}/*/dossier.json"))
        path = candidates[-1] if candidates else None
    if not path or not Path(path).exists():
        ctx.emit({"card": args.card, "error": "no dossier yet"})
        return EXIT_REFUSED
    payload = read_json(path) or {}
    if args.format == "json" or ctx.json:
        ctx.emit(payload)
    else:
        print(render_markdown(payload))
    return EXIT_OK


def cmd_verdict(ctx: Context, args) -> int:
    """Records the reviewing lane's verdict. Writes the state folder only."""
    candidates = sorted(ctx.state.cards.glob(f"{args.card}/*/card-record.json"))
    if not candidates:
        ctx.emit({"card": args.card, "error": "no run for this card"})
        return EXIT_REFUSED
    card_dir = candidates[-1].parent
    payload = {"card": args.card, "verdict": args.verdict,
               "review_tokens": args.review_tokens,
               "minutes": args.minutes, "note": args.note or "",
               "at": utc_now()}
    write_json(card_dir / "verdict.json", payload)
    ctx.emit(payload)
    return EXIT_OK


def cmd_escape(ctx: Context, args) -> int:
    """Records a defect found later in farm work. Writes the state folder only."""
    candidates = sorted(ctx.state.cards.glob(f"{args.card}/*/card-record.json"))
    if not candidates:
        ctx.emit({"card": args.card, "error": "no run for this card"})
        return EXIT_REFUSED
    from .state import append_jsonl
    path = candidates[-1].parent / "escapes.jsonl"
    payload = {"card": args.card, "note": args.note, "at": utc_now()}
    append_jsonl(path, payload)
    ctx.emit(payload)
    return EXIT_OK


# -- the parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openfarm",
        description="A plain-code orchestrator for narrowly scoped model work.")
    parser.add_argument("--repo", default=".", help="the repository root")
    parser.add_argument("--config", default=None, help="an explicit config path")
    parser.add_argument("--json", action="store_true",
                        help="print the result as one JSON object")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start a run (a night)")
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
    run.add_argument("--run-id", default=None, help="name this run")

    status = sub.add_parser("status", help="read-only state")
    status.add_argument("--card", default=None)

    pause = sub.add_parser("pause", help="stop new claims")
    pause.add_argument("--reason", default=None)

    resume = sub.add_parser("resume", help="remove the pause")
    resume.add_argument("--clear-breaker", action="store_true")
    resume.add_argument("--clear-kill", action="store_true")

    stop = sub.add_parser("stop", help="the kill switch")
    stop.add_argument("--reason", default=None)

    release = sub.add_parser("release", help="free a stuck card")
    release.add_argument("card")
    release.add_argument("--reason", required=True)

    dossier = sub.add_parser("dossier", help="print a card's dossier")
    dossier.add_argument("card")
    dossier.add_argument("--run", default=None)
    dossier.add_argument("--attempt", type=int, default=None)
    dossier.add_argument("--format", choices=("md", "json"), default="md")

    check = sub.add_parser("check", help="eligibility on one card")
    check.add_argument("card")
    check.add_argument("--baseline", action="store_true")

    verdict = sub.add_parser("verdict", help="record the reviewer's verdict")
    verdict.add_argument("card")
    verdict.add_argument("verdict",
                         choices=("clean", "minor-fix", "bounced", "scrapped"))
    verdict.add_argument("--review-tokens", type=int, required=True)
    verdict.add_argument("--minutes", type=float, default=None)
    verdict.add_argument("--note", default=None)

    escape = sub.add_parser("escape", help="record a later defect")
    escape.add_argument("card")
    escape.add_argument("--note", required=True)

    return parser


COMMANDS = {
    "run": cmd_run,
    "status": cmd_status,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "stop": cmd_stop,
    "release": cmd_release,
    "dossier": cmd_dossier,
    "check": cmd_check,
    "verdict": cmd_verdict,
    "escape": cmd_escape,
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
        if token == "--json":
            early.append(token)
            index += 1
            continue
        if token in taking_value:
            early.append(token)
            if index + 1 < len(argv):
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalise_argv(
        list(sys.argv[1:] if argv is None else argv)))
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    try:
        ctx = Context(Path(args.repo), Path(args.config) if args.config else None,
                      json_output=args.json)
    except ConfigError as exc:
        print(f"openfarm: config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"openfarm: cannot read the repository: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        return COMMANDS[args.command](ctx, args)
    except UsageError as exc:
        print(f"openfarm: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GitRefused as exc:
        print(f"openfarm: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Refused as exc:
        print(f"openfarm: refused: {exc.message}", file=sys.stderr)
        return EXIT_REFUSED
    except Invalid as exc:
        print(f"openfarm: adapter trouble: {exc.message}", file=sys.stderr)
        return EXIT_TROUBLE
    except Infra as exc:
        print(f"openfarm: trouble: {exc.message}", file=sys.stderr)
        return EXIT_TROUBLE
    except AdapterError as exc:
        print(f"openfarm: trouble: {exc.message}", file=sys.stderr)
        return EXIT_TROUBLE
    except KeyboardInterrupt:
        print("openfarm: interrupted", file=sys.stderr)
        return EXIT_TROUBLE


if __name__ == "__main__":
    raise SystemExit(main())
