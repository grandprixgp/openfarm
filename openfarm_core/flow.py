"""The per-card flow: nine steps, five exits to blocked.

Code moves the card at every arrow. A failed gate sends the card back to the
implementer while attempts remain, with only the diff and the failure output.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from pathlib import Path

from .board import normalize_role
from .briefs import (Brief, BriefBook, checker_pass1_brief, checker_pass2_brief,
                     fence, implementer_brief, planner_brief, test_writer_brief)
from .budget import Budget, BudgetExceeded, NightBudget
from .cards import Card
from .edits import (apply_edits, diff_text, line_counts, parse_edits,
                    touched_paths)
from .errors import (AdapterError, GateFailure, Infra, Invalid, Refused,
                     StopCode, Unsupported)
from .gates.checks import (PASS, FAIL, INFRA, GateResult, banned_patterns,
                           diff_within_touches, extra_gate_result, frozen_check,
                           _code_of,
                           combined_status, ignored_or_filtered, mutation_gate,
                           rows_for_test,
                           paths_touched_by_extra_gate, revert_check,
                           suite_green, test_loss)
from .gates.eligibility import (check_eligibility, derive_cargo_unit,
                                file_is_blocked)
from .git import Git, GitRefused
from .globs import matches_any, normalise, resolves_within
from .harness_global import GlobalLibrary, global_dir_for
from .harness_scope import project_markers
from .harness_memory import (GuidanceSelection, HarnessLibrary, _classify_failure,
                             _is_unsafe_path)
from .micromanager import open_micromanager
from .roles import (Plan, Roles, parse_answer_edits, parse_handback, parse_pass1,
                    parse_plan, pass1_answer_form, wants_planner)
from .state import append_jsonl, baseline_lock_for, utc_now, utc_after, write_json
from .swarm import WORKER_ROLES, swarm_set, worker_kind_for_config, worker_member_name

_HARNESS_ISOLATED_STOP_CODES = frozenset({
    StopCode.INFRA,
    StopCode.KILLED,
    StopCode.RELEASED,
    StopCode.PAUSED,
    StopCode.INELIGIBLE,
    StopCode.DIRTY_BASELINE,
    StopCode.STALE_CONFLICT,
    StopCode.CARD_CHANGED,
    StopCode.LEASE_EXPIRED,
    StopCode.HANDED_BACK,
    StopCode.SEAT_SILENT,
})
#: succeeded and the flow should checkpoint and continue to the next step.
_STEP_ADVANCED = "__STEP_ADVANCED__"

#: Builds that a model's edit caused, and which count against the model cap.
MODEL_BUILD_KINDS = ("test", "lint", "build")
#: Fixed gates. Outside the model cap so a healthy card isn't stopped by them.
FIXED_BUILD_KINDS = ("list", "ws-lint", "typecheck", "extra")

#: A pack's purpose, and the runner kind it runs as. The two vocabularies differ:
#: a pack knows `ws-lint`, the runner knows `lint`.
_RUNNER_KIND = {
    "test": "test",
    "list": "list",
    "build": "build",
    "lint": "lint",
    "ws-lint": "lint",
    "typecheck": "typecheck",
    "format": "extra",
    "fmt-check": "extra",
    "mutation": "mutation",
    "extra": "extra",
}


def runner_kind(purpose: str) -> str:
    return _RUNNER_KIND.get(purpose, "extra")


class CardRun:
    """One card's whole run, from claim to in-review or blocked."""

    def __init__(self, card: Card, cfg, state, card_dir: Path, worktree: Path,
                 runner, pack, backend, book: BriefBook, run_id: str,
                 telemetry, budget: Budget | None = None,
                 deps_done: bool = True, conventions: str = "",
                 log=None, micromanager=None,
                 harness_library: HarnessLibrary | None = None,
                 board=None, swarm=None, on_handback=None,
                 on_struggling=None) -> None:
        self.card = card
        self.cfg = cfg
        self.state = state
        self.card_dir = Path(card_dir)
        self.worktree = Path(worktree)
        self.runner = runner
        self.pack = pack
        self.backend = backend
        self.book = book
        self.run_id = run_id
        self.telemetry = telemetry
        self.deps_done = deps_done
        self.conventions = conventions
        self.log = log or (lambda message: None)

        self.budget = budget or Budget.from_config(cfg, card.frontier_estimate)
        self.roles = Roles(cfg, backend, card.id)

        self.git = Git(self.worktree)
        self.card_layout = self.state.card_dir(card.id, run_id)
        self.target_dir = self.card_layout / "target"
        self.target_dir.mkdir(parents=True, exist_ok=True)

        self.board = board
        self.swarm = swarm if swarm is not None else getattr(cfg, "swarm", None)
        #: With the swarm off a run is v0.7's: briefs carry no hand-back text and no
        #: answer is read as a hand-back. A run gets a board only with the swarm on.
        self.swarm_on = board is not None or bool(getattr(self.swarm, "enabled", False))
        self.on_handback = on_handback
        self.on_struggling = on_struggling
        self.team_sets: list[dict] = []
        self.handbacks: list[dict] = []
        self.effort_tokens: dict[str, int] = {}
        self._member_stats: dict[str, dict] = {}
        self._pending_hints: dict[str, list[str]] = {}
        self._role_overrides: dict[str, tuple[str | None, str | None, str | None]] = {}
        self._effort_failures: dict[str, tuple[str, int]] = {}
        self._board_members_ready = False
        #: The coordinator's reason when it stopped this card while it ran.
        self.coordinator_stop = ""
        #: Board seqs of the hints this card posted itself as code.
        self._own_hint_seqs: set[int] = set()

        self._owns_micromanager = micromanager is None
        cancel_fn = lambda: bool(
            self.state.kill_requested(getattr(self.cfg, "kill_switch", None))
        )
        if micromanager is None:
            self.micromanager = open_micromanager(
                cfg, self.card_layout, self.worktree,
                worktree=self.worktree, card_dir=self.card_layout,
                board=board, worker_backend=backend,
                card_budget=self.budget, heartbeat_fn=self._heartbeat,
                cancel_check=cancel_fn,
            )
        else:
            self.micromanager = micromanager
            if hasattr(self.micromanager, "card_dir"):
                self.micromanager.card_dir = self.card_layout
        mm_backend = getattr(self.micromanager, "backend", None)
        if mm_backend is not None:
            if hasattr(mm_backend, "board") and getattr(mm_backend, "board", None) is None:
                mm_backend.board = board
            if hasattr(mm_backend, "budget") and getattr(mm_backend, "budget", None) is None:
                mm_backend.budget = self.budget
            if hasattr(mm_backend, "heartbeat_fn") and getattr(mm_backend, "heartbeat_fn", None) is None:
                mm_backend.heartbeat_fn = self._heartbeat
            if hasattr(mm_backend, "cancel_check") and getattr(mm_backend, "cancel_check", None) is None:
                mm_backend.cancel_check = cancel_fn
        if hasattr(self.runner, "budget") and getattr(self.runner, "budget", None) is None:
            self.runner.budget = self.budget
        if hasattr(self.micromanager, "harness_context"):
            self.micromanager.harness_context = {}
        mm_budget = getattr(self.micromanager, "budget", None)
        if mm_budget is not None and hasattr(mm_budget, "usage_for"):
            self._mm_start = dict(mm_budget.usage_for(self.card.id))
        else:
            self._mm_start = {
                "calls": int(getattr(mm_budget, "calls", 0) or 0),
                "tokens_in": int(getattr(mm_budget, "tokens_in", 0) or 0),
                "tokens_out": int(getattr(mm_budget, "tokens_out", 0) or 0),
                "spend": float(getattr(mm_budget, "spend", 0.0) or 0.0),
                "false_rejects_dismissed": int(
                    getattr(mm_budget, "false_rejects_dismissed", 0) or 0
                ),
            }

        if hasattr(self.book, "allowed_extra_reads") and getattr(self.card, "context_files", None):
            self.book.allowed_extra_reads.update(
                normalise(p) for p in self.card.context_files
            )

        self.lease_id = ""
        self.attempt = 0
        self.phase = "claim"
        self.stop_code: StopCode | None = None
        self.detail = ""
        self.gates: list[GateResult] = []
        self.attempts: list[dict] = []
        self.frozen_tests: dict[str, str] = {}
        self.frozen_hash = ""
        self.frozen_commit = ""
        self.scaffold_commit = ""
        self.step_checkpoint_commit = ""
        self.base_commit = ""
        self.branch = ""
        self.effective_unit = self.card.unit
        self.base_test_list: dict = {"tests": []}
        self.named_passing = 0
        self.diff_hashes: list[str] = []
        self.error_signatures: list[str] = []
        self.best_named_passing = 0
        #: One entry per judged attempt: how many named tests passed then.
        self.named_history: list[int] = []
        self.planner_used = False
        self.plan = None
        self.step_index = 0
        self.step_failures = 0
        self.step_summaries: list[str] = []
        self.depth = 1
        self.counter_summary = {"written": 0, "dropped_uncompiled": 0,
                                "passed": 0, "failed": 0, "not_run": 0}
        self._last_failing_counters: list[dict] = []
        self.mutation_summary = {"killed": 0, "survived": 0, "total": 0,
                                 "capped": False}
        self.known_failing_excluded: list[str] = []
        self.findings_filed: list[str] = []
        self.dossier_path = ""
        self.dossier_payload: dict | None = None
        self.current_diff = ""
        self.started_at = utc_now()
        self.checker_restatement = None
        #: What the checker's first pass said: its form, whether the tests
        #: would catch a bad implementation, what they miss (an opinion).
        self.checker_pass1: dict = {}
        self.checker_adjudication: dict = {}
        self.reviewer_brief = ""
        self.clarified_notes = ""
        self.extra_context_files: list[str] = [
            normalise(p) for p in (getattr(self.card, "context_files", ()) or ())
        ]
        self.coaching_guidance = ""
        self.coach_used_for_stall = False
        self.failed_on_base_by_test: dict[str, str] = {}
        self.head_status_by_test: dict[str, str] = {}
        self.micromanager_summary: dict = {}
        self.phase_a_stub_files: set[str] = set()
        self._tests_proven_in_phase_a = False
        #: Sum of every runner call's own duration, which the pilot measures.
        self.runner_seconds = 0.0
        self.catch_up_result = ""
        self.unformatted: list[str] = []
        self.existing_tests_changed: list[str] = []
        self.ended_at = ""

        harness_cfg = getattr(cfg, "harness", None)
        harness_enabled = bool(getattr(harness_cfg, "enabled", False))
        if harness_library is not None:
            self.harness_library = harness_library
        elif harness_enabled:
            hdir_raw = str(getattr(harness_cfg, "dir", "") or "").strip()
            if hdir_raw:
                hdir_path = Path(hdir_raw).expanduser()
                if not hdir_path.is_absolute():
                    repo_root = getattr(cfg, "repo", None)
                    base = Path(repo_root) if repo_root else self.worktree
                    hdir_path = (base / hdir_path).resolve()
                hdir = hdir_path
            else:
                hdir = self.state.harness()
            self.harness_library = HarnessLibrary(hdir, config=cfg)
        else:
            self.harness_library = None
        # v0.7: the global library. Each card first imports new global lessons
        # and drops retired ones; after the card, scores go back and proven
        # lessons go out (when `[harness] share`).
        self.global_library: GlobalLibrary | None = None
        self.global_summary: dict = {}
        if self.harness_library is not None and harness_enabled:
            try:
                self.global_library = GlobalLibrary(global_dir_for(harness_cfg))
                self.global_summary = {
                    "dir": str(self.global_library.root),
                    "learn_from": self.harness_library.learn_from,
                    **self.harness_library.sync_from_global(self.global_library)}
            except Exception as exc:  # noqa: BLE001 — global learning never stops a card
                self.global_summary = {"error": f"{type(exc).__name__}: {exc}"}
        self.harness_cues_applied: list[str] = []
        self.harness_lessons_added: list[str] = []
        self.harness_revision: int = int(
            self.harness_library.status().get("revision", 0)
            if self.harness_library is not None
            else 0
        )
        self.harness_summary: dict = {}
        self.attempt_records: list[dict] = []

    # -- helpers --------------------------------------------------------------

    def _harness_enabled(self) -> bool:
        harness_cfg = getattr(self.cfg, "harness", None)
        return (
            self.harness_library is not None
            and bool(getattr(harness_cfg, "enabled", False))
        )

    def _harness_query_attrs(self) -> tuple[str, str, tuple[str, ...]]:
        langs = getattr(self.cfg, "languages", None) or ("rust",)
        language = str(langs[0] or "rust")
        kind = str(getattr(self.card, "category", "") or "*")
        raw_tags = (
            self.card.fields.get("tags", ())
            if isinstance(getattr(self.card, "fields", None), dict)
            else getattr(self.card, "tags", ())
        )
        if isinstance(raw_tags, str):
            tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
        else:
            tags = tuple(str(t).strip() for t in (raw_tags or ()) if str(t).strip())
        return language, kind, tags

    def _add_harness_cues(self, cue_ids: list[str]) -> None:
        for cid in cue_ids:
            if cid and cid not in self.harness_cues_applied:
                self.harness_cues_applied.append(cid)

    def _refresh_micromanager_harness_context(
        self,
        role: str = "micromanager",
        *,
        failure_class: str = "",
        role_sel: GuidanceSelection | None = None,
    ) -> None:
        if not self._harness_enabled() or self.harness_library is None:
            return
        language, kind, tags = self._harness_query_attrs()
        mm_sel = self.harness_library.select_guidance(
            language=language,
            kind=kind,
            role="micromanager",
            failure_class=failure_class,
            tags=tags,
        )
        if self.micromanager.enabled:
            mm_specific = [
                str(c.get("id", ""))
                for c in (mm_sel.micromanager_context.get("cues") or ())
                if isinstance(c, dict)
                and (
                    c.get("surface") == "micromanager_context"
                    or c.get("role") == "micromanager"
                )
            ]
            self._add_harness_cues(mm_specific)

        merged_cues: list[dict] = []
        seen: set[str] = set()
        existing_ctx = (
            self.micromanager.harness_context
            if isinstance(getattr(self.micromanager, "harness_context", None), dict)
            else {}
        )
        for ctx_obj in (
            mm_sel.micromanager_context,
            role_sel.micromanager_context if role_sel is not None else {},
            existing_ctx,
        ):
            if not isinstance(ctx_obj, dict):
                continue
            for cue in ctx_obj.get("cues") or ():
                if not isinstance(cue, dict):
                    continue
                cid = str(cue.get("id", ""))
                if cid and cid in seen:
                    continue
                if cid:
                    seen.add(cid)
                merged_cues.append(cue)

        if merged_cues:
            rev = (
                mm_sel.micromanager_context.get("revision")
                or (role_sel.micromanager_context.get("revision") if role_sel else 0)
                or self.harness_revision
            )
            state_str = (
                mm_sel.micromanager_context.get("state")
                or (role_sel.micromanager_context.get("state") if role_sel else "confirmed")
            )
            self.micromanager.harness_context = {
                "revision": int(rev or 0),
                "state": str(state_str or "confirmed"),
                "cues": merged_cues,
            }
        else:
            self.micromanager.harness_context = {}

    def _card_mm_usage(self) -> dict:
        mm_budget = getattr(self.micromanager, "budget", None)
        if mm_budget is None:
            return {
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "spend": 0.0,
                "false_rejects_dismissed": 0,
            }
        start = getattr(self, "_mm_start", None) or {}
        if hasattr(mm_budget, "usage_for"):
            cur = mm_budget.usage_for(self.card.id)
        else:
            cur = {
                "calls": int(getattr(mm_budget, "calls", 0) or 0),
                "tokens_in": int(getattr(mm_budget, "tokens_in", 0) or 0),
                "tokens_out": int(getattr(mm_budget, "tokens_out", 0) or 0),
                "spend": float(getattr(mm_budget, "spend", 0.0) or 0.0),
                "false_rejects_dismissed": int(
                    getattr(mm_budget, "false_rejects_dismissed", 0) or 0
                ),
            }
        return {
            "calls": max(0, int(cur.get("calls", 0)) - int(start.get("calls", 0))),
            "tokens_in": max(
                0, int(cur.get("tokens_in", 0)) - int(start.get("tokens_in", 0))
            ),
            "tokens_out": max(
                0, int(cur.get("tokens_out", 0)) - int(start.get("tokens_out", 0))
            ),
            "spend": round(
                max(0.0, float(cur.get("spend", 0.0)) - float(start.get("spend", 0.0))),
                6,
            ),
            "false_rejects_dismissed": max(
                0,
                int(cur.get("false_rejects_dismissed", 0))
                - int(start.get("false_rejects_dismissed", 0)),
            ),
        }

    def _apply_harness_context_hints(self, role: str, *, failure_class: str = "") -> None:
        if not self._harness_enabled() or self.harness_library is None:
            return
        language, kind, tags = self._harness_query_attrs()
        max_ctx = max(1, int(getattr(self.cfg, "max_context_files", 8) or 8))
        cue_ids, paths = self.harness_library.select_context_hints(
            language=language,
            kind=kind,
            role=role,
            failure_class=failure_class,
            tags=tags,
            max_files=max_ctx,
        )
        applied_any = False
        for raw_p in paths:
            if len(self.extra_context_files) >= max_ctx:
                break
            if _is_unsafe_path(
                raw_p,
                never_touch=getattr(self.cfg, "never_touch", ()) or (),
                forbidden_files=getattr(self.cfg, "forbidden_files", ()) or (),
            ):
                continue
            norm_p = normalise(raw_p)
            if not norm_p:
                continue
            if not resolves_within(self.worktree, norm_p):
                continue
            if not (self.worktree / norm_p).is_file():
                continue
            if hasattr(self.book, "is_hidden") and self.book.is_hidden(norm_p):
                continue
            if norm_p not in self.extra_context_files:
                self.extra_context_files.append(norm_p)
                applied_any = True
            if hasattr(self.book, "allowed_extra_reads"):
                self.book.allowed_extra_reads.add(norm_p)
        if applied_any and cue_ids:
            self._add_harness_cues(cue_ids)

    def _select_harness_guidance(self, role: str, *, failure_class: str = "") -> str:
        if not self._harness_enabled() or self.harness_library is None:
            return ""
        self._apply_harness_context_hints(role, failure_class=failure_class)
        language, kind, tags = self._harness_query_attrs()
        sel = self.harness_library.select_guidance(
            language=language,
            kind=kind,
            role=role,
            failure_class=failure_class,
            tags=tags,
        )
        self._add_harness_cues(list(sel.cue_ids))
        self._refresh_micromanager_harness_context(
            role=role,
            failure_class=failure_class,
            role_sel=sel,
        )
        return sel.brief_text

    def _record_contrast_attempt(
        self,
        *,
        attempt: int,
        phase: str,
        role: str,
        outcome: str,
        error_signature: str = "",
        detail: str = "",
        stop_code: StopCode | None = None,
        gates: list[GateResult] | None = None,
        call=None,
    ) -> None:
        self.attempt_records.append({
            "attempt": int(attempt),
            "phase": phase,
            "role": role,
            "outcome": outcome,
            "status": outcome,
            "error_signature": error_signature,
            "detail": detail[-2000:] if detail else "",
            "stop_code": stop_code.value if stop_code else "",
            "tokens_in": int(getattr(call, "tokens_in", 0) or 0) if call is not None else 0,
            "tokens_out": int(getattr(call, "tokens_out", 0) or 0) if call is not None else 0,
            "spend": float(getattr(call, "spend", 0.0) or 0.0) if call is not None else 0.0,
            "gates": [g.as_dict() for g in (gates or ())],
        })

    def _should_reflect_harness(self) -> bool:
        if not getattr(self.micromanager, "enabled", False):
            return False
        harness_cfg = getattr(self.cfg, "harness", None)
        if not bool(getattr(harness_cfg, "allow_micromanager_reflection", True)):
            return False
        backend = getattr(self.micromanager, "backend", None)
        if backend is None:
            return True
        if hasattr(backend, "script") and isinstance(getattr(backend, "script"), dict):
            script = backend.script
            keys = (
                f"{self.card.id}/reflect_harness",
                f"micromanager/{self.card.id}/reflect_harness",
                "micromanager/reflect_harness",
                "reflect_harness",
                "*",
            )
            return any(k in script for k in keys)
        cmd = getattr(backend, "command", None)
        if isinstance(cmd, (list, tuple)):
            cmd_tokens = [str(tok) for tok in cmd]
            for idx, tok in enumerate(cmd_tokens):
                if tok == "--mode" and idx + 1 < len(cmd_tokens) and cmd_tokens[idx + 1] == "script":
                    return False
                if tok.startswith("--mode=script"):
                    return False
        return True

    def _effective_unit(self) -> str:
        if self.card.unit:
            return self.card.unit
        if not self.effective_unit:
            self.effective_unit = derive_cargo_unit(self.card.touches, self.worktree)
        return self.effective_unit

    def _file_finding(self, entry: dict) -> str | None:
        limit = getattr(self.cfg, "findings_per_card", 0)
        if limit <= 0 or len(self.findings_filed) >= limit:
            return None
        source = getattr(self, "card_source", None)
        if source is None:
            return None
        finding_fn = getattr(source, "finding", None)
        if finding_fn is None:
            return None
        title = str(entry.get("title", "")).strip()
        if not title:
            return None
        body_md = str(entry.get("body", "")).strip()
        try:
            res = finding_fn(self.card.id, title, body_md)
        except (AdapterError, Unsupported, Refused) as exc:
            self.log(f"{self.card.id}: could not file finding: {exc}")
            return None
        fid = str((res or {}).get("id", "") or title)
        self.findings_filed.append(fid)
        return fid

    def _record_gate(self, gate: GateResult) -> GateResult:
        """Write one gate's result where the spec puts it: `gates/<attempt>/<gate>`.

        A gate can run on more than one attempt, and the same gate can run twice in
        one card (the end-of-run scan and the per-attempt scan), so a numbered file
        inside the attempt folder keeps every result rather than overwriting one.
        """
        self.gates.append(gate)
        attempt = max(1, self.attempt)
        gate.attempt = attempt
        folder = self.card_layout / "gates" / str(attempt)
        folder.mkdir(parents=True, exist_ok=True)
        stem = gate.name.replace(":", "-")
        path = folder / f"{stem}.json"
        if path.exists():
            index = 2
            while (folder / f"{stem}-{index}.json").exists():
                index += 1
            path = folder / f"{stem}-{index}.json"
        gate.detail_path = path
        write_json(path, gate.as_dict())
        self.log(f"{self.card.id}: {gate.name} {gate.result}"
                 + (f" — {gate.detail}" if gate.detail else ""))
        if self.board is not None:
            self._ensure_board_members()
            to = [] if gate.result == PASS else self._gate_target(gate.name)
            body = (
                f"{gate.name}: {gate.result}"
                + (f" — {gate.detail}" if gate.detail else "")
            )
            self._post_board(
                author="gates",
                to=to,
                kind="gate",
                attempt=attempt,
                body=body,
                evidence=[str(path)],
                trusted=True,
            )
        return gate

    def _gate_target(self, gate_name: str) -> list[str]:
        if (
            gate_name in ("eligibility", "baseline", "harness-efficiency")
            or gate_name.startswith("micromanager-")
        ):
            return []
        if (
            self.phase in ("test-writer", "prove-tests")
            or gate_name in ("named-tests-exist", "fail-before", "revert-check")
        ):
            return [worker_member_name(self.card.id, "test-writer")]
        return [worker_member_name(self.card.id, "implementer")]

    def _ensure_board_members(self) -> None:
        if self.board is None or self._board_members_ready:
            return
        self.board.ensure()
        if self.board.get_member("gates") is None:
            self.board.upsert_member(
                "gates", kind="code", role="gates", added_by="system"
            )
        if (
            self.board.get_member("coordinator") is None
            and getattr(self.swarm, "coordinator", "code") == "code"
        ):
            self.board.upsert_member(
                "coordinator", kind="code", role="coordinator", added_by="system"
            )
        tiers = getattr(self.cfg, "tiers", {}) or {}
        default_tier_name = str(
            getattr(self.swarm, "default_tier", "") or (next(iter(tiers), "default"))
        )
        kind = worker_kind_for_config(self.cfg)
        for role in WORKER_ROLES:
            mname = worker_member_name(self.card.id, role)
            if self.board.get_member(mname) is None:
                tier_cfg = tiers.get(default_tier_name) or next(iter(tiers.values()), None)
                tier_name = tier_cfg.name if tier_cfg is not None else default_tier_name
                if hasattr(self.cfg, "member_defaults"):
                    # Same rule `swarm start` uses: a role's own [model] entry
                    # wins until the coordinator names a tier.
                    tier_name, model_name = self.cfg.member_defaults(role)
                elif tier_cfg is not None and tier_cfg.model:
                    model_name = tier_cfg.model
                else:
                    rm = (getattr(self.cfg, "role_models", {}) or {}).get(
                        "test_writer" if role == "test-writer" else role
                    )
                    model_name = rm.model if rm is not None else ""
                default_effort = (
                    self.cfg.effort_for_role(role)[0]
                    if hasattr(self.cfg, "effort_for_role")
                    else ""
                )
                self.board.upsert_member(
                    mname,
                    kind=kind,
                    role=role,
                    card=self.card.id,
                    tier=tier_name,
                    model=model_name,
                    effort=default_effort,
                    state="active",
                    added_by="system",
                )
        self._board_members_ready = True

    def _post_board(
        self,
        *,
        author: str,
        to: list[str] | str,
        kind: str,
        body: str,
        attempt: int = 0,
        ref: int | None = None,
        evidence: list[str] | None = None,
        trusted: bool | None = None,
    ) -> dict | None:
        if self.board is None:
            return None
        return self.board.post(
            author=author,
            to=to,
            kind=kind,
            body=body,
            card=self.card.id,
            attempt=attempt,
            ref=ref,
            evidence=evidence,
            trusted=trusted,
        )

    def _may_steer(self, author: str) -> bool:
        """Code, or a name that holds the coordinator's or supervisor's seat now.

        A name typed after `--as` proves nothing by itself: only those may hint a
        worker or answer for the coordinator.
        """
        if self.board is None:
            return False
        member = self.board.get_member(str(author or "").strip())
        if member is None or member.get("state") == "removed":
            return False
        if member.get("kind") == "code":
            return True
        return self.board.seat_of(author) in ("coordinator", "supervisor")

    def _is_steering_post(self, post: dict, member_name: str) -> bool:
        """A post addressed to `member_name` by name, from code or a seat holder.

        A note to `all` or to a role is an announcement for people, not a hint.
        """
        if member_name not in (post.get("to") or ()):
            return False
        if int(post.get("seq", 0) or 0) in self._own_hint_seqs:
            return True
        return self._may_steer(str(post.get("from", "") or ""))

    def _steering_posts(self, member_name: str, kind: str,
                        since: int | None = None) -> list[dict]:
        """Posts of `kind` that may steer `member_name`: unread ones, or after `since`."""
        if self.board is None:
            return []
        return [
            post
            for post in self.board.read(
                as_name=member_name,
                since=since,
                limit=None,
                for_model=False,
                advance_cursor=False,
            )
            if post.get("kind") == kind and self._is_steering_post(post, member_name)
        ]

    def _consume_member_hints(self, member_name: str) -> list[str]:
        if self.board is None:
            return []
        # Reading advances the member's cursor past every unread post, so a note
        # that is skipped here is marked read and not looked at again.
        unread = self.board.read(
            as_name=member_name, limit=None, for_model=True, advance_cursor=True
        )
        return [
            str(post.get("body", "")).strip()
            for post in unread
            if post.get("kind") == "note"
            and str(post.get("body", "")).strip()
            and self._is_steering_post(post, member_name)
        ]

    def _refund_member_attempt(self, board_role: str) -> None:
        if self.board is None:
            return
        member_name = worker_member_name(self.card.id, board_role)
        current = self.board.get_member(member_name)
        if current is None:
            return
        refunded = max(0, int(current.get("attempts", 0) or 0) - 1)
        self.board.update_member(member_name, attempts=refunded)
        if member_name in self._member_stats:
            self._member_stats[member_name]["attempts"] = refunded

    def _apply_handback_set_no_coordinator(
        self,
        member_name: str,
        *,
        tier: str | None,
        effort: str | None,
    ) -> None:
        """Apply a `set` resolution when `coordinator` is not yet registered on board."""
        tiers = getattr(self.cfg, "tiers", {}) or {}
        current = self.board.get_member(member_name) or {}
        new_tier = str(tier) if tier is not None else str(current.get("tier", "") or "")
        new_model = str(current.get("model", "") or "")
        tier_cfg = tiers.get(new_tier)
        if tier is not None:
            if tier_cfg is None:
                raise Refused("unknown-tier", f"unknown tier {tier!r}")
            new_model = tier_cfg.model
        new_effort = (
            str(effort) if effort is not None else str(current.get("effort", "") or "")
        )
        if effort is not None:
            if tier_cfg is None and hasattr(self.cfg, "tier_for_model"):
                tier_cfg = self.cfg.tier_for_model(new_model)
            if tier_cfg is not None and tier_cfg.efforts and new_effort not in tier_cfg.efforts:
                raise Refused(
                    "invalid-effort",
                    f"effort {new_effort!r} is not allowed for tier {tier_cfg.name!r}",
                )
        self.board.update_member(
            member_name, tier=new_tier, model=new_model, effort=new_effort
        )
        self.team_sets.append(
            {
                "member": member_name,
                "tier": new_tier,
                "effort": new_effort,
                "detail": f"set {member_name}: tier={new_tier} effort={new_effort}",
            }
        )

    def _member_effort_key(self, board_role: str) -> str:
        member_name = worker_member_name(self.card.id, board_role)
        if self.board is not None:
            member = self.board.get_member(member_name)
            if member is not None:
                return f"{member.get('tier', '')}:{member.get('effort', '')}"
        ov = self._role_overrides.get(board_role)
        if ov is not None:
            return f"{ov[0] or ''}:{ov[1] or ''}"
        if hasattr(self.cfg, "effort_for_role"):
            eff, _ = self.cfg.effort_for_role(board_role)
            return f":{eff}"
        return ""

    def _apply_coordinator_resolution(
        self,
        resolution,
        *,
        board_role: str,
        member_name: str,
        member_before: dict | None,
        unread_notes_before: int,
        reason: str,
        context_label: str,
        since_seq: int = 0,
    ) -> tuple[str | None, str]:
        """Interpret coordinator hint/set/retry/block action or board mutation."""
        action = ""
        res_dict: dict = {}
        if isinstance(resolution, str):
            action = resolution.strip().lower()
        elif isinstance(resolution, dict):
            res_dict = dict(resolution)
            action = str(res_dict.get("action", "") or "").strip().lower()

        if action == "hint":
            hint_body = str(res_dict.get("body", "") or "").strip()
            if self.board is not None and hint_body:
                # The coordinator may have posted the hint itself; only a note that
                # will reach the worker's brief counts as that.
                existing_notes = [
                    str(post.get("body", "")).strip()
                    for post in self._steering_posts(member_name, "note")
                ]
                if hint_body not in existing_notes:
                    posted = self.board.post(
                        author="coordinator",
                        to=[member_name],
                        kind="note",
                        card=self.card.id,
                        body=hint_body,
                        trusted=True,
                    )
                    self._own_hint_seqs.add(int(posted.get("seq", 0) or 0))
            elif hint_body:
                self._pending_hints.setdefault(board_role, []).append(hint_body)
            return "hint", ""

        if action == "set":
            tier_new = res_dict.get("tier")
            effort_new = res_dict.get("effort")
            if self.board is not None:
                current = self.board.get_member(member_name) or {}
                changed_by_hook = (
                    member_before is not None
                    and (
                        current.get("tier") != member_before.get("tier")
                        or current.get("effort") != member_before.get("effort")
                    )
                )
                if not changed_by_hook and (
                    tier_new is not None or effort_new is not None
                ):
                    coord = self.board.get_member("coordinator")
                    if coord is not None and coord.get("role") == "coordinator":
                        swarm_set(
                            self.board,
                            self.cfg,
                            caller="coordinator",
                            member_name=member_name,
                            tier=tier_new,
                            effort=effort_new,
                            reason=f"{context_label}: {reason[:120]}",
                        )
                    else:
                        self._apply_handback_set_no_coordinator(
                            member_name, tier=tier_new, effort=effort_new
                        )
            elif tier_new is not None or effort_new is not None:
                tiers = getattr(self.cfg, "tiers", {}) or {}
                tier_cfg = tiers.get(str(tier_new)) if tier_new else None
                model_ov = tier_cfg.model if tier_cfg is not None else None
                eff_ov = str(effort_new) if effort_new is not None else None
                ep_ov = (
                    tier_cfg.effort_param
                    if tier_cfg is not None
                    else (
                        self.cfg.tier_for_model(model_ov or "").effort_param
                        if hasattr(self.cfg, "tier_for_model") and eff_ov
                        else ("reasoning_effort" if eff_ov else None)
                    )
                )
                self._role_overrides[board_role] = (model_ov, eff_ov, ep_ov)
            return "set", ""

        if action == "retry":
            return "retry", ""

        if action == "block":
            return "block", str(res_dict.get("reason", "") or reason)

        if not action and self.board is not None:
            # No answer from the callback: look at what was written while it
            # waited, counting only what code or a seat holder wrote.
            current = self.board.get_member(member_name) or {}
            unread_notes_after = len(self._steering_posts(member_name, "note"))
            tier_or_effort_changed = (
                member_before is not None
                and (
                    current.get("tier") != member_before.get("tier")
                    or current.get("effort") != member_before.get("effort")
                )
            )
            set_posted = any(
                str(post.get("body", "") or "").startswith("set ")
                for post in self._steering_posts(member_name, "action", since=since_seq)
            )
            if unread_notes_after > unread_notes_before:
                return "hint", ""
            if tier_or_effort_changed and set_posted:
                return "set", ""

        return None, str(res_dict.get("reason", "") or reason)

    def _maybe_handle_struggling(
        self,
        *,
        role: str,
        attempt_no: int,
        detail: str,
        diff_text: str = "",
    ) -> bool:
        """Track failures at the worker's current effort and escalate via coordinator."""
        board_role = (
            "test-writer" if role == "test_writer" else normalize_role(role)
        )
        eff_key = self._member_effort_key(board_role)
        prev_key, prev_count = self._effort_failures.get(board_role, ("", 0))
        count = 1 if eff_key != prev_key else prev_count + 1
        self._effort_failures[board_role] = (eff_key, count)

        escalate_after = int(
            getattr(self.swarm, "escalate_after", 2)
            if self.swarm is not None
            else 2
        )
        attempts_left = (
            max(0, self.budget.test_writer_attempt_cap - self.budget.test_writer_attempts)
            if role == "test_writer"
            else self.budget.implementer_attempts_left()
        )
        if count < max(1, escalate_after) or attempts_left <= 0:
            return False
        if self.on_struggling is None:
            return False

        member_name = worker_member_name(self.card.id, board_role)
        member_before = (
            self.board.get_member(member_name) if self.board is not None else None
        )
        unread_notes_before = 0
        post_rec = None
        if self.board is not None:
            unread_notes_before = len(self._steering_posts(member_name, "note"))
            post_rec = self._post_board(
                author="gates",
                to=["coordinator"],
                kind="note",
                attempt=attempt_no,
                body=(
                    f"struggling {member_name} ({eff_key or 'default'}, "
                    f"{count} failure(s)): {detail[:300]}"
                ),
                trusted=True,
            )

        tier_cur = str((member_before or {}).get("tier", "") or "")
        effort_cur = str((member_before or {}).get("effort", "") or "")
        self._check_stop_requested()
        cancel_fn = lambda: bool(
            self.state.kill_requested(getattr(self.cfg, "kill_switch", None))
        )
        resolution = self.on_struggling(
            {
                "card_id": self.card.id,
                "role": role,
                "board_role": board_role,
                "member": member_name,
                "attempt": attempt_no,
                "failed_at_effort": count,
                "tier": tier_cur,
                "effort": effort_cur,
                "detail": detail,
                "diff": diff_text,
                "board": self.board,
                "post": post_rec,
                "budget": self.budget,
                "heartbeat_fn": self._heartbeat,
                "cancel_check": cancel_fn,
                "micromanager": self.micromanager,
            }
        )
        outcome, block_reason = self._apply_coordinator_resolution(
            resolution,
            board_role=board_role,
            member_name=member_name,
            member_before=member_before,
            unread_notes_before=unread_notes_before,
            reason=detail[:300],
            context_label="struggling",
            since_seq=int((post_rec or {}).get("seq", 0) or 0),
        )
        if outcome in ("hint", "set", "retry"):
            self.diff_hashes.clear()
            self.error_signatures.clear()
            self.named_history.clear()
            self._effort_failures[board_role] = (
                self._member_effort_key(board_role),
                0,
            )
            return True
        if outcome == "block":
            raise GateFailure(StopCode.HANDED_BACK, block_reason)
        return False

    def _handle_handback(
        self,
        *,
        role: str,
        call,
        attempt_no: int,
        call_seq: int,
        reason: str,
    ) -> str | None:
        """Handle a worker hand-back answer (`<<<HANDBACK ... >>>`).

        Returns None to refund & retry, a failure detail string when
        `handbacks_per_card` is exceeded (counting it as a failed attempt), or
        raises `GateFailure(HANDED_BACK)` when blocked.
        """
        board_role = (
            "checker"
            if role in ("checker_pass1", "checker_pass2")
            else ("test-writer" if role == "test_writer" else role)
        )
        member_name = worker_member_name(self.card.id, board_role)
        max_handbacks = int(
            getattr(self.swarm, "handbacks_per_card", 2)
            if self.swarm is not None
            else 2
        )
        hb_index = len(self.handbacks) + 1
        within_limit = hb_index <= max_handbacks
        hb_entry: dict = {
            "role": role,
            "member": member_name,
            "attempt": attempt_no,
            "call_seq": call_seq,
            "reason": reason,
            "refunded": within_limit,
            "outcome": "limit-exceeded" if not within_limit else "pending",
        }
        self.handbacks.append(hb_entry)
        self.log(f"{self.card.id}: {role} handed back (#{hb_index}): {reason}")

        member_before = (
            self.board.get_member(member_name) if self.board is not None else None
        )
        unread_notes_before = 0
        post_rec = None
        if self.board is not None:
            unread_notes_before = len(self._steering_posts(member_name, "note"))
            post_rec = self._post_board(
                author=member_name,
                to=["coordinator"],
                kind="handback",
                attempt=call_seq,
                body=(
                    reason
                    if within_limit
                    else f"hand-back limit ({max_handbacks}) exceeded: {reason}"
                ),
            )

        if not within_limit:
            detail = f"hand-back limit ({max_handbacks}) exceeded: {reason}"
            att_phase = "test-writer" if role == "test_writer" else (
                "implement" if role == "implementer" else role
            )
            self._record_attempt(
                att_phase,
                "failed-gates",
                StopCode.HANDED_BACK,
                role=role,
                attempt=attempt_no,
                detail=detail,
            )
            self._record_contrast_attempt(
                attempt=attempt_no,
                phase=role,
                role=role,
                outcome="fail",
                error_signature="handback-limit",
                detail=detail,
                stop_code=StopCode.HANDED_BACK,
                call=call,
            )
            return detail

        # Refund attempt before checking coordinator resolution so blocking leaves 0 attempts.
        if role == "test_writer":
            self.budget.test_writer_attempts = max(
                0, self.budget.test_writer_attempts - 1
            )
        elif role == "implementer":
            self.budget.implementer_attempts = max(
                0, self.budget.implementer_attempts - 1
            )
        if role != "checker_pass2":
            # `_call_board_role` counts no member attempt for pass 2, so there is
            # nothing to give back.
            self._refund_member_attempt(board_role)

        resolution = None
        if self.on_handback is not None:
            try:
                self._check_stop_requested()
            except GateFailure:
                hb_entry["outcome"] = "stopped"
                raise
            cancel_fn = lambda: bool(
                self.state.kill_requested(getattr(self.cfg, "kill_switch", None))
            )
            resolution = self.on_handback(
                {
                    "card_id": self.card.id,
                    "role": role,
                    "board_role": board_role,
                    "member": member_name,
                    "attempt": attempt_no,
                    "call_seq": call_seq,
                    "handback_index": hb_index,
                    "reason": reason,
                    "board": self.board,
                    "post": post_rec,
                    "budget": self.budget,
                    "heartbeat_fn": self._heartbeat,
                    "cancel_check": cancel_fn,
                    "micromanager": self.micromanager,
                }
            )

        outcome, block_reason = self._apply_coordinator_resolution(
            resolution,
            board_role=board_role,
            member_name=member_name,
            member_before=member_before,
            unread_notes_before=unread_notes_before,
            reason=reason,
            context_label="handback",
            since_seq=int((post_rec or {}).get("seq", 0) or 0),
        )
        if outcome in ("hint", "set", "retry"):
            hb_entry["outcome"] = outcome
            return None

        hb_entry["outcome"] = "blocked"
        raise GateFailure(StopCode.HANDED_BACK, block_reason)

    def _handback_reason(self, call, *, json_form: bool = False) -> str | None:
        """The hand-back reason in a worker's answer, or None.

        Only with the swarm on. With it off no brief invites a hand-back, so the
        answer goes down v0.7's path like any other answer without usable edits.
        A cut-off answer is never a hand-back. `json_form` also reads the planner's
        JSON-mode shape, `{"handback": "<reason>"}`.
        """
        if not self.swarm_on or call.cut_off:
            return None
        reason = parse_handback(call.content)
        if reason is None and json_form:
            reason = _json_handback(call.content)
        return reason

    def _call_role_with_handbacks(
        self,
        role: str,
        brief: Brief,
        *,
        first_attempt: int,
        response_name: str,
        response_format: str | None = None,
        json_handback: bool = False,
        unavailable: tuple = (),
    ):
        """Call the planner or a checker pass, giving a hand-back the spec's handling.

        These roles have no attempt budget of their own. A hand-back goes through
        `_handle_handback` like the implementer's: a hint or set within
        `handbacks_per_card` sends the brief again (`_call_board_role` adds the
        hint), a block raises `handed-back`. Returns `(answer, None)` for an answer
        that is not a hand-back, `(hand-back answer, detail)` once a hand-back went
        past the limit, and `(None, None)` when the call raised one of `unavailable`.
        """
        call_seq = 0
        while True:
            call_seq += 1
            try:
                call = self._call_board_role(
                    role,
                    brief,
                    attempt=first_attempt + call_seq - 1,
                    response_format=response_format,
                    response_path=self.card_layout / "responses" / (
                        f"{response_name}.json" if call_seq == 1
                        else f"{response_name}-{call_seq}.json"
                    ),
                )
            except unavailable:
                return None, None
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
            reason = self._handback_reason(call, json_form=json_handback)
            if reason is None:
                return call, None
            # Every pass through here records a hand-back, so the loop ends at the
            # limit at the latest.
            limit_err = self._handle_handback(
                role=role,
                call=call,
                attempt_no=1,
                call_seq=call_seq,
                reason=reason,
            )
            if limit_err is not None:
                return call, limit_err

    def _member_call_settings(self, role: str, member: dict) -> tuple[str, str | None, str, str]:
        tiers = getattr(self.cfg, "tiers", {}) or {}
        tier_name = str(member.get("tier", "") or getattr(self.swarm, "default_tier", "") or "")
        tier_cfg = tiers.get(tier_name)
        model_override = str(
            member.get("model", "") or (tier_cfg.model if tier_cfg is not None else "") or ""
        ) or None
        effort = str(member.get("effort", "") or "")
        if not effort:
            effort_param = ""
        elif tier_cfg is not None:
            effort_param = tier_cfg.effort_param
        elif hasattr(self.cfg, "tier_for_model"):
            effort_param = self.cfg.tier_for_model(model_override or "").effort_param
        else:
            effort_param = "reasoning_effort"
        return tier_name, model_override, effort, effort_param

    def _call_board_role(
        self,
        role: str,
        brief: Brief,
        *,
        attempt: int = 1,
        response_format: str | None = None,
        brief_path: Path | None = None,
        brief_is_json: bool = False,
        response_path: Path | None = None,
    ):
        if self.board is None:
            board_role = (
                "checker" if role in ("checker_pass1", "checker_pass2") else normalize_role(role)
            )
            pending = self._pending_hints.pop(board_role, [])
            effective_brief = brief
            if pending:
                hint_block = "\n\nCoordinator hint (data):\n" + fence(
                    "\n\n".join(pending), "coordinator hint"
                )
                effective_brief = Brief(brief.role, brief.body + hint_block)
            if brief_path is not None and brief_is_json:
                write_json(brief_path, {"brief": effective_brief.body})
            ov_model, ov_eff, ov_ep = self._role_overrides.get(
                board_role, (None, None, None)
            )
            call = self.roles.call(
                role,
                effective_brief,
                attempt=attempt,
                response_format=response_format,
                model=ov_model,
                effort=ov_eff,
                effort_param=ov_ep,
            )
            if response_path is not None:
                write_json(response_path, call.as_dict())
            return call

        self._check_stop_requested()
        self._ensure_board_members()
        board_role = (
            "checker" if role in ("checker_pass1", "checker_pass2") else normalize_role(role)
        )
        member_name = worker_member_name(self.card.id, board_role)
        hints = self._consume_member_hints(member_name)
        effective_brief = brief
        if hints:
            hint_block = "\n\nCoordinator hint (data):\n" + fence(
                "\n\n".join(hints), "coordinator hint"
            )
            effective_brief = Brief(brief.role, brief.body + hint_block)

        target_brief = brief_path or (
            self.card_layout / "briefs" / f"{attempt}-{role.replace('_', '-')}.md"
        )
        if brief_is_json:
            write_json(target_brief, {"brief": effective_brief.body})
        else:
            target_brief.parent.mkdir(parents=True, exist_ok=True)
            target_brief.write_text(effective_brief.body, encoding="utf-8")

        member = self.board.get_member(member_name) or {}
        tier_name, model_override, effort_val, effort_param = self._member_call_settings(
            role, member
        )
        self._post_board(
            author="gates",
            to=[member_name],
            kind="brief",
            attempt=attempt,
            body=f"{board_role} brief (attempt {attempt})",
            evidence=[str(target_brief)],
            trusted=True,
        )

        call = self.roles.call(
            role,
            effective_brief,
            attempt=attempt,
            response_format=response_format,
            model=model_override,
            effort=effort_val,
            effort_param=effort_param,
        )

        target_resp = response_path or (
            self.card_layout / "responses" / f"{attempt}-{role.replace('_', '-')}.json"
        )
        write_json(target_resp, call.as_dict())

        tok_in = int(getattr(call, "tokens_in", 0) or 0)
        tok_out = int(getattr(call, "tokens_out", 0) or 0)
        eff_key = effort_val or "none"
        self.effort_tokens[eff_key] = self.effort_tokens.get(eff_key, 0) + tok_in + tok_out

        current = self.board.get_member(member_name) or member
        new_in = int(current.get("tokens_in", 0) or 0) + tok_in
        new_out = int(current.get("tokens_out", 0) or 0) + tok_out
        new_calls = int(current.get("calls", 0) or 0) + 1
        new_attempts = int(current.get("attempts", 0) or 0) + (
            0 if role == "checker_pass2" else 1
        )
        by_eff = dict(current.get("tokens_by_effort") or {})
        by_eff[eff_key] = int(by_eff.get(eff_key, 0) or 0) + tok_in + tok_out
        self.board.update_member(
            member_name,
            tokens_in=new_in,
            tokens_out=new_out,
            tokens=new_in + new_out,
            calls=new_calls,
            attempts=new_attempts,
            tokens_by_effort=by_eff,
        )
        self._member_stats[member_name] = {
            "member": member_name,
            "role": board_role,
            "tier": tier_name,
            "effort": effort_val,
            "model": model_override or "",
            "attempts": new_attempts,
            "calls": new_calls,
            "tokens_in": new_in,
            "tokens_out": new_out,
            "tokens": new_in + new_out,
        }

        self._post_board(
            author=member_name,
            to=["gates"],
            kind="result",
            attempt=attempt,
            body=f"{board_role} answered ({len(call.content)} chars, {tok_in} in / {tok_out} out)",
            evidence=[str(target_resp)],
        )
        return call

    def team_summary(self) -> list[dict]:
        """Per-member team summary for the dossier and telemetry."""
        if self.board is None:
            return []
        rows: list[dict] = []
        for role in WORKER_ROLES:
            mname = worker_member_name(self.card.id, role)
            member = self.board.get_member(mname)
            if member is None:
                continue
            tok_in = int(member.get("tokens_in", 0) or 0)
            tok_out = int(member.get("tokens_out", 0) or 0)
            rows.append({
                "member": mname,
                "role": role,
                "tier": str(member.get("tier", "") or ""),
                "effort": str(member.get("effort", "") or ""),
                "model": str(member.get("model", "") or ""),
                "attempts": int(member.get("attempts", 0) or 0),
                "calls": int(member.get("calls", 0) or 0),
                "tokens_in": tok_in,
                "tokens_out": tok_out,
                "tokens": tok_in + tok_out,
            })
        return rows

    def board_sets_summary(self) -> list[dict]:
        """Set actions targeting this card's team on the board."""
        if self.board is None or not self.board.board_dir.exists():
            return list(self.team_sets)
        team_names = {worker_member_name(self.card.id, role) for role in WORKER_ROLES}
        posts = self.board.read(as_name="*", since=0, limit=None, for_model=False)
        sets: list[dict] = []
        for post in posts:
            if post.get("kind") != "action":
                continue
            body = str(post.get("body", "") or "")
            if not body.startswith("set "):
                continue
            recipients = set(post.get("to") or ())
            if post.get("card") == self.card.id or (recipients & team_names):
                sets.append({
                    "seq": post.get("seq"),
                    "at": post.get("at", ""),
                    "from": post.get("from", ""),
                    "to": list(post.get("to") or ()),
                    "detail": body,
                })
        return sets or list(self.team_sets)

    def _current_step(self) -> dict | None:
        """The planner step the implementer is on, or None when there is no plan.

        The planner exists to split phase B into sequential steps, each checkpointed
        by the harness. A step is done when its named tests pass; until then the
        implementer sees that step alone, which is the point of splitting a card the
        model would otherwise lose track of.
        """
        if self.plan is None or not self.plan.steps:
            return None
        if self.step_index >= len(self.plan.steps):
            return self.plan.steps[-1]
        return self.plan.steps[self.step_index]

    def _advance_step(self, parsed: dict) -> None:
        """Move to the next planner step once its tests pass."""
        if self.plan is None or not self.plan.steps:
            return
        step = self._current_step()
        if not step:
            return
        wanted = step.get("tests") or []
        if wanted and all(_status_of(parsed, name) == "ok" for name in wanted):
            self.step_index = min(self.step_index + 1, len(self.plan.steps))
            self.step_failures = 0
            self.log(f"{self.card.id}: planner step done, now on "
                     f"{self.step_index + 1} of {len(self.plan.steps)}")

    def _check_kill_switch(self) -> None:
        """A kill switch stops the card after the call or build in progress.

        The spec: "The live run stops after the model call or build in progress,
        archives partial work, and moves every card it holds to blocked with stop
        code `killed`." The check runs between a gate's steps, never inside one, so
        nothing is torn mid-build.
        """
        if self.state.kill_requested(self.cfg.kill_switch):
            raise GateFailure(
                StopCode.KILLED,
                "the kill switch was set while this card was in progress")
        self._check_stop_requested()

    def _check_stop_requested(self) -> None:
        """A coordinator's stop request ends the card before its next call or build.

        `swarm block` (or a tier coordinator's `block`) on a running card only posts
        to the board, so the card asks before each model call and wherever the kill
        switch is checked. A stopped card ends blocked with `refused` through the
        normal finish path. With the swarm off there is no board and nothing to ask.
        """
        if self.board is None:
            return
        reason = self.board.stop_requested(self.card.id)
        if reason:
            self.coordinator_stop = reason
            raise GateFailure(StopCode.REFUSED, f"stopped by coordinator: {reason}")

    def _run(self, kind: str, argv: list[str], counted: str = "model",
             unit: str = "", timeout_s: float | None = None):
        self._check_kill_switch()
        resolved_unit = unit or self._effective_unit()
        env = self.cfg.runner_env.get(resolved_unit, {})
        timeout = timeout_s
        if timeout is None:
            left = self.budget.wall_clock_left_s()
            timeout = max(30.0, left)
        result = self.runner.exec(
            kind=kind, argv=argv, cwd=self.worktree,
            target_dir=self.target_dir, unit=resolved_unit,
            env=env, timeout_s=timeout)
        self.runner_seconds += getattr(result, "duration_s", 0.0) or 0.0
        if counted == "model":
            self.budget.note_model_build()
        elif counted == "fixed":
            self.budget.note_fixed_build()
        elif counted == "mutation":
            self.budget.note_mutation_build()
        self.budget.check_clock()
        return result

    def _output(self, result) -> tuple[str, str]:
        """stdout and stderr, whether they came inline or in files."""
        out, err = result.stdout, result.stderr
        if hasattr(self.runner, "read_output") and not (out or err):
            return self.runner.read_output(result)
        return out, err

    def _test_writer_paths(self) -> list[str]:
        """What the test writer may write: the named test files, plus touches.

        A test file the card names is answerable even when it isn't in `touches`,
        because the done text named it. Production files stay additions-only, and
        `tests-locked` still stops the implementer from touching either.
        """
        paths = list(self.card.touches)
        for test in self.card.tests:
            if test.file and test.file not in paths:
                paths.append(normalise(test.file))
        for path in self.card.test_edits:
            if path not in paths:
                paths.append(normalise(path))
        return paths

    def _stub_inserter(self):
        """`append_stub` for this language, or None where it isn't offered."""
        if not hasattr(self.pack, "append_stub"):
            return None

        def insert(target: Path, test_name: str, body: str) -> None:
            self.pack.append_stub(target, test_name, body)

        return insert

    def _freeze(self) -> None:
        """Hash the test files and stub lines after phase A."""
        import json
        hashes = {}
        for path in self._test_paths():
            full = self.worktree / path
            if full.exists():
                hashes[path] = hashlib.sha256(full.read_bytes()).hexdigest()
        self.frozen_tests = hashes
        self.frozen_hash = _tests_hash(hashes)
        write_json(self.card_layout / "frozen" / "tests.sha256",
                   {"hash": self.frozen_hash, "files": hashes})

    def _test_paths(self) -> list[str]:
        """Files in the card's test surface: declared test files, touches."""
        declared = [t.file for t in self.card.tests if t.file]
        return list(dict.fromkeys([normalise(p) for p in declared] +
                                  [p for p in self.card.touches
                                   if self._is_test_path(p)]))

    def _is_test_path(self, path: str) -> bool:
        return self.pack.classify_paths([normalise(path)]).get(
            normalise(path)) == "test"

    def _changed(self) -> list[str]:
        """Changes in the working tree right now, against HEAD.

        This is what a per-attempt gate wants: after `reset --hard` it is exactly
        the attempt's own edits, committed or not.
        """
        return self.git.changed_files()

    def _changed_since_base(self) -> list[str]:
        """Every path the card has touched, from the base commit to now.

        Once the implementer commits, `git diff HEAD` is empty, so the end gates and
        the dossier have to look back to the base to see the card's whole diff.
        """
        start = self.base_commit or "HEAD"
        names = self.git.diff_name_only(start)
        for path in self.git.untracked_files():
            if path not in names:
                names.append(path)
        return sorted(names)

    def _commit(self, message: str) -> str:
        self.git.stage_all()
        if not self.git.has_staged_changes():
            return self.git.head()
        return self.git.commit(message)

    # -- the flow -------------------------------------------------------------

    def run(self, claim=None) -> dict:
        """Drive the whole card. Returns the card record."""
        try:
            self._step_claim(claim)
            self._step_worktree()
            self._step_baseline()
            self._step_plan()
            self._step_phase_a()
            self._step_prove_tests()
            bounce_diff = ""
            bounce_failure = ""
            bounce_counter = ""
            checker_bounced = False
            pre_review_bounced = False
            while True:
                self._step_phase_b(
                    initial_diff=bounce_diff,
                    initial_failure=bounce_failure,
                    initial_counter_tests=bounce_counter,
                )
                bounce = self._step_end_gates(
                    allow_checker_bounce=(
                        not checker_bounced
                        and self.card.category != "test-backfill"
                        and self.budget.implementer_attempts_left() > 0
                    ),
                    allow_pre_review_bounce=(
                        not pre_review_bounced
                        and self.card.category != "test-backfill"
                        and self.budget.implementer_attempts_left() > 0
                    ),
                )
                if bounce is None:
                    break
                kind, bounce_diff, bounce_failure, bounce_counter = bounce
                if kind == "checker":
                    checker_bounced = True
                else:
                    pre_review_bounced = True
            self._step_catch_up()
            final_state = "in-review"
            self.stop_code = None
        except (Refused, GitRefused) as exc:
            # A git refusal is the same class of thing as an adapter refusal: a rule
            # said no. Letting it escape would leave the card claimed and no run
            # record written.
            self.stop_code = StopCode.REFUSED
            self.detail = getattr(exc, "message", None) or str(exc)
            final_state = "blocked"
        except BudgetExceeded as exc:
            self.stop_code = exc.code
            self.detail = exc.detail
            final_state = "blocked"
        except GateFailure as exc:
            self.stop_code = exc.code
            self.detail = exc.detail
            final_state = "blocked"
        except Infra as exc:
            self.stop_code = StopCode.INFRA
            self.detail = exc.message
            final_state = "blocked"
        except AdapterError as exc:
            self.stop_code = StopCode.INFRA
            self.detail = exc.message
            final_state = "blocked"
        finally:
            self.budget.stop_clock()
            self.ended_at = utc_now()

        if final_state == "blocked" and self.stop_code in (
                StopCode.INELIGIBLE,):
            final_state = "released"

        try:
            return self._finish(final_state)
        finally:
            if self._owns_micromanager:
                try:
                    self.micromanager.close()
                except Exception:
                    pass

    # -- step 1: claim and eligibility ----------------------------------------

    def _step_claim(self, claim) -> None:
        self.phase = "claim"
        if claim is not None:
            self.lease_id = claim.get("lease_id", "")
        self.card_hash_at_claim = self.card.hash()
        write_json(self.card_layout / "card.json",
                   {"card": self.card.as_dict(), "hash": self.card_hash_at_claim,
                    "read_at": utc_now()})
        if self.board is not None:
            self._ensure_board_members()
            self._post_board(
                author="gates",
                to=[],
                kind="note",
                body=f"claimed {self.card.id} (hash {self.card_hash_at_claim[:12]})",
                trusted=True,
            )

        eligibility = check_eligibility(self.card, self.cfg,
                                        deps_done=self.deps_done,
                                        known_failing=self.cfg.known_failing,
                                        worktree=self.worktree)
        gate = GateResult("eligibility", PASS if eligibility.ok else FAIL,
                          "" if eligibility.ok else _summarise(eligibility.failures),
                          eligibility.as_dict())
        self._record_gate(gate)
        if not eligibility.ok:
            raise GateFailure(StopCode.INELIGIBLE,
                              _summarise(eligibility.failures))

    # -- step 2: branch, worktree, target -------------------------------------

    def _step_worktree(self) -> None:
        self.phase = "worktree"
        self.base_commit = self.git.head(self._base_ref())
        self.branch = f"openfarm/{self.card.id}-{self.card.slug()}"
        if self.git.current_branch() == self.branch:
            self.git.checkout(self.base_commit, detach=True)
        self.git.create_branch(self.branch, self.base_commit)
        # The worktree was made by the controller and entered here; one target
        # folder per card, never shared between branches.
        self.git.checkout(self.branch, detach=False)
        # The pack decides from this run's config (as eligibility did), not from
        # the base checkout's config file, which `--config` may not be.
        if hasattr(self.pack, "apply_settings"):
            self.pack.apply_settings(self.cfg)
        if hasattr(self.pack, "configure"):
            self.pack.configure(self.worktree)
        self.effective_unit = self.card.unit or derive_cargo_unit(
            self.card.touches, self.worktree
        )

    def _pack_gate_evidence(self, gate: str) -> dict:
        """What the pack says a gate's record should carry (see `Pack.gate_evidence`)."""
        evidence = getattr(self.pack, "gate_evidence", None)
        if evidence is None:
            return {}
        try:
            found = evidence(gate)
        except Exception:  # noqa: BLE001 - a pack's extra words never stop a gate
            return {}
        return dict(found) if isinstance(found, dict) else {}

    def _prepare_checkout(self, path: Path) -> None:
        """A checkout gets what the pack gives the card's own worktree.

        A TypeScript project's linked `node_modules`, for one: without it a run in
        a revert-check or catch-up checkout could not import what the card's runs
        import. Also used to set the card's worktree up again after a
        counter-test removed part of it.
        """
        prepare = getattr(self.pack, "prepare_worktree", None)
        if prepare is None:
            return
        note = prepare(path)
        if note:
            self.log(f"{self.card.id}: {note}")

    def _base_ref(self) -> str:
        name = f"refs/heads/{self.cfg.base_branch}"
        return name if self.git.ref_exists(name) else "HEAD"

    def _named_test_files(self) -> dict[str, str]:
        """The card's file for each named test, so rows of a same-named test in
        another file are not counted for it."""
        return {test.name: test.file for test in self.card.tests if test.file}

    def _row_file(self):
        return getattr(self.pack, "row_file", None)

    # -- step 3: baseline green ----------------------------------------------

    def _step_baseline(self) -> None:
        self.phase = "baseline"
        unit = self._effective_unit()
        lock_cm = (
            baseline_lock_for(self._cache_file(unit))
            if self.cfg.baseline_cache
            else contextlib.nullcontext()
        )
        with lock_cm:
            cached = None
            if self.cfg.baseline_cache:
                cached = self._read_baseline_cache(unit)
            if cached is not None:
                gate = GateResult("baseline", PASS, "cached: two runs agreed",
                                  {"cached": True})
                self._record_gate(gate)
                self.base_test_list = cached.get("list", {"tests": []})
                return

            runs = []
            for index in (1, 2):
                result = self._run(runner_kind("test"),
                                   self.pack.commands("test", unit=unit)["argv"],
                                   counted="fixed")
                out, err = self._output(result)
                parsed = self.pack.parse_tests(out, err)
                runs.append((result, parsed))

            first, second = runs
            sg_first = suite_green(first[1], self.cfg.known_failing,
                                   run_cls=first[0].cls, exit_code=first[0].exit_code)
            sg_second = suite_green(second[1], self.cfg.known_failing,
                                    run_cls=second[0].cls, exit_code=second[0].exit_code)

            # A baseline that does not compile is not green, and not "no failures"
            # either. Check compile errors before INFRA/missing_footers so compile
            # failures stay a card/repo baseline error cleanly.
            compile_broken = (first[1].get("compile_errors")
                              or second[1].get("compile_errors"))
            if compile_broken:
                detail = ("the base does not build: "
                          + str(compile_broken[:3]))
                self._record_gate(GateResult("baseline", FAIL, detail,
                                             {"compile_errors": compile_broken}))
                raise GateFailure(StopCode.DIRTY_BASELINE, detail)

            if (first[0].cls == INFRA or second[0].cls == INFRA
                    or sg_first.result == INFRA or sg_second.result == INFRA):
                self._record_gate(GateResult("baseline", INFRA,
                                             "a baseline run did not finish"))
                raise Infra("baseline-infra",
                            "a baseline run did not finish, so nothing can be judged")

            red_first = _red(parsed=first[1], known=self.cfg.known_failing)
            red_second = _red(parsed=second[1], known=self.cfg.known_failing)
            disagree = [name for name in red_first if name not in red_second]
            disagree += [name for name in red_second if name not in red_first]

            if red_first or red_second or not sg_first.ok or not sg_second.ok:
                if red_first or red_second:
                    detail = ("red on base: "
                              + ", ".join(sorted(set(red_first + red_second))[:10]))
                    if disagree:
                        detail += f"; the two runs disagreed on {len(disagree)} test(s)"
                else:
                    detail = sg_first.detail or sg_second.detail or "baseline failed"
                self._record_gate(GateResult("baseline", FAIL, detail,
                                             {"red": red_first + red_second,
                                              "disagreed": disagree,
                                              "first": sg_first.evidence,
                                              "second": sg_second.evidence}))
                raise GateFailure(StopCode.DIRTY_BASELINE, detail)

            list_result = self._run(runner_kind("list"),
                                    self.pack.commands("list", unit=unit)["argv"],
                                    counted="fixed")
            out, err = self._output(list_result)
            self.base_test_list = self.pack.parse_list(out)
            self._record_gate(GateResult(
                "baseline", PASS,
                f"two runs green and agreed; {len(self.base_test_list['tests'])} "
                f"existing test(s)",
                {"cached": False, "existing_tests": len(self.base_test_list["tests"])}))
            self._write_baseline_cache(unit, self.base_test_list)

    def _cache_file(self, unit: str) -> Path:
        return self.state.cache_path(
            self.base_commit or "unknown", unit or "workspace",
            self.cfg.known_failing_hash(), self.cfg.env_hash(unit))

    def _read_baseline_cache(self, unit: str) -> dict | None:
        from .state import read_json
        path = self._cache_file(unit)
        payload = read_json(path)
        if not isinstance(payload, dict):
            return None
        if payload.get("known_failing_hash") != self.cfg.known_failing_hash():
            return None
        if payload.get("env_hash") != self.cfg.env_hash(unit):
            return None
        return payload

    def _write_baseline_cache(self, unit: str, listing: dict) -> None:
        if not self.cfg.baseline_cache:
            return
        write_json(self._cache_file(unit), {
            "commit": self.base_commit, "unit": unit,
            "known_failing_hash": self.cfg.known_failing_hash(),
            "env_hash": self.cfg.env_hash(unit),
            "agreed": True, "list": listing, "at": utc_now(),
        })

    # -- step 4: planner ------------------------------------------------------

    def _step_plan(self) -> None:
        self.phase = "plan"
        if self.micromanager.enabled:
            sigs = {}
            for path in self.card.touches:
                norm = normalise(path)
                if "*" in norm or "?" in norm:
                    continue
                if resolves_within(self.worktree, norm) and (self.worktree / norm).is_file():
                    try:
                        sigs[norm] = self.book.signatures(norm, self.pack)
                    except (OSError, Refused):
                        pass
            self._apply_harness_context_hints("planner")
            ctx_contents = {}
            max_ctx = getattr(self.cfg, "max_context_files", 6)
            for path in (
                self.extra_context_files or getattr(self.card, "context_files", ()) or ()
            )[:max_ctx]:
                norm = normalise(path)
                if resolves_within(self.worktree, norm) and (self.worktree / norm).is_file():
                    try:
                        ctx_contents[norm] = self.book.guarded_read(norm)
                    except (OSError, Refused):
                        pass
            known_tests = [
                t.get("name", "")
                for t in self.base_test_list.get("tests", [])
                if t.get("name")
            ]
            self._refresh_micromanager_harness_context("planner")
            self._check_stop_requested()
            try:
                decision = self.micromanager.shape_and_plan(
                    self.card, sigs, ctx_contents, known_tests=known_tests, cfg=self.cfg
                )
            except (Refused, Invalid, ValueError, Infra) as exc:
                # Planning is help, not a gate: a supervisor that is slow to answer
                # costs the card its plan, not its run. Other infra still stops it.
                if isinstance(exc, Infra) and exc.code != "acp-timeout":
                    raise
                self._record_gate(GateResult(
                    "micromanager-shape",
                    FAIL if isinstance(exc, Refused) else INFRA,
                    f"shape_and_plan degraded ({type(exc).__name__}): "
                    f"{exc.message if hasattr(exc, 'message') else exc}",
                    {"error": str(exc)},
                ))
                self.log(f"{self.card.id}: micromanager shape_and_plan degraded: {exc}")
                decision = None
            if decision is not None:
                self._record_gate(GateResult(
                    "micromanager-shape",
                    FAIL if (self.micromanager.mode == "active"
                             and decision.verdict in ("ineligible", "split")) else PASS,
                    decision.verdict,
                    decision.as_dict(),
                ))
                for entry in decision.findings:
                    self._file_finding(entry)
                self.clarified_notes = decision.clarified_notes
                for extra_p in decision.extra_context_files:
                    norm_e = normalise(extra_p)
                    if norm_e not in self.extra_context_files:
                        self.extra_context_files.append(norm_e)
                    if hasattr(self.book, "allowed_extra_reads"):
                        self.book.allowed_extra_reads.add(norm_e)
                if self.micromanager.mode == "active":
                    if decision.verdict == "ineligible":
                        raise GateFailure(
                            StopCode.INELIGIBLE,
                            decision.clarified_notes or "micromanager marked card ineligible",
                        )
                    if decision.verdict == "split":
                        raise GateFailure(
                            StopCode.CANNOT_SPLIT,
                            decision.clarified_notes or "micromanager recommended splitting card",
                        )
                    if decision.steps:
                        self.plan = Plan(decision.steps)
                        self.planner_used = True
                        self.step_index = 0
                        self.depth = len(self.plan.steps)
                        write_json(self.card_layout / "plan.json", self.plan.as_dict())
                        return

        if not wants_planner(self.card, getattr(self.cfg, "planner", "auto")):
            self.planner_used = False
            self.depth = 1
            return
        planner_harness = self._select_harness_guidance("planner")
        brief = planner_brief(
            self.card, self.book, harness_guidance=planner_harness,
            handback=self.swarm_on,
        )
        call, limit_err = self._call_role_with_handbacks(
            "planner", brief, first_attempt=1, response_name="planner",
            response_format="json", json_handback=True,
            unavailable=(Infra, Invalid))
        if call is None:
            self.planner_used = False
            self.log(f"{self.card.id}: planner unavailable, running unsplit")
            return
        try:
            if limit_err is not None:
                # Past `handbacks_per_card` a hand-back counts as a failed attempt,
                # and a planner's failed attempt is an unusable plan.
                raise Invalid("planner-handback-limit", limit_err)
            plan = parse_plan(call.content, card=self.card)
        except Invalid as exc:
            if getattr(self.cfg, "planner", "auto") == "on":
                raise GateFailure(StopCode.CANNOT_SPLIT, exc.message) from exc
            self.planner_used = False
            self.depth = 1
            self.log(f"{self.card.id}: planner returned invalid plan ({exc.code}), running unsplit")
            return
        if plan.cannot_split:
            raise GateFailure(StopCode.CANNOT_SPLIT,
                              "the planner could not split without leaving touches")
        self.planner_used = True
        self.plan = plan
        self.step_index = 0
        self.depth = len(plan.steps) or 1
        write_json(self.card_layout / "plan.json", plan.as_dict())

    # -- step 5: phase A, the test writer ------------------------------------

    def _step_phase_a(self) -> None:
        self.phase = "test-writer"
        check_disk = getattr(self.state, "check_disk_space", None)
        if check_disk is not None:
            check_disk(getattr(self.cfg, "min_free_mb", 0))

        self.scaffold_commit = self.base_commit or self.git.head(self._base_ref())
        if not self.card.test_names and self.card.category == "mechanical":
            # A mechanical card is a compiler-guided edit: there is no new test to
            # write, and the existing suite is the judge. Freeze the tests as they
            # are, so the freeze and revert checks still mean what they say.
            self._freeze()
            self.frozen_commit = self._commit(
                f"{self.card.id}: freeze the existing tests")
            self.step_checkpoint_commit = self.frozen_commit
            self._record_gate(GateResult(
                "test-writer", PASS,
                "skipped: a mechanical card names no new tests"))
            return

        if self.micromanager.enabled and (
                self.card.new_symbols or getattr(self.card, "type_changes", False)):
            self._run_scaffold_contract()

        last_failure = ""
        tw_guidance = self.clarified_notes
        # Bounded the same way phase B is: a refused answer gives its attempt back,
        # so the loop is bounded by free sends rather than by the config number,
        # while `free_retries` bounds malformed / non-unique search retries.
        free_sends = self.cfg.test_writer_attempts
        free_retries = self.cfg.test_writer_attempts
        attempt = 0
        call_seq = 0
        while free_sends > 0:
            free_sends -= 1
            attempt += 1
            call_seq += 1
            self.attempt = attempt
            # Every test-writer attempt starts cleanly from the scaffold commit,
            # so a failed or refused attempt cannot leave an edit behind.
            self.git.reset_hard(self.scaffold_commit)
            self.budget.take_test_writer_attempt()
            tw_fc = (
                _classify_failure(failure=last_failure)
                if (attempt > 1 or last_failure)
                else ""
            )
            tw_harness = self._select_harness_guidance(
                "test_writer", failure_class=tw_fc
            )
            brief = test_writer_brief(
                self.card, self.book, self.pack, self.conventions,
                attempt=attempt, previous_failure=last_failure,
                guidance=tw_guidance, context_files=self.extra_context_files,
                harness_guidance=tw_harness, handback=self.swarm_on)
            call = self._call_board_role(
                "test_writer",
                brief,
                attempt=call_seq,
                response_path=self.card_layout / "responses" / f"A{call_seq}-test-writer.json",
            )
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)

            hb_reason = self._handback_reason(call)
            if hb_reason is not None:
                limit_err = self._handle_handback(
                    role="test_writer",
                    call=call,
                    attempt_no=attempt,
                    call_seq=call_seq,
                    reason=hb_reason,
                )
                if limit_err is None:
                    attempt -= 1
                    free_sends += 1
                    continue
                last_failure = limit_err
                if free_sends <= 0:
                    raise GateFailure(StopCode.TESTS_UNPROVABLE, last_failure)
                continue

            try:
                edits = parse_answer_edits(call)
            except Invalid as exc:
                last_failure = exc.message
                self.budget.test_writer_attempts -= 1
                self._record_attempt("test-writer", "refused",
                                     StopCode.TESTS_UNPROVABLE, role="test_writer",
                                     attempt=attempt, detail=last_failure)
                self._record_contrast_attempt(
                    attempt=attempt, phase="test_writer", role="test_writer",
                    outcome="refused", error_signature="parse-edits-invalid",
                    detail=last_failure, stop_code=StopCode.TESTS_UNPROVABLE,
                    call=call,
                )
                free_retries -= 1
                if free_retries <= 0:
                    raise GateFailure(
                        StopCode.TESTS_UNPROVABLE,
                        "the test writer produced no usable edit in "
                        f"{self.cfg.test_writer_attempts} attempts: {last_failure}")
                free_sends += 1
                continue

            # Production files in touches are not the test writer's to rewrite. On a
            # card that declares new symbols it may only append a stub there; on any
            # other card it may not write them at all.
            production = {
                normalise(path) for path in self.card.touches
                if self.pack.classify_paths([path]).get(
                    normalise(path), "") == "production"}
            report = apply_edits(
                self.worktree, edits, self._test_writer_paths(),
                self.cfg.never_touch, self.cfg.forbidden_files,
                additions_only=production if self.card.new_symbols else set(),
                allow_stubs=self.card.new_symbols,
                stub_inserter=self._stub_inserter(),
                production_paths=production,
                forbid_production=not self.card.new_symbols)
            write_json(self.card_layout / "edits" / f"A{attempt}.json",
                       report.as_dict())

            if not report.ok:
                last_failure = _report_detail(report)
                tw_stop = (
                    StopCode.REFUSED
                    if (report.outside_touches or report.not_additions_only)
                    else StopCode.TESTS_UNPROVABLE
                )
                self._record_attempt("test-writer", "refused",
                                     tw_stop, role="test_writer",
                                     attempt=attempt, detail=last_failure)
                self._record_contrast_attempt(
                    attempt=attempt, phase="test_writer", role="test_writer",
                    outcome="refused",
                    error_signature=last_failure[:120] or "edit-refused",
                    detail=last_failure, stop_code=tw_stop, call=call,
                )
                if report.outside_touches or report.not_additions_only:
                    raise GateFailure(
                        StopCode.REFUSED,
                        "the test writer left the allowed paths: "
                        + ", ".join(report.outside_touches or report.not_additions_only))
                retryable = {entry["code"] for entry in report.refused}
                # Asked again with the reason: a search block that did not match,
                # or a stub the pack could not take (malformed, or one that does
                # not fail when called). Nothing of a refused answer is on disk.
                if not retryable <= {"no-unique-match", "non-panicking-stub"}:
                    raise GateFailure(
                        StopCode.REFUSED,
                        "the test writer's answer was refused: "
                        + _report_detail(report))
                self.budget.test_writer_attempts -= 1
                free_retries -= 1
                if free_retries <= 0:
                    if "non-panicking-stub" in retryable:
                        raise GateFailure(
                            StopCode.REFUSED,
                            "the test writer's answer was refused: "
                            + _report_detail(report))
                    raise GateFailure(
                        StopCode.TESTS_UNPROVABLE,
                        "the test writer's search blocks kept failing to match "
                        f"exactly once in {self.cfg.test_writer_attempts} attempts: "
                        f"{last_failure}")
                free_sends += 1
                continue

            try:
                self._prove_current_tests()
            except GateFailure as exc:
                if exc.code != StopCode.TESTS_UNPROVABLE:
                    raise
                self._record_attempt("test-writer", "failed-gates",
                                     StopCode.TESTS_UNPROVABLE, role="test_writer",
                                     attempt=attempt, detail=exc.detail)
                self._record_contrast_attempt(
                    attempt=attempt, phase="test_writer", role="test_writer",
                    outcome="fail", error_signature="tests-unprovable",
                    detail=exc.detail, stop_code=StopCode.TESTS_UNPROVABLE,
                    call=call,
                )
                if free_sends <= 0:
                    raise
                last_failure = exc.detail
                self._maybe_handle_struggling(
                    role="test_writer",
                    attempt_no=attempt,
                    detail=last_failure,
                )
                if self.micromanager.enabled:
                    self._refresh_micromanager_harness_context(
                        "test_writer",
                        failure_class=_classify_failure(failure=last_failure),
                    )
                    self._check_stop_requested()
                    try:
                        advice = self.micromanager.coach(
                            self.card, "test-writer", attempt,
                            self._working_test_texts(), "", last_failure, cfg=self.cfg)
                    except (Refused, Invalid, ValueError, Infra) as mm_exc:
                        if isinstance(mm_exc, Infra) and mm_exc.code != "acp-timeout":
                            raise
                        self._record_gate(GateResult(
                            "micromanager-coach",
                            FAIL if isinstance(mm_exc, Refused) else INFRA,
                            f"coach degraded ({type(mm_exc).__name__}): "
                            f"{mm_exc.message if hasattr(mm_exc, 'message') else mm_exc}",
                            {"error": str(mm_exc)},
                        ))
                        self.log(f"{self.card.id}: micromanager coach degraded: {mm_exc}")
                        continue
                    self._record_gate(GateResult(
                        "micromanager-coach",
                        FAIL if advice.abort else PASS,
                        advice.abort_reason or advice.root_cause or "coaching provided",
                        advice.as_dict(),
                    ))
                    if advice.abort:
                        raise GateFailure(StopCode.TESTS_UNPROVABLE,
                                          advice.abort_reason or exc.detail) from exc
                    tw_guidance = advice.guidance or self.clarified_notes
                continue

            # Freeze, then commit: an attempt starts from a clean tree, so the
            # frozen tests must be in the commit or `reset --hard` would delete them.
            self.phase_a_stub_files = {
                normalise(edit.path) for edit in edits
                if normalise(edit.path) in production
            }
            self._freeze()
            self.frozen_commit = self._commit(f"{self.card.id}: freeze the tests")
            self.step_checkpoint_commit = self.frozen_commit
            self._tests_proven_in_phase_a = True
            self._record_attempt("test-writer", "passed-gates", role="test_writer",
                                 attempt=attempt)
            if self.card.category == "test-backfill":
                self._record_contrast_attempt(
                    attempt=attempt, phase="test_writer", role="test_writer",
                    outcome="pass", call=call,
                )
            return

        raise GateFailure(StopCode.TESTS_UNPROVABLE,
                          "the test writer produced no usable edit in "
                          f"{self.cfg.test_writer_attempts} attempts: {last_failure}")

    def _run_scaffold_contract(self) -> None:
        base_files: dict[str, str] = {}
        sigs: dict[str, str] = {}
        for path in self.card.touches:
            norm = normalise(path)
            if "*" in norm or "?" in norm or self.book.is_hidden(norm):
                continue
            if resolves_within(self.worktree, norm) and (self.worktree / norm).is_file():
                try:
                    base_files[norm] = self.book.guarded_read(norm)
                    sigs[norm] = self.book.signatures(norm, self.pack)
                except (OSError, Refused):
                    pass
        self._check_stop_requested()
        try:
            scaffold_edits = self.micromanager.scaffold_contract(
                self.card, base_files, sigs,
                never_touch=self.cfg.never_touch,
                forbidden=self.cfg.forbidden_files,
            )
        except (Refused, Invalid, ValueError, Infra) as exc:
            if isinstance(exc, Infra) and exc.code != "acp-timeout":
                raise
            self._record_gate(GateResult(
                "micromanager-scaffold",
                FAIL if isinstance(exc, Refused) else INFRA,
                f"scaffold_contract degraded ({type(exc).__name__}): "
                f"{exc.message if hasattr(exc, 'message') else exc}",
                {"error": str(exc)},
            ))
            self.log(f"{self.card.id}: micromanager scaffold_contract degraded: {exc}")
            return
        if not scaffold_edits:
            return
        sc_report = apply_edits(
            self.worktree, scaffold_edits, self.card.touches,
            self.cfg.never_touch, self.cfg.forbidden_files,
        )
        write_json(self.card_layout / "edits" / "A0-scaffold.json",
                   sc_report.as_dict())
        self._record_gate(GateResult(
            "micromanager-scaffold",
            PASS if sc_report.ok else FAIL,
            "scaffold applied" if sc_report.ok else _report_detail(sc_report),
            sc_report.as_dict(),
        ))
        if sc_report.ok:
            self.scaffold_commit = self._commit(
                f"{self.card.id}: scaffold declared contract")
        else:
            self.git.reset_hard(self.scaffold_commit)

    def _working_test_texts(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in self._test_paths():
            full = self.worktree / path
            if full.exists():
                out[path] = full.read_text()
        return out

    # -- step 6: named tests exist, and fail before the code ------------------

    def _step_prove_tests(self) -> None:
        self.phase = "prove"
        if self._tests_proven_in_phase_a:
            return
        self._prove_current_tests()

    def _prove_current_tests(self) -> None:
        self.phase = "prove"
        unit = self._effective_unit()

        list_result = self._run(runner_kind("list"),
                                self.pack.commands("list", unit=unit)["argv"],
                                counted="fixed")
        out, err = self._output(list_result)
        if list_result.cls == INFRA:
            raise Infra(
                "runner-infra",
                (err or out).strip()[-500:] or "test list command failed on infra",
            )
        listing = self.pack.parse_list(out)
        write_json(self.card_layout / "gates" / "named-tests-list.json", listing)

        if not self.card.test_names:
            self._record_gate(GateResult(
                "named-tests-exist", PASS,
                f"skipped: a {self.card.category} card names no new tests"))
            return
        # A parametrized test is listed once per case (`test_param[1-2]`); those
        # rows are the named test's, as they are in every run below.
        listed_rows = _named_rows({"results": listing.get("tests", [])},
                                  self.card.test_names, self._named_test_files(),
                                  self._row_file())
        missing = [name for name in self.card.test_names if not listed_rows[name]]
        if missing:
            load_errors = [str(e) for e in listing.get("errors", []) or []][:5]
            why = ("; could not load: " + " | ".join(load_errors)) if load_errors else ""
            # A build that failed lists nothing: say what the compiler said, so
            # the test writer's next try is not a guess.
            build_errors = ""
            if list_result.cls != PASS and hasattr(self.pack, "build_errors"):
                build_errors = str(self.pack.build_errors(out, err) or "")
            if build_errors:
                why += "; the test build failed: " + build_errors
            self._record_gate(GateResult(
                "named-tests-exist", FAIL,
                "not reported by the test binary: " + ", ".join(missing[:10]) + why,
                {"missing": missing, "load_errors": load_errors,
                 "build_errors": build_errors}))
            raise GateFailure(StopCode.TESTS_UNPROVABLE,
                              "the test binary does not list: "
                              + ", ".join(missing[:10]) + why)
        self._record_gate(GateResult("named-tests-exist", PASS,
                                     f"{len(self.card.test_names)} named test(s) "
                                     f"listed"))

        if self.card.category == "test-backfill":
            # Backfill tests pass on base by design. Mutation is their only
            # blocking proof, run at the end.
            self.failed_on_base_by_test = {
                name: "ok" for name in self.card.test_names
            }
            self._record_gate(GateResult(
                "fail-before", PASS,
                "skipped for test-backfill: the new tests pass on base by design"))
            return

        rerun = self._run(runner_kind("test"),
                          self.pack.commands("test", unit=unit,
                                             tests=self.card.test_names)["argv"], counted="fixed")
        out, err = self._output(rerun)
        parsed = self.pack.parse_tests(out, err)
        named_files = self._named_test_files()
        failures = _named_failures(parsed, self.card.test_names, named_files,
                                   self._row_file())
        named_rows = _named_rows(parsed, self.card.test_names, named_files,
                                 self._row_file())
        compile_errors = parsed.get("compile_errors", [])

        if rerun.cls == INFRA:
            self._record_gate(GateResult(
                "fail-before", INFRA,
                "the fail-before run did not finish", parsed))
            raise Infra("fail-before-infra", "the fail-before run did not finish")

        if compile_errors:
            self._record_gate(GateResult(
                "fail-before", FAIL,
                "on base the frozen tests do not compile: a compile error proves "
                "nothing", {"compile_errors": compile_errors}))
            raise GateFailure(
                StopCode.TESTS_UNPROVABLE,
                "the frozen tests do not compile on base. Declare the new symbols "
                "so the test writer may append stubs, or test existing items.\n"
                + (err or out).strip()[-2000:])

        if parsed.get("missing_footers"):
            if rerun.cls == FAIL and rerun.exit_code < 0 and rerun.exit_code != -9:
                self._record_gate(GateResult(
                    "fail-before", FAIL,
                    f"test binary crashed with signal {-rerun.exit_code} on base",
                    parsed))
                raise GateFailure(
                    StopCode.TESTS_UNPROVABLE,
                    f"test binary crashed with signal {-rerun.exit_code} on base")
            self._record_gate(GateResult(
                "fail-before", INFRA,
                "the fail-before run did not finish", parsed))
            raise Infra("fail-before-infra", "the fail-before run did not finish")

        # A test whose rows disagree (one parameter case or binary failing, another
        # passing) proves nothing about the change: every row must fail on base.
        mixed = [name for name, status in failures.items() if status == "mixed"]
        if mixed:
            self._record_gate(GateResult(
                "fail-before", FAIL,
                "these named tests were reported both failing and not failing on "
                "base, so they prove nothing: " + ", ".join(mixed[:10]),
                {"mixed_on_base": mixed,
                 "rows": {name: named_rows[name] for name in mixed}}))
            raise GateFailure(
                StopCode.TESTS_UNPROVABLE,
                "named tests are not failing in every row on base: "
                + ", ".join(mixed[:10]))

        # `ignored` is not proof: an ignored test never ran on base, so it proves
        # nothing about what the code does.
        wrong = [name for name, status in failures.items()
                 if status != "FAILED"]
        if wrong:
            self._record_gate(GateResult(
                "fail-before", FAIL,
                "these named tests do not fail at runtime on base, so they assert "
                "nothing: " + ", ".join(wrong[:10]),
                {"passing_on_base": wrong,
                 "rows": {name: named_rows[name] for name in wrong}}))
            raise GateFailure(
                StopCode.TESTS_UNPROVABLE,
                "named tests do not fail on base: " + ", ".join(wrong[:10]))

        self.failed_on_base_by_test = dict(failures)
        self._record_gate(GateResult(
            "fail-before", PASS,
            f"{len(self.card.test_names)} named test(s) failed at runtime on base",
            {"failures": failures, "rows": named_rows}))

    # -- step 7: phase B, the implementer ------------------------------------

    def _step_phase_b(self, initial_diff: str = "", initial_failure: str = "",
                      initial_counter_tests: str = "") -> None:
        self.phase = "implement"
        if self.card.category == "test-backfill":
            # A backfill card adds tests to code that already works: there is no
            # production change to make, and its proof is mutation at the end.
            self._record_gate(GateResult(
                "implementer", PASS,
                "skipped for test-backfill: the code already works and mutation is "
                "the proof"))
            return

        last_failure = initial_failure
        last_failed_diff = initial_diff
        counter_tests = initial_counter_tests
        free_calls = self.budget.implementer_attempts_left()
        free_retries = max(1, free_calls)
        step_advances = 0
        max_step_advances = len(self.plan.steps) if self.plan else 0
        attempt_seq = int(self.budget.implementer_attempts)
        call_seq = attempt_seq

        while free_calls > 0:
            uncommitted = self._diff_against_head_before_edits()
            if uncommitted["diff"]:
                last_failed_diff = uncommitted["diff"]
            free_calls -= 1
            self.budget.take_implementer_attempt()
            attempt_seq += 1
            attempt_no = attempt_seq
            call_seq += 1
            self._heartbeat()
            self._begin_attempt(attempt_no)

            showing_diff = bool(last_failed_diff)
            step = self._current_step()
            guidance = self.coaching_guidance or self.clarified_notes
            impl_fc = (
                _classify_failure(
                    self.error_signatures[-1] if self.error_signatures else "",
                    failure=last_failure,
                )
                if (attempt_no > 1 or last_failure or self.error_signatures)
                else ""
            )
            impl_harness = self._select_harness_guidance(
                "implementer", failure_class=impl_fc
            )
            brief = implementer_brief(
                self.card, self.book, self._frozen_texts(),
                attempt=max(2, attempt_no) if showing_diff else 1,
                diff=last_failed_diff,
                failure=last_failure, counter_tests=counter_tests,
                step=step,
                guidance=guidance,
                step_context="\n".join(self.step_summaries),
                context_files=self.extra_context_files,
                harness_guidance=impl_harness,
                handback=self.swarm_on)
            call = self._call_board_role(
                "implementer",
                brief,
                attempt=call_seq,
                brief_path=self.card_layout / "briefs" / f"{call_seq}-implementer.md",
                brief_is_json=True,
                response_path=self.card_layout / "responses" / f"{call_seq}-implementer.json",
            )
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)

            hb_reason = self._handback_reason(call)
            if hb_reason is not None:
                limit_err = self._handle_handback(
                    role="implementer",
                    call=call,
                    attempt_no=attempt_no,
                    call_seq=call_seq,
                    reason=hb_reason,
                )
                if limit_err is None:
                    attempt_seq -= 1
                    free_calls += 1
                    continue
                last_failure = limit_err
                continue

            try:
                edits = parse_answer_edits(call)
            except Invalid as exc:
                last_failure = exc.message
                self.budget.implementer_attempts -= 1
                attempt_seq -= 1
                self._record_attempt("implement", "refused",
                                     role="implementer", attempt=attempt_no,
                                     detail=last_failure)
                self._record_contrast_attempt(
                    attempt=attempt_no, phase="implementer", role="implementer",
                    outcome="refused", error_signature="parse-edits-invalid",
                    detail=last_failure, stop_code=StopCode.REFUSED, call=call,
                )
                free_retries -= 1
                if free_retries <= 0:
                    raise GateFailure(
                        StopCode.ATTEMPTS,
                        f"the implementer produced no usable edit in "
                        f"{self.cfg.implementer_attempts} tries: {last_failure}")
                free_calls += 1
                continue

            report = apply_edits(self.worktree, edits, self.card.touches,
                                 self.cfg.never_touch, self.cfg.forbidden_files)
            write_json(self.card_layout / "edits" / f"{attempt_no}.json",
                       report.as_dict())

            if report.refused:
                if all(e["code"] == "no-unique-match" for e in report.refused):
                    last_failure = _report_detail(report)
                    self.budget.implementer_attempts -= 1
                    attempt_seq -= 1
                    self._record_attempt("implement", "refused",
                                         role="implementer", attempt=attempt_no,
                                         detail=last_failure)
                    self._record_contrast_attempt(
                        attempt=attempt_no, phase="implementer",
                        role="implementer", outcome="refused",
                        error_signature="no-unique-match",
                        detail=last_failure, stop_code=StopCode.REFUSED,
                        call=call,
                    )
                    free_retries -= 1
                    if free_retries <= 0:
                        raise GateFailure(
                            StopCode.ATTEMPTS,
                            "the implementer's search blocks kept failing to match "
                            f"exactly once, after {self.cfg.implementer_attempts} "
                            f"tries: {last_failure}")
                    free_calls += 1
                    continue
                refused_detail = _report_detail(report)
                self._record_contrast_attempt(
                    attempt=attempt_no, phase="implementer", role="implementer",
                    outcome="refused",
                    error_signature=refused_detail[:120] or "edit-refused",
                    detail=refused_detail, stop_code=StopCode.REFUSED, call=call,
                )
                raise GateFailure(StopCode.REFUSED, refused_detail)
            if report.outside_touches:
                outside_detail = (
                    "the implementer edited outside touches: "
                    + ", ".join(report.outside_touches)
                )
                self._record_contrast_attempt(
                    attempt=attempt_no, phase="implementer", role="implementer",
                    outcome="refused", error_signature="outside-touches",
                    detail=outside_detail, stop_code=StopCode.REFUSED, call=call,
                )
                raise GateFailure(StopCode.REFUSED, outside_detail)

            outcome = self._judge_attempt(attempt_no, report)
            if outcome is None:
                return
            if outcome == _STEP_ADVANCED:
                last_failure = ""
                last_failed_diff = ""
                counter_tests = ""
                self.coaching_guidance = ""
                if step_advances < max_step_advances:
                    step_advances += 1
                    self.budget.implementer_attempts -= 1
                    free_calls += 1
                continue
            last_failure = outcome
            last_failed_diff = self.current_diff
            if self.plan is not None:
                # A step that fails twice is the spec's second `cannot-split`
                # condition: the split was wrong, and retrying it cannot help.
                self.step_failures += 1
                if self.step_failures >= 2:
                    raise GateFailure(
                        StopCode.CANNOT_SPLIT,
                        f"planner step {self.step_index + 1} failed twice")

        raise GateFailure(StopCode.ATTEMPTS,
                          f"{self.cfg.implementer_attempts} implementer attempts used")

    def _begin_attempt(self, attempt_no: int) -> None:
        self.attempt = attempt_no
        self.git.reset_hard(self._head_before_edits())
        self._restore_frozen()
        if not self.git.is_clean():
            raise Infra("dirty-attempt",
                        "the worktree was not clean before an attempt")

    def _head_before_edits(self) -> str:
        return self.step_checkpoint_commit or self.frozen_commit or self._base_ref()

    def _diff_against_head_before_edits(self) -> dict:
        ref = self._head_before_edits()
        changed = sorted(set(self.git.diff_name_only(ref))
                         | set(self.git.untracked_files()))
        text = diff_text(self.worktree, changed, ref=ref) if changed else ""
        return {"diff": text, "paths": changed,
                "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def _restore_frozen(self) -> None:
        """Test files and stub lines are locked by hash, not by trust."""
        for path, digest in self.frozen_tests.items():
            full = self.worktree / path
            if not full.exists():
                raise Infra("frozen-missing", f"{path} vanished between attempts")

    def _frozen_texts(self) -> dict[str, str]:
        out = {}
        for path in self.frozen_tests:
            full = self.worktree / path
            if full.exists():
                out[path] = full.read_text()
        return out

    def _stash_diff(self) -> dict:
        changed = self._changed()
        text = diff_text(self.worktree, changed) if changed else ""
        return {"diff": text, "paths": changed,
                "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def _check_card_unchanged(self) -> None:
        """A card edited mid-run no longer matches its hash.

        Cards and findings can be written by people and tools outside the farm, so
        the hash taken at claim is re-checked before the branch is handed over. This
        is cheap (one `hash` op) and it is the only thing that makes the dossier's
        "card changed" line mean anything.
        """
        source = getattr(self, "card_source", None)
        if source is None:
            return
        try:
            now = source.hash(self.card.id)["hash"]
        except (AdapterError, KeyError, TypeError):
            return
        if now != getattr(self, "card_hash_at_claim", ""):
            raise GateFailure(
                StopCode.CARD_CHANGED,
                "the card changed while it was being worked: its hash no longer "
                "matches the one taken at claim")

    def _heartbeat(self) -> None:
        """Renew the lease, where the card source has one.

        A lease that silently expires lets another lane claim the card, and this run's
        `move` would then fail after all the work. Best effort: a card source without
        leases answers `unsupported`, which is not an error.
        """
        source = getattr(self, "card_source", None)
        if source is None or not self.lease_id:
            return
        heartbeat = getattr(source, "heartbeat", None)
        if heartbeat is None:
            return
        try:
            heartbeat(self.card.id, self.lease_id, self.cfg.lease_ttl_s)
        except (Unsupported, Refused) as exc:
            self.log(f"{self.card.id}: lease not renewed: {exc.message}")
        except AdapterError as exc:
            self.log(f"{self.card.id}: lease not renewed: {exc.message}")

    def _record_attempt(self, phase: str, outcome: str,
                        stop_code: StopCode | None = None, *,
                        diff: dict | None = None, gates: list[GateResult] | None = None,
                        role: str = "", started: str = "",
                        attempt: int | None = None,
                        detail: str = "") -> None:
        """Write one attempt record, in the fields the spec fixes.

        `attempt` is passed explicitly by the implementer, because a refused answer
        gives the attempt back and the budget's counter is then decremented: reading
        it here would number two different records the same.
        """
        from .adapters.jsonl import ATTEMPT_FIELDS, complete_record
        from .edits import line_counts

        diff = diff or {}
        added, removed = line_counts(diff.get("diff", "") or "")
        calls = [call for call in self.roles.calls
                 if call.role == (role or phase)]
        record = {
            "run_id": self.run_id,
            "card_id": self.card.id,
            "card_hash": getattr(self, "card_hash_at_claim", ""),
            "attempt": (attempt if attempt is not None else
                        self.budget.implementer_attempts
                        if phase in ("implement", "fix")
                        else self.budget.test_writer_attempts),
            "phase": phase,
            "role": role or phase,
            "status": outcome,
            "detail": detail[-2000:] if detail else "",
            "started_at": started or self.started_at,
            "ended_at": utc_now(),
            "model": calls[-1].model if calls else "",
            "temperature": calls[-1].temperature if calls else 0.0,
            "seed": calls[-1].seed if calls else 0,
            "tokens_in": sum(call.tokens_in for call in calls),
            "tokens_out": sum(call.tokens_out for call in calls),
            "spend": round(sum(call.spend for call in calls), 6),
            "edit_format": "search_replace",
            "files_changed": list(diff.get("paths", [])),
            "lines_added": added,
            "lines_removed": removed,
            "diff_hash": diff.get("hash", ""),
            "model_builds": self.budget.model_builds,
            "fixed_builds": self.budget.fixed_builds,
            "runner_seconds": round(self.runner_seconds, 2),
            "gates": [gate.as_dict() for gate in (gates or self.gates)],
            "named_tests_passing": self.named_passing,
            "error_signature": (self.error_signatures[-1]
                                if self.error_signatures else ""),
            "outcome": outcome,
            "stop_code": stop_code.value if stop_code else "",
        }
        complete = complete_record(record, ATTEMPT_FIELDS)
        write_json(self.card_layout / "attempts"
                   / f"{len(self.attempts) + 1:02d}-{phase}.json", complete)
        self.attempts.append(complete)
        self.telemetry.write("attempt", complete)

    def _judge_attempt(self, attempt_no: int, report) -> str | None:
        """Run the per-attempt gates. Returns a failure detail, or None to move on."""
        diff = self._stash_diff()
        self.current_diff = diff["diff"]
        write_json(self.card_layout / "edits" / f"{attempt_no}-diff.json", diff)
        (self.card_layout / "edits" / f"{attempt_no}.diff").write_text(diff["diff"])
        self.diff_hashes.append(diff["hash"])

        changed = diff["paths"]
        touches_gate = self._record_gate(
            diff_within_touches(changed, self.card.touches, self.cfg))
        if not touches_gate.ok:
            raise GateFailure(StopCode.REFUSED, touches_gate.detail)

        # Banned patterns on added lines, before anything is built.
        from .packs.base import added_lines_from_diff
        added = added_lines_from_diff(diff["diff"])
        production = {path for path in changed
                      if self.pack.classify_paths([path]).get(path) == "production"}
        banned = self._record_gate(banned_patterns(
            added, self.cfg, production,
            base_counts=self._banned_counts(changed, at_base=True),
            head_counts=self._banned_counts(changed, at_base=False),
            pack=self.pack))
        if not banned.ok:
            sig = "banned:" + banned.detail[:120]
            self.error_signatures.append(sig)
            self._check_progress_with_coach(diff, banned.detail, attempt_no)
            self._record_attempt("fix", "failed-gates", diff=diff,
                                 attempt=attempt_no, role="implementer",
                                 detail=banned.detail)
            last_call = next(
                (c for c in reversed(self.roles.calls) if c.role == "implementer"),
                None,
            )
            self._record_contrast_attempt(
                attempt=attempt_no, phase="implementer", role="implementer",
                outcome="fail", error_signature=sig, detail=banned.detail,
                gates=[banned], call=last_call,
            )
            return banned.detail

        frozen_gate = self._record_gate(
            frozen_check(self.frozen_tests, self._current_test_hashes()))
        if not frozen_gate.ok:
            raise GateFailure(StopCode.REFUSED, frozen_gate.detail)

        # The suite and lint a model's edit caused. These count against the cap.
        unit = self._effective_unit()
        test_result = self._run(runner_kind("test"),
                                self.pack.commands("test", unit=unit)["argv"])
        out, err = self._output(test_result)
        parsed = self.pack.parse_tests(out, err)
        suite = self._record_gate(suite_green(
            parsed, self.cfg.known_failing,
            run_cls=test_result.cls, exit_code=test_result.exit_code))
        passing = sum(1 for name in self.card.test_names
                      if _status_of(parsed, name) == "ok")
        self.named_passing = passing
        self.best_named_passing = max(self.best_named_passing, passing)
        # Only cards with named tests have a count that can rise. A mechanical card
        # or a backfill card would sit at zero for ever, and the "not rising" rule
        # would then stop it on its second attempt whatever it had achieved.
        if self.card.test_names:
            self.named_history.append(passing)

        lint_result = self._run(runner_kind("lint"),
                                self.pack.commands("lint", unit=unit)["argv"])
        lint_out, lint_err = self._output(lint_result)
        lint_evidence = self._pack_gate_evidence("lint")
        lint_gate = self._record_gate(
            GateResult("lint", PASS if lint_result.cls == PASS else
                       (INFRA if lint_result.cls == INFRA else FAIL),
                       "" if lint_result.cls == PASS else
                       (lint_err or lint_out).strip()[-800:], lint_evidence))
        if lint_result.cls == INFRA or suite.result == INFRA:
            raise Infra("attempt-infra",
                        "a gate did not finish, so the attempt is not judged")

        signature = self.pack.error_signature(
            lint_err + "\n" + err + "\n" + out)["signature"]
        self.error_signatures.append(signature)

        last_impl_call = next(
            (c for c in reversed(self.roles.calls) if c.role == "implementer"),
            None,
        )

        all_named = all(_status_of(parsed, name) == "ok"
                        for name in self.card.test_names)
        if suite.ok and lint_gate.ok and all_named:
            if self.plan is not None and self.plan.steps:
                self.step_index = len(self.plan.steps)
            self._commit(f"{self.card.id}: make the frozen tests pass")
            self._record_attempt("implement", "passed-gates", diff=diff,
                                 attempt=attempt_no, role="implementer")
            self._record_contrast_attempt(
                attempt=attempt_no, phase="implementer", role="implementer",
                outcome="pass", error_signature=signature,
                gates=[suite, lint_gate], call=last_impl_call,
            )
            return None

        if self.plan is not None and self.step_index + 1 < len(self.plan.steps):
            step = self._current_step() or {}
            wanted = step.get("tests") or []
            step_tests_ok = bool(wanted) and all(
                _status_of(parsed, name) == "ok" for name in wanted)
            later_step_tests = {
                t
                for s in self.plan.steps[self.step_index + 1:]
                for t in (s.get("tests") or [])
            }
            unexpected = suite.evidence.get("unexpected", [])
            no_compile_or_infra = (
                not suite.evidence.get("compile_errors")
                and not parsed.get("missing_footers")
                and test_result.cls != INFRA
                and suite.result != INFRA
            )
            outside_later_ok = suite.ok or (
                no_compile_or_infra
                and bool(unexpected)
                and set(unexpected) <= later_step_tests
            )
            if lint_gate.ok and step_tests_ok and outside_later_ok:
                self.step_checkpoint_commit = self._commit(
                    f"{self.card.id}: checkpoint step {self.step_index + 1}")
                self.step_summaries.append(
                    f"Step {self.step_index + 1} ({step.get('does', '')}): "
                    f"passed {', '.join(wanted)}")
                self.step_index += 1
                self.diff_hashes.clear()
                self.error_signatures.clear()
                self.named_history.clear()
                self.step_failures = 0
                self.coach_used_for_stall = False
                self._record_attempt("implement", "passed-gates", diff=diff,
                                     attempt=attempt_no, role="implementer")
                self._record_contrast_attempt(
                    attempt=attempt_no, phase="implementer", role="implementer",
                    outcome="pass", error_signature=signature,
                    gates=[suite, lint_gate], call=last_impl_call,
                )
                self.log(f"{self.card.id}: planner step done, checkpointed, now on "
                         f"{self.step_index + 1} of {len(self.plan.steps)}")
                return _STEP_ADVANCED

        if suite.ok and lint_gate.ok:
            detail = ("named tests still failing: " + ", ".join(
                name for name in self.card.test_names
                if _status_of(parsed, name) != "ok"))
            # The suite can be green while a named test is still red, so the progress
            # signals have to be checked on this path too.
            self._check_progress_with_coach(diff, detail, attempt_no)
            self._record_attempt("fix", "failed-gates", diff=diff,
                                 attempt=attempt_no, role="implementer",
                                 detail=detail)
            self._record_contrast_attempt(
                attempt=attempt_no, phase="implementer", role="implementer",
                outcome="fail", error_signature=signature or "named-test-fail",
                detail=detail, gates=[suite, lint_gate], call=last_impl_call,
            )
            return detail

        compile_output = ""
        if suite.evidence.get("compile_errors"):
            compile_output = (err or out).strip()
        detail = (suite.detail or "") + (
            " | " + lint_gate.detail if lint_gate.detail else "")
        if compile_output:
            detail += "\n\n" + compile_output[-6000:]
        self._check_progress_with_coach(diff, detail, attempt_no)
        self._record_attempt("fix", "failed-gates", diff=diff,
                             attempt=attempt_no, role="implementer",
                             detail=detail)
        self._record_contrast_attempt(
            attempt=attempt_no, phase="implementer", role="implementer",
            outcome="fail", error_signature=signature,
            detail=detail, gates=[suite, lint_gate], call=last_impl_call,
        )
        return detail

    def _check_progress_with_coach(self, diff: dict, detail: str,
                                   attempt_no: int) -> None:
        if self._maybe_handle_struggling(
            role="implementer",
            attempt_no=attempt_no,
            detail=detail,
            diff_text=diff.get("diff", ""),
        ):
            return
        try:
            self._check_progress()
        except GateFailure as exc:
            if (exc.code == StopCode.NO_PROGRESS
                    and self.micromanager.enabled
                    and not self.coach_used_for_stall
                    and self.budget.implementer_attempts_left() > 0):
                self._refresh_micromanager_harness_context(
                    "implementer",
                    failure_class=_classify_failure(
                        self.error_signatures[-1] if self.error_signatures else "",
                        failure=detail,
                    ),
                )
                self._check_stop_requested()
                try:
                    advice = self.micromanager.coach(
                        self.card, "implementer", attempt_no,
                        self._frozen_texts(), diff.get("diff", ""), detail,
                        cfg=self.cfg)
                except (Refused, Invalid, ValueError, Infra) as mm_exc:
                    if isinstance(mm_exc, Infra) and mm_exc.code != "acp-timeout":
                        raise
                    self._record_gate(GateResult(
                        "micromanager-coach",
                        FAIL if isinstance(mm_exc, Refused) else INFRA,
                        f"coach degraded ({type(mm_exc).__name__}): "
                        f"{mm_exc.message if hasattr(mm_exc, 'message') else mm_exc}",
                        {"error": str(mm_exc)},
                    ))
                    self.log(f"{self.card.id}: micromanager coach degraded: {mm_exc}")
                    raise
                self._record_gate(GateResult(
                    "micromanager-coach",
                    FAIL if advice.abort else PASS,
                    advice.abort_reason or advice.root_cause or "coaching provided",
                    advice.as_dict(),
                ))
                if advice.abort:
                    raise GateFailure(StopCode.NO_PROGRESS,
                                      advice.abort_reason or exc.detail) from exc
                self.coaching_guidance = advice.guidance
                self.coach_used_for_stall = True
                return
            raise

    def _check_progress(self) -> None:
        """Any one of three signals stops the card, per the spec.

        The three catch different stalls: the same diff, the same error, and passing
        named tests that don't rise.
        """
        if len(self.diff_hashes) >= 2 and self.diff_hashes[-1] == self.diff_hashes[-2]:
            raise GateFailure(StopCode.NO_PROGRESS, "the same diff hash came back twice")
        if (len(self.error_signatures) >= 2
                and self.error_signatures[-1] == self.error_signatures[-2]):
            raise GateFailure(StopCode.NO_PROGRESS,
                              "the same error signature came back twice")
        # "Passing named tests not up over two attempts" needs two attempts that
        # actually ran. `named_history` holds one entry per judged attempt.
        if len(self.named_history) >= 2:
            previous, current = self.named_history[-2], self.named_history[-1]
            if current <= previous:
                raise GateFailure(
                    StopCode.NO_PROGRESS,
                    f"passing named tests did not rise: {previous} then {current}")

    def _banned_counts(self, paths: list[str], at_base: bool
                       ) -> dict[str, dict[str, int]]:
        """How often each banned pattern appears in each file, at base or at head.

        The gate compares the two counts. A label whose count rose was introduced by
        this card; one that did not was already there and is flagged. Counting the
        file's whole content is what makes a moved line safe to allow.
        """
        pack = self.pack
        out: dict[str, dict[str, int]] = {}
        for path in paths:
            if at_base:
                content = self.git.show_blob(f"{self.base_commit}:{path}")
            else:
                full = self.worktree / path
                content = full.read_text() if full.exists() else None
            if content is None:
                continue
            production = ({path} if pack.classify_paths([path]).get(path)
                          == "production" else set())
            lines = [{"path": path, "line": number, "text": line}
                     for number, line in enumerate(content.splitlines(), start=1)]
            found = pack.patterns(lines, [], production)
            counts: dict[str, int] = {}
            for entry in found.get("banned", []):
                label = entry["pattern"]
                counts[label] = counts.get(label, 0) + 1
            if counts:
                out[path] = counts
        return out

    def _current_test_hashes(self) -> dict[str, str]:
        hashes = {}
        for path in self.frozen_tests:
            full = self.worktree / path
            if full.exists():
                hashes[path] = hashlib.sha256(full.read_bytes()).hexdigest()
        return hashes

    # -- step 8: the end gates ------------------------------------------------

    def _step_end_gates(self, allow_checker_bounce: bool = False,
                        allow_pre_review_bounce: bool = False
                        ) -> tuple[str, str, str, str] | None:
        self.phase = "check"
        unit = self._effective_unit()

        # pass-after: the named tests, twice, per the spec's flakiness rule. A card
        # that names no tests has nothing to pass here; the suite gate below is its
        # judge.
        named_ok = True
        if self.card.test_names:
            for index in (1, 2):
                result = self._run(runner_kind("test"),
                                   self.pack.commands("test", unit=unit,
                                                      tests=self.card.test_names)["argv"], counted="fixed")
                out, err = self._output(result)
                parsed = self.pack.parse_tests(out, err)
                self.head_status_by_test = _named_failures(
                    parsed, self.card.test_names, self._named_test_files(),
                    self._row_file())
                gate = self._record_gate(
                    ignored_or_filtered(parsed, self.card.test_names,
                                        run_cls=result.cls,
                                        exit_code=result.exit_code,
                                        files=self._named_test_files(),
                                        row_file=self._row_file()))
                named_ok = named_ok and gate.ok
                if gate.result == INFRA:
                    raise Infra("pass-after-infra", gate.detail)
                if not gate.ok:
                    raise GateFailure(
                        StopCode.ATTEMPTS,
                        f"named tests not passing at the end: {gate.detail}")
        else:
            self._record_gate(GateResult(
                "pass-after", PASS,
                "skipped: the card names no tests, so the suite gate is the judge"))

        # Suite and lint, then formatting and workspace lint.
        suite_result = self._run(runner_kind("test"),
                                 self.pack.commands("test", unit=unit)["argv"],
                                 counted="fixed")
        out, err = self._output(suite_result)
        parsed = self.pack.parse_tests(out, err)
        suite = self._record_gate(suite_green(
            parsed, self.cfg.known_failing,
            run_cls=suite_result.cls, exit_code=suite_result.exit_code))
        if suite.result == INFRA:
            raise Infra("suite-infra", suite.detail)
        if not suite.ok:
            raise GateFailure(StopCode.ATTEMPTS, suite.detail)

        # Format production files (never frozen tests) BEFORE computing full_diff
        # and running revert-check / banned-patterns / checker.
        self._format_touched(self._changed_since_base())

        changed = self._changed_since_base()
        full_diff = diff_text(self.worktree, changed,
                              ref=self.base_commit or "HEAD")
        self.current_diff = full_diff
        added, removed = line_counts(full_diff)

        if self.cfg.workspace_lint:
            ws = self._run(runner_kind("ws-lint"),
                           self.pack.commands("ws-lint")["argv"], counted="fixed")
            ws_out, ws_err = self._output(ws)
            ws_evidence = self._pack_gate_evidence("workspace-lint")
            ws_gate = self._record_gate(GateResult(
                "workspace-lint", PASS if ws.cls == PASS else
                (INFRA if ws.cls == INFRA else FAIL),
                "" if ws.cls == PASS else (ws_err or ws_out).strip()[-800:],
                ws_evidence))
            if ws_gate.result == INFRA:
                raise Infra("workspace-lint-infra", "the workspace lint did not finish")
            if not ws_gate.ok:
                raise GateFailure(StopCode.ATTEMPTS, ws_gate.detail)

        self._extra_gates(changed)

        # Banned patterns again over the card's whole diff.
        from .packs.base import added_lines_from_diff
        whole_added = added_lines_from_diff(full_diff)
        production_all = {
            path for path in changed
            if self.pack.classify_paths([path]).get(path) == "production"}
        final_banned = self._record_gate(
            banned_patterns(
                whole_added, self.cfg, production_all,
                base_counts=self._banned_counts(changed, at_base=True),
                head_counts=self._banned_counts(changed, at_base=False),
                pack=self.pack))
        if not final_banned.ok:
            raise GateFailure(StopCode.REFUSED, final_banned.detail)

        # No test loss, against the list taken on base.
        final_list_result = self._run(
            runner_kind("list"),
            self.pack.commands("list", unit=unit)["argv"], counted="fixed")
        list_out, list_err = self._output(final_list_result)
        if final_list_result.cls == INFRA:
            raise Infra(
                "runner-infra",
                (list_err or list_out).strip()[-500:] or "final test list command failed on infra",
            )
        final_list = self.pack.parse_list(list_out)
        self.known_failing_excluded = sorted(
            set(self.cfg.known_failing) & {test["name"]
                                          for test in final_list.get("tests", [])})
        loss = self._record_gate(
            test_loss(self.base_test_list, final_list, self.cfg.known_failing))
        if not loss.ok:
            raise GateFailure(StopCode.REFUSED, loss.detail)

        # Revert check: skipped for test-backfill, by the spec.
        if self.cfg.revert_check == "required" and self.card.category != "test-backfill":
            current = self._current_test_hashes()
            gate = self._record_gate(frozen_check(self.frozen_tests, current))
            if not gate.ok:
                raise GateFailure(StopCode.REFUSED, gate.detail)
            if not self.card.test_names:
                self._record_gate(revert_check(
                    self.frozen_hash, _tests_hash(current)))
            else:
                trial_wt = self.card_layout / "revert-check-wt"
                revert_target = self.card_layout / "revert-check-target"
                if trial_wt.exists():
                    self.git.worktree_remove(trial_wt)
                    shutil.rmtree(trial_wt, ignore_errors=True)
                shutil.rmtree(revert_target, ignore_errors=True)
                base_ref = self.scaffold_commit or self.base_commit or "HEAD"
                self.git.worktree_add(trial_wt, base_ref)
                self._prepare_checkout(trial_wt)
                try:
                    overlay = sorted(set(self.frozen_tests) | set(self.phase_a_stub_files))
                    for rel in overlay:
                        blob = self.git.show_blob(f"{self.frozen_commit}:{rel}")
                        if blob is not None:
                            dst = trial_wt / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            dst.write_text(blob)
                    env = self.cfg.runner_env.get(unit, {})
                    rev_result = self.runner.exec(
                        kind=runner_kind("test"),
                        argv=self.pack.commands("test", unit=unit,
                                           tests=self.card.test_names)["argv"],
                        cwd=trial_wt,
                        target_dir=revert_target,
                        unit=unit,
                        env=env,
                        timeout_s=max(30.0, self.budget.wall_clock_left_s()),
                    )
                    self.runner_seconds += getattr(rev_result, "duration_s", 0.0) or 0.0
                    self.budget.note_fixed_build()
                    r_out, r_err = self._output(rev_result)
                    r_parsed = self.pack.parse_tests(r_out, r_err)
                    r_failures = _named_failures(r_parsed, self.card.test_names,
                                                 self._named_test_files(),
                                                 self._row_file())
                    r_compile = r_parsed.get("compile_errors", [])
                    if (rev_result.cls == INFRA
                            or (r_parsed.get("missing_footers") and not r_compile
                                and not (rev_result.cls == FAIL
                                         and rev_result.exit_code < 0
                                         and rev_result.exit_code != -9))):
                        self._record_gate(GateResult(
                            "revert-check", INFRA,
                            "the revert-check run did not finish", r_parsed))
                        raise Infra("revert-check-infra",
                                    "the revert-check run did not finish")
                    wrong = [n for n, st in r_failures.items() if st != "FAILED"]
                    if r_compile or wrong:
                        detail = (
                            "revert-check failed to reproduce red tests on base"
                            if r_compile else
                            "tests did not fail when implementation was reverted: "
                            + ", ".join(wrong[:10])
                        )
                        self._record_gate(GateResult(
                            "revert-check", FAIL, detail,
                            {"compile_errors": r_compile, "not_failing": wrong,
                             "failures": r_failures}))
                        raise GateFailure(StopCode.TESTS_UNPROVABLE, detail)
                    self._record_gate(GateResult(
                        "revert-check", PASS,
                        f"{len(self.card.test_names)} named test(s) reverted to failing",
                        {"failures": r_failures}))
                finally:
                    self.git.worktree_remove(trial_wt)
                    shutil.rmtree(trial_wt, ignore_errors=True)
                    shutil.rmtree(revert_target, ignore_errors=True)

        self._mutation_gate(changed, test_paths=self._test_paths())

        # Checker pass 1, then pass 2 with counter-tests (plus adjudication/bounce).
        checker_bounce = self._checker(changed, full_diff,
                                       allow_bounce=allow_checker_bounce)
        if checker_bounce is not None:
            return checker_bounce

        if self.micromanager.enabled:
            self._refresh_micromanager_harness_context("micromanager")
            self._check_stop_requested()
            try:
                report = self.micromanager.triage_and_review("pre_review", {
                    "card_id": self.card.id,
                    "card": self.card.as_dict(),
                    "full_diff": full_diff,
                    "gates": [g.as_dict() for g in self.gates],
                    "touches": list(self.card.touches),
                    "counter_tests": dict(self.counter_summary),
                    "mutation": dict(self.mutation_summary),
                })
            except (Refused, Invalid, ValueError, Infra) as exc:
                if isinstance(exc, Infra) and exc.code != "acp-timeout":
                    raise
                self._record_gate(GateResult(
                    "micromanager-pre-review",
                    FAIL if isinstance(exc, Refused) else INFRA,
                    f"pre_review degraded ({type(exc).__name__}): "
                    f"{exc.message if hasattr(exc, 'message') else exc}",
                    {"error": str(exc)},
                ))
                self.log(f"{self.card.id}: micromanager pre_review degraded: {exc}")
                report = None
            if report is not None:
                self._record_gate(GateResult(
                    "micromanager-pre-review",
                    PASS if report.verdict == "approve" else FAIL,
                    report.verdict,
                    report.as_dict(),
                ))
                for entry in report.findings:
                    self._file_finding(entry)
                if report.reviewer_brief:
                    self.reviewer_brief = report.reviewer_brief
                if report.verdict == "block":
                    raise GateFailure(
                        StopCode.SUPERVISOR_REJECT,
                        report.missed_clause or report.summary
                        or "micromanager blocked pre-review",
                    )
                if report.verdict == "bounce":
                    if allow_pre_review_bounce:
                        self.coaching_guidance = (
                            report.missed_clause or report.reviewer_brief
                            or report.summary)
                        return (
                            "pre_review",
                            full_diff,
                            report.missed_clause
                            or "supervisor requested changes during pre-review",
                            "",
                        )
                    raise GateFailure(
                        StopCode.SUPERVISOR_REJECT,
                        report.missed_clause or report.summary
                        or "micromanager bounced pre-review with no attempts left",
                    )

        self.existing_tests_changed = [
            path for path in changed
            if self._is_test_path(path) and path not in self.frozen_tests
            and path not in self.card.test_edits]
        write_json(self.card_layout / "gates" / "summary.json",
                   {"gates": [g.as_dict() for g in self.gates],
                    "files_changed": changed, "lines_added": added,
                    "lines_removed": removed})
        return None

    def _format_touched(self, changed: list[str]) -> None:
        """Format only non-frozen files in touches that were format-clean at base.

        A file that wasn't clean at base is left alone and named in the dossier,
        because formatting it could change lines the card never touched.
        """
        # Only the pack's own language is formatted; everything else is `other`.
        kinds = self.pack.classify_paths(list(changed)) if changed else {}
        candidates = [path for path in changed
                      if kinds.get(path, "other") != "other"
                      and path not in self.frozen_tests
                      and matches_any(path, self.card.touches)]
        if not candidates:
            return
        dirty_at_base = []
        clean = []
        for path in candidates:
            if self._was_format_clean_at_base(path):
                clean.append(path)
            else:
                dirty_at_base.append(path)

        if not clean:
            self.unformatted = dirty_at_base
            return
        result = self._run(runner_kind("extra"),
                           self.pack.commands("format", files=clean)["argv"],
                           counted="fixed")
        if result.cls != PASS:
            self.log(f"{self.card.id}: the formatter could not format "
                     f"{len(clean)} file(s)")
        else:
            # The reviewer gets the branch, so the formatting has to be on it. Without
            # this commit the dossier's diff and the branch disagree.
            self._commit(f"{self.card.id}: format the touched files")
        self.unformatted = dirty_at_base

    def _was_format_clean_at_base(self, path: str) -> bool:
        """Check the file's formatting at the base commit, once per run."""
        blob = self.git.show_blob(f"{self.base_commit}:{path}")
        if blob is None:
            return True  # a new file: nothing at base to compare
        scratch = self.card_layout / "frozen" / "format-check"
        scratch.mkdir(parents=True, exist_ok=True)
        temp = scratch / Path(path).name
        temp.write_text(blob)
        result = self._run(runner_kind("extra"),
                           self.pack.commands("fmt-check", files=[str(temp)])["argv"],
                           counted="fixed")
        return result.cls == PASS

    def _extra_gates(self, changed: list[str]) -> None:
        from .adapters.direct import DirectRunner

        for extra in self.cfg.extra_gates:
            if not paths_touched_by_extra_gate(changed, extra.paths):
                continue
            argv = _split_command(extra.run)
            if getattr(extra, "via", "runner") == "local":
                self._check_kill_switch()
                resolved_unit = self._effective_unit()
                env = self.cfg.runner_env.get(resolved_unit, {})
                timeout = max(30.0, self.budget.wall_clock_left_s())
                local_runner = DirectRunner(
                    self.worktree,
                    output_dir=self.card_layout / "gates" / "output",
                )
                result = local_runner.exec(
                    kind=runner_kind("extra"),
                    argv=argv,
                    cwd=self.worktree,
                    target_dir=self.target_dir,
                    unit=resolved_unit,
                    env=env,
                    timeout_s=timeout,
                )
                self.runner_seconds += getattr(result, "duration_s", 0.0) or 0.0
                self.budget.note_fixed_build()
                self.budget.check_clock()
                out, err = result.stdout, result.stderr
            else:
                result = self._run(runner_kind("extra"), argv, counted="fixed")
                out, err = self._output(result)
            if result.cls == INFRA:
                detail = (
                    (err or out).strip()[-500:]
                    or f"extra gate {extra.name!r} failed on infra"
                )
                self._record_gate(GateResult(f"extra:{extra.name}", INFRA, detail))
                raise Infra("runner-infra", detail)
            passed = result.cls == PASS
            gate = self._record_gate(extra_gate_result(
                extra.name, passed, extra.on_fail,
                "" if passed else (err or out).strip()[-500:]))
            if not passed:
                if extra.on_fail == "ineligible":
                    raise GateFailure(StopCode.INELIGIBLE, gate.detail)
                raise GateFailure(StopCode.EXTRA_GATE, gate.detail)

    def _mutation_gate(self, changed: list[str],
                       test_paths: list[str] | None = None) -> None:
        mode = self.cfg.mutation
        if mode == "off":
            self._record_gate(GateResult("mutation", PASS, "off in this config"))
            return
        breaking = (self.card.category in self.cfg.mutation_required_for
                    or mode == "required")
        production = [path for path in changed
                      if self.pack.classify_paths([path]).get(path) == "production"]
        if not production and breaking:
            # A test-backfill card changes no production code, and its whole proof is
            # that its new tests kill mutants in the code they cover. That code is the
            # card's unit, not its diff.
            production = self._mutated_unit_files()
        if not production:
            self._record_gate(GateResult(
                "mutation", PASS, "no production lines changed"))
            return

        if hasattr(self.pack, "mutation_output"):
            # The tool's own output folder goes with the card's evidence, never into
            # the worktree, where it would be an untracked folder on the branch.
            self.pack.mutation_output = self.card_layout / "gates" / "mutation"
        try:
            argv = self.pack.commands(
                "mutation", files=production, tests=self.card.test_names)["argv"]
            result = self._run(runner_kind("mutation"), argv, counted="mutation")
        except (Infra, Invalid) as exc:
            self._record_gate(GateResult(
                "mutation", FAIL if breaking else PASS,
                f"the mutation tool did not run: {exc}"))
            if breaking:
                raise GateFailure(StopCode.MUTATION_CAP,
                                  "mutation is this card's only proof and it "
                                  "could not run") from exc
            return

        out, err = self._output(result)
        if result.cls == INFRA:
            detail = ("the mutation tool reported an infra error: "
                      + (err or out).strip()[:300])
            self._record_gate(GateResult(
                "mutation", FAIL if breaking else PASS, detail))
            if breaking:
                # Mutation is this card's only proof, so a tool that cannot run
                # leaves it unproven. That is a card-level block, not infra: infra
                # would pause the queue and spend no attempt, and retrying would meet
                # the same missing tool.
                raise GateFailure(StopCode.MUTATION_CAP, detail)
            return

        try:
            # A pack that knows its tool's exit codes reads them first: a failing
            # baseline or a usage error is the tool not having run, not a result.
            reader = getattr(self.pack, "read_mutation", None)
            if reader is not None:
                parsed = reader(out, err, result.exit_code)
            else:
                parsed = self.pack.parse_mutation(out)
        except Invalid as exc:
            # A tool that is not installed, or prints nothing, is not a finding
            # about the code. In report mode that is recorded and the card carries
            # on; when mutation is this card's only proof, it blocks.
            not_run = exc.code == "mutation-not-run"
            self._record_gate(GateResult(
                "mutation", FAIL if breaking else PASS,
                (f"the mutation tool did not run: {exc.message}" if not_run else
                 f"the mutation tool's output could not be read: {exc.message}"),
                {"exit_code": result.exit_code}))
            if breaking:
                raise GateFailure(
                    StopCode.MUTATION_CAP,
                    "mutation is this card's only proof and "
                    + ("the tool did not run" if not_run else
                       "its output could not be read")) from exc
            return
        # One mutation run is many builds: `cargo mutants` compiles every mutant, so
        # the cap counts the mutants it set out to test, not the one command. The cap
        # is recorded rather than raised, because the run already happened.
        self.budget.mutation_builds += max(1, int(parsed.get("total", 1) or 1))
        self.mutation_summary = {
            "killed": parsed.get("killed", 0),
            "survived": parsed.get("survived", 0),
            "total": parsed.get("total", 0),
            "capped": parsed.get("capped", False) or self.budget.mutation_capped()}
        for key in ("timeout", "unviable", "judged"):
            if key in parsed:
                self.mutation_summary[key] = parsed.get(key, 0)
        gate = self._record_gate(mutation_gate(
            parsed, self.cfg.mutation_min_kill_rate, mode, self.card.category,
            required_for=self.cfg.mutation_required_for))
        if not gate.ok and breaking:
            code = StopCode.MUTATION_CAP if parsed.get("capped") else \
                StopCode.TESTS_UNPROVABLE
            raise GateFailure(code, gate.detail)

    def _mutated_unit_files(self) -> list[str]:
        """The production files of the card's unit, for a card that changed none.

        A `test-backfill` card's diff is all tests, so its mutation target has to come
        from the unit it covers. The card may name them in `touches` alongside the
        tests; otherwise every source file under the unit's `src/` is the target.
        """
        named = [path for path in self.card.touches
                 if self.pack.classify_paths([path]).get(path) == "production"]
        if named:
            return named
        unit = self._effective_unit()
        roots = [self.worktree / unit / "src", self.worktree / "src"] \
            if unit and unit != "." else [self.worktree / "src"]
        out: list[str] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*" + self._extension())):
                out.append(str(path.relative_to(self.worktree)))
            break
        return out

    def _checker(self, changed: list[str], full_diff: str,
                 allow_bounce: bool = False) -> tuple[str, str, str, str] | None:
        test_diff = diff_text(
            self.worktree,
            [path for path in changed if self._is_test_path(path)],
            ref=self.base_commit or "HEAD")
        form1 = pass1_answer_form()
        brief1 = checker_pass1_brief(self.card, test_diff, handback=self.swarm_on,
                                     answer_form=form1)
        # A checker's answer is checked for a hand-back before anything reads it: a
        # hand-back is never a restatement and never a passing checker gate.
        call1, limit_err = self._call_role_with_handbacks(
            "checker_pass1", brief1, first_attempt=1, response_name="checker-pass1")
        if limit_err is not None:
            self._checker_handback_limit("checker-pass1", limit_err)
        first = parse_pass1(call1.content, self.card, cut_off=call1.cut_off)
        first_problem1 = ""
        if not first.readable:
            # Never taken as the restatement: ask once more with the form
            # restated. A hand-back is still read first.
            first_problem1 = first.problem
            retry1 = checker_pass1_brief(self.card, test_diff, handback=self.swarm_on,
                                         answer_form=form1,
                                         retry_problem=first.problem)
            asked1 = sum(1 for c in getattr(self.roles, "calls", []) or []
                         if getattr(c, "role", "") == "checker_pass1")
            call1, limit_err = self._call_role_with_handbacks(
                "checker_pass1", retry1, first_attempt=1 + max(1, asked1),
                response_name="checker-pass1-again")
            if limit_err is not None:
                self._checker_handback_limit("checker-pass1", limit_err)
            first = parse_pass1(call1.content, self.card, cut_off=call1.cut_off)
        restatement = first.restatement
        self.checker_restatement = restatement
        self.checker_pass1 = {**first.as_dict(), "asked_again": bool(first_problem1),
                              "first_problem": first_problem1}
        if not first.readable:
            detail1 = ("the first pass could not be read, even when asked again "
                       f"({first.problem}): no restatement to compare with the tests")
        else:
            detail1 = ("restatement flagged as drifting" if restatement.mismatch
                       else "restatement matches the named tests")
            said1 = {"yes": "would catch a bad implementation",
                     "partly": "would partly catch a bad implementation",
                     "no": "would not catch a bad implementation"}.get(first.catches)
            if said1:
                detail1 += f"; the checker says the tests {said1}"
        gate1 = self._record_gate(GateResult(
            "checker-pass1", PASS, detail1,
            {"restatement": restatement.as_dict(), "checker": self.checker_pass1}))

        from .roles import MAX_COUNTER_TESTS, parse_pass2, pass2_answer_form

        modified = {}
        for path in changed[:8]:
            full = self.worktree / path
            if full.exists():
                modified[path] = full.read_text()[:8000]
        # The brief names the language, the runner and the files the counter-tests
        # are saved as, in the same form `parse_pass2` reads.
        language = str(getattr(self.pack, "name", "") or "")
        crate = ""
        if language == "rust":
            import tomllib
            unit = self._effective_unit()
            manifests = ([self.worktree / unit / "Cargo.toml"]
                         if unit and unit != "." else []) + [self.worktree / "Cargo.toml"]
            for manifest in manifests:
                try:
                    data = tomllib.loads(manifest.read_text())
                except (OSError, ValueError):
                    continue
                name = str((data.get("lib") or {}).get("name")
                           or (data.get("package") or {}).get("name") or "")
                if name:
                    crate = name.replace("-", "_")
                    break
            if not crate and unit and unit != "." and "/" not in unit:
                crate = unit.replace("-", "_")
        answer_form = pass2_answer_form(
            language, str(getattr(self.pack, "runner", "") or ""),
            [self._counter_fallback_path(i) for i in range(1, MAX_COUNTER_TESTS + 1)],
            crate)
        gate_list = [g.as_dict() for g in self.gates]
        brief2 = checker_pass2_brief(
            self.card, full_diff, gate_list, modified,
            handback=self.swarm_on, answer_form=answer_form)
        call2, limit_err = self._call_role_with_handbacks(
            "checker_pass2", brief2, first_attempt=2, response_name="checker-pass2")
        if limit_err is not None:
            self._checker_handback_limit("checker-pass2", limit_err)
        answer = parse_pass2(call2.content, cut_off=call2.cut_off)
        first_problem = ""
        if not answer.readable:
            # An answer that cannot be read is never "no counter-tests": ask once
            # more with the form restated. A hand-back is still read first.
            first_problem = answer.problem
            retry = checker_pass2_brief(
                self.card, full_diff, gate_list, modified,
                handback=self.swarm_on, answer_form=answer_form,
                retry_problem=answer.problem)
            asked = sum(1 for c in getattr(self.roles, "calls", []) or []
                        if getattr(c, "role", "") == "checker_pass2")
            call2, limit_err = self._call_role_with_handbacks(
                "checker_pass2", retry, first_attempt=2 + max(1, asked),
                response_name="checker-pass2-again")
            if limit_err is not None:
                self._checker_handback_limit("checker-pass2", limit_err)
            answer = parse_pass2(call2.content, cut_off=call2.cut_off)

        counted_drops = [d for d in answer.dropped if not d.get("over_limit")]
        self.counter_summary = {
            "written": len(answer.counter_tests) + len(counted_drops),
            "dropped_uncompiled": len(counted_drops),
            "passed": 0,
            "failed": 0,
            "not_run": 0,
            "unreadable": 0 if answer.readable else 1,
        }
        self._last_failing_counters = []
        self.counter_results = []
        if answer.counter_tests:
            self._run_counter_tests(answer.counter_tests)
        self.counter_results += [
            {"name": d.get("name", ""), "file": "", "outcome": "dropped",
             "why": d.get("why", ""), "output_head": ""}
            for d in answer.dropped]
        self.checker_pass2 = {
            **answer.as_dict(),
            "asked_again": bool(first_problem),
            "first_problem": first_problem,
            "counter_results": list(self.counter_results),
        }

        if self.board is not None and self._last_failing_counters:
            names = ", ".join(
                str(c.get("name", "?")) for c in self._last_failing_counters
            )
            evidence = [
                str(p)
                for idx in range(1, len(answer.counter_tests) + 1)
                for p in [self.card_layout / "gates" / f"counter-test-{idx}.json"]
                if p.exists()
            ]
            self._post_board(
                author=worker_member_name(self.card.id, "checker"),
                to=[worker_member_name(self.card.id, "implementer")],
                kind="defect",
                attempt=max(1, self.attempt),
                body=f"counter-tests failed: {names}",
                evidence=evidence,
            )

        gate_evidence = {"counter_tests": self.counter_summary,
                         "checker": self.checker_pass2}
        if not answer.readable:
            detail = ("the second pass could not be read, even when asked again "
                      f"({answer.problem}): no counter-test ran, nothing was checked")
            if self.cfg.counter_tests == "reject":
                # The person asked for the checker to gate, and no check happened.
                self._record_gate(GateResult("checker-pass2", FAIL, detail,
                                             gate_evidence))
                raise GateFailure(StopCode.CHECKER_REJECT, detail)
            self._record_gate(GateResult(
                "checker-pass2", PASS,
                detail + " (counter-tests are report-only)", gate_evidence))
        else:
            said = {"accept": "checker accepts the diff",
                    "changes-requested": "checker asks for changes"}.get(
                        answer.verdict, "checker stated no verdict (old answer form)")
            summary = self.counter_summary
            detail = (f"{said}; {summary['written']} counter-test(s): "
                      f"{summary['failed']} failed, {summary['passed']} passed, "
                      f"{summary['dropped_uncompiled'] + summary['not_run']} dropped")
            if answer.verdict == "changes-requested" and not summary["failed"]:
                detail += "; no counter-test fails on the diff"
            self._record_gate(GateResult("checker-pass2", PASS, detail,
                                         gate_evidence))
        self._record_attempt("checker", "passed-gates", role="checker_pass2")

        if (self.cfg.counter_tests == "reject"
                and self.counter_summary["failed"] > 0):
            fix_hint = ""
            if self.micromanager.enabled:
                self._refresh_micromanager_harness_context(
                    "checker", failure_class="counter-test"
                )
                self._check_stop_requested()
                try:
                    verdict = self.micromanager.adjudicate_checker(
                        self.card, full_diff, restatement,
                        self._last_failing_counters,
                    )
                except (Refused, Invalid, ValueError, Infra) as exc:
                    if isinstance(exc, Infra) and exc.code != "acp-timeout":
                        raise
                    self._record_gate(GateResult(
                        "micromanager-adjudicate",
                        FAIL if isinstance(exc, Refused) else INFRA,
                        f"adjudicate_checker degraded ({type(exc).__name__}): "
                        f"{exc.message if hasattr(exc, 'message') else exc}",
                        {"error": str(exc)},
                    ))
                    self.log(f"{self.card.id}: micromanager adjudicate_checker degraded: {exc}")
                    verdict = None
                if verdict is not None:
                    self.checker_adjudication = verdict.as_dict()
                    self._record_gate(GateResult(
                        "micromanager-adjudicate",
                        PASS if verdict.verdict == "dismiss_false_reject" else FAIL,
                        verdict.verdict,
                        verdict.as_dict(),
                    ))
                    if verdict.verdict == "dismiss_false_reject":
                        return None
                    fix_hint = verdict.fix_hint
            if allow_bounce:
                if fix_hint:
                    self.coaching_guidance = fix_hint
                counter_text = "\n\n".join(
                    f"Counter-test {c.get('name')} ({c.get('file')}):\n"
                    f"{c.get('output', '')}"
                    for c in self._last_failing_counters
                )
                return (
                    "checker",
                    full_diff,
                    f"{self.counter_summary['failed']} counter-test(s) fail on the diff"
                    + (f": {fix_hint}" if fix_hint else ""),
                    counter_text,
                )
            raise GateFailure(
                StopCode.CHECKER_REJECT,
                f"{self.counter_summary['failed']} counter-test(s) fail on the diff")
        return None

    def _checker_handback_limit(self, gate_name: str, detail: str) -> None:
        """A checker hand-back past `handbacks_per_card` is a failed checker gate.

        The checker has no attempts to spend, so the failed attempt fails the gate:
        the card blocks with `checker-reject`, never passing on a hand-back.
        """
        self._record_gate(GateResult(gate_name, FAIL, detail))
        raise GateFailure(StopCode.CHECKER_REJECT, detail)

    def _extension(self) -> str:
        """The pack's source extension; `.rs` for packs that predate the field."""
        return str(getattr(self.pack, "extension", "") or ".rs")

    def _counter_fallback_path(self, index: int) -> str:
        unit = self._effective_unit()
        ext = self._extension()
        # Python test files are found by their `test_` prefix, TypeScript ones by
        # their `.test` suffix (the pack's own test-file rule).
        stem = (f"test_openfarm_counter_{index}" if ext == ".py"
                else f"openfarm_counter_{index}.test" if ext == ".ts"
                else f"openfarm_counter_{index}")
        if unit and unit != "." and (self.worktree / unit).is_dir() and any(
                (self.worktree / unit / manifest).is_file()
                for manifest in ("Cargo.toml", "pyproject.toml", "setup.py",
                                 "package.json")):
            return f"{unit.strip('/')}/tests/{stem}{ext}"
        return f"tests/{stem}{ext}"

    def _run_counter_tests(self, tests: list[dict]) -> None:
        """Run the checker's counter-tests in this card's own worktree and target.

        Each is saved as a new file, run alone, and taken away again: the worktree
        is left exactly as it was, nothing new, nothing changed, nothing committed.
        One that doesn't compile is dropped and counted, because a counter-test can
        itself be wrong. Every outcome goes to `self.counter_results`.
        """
        import os
        import re

        if not hasattr(self, "counter_results"):
            self.counter_results = []
        unit = self._effective_unit()

        def listing(*args: str) -> list[str]:
            out = self.git._run(["ls-files", "-z", *args]).stdout
            return [p for p in out.split("\0") if p]

        def snapshot() -> dict:
            dirty = listing("--modified", "--deleted") + listing(
                "--others", "--exclude-standard")
            dirty += [p for p in self.git.staged_changes() if p not in dirty]
            files: dict[str, bytes | None] = {}
            for rel in dirty:
                full = self.worktree / rel
                files[rel] = (full.read_bytes()
                              if full.is_file() and not full.is_symlink() else None)
            ignored = set(listing("--others", "--ignored", "--exclude-standard"))
            # Ignored links (a TypeScript worktree's linked `node_modules`): git
            # never restores them, and a run can remove or re-point one.
            links = {}
            for rel in ignored:
                full = self.worktree / rel
                if full.is_symlink():
                    with contextlib.suppress(OSError):
                        links[rel] = os.readlink(full)
            dirs = {""}
            for rel in list(ignored) + listing() + list(files):
                parent = Path(rel).parent
                while str(parent) not in dirs and str(parent) != ".":
                    dirs.add(str(parent))
                    parent = parent.parent
            return {"head": self.git.head(), "branch": self.git.current_branch(),
                    "files": files, "ignored": ignored, "dirs": dirs,
                    "links": links, "status": self.git.status_lines()}

        def restore(before: dict) -> None:
            if self.git.current_branch() != before["branch"] and before["branch"]:
                self.git._run(["checkout", "-f", before["branch"]])
            # Tracked changes and new files go; a commit a test made goes too.
            self.git.reset_hard(before["head"])
            for rel, data in before["files"].items():
                full = self.worktree / rel
                if data is None:
                    continue
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(data)
            # Ignored files the run left behind (`__pycache__`, `Cargo.lock`, ...).
            for rel in sorted(set(listing("--others", "--ignored", "--exclude-standard"))
                              - before["ignored"]):
                full = self.worktree / rel
                with contextlib.suppress(OSError):
                    os.unlink(full)
                parent = Path(rel).parent
                while str(parent) not in before["dirs"] and str(parent) != ".":
                    with contextlib.suppress(OSError):
                        (self.worktree / parent).rmdir()
                    parent = parent.parent
            # Ignored entries the run removed: the pack sets its own up again
            # (a removed `node_modules` comes back with its marker and links).
            if any(not os.path.lexists(self.worktree / rel)
                   for rel in before["ignored"]):
                self._prepare_checkout(self.worktree)
            # Every ignored link exactly as it was, never followed: a link
            # removed, re-pointed or replaced is made again.
            for rel, link_to in sorted(before["links"].items()):
                full = self.worktree / rel
                with contextlib.suppress(OSError):
                    if full.is_symlink() and os.readlink(full) == link_to:
                        continue
                    if full.is_symlink() or full.is_file():
                        full.unlink()
                    elif full.is_dir():
                        shutil.rmtree(full)
                    full.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(link_to, full)
            after_ignored = set(listing("--others", "--ignored", "--exclude-standard"))
            if (self.git.head() != before["head"]
                    or self.git.status_lines() != before["status"]
                    or after_ignored != before["ignored"]):
                self.log(f"{self.card.id}: the worktree did not come back exactly "
                         "as it was after a counter-test")

        def head_of(text: str) -> str:
            lines = text.strip("\n").splitlines()
            for at, line in enumerate(lines):
                if re.search(r"^(FAIL|ERROR):|panicked at|AssertionError|"
                             r"assert(ion)?\b.*(fail|left|right)|^not ok\b|"
                             r"error(\[E\d+\])?:", line):
                    return "\n".join(lines[at:at + 12])
            return "\n".join(lines[:12])

        def status_of(parsed: dict, name: str) -> str:
            status = _status_of(parsed, name)
            if status != "not-run":
                return status
            for entry in parsed.get("results", []):
                got = str(entry.get("name", ""))
                if got and (got.endswith(("::" + name, "." + name))
                            or name.endswith(("::" + got, "." + got))):
                    return entry.get("status", "")
            # `test_x checks the start`: a header with words after a one-word
            # name, from a language whose test names have no spaces.
            first = name.split(" ", 1)[0]
            if first != name:
                return status_of(parsed, first)
            return "not-run"

        for index, counter in enumerate(tests, start=1):
            name = str(counter.get("name", "") or "")
            raw_path = str(counter.get("file", "") or "").strip()
            path = self._counter_fallback_path(index)
            if raw_path and resolves_within(self.worktree, raw_path):
                candidate = normalise(raw_path)
                # Never over an existing file: a counter-test is a new file.
                if (candidate
                        and not matches_any(candidate, self.cfg.never_touch)
                        and not matches_any(candidate, self.cfg.forbidden_files)
                        and self._is_test_path(candidate)
                        and not (self.worktree / candidate).exists()):
                    path = candidate
            target = self.worktree / path
            content = str(counter.get("content", "") or "")
            entry = {"name": name, "file": path, "content": content,
                     "outcome": "", "why": "", "status": "", "output": "",
                     "output_head": ""}
            if not content.strip():
                self.counter_summary["dropped_uncompiled"] += 1
                entry.update(outcome="dropped", why="the counter-test is empty")
            else:
                before = snapshot()
                existed = target.exists()
                previous = target.read_bytes() if existed else b""
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                    result = self._run(
                        runner_kind("test"),
                        self.pack.commands("test", unit=unit, files=[path])["argv"],
                        counted="model")
                    out, err = self._output(result)
                    parsed = self.pack.parse_tests(out, err)
                    text = out + ("\n" + err if err else "")
                    entry["output"] = text[-4000:]
                    entry["output_head"] = head_of(text)
                    # No results and no footer: the runner could not even load it
                    # (a syntax error in a module run by name, for one).
                    unloaded = (not parsed.get("results")
                                and bool(parsed.get("missing_footers"))
                                and result.cls != PASS)
                    if parsed.get("compile_errors") or result.cls == INFRA or unloaded:
                        self.counter_summary["dropped_uncompiled"] += 1
                        entry.update(
                            outcome="dropped",
                            why=("the run did not finish" if result.cls == INFRA
                                 else "it did not compile or could not be collected"))
                    else:
                        status = status_of(parsed, name)
                        died = bool(parsed.get("missing_footers"))
                        if died:
                            # The run died partway: a row it printed is not a pass.
                            status = "not-run"
                        elif status == "mixed":
                            # Any failing row of a counter-test is the finding it
                            # exists for.
                            status = ("FAILED" if any(
                                row.get("status") == "FAILED" for row in rows_for_test(
                                    parsed.get("results", []), name))
                                else "not-run")
                        entry["status"] = status
                        if status == "ok":
                            self.counter_summary["passed"] += 1
                            entry["outcome"] = "passed"
                        elif status == "FAILED":
                            self.counter_summary["failed"] += 1
                            entry["outcome"] = "failed"
                            self._last_failing_counters.append(
                                {k: entry[k] for k in
                                 ("name", "file", "status", "content", "output")})
                        else:
                            self.counter_summary["not_run"] += 1
                            entry.update(
                                outcome="not-run",
                                why=("the run died before it finished" if died
                                     else f"the runner reported no test named {name!r}"
                                     if status == "not-run"
                                     else f"the runner reported it as {status}"))
                finally:
                    if existed:
                        target.write_bytes(previous)
                    elif target.exists() or target.is_symlink():
                        target.unlink()
                    restore(before)
            write_json(self.card_layout / "gates" / f"counter-test-{index}.json",
                       entry)
            self.counter_results.append(
                {k: entry[k] for k in ("name", "file", "outcome", "why", "status",
                                       "output_head")})

    # -- step 9: catch-up -----------------------------------------------------

    def _step_catch_up(self) -> None:
        self.phase = "catch-up"
        self._check_card_unchanged()
        if self.cfg.catch_up == "off":
            self._record_gate(GateResult("catch-up", PASS, "off in this config"))
            return

        unit = self._effective_unit()
        current_base = self.git.head(self._base_ref())
        if current_base == self.base_commit:
            self.catch_up_result = "skipped: the base has not moved"
            self._record_gate(GateResult("catch-up", PASS, self.catch_up_result,
                                         {"skipped": True}))
            return

        trial = self.card_layout / "catch-up"
        if trial.exists():
            shutil.rmtree(trial, ignore_errors=True)
        self.git.worktree_add(trial, self.branch)
        self._prepare_checkout(trial)
        trial_git = Git(trial)
        try:
            merged, output = trial_git.merge_no_commit(current_base)
            if not merged:
                self.catch_up_result = "conflict with the current base"
                self._record_gate(GateResult(
                    "catch-up", FAIL, self.catch_up_result,
                    {"conflict": output[-2000:]}))
                raise GateFailure(StopCode.STALE_CONFLICT, self.catch_up_result)

            trial_target = self.card_layout / "catch-up-target"
            env = self.cfg.runner_env.get(unit, {})
            result = self.runner.exec(
                kind="test",
                argv=self.pack.commands("test", unit=unit)["argv"],
                cwd=trial, target_dir=trial_target, unit=unit,
                env=env, timeout_s=max(60.0, self.budget.wall_clock_left_s()))
            self.budget.note_fixed_build()
            out, err = self._output(result)
            parsed = self.pack.parse_tests(out, err)
            gate = suite_green(parsed, self.cfg.known_failing,
                               run_cls=result.cls, exit_code=result.exit_code)
            if gate.result == INFRA or result.cls == INFRA:
                self.catch_up_result = gate.detail or "catch-up test run did not finish"
                self._record_gate(GateResult(
                    "catch-up", INFRA, self.catch_up_result,
                    {"suite": gate.as_dict()}))
                raise Infra("catch-up-infra", self.catch_up_result)
            if gate.result != PASS:
                self.catch_up_result = "the merged result is not green"
                self._record_gate(GateResult(
                    "catch-up", FAIL, self.catch_up_result,
                    {"suite": gate.as_dict()}))
                raise GateFailure(StopCode.STALE_CONFLICT, self.catch_up_result)

            self.catch_up_result = "the trial merge is green"
            self._record_gate(GateResult("catch-up", PASS, self.catch_up_result,
                                         {"skipped": False}))
        finally:
            trial_git.abort_merge()
            self.git.worktree_remove(trial)

    # -- finishing ------------------------------------------------------------

    def _project_name(self) -> str:
        name = str(getattr(self.cfg, "name", "") or "").strip()
        if name:
            return name
        repo = getattr(self.cfg, "repo", None)
        return Path(repo).name if repo else self.worktree.name

    def _scope_markers(self) -> set[str] | None:
        """What ties a lesson to this project (`harness_scope.project_markers`).

        None when they can't be worked out; every lesson is then kept `project`.
        """
        try:
            tracked = self.git.tracked_files()
        except Exception:  # noqa: BLE001
            return None
        return project_markers(self.worktree, self._project_name(), tracked)

    def _record_harness_outcome(self, final_state: str) -> None:
        if not self._harness_enabled() or self.harness_library is None:
            return
        pre_revision = int(self.harness_revision or 0)
        try:
            model_calls = len(getattr(self.roles, "calls", ()) or ())
            isolated = (
                self.stop_code in _HARNESS_ISOLATED_STOP_CODES
                or (self.stop_code == StopCode.REFUSED and model_calls == 0)
                # The coordinator's decision, not the card's failure; and no
                # reflection call is made for a stopped card.
                or (self.stop_code == StopCode.REFUSED and bool(self.coordinator_stop))
                or final_state == "released"
            )
            if isolated:
                status = self.harness_library.status()
                post_revision = int(status.get("revision", pre_revision) or 0)
                self.harness_revision = pre_revision
                self.harness_summary = {
                    "revision": pre_revision,
                    "pre_revision": pre_revision,
                    "post_revision": post_revision,
                    "state": str(status.get("state", "confirmed")),
                    "cues_applied": list(self.harness_cues_applied),
                    "lessons_added": [],
                    "probe_transition": None,
                    "metrics": dict(status.get("metrics") or {}),
                    "skipped_reason": (
                        self.stop_code.value if self.stop_code else final_state
                    ),
                }
                return

            language, kind, tags = self._harness_query_attrs()
            attempts_count = (
                max(1, int(self.budget.implementer_attempts))
                if self.budget.implementer_attempts > 0
                else max(1, len(self.attempt_records))
            )
            card_meta = {
                "card_id": self.card.id,
                "id": self.card.id,
                "kind": kind,
                "category": self.card.category,
                "language": language,
                "unit": self._effective_unit(),
                "touches": list(self.card.touches),
                "tags": list(tags),
                "final_state": final_state,
                "stop_code": self.stop_code.value if self.stop_code else "",
                "passed": final_state == "in-review",
                "attempts": attempts_count,
                "tokens_in": self.budget.tokens_in,
                "tokens_out": self.budget.tokens_out,
                "spend": round(self.budget.spend, 6),
                "counter_tests": dict(self.counter_summary),
                "mutation": dict(self.mutation_summary),
            }
            res = self.harness_library.record_card_outcome(
                card_meta,
                self.attempt_records,
                cues_applied=list(self.harness_cues_applied),
                failure=self.detail,
                micromanager=self.micromanager,
                reflect=self._should_reflect_harness(),
                scope_markers=self._scope_markers(),
            )
            post_revision = int(res.get("revision", pre_revision) or 0)
            self.harness_revision = pre_revision
            self.harness_lessons_added = list(res.get("lessons_added") or ())
            self.harness_summary = {
                **res,
                "revision": pre_revision,
                "pre_revision": pre_revision,
                "post_revision": post_revision,
                "cues_applied": list(self.harness_cues_applied),
            }
            if self.global_library is not None:
                try:
                    share = bool(getattr(getattr(self.cfg, "harness", None), "share", True))
                    self.global_summary.update(self.harness_library.report_to_global(
                        self.global_library, self._project_name(), share=share))
                except Exception as exc:  # noqa: BLE001
                    self.global_summary["error"] = f"{type(exc).__name__}: {exc}"
            if self.global_summary:
                self.harness_summary["global"] = dict(self.global_summary)
            metrics = res.get("metrics") or {}
            eff = float(metrics.get("harness_efficiency", 0.0) or 0.0)
            self._record_gate(GateResult(
                "harness-efficiency",
                PASS,
                f"rev={pre_revision}->{post_revision} cues={len(self.harness_cues_applied)} "
                f"lessons={len(self.harness_lessons_added)} eff={eff:.3f}",
                {
                    "revision": pre_revision,
                    "pre_revision": pre_revision,
                    "post_revision": post_revision,
                    "state": str(res.get("state", "")),
                    "cues_applied": list(self.harness_cues_applied),
                    "lessons_added": list(self.harness_lessons_added),
                    "probe_transition": res.get("probe_transition"),
                    "efficiency_score": eff,
                },
            ))
        except GateFailure:
            raise
        except Exception as exc:  # noqa: BLE001 — never abort _finish() on harness errors
            self.harness_revision = pre_revision
            self.harness_summary = {
                "revision": pre_revision,
                "pre_revision": pre_revision,
                "post_revision": pre_revision,
                "cues_applied": list(self.harness_cues_applied),
                "lessons_added": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
            try:
                self._record_gate(GateResult(
                    "harness-efficiency",
                    INFRA,
                    f"harness outcome degraded ({type(exc).__name__}): {exc}",
                    {"error": str(exc)},
                ))
            except Exception:  # noqa: BLE001
                pass
            self.log(f"{self.card.id}: harness outcome degraded: {exc}")

    def _finish(self, final_state: str) -> dict:
        from .dossier import build_dossier, write_dossier

        self.budget.stop_clock()
        if not self.ended_at:
            self.ended_at = utc_now()
        try:
            self._record_harness_outcome(final_state)
        except GateFailure as exc:
            self.stop_code = exc.code
            self.detail = exc.detail
            final_state = "blocked"
        except Exception as exc:  # noqa: BLE001 — guarantee finish completes cleanly
            self.log(f"{self.card.id}: harness outcome error ignored in _finish: {exc}")
        mm_budget = getattr(self.micromanager, "budget", None)
        mm_usage = self._card_mm_usage()
        if mm_budget is not None and hasattr(mm_budget, "as_dict"):
            self.micromanager_summary = {
                **mm_budget.as_dict(),
                **mm_usage,
                "night_totals": mm_budget.as_dict(),
                "mode": getattr(self.micromanager, "mode", "off"),
                "reviewer_brief": self.reviewer_brief,
            }
        self._write_dossier(final_state)
        if self.board is not None:
            self._ensure_board_members()
            ev = [self.dossier_path] if self.dossier_path else []
            if final_state == "in-review":
                self._post_board(
                    author="gates",
                    to=["coordinator"],
                    kind="done",
                    attempt=max(1, self.attempt),
                    body=f"{self.card.id} reached in-review ({len(self.gates)} gates)",
                    evidence=ev,
                    trusted=True,
                )
            else:
                code_str = self.stop_code.value if self.stop_code else "blocked"
                self._post_board(
                    author="gates",
                    to=["coordinator"],
                    kind="blocked",
                    attempt=max(1, self.attempt),
                    body=f"{self.card.id} {final_state} ({code_str}): {(self.detail or '')[:500]}",
                    evidence=ev,
                    trusted=True,
                )

        record = self._card_record(final_state)
        write_json(self.card_layout / "card-record.json", record)
        self.telemetry.write("card", record)

        status = "blocked" if final_state == "blocked" else (
            "released" if final_state == "released" else "in-review")
        note = self._note(final_state)
        try:
            self._mover(status, note)
        except AdapterError as exc:
            self.log(f"{self.card.id}: could not move the card: {exc.message}")
        if self.harness_summary:
            return {**record, "harness": dict(self.harness_summary)}
        return record

    def _mover(self, status: str, note: str) -> None:
        mover = getattr(self, "card_source", None)
        if mover is None:
            return
        if status == "released":
            mover.release(self.card.id, self.lease_id, reason=note,
                          stop_code=self.stop_code.value if self.stop_code else "")
            return
        mover.move(self.card.id, self.lease_id, status, note_md=note,
                   stop_code=self.stop_code.value if self.stop_code else "")

    def _note(self, final_state: str) -> str:
        """The card's in-review note: the dossier itself, not a path to it.

        The spec: "the dossier goes into the card's in-review note through the card
        adapter". The reviewing lane works from the card, not from this machine's
        state folder, so a file path there would be useless to it.
        """
        from .dossier import render_markdown

        counts = {gate.name: gate.result for gate in self.gates}
        if final_state in ("blocked", "released"):
            return (f"{self.card.id}: {self.stop_code.value if self.stop_code else 'blocked'}"
                    f" — {self.detail}\n\nGates: {_gate_line(counts)}")

        header = (f"{self.card.id}: in review\n\n"
                  f"{len(self.gates)} gates, all passed. "
                  f"Attempts: {self.budget.implementer_attempts}. "
                  f"Named tests: {self.named_passing}. "
                  f"Builds: {self.budget.model_builds} model, "
                  f"{self.budget.fixed_builds} fixed, "
                  f"{self.budget.mutation_builds} mutation.\n\n")
        if self.dossier_payload is None:
            return header + f"Full dossier: {self.dossier_path}"
        # The Markdown dossier is one page and is meant to be read here.
        return header + render_markdown(self.dossier_payload)

    def _write_dossier(self, final_state: str) -> None:
        from .dossier import build_dossier, write_dossier
        payload = build_dossier(self, final_state, harness=self.harness_summary)
        self.dossier_payload = payload
        md_path, json_path = write_dossier(self.card_layout, payload)
        self.dossier_path = str(md_path)

    def _card_record(self, final_state: str) -> dict:
        from .adapters.jsonl import CARD_FIELDS, complete_record
        try:
            hash_at_end = self.card_source.hash(self.card.id)["hash"] if \
                getattr(self, "card_source", None) else self.card.hash()
        except AdapterError:
            hash_at_end = self.card.hash()

        mm_usage = self._card_mm_usage()
        record = {
            "run_id": self.run_id,
            "card_id": self.card.id,
            "card_hash_at_claim": self.card_hash_at_claim,
            "card_hash_at_end": hash_at_end,
            "category": self.card.category,
            "unit": self._effective_unit(),
            "claimed_at": self.started_at,
            "ended_at": self.ended_at,
            "final_state": final_state,
            "stop_code": self.stop_code.value if self.stop_code else "",
            "detail": (self.detail or "")[-2000:] if self.stop_code else "",
            "attempts": len(self.attempts),
            "depth": self.depth,
            "planner_used": self.planner_used,
            "builds": self.budget.builds(),
            "runner_seconds": round(self.runner_seconds, 2),
            "wall_clock_s": round(self.budget.wall_clock_s, 2),
            "tokens_in": self.budget.tokens_in,
            "tokens_out": self.budget.tokens_out,
            "spend": round(self.budget.spend, 6),
            "micromanager_calls": mm_usage["calls"],
            "micromanager_tokens_in": mm_usage["tokens_in"],
            "micromanager_tokens_out": mm_usage["tokens_out"],
            "micromanager_spend": mm_usage["spend"],
            "false_rejects_dismissed": mm_usage["false_rejects_dismissed"],
            "files_changed": len(self._changed_since_base()),
            "lines_added": line_counts(self.current_diff or "")[0],
            "lines_removed": line_counts(self.current_diff or "")[1],
            "named_tests": self.card.test_names,
            "known_failing_excluded": self.known_failing_excluded,
            "counter_tests": dict(self.counter_summary),
            "mutation": dict(self.mutation_summary),
            "findings_filed": list(self.findings_filed),
            "frontier_estimate": self.card.frontier_estimate or "",
            "dossier_path": self.dossier_path,
            "harness_revision": int(self.harness_revision or 0),
            "harness_cues_applied": list(self.harness_cues_applied),
            "harness_lessons_added": list(self.harness_lessons_added),
            "team": self.team_summary(),
            "handbacks": list(self.handbacks),
        }
        summary = getattr(self.pack, "tooling_summary", None)
        if summary is not None:
            # The runner, tools and the pack's notes this card was judged with.
            with contextlib.suppress(Exception):
                record["tooling"] = summary()
        if self.board is not None:
            record["by_effort"] = dict(self.effort_tokens)
        return complete_record(record, CARD_FIELDS)


# -- helpers -------------------------------------------------------------------

def _json_handback(content: str) -> str | None:
    """A planner's JSON-mode hand-back, `{"handback": "<reason>"}`, or None.

    An object that also carries `steps` is a plan, not a hand-back.
    """
    text = str(content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(payload, dict) or "steps" in payload:
        return None
    reason = payload.get("handback")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip()


def _tests_hash(hashes: dict[str, str]) -> str:
    """One hash for a whole set of frozen test files, so two can be compared."""
    import json
    return hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()


def _summarise(failures: list[dict]) -> str:
    return "; ".join(f"{entry['rule']}: {entry['detail']}" for entry in failures)


def _gate_line(counts: dict) -> str:
    return ", ".join(f"{name}={result}" for name, result in counts.items())


def _red(parsed: dict, known: list[str]) -> list[str]:
    known_set = set(known)
    return [entry["name"] for entry in parsed.get("results", [])
            if entry.get("status") == "FAILED" and entry.get("name") not in known_set]


def _named_rows(parsed: dict, named: list[str], files: dict[str, str] | None = None,
                row_file=None) -> dict[str, list[dict]]:
    """Every row each named test was reported with (`rows_for_test`)."""
    results = parsed.get("results", [])
    files = files or {}
    return {name: rows_for_test(results, name, files.get(name, ""), row_file)
            for name in named}


def _named_failures(parsed: dict, named: list[str],
                    files: dict[str, str] | None = None,
                    row_file=None) -> dict[str, str]:
    """One status per named test, from every row reported for it.

    `FAILED` only when every row failed and `ok` only when every row passed; rows
    that disagree (a parameter case passing while another fails, or the same test
    in two binaries) are `mixed`, which proves nothing either way. Keeping the last
    row instead let a planted or duplicate row decide the proof.
    """
    rows = _named_rows(parsed, named, files, row_file)
    return {name: combined_status(rows[name]) for name in named}


def _status_of(parsed: dict, name: str) -> str:
    """A named test's status over all its rows, as `_named_failures` reads it."""
    return combined_status(rows_for_test(parsed.get("results", []), name))


def _report_detail(report) -> str:
    parts = []
    for entry in report.refused:
        parts.append(f"{entry['path']}: {entry['message']} ({entry['code']})")
    if report.outside_touches:
        parts.append("outside touches: " + ", ".join(report.outside_touches))
    if report.not_additions_only:
        parts.append("not additions only: " + ", ".join(report.not_additions_only))
    return "; ".join(parts) or "the edit was refused"


def _split_command(command: str) -> list[str]:
    import shlex
    return shlex.split(command)
