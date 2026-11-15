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
from .gates.checks import (PASS, FAIL, INFRA, GateResult, banned_patterns_rust,
                           diff_within_touches, extra_gate_result, frozen_check,
                           ignored_or_filtered, mutation_gate,
                           paths_touched_by_extra_gate, revert_check,
                           suite_green, test_loss)
from .gates.eligibility import check_eligibility, file_is_blocked
from .git import Git
from .globs import matches_any, normalise
from .roles import (Roles, build_checker_pass1_brief, build_checker_pass2_brief,
                    build_implementer_brief, build_planner_brief,
                    build_test_writer_brief, parse_counter_tests, parse_plan,
                    parse_restatement, wants_planner)
from .state import append_jsonl, utc_now, utc_after, write_json

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
                 log=None) -> None:
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
        self.base_commit = ""
        self.branch = ""
        self.base_test_list: dict = {"tests": []}
        self.named_passing = 0
        self.diff_hashes: list[str] = []
        self.error_signatures: list[str] = []
        self.best_named_passing = 0
        #: One entry per judged attempt: how many named tests passed then.
        self.named_history: list[int] = []
        self.planner_used = False
        self.depth = 1
        self.counter_summary = {"written": 0, "dropped_uncompiled": 0,
                                "passed": 0, "failed": 0}
        self.mutation_summary = {"killed": 0, "total": 0, "capped": False}
        self.known_failing_excluded: list[str] = []
        self.findings_filed: list[str] = []
        self.dossier_path = ""
        self.current_diff = ""
        self.started_at = utc_now()
        self.checker_restatement = None
        self.catch_up_result = ""
        self.unformatted: list[str] = []
        self.existing_tests_changed: list[str] = []
        self.ended_at = ""

    # -- helpers --------------------------------------------------------------

    def _record_gate(self, gate: GateResult) -> GateResult:
        self.gates.append(gate)
        write_json(self.card_layout / "gates" / f"{len(self.gates):02d}-{gate.name}.json",
                   gate.as_dict())
        self.log(f"{self.card.id}: {gate.name} {gate.result}"
                 + (f" — {gate.detail}" if gate.detail else ""))
        return gate

    def _run(self, kind: str, argv: list[str], counted: str = "model",
             unit: str = "", timeout_s: float | None = None):
        env = self.cfg.runner_env.get(unit or self.card.unit, {})
        timeout = timeout_s
        if timeout is None:
            left = self.budget.wall_clock_left_s()
            timeout = max(30.0, left)
        result = self.runner.exec(
            kind=kind, argv=argv, cwd=self.worktree,
            target_dir=self.target_dir, unit=unit or self.card.unit,
            env=env, timeout_s=timeout)
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
            self._step_phase_b()
            self._step_end_gates()
            self._step_catch_up()
            final_state = "in-review"
            self.stop_code = None
        except Refused as exc:
            self.stop_code = StopCode.REFUSED
            self.detail = exc.message
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

        return self._finish(final_state)

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
        if not self.git.branch_exists(self.branch):
            self.git.create_branch(self.branch, self.base_commit)
        # The worktree was made by the controller and entered here; one target
        # folder per card, never shared between branches.
        self.git.checkout(self.branch, detach=False)

    def _base_ref(self) -> str:
        name = f"refs/heads/{self.cfg.base_branch}"
        return name if self.git.ref_exists(name) else "HEAD"

    # -- step 3: baseline green ----------------------------------------------

    def _step_baseline(self) -> None:
        self.phase = "baseline"
        unit = self.card.unit
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
        if first[0].cls == INFRA or second[0].cls == INFRA:
            self._record_gate(GateResult("baseline", INFRA,
                                         "a baseline run did not finish"))
            raise Infra("baseline-infra",
                        "a baseline run did not finish, so nothing can be judged")

        # A baseline that does not compile is not green, and not "no failures"
        # either. Its per-test lines are empty and its footers are missing, so
        # without this check a broken base would be cached as clean and every later
        # card at that commit would get a vacuous no-test-loss gate.
        broken = (first[1].get("compile_errors") or second[1].get("compile_errors")
                  or first[1].get("missing_footers")
                  or second[1].get("missing_footers"))
        if broken:
            detail = ("the base does not build: "
                      + str((first[1].get("compile_errors")
                             or first[1].get("missing_footers"))[:3]))
            self._record_gate(GateResult("baseline", FAIL, detail,
                                         {"compile_errors": broken}))
            raise GateFailure(StopCode.DIRTY_BASELINE, detail)

        red_first = _red(parsed=first[1], known=self.cfg.known_failing)
        red_second = _red(parsed=second[1], known=self.cfg.known_failing)
        disagree = [name for name in red_first if name not in red_second]
        disagree += [name for name in red_second if name not in red_first]

        if red_first or red_second:
            detail = "red on base: " + ", ".join(sorted(set(red_first + red_second))[:10])
            if disagree:
                detail += f"; the two runs disagreed on {len(disagree)} test(s)"
            self._record_gate(GateResult("baseline", FAIL, detail,
                                         {"red": red_first + red_second,
                                          "disagreed": disagree}))
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
        if not wants_planner(self.card):
            self.planner_used = False
            return
        brief = build_planner_brief(self.card, self.book)
        try:
            call = self.roles.call("planner", brief, response_format="json")
        except (Infra, Invalid):
            self.planner_used = False
            self.log(f"{self.card.id}: planner unavailable, running unsplit")
            return
        self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
        plan = parse_plan(call.content)
        if plan.cannot_split:
            raise GateFailure(StopCode.CANNOT_SPLIT,
                              "the planner could not split without leaving touches")
        self.planner_used = True
        write_json(self.card_layout / "plan.json", plan.as_dict())

    # -- step 5: phase A, the test writer ------------------------------------

    def _step_phase_a(self) -> None:
        self.phase = "test-writer"
        if not self.card.test_names and self.card.category == "mechanical":
            # A mechanical card is a compiler-guided edit: there is no new test to
            # write, and the existing suite is the judge. Freeze the tests as they
            # are, so the freeze and revert checks still mean what they say.
            self._freeze()
            self.frozen_commit = self._commit(
                f"{self.card.id}: freeze the existing tests")
            self._record_gate(GateResult(
                "test-writer", PASS,
                "skipped: a mechanical card names no new tests"))
            return

        last_failure = ""
        for attempt in range(1, self.cfg.test_writer_attempts + 1):
            # Every test-writer attempt starts from the frozen base, so a refused or
            # partial answer cannot leave an edit behind that the next attempt then
            # gets committed.
            self.git.reset_hard(self._base_ref())
            self.budget.take_test_writer_attempt()
            brief = build_test_writer_brief(
                self.card, self.book, self.pack, self.conventions,
                attempt=attempt, previous_failure=last_failure)
            call = self.roles.call("test_writer", brief, attempt=attempt)
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
            write_json(self.card_layout / "responses" / f"A{attempt}-test-writer.json",
                       call.as_dict())

            try:
                edits = parse_edits(call.content)
            except Invalid as exc:
                last_failure = exc.message
                self._record_attempt("test-writer", "refused",
                                     StopCode.TESTS_UNPROVABLE, role="test_writer")
                continue

            report = apply_edits(
                self.worktree, edits, self._test_writer_paths(),
                self.cfg.never_touch, self.cfg.forbidden_files,
                additions_only=set(self.card.touches) if self.card.new_symbols else set(),
                allow_stubs=self.card.new_symbols,
                stub_inserter=self._stub_inserter(),
                production_paths=set(self.card.touches),
                forbid_production=not self.card.new_symbols)
            write_json(self.card_layout / "edits" / f"A{attempt}.json",
                       report.as_dict())

            if not report.ok:
                last_failure = _report_detail(report)
                self._record_attempt("test-writer", "refused",
                                     StopCode.TESTS_UNPROVABLE, role="test_writer")
                if report.outside_touches or report.not_additions_only:
                    raise GateFailure(
                        StopCode.REFUSED,
                        "the test writer left the allowed paths: "
                        + ", ".join(report.outside_touches or report.not_additions_only))
                # A refusal the model could fix, such as a search block that matched
                # twice, gets the next attempt. Anything else is the card's problem
                # and stops it, rather than being retried into `tests-unprovable`.
                retryable = {entry["code"] for entry in report.refused}
                if not retryable <= {"no-unique-match"}:
                    raise GateFailure(
                        StopCode.REFUSED,
                        "the test writer's answer was refused: "
                        + _report_detail(report))
                continue

            # Freeze, then commit: an attempt starts from a clean tree, so the
            # frozen tests must be in the commit or `reset --hard` would delete them.
            self._freeze()
            self.frozen_commit = self._commit(f"{self.card.id}: freeze the tests")
            self._record_attempt("test-writer", "passed-gates", role="test_writer")
            return

        raise GateFailure(StopCode.TESTS_UNPROVABLE,
                          "the test writer produced no usable edit in "
                          f"{self.cfg.test_writer_attempts} attempts: {last_failure}")

    # -- step 6: named tests exist, and fail before the code ------------------

    def _step_prove_tests(self) -> None:
        self.phase = "prove"
        unit = self.card.unit

        list_result = self._run(runner_kind("list"),
                                self.pack.commands("list", unit=unit)["argv"],
                                counted="fixed")
        out, err = self._output(list_result)
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
            self._record_gate(GateResult(
                "fail-before", PASS,
                "skipped for test-backfill: the new tests pass on base by design"))
            return

        if not self.card.test_names:
            # A mechanical card is judged by the existing suite, which the baseline
            # and no-test-loss gates already cover. There is nothing new to prove
            # red, and running the whole suite again here would only spend a build.
            self._record_gate(GateResult(
                "fail-before", PASS,
                f"skipped: a {self.card.category} card names no new tests, so the "
                f"existing suite is the judge"))
            return

        base = self._base_ref()
        rerun = self._run(runner_kind("test"), self.pack.commands("test", unit=unit)["argv"]
                          + ["--", *self.card.test_names], counted="fixed")
        out, err = self._output(rerun)
        parsed = self.pack.parse_tests(out, err)
        failures = _named_failures(parsed, self.card.test_names)
        compile_errors = parsed.get("compile_errors", [])

        if parsed.get("missing_footers") or rerun.cls == INFRA:
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
                "so the test writer may append stubs, or test existing items.")

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

        self._record_gate(GateResult(
            "fail-before", PASS,
            f"{len(self.card.test_names)} named test(s) failed at runtime on base",
            {"failures": failures}))

    # -- step 7: phase B, the implementer ------------------------------------

    def _step_phase_b(self) -> None:
        self.phase = "implement"
        last_failure = ""
        counter_tests = ""
        # Bounded by a free-slots budget rather than by the config number, so a
        # malformed or unmatched answer really does spend no attempt: the loop can
        # make more model calls than the attempt cap, but only that many attempts are
        # counted and only they can judge the card. `free_retries` bounds the
        # no-attempt path, because "spends no attempt" must not mean "loops forever".
        free_calls = self.cfg.implementer_attempts
        free_retries = self.cfg.implementer_attempts

        while free_calls > 0:
            stashed = self._stash_diff()
            free_calls -= 1
            attempt_no = self.budget.take_implementer_attempt()
            self._heartbeat()
            self._begin_attempt(attempt_no)

            # A retry with no edits to show would leave the model with nothing, so
            # the first attempt's shape is used whenever the tree is clean.
            showing_diff = bool(stashed["diff"])
            brief = build_implementer_brief(
                self.card, self.book, self._frozen_texts(),
                attempt=attempt_no if showing_diff else 1,
                diff=stashed["diff"],
                failure=last_failure, counter_tests=counter_tests)
            write_json(self.card_layout / "briefs" / f"{attempt_no}-implementer.md",
                       {"brief": brief.body})

            call = self.roles.call("implementer", brief, attempt=attempt_no)
            self.budget.note_tokens(call.tokens_in, call.tokens_out, call.spend)
            write_json(self.card_layout / "responses" / f"{attempt_no}-implementer.json",
                       call.as_dict())

            try:
                edits = parse_edits(call.content)
            except Invalid as exc:
                # A malformed answer spends no attempt and no build, but it does
                # spend one free retry: otherwise a model that never produces a
                # block would loop until the token cap.
                last_failure = exc.message
                self.budget.implementer_attempts -= 1
                self._record_attempt("implement", "refused",
                                     role="implementer")
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
                # A refused edit spends no attempt and no build.
                if all(e["code"] == "no-unique-match" for e in report.refused):
                    last_failure = _report_detail(report)
                    self.budget.implementer_attempts -= 1
                    self._record_attempt("implement", "refused",
                                         role="implementer")
                    free_retries -= 1
                    if free_retries <= 0:
                        raise GateFailure(
                            StopCode.ATTEMPTS,
                            "the implementer's search blocks kept matching more "
                            f"than once, after {self.cfg.implementer_attempts} "
                            f"tries: {last_failure}")
                    free_calls += 1
                    continue
                raise GateFailure(StopCode.REFUSED, _report_detail(report))
            if report.outside_touches:
                raise GateFailure(
                    StopCode.REFUSED,
                    "the implementer edited outside touches: "
                    + ", ".join(report.outside_touches))

            outcome = self._judge_attempt(attempt_no, report)
            if outcome is None:
                return
            last_failure = outcome

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
        return self.frozen_commit if hasattr(self, "frozen_commit") else self._base_ref()

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
                        role: str = "", started: str = "") -> None:
        """Write one attempt record, in the fields the spec fixes."""
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
            "attempt": self.budget.implementer_attempts
            if phase in ("implement", "fix") else self.budget.test_writer_attempts,
            "phase": phase,
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
            "runner_seconds": 0,
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
        from .packs.rust import added_lines_from_diff
        added = added_lines_from_diff(diff["diff"])
        production = {path for path in changed
                      if self.pack.classify_paths([path]).get(path) == "production"}
        banned = self._record_gate(banned_patterns_rust(added, self.cfg, production))
        if not banned.ok:
            return banned.detail

        frozen_gate = self._record_gate(
            frozen_check(self.frozen_tests, self._current_test_hashes()))
        if not frozen_gate.ok:
            raise GateFailure(StopCode.REFUSED, frozen_gate.detail)

        # The suite and lint a model's edit caused. These count against the cap.
        unit = self.card.unit
        test_result = self._run(runner_kind("test"),
                                self.pack.commands("test", unit=unit)["argv"])
        out, err = self._output(test_result)
        parsed = self.pack.parse_tests(out, err)
        suite = self._record_gate(suite_green(parsed, self.cfg.known_failing))
        passing = sum(1 for name in self.card.test_names
                      if _status_of(parsed, name) == "ok")
        self.named_passing = passing
        self.best_named_passing = max(self.best_named_passing, passing)
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

        signature = self.pack.error_signature(lint_err + err)["signature"]
        self.error_signatures.append(signature)

        if suite.ok and lint_gate.ok:
            all_named = all(_status_of(parsed, name) == "ok"
                            for name in self.card.test_names)
            if all_named:
                self._commit(f"{self.card.id}: make the frozen tests pass")
                self._record_attempt("implement", "passed-gates", diff=diff)
                return None
            detail = ("named tests still failing: " + ", ".join(
                name for name in self.card.test_names
                if _status_of(parsed, name) != "ok"))
            # The suite can be green while a named test is still red, so the progress
            # signals have to be checked on this path too.
            self._check_progress()
            self._record_attempt("fix", "failed-gates", diff=diff)
            return detail

        self._check_progress()
        detail = (suite.detail or "") + (
            " | " + lint_gate.detail if lint_gate.detail else "")
        self._record_attempt("fix", "failed-gates", diff=diff)
        return detail

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

    def _current_test_hashes(self) -> dict[str, str]:
        hashes = {}
        for path in self.frozen_tests:
            full = self.worktree / path
            if full.exists():
                hashes[path] = hashlib.sha256(full.read_bytes()).hexdigest()
        return hashes

    # -- step 8: the end gates ------------------------------------------------

    def _step_end_gates(self) -> None:
        self.phase = "check"
        unit = self.card.unit
        changed = self._changed_since_base()
        full_diff = diff_text(self.worktree, changed,
                              ref=self.base_commit or "HEAD")
        self.current_diff = full_diff
        added, removed = line_counts(full_diff)

        # pass-after: the named tests, twice, per the spec's flakiness rule. A card
        # that names no tests has nothing to pass here; the suite gate below is its
        # judge.
        named_ok = True
        if self.card.test_names:
            for index in (1, 2):
                result = self._run(runner_kind("test"),
                                   self.pack.commands("test", unit=unit)["argv"]
                                   + ["--", *self.card.test_names], counted="fixed")
                out, err = self._output(result)
                parsed = self.pack.parse_tests(out, err)
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

        # Suite and lint, then workspace lint.
        suite_result = self._run(runner_kind("test"),
                                 self.pack.commands("test", unit=unit)["argv"],
                                 counted="fixed")
        out, err = self._output(suite_result)
        parsed = self.pack.parse_tests(out, err)
        suite = self._record_gate(suite_green(parsed, self.cfg.known_failing))
        if suite.result == INFRA:
            raise Infra("suite-infra", suite.detail)
        if not suite.ok:
            raise GateFailure(StopCode.ATTEMPTS, suite.detail)

        self._format_touched(changed)

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

        # Banned patterns again over the card's whole diff. The per-attempt scan only
        # sees the implementer's edit, so without this the test writer's added lines
        # would never be scanned.
        from .packs.rust import added_lines_from_diff
        whole_added = added_lines_from_diff(full_diff)
        production_all = {
            path for path in changed
            if self.pack.classify_paths([path]).get(path) == "production"}
        final_banned = self._record_gate(
            banned_patterns_rust(whole_added, self.cfg, production_all))
        if not final_banned.ok:
            raise GateFailure(StopCode.REFUSED, final_banned.detail)

        # No test loss, against the list taken on base.
        final_list_result = self._run(
            runner_kind("list"),
            self.pack.commands("list", unit=unit)["argv"], counted="fixed")
        list_out, list_err = self._output(final_list_result)
        final_list = self.pack.parse_list(list_out)
        # Section 3 of the dossier names every test left out of the counts, so a
        # reader can see what is hidden rather than take a number on trust.
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
            self._record_gate(revert_check(
                self.frozen_hash, _tests_hash(current)))

        self._mutation_gate(changed)

        # Checker pass 1, then pass 2 with counter-tests.
        self._checker(changed, full_diff)

        self.existing_tests_changed = [
            path for path in changed
            if self._is_test_path(path) and path not in self.frozen_tests
            and path not in self.card.test_edits]
        write_json(self.card_layout / "gates" / "summary.json",
                   {"gates": [g.as_dict() for g in self.gates],
                    "files_changed": changed, "lines_added": added,
                    "lines_removed": removed})

    def _format_touched(self, changed: list[str]) -> None:
        """Format only files in touches that were format-clean at base.

        A file that wasn't clean at base is left alone and named in the dossier,
        because formatting it could change lines the card never touched.
        """
        candidates = [path for path in changed if path.endswith(".rs")
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
            self.log(f"{self.card.id}: rustfmt could not format {len(clean)} file(s)")
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
        for extra in self.cfg.extra_gates:
            if not paths_touched_by_extra_gate(changed, extra.paths):
                continue
            argv = _split_command(extra.run)
            result = self._run(runner_kind("extra"), argv, counted="fixed")
            out, err = self._output(result)
            passed = result.cls == PASS
            gate = self._record_gate(extra_gate_result(
                extra.name, passed, extra.on_fail,
                "" if passed else (err or out).strip()[-500:]))
            if not passed:
                if extra.on_fail == "ineligible":
                    raise GateFailure(StopCode.INELIGIBLE, gate.detail)
                raise GateFailure(StopCode.EXTRA_GATE, gate.detail)

    def _mutation_gate(self, changed: list[str]) -> None:
        mode = self.cfg.mutation
        if mode == "off":
            self._record_gate(GateResult("mutation", PASS, "off in this config"))
            return
        breaking = (self.card.category in self.cfg.mutation_required_for
                    or mode == "required")
        production = [path for path in changed
                      if self.pack.classify_paths([path]).get(path) == "production"]
        if not production:
            self._record_gate(GateResult(
                "mutation", PASS, "no production lines changed"))
            return

        try:
            argv = self.pack.commands("mutation", files=production)["argv"]
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
            self._record_gate(GateResult(
                "mutation", FAIL if breaking else PASS,
                "the mutation tool reported an infra error"))
            if breaking:
                raise Infra("mutation-infra", "the mutation tool failed")
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
            "killed": parsed.get("killed", 0), "total": parsed.get("total", 0),
            "capped": parsed.get("capped", False) or self.budget.mutation_capped()}
        gate = self._record_gate(mutation_gate(
            parsed, self.cfg.mutation_min_kill_rate, mode, self.card.category))
        if not gate.ok and breaking:
            code = StopCode.MUTATION_CAP if parsed.get("capped") else \
                StopCode.TESTS_UNPROVABLE
            raise GateFailure(code, gate.detail)

    def _checker(self, changed: list[str], full_diff: str) -> None:
        test_diff = diff_text(
            self.worktree,
            [path for path in changed if self._is_test_path(path)])
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
        self.counter_summary["written"] = len(counters.tests)
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
            raise GateFailure(
                StopCode.CHECKER_REJECT,
                f"{self.counter_summary['failed']} counter-test(s) fail on the diff")

    def _run_counter_tests(self, tests: list[dict]) -> None:
        """Run the checker's counter-tests in this card's own worktree and target.

        Never committed. One that doesn't compile is dropped and counted, because
        a counter-test can itself be wrong.
        """
        for index, counter in enumerate(tests, start=1):
            path = (counter.get("file")
                    or f"tests/counter_test_{index}.rs")
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
                    self.pack.commands("test", unit=self.card.unit)["argv"]
                    + ["--test", path],
                    counted="model")
                out, err = self._output(result)
                parsed = self.pack.parse_tests(out, err)
                if parsed.get("compile_errors") or result.cls == INFRA:
                    self.counter_summary["dropped_uncompiled"] += 1
                    continue
                status = _status_of(parsed, counter.get("name", ""))
                if status == "ok":
                    self.counter_summary["passed"] += 1
                else:
                    self.counter_summary["failed"] += 1
                    write_json(self.card_layout / "gates"
                               / f"counter-test-{index}.json",
                               {"name": counter.get("name"), "status": status,
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

        # The base as it is now, which is what the frontier lane would merge into.
        # `base_commit` is where the card's branch started, so the two only differ
        # when someone landed work while the card ran.
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
            env = self.cfg.runner_env.get(self.card.unit, {})
            result = self.runner.exec(
                kind="test",
                argv=self.pack.commands("test", unit=self.card.unit)["argv"],
                cwd=trial, target_dir=trial_target, unit=self.card.unit,
                env=env, timeout_s=max(60.0, self.budget.wall_clock_left_s()))
            self.budget.note_fixed_build()
            out, err = self._output(result)
            parsed = self.pack.parse_tests(out, err)
            gate = suite_green(parsed, self.cfg.known_failing)
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

    def _finish(self, final_state: str) -> dict:
        from .dossier import build_dossier, write_dossier

        self.budget.stop_clock()
        if not self.ended_at:
            self.ended_at = utc_now()
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
        counts = {gate.name: gate.result for gate in self.gates}
        if final_state == "blocked" or final_state == "released":
            return (f"{self.card.id}: {self.stop_code.value if self.stop_code else 'blocked'}"
                    f" — {self.detail}\n\nGates: {_gate_line(counts)}")
        return (f"{self.card.id}: in review\n\n"
                f"{len(self.gates)} gates, all passed. "
                f"Attempts: {self.budget.implementer_attempts}. "
                f"Named tests: {self.named_passing}. "
                f"Builds: {self.budget.model_builds} model, "
                f"{self.budget.fixed_builds} fixed, "
                f"{self.budget.mutation_builds} mutation.\n\n"
                f"Full dossier: {self.dossier_path}")

    def _write_dossier(self, final_state: str) -> None:
        from .dossier import build_dossier, write_dossier
        payload = build_dossier(self, final_state)
        md_path, json_path = write_dossier(self.card_layout, payload)
        self.dossier_path = str(md_path)

    def _card_record(self, final_state: str) -> dict:
        from .adapters.jsonl import CARD_FIELDS, complete_record
        try:
            hash_at_end = self.card_source.hash(self.card.id)["hash"] if \
                getattr(self, "card_source", None) else self.card.hash()
        except AdapterError:
            hash_at_end = self.card.hash()

        record = {
            "run_id": self.run_id,
            "card_id": self.card.id,
            "card_hash_at_claim": self.card_hash_at_claim,
            "card_hash_at_end": hash_at_end,
            "category": self.card.category,
            "unit": self.card.unit,
            "claimed_at": self.started_at,
            "ended_at": self.ended_at,
            "final_state": final_state,
            "stop_code": self.stop_code.value if self.stop_code else "",
            "attempts": len(self.attempts),
            "depth": self.depth,
            "planner_used": self.planner_used,
            "builds": self.budget.builds(),
            "runner_seconds": 0.0,
            "wall_clock_s": round(self.budget.wall_clock_s, 2),
            "tokens_in": self.budget.tokens_in,
            "tokens_out": self.budget.tokens_out,
            "spend": round(self.budget.spend, 6),
            "files_changed": len(self._changed_since_base()),
            "lines_added": 0,
            "lines_removed": 0,
            "named_tests": self.card.test_names,
            "known_failing_excluded": self.known_failing_excluded,
            "counter_tests": dict(self.counter_summary),
            "mutation": dict(self.mutation_summary),
            "findings_filed": list(self.findings_filed),
            "frontier_estimate": self.card.frontier_estimate or "",
            "dossier_path": self.dossier_path,
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
