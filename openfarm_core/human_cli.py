"""Human-friendly terminal rendering for `openfarm` CLI output and hints.

Keeps stdout crisp and scannable when `--json` is omitted: ANSI colors only
when writing to an interactive TTY (respecting `NO_COLOR` and `TERM=dumb`),
aligned Unicode/ASCII tables, token-split bars, chronological board feeds, and
contextual next-step hints.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path

ICON_OK = "✓"
ICON_FAIL = "✗"
ICON_DOT = "•"
ICON_PAUSED = "⏸"
ICON_RETURN = "↩"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def should_color(stream=None) -> bool:
    """Return True when `stream` is a TTY and color is not disabled by env."""
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    if not callable(isatty):
        return False
    try:
        if not isatty():
            return False
    except (OSError, ValueError):
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return True


class Style:
    """Minimal ANSI color/weight wrapper gated by TTY and `NO_COLOR`."""

    def __init__(self, stream=None, *, enabled: bool | None = None) -> None:
        self.stream = sys.stdout if stream is None else stream
        self.enabled = should_color(self.stream) if enabled is None else bool(enabled)

    def _wrap(self, code: str, text: object) -> str:
        raw = str(text)
        if not self.enabled or not raw:
            return raw
        return f"\x1b[{code}m{raw}\x1b[0m"

    def bold(self, text: object) -> str:
        return self._wrap("1", text)

    def dim(self, text: object) -> str:
        return self._wrap("2", text)

    def red(self, text: object) -> str:
        return self._wrap("31", text)

    def green(self, text: object) -> str:
        return self._wrap("32", text)

    def yellow(self, text: object) -> str:
        return self._wrap("33", text)

    def magenta(self, text: object) -> str:
        return self._wrap("35", text)

    def cyan(self, text: object) -> str:
        return self._wrap("36", text)

    def heading(self, text: object) -> str:
        return self._wrap("1;36", text)

    def tone(self, kind: str, text: object) -> str:
        mapping = {
            "ok": self.green,
            "green": self.green,
            "warn": self.yellow,
            "yellow": self.yellow,
            "err": self.red,
            "red": self.red,
            "info": self.cyan,
            "cyan": self.cyan,
            "accent": self.magenta,
            "magenta": self.magenta,
            "dim": self.dim,
            "bold": self.bold,
        }
        fn = mapping.get(kind)
        return fn(text) if fn is not None else str(text)


def visible_len(text: object) -> int:
    """Display length of `text` ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", str(text)))


def _pad_cell(text: object, width: int, *, right: bool = False) -> str:
    raw = str(text)
    pad = max(0, width - visible_len(raw))
    return (" " * pad + raw) if right else (raw + " " * pad)


def _truncate(text: object, limit: int = 72) -> str:
    single = " ".join(str(text or "").split())
    if len(single) <= limit:
        return single
    return single[: max(1, limit - 3)].rstrip() + "..."


def _fmt_int(value: object) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_usd(value: object, *, measured: bool = True) -> str:
    try:
        num = float(value or 0.0)
    except (TypeError, ValueError):
        return str(value)
    text = f"${num:.4f}"
    return text if measured else f"{text} (unpriced)"


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def format_token_split(tokens: dict, *, style: Style | None = None, bar_width: int = 16) -> str:
    """`12,180 total [████░] 91% worker (11,100) / 0% supervisor (0) / 9% coordinator (1,080)`.

    The coordinator's part is shown only when it used tokens, so a run without a
    model coordinator reads as it always did. Older splits have no coordinator.
    """
    st = style or Style(enabled=False)
    tokens = tokens if isinstance(tokens, dict) else {}
    total_tok = _as_int(tokens.get("total", 0))
    worker_tok = _as_int(tokens.get("worker", 0))
    sup_tok = _as_int(tokens.get("supervisor", 0))
    coord_tok = _as_int(tokens.get("coordinator", 0))
    w_share = _as_float(tokens.get("worker_share", 0.0))
    s_share = _as_float(tokens.get("supervisor_share", 0.0))
    c_share = _as_float(tokens.get("coordinator_share", 0.0))
    bar = format_bar(w_share, width=bar_width, style=st)
    text = (
        f"{_fmt_int(total_tok)} total  {bar}  "
        f"{w_share * 100:.0f}% worker ({_fmt_int(worker_tok)}) / "
        f"{s_share * 100:.0f}% supervisor ({_fmt_int(sup_tok)})"
    )
    if coord_tok > 0:
        text += f" / {c_share * 100:.0f}% coordinator ({_fmt_int(coord_tok)})"
    return text


def format_table(
    headers: list[str] | tuple[str, ...],
    rows: list[list[object] | tuple[object, ...]],
    *,
    style: Style | None = None,
    indent: int = 2,
    right_align: set[int] | None = None,
) -> str:
    """Render an aligned plain/ANSI-safe table."""
    st = style or Style(enabled=False)
    if not rows:
        return ""
    align_right = right_align or set()
    str_headers = [str(h) for h in headers]
    str_rows = [[str(cell) for cell in row] for row in rows]
    widths = [visible_len(h) for h in str_headers]
    for row in str_rows:
        for idx, cell in enumerate(row):
            if idx < len(widths):
                widths[idx] = max(widths[idx], visible_len(cell))
            else:
                widths.append(visible_len(cell))

    prefix = " " * max(0, indent)
    head_cells = [
        _pad_cell(st.bold(str_headers[i]), widths[i], right=(i in align_right))
        for i in range(len(str_headers))
    ]
    sep_cells = [st.dim("-" * widths[i]) for i in range(len(str_headers))]
    lines = [prefix + "  ".join(head_cells).rstrip(), prefix + "  ".join(sep_cells).rstrip()]
    for row in str_rows:
        cells = []
        for i in range(len(str_headers)):
            val = row[i] if i < len(row) else ""
            cells.append(_pad_cell(val, widths[i], right=(i in align_right)))
        lines.append(prefix + "  ".join(cells).rstrip())
    return "\n".join(lines)


def format_kv(
    pairs: list[tuple[str, object]],
    *,
    indent: int = 2,
    style: Style | None = None,
) -> str:
    """Render aligned `key: value` lines."""
    st = style or Style(enabled=False)
    items = [(str(k), v) for k, v in pairs if k]
    if not items:
        return ""
    width = max(visible_len(k) for k, _ in items)
    prefix = " " * max(0, indent)
    lines = []
    for key, val in items:
        padded_key = _pad_cell(st.dim(key + ":"), width + 1)
        lines.append(f"{prefix}{padded_key}  {val}")
    return "\n".join(lines)


def format_bar(share: float, width: int = 16, *, style: Style | None = None) -> str:
    """Render a compact horizontal share bar `[████░░░░]`."""
    st = style or Style(enabled=False)
    w = max(4, int(width))
    try:
        ratio = max(0.0, min(1.0, float(share)))
    except (TypeError, ValueError):
        ratio = 0.0
    filled = round(ratio * w)
    full = st.cyan("█" * filled) if filled else ""
    empty = st.dim("░" * (w - filled)) if filled < w else ""
    return f"[{full}{empty}]"


def status_icon(label: object) -> str:
    """Return a status glyph for a card, member, run, or gate state."""
    key = str(label or "").strip().lower()
    if key in (
        "done", "in-review", "in_review", "pass", "ok", "eligible",
        "active", "ready", "approved", "approve", "clean", "confirmed",
        "uphold", "working", "finished",
    ):
        return ICON_OK
    if key in (
        "blocked", "fail", "failed", "ineligible", "refused", "error",
        "stopped", "ended", "missing", "scrapped", "bounced", "bounce",
        "block", "reject", "killed", "infra",
    ):
        return ICON_FAIL
    if key in ("paused", "waiting", "minor-fix", "probing", "advisory"):
        return ICON_PAUSED
    if key in (
        "released", "handed-back", "handed_back", "demoted", "retired",
        "removed", "skipped", "dismiss_false_reject",
    ):
        return ICON_RETURN
    return ICON_DOT


def _state_tone(label: object) -> str:
    icon = status_icon(label)
    if icon == ICON_OK:
        return "ok"
    if icon == ICON_FAIL:
        return "err"
    if icon == ICON_PAUSED:
        return "warn"
    if icon == ICON_RETURN:
        return "yellow"
    return "cyan"


def format_state(label: object, *, style: Style | None = None) -> str:
    st = style or Style(enabled=False)
    text = str(label or "-")
    icon = status_icon(text)
    return st.tone(_state_tone(text), f"{icon} {text}")


def _render_next(steps: list[str], *, style: Style, numbered: bool = False) -> list[str]:
    clean = [s for s in steps if s]
    if not clean:
        return []
    lines = [style.bold("Next:")]
    for idx, step in enumerate(clean, start=1):
        prefix = f"  {idx}." if numbered else f"  {ICON_DOT}"
        lines.append(f"{style.dim(prefix)} {step}")
    return lines


def _render_kv_section(title: str, pairs: list[tuple[str, object]], *, style: Style) -> list[str]:
    lines = [style.bold(title)]
    body = format_kv(pairs, indent=2, style=style)
    if body:
        lines.append(body)
    return lines


def _format_tiers_table(tiers: dict, default_tier: str, *, style: Style) -> str:
    if not tiers:
        return f"  {style.dim('(no tiers configured)')}"
    rows = []
    for name in sorted(tiers):
        t = tiers[name] if isinstance(tiers[name], dict) else {}
        marker = f"{name} *" if name == default_tier else name
        efforts = ", ".join(str(e) for e in (t.get("efforts") or ())) or "-"
        eparam = str(t.get("effort_param") or "-")
        rows.append([
            style.cyan(marker) if name == default_tier else marker,
            t.get("model", ""),
            efforts,
            eparam,
        ])
    return format_table(["TIER", "MODEL", "EFFORTS", "EFFORT PARAM"], rows, style=style)


def _format_roster_table(members: list[dict], *, style: Style) -> str:
    if not members:
        return f"  {style.dim('(no board members)')}"
    rows = []
    for m in members:
        tier = str(m.get("tier") or "")
        effort = str(m.get("effort") or "")
        model = str(m.get("model") or "")
        if tier and effort:
            model_spec = f"{tier}:{effort}"
        elif tier:
            model_spec = tier
        elif model:
            model_spec = model
        else:
            model_spec = "-"
        toks = int(m.get("tokens_in", 0) or 0) + int(m.get("tokens_out", 0) or 0)
        state = str(m.get("state") or "active")
        wait_note = "waiting" if m.get("wait_since") and state == "waiting" else ""
        rows.append([
            m.get("name", ""),
            m.get("role", ""),
            m.get("kind", ""),
            format_state(state, style=style),
            m.get("card") or "-",
            model_spec,
            str(m.get("last_read_seq", 0) or 0),
            _fmt_int(toks),
            wait_note or "-",
        ])
    return format_table(
        ["NAME", "ROLE", "KIND", "STATE", "CARD", "TIER/MODEL", "SEQ", "TOKENS", "WAIT"],
        rows,
        style=style,
        right_align={6, 7},
    )


# -- individual command formatters -------------------------------------------


def _format_status(payload: dict, style: Style) -> str:
    run = payload.get("run") or {}
    paused = bool(payload.get("paused"))
    kill = bool(payload.get("kill_switch"))
    breaker = payload.get("breaker") or {}
    tripped = bool(breaker.get("tripped"))

    if kill:
        banner = style.red(f"{ICON_FAIL} KILL SWITCH ACTIVE")
        next_tips = ["openfarm resume --clear-kill  (clear the stop file after inspecting)"]
    elif tripped:
        reason = breaker.get("reason") or "consecutive card failures"
        banner = style.red(f"{ICON_FAIL} BREAKER TRIPPED") + f"  {style.dim(reason)}"
        next_tips = [
            "openfarm supervise --triage-blocked",
            "openfarm resume --clear-breaker",
        ]
    elif paused:
        banner = style.yellow(f"{ICON_PAUSED} PAUSED")
        next_tips = ["openfarm resume  (allow new card claims)"]
    elif run.get("run_id") and run.get("stale"):
        banner = style.yellow(f"{ICON_PAUSED} STALE LOCK {run['run_id']}") + "  " + style.dim(
            f"pid {run.get('pid')} is no longer running")
        next_tips = [
            "openfarm run --cards 3  (takes the lock over)",
            f"openfarm board read --run {run['run_id']}",
        ]
    elif run.get("run_id"):
        started = run.get("started_at") or ""
        banner = style.cyan(f"{ICON_DOT} ACTIVE RUN {run['run_id']}") + (
            f"  {style.dim('since ' + started)}" if started else ""
        )
        next_tips = [
            f"openfarm board read --run {run['run_id']}",
            f"openfarm swarm status --run {run['run_id']}",
            "openfarm tui",
        ]
    else:
        banner = style.green(f"{ICON_OK} IDLE")
        next_tips = [
            "openfarm check <CARD> --baseline",
            "openfarm run --cards 3",
        ]

    lines = [banner]
    spend_cap = payload.get("spend_cap", 0.0)
    cap_label = f"${float(spend_cap):.2f}/night" if spend_cap else "unlimited (0)"
    lines.extend(_render_kv_section("Configuration", [
        ("spend_cap", cap_label),
        ("concurrent_cards", payload.get("concurrent_cards", 1)),
    ], style=style))

    last_run = payload.get("last_run") or {}
    if last_run:
        outcomes = last_run.get("outcomes") or {}
        summary = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())) or "none"
        lines.extend(_render_kv_section("Last Run", [
            ("run_id", last_run.get("run_id", "-")),
            ("finished_at", last_run.get("finished_at", "-")),
            ("outcomes", summary),
        ], style=style))
    else:
        lines.extend(_render_kv_section("Last Run", [("status", style.dim("(none yet)"))], style=style))

    harness = payload.get("harness") or {}
    if harness.get("enabled"):
        rev = f"rev-{int(harness.get('revision', 0)):04d}"
        counts = (
            f"active={harness.get('active_count', 0)}, "
            f"probing={harness.get('probing_count', 0)}, "
            f"candidate={harness.get('candidate_count', 0)}, "
            f"demoted={harness.get('demoted_count', 0)}"
        )
        lines.extend(_render_kv_section("Harness", [
            ("revision", f"{rev} ({harness.get('state', 'confirmed')})"),
            ("entries", counts),
        ], style=style))
    else:
        lines.extend(_render_kv_section("Harness", [("status", style.dim("disabled"))], style=style))

    if "card" in payload:
        card_label = Path(str(payload["card"])).name or str(payload["card"])
        history = payload.get("history") or []
        lines.append(style.bold(f"Card {card_label} History"))
        if history:
            rows = [
                [
                    h.get("run", ""),
                    format_state(h.get("state") or "-", style=style),
                    h.get("stop_code") or "-",
                ]
                for h in history
            ]
            lines.append(format_table(["RUN", "STATE", "STOP CODE"], rows, style=style))
        else:
            lines.append(f"  {style.dim('(no runs recorded)')}")

    lines.extend(_render_next(next_tips, style=style))
    return "\n".join(lines)


def _format_tooling(tooling: object, style: Style) -> list[str]:
    """What the card's gates will run it with, the same way for every language."""
    if not isinstance(tooling, dict) or not tooling:
        return []
    if tooling.get("error"):
        return _render_kv_section("Tooling", [("error", style.red(tooling["error"]))],
                                  style=style)
    pairs: list[tuple[str, object]] = []
    if tooling.get("pack"):
        pairs.append(("language", tooling["pack"]))
    if tooling.get("runner"):
        pairs.append(("tests", tooling["runner"]))
    if tooling.get("typecheck"):
        check = tooling["typecheck"]
        if tooling.get("syntax_only") or check == "syntax":
            check = style.yellow("syntax only (no types are checked)")
        elif tooling.get("tsc"):
            check = f"{check} ({tooling['tsc']})"
        pairs.append(("typecheck", check))
    if tooling.get("lint"):
        lint = tooling["lint"]
        pairs.append(("lint", style.yellow("syntax only") if lint == "syntax" else lint))
    if tooling.get("format"):
        fmt = tooling["format"]
        if fmt == "off":
            fmt = style.yellow("off (formatting is not checked)")
        elif tooling.get("prettier"):
            fmt = f"{fmt} ({tooling['prettier']})"
        pairs.append(("format", fmt))
    lines = _render_kv_section("Tooling", pairs, style=style) if pairs else []
    notes = [str(note) for note in tooling.get("notes") or [] if str(note).strip()]
    if notes:
        lines.append(style.bold("Tooling Notes"))
        for note in notes:
            lines.append(f"  {style.yellow(ICON_DOT)} {note}")
    return lines


def _format_check(payload: dict, style: Style) -> str:
    card = payload.get("card", "?")
    eligible = bool(payload.get("eligible"))
    lines = []
    if eligible:
        lines.append(style.green(f"{ICON_OK} {card} is eligible"))
    else:
        lines.append(style.red(f"{ICON_FAIL} {card} is not eligible"))

    if payload.get("card_hash"):
        lines.append(format_kv([("card_hash", payload["card_hash"])], style=style))

    lines.extend(_format_tooling(payload.get("tooling"), style))

    failures = payload.get("failures") or []
    if failures:
        lines.append(style.bold("Rule Failures"))
        for f in failures:
            rule = f.get("rule", "rule")
            detail = f.get("detail", "")
            lines.append(f"  {style.red(ICON_FAIL)} [{style.bold(rule)}] {detail}")

    baseline = payload.get("baseline")
    if isinstance(baseline, dict):
        base_ok = bool(baseline.get("ok"))
        badge = style.green(f"{ICON_OK} ok") if base_ok else style.red(f"{ICON_FAIL} failed")
        pairs: list[tuple[str, object]] = [("status", badge)]
        if baseline.get("class"):
            pairs.append(("class", baseline["class"]))
        if baseline.get("error"):
            pairs.append(("error", style.red(baseline["error"])))
        lines.extend(_render_kv_section("Baseline", pairs, style=style))

        compile_errs = baseline.get("compile_errors") or []
        if compile_errs:
            lines.append(style.bold("Compile Errors"))
            for err in compile_errs[:5]:
                if isinstance(err, dict):
                    label = err.get("binary") or err.get("target") or err.get("pkg") or json.dumps(err)
                else:
                    label = str(err)
                lines.append(f"  {style.red(ICON_FAIL)} {label}")

        named = baseline.get("named") or {}
        if named:
            rows = []
            for test_name, st_val in named.items():
                if st_val == "ok":
                    note = style.red(f"{ICON_FAIL} already passes on base")
                elif st_val == "FAILED":
                    note = style.green(f"{ICON_OK} fails on base (expected)")
                elif st_val == "mixed":
                    note = style.red(f"{ICON_FAIL} fails in some rows only, proves nothing")
                elif st_val == "ignored":
                    note = style.red(f"{ICON_FAIL} skipped or expected to fail, proves nothing")
                elif st_val == "not-run":
                    note = style.yellow(f"{ICON_DOT} not on base yet (the test writer adds it)")
                else:
                    note = style.yellow(f"{ICON_DOT} {st_val}")
                rows.append([test_name, st_val, note])
            lines.append(format_table(["NAMED TEST", "STATUS", "BASELINE NOTE"], rows, style=style))

    if eligible:
        next_tips = [f"openfarm run --card {card}"]
        if baseline is None:
            next_tips.insert(0, f"openfarm check {card} --baseline  (verify base suite & failing tests)")
    else:
        next_tips = [f"Fix the issues above in cards/{card}.md, then re-run: openfarm check {card}"]
    lines.extend(_render_next(next_tips, style=style))
    return "\n".join(lines)


def _outcome_card(outcome: object) -> str:
    """The card an outcome belongs to.

    A real card record carries `card_id`; dry-run and skipped records carry `card`.
    """
    if not isinstance(outcome, dict):
        return ""
    return str(outcome.get("card_id") or outcome.get("card") or "")


def _outcome_row(outcome: dict, style: Style) -> list[str]:
    card = _outcome_card(outcome)
    if "skipped" in outcome:
        return [
            card,
            format_state("skipped", style=style),
            "-",
            "-",
            _truncate(outcome.get("skipped", ""), 64),
        ]
    if outcome.get("dry_run") or ("eligible" in outcome and "final_state" not in outcome):
        if outcome.get("error"):
            state = format_state("error", style=style)
            detail = _truncate(outcome["error"], 64)
        elif outcome.get("eligible"):
            state = format_state("eligible", style=style)
            base = outcome.get("baseline") or {}
            detail = "baseline ok" if base.get("ok") else (
                _truncate(base.get("error", ""), 64) if base else "eligible (dry-run)"
            )
        else:
            state = format_state("ineligible", style=style)
            fails = outcome.get("failures") or []
            if fails:
                detail = _truncate(f"[{fails[0].get('rule', '')}] {fails[0].get('detail', '')}", 64)
            else:
                detail = _truncate((outcome.get("baseline") or {}).get("error", "ineligible"), 64)
        return [card, state, "-", "-", detail]

    final_state = str(outcome.get("final_state") or "-")
    stop_code = str(outcome.get("stop_code") or "-")
    attempts = str(outcome.get("attempts", "-"))
    detail = _truncate(outcome.get("detail") or "-", 64)
    return [card, format_state(final_state, style=style), stop_code, attempts, detail]


def _format_run(payload: dict, style: Style) -> str:
    if payload.get("refused"):
        reason = payload.get("reason", "refused")
        lines = [style.red(f"{ICON_FAIL} Run refused: {reason}")]
        if payload.get("code"):
            lines.append(format_kv([("code", payload["code"])], style=style))
        if payload.get("child"):
            # Which ACP child the confinement rules refused (worker, coordinator, supervisor).
            child = f"{payload['child']} ({payload.get('provider') or '?'})"
            lines.append(format_kv([("ACP child", child)], style=style))
        adapters = payload.get("adapters")
        if isinstance(adapters, dict) and adapters:
            lines.append(format_kv([(k, v) for k, v in adapters.items()], style=style))
        lower = str(reason).lower()
        if "kill switch" in lower:
            tips = ["openfarm resume --clear-kill"]
        elif "paused" in lower:
            tips = ["openfarm resume"]
        elif "breaker" in lower:
            tips = ["openfarm supervise --triage-blocked", "openfarm resume --clear-breaker"]
        elif "lock" in lower:
            tips = ["openfarm status", "openfarm swarm status"]
        else:
            tips = ["openfarm status"]
        lines.extend(_render_next(tips, style=style))
        return "\n".join(lines)

    run_id = payload.get("run_id", "?")
    dry = bool(payload.get("dry_run"))
    badge = f" {style.magenta('[DRY RUN]')}" if dry else ""
    lines = [style.heading(f"Run {run_id}") + badge]

    caps = payload.get("caps") or {}
    spend_cap = caps.get("spend", 0.0)
    cap_text = f"cards={caps.get('cards', '-')}, spend={'$' + f'{float(spend_cap):.2f}' if spend_cap else 'unlimited'}"
    measured = bool(payload.get("spend_measured", True))
    tokens = payload.get("tokens") or {}
    total_spend = _as_float(payload.get("spend", 0.0))
    sup_spend = _as_float(payload.get("micromanager_spend", 0.0))
    coord_spend = _as_float(payload.get("coordinator_spend", 0.0))
    # Older run records have no `worker_spend`; their `spend` is the total.
    worker_spend = (_as_float(payload["worker_spend"]) if "worker_spend" in payload
                    else max(0.0, total_spend - sup_spend - coord_spend))
    summary_pairs: list[tuple[str, object]] = [
        ("finished_at", payload.get("finished_at", "-")),
        ("caps", cap_text),
        ("spend", _fmt_usd(total_spend, measured=measured)),
        ("worker_spend", _fmt_usd(worker_spend, measured=measured)),
        ("supervisor_spend", _fmt_usd(sup_spend, measured=measured)),
    ]
    if coord_spend or _as_int(tokens.get("coordinator", 0)):
        summary_pairs.append(("coordinator_spend", _fmt_usd(coord_spend, measured=measured)))
    lines.extend(_render_kv_section("Summary", summary_pairs, style=style))

    outcomes = payload.get("outcomes") or []
    lines.append(style.bold(f"Outcomes ({len(outcomes)})"))
    if outcomes:
        rows = [_outcome_row(o, style) for o in outcomes]
        lines.append(format_table(
            ["CARD", "STATE", "STOP CODE", "ATTEMPTS", "DETAIL"],
            rows,
            style=style,
            right_align={3},
        ))
    else:
        lines.append(f"  {style.dim('(no ready cards claimed)')}")

    lines.append(style.bold("Token Split"))
    lines.append("  " + format_token_split(tokens, style=style))
    for title, key in (("MEMBER", "by_member"), ("EFFORT", "by_effort")):
        table = tokens.get(key) or {}
        if isinstance(table, dict) and table:
            rows = [
                [name, _fmt_int(tok)]
                for name, tok in sorted(table.items(), key=lambda kv: (-_as_int(kv[1]), kv[0]))
            ]
            lines.append(format_table([title, "TOKENS"], rows, style=style, right_align={1}))
    external = tokens.get("external_by_member") or {}
    if isinstance(external, dict) and external:
        lines.append(f"  {style.dim('outside seats (their own spend, not in the total):')}")
        rows = [[name, _fmt_int(tok)] for name, tok in sorted(external.items())]
        lines.append(format_table(["MEMBER", "TOKENS"], rows, style=style, right_align={1}))

    harness = payload.get("harness") or {}
    if harness.get("enabled"):
        lines.extend(_render_kv_section("Harness", [
            ("revision", f"rev-{int(harness.get('revision', 0)):04d} ({harness.get('state', 'confirmed')})"),
            ("active", harness.get("active_count", 0)),
        ], style=style))

    notes = payload.get("notes") or []
    if notes:
        lines.append(style.bold("Notes"))
        for note in notes:
            lines.append(f"  {style.yellow(ICON_DOT)} {note}")

    problems = payload.get("telemetry_problems") or []
    if problems:
        lines.append(style.bold("Telemetry Warnings"))
        for prob in problems:
            lines.append(f"  {style.red(ICON_FAIL)} {prob}")

    next_tips: list[str] = []
    in_review = [_outcome_card(o) for o in outcomes
                 if o.get("final_state") in ("in-review", "in_review")]
    blocked = [_outcome_card(o) for o in outcomes if o.get("final_state") == "blocked"]
    if in_review:
        sample = in_review[0]
        next_tips.extend([
            f"openfarm dossier {sample}",
            f"openfarm verdict {sample} clean --review-tokens 1000",
            f"git merge --no-ff openfarm/{sample}-<slug> && openfarm done {sample}",
        ])
    if blocked:
        next_tips.append(f"openfarm dossier {blocked[0]}  (or: openfarm supervise --triage-blocked)")
    if dry:
        eligible_dry = [_outcome_card(o) for o in outcomes if o.get("eligible")]
        if eligible_dry:
            next_tips.append(f"openfarm run --cards {len(eligible_dry)}")
    if not next_tips:
        next_tips.append("openfarm status")
    lines.extend(_render_next(next_tips, style=style))
    return "\n".join(lines)


def _format_pause(payload: dict, style: Style) -> str:
    lines = [style.yellow(f"{ICON_PAUSED} Queue paused")]
    if payload.get("reason"):
        lines.append(format_kv([("reason", payload["reason"])], style=style))
    lines.extend(_render_next(["openfarm status", "openfarm resume"], style=style))
    return "\n".join(lines)


def _format_resume(payload: dict, style: Style) -> str:
    if payload.get("refused"):
        reason = payload.get("reason", "refused")
        lines = [style.red(f"{ICON_FAIL} Resume refused: {reason}")]
        tips = []
        if "breaker" in str(reason).lower():
            tips.append("openfarm resume --clear-breaker")
        if "kill" in str(reason).lower():
            tips.append("openfarm resume --clear-kill")
        lines.extend(_render_next(tips or ["openfarm status"], style=style))
        return "\n".join(lines)
    flags = []
    if payload.get("breaker_cleared"):
        flags.append("breaker cleared")
    if payload.get("kill_cleared"):
        flags.append("kill switch cleared")
    extra = f" ({', '.join(flags)})" if flags else ""
    lines = [style.green(f"{ICON_OK} Queue resumed{extra}")]
    lines.extend(_render_next(["openfarm run --cards 3"], style=style))
    return "\n".join(lines)


def _format_stop(payload: dict, style: Style) -> str:
    lines = [style.red(f"{ICON_FAIL} Kill switch armed")]
    pairs: list[tuple[str, object]] = [("kill_switch", payload.get("kill_switch", ""))]
    if payload.get("reason"):
        pairs.append(("reason", payload["reason"]))
    lines.append(format_kv(pairs, style=style))
    lines.extend(_render_next(["openfarm status", "openfarm resume --clear-kill"], style=style))
    return "\n".join(lines)


def _format_release(payload: dict, style: Style) -> str:
    if not payload.get("released"):
        lines = [style.red(f"{ICON_FAIL} Release refused: {payload.get('reason', 'refused')}")]
        lines.extend(_render_next(["openfarm status"], style=style))
        return "\n".join(lines)
    card = payload.get("card", "?")
    archived = payload.get("archived") or []
    lines = [style.yellow(f"{ICON_RETURN} Released {card}")]
    if archived:
        lines.append(format_kv([("archived", f"{len(archived)} item(s)")], style=style))
    lines.extend(_render_next([f"openfarm check {card}"], style=style))
    return "\n".join(lines)


def _format_done(payload: dict, style: Style) -> str:
    card = payload.get("card", "?")
    if not payload.get("done"):
        reason = str(payload.get("reason", "refused"))
        lines = [style.red(f"{ICON_FAIL} Cannot mark {card} done: {reason}")]
        match = re.search(r"(openfarm/\S+)\s+has\s+\d+\s+commit", reason)
        if match:
            tips = [f"git merge --no-ff {match.group(1)} && openfarm done {card}"]
        elif "no such card" in reason:
            tips = ["openfarm status"]
        else:
            tips = [f"openfarm status --card {card}", f"openfarm dossier {card}"]
        lines.extend(_render_next(tips, style=style))
        return "\n".join(lines)
    lines = [style.green(f"{ICON_OK} Marked {card} done")]
    lines.append(format_kv([
        ("branch", payload.get("branch", "")),
        ("base", payload.get("base", "")),
    ], style=style))
    lines.extend(_render_next(["openfarm status", "openfarm run --cards 3"], style=style))
    return "\n".join(lines)


def _format_dossier(payload: dict, style: Style) -> str:
    card = payload.get("card", "?")
    err = payload.get("error", "unavailable")
    lines = [style.red(f"{ICON_FAIL} Dossier unavailable for {card}: {err}")]
    if payload.get("attempts_run"):
        lines.append(format_kv([
            ("attempts_run", ", ".join(str(a) for a in payload["attempts_run"])),
        ], style=style))
    lines.extend(_render_next([f"openfarm status --card {card}"], style=style))
    return "\n".join(lines)


def _format_verdict(payload: dict, style: Style) -> str:
    card = payload.get("card", "?")
    if payload.get("error"):
        return "\n".join([
            style.red(f"{ICON_FAIL} Verdict refused for {card}: {payload['error']}"),
            *_render_next([f"openfarm status --card {card}"], style=style),
        ])
    v = str(payload.get("verdict", ""))
    lines = [style.green(f"{ICON_OK} Recorded verdict '{v}' for {card}")]
    pairs: list[tuple[str, object]] = [("review_tokens", _fmt_int(payload.get("review_tokens", 0)))]
    if payload.get("minutes") is not None:
        pairs.append(("minutes", payload["minutes"]))
    if payload.get("note"):
        pairs.append(("note", payload["note"]))
    lines.append(format_kv(pairs, style=style))
    if v in ("clean", "minor-fix"):
        tips = [f"git merge --no-ff openfarm/{card}-<slug> && openfarm done {card}"]
    else:
        tips = [f"openfarm release {card} --reason \"verdict {v}\""]
    lines.extend(_render_next(tips, style=style))
    return "\n".join(lines)


def _format_escape(payload: dict, style: Style) -> str:
    card = payload.get("card", "?")
    if payload.get("error"):
        return "\n".join([
            style.red(f"{ICON_FAIL} Escape refused for {card}: {payload['error']}"),
            *_render_next([f"openfarm status --card {card}"], style=style),
        ])
    lines = [style.yellow(f"{ICON_DOT} Recorded escape on {card}")]
    lines.append(format_kv([
        ("note", payload.get("note", "")),
        ("at", payload.get("at", "")),
    ], style=style))
    lines.extend(_render_next([f"openfarm dossier {card}"], style=style))
    return "\n".join(lines)


def _format_supervise_section(title: str, report: dict, style: Style) -> list[str]:
    lines = [style.bold(title)]
    if report.get("verdict"):
        lines.append(format_kv([("verdict", format_state(report["verdict"], style=style))], style=style))
    if report.get("summary"):
        lines.append(f"  {report['summary']}")
    recs = report.get("recommendations") or []
    if recs:
        rows = []
        for rec in recs:
            if isinstance(rec, dict):
                rows.append([
                    rec.get("action") or "-",
                    rec.get("card_id") or rec.get("card") or "-",
                    _truncate(rec.get("reason") or rec.get("detail") or rec.get("note") or "-", 64),
                ])
            else:
                rows.append(["-", "-", _truncate(rec, 64)])
        lines.append(format_table(["ACTION", "CARD", "REASON"], rows, style=style))
    findings = report.get("findings") or []
    if findings:
        lines.append(f"  {style.bold('Findings')}")
        for f in findings:
            if isinstance(f, dict):
                src = f.get("from_card") or ""
                prefix = f"[{src}] " if src else ""
                lines.append(f"    {ICON_DOT} {prefix}{f.get('title', '')}")
                if f.get("body"):
                    lines.append(f"      {style.dim(_truncate(f['body'], 90))}")
            else:
                lines.append(f"    {ICON_DOT} {f}")
    return lines


def _format_supervise(payload: dict, style: Style) -> str:
    lines = [style.heading("Supervision Report")]
    reports = payload.get("reports")
    if isinstance(reports, dict):
        for name, rep in reports.items():
            if isinstance(rep, dict):
                lines.extend(_format_supervise_section(name.replace("_", " ").title(), rep, style))
    else:
        op_title = str(payload.get("op") or "Report").replace("_", " ").title()
        lines.extend(_format_supervise_section(op_title, payload, style))

    applied = payload.get("applied")
    if isinstance(applied, dict):
        rel = ", ".join(applied.get("released") or ()) or "none"
        fnd = ", ".join(applied.get("findings") or ()) or "none"
        lines.extend(_render_kv_section("Applied", [
            ("released", rel),
            ("findings", fnd),
        ], style=style))
    else:
        lines.append(f"  {style.dim('(advisory only: pass --apply to execute releases and file findings)')}")

    mm = payload.get("micromanager") or {}
    if mm:
        lines.extend(_render_kv_section("Micromanager Budget", [
            ("calls", f"{mm.get('calls', 0)} (max/card {mm.get('max_calls_per_card', '-')})"),
            ("tokens", f"{_fmt_int(mm.get('tokens_in', 0))} in / {_fmt_int(mm.get('tokens_out', 0))} out"),
            ("spend", _fmt_usd(mm.get("spend", 0.0))),
        ], style=style))

    if payload.get("report_path"):
        lines.append(format_kv([("report_path", payload["report_path"])], style=style))
    return "\n".join(lines)


def _format_harness_status(payload: dict, style: Style) -> str:
    enabled = bool(payload.get("enabled"))
    rev = f"rev-{int(payload.get('revision', 0)):04d}"
    state = payload.get("state", "confirmed")
    badge = style.green(f"{ICON_OK} enabled") if enabled else style.yellow(f"{ICON_PAUSED} disabled")
    lines = [style.heading(f"Harness Status ({rev}, {state})") + f"  {badge}"]
    lines.extend(_render_kv_section("Entries", [
        ("active", payload.get("active_count", 0)),
        ("probing", f"{payload.get('probing_count', 0)} (remaining {payload.get('probe_remaining', 0)})"),
        ("candidate", payload.get("candidate_count", 0)),
        ("demoted", payload.get("demoted_count", 0)),
        ("total", payload.get("total_entries", 0)),
    ], style=style))

    metrics = payload.get("metrics") or {}
    if metrics:
        lines.extend(_render_kv_section("Metrics", [
            ("cards_total", metrics.get("cards_total", 0)),
            ("pass_rate", f"{float(metrics.get('pass_rate', 0.0)) * 100:.1f}%"),
            ("first_pass_rate", f"{float(metrics.get('first_pass_rate', 0.0)) * 100:.1f}%"),
            ("harness_efficiency", f"{float(metrics.get('harness_efficiency', 0.0)):.3f}"),
        ], style=style))

    active_ids = payload.get("active_entry_ids") or []
    if active_ids:
        lines.append(format_kv([("active_entry_ids", ", ".join(active_ids))], style=style))
    lines.extend(_render_next(["openfarm harness index", "openfarm harness show <ID>"], style=style))
    return "\n".join(lines)


def _format_harness_global_status(payload: dict, style: Style) -> str:
    lines = [style.heading("Global Harness Library")]
    lines.extend(_render_kv_section("Summary", [
        ("dir", payload.get("dir", "")),
        ("active", payload.get("active", 0)),
        ("retired", payload.get("retired", 0)),
    ], style=style))
    entries = payload.get("entries") or []
    if entries:
        rows = [
            [
                e.get("id", ""),
                format_state(e.get("status") or "-", style=style),
                e.get("surface", ""),
                e.get("scope", ""),
                ", ".join(e.get("sources") or ()) or "-",
                ", ".join(e.get("projects") or ()) or "-",
                _truncate(e.get("cue", ""), 50),
            ]
            for e in entries
        ]
        lines.append(format_table(
            ["ID", "STATUS", "SURFACE", "SCOPE", "SOURCES", "PROJECTS", "CUE"],
            rows,
            style=style,
        ))
    else:
        lines.append(f"  {style.dim('(no global entries yet)')}")
    return "\n".join(lines)


def _format_harness_show(payload: dict, style: Style) -> str:
    if payload.get("error"):
        return style.red(f"{ICON_FAIL} {payload['error']}")
    eid = payload.get("id", "?")
    st_val = payload.get("status", "?")
    lines = [style.heading(f"Harness Entry {eid}") + f"  {format_state(st_val, style=style)}"]
    stats = payload.get("stats") or {}
    lines.extend(_render_kv_section("Metadata", [
        ("title", payload.get("title", "")),
        ("surface", payload.get("surface", "")),
        ("role", payload.get("role", "*")),
        ("scope", f"{payload.get('language', '*')}/{payload.get('kind', '*')}"),
        ("failure_class", payload.get("failure_class") or "-"),
        ("confidence", f"{float(payload.get('confidence', 0.0)):.2f}"),
        ("origin", payload.get("origin", "project")),
        ("stats", (
            f"applied={stats.get('applied', 0)}, helped={stats.get('helped', 0)}, "
            f"hurt={stats.get('hurt', 0)}, neutral={stats.get('neutral', 0)}"
        )),
    ], style=style))
    if payload.get("cue"):
        lines.append(style.bold("Cue"))
        lines.append(f"  {payload['cue']}")
    if payload.get("lesson"):
        lines.append(style.bold("Lesson"))
        lines.append(f"  {payload['lesson']}")
    if payload.get("proposed_patch") is not None:
        lines.append(style.bold("Proposed Patch"))
        patch = payload["proposed_patch"]
        if isinstance(patch, (dict, list)):
            for line in json.dumps(patch, indent=2, ensure_ascii=False).splitlines():
                lines.append(f"  {line}")
        else:
            lines.append(f"  {patch}")
    exp = str(payload.get("experience_md") or "").strip()
    if exp:
        lines.append(style.bold("Experience"))
        lines.append(exp)
    return "\n".join(lines)


def _format_harness_mutation(kind: str, payload: dict, style: Style) -> str:
    if payload.get("error"):
        code = payload.get("code") or payload.get("kind") or "refused"
        return style.red(f"{ICON_FAIL} Refused ({code}): {payload['error']}")
    if kind == "harness_reset":
        demoted = payload.get("demoted_entry_ids") or []
        lines = [style.yellow(f"{ICON_RETURN} Harness reset to revision 0")]
        lines.append(format_kv([("demoted_entries", len(demoted))], style=style))
        return "\n".join(lines)
    if kind == "harness_promote":
        if payload.get("forced"):
            return style.green(
                f"{ICON_OK} Promoted {payload.get('entry_id')} to global library ({payload.get('global_id')})"
            )
        if "id" in payload and "status" in payload:
            return style.green(
                f"{ICON_OK} Global lesson {payload['id']} is now {payload['status']}"
            )
        rev = f"rev-{int(payload.get('revision', 0)):04d}"
        lines = [style.green(f"{ICON_OK} Promoted {payload.get('entry_id', '?')} ({rev})")]
        evicted = payload.get("evicted") or []
        if evicted:
            lines.append(format_kv([("evicted_by_cap", ", ".join(evicted))], style=style))
        return "\n".join(lines)
    # harness_demote
    if "id" in payload and "status" in payload:
        msg = f"{ICON_RETURN} Global lesson {payload['id']} {payload['status']}"
        if payload.get("retire_reason"):
            msg += f" ({payload['retire_reason']})"
        return style.yellow(msg)
    demoted = payload.get("demoted_entry_ids") or []
    lines = [style.yellow(f"{ICON_RETURN} Demoted {payload.get('target', '?')}")]
    lines.append(format_kv([
        ("revision", f"rev-{int(payload.get('revision', 0)):04d}"),
        ("demoted_entries", ", ".join(demoted) or "none"),
    ], style=style))
    return "\n".join(lines)


def _format_post_block(post: dict, style: Style) -> list[str]:
    seq = post.get("seq", "?")
    kind = str(post.get("kind") or "note")
    sender = str(post.get("from") or "?")
    raw_to = post.get("to")
    if isinstance(raw_to, list):
        recipients = ", ".join(str(x) for x in raw_to) or "-"
    else:
        recipients = str(raw_to or "-")
    card = str(post.get("card") or "")
    attempt = int(post.get("attempt") or 0)
    scope = ""
    if card and attempt:
        scope = f"({card} #{attempt})"
    elif card:
        scope = f"({card})"
    at = str(post.get("at") or "")
    tone = "err" if kind in ("blocked", "stall") else ("warn" if kind == "question" else "cyan")
    header = "  ".join(part for part in (
        style.bold(f"#{seq}"),
        style.tone(tone, f"[{kind}]"),
        f"{sender} -> {recipients}",
        scope,
        style.dim(at) if at else "",
    ) if part)
    lines = [header]
    body = str(post.get("body") or "").strip()
    if body:
        for body_line in body.splitlines():
            lines.append(f"    {body_line}")
    if post.get("ref") is not None:
        lines.append(f"    {style.dim('ref: #' + str(post['ref']))}")
    evidence = post.get("evidence") or []
    if evidence:
        lines.append(f"    {style.dim('evidence: ' + ', '.join(str(e) for e in evidence))}")
    if post.get("expects"):
        lines.append(f"    {style.dim('expects: ' + json.dumps(post['expects'], ensure_ascii=False))}")
    return lines


def _format_board_read(payload: dict, style: Style) -> str:
    rid = payload.get("run_id", "?")
    posts = payload.get("posts") or []
    lines = [style.heading(f"Board {rid}") + f"  {style.dim(f'({len(posts)} post(s))')}"]
    if not posts:
        lines.append(f"  {style.dim('(no matching posts)')}")
        return "\n".join(lines)
    for post in posts:
        lines.extend(_format_post_block(post, style))
    return "\n".join(lines)


def _format_board_status(payload: dict, style: Style) -> str:
    rid = payload.get("run_id", "?")
    ended = bool(payload.get("ended"))
    state_badge = style.dim("ended") if ended else style.green(f"{ICON_OK} live")
    lines = [style.heading(f"Board Status ({rid})") + f"  {state_badge}"]
    lines.extend(_render_kv_section("Summary", [
        ("posts", payload.get("posts", 0)),
        ("last_seq", payload.get("last_seq", 0)),
        ("members", payload.get("members", 0)),
        ("unanswered_questions", payload.get("unanswered_questions", 0)),
    ], style=style))
    roster = payload.get("roster") or []
    lines.append(style.bold("Roster"))
    lines.append(_format_roster_table(roster, style=style))
    return "\n".join(lines)


def _format_board_post(kind: str, payload: dict, style: Style) -> str:
    rid = payload.get("run_id", "?")
    post = payload.get("post") or {}
    seq = post.get("seq", "?")
    if kind == "board_answer":
        ref = post.get("ref", "?")
        return style.green(
            f"{ICON_OK} Answered #{ref} with #{seq} on {rid} (from {post.get('from', '?')})"
        )
    to_list = ", ".join(str(x) for x in (post.get("to") or ())) or "-"
    card = f" [{post['card']}]" if post.get("card") else ""
    return style.green(
        f"{ICON_OK} Posted #{seq} [{post.get('kind', 'note')}] {post.get('from', '?')} -> {to_list}{card} on {rid}"
    )


def _format_swarm_tiers(payload: dict, style: Style) -> str:
    default_tier = str(payload.get("default_tier") or "default")
    tiers = payload.get("tiers") or {}
    return "\n".join([
        style.heading(f"Swarm Tiers (default: {default_tier})"),
        _format_tiers_table(tiers, default_tier, style=style),
    ])


def _format_swarm_status(payload: dict, style: Style) -> str:
    rid = payload.get("run_id", "?")
    board = payload.get("board") or {}
    swarm = payload.get("swarm") or {}
    tiers = payload.get("tiers") or {}
    members = payload.get("members") or []
    running = payload.get("running_cards") or []
    default_tier = str(swarm.get("default_tier") or "default")

    lines = [style.heading(f"Swarm Status ({rid})")]
    lines.extend(_render_kv_section("Seats & Swarm", [
        ("enabled", swarm.get("enabled", False)),
        ("coordinator", f"{swarm.get('coordinator', 'code')} (effort: {swarm.get('coordinator_effort') or '-'})"),
        ("supervisor", f"{swarm.get('supervisor', 'config')} (effort: {swarm.get('supervisor_effort') or '-'})"),
        ("default_tier", default_tier),
        ("running_cards", ", ".join(running) or "(none)"),
        ("board", (
            f"posts={board.get('posts', 0)}, last_seq={board.get('last_seq', 0)}, "
            f"unanswered={board.get('unanswered_questions', 0)}, ended={board.get('ended', False)}"
        )),
    ], style=style))
    lines.append(style.bold("Roster"))
    lines.append(_format_roster_table(members, style=style))
    lines.append(style.bold("Tiers"))
    lines.append(_format_tiers_table(tiers, default_tier, style=style))
    return "\n".join(lines)


def _format_swarm_overview(payload: dict, style: Style) -> str:
    swarm = payload.get("swarm") or {}
    tiers = payload.get("tiers") or {}
    default_tier = str(payload.get("default_tier") or swarm.get("default_tier") or "default")
    lines = [style.heading("Swarm Configuration") + f"  {style.dim('(no active run)')}"]
    lines.extend(_render_kv_section("Seats", [
        ("enabled", swarm.get("enabled", False)),
        ("coordinator", f"{swarm.get('coordinator', 'code')} (effort: {swarm.get('coordinator_effort') or '-'})"),
        ("supervisor", f"{swarm.get('supervisor', 'config')} (effort: {swarm.get('supervisor_effort') or '-'})"),
        ("default_tier", default_tier),
        ("max_members", swarm.get("max_members", 24)),
        ("stall_after_s", swarm.get("stall_after_s", 300.0)),
    ], style=style))
    lines.append(style.bold("Tiers"))
    lines.append(_format_tiers_table(tiers, default_tier, style=style))
    lines.extend(_render_next([
        "openfarm swarm status --run <ID>  (inspect a past or active run)",
        "openfarm swarm tiers",
        "openfarm run --cards 3",
    ], style=style))
    return "\n".join(lines)


def _format_swarm_action(kind: str, payload: dict, style: Style) -> str:
    rid = payload.get("run_id", "?")
    if kind == "swarm_join":
        m = payload.get("member") or {}
        return style.green(
            f"{ICON_OK} Joined seat '{m.get('role', '?')}' as '{m.get('name', '?')}' "
            f"({m.get('owner_kind', 'human')}) on {rid}"
        )
    if kind == "swarm_start":
        card = payload.get("card", "?")
        team = payload.get("team") or {}
        lines = [style.green(f"{ICON_OK} Started {card} on {rid}")]
        if team:
            rows = [
                [
                    role,
                    m.get("name", ""),
                    m.get("tier") or "-",
                    m.get("effort") or "-",
                    m.get("model") or "-",
                ]
                for role, m in team.items()
            ]
            lines.append(format_table(["ROLE", "MEMBER", "TIER", "EFFORT", "MODEL"], rows, style=style))
        return "\n".join(lines)
    if kind == "swarm_set":
        m = payload.get("member") or {}
        msg = f"{ICON_OK} Updated {m.get('name', '?')}: tier={m.get('tier') or '-'}, effort={m.get('effort') or '-'}"
        if payload.get("reason"):
            msg += f" ({payload['reason']})"
        return style.green(msg)
    if kind == "swarm_hint":
        post = payload.get("post") or {}
        to = ", ".join(str(x) for x in (post.get("to") or ())) or "-"
        return style.green(f"{ICON_OK} Sent hint #{post.get('seq', '?')} to {to} on {rid}")
    if kind == "swarm_block":
        post = payload.get("post") or {}
        return style.red(
            f"{ICON_FAIL} Blocked {payload.get('card', '?')} on {rid} (#{post.get('seq', '?')}): {payload.get('reason', '')}"
        )
    # swarm_remove
    m = payload.get("member") or {}
    return style.yellow(f"{ICON_RETURN} Removed {m.get('name', '?')} (state: {m.get('state', 'removed')})")


def _format_init(payload: dict, style: Style) -> str:
    proj = payload.get("project", "?")
    lines = [style.green(f"{ICON_OK} Initialized Openfarm project '{proj}'")]
    key_present = bool(payload.get("key_file_present"))
    key_badge = style.green(f"{ICON_OK} present") if key_present else style.yellow(f"{ICON_DOT} missing (create before run)")
    lines.extend(_render_kv_section("Setup", [
        ("repo", payload.get("repo", "")),
        ("crate_name", payload.get("crate_name", "")),
        ("base_branch", payload.get("base_branch", "main")),
        ("profile", f"{payload.get('profile', 'active')} ({payload.get('micromanager_adapter', '')} / {payload.get('micromanager_auth', '')})"),
        ("mutation", payload.get("mutation", "off")),
        ("state_dir", payload.get("state_dir", "")),
        ("key_file", f"{payload.get('key_file', '')}  [{key_badge}]"),
        ("created_files", f"{len(payload.get('created_files') or ())} file(s)"),
    ], style=style))
    lines.extend(_render_next(list(payload.get("next_steps") or ()), style=style, numbered=True))
    return "\n".join(lines)


def _format_adopt(payload: dict, style: Style) -> str:
    if payload.get("error"):
        return style.red(f"{ICON_FAIL} Adopt refused: {payload['error']}")
    lang = payload.get("language", "?")
    lines = [style.green(f"{ICON_OK} Adopted repository ({lang})")]
    written = payload.get("written") or []
    if written:
        lines.append(style.bold("Files Written"))
        for path in written:
            lines.append(f"  {style.green(ICON_OK)} {path}")
    if "suite_ran" in payload:
        proposed = payload.get("known_failing_proposed") or []
        lines.extend(_render_kv_section("Baseline Suite", [
            ("suite_ran", payload.get("suite_ran", False)),
            ("known_failing_proposed", ", ".join(proposed) if proposed else "none"),
        ], style=style))
        for note in payload.get("suite_notes") or ():
            lines.append(f"  {style.yellow(ICON_DOT)} {note}")
    if payload.get("next"):
        lines.extend(_render_next([str(payload["next"])], style=style))
    return "\n".join(lines)


def _format_install_skill(payload: dict, style: Style) -> str:
    dry = bool(payload.get("dry_run"))
    badge = f" {style.magenta('[DRY RUN]')}" if dry else ""
    lines = [style.green(f"{ICON_OK} Installed Openfarm skills") + badge]
    lines.extend(_render_kv_section("Bundle", [
        ("target", payload.get("target", "all")),
        ("assets_root", payload.get("assets_root", "")),
        ("bin_shim", payload.get("bin_shim") or "(not requested)"),
    ], style=style))
    installed = payload.get("installed") or {}
    if installed:
        rows = []
        for runtime, parts in installed.items():
            p = parts if isinstance(parts, dict) else {}
            rows.append([
                runtime,
                str(len(p.get("skills") or ())),
                str(len(p.get("commands") or ())),
                str(len(p.get("plugin") or ())),
            ])
        lines.append(format_table(
            ["RUNTIME", "SKILLS", "COMMANDS", "PLUGIN"],
            rows,
            style=style,
            right_align={1, 2, 3},
        ))
    return "\n".join(lines)


def _check_line(ok: bool, text: str, style: Style) -> str:
    icon = style.green(ICON_OK) if ok else style.red(ICON_FAIL)
    return f"  {icon} {text}"


_ACP_PROVIDER_LABELS = {
    "claude": "Claude",
    "antigravity": "Antigravity",
    "codex": "Codex",
}


def _acp_held(value: object) -> str:
    """One tool policy's hold, from the status form (a word) or a probe's hold."""
    if isinstance(value, dict):
        return str(value.get("held_by") or ("refused" if value.get("refused") else "?"))
    return str(value or "?")


def _acp_login_place(info: dict) -> str:
    """Where a provider's login was looked for, and whether it was there."""
    provider = str(info.get("provider") or "")
    where = str(info.get("credentials_from") or "HOME")
    where = "$HOME" if where == "HOME" else where
    found = bool(info.get("credentials_file_present"))
    text = f"{where} ({'found' if found else 'not found'})"
    if info.get("oauth_token_env"):
        text += ", CLAUDE_CODE_OAUTH_TOKEN set"
    if info.get("api_key_env") and provider == "antigravity":
        text += ", API key in the environment"
    return text


def _format_acp_children(payload: dict, style: Style) -> list[str]:
    """How each provider's child is held, and where its login was found."""
    providers = payload.get("providers")
    if not isinstance(providers, dict) or not providers:
        return []
    results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    top = payload.get("confinement") if isinstance(payload.get("confinement"), dict) else {}
    rows = []
    refusals: list[str] = []
    for name, info in providers.items():
        if not isinstance(info, dict):
            continue
        held = info.get("confinement") if isinstance(info.get("confinement"), dict) else {}
        probed = (results.get(name) or {}).get("confinement") if isinstance(
            results.get(name), dict) else None
        if isinstance(probed, dict) and probed.get("none") is not None:
            held = probed
        elif isinstance(top.get("none"), dict) and len(providers) == 1:
            # A single probe's payload carries the hold the probe itself used.
            held = top
        cells = [name, _acp_login_place(info)]
        for policy in ("none", "read-only"):
            word = _acp_held(held.get(policy)) if held else "?"
            cells.append(style.red(word) if word == "refused" else
                         style.yellow(word) if word == "nothing" else word)
            why = held.get(f"{policy}_refused") if held else None
            if not why and isinstance(held.get(policy), dict):
                why = held[policy].get("refused")
            if why:
                refusals.append(f"{name}, tool policy {policy}: {why}")
        cells.append(str(held.get("transport") or info.get("resolved_transport") or "missing"))
        rows.append(cells)
    if not rows:
        return []
    lines = ["", style.bold("Provider Children")]
    lines.append(format_table(["PROVIDER", "LOGIN FOUND IN", "TOOLS NONE", "READ-ONLY",
                               "TRANSPORT"], rows, style=style))
    for line in refusals:
        lines.append(f"  {style.red(ICON_FAIL)} {line}")
    if top.get("setting") or top.get("bwrap") or top.get("mode"):
        pairs: list[tuple[str, object]] = [
            ("setting", f'{top.get("setting", "[micromanager] confine")} = '
                        f'"{top.get("mode", "auto")}"')]
        if isinstance(top.get("bwrap"), dict):
            bwrap = top["bwrap"]
            badge = (style.green(f"{ICON_OK} usable") if bwrap.get("usable")
                     else style.yellow(f"{ICON_DOT} not usable"))
            pairs.append(("bwrap", f"{badge} ({bwrap.get('detail', 'unknown')})"))
        if top.get("read_roots"):
            pairs.append(("read_roots", ", ".join(str(r) for r in top["read_roots"])))
        lines.extend(_render_kv_section("Confinement", pairs, style=style))
        for note in top.get("notes") or ():
            lines.append(f"  {style.yellow(ICON_DOT)} {note}")
    return lines


def _format_acp(kind: str, payload: dict, style: Style) -> str:
    action = payload.get("action") or (kind[4:] if kind.startswith("acp_") else "status")
    raw_prov = str(payload.get("provider") or "").strip().lower()
    if not raw_prov or raw_prov == "all":
        display_prov = str(payload.get("primary_provider") or "claude").strip().lower()
    else:
        display_prov = raw_prov
    label = _ACP_PROVIDER_LABELS.get(display_prov, "Claude")
    titles = {
        "status": f"Frontier {label} ACP Status",
        "login": f"Frontier {label} OAuth Login",
        "configure": f"Frontier {label} ACP Configured",
        "probe": f"Frontier {label} ACP Probe",
    }
    lines = [style.heading(titles.get(action, f"Frontier {label} ACP"))]

    oauth_ready = bool(payload.get("oauth_ready"))
    transport = str(payload.get("resolved_transport") or "missing")
    creds_present = bool(payload.get("credentials_file_present"))
    cli_auth = payload.get("cli_auth") or {}
    logged_in = bool(cli_auth.get("loggedIn"))
    env_tok = bool(payload.get("oauth_token_env"))

    lines.append(style.bold("Checks"))
    lines.append(_check_line(
        oauth_ready,
        f"OAuth readiness: {'ready' if oauth_ready else 'not logged in'} "
        f"(cli_logged_in={logged_in}, credentials_file={creds_present}, env_token={env_tok})",
        style,
    ))
    if display_prov == "antigravity":
        tr_detail = (
            f"(agy={payload.get('agy_cli') or 'missing'}, "
            f"acp={payload.get('antigravity_acp') or 'missing'})"
        )
    elif display_prov == "codex":
        tr_detail = (
            f"(codex={payload.get('codex_cli') or 'missing'}, "
            f"acp={payload.get('codex_acp') or 'missing'})"
        )
    else:
        tr_detail = (
            f"(claude={payload.get('claude_cli') or 'missing'}, "
            f"acp={payload.get('claude_agent_acp') or 'missing'})"
        )
    lines.append(_check_line(
        transport != "missing",
        f"ACP transport: {transport} {tr_detail}",
        style,
    ))

    if action == "login" and payload.get("login_command"):
        lines.append(_check_line(True, f"Login command: {' '.join(payload['login_command'])}", style))
    if action == "configure" and payload.get("config_path"):
        lines.append(_check_line(True, f"Updated config: {payload['config_path']}", style))
    if action == "probe":
        if isinstance(payload.get("results"), dict):
            for pname, pres in payload["results"].items():
                if not isinstance(pres, dict):
                    continue
                if pres.get("ok"):
                    methods = ", ".join(
                        str(m.get("id", m)) if isinstance(m, dict) else str(m)
                        for m in (pres.get("auth_methods") or ())
                    ) or "none"
                    lines.append(_check_line(
                        True,
                        f"{pname}: protocol v{pres.get('protocol_version', '?')} (auth_methods: {methods})",
                        style,
                    ))
                else:
                    lines.append(_check_line(
                        False,
                        f"{pname}: {pres.get('error', 'probe failed')}",
                        style,
                    ))
        else:
            methods = ", ".join(str(m.get("id", m)) if isinstance(m, dict) else str(m)
                                for m in (payload.get("auth_methods") or ())) or "none"
            lines.append(_check_line(
                True,
                f"Handshake protocol v{payload.get('protocol_version', '?')} (auth_methods: {methods})",
                style,
            ))

    providers = payload.get("providers")
    if isinstance(providers, dict) and len(providers) > 1:
        rows = []
        for pname, pinfo in providers.items():
            if not isinstance(pinfo, dict):
                continue
            rows.append([
                pname,
                str(pinfo.get("resolved_transport") or "missing"),
                "ready" if pinfo.get("oauth_ready") else "not logged in",
                str(pinfo.get("cli_path") or "missing"),
            ])
        if rows:
            lines.append("")
            lines.append(style.bold("Providers"))
            lines.append(format_table(["PROVIDER", "TRANSPORT", "OAUTH", "CLI"], rows, style=style))

    if action in ("status", "probe"):
        lines.extend(_format_acp_children(payload, style))

    frontier = payload.get("repo_frontier")
    if isinstance(frontier, dict):
        lines.extend(_render_kv_section("Repo Frontier", [
            ("adapter", f"{frontier.get('adapter', '')} (mode={frontier.get('mode', '')}, enabled={frontier.get('enabled', False)})"),
            ("auth", frontier.get("auth", "")),
            ("binary", frontier.get("binary", "")),
            ("effort", frontier.get("effort", "")),
            ("tool_policy", frontier.get("tool_policy", "")),
        ], style=style))

    workers = payload.get("repo_workers")
    if isinstance(workers, dict):
        kp = bool(workers.get("key_file_present"))
        kbadge = style.green(f"{ICON_OK} present") if kp else style.yellow(f"{ICON_DOT} missing")
        lines.extend(_render_kv_section("Repo Workers", [
            ("adapter", workers.get("adapter", "")),
            ("endpoint", workers.get("endpoint", "")),
            ("key_file", f"{workers.get('key_file') or '(unset)'}  [{kbadge}]"),
        ], style=style))

    tips: list[str] = []
    if payload.get("hint"):
        tips.append(str(payload["hint"]))
    if not oauth_ready:
        tips.append(
            "openfarm acp login"
            if display_prov == "claude"
            else f"openfarm acp login --provider {display_prov}"
        )
    if isinstance(workers, dict) and workers.get("key_file") and not workers.get("key_file_present"):
        tips.append(f"Create worker key file at {workers['key_file']} (chmod 600)")
    if not tips and action != "probe":
        tips.append(
            "openfarm acp probe"
            if display_prov == "claude"
            else f"openfarm acp probe --provider {display_prov}"
        )
    elif not tips:
        tips.append("openfarm check <CARD>")
    lines.extend(_render_next(tips, style=style, numbered=True))
    return "\n".join(lines)


def _format_bench_list(payload: dict, style: Style) -> str:
    tasks = payload.get("tasks") or []
    lines = [style.heading(f"Bench Tasks ({len(tasks)})")]
    if not tasks:
        lines.append(f"  {style.dim('(no bench tasks found)')}")
        return "\n".join(lines)
    rows = [[t.get("id", ""), t.get("language", ""), t.get("category", "")] for t in tasks]
    lines.append(format_table(["ID", "LANGUAGE", "CATEGORY"], rows, style=style))
    lines.extend(_render_next([
        "openfarm bench run --dry-run --task <ID>",
    ], style=style))
    return "\n".join(lines)


def _format_drive(payload: dict, style: Style) -> str:
    if payload.get("refused"):
        reason = payload.get("reason", "refused")
        lines = [style.red(f"{ICON_FAIL} Drive refused: {reason}")]
        if payload.get("code"):
            lines.append(format_kv([("code", payload["code"])], style=style))
        lines.extend(_render_next(["openfarm status"], style=style))
        return "\n".join(lines)

    run_id = payload.get("run_id", "?")
    agent = payload.get("agent", "?")
    seat = payload.get("seat", "both")
    ok = bool(payload.get("ok", True))
    badge = style.green(f"{ICON_OK} ok") if ok else style.red(f"{ICON_FAIL} failed")
    lines = [style.heading(f"Driver Run {run_id}") + f"  [{agent} / {seat}]  {badge}"]
    tokens_txt = (
        f"{_fmt_int(payload.get('driver_tokens_in', 0))} in / "
        f"{_fmt_int(payload.get('driver_tokens_out', 0))} out"
    )
    if payload.get("driver_tokens_estimated"):
        tokens_txt += " (estimated)"
    pairs: list[tuple[str, object]] = [
        ("agent", agent),
        ("seat", seat),
        ("driver_turns", payload.get("driver_turns", 0)),
        ("driver_tokens", tokens_txt),
    ]
    if "driver_spend" in payload:
        pairs.append(("driver_spend", _fmt_usd(
            payload.get("driver_spend", 0.0),
            measured=bool(payload.get("driver_spend_measured", False)),
        )))
    if payload.get("run_spend") is not None:
        pairs.append(("run_spend", _fmt_usd(payload["run_spend"]) + " (driver included)"))
    elif payload.get("driver_counted_in_run") is False:
        pairs.append(("run_spend", "not counted: the run belongs to another process"))
    if isinstance(payload.get("run_tokens"), dict):
        from .budget import describe_split
        split = payload["run_tokens"]
        pairs.append(("run_tokens", describe_split(split) + " (driver included"
                      + (", driver tokens estimated)" if split.get("estimated") else ")")))
    for part, vals in (payload.get("driver_by_part") or {}).items():
        part_txt = (
            f"{_fmt_int(vals.get('tokens_in', 0))} in / {_fmt_int(vals.get('tokens_out', 0))} out, "
            f"{vals.get('turns', 0)} turn(s)"
        )
        if vals.get("estimated"):
            part_txt += " (estimated)"
        pairs.append((f"  {part}", part_txt))
    pairs.extend([
        ("actions_taken", len(payload.get("actions_taken") or ())),
        ("answers_sent", len(payload.get("answers_sent") or ())),
    ])
    if payload.get("timeouts"):
        pairs.append((
            "timed_out",
            f"{len(payload['timeouts'])} (on_external_timeout = "
            f"{payload.get('on_external_timeout', 'fallback')})",
        ))
    if payload.get("driver_errors"):
        pairs.append(("failed_turns", len(payload["driver_errors"])))
    if payload.get("spend_stopped"):
        pairs.append(("stopped", payload["spend_stopped"]))
    lines.extend(_render_kv_section("Driver", pairs, style=style))

    outcomes = payload.get("outcomes") or []
    lines.append(style.bold(f"Outcomes ({len(outcomes)})"))
    if outcomes:
        rows = [_outcome_row(o, style) for o in outcomes]
        lines.append(format_table(
            ["CARD", "STATE", "STOP CODE", "ATTEMPTS", "DETAIL"],
            rows,
            style=style,
            right_align={3},
        ))
    else:
        lines.append(f"  {style.dim('(no card outcomes)')}")

    reviews = payload.get("reviews") or []
    if reviews:
        lines.append(style.bold(f"Wave Reviews ({len(reviews)})"))
        def _rev_note(r: dict) -> str:
            if r.get("not_reviewed_reason"):
                return f"not reviewed: {r['not_reviewed_reason']}"
            if r.get("not_recorded_reason"):
                return f"{r.get('note') or ''} (not recorded: {r['not_recorded_reason']})".strip()
            return r.get("note") or "-"

        rev_rows = [
            [
                _outcome_card(r),
                format_state(r.get("verdict") or "not reviewed", style=style),
                _fmt_int(r.get("review_tokens", 0)) + ("~" if r.get("review_tokens_estimated") else ""),
                "yes" if r.get("recorded") else (
                    "kept existing" if r.get("kept_existing") else "no"
                ),
                _truncate(_rev_note(r), 72),
            ]
            for r in reviews
        ]
        lines.append(format_table(
            ["CARD", "VERDICT", "TOKENS", "RECORDED", "NOTE"],
            rev_rows,
            style=style,
            right_align={2},
        ))

    for title, items, fmt in (
        ("Timed Out (no usable driver answer)", payload.get("timeouts") or [],
         lambda t: f"{t.get('card') or '-'} {t.get('op') or t.get('event') or ''} "
                   f"#{t.get('seq')}: {t.get('policy')}; {t.get('reason')}"),
        ("Failed Driver Turns", payload.get("driver_errors") or [],
         lambda e: f"turn {e.get('turn')}: {e.get('code')}: {e.get('message')}"),
    ):
        if items:
            lines.append(style.bold(f"{title} ({len(items)})"))
            for item in items[:10]:
                lines.append(f"  {style.yellow(ICON_DOT)} {_truncate(fmt(item), 110)}")
            if len(items) > 10:
                lines.append(f"  {style.dim(f'... {len(items) - 10} more (see --json)')}")

    notes = payload.get("notes") or []
    if notes:
        lines.append(style.bold("Notes"))
        for note in notes:
            lines.append(f"  {style.yellow(ICON_DOT)} {note}")
    for problem in payload.get("cleanup_problems") or []:
        lines.append(f"  {style.red(ICON_FAIL)} clean-up: {problem}")

    next_tips: list[str] = []
    in_review = [
        _outcome_card(o)
        for o in outcomes
        if o.get("final_state") in ("in-review", "in_review")
    ]
    if in_review:
        sample = in_review[0]
        next_tips.append(f"openfarm dossier {sample}")
        next_tips.append(f"git merge --no-ff openfarm/{sample}-<slug> && openfarm done {sample}")
    else:
        next_tips.append("openfarm status")
    lines.extend(_render_next(next_tips, style=style))
    return "\n".join(lines)


def _format_fallback(payload: dict, style: Style) -> str:
    if payload.get("error"):
        kind = payload.get("kind") or "error"
        return style.red(f"{ICON_FAIL} {kind}: {payload['error']}")
    pairs: list[tuple[str, object]] = []
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            pairs.append((key, json.dumps(value, ensure_ascii=False)))
        else:
            pairs.append((key, value))
    return format_kv(pairs, indent=0, style=style)


def format_human(kind: str, payload: dict, *, style: Style | None = None) -> str:
    """Format a command payload for human readers when `--json` is False.

    A formatter bug must never turn a command that worked into a failure: the
    work is already done and its exit code already decided. So any error here
    falls back to the JSON the command would have printed with `--json`, with a
    one-line note on stderr.
    """
    st = style or Style(sys.stdout)
    try:
        return format_human_strict(kind, payload, style=st)
    except Exception as exc:  # any formatter bug, by design
        with contextlib.suppress(OSError, ValueError):
            print(
                f"openfarm: could not format the {kind or 'command'} result for "
                f"people ({type(exc).__name__}: {exc}); showing it as JSON",
                file=sys.stderr,
            )
        try:
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(payload)


def format_human_strict(kind: str, payload: dict, *, style: Style | None = None) -> str:
    """Like `format_human`, but a formatter error raises. For tests."""
    st = style or Style(sys.stdout)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    if kind == "status":
        return _format_status(payload, st)
    if kind == "check":
        return _format_check(payload, st)
    if kind == "run":
        return _format_run(payload, st)
    if kind == "pause":
        return _format_pause(payload, st)
    if kind == "resume":
        return _format_resume(payload, st)
    if kind == "stop":
        return _format_stop(payload, st)
    if kind == "release":
        return _format_release(payload, st)
    if kind == "done":
        return _format_done(payload, st)
    if kind == "dossier":
        return _format_dossier(payload, st)
    if kind == "verdict":
        return _format_verdict(payload, st)
    if kind == "escape":
        return _format_escape(payload, st)
    if kind == "supervise":
        return _format_supervise(payload, st)
    if kind == "harness_status":
        return _format_harness_status(payload, st)
    if kind == "harness_global_status":
        return _format_harness_global_status(payload, st)
    if kind == "harness_index":
        return str(payload.get("index", "")).rstrip()
    if kind == "harness_show":
        return _format_harness_show(payload, st)
    if kind in ("harness_promote", "harness_demote", "harness_reset"):
        return _format_harness_mutation(kind, payload, st)
    if kind == "board_read":
        return _format_board_read(payload, st)
    if kind == "board_status":
        return _format_board_status(payload, st)
    if kind in ("board_post", "board_answer"):
        return _format_board_post(kind, payload, st)
    if kind == "swarm_tiers":
        return _format_swarm_tiers(payload, st)
    if kind == "swarm_status":
        return _format_swarm_status(payload, st)
    if kind == "swarm_overview":
        return _format_swarm_overview(payload, st)
    if kind in ("swarm_join", "swarm_start", "swarm_set", "swarm_hint", "swarm_block", "swarm_remove"):
        return _format_swarm_action(kind, payload, st)
    if kind == "init":
        return _format_init(payload, st)
    if kind == "adopt":
        return _format_adopt(payload, st)
    if kind == "install_skill":
        return _format_install_skill(payload, st)
    if kind.startswith("acp_"):
        return _format_acp(kind, payload, st)
    if kind == "bench_list":
        return _format_bench_list(payload, st)
    if kind == "drive":
        return _format_drive(payload, st)
    return _format_fallback(payload, st)


def error_hint(kind: str, message: str) -> str:
    """Return a concise recovery hint for common stderr errors, or empty string."""
    msg = str(message or "")
    low = msg.lower()
    if "no config at " in low:
        return "run 'openfarm adopt' in an existing repo, 'openfarm init <name>' for a new project, or pass --config PATH"
    if "no active run holds the lock" in low:
        return "start a run with 'openfarm run', pass '--run <RUN_ID>', or run 'openfarm swarm tiers'"
    if "caller name (--as) is required" in low:
        return "pass '--as <member>' using the name registered with 'openfarm swarm join'"
    if "does not hold the coordinator seat" in low:
        return "run 'openfarm swarm status' to see active seats, or join as coordinator when [swarm] coordinator = \"external\""
    if "not 'external'" in msg:
        return "set [swarm] coordinator = \"external\" (or supervisor = \"external\") in .openfarm/config.toml to claim that seat"
    if "not merged into" in low:
        return "review 'openfarm dossier <CARD>', merge its branch locally (git merge --no-ff <branch>), then run 'openfarm done <CARD>'"
    if "supervise requires --triage-blocked or --prepare-queue" in low:
        return "try 'openfarm supervise --triage-blocked' or 'openfarm supervise --prepare-queue'"
    if "harness reset requires --yes" in low:
        return "pass '--yes' to confirm resetting active harness entries to revision 0"
    if "unknown board member" in low:
        return "check active members with 'openfarm board read --status' or 'openfarm swarm status'"
    if "unknown tier" in low or "efforts [" in low:
        return "run 'openfarm swarm tiers' to inspect configured tiers and allowed efforts"
    if "another run holds the lock" in low:
        return "inspect the live run with 'openfarm status' or 'openfarm swarm status'"
    if "kill switch" in low:
        return "inspect with 'openfarm status' and clear with 'openfarm resume --clear-kill'"
    if "circuit breaker" in low:
        return "run 'openfarm supervise --triage-blocked' then 'openfarm resume --clear-breaker'"
    if "uncommitted changes" in low or "dirty-worktree" in low:
        return "commit or stash working tree changes before retrying"
    if "not a git repository" in low:
        return "run inside a git repository (or pass '--repo <PATH>'), or create one with 'openfarm init <name>'"
    if "is not empty" in low and "openfarm adopt" in low:
        return "pass '--force' to overwrite scaffold files or run 'openfarm adopt' on an existing repo"
    if "outside the repository" in low:
        return "point state_dir and key_file outside the repository (e.g. ~/.openfarm/<name> and ~/.config/openfarm/<name>.key)"
    if kind == "usage error" and "no subcommand" in low:
        return "run 'openfarm --help' to list all 32 commands and workflows"
    return ""
