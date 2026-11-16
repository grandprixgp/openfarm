"""The per-card flow: nine steps, five exits to blocked.

Code moves the card at every arrow. A failed gate sends the card back to the
implementer while attempts remain, with only the diff and the failure output.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .briefs import BriefBook
from .budget import Budget, BudgetExceeded, NightBudget
from .cards import Card
from .edits import (apply_edits, diff_text, line_counts, parse_edits,
                    touched_paths)
from .errors import (AdapterError, GateFailure, Infra, Invalid, Refused,
                     StopCode, Unsupported)
from .gates.checks import (PASS, FAIL, INFRA, GateResult, banned_patterns,
                           diff_within_touches, extra_gate_result, frozen_check,
                           _code_of,
                           ignored_or_filtered, mutation_gate,
                           paths_touched_by_extra_gate, revert_check,
                           suite_green, test_loss)
from .gates.eligibility import (check_eligibility, derive_cargo_unit,
                                file_is_blocked)
from .git import Git, GitRefused
from .globs import matches_any, normalise, resolves_within
from .harness_memory import (GuidanceSelection, HarnessLibrary, _classify_failure,
                             _is_unsafe_path)
from .micromanager import open_micromanager
from .roles import (Plan, Roles, parse_answer_edits, build_checker_pass1_brief,
                    build_checker_pass2_brief, build_implementer_brief,
                    build_planner_brief, build_test_writer_brief,
                    parse_counter_tests, parse_plan, parse_restatement,
                    wants_planner)
from .state import append_jsonl, utc_now, utc_after, write_json

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
                 harness_library: HarnessLibrary | None = None) -> None:
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

        self._owns_micromanager = micromanager is None
        if micromanager is None:
            self.micromanager = open_micromanager(
                cfg, self.card_layout, self.worktree,
                worktree=self.worktree, card_dir=self.card_layout,
            )
        else:
            self.micromanager = micromanager
            if hasattr(self.micromanager, "card_dir"):
                self.micromanager.card_dir = self.card_layout
        if hasattr(self.micromanager, "harness_context"):
            self.micromanager.harness_context = {}
        mm_budget = getattr(self.micromanager, "budget", None)
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
        return {
            "calls": max(0, int(getattr(mm_budget, "calls", 0) or 0) - int(start.get("calls", 0))),
            "tokens_in": max(
                0, int(getattr(mm_budget, "tokens_in", 0) or 0) - int(start.get("tokens_in", 0))
            ),
            "tokens_out": max(
                0, int(getattr(mm_budget, "tokens_out", 0) or 0) - int(start.get("tokens_out", 0))
            ),
            "spend": round(
                max(0.0, float(getattr(mm_budget, "spend", 0.0) or 0.0) - float(start.get("spend", 0.0))),
                6,
            ),
            "false_rejects_dismissed": max(
                0,
                int(getattr(mm_budget, "false_rejects_dismissed", 0) or 0)
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
        return gate

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
        if hasattr(self.pack, "configure"):
            self.pack.configure(self.worktree)
        self.effective_unit = self.card.unit or derive_cargo_unit(
            self.card.touches, self.worktree
        )

    def _base_ref(self) -> str:
        name = f"refs/heads/{self.cfg.base_branch}"
        return name if self.git.ref_exists(name) else "HEAD"

    # -- step 3: baseline green ----------------------------------------------

    def _step_baseline(self) -> None:
        self.phase = "baseline"
        unit = self._effective_unit()
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
            try:
                decision = self.micromanager.shape_and_plan(
                    self.card, sigs, ctx_contents, known_tests=known_tests, cfg=self.cfg
                )
            except (Refused, Invalid, ValueError) as exc:
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
        brief = build_planner_brief(
            self.card, self.book, harness_guidance=planner_harness
        )
        try:
            call = self.roles.call("planner", brief, response_format="json")
        except (Infra, Invalid):
            self.planner_used = False
            self.log(f"{self.card.id}: planner unavailable, running unsplit")
            return
        self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
        try:
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
        while free_sends > 0:
            free_sends -= 1
            attempt += 1
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
            brief = build_test_writer_brief(
                self.card, self.book, self.pack, self.conventions,
                attempt=attempt, previous_failure=last_failure,
                guidance=tw_guidance, context_files=self.extra_context_files,
                harness_guidance=tw_harness)
            call = self.roles.call("test_writer", brief, attempt=attempt)
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
            write_json(self.card_layout / "responses" / f"A{attempt}-test-writer.json",
                       call.as_dict())

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
                if not retryable <= {"no-unique-match"}:
                    raise GateFailure(
                        StopCode.REFUSED,
                        "the test writer's answer was refused: "
                        + _report_detail(report))
                self.budget.test_writer_attempts -= 1
                free_retries -= 1
                if free_retries <= 0:
                    raise GateFailure(
                        StopCode.TESTS_UNPROVABLE,
                        "the test writer's search blocks kept matching more "
                        f"than once in {self.cfg.test_writer_attempts} attempts: "
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
                if self.micromanager.enabled:
                    self._refresh_micromanager_harness_context(
                        "test_writer",
                        failure_class=_classify_failure(failure=last_failure),
                    )
                    try:
                        advice = self.micromanager.coach(
                            self.card, "test-writer", attempt,
                            self._working_test_texts(), "", last_failure, cfg=self.cfg)
                    except (Refused, Invalid, ValueError) as mm_exc:
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
        try:
            scaffold_edits = self.micromanager.scaffold_contract(
                self.card, base_files, sigs,
                never_touch=self.cfg.never_touch,
                forbidden=self.cfg.forbidden_files,
            )
        except (Refused, Invalid, ValueError) as exc:
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

        listed = {t["name"] for t in listing.get("tests", [])}
        if not self.card.test_names:
            self._record_gate(GateResult(
                "named-tests-exist", PASS,
                f"skipped: a {self.card.category} card names no new tests"))
            return
        missing = [name for name in self.card.test_names if name not in listed]
        if missing:
            self._record_gate(GateResult(
                "named-tests-exist", FAIL,
                "not reported by the test binary: " + ", ".join(missing[:10]),
                {"missing": missing}))
            raise GateFailure(StopCode.TESTS_UNPROVABLE,
                              "the test binary does not list: "
                              + ", ".join(missing[:10]))
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
        failures = _named_failures(parsed, self.card.test_names)
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

        # `ignored` is not proof: an ignored test never ran on base, so it proves
        # nothing about what the code does.
        wrong = [name for name, status in failures.items()
                 if status != "FAILED"]
        if wrong:
            self._record_gate(GateResult(
                "fail-before", FAIL,
                "these named tests do not fail at runtime on base, so they assert "
                "nothing: " + ", ".join(wrong[:10]), {"passing_on_base": wrong}))
            raise GateFailure(
                StopCode.TESTS_UNPROVABLE,
                "named tests do not fail on base: " + ", ".join(wrong[:10]))

        self.failed_on_base_by_test = dict(failures)
        self._record_gate(GateResult(
            "fail-before", PASS,
            f"{len(self.card.test_names)} named test(s) failed at runtime on base",
            {"failures": failures}))

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

        while free_calls > 0:
            uncommitted = self._diff_against_head_before_edits()
            if uncommitted["diff"]:
                last_failed_diff = uncommitted["diff"]
            free_calls -= 1
            self.budget.take_implementer_attempt()
            attempt_seq += 1
            attempt_no = attempt_seq
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
            brief = build_implementer_brief(
                self.card, self.book, self._frozen_texts(),
                attempt=max(2, attempt_no) if showing_diff else 1,
                diff=last_failed_diff,
                failure=last_failure, counter_tests=counter_tests,
                step=step,
                guidance=guidance,
                step_context="\n".join(self.step_summaries),
                context_files=self.extra_context_files,
                harness_guidance=impl_harness)
            write_json(self.card_layout / "briefs" / f"{attempt_no}-implementer.md",
                       {"brief": brief.body})

            call = self.roles.call("implementer", brief, attempt=attempt_no)
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
            write_json(self.card_layout / "responses" / f"{attempt_no}-implementer.json",
                       call.as_dict())

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
                            "the implementer's search blocks kept matching more "
                            f"than once, after {self.cfg.implementer_attempts} "
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
        lint_gate = self._record_gate(
            GateResult("lint", PASS if lint_result.cls == PASS else
                       (INFRA if lint_result.cls == INFRA else FAIL),
                       "" if lint_result.cls == PASS else
                       (lint_err or lint_out).strip()[-800:]))
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
                try:
                    advice = self.micromanager.coach(
                        self.card, "implementer", attempt_no,
                        self._frozen_texts(), diff.get("diff", ""), detail,
                        cfg=self.cfg)
                except (Refused, Invalid, ValueError) as mm_exc:
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
                self.head_status_by_test = _named_failures(parsed, self.card.test_names)
                gate = self._record_gate(
                    ignored_or_filtered(parsed, self.card.test_names))
                named_ok = named_ok and gate.ok
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
            ws_gate = self._record_gate(GateResult(
                "workspace-lint", PASS if ws.cls == PASS else
                (INFRA if ws.cls == INFRA else FAIL),
                "" if ws.cls == PASS else (ws_err or ws_out).strip()[-800:]))
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
                    r_failures = _named_failures(r_parsed, self.card.test_names)
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
                            {"compile_errors": r_compile, "not_failing": wrong}))
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
            except (Refused, Invalid, ValueError) as exc:
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
            parsed = self.pack.parse_mutation(out)
        except Invalid as exc:
            # A tool that is not installed, or prints nothing, is not a finding
            # about the code. In report mode that is recorded and the card carries
            # on; when mutation is this card's only proof, it blocks.
            self._record_gate(GateResult(
                "mutation", FAIL if breaking else PASS,
                f"the mutation tool's output could not be read: {exc.message}"))
            if breaking:
                raise GateFailure(
                    StopCode.MUTATION_CAP,
                    "mutation is this card's only proof and its output could not "
                    "be read") from exc
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
        brief1 = build_checker_pass1_brief(self.card, test_diff)
        call1 = self.roles.call("checker_pass1", brief1, attempt=1)
        self.budget.note_tokens(call1.tokens_in, call1.tokens_out, call1.spend)
        write_json(self.card_layout / "responses" / "checker-pass1.json",
                   call1.as_dict())
        restatement = parse_restatement(call1.content, self.card)
        self.checker_restatement = restatement
        gate1 = self._record_gate(GateResult(
            "checker-pass1", PASS,
            "restatement flagged as drifting" if restatement.mismatch
            else "restatement matches the named tests",
            {"restatement": restatement.as_dict()}))

        modified = {}
        for path in changed[:8]:
            full = self.worktree / path
            if full.exists():
                modified[path] = full.read_text()[:8000]
        brief2 = build_checker_pass2_brief(
            self.card, full_diff, [g.as_dict() for g in self.gates], modified)
        call2 = self.roles.call("checker_pass2", brief2, attempt=2)
        self.budget.note_tokens(call2.tokens_in, call2.tokens_out, call2.spend)
        write_json(self.card_layout / "responses" / "checker-pass2.json",
                   call2.as_dict())

        counters = parse_counter_tests(call2.content)
        self.counter_summary = {
            "written": len(counters.tests),
            "dropped_uncompiled": 0,
            "passed": 0,
            "failed": 0,
            "not_run": 0,
        }
        self._last_failing_counters = []
        if counters.tests:
            self._run_counter_tests(counters.tests)

        self._record_gate(GateResult(
            "checker-pass2", PASS,
            f"traceability returned; {len(counters.tests)} counter-test(s), "
            f"{self.counter_summary['failed']} failing",
            {"counter_tests": self.counter_summary}))
        self._record_attempt("checker", "passed-gates", role="checker_pass2")

        if (self.cfg.counter_tests == "reject"
                and self.counter_summary["failed"] > 0):
            fix_hint = ""
            if self.micromanager.enabled:
                self._refresh_micromanager_harness_context(
                    "checker", failure_class="counter-test"
                )
                try:
                    verdict = self.micromanager.adjudicate_checker(
                        self.card, full_diff, restatement,
                        self._last_failing_counters,
                    )
                except (Refused, Invalid, ValueError) as exc:
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

    def _extension(self) -> str:
        """The pack's source extension; `.rs` for packs that predate the field."""
        return str(getattr(self.pack, "extension", "") or ".rs")

    def _counter_fallback_path(self, index: int) -> str:
        unit = self._effective_unit()
        ext = self._extension()
        # Python test files are found by their `test_` prefix.
        stem = (f"test_openfarm_counter_{index}" if ext == ".py"
                else f"openfarm_counter_{index}")
        if unit and unit != "." and (self.worktree / unit).is_dir() and any(
                (self.worktree / unit / manifest).is_file()
                for manifest in ("Cargo.toml", "pyproject.toml", "setup.py")):
            return f"{unit.strip('/')}/tests/{stem}{ext}"
        return f"tests/{stem}{ext}"

    def _run_counter_tests(self, tests: list[dict]) -> None:
        """Run the checker's counter-tests in this card's own worktree and target.

        Never committed. One that doesn't compile is dropped and counted, because
        a counter-test can itself be wrong.
        """
        unit = self._effective_unit()
        for index, counter in enumerate(tests, start=1):
            raw_path = str(counter.get("file", "") or "").strip()
            path = self._counter_fallback_path(index)
            if raw_path and resolves_within(self.worktree, raw_path):
                candidate = normalise(raw_path)
                if (candidate
                        and not matches_any(candidate, self.cfg.never_touch)
                        and not matches_any(candidate, self.cfg.forbidden_files)
                        and self._is_test_path(candidate)):
                    path = candidate
            target = self.worktree / path
            content = counter.get("content", "")
            if not content:
                self.counter_summary["dropped_uncompiled"] += 1
                continue
            existed = target.exists()
            previous = target.read_text() if existed else ""
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                result = self._run(
                    runner_kind("test"),
                    self.pack.commands("test", unit=unit, files=[path])["argv"],
                    counted="model")
                out, err = self._output(result)
                parsed = self.pack.parse_tests(out, err)
                if parsed.get("compile_errors") or result.cls == INFRA:
                    self.counter_summary["dropped_uncompiled"] += 1
                    continue
                status = _status_of(parsed, counter.get("name", ""))
                if status == "ok":
                    self.counter_summary["passed"] += 1
                elif status == "FAILED":
                    self.counter_summary["failed"] += 1
                    detail_entry = {
                        "name": counter.get("name"),
                        "file": path,
                        "status": status,
                        "content": content,
                        "output": (out + err)[-4000:],
                    }
                    self._last_failing_counters.append(detail_entry)
                    write_json(self.card_layout / "gates"
                               / f"counter-test-{index}.json",
                               detail_entry)
                else:
                    self.counter_summary["not_run"] += 1
                    write_json(self.card_layout / "gates"
                               / f"counter-test-{index}.json",
                               {"name": counter.get("name"), "file": path,
                                "status": status,
                                "output": (out + err)[-4000:]})
            finally:
                if existed:
                    target.write_text(previous)
                elif target.exists():
                    target.unlink()
                self.git.reset_hard("HEAD")

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

    def _record_harness_outcome(self, final_state: str) -> None:
        if not self._harness_enabled() or self.harness_library is None:
            return
        pre_revision = int(self.harness_revision or 0)
        try:
            model_calls = len(getattr(self.roles, "calls", ()) or ())
            isolated = (
                self.stop_code in _HARNESS_ISOLATED_STOP_CODES
                or (self.stop_code == StopCode.REFUSED and model_calls == 0)
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
        }
        return complete_record(record, CARD_FIELDS)


# -- helpers -------------------------------------------------------------------

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


def _named_failures(parsed: dict, named: list[str]) -> dict[str, str]:
    by_name = {entry["name"]: entry["status"]
               for entry in parsed.get("results", [])}
    return {name: by_name.get(name, "not-run") for name in named}


def _status_of(parsed: dict, name: str) -> str:
    for entry in parsed.get("results", []):
        if entry.get("name") == name:
            return entry.get("status", "")
    return "not-run"


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
