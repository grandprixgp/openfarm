"""v0.20 Wave 1 regression tests for pipeline, gates, and language packs."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfarm_core import acceptance, blocked, dag, edits, invariants, mutate, preflight, tryit
from openfarm_core.cards import Card, TestRef
from openfarm_core.config import Config
from openfarm_core.errors import GateFailure, Invalid, Refused, StopCode
from openfarm_core.flow import CardRun, _status_of
from openfarm_core.git import Git
from openfarm_core.micromanager import Micromanager, MicromanagerBudget, _is_test_path as mm_is_test_path
from openfarm_core.packs.go import GoPack, valid_go_scaffold
from openfarm_core.packs.python import PythonPack
from openfarm_core.packs.typescript import TypeScriptPack, resolve_tooling, settings_from_config


def _git_init(root: Path) -> Git:
    git = Git(root)
    git.init(branch="main")
    git._run(["config", "user.email", "test@example.invalid"])
    git._run(["config", "user.name", "Test"])
    return git


class FlowHardeningTests(unittest.TestCase):
    def _bare_run(self, card: Card) -> CardRun:
        run = object.__new__(CardRun)
        run.card = card
        run.diff_hashes = []
        run.error_signatures = []
        run.named_history = []
        return run

    def test_check_progress_allows_all_named_tests_already_passing(self) -> None:
        card = Card("C1", "t", fields={"tests": [{"name": "t1", "file": "tests/a.py"}, {"name": "t2", "file": "tests/a.py"}]})
        run = self._bare_run(card)
        # 100% of named tests passing cannot rise further (e.g. while fixing lint).
        run.named_history = [2, 2]
        run._check_progress()

        # Less than 100% passing and flat still triggers NO_PROGRESS.
        run.named_history = [1, 1]
        with self.assertRaises(GateFailure) as caught:
            run._check_progress()
        self.assertEqual(caught.exception.code, StopCode.NO_PROGRESS)

    def test_bounce_resets_named_history(self) -> None:
        card = Card("C2", "t", fields={"tests": [{"name": "t1", "file": "tests/a.py"}]})
        run = self._bare_run(card)
        run.cfg = SimpleNamespace(implementer_attempts=3)
        run.budget = SimpleNamespace(implementer_attempts_left=lambda: 2, stop_clock=lambda: None)
        run.stop_code = None
        run.detail = ""
        run._owns_micromanager = False

        seen_histories: list[list[int]] = []

        def fake_phase_b(**_kwargs) -> None:
            seen_histories.append(list(run.named_history))
            run.named_history.append(1)

        bounce_calls = iter([("checker", "diff", "fail", "counter"), None])

        run._step_claim = lambda _claim=None: None
        run._step_worktree = lambda: None
        run._step_baseline = lambda: None
        run._step_plan = lambda: None
        run._step_phase_a = lambda: None
        run._step_prove_tests = lambda: None
        run._step_phase_b = fake_phase_b
        run._step_end_gates = lambda **_kwargs: next(bounce_calls)
        run._step_catch_up = lambda: None
        run._finish = lambda final_state: {"final_state": final_state}

        res = run.run()
        self.assertEqual(res["final_state"], "in-review")
        self.assertEqual(seen_histories, [[], []])

    def test_status_of_disambiguates_duplicate_test_names_by_file(self) -> None:
        parsed = {
            "results": [
                {"name": "test_adds", "binary": "tests/test_a.py", "status": "ok"},
                {"name": "test_adds", "binary": "tests/test_b.py", "status": "FAILED"},
            ]
        }
        row_file = lambda row: row.get("binary", "")
        self.assertEqual(_status_of(parsed, "test_adds"), "mixed")
        self.assertEqual(_status_of(parsed, "test_adds", "tests/test_a.py", row_file), "ok")
        self.assertEqual(_status_of(parsed, "test_adds", "tests/test_b.py", row_file), "FAILED")

    def test_prove_tests_passing_on_base_excludes_not_run_and_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = Card(
                "C3", "t",
                fields={"tests": [
                    {"name": "t_ok", "file": "tests/a.py"},
                    {"name": "t_ignored", "file": "tests/a.py"},
                    {"name": "t_missing", "file": "tests/a.py"},
                ]},
            )
            run = self._bare_run(card)
            run.phase = "prove"
            run.card_layout = Path(tmp) / "layout"
            (run.card_layout / "gates").mkdir(parents=True)
            run.cfg = SimpleNamespace(known_failing=[])
            run.pack = SimpleNamespace(
                commands=lambda *_a, **_k: {"argv": ["true"]},
                parse_list=lambda _o: {
                    "tests": [
                        {"name": "t_ok", "binary": "tests/a.py"},
                        {"name": "t_ignored", "binary": "tests/a.py"},
                        {"name": "t_missing", "binary": "tests/a.py"},
                    ]
                },
                parse_tests=lambda _o, _e="": {
                    "results": [
                        {"name": "t_ok", "binary": "tests/a.py", "status": "ok"},
                        {"name": "t_ignored", "binary": "tests/a.py", "status": "ignored"},
                    ],
                    "footers": [{"binary": "tests/a.py", "status": "ok"}],
                    "missing_footers": [],
                    "compile_errors": [],
                },
                row_file=lambda row: row.get("binary", ""),
            )
            recorded = []
            run._effective_unit = lambda: ""
            run._run = lambda *_a, **_k: SimpleNamespace(cls="pass", exit_code=0)
            run._output = lambda _r: ("", "")
            run._record_gate = lambda g: (recorded.append(g), g)[1]

            with self.assertRaises(GateFailure) as caught:
                run._prove_current_tests()
            self.assertEqual(caught.exception.code, StopCode.TESTS_UNPROVABLE)
            self.assertEqual(recorded[-1].evidence["passing_on_base"], ["t_ok"])
            self.assertEqual(
                recorded[-1].evidence["non_failing_on_base"],
                ["t_ok", "t_ignored", "t_missing"],
            )

    def test_phase_b_checks_outside_touches_before_no_unique_match_refund(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            card = Card("C4", "t", touches=["src/a.py"], fields={"tests": [{"name": "t1", "file": "tests/t.py"}]})
            run = self._bare_run(card)
            run.worktree = root
            run.card_layout = root / "layout"
            (run.card_layout / "edits").mkdir(parents=True)
            run.cfg = SimpleNamespace(
                implementer_attempts=3,
                never_touch=[],
                forbidden_files=[],
            )
            run.budget = SimpleNamespace(
                implementer_attempts=0,
                implementer_attempts_left=lambda: max(0, 3 - run.budget.implementer_attempts),
                take_implementer_attempt=lambda: setattr(run.budget, "implementer_attempts", run.budget.implementer_attempts + 1),
                note_tokens=lambda *_a, **_k: None,
            )
            fake_call = SimpleNamespace(
                content="=== search_replace: src/a.py\n<<< SEARCH\nmissing\n===\nx\n>>> REPLACE\n"
                        "=== whole_file: src/other.py\nhello\n",
                tokens_in=1, tokens_out=1, spend=0.0, tokens_in_cached=0, cut_off=False,
            )
            run.phase = "implement"
            run.plan = None
            run.step_index = 0
            run.step_summaries = []
            run.coaching_guidance = ""
            run.clarified_notes = ""
            run.extra_context_files = {}
            run.book = SimpleNamespace(slices=lambda *_a, **_k: {}, signatures=lambda *_a, **_k: {})
            run.swarm_on = False
            run.micromanager = SimpleNamespace(enabled=False)
            run.log = lambda *_a, **_k: None
            run._check_card_unchanged = lambda: None
            run._check_kill_switch = lambda: None
            run._select_harness_guidance = lambda *_a, **_k: ""
            run._frozen_texts = lambda: {}
            run._call_board_role = lambda *_a, **_k: fake_call
            run._handback_reason = lambda _c: None
            run._record_attempt = lambda *_a, **_k: None
            run._record_contrast_attempt = lambda *_a, **_k: None
            run._current_step = lambda: None
            run._phase_b_reset = lambda: None
            run._diff_against_head_before_edits = lambda: {"diff": ""}
            run._begin_attempt = lambda _n: None
            run.step_checkpoint_commit = ""
            run.frozen_commit = ""

            bad_report = edits.ApplyReport()
            bad_report.refused.append({"path": "src/a.py", "code": "no-unique-match", "message": "not found"})
            bad_report.outside_touches.append("src/other.py")

            with mock.patch("openfarm_core.flow.implementer_brief", return_value="brief"), \
                 mock.patch("openfarm_core.flow.apply_edits", return_value=bad_report):
                with self.assertRaises(GateFailure) as caught:
                    run._step_phase_b()
            self.assertEqual(caught.exception.code, StopCode.REFUSED)
            self.assertIn("outside touches", caught.exception.detail)
            self.assertEqual(run.budget.implementer_attempts, 1)

    def test_multistep_scaffold_leftover_stub_allowed_on_step1_not_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git = _git_init(repo)
            (repo / "src").mkdir()
            (repo / "src" / "a.py").write_text("def existing():\n    return 1\n", encoding="utf-8")
            base_sha = git.commit_all("base")

            (repo / "src" / "a.py").write_text(
                "def existing():\n    return 1\n\n"
                "def step1():\n    raise NotImplementedError\n\n"
                "def step2():\n    raise NotImplementedError\n",
                encoding="utf-8",
            )
            scaffold_sha = git.commit_all("scaffold")

            # Implement step1, leaving step2 stub from scaffold_commit untouched.
            (repo / "src" / "a.py").write_text(
                "def existing():\n    return 1\n\n"
                "def step1():\n    return 10\n\n"
                "def step2():\n    raise NotImplementedError\n",
                encoding="utf-8",
            )

            card = Card(
                "C5", "t", touches=["src/a.py"],
                fields={"tests": [{"name": "t1", "file": "tests/t.py"}, {"name": "t2", "file": "tests/t.py"}]},
            )
            run = self._bare_run(card)
            run.worktree = repo
            run.card_layout = Path(tmp) / "layout"
            (run.card_layout / "edits").mkdir(parents=True)
            run.git = git
            run.base_commit = base_sha
            run.scaffold_commit = scaffold_sha
            run.frozen_tests = {}
            run.pack = PythonPack(ruff=False)
            run.cfg = SimpleNamespace(
                never_touch=[],
                forbidden_files=[],
                flagged_patterns=[],
                known_failing=[],
            )
            run.plan = SimpleNamespace(steps=[{"tests": ["t1"], "does": "s1"}, {"tests": ["t2"], "does": "s2"}])
            run.step_index = 0
            run.step_summaries = []
            run.named_passing = 0
            run.best_named_passing = 0
            run.roles = SimpleNamespace(calls=[])
            run.log = lambda *_a, **_k: None
            run._effective_unit = lambda: ""
            run._pack_gate_evidence = lambda _k: {}
            run._record_gate = lambda g: g
            run._record_attempt = lambda *_a, **_k: None
            run._record_contrast_attempt = lambda *_a, **_k: None
            run._check_progress_with_coach = lambda *_a, **_k: None
            run._commit = lambda _msg: git.commit_all(_msg)
            run._stash_diff = lambda: {
                "diff": edits.diff_text(repo, ["src/a.py"], ref=base_sha),
                "paths": ["src/a.py"],
                "hash": "h1",
            }
            # Step 1: banned-patterns should PASS (not reject untouched step2 stub).
            run._run = lambda kind, _argv: SimpleNamespace(cls="pass", exit_code=0)
            run._output = lambda _r: ("test_t1 (tests.t.T) ... ok\nRan 1 test in 0.001s\n\nOK\n", "")
            run.pack.parse_tests = lambda _o, _e="": {
                "results": [
                    {"name": "t1", "binary": "tests/t.py", "status": "ok"},
                    {"name": "t2", "binary": "tests/t.py", "status": "FAILED"},
                ],
                "footers": [{"binary": "tests/t.py", "status": "FAILED"}],
                "missing_footers": [],
                "compile_errors": [],
            }
            outcome_step1 = run._judge_attempt(1, edits.ApplyReport())
            self.assertEqual(outcome_step1, "__STEP_ADVANCED__")
            self.assertEqual(run.step_index, 1)

            # On final step (step_index == 1), leftover step2 stub MUST fail banned-patterns!
            outcome_step2 = run._judge_attempt(2, edits.ApplyReport())
            self.assertIsNotNone(outcome_step2)
            self.assertIn("leftover-stub", outcome_step2)

    def test_catch_up_cleans_up_catch_up_target_in_finally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git = _git_init(repo)
            (repo / "a.txt").write_text("v1\n", encoding="utf-8")
            base_sha = git.commit_all("base")
            git.create_branch("openfarm/C6-test", "main")
            git.checkout("openfarm/C6-test")
            (repo / "b.txt").write_text("branch\n", encoding="utf-8")
            git.commit_all("card")
            git.checkout("main")
            (repo / "c.txt").write_text("main-adv\n", encoding="utf-8")
            git.commit_all("advance main")

            card = Card("C6", "test")
            run = self._bare_run(card)
            run.git = git
            run.worktree = repo
            run.base_commit = base_sha
            run.branch = "openfarm/C6-test"
            run.card_layout = Path(tmp) / "layout"
            run.card_layout.mkdir(parents=True)
            run.cfg = SimpleNamespace(
                catch_up="required",
                base_branch="main",
                runner_env={},
                known_failing=[],
            )
            run.budget = SimpleNamespace(wall_clock_left_s=lambda: 60.0, note_fixed_build=lambda: None)
            run.pack = SimpleNamespace(
                commands=lambda *_a, **_k: {"argv": ["true"]},
                parse_tests=lambda _o, _e="": {
                    "results": [{"name": "t1", "status": "ok"}],
                    "footers": [{"status": "ok"}],
                    "missing_footers": [],
                    "compile_errors": [],
                },
            )
            run._check_card_unchanged = lambda: None
            run._effective_unit = lambda: ""
            run._base_ref = lambda: "main"
            run._prepare_checkout = lambda _wt: None
            run._record_gate = lambda g: g
            run._output = lambda _r: ("", "")

            def fake_exec(**kwargs):
                target_dir = Path(kwargs["target_dir"])
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "artifact.bin").write_bytes(b"123")
                return SimpleNamespace(cls="pass", exit_code=0)

            run.runner = SimpleNamespace(exec=fake_exec)
            run._step_catch_up()
            self.assertFalse((run.card_layout / "catch-up-target").exists())
            self.assertFalse((run.card_layout / "catch-up").exists())

    def test_banned_counts_handles_binary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git = _git_init(repo)
            (repo / "img.bin").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
            base_sha = git.commit_all("base")
            run = self._bare_run(Card("C7", "t"))
            run.worktree = repo
            run.git = git
            run.base_commit = base_sha
            run.pack = PythonPack(ruff=False)
            self.assertEqual(run._banned_counts(["img.bin"], at_base=False), {})


class EditsHardeningTests(unittest.TestCase):
    def test_edit_as_dict_from_dict_preserves_test_name(self) -> None:
        ed = edits.Edit(edits.FORMAT_APPEND_STUB, "src/a.py", content="def f(): raise NotImplementedError\n", test_name="test_f")
        d = ed.as_dict()
        self.assertEqual(d["test_name"], "test_f")
        rt = edits.Edit.from_dict(d)
        self.assertEqual(rt.test_name, "test_f")

    def test_diff_text_handles_untracked_binary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git = _git_init(repo)
            (repo / "a.txt").write_text("hi\n", encoding="utf-8")
            git.commit_all("init")
            (repo / "blob.bin").write_bytes(b"\x80\x81\x82\xff\x00")
            diff = edits.diff_text(repo, ["blob.bin"])
            self.assertIn("Binary files /dev/null and b/blob.bin differ", diff)
            self.assertIn("blob.bin", edits.touched_paths(diff))

    def test_apply_edits_rolls_back_failed_write_and_tolerates_rollback_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f1 = root / "a.txt"
            f2 = root / "b.txt"
            f1.write_text("orig-a", encoding="utf-8")
            f2.write_text("orig-b", encoding="utf-8")

            real_write_text = Path.write_text
            calls: list[tuple[str, str]] = []

            def flaky_write_text(self_path: Path, data: str, *args, **kwargs):
                calls.append((self_path.name, data))
                if self_path == f2 and data == "new-b":
                    # Simulate partial write before ENOSPC failure
                    real_write_text(self_path, "PARTIAL", *args, **kwargs)
                    raise OSError("disk full")
                return real_write_text(self_path, data, *args, **kwargs)

            batch = [
                edits.Edit(edits.FORMAT_WHOLE_FILE, "a.txt", content="new-a"),
                edits.Edit(edits.FORMAT_WHOLE_FILE, "b.txt", content="new-b"),
            ]
            with mock.patch.object(Path, "write_text", flaky_write_text):
                with self.assertRaises(Invalid) as caught:
                    edits.apply_edits(root, batch, ["a.txt", "b.txt"], [], [])
            self.assertEqual(caught.exception.code, "write-failed")
            self.assertEqual(f1.read_text(encoding="utf-8"), "orig-a")
            self.assertEqual(f2.read_text(encoding="utf-8"), "orig-b")

            # Also verify rollback OSError does not mask Invalid("write-failed")
            def double_fail_write(self_path: Path, data: str, *args, **kwargs):
                if self_path == f2:
                    raise OSError("write/rollback error")
                return real_write_text(self_path, data, *args, **kwargs)

            with mock.patch.object(Path, "write_text", double_fail_write):
                with self.assertRaises(Invalid) as caught2:
                    edits.apply_edits(root, batch, ["a.txt", "b.txt"], [], [])
            self.assertEqual(caught2.exception.code, "write-failed")
            self.assertEqual(f1.read_text(encoding="utf-8"), "orig-a")


class MicromanagerHardeningTests(unittest.TestCase):
    def test_go_scaffold_validation_tightened(self) -> None:
        # Comment bypass rejected
        self.assertFalse(valid_go_scaffold('func Add(a, b int) int { return a + b } // panic("todo")\n'))
        self.assertFalse(valid_go_scaffold('func Add(a, b int) int { /* panic("todo") */ return a + b }\n'))
        # Unreachable / mixed panic rejected
        self.assertFalse(valid_go_scaffold('func Add(a, b int) int { return a + b; panic("todo") }\n'))
        self.assertFalse(valid_go_scaffold('func Add(a, b int) int { if false { panic("x") }; return 0 }\n'))
        # Valid panic-only stub accepted
        self.assertTrue(valid_go_scaffold('func Add(a, b int) int { panic("todo") }\n', require_panic_func=True))
        # Pure type/interface/struct Go contracts (including func-type fields/aliases) accepted
        pure_iface = "package store\n\ntype Store interface {\n\tGet(id string) (string, error)\n}\n"
        self.assertTrue(valid_go_scaffold(pure_iface))
        self.assertFalse(valid_go_scaffold(pure_iface, require_panic_func=True))
        self.assertTrue(valid_go_scaffold("package foo\ntype Fn func(int) error\n"))
        self.assertTrue(valid_go_scaffold("package foo\ntype Opts struct {\n\tOnDone func(int) error\n\tNext func(int) func(string) error\n}\n"))
        self.assertFalse(valid_go_scaffold("package foo\nvar F = func() int { return 1 }\n"))

        # GoPack.append_stub rejects comment bypass
        pack = GoPack()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "calc.go"
            p.write_text("package calc\n", encoding="utf-8")
            with self.assertRaises(Refused) as caught:
                pack.append_stub(p, "TestAdd", 'func Add(a, b int) int { return a + b } // panic("x")\n')
            self.assertEqual(caught.exception.code, "non-panicking-stub")

        # Micromanager accepts pure Go interface scaffold & rejects comment bypass
        card = Card("G1", "go iface", touches=["pkg/store.go"])
        good_backend = SimpleNamespace(
            assist=lambda _op, _p: {"edits": [{"format": "whole_file", "path": "pkg/store.go", "content": pure_iface}]},
            last_usage={},
        )
        mm_good = Micromanager(good_backend, mode="active")
        self.assertEqual(len(mm_good.scaffold_contract(card, {}, {})), 1)

        bad_backend = SimpleNamespace(
            assist=lambda _op, _p: {
                "edits": [{"format": "whole_file", "path": "pkg/store.go", "content": 'func A() int { return 1 } // panic("x")\n'}]
            },
            last_usage={},
        )
        mm_bad = Micromanager(bad_backend, mode="active")
        with self.assertRaises(Refused) as caught_mm:
            mm_bad.scaffold_contract(card, {}, {})
        self.assertEqual(caught_mm.exception.code, "scaffold-missing-todo")

    def test_call_backend_records_failed_usage_before_retry_budget_check(self) -> None:
        calls = 0

        class FlakyBackend:
            last_usage: dict = {}

            def assist(self, _op: str, _payload: dict) -> dict:
                nonlocal calls
                calls += 1
                self.last_usage = {"tokens_in": 100, "tokens_out": 50, "spend": 0.01}
                raise Invalid("bad-micromanager-json", "malformed json")

        budget = MicromanagerBudget(max_calls_per_card=1)
        mm = Micromanager(FlakyBackend(), budget=budget, mode="active")
        with self.assertRaises(Invalid) as caught:
            mm._call_backend("shape_card", "C1", {})
        self.assertEqual(caught.exception.code, "bad-micromanager-json")
        # Must NOT have retried because call 1 exhausted max_calls_per_card=1!
        self.assertEqual(calls, 1)
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.tokens_in, 100)

    def test_micromanager_is_test_path_aligns_with_packs(self) -> None:
        card = Card("T1", "ts")
        self.assertTrue(mm_is_test_path("src/math.test.ts", card))
        self.assertTrue(mm_is_test_path("src/math.spec.ts", card))
        self.assertTrue(mm_is_test_path("src/math_test.ts", card))
        self.assertTrue(mm_is_test_path("src/__tests__/math.ts", card))
        self.assertFalse(mm_is_test_path("src/test_helpers.ts", card))
        self.assertFalse(mm_is_test_path("pkg/test_helpers.go", card))
        self.assertTrue(mm_is_test_path("tests/test_math.py", card))


class LanguagePacksAndMutateHardeningTests(unittest.TestCase):
    def test_typescript_resolve_tooling_preserves_vitest_jest_auto_detection(self) -> None:
        for runner, script in (("vitest", "vitest run"), ("jest", "jest --ci")):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "package.json").write_text(json.dumps({"scripts": {"test": script}}) + "\n", encoding="utf-8")
                (root / ".openfarm").mkdir()
                # 1. Omitted [runner] typescript in config.toml
                (root / ".openfarm" / "config.toml").write_text("[runner]\nlink_node_modules = true\n", encoding="utf-8")
                self.assertEqual(resolve_tooling(root)["runner"], runner)
                # 2. Explicit "auto" in config.toml
                (root / ".openfarm" / "config.toml").write_text('[runner]\ntypescript = "auto"\n', encoding="utf-8")
                self.assertEqual(resolve_tooling(root)["runner"], runner)
                # 3. Explicit "auto" via settings_from_config
                fake_cfg = SimpleNamespace(runner_typescript="auto", typescript_typecheck="auto",
                                           typescript_format="auto", link_node_modules=True)
                self.assertEqual(resolve_tooling(root, settings_from_config(fake_cfg))["runner"], runner)
                # 4. Explicit "node" still overrides auto-detection
                (root / ".openfarm" / "config.toml").write_text('[runner]\ntypescript = "node"\n', encoding="utf-8")
                self.assertEqual(resolve_tooling(root)["runner"], "node --test")

    def test_go_run_pattern_subtests_and_crashed_package(self) -> None:
        pack = GoPack()
        # Multiple subtests across different parent tests
        argv = pack.commands("test", tests=["TestA/sub1", "TestB/sub2"])["argv"]
        pat = argv[argv.index("-run") + 1]
        self.assertEqual(pat, "^(?:TestA|TestB)$/^(?:sub1|sub2)$")
        for seg in pat.split("/"):
            re.compile(seg)  # every /-split segment must compile as valid regex

        # Mixed depth (subtest + top-level test)
        argv_mix = pack.commands("test", tests=["TestA/sub1", "TestB"])["argv"]
        pat_mix = argv_mix[argv_mix.index("-run") + 1]
        self.assertEqual(pat_mix, "^(?:TestA|TestB)$")
        for seg in pat_mix.split("/"):
            re.compile(seg)

        # Crashed package B reported even though package A printed a footer
        events = [
            {"Action": "start", "Package": "example.com/a"},
            {"Action": "pass", "Package": "example.com/a", "Test": "TestA"},
            {"Action": "pass", "Package": "example.com/a"},
            {"Action": "start", "Package": "example.com/b"},
            {"Action": "run", "Package": "example.com/b", "Test": "TestCrash"},
            # Package B crashes with no pass/fail/skip footer!
        ]
        parsed = pack.parse_tests("\n".join(json.dumps(e) for e in events) + "\n")
        self.assertIn("example.com/b", parsed["missing_footers"])
        self.assertNotIn("example.com/a", parsed["missing_footers"])

    def test_python_multi_unit_unittest_counter_test_and_k_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit = root / "backend"
            (unit / "pkg").mkdir(parents=True)
            (unit / "tests").mkdir(parents=True)
            (unit / "pyproject.toml").write_text('[project]\nname = "backend"\n', encoding="utf-8")
            (unit / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (unit / "pkg" / "calc.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
            (unit / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (unit / "tests" / "test_counter.py").write_text(
                "import unittest\n"
                "from pkg.calc import double\n"
                "class T(unittest.TestCase):\n"
                "    def test_wanted(self):\n"
                "        self.assertEqual(double(3), 6)\n"
                "    def test_other(self):\n"
                "        self.fail('should be filtered out')\n",
                encoding="utf-8",
            )
            pack = PythonPack(runner="unittest", ruff=False, python=sys.executable)
            pack.configure(root)
            argv = pack.commands(
                "test",
                unit="backend",
                files=["backend/tests/test_counter.py"],
                tests=["test_wanted"],
            )["argv"]
            proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            parsed = pack.parse_tests(proc.stdout, proc.stderr)
            names = [r["name"] for r in parsed["results"]]
            self.assertEqual(names, ["test_wanted"])

    def test_own_mutation_propagates_unit_and_cleans_up_temp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / "backend" / "pyproject.toml").write_text('[project]\nname = "b"\n', encoding="utf-8")
            pack = PythonPack(runner="unittest", ruff=False, python=sys.executable)
            pack.configure(root)
            argv = pack.commands("mutation", unit="backend", files=["backend/a.py"], tests=["test_a"])["argv"]
            test_argv = json.loads(argv[argv.index("--test-argv") + 1])
            self.assertIn("backend/tests", test_argv)
            self.assertIn("backend", test_argv)
            temp_dir = pack._mutation_dir
            self.assertIsNotNone(temp_dir)
            self.assertTrue(temp_dir.is_dir())
            # Write minimal outcomes.json and read_mutation -> temp dir must be cleaned up!
            (temp_dir / "outcomes.json").write_text(
                json.dumps({"caught": 1, "missed": 0, "total": 1, "capped": False, "outcomes": []}),
                encoding="utf-8",
            )
            parsed = pack.read_mutation("", "", 0)
            self.assertEqual(parsed["killed"], 1)
            self.assertFalse(temp_dir.exists())

    def test_mutate_code_spans_handles_ts_regex_with_quotes(self) -> None:
        src = (
            "const quotes = /['\"`]/g;\n"
            "const ratio = a / b / c;\n"
            "export function check(x: number, y: number) {\n"
            "  return x === y && x > 0;\n"
            "}\n"
        )
        mutants = mutate.typescript_mutants(src, None)
        whats = {m["what"] for m in mutants}
        self.assertIn("=== -> !==", whats)
        self.assertIn("&& -> ||", whats)
        self.assertIn("> -> >=", whats)


class PreflightBlockedTryitAcceptanceInvariantsDagTests(unittest.TestCase):
    def test_preflight_merges_touches_fixes(self) -> None:
        header = {"id": "C1", "touches": ["src/base.ts"], "test_edits": []}
        checks = [
            {"check": "touches", "status": "fail", "fix": {"touches": ["src/base.ts", "src/new.ts"], "notes": ["n1"]}},
            {"check": "frozen", "status": "fail", "fix": {"header": {"touches": ["src/base.ts", "tests/old.test.ts"], "test_edits": ["tests/old.test.ts"]}, "notes": ["n2"]}},
        ]
        new_h, _body, notes = preflight.apply_fixes(header, "body", checks)
        self.assertEqual(new_h["touches"], ["src/base.ts", "src/new.ts", "tests/old.test.ts"])
        self.assertEqual(new_h["test_edits"], ["tests/old.test.ts"])
        self.assertEqual(notes, ["n1", "n2"])

    def test_preflight_check_seams_respects_upstream_classes(self) -> None:
        owner = Card(
            "U1", "HTML shell",
            done_text='Serves HTML with `#app`, `#main`, and `<div class="hero card"></div>`.',
        )
        good_css = Card(
            "U2", "Styles", deps=["U1"], touches=["public/style.css"],
            done_text="- `public/style.css`\n  - styles `#app`, `#main`, `.hero`, `.card`\n",
        )
        bad_css = Card(
            "U3", "Styles", deps=["U1"], touches=["public/style.css"],
            done_text="- `public/style.css`\n  - styles `#app`, `#main`, `.ghost`\n",
        )
        by_id = {"U1": owner, "U2": good_css, "U3": bad_css}
        self.assertEqual(preflight._check_seams(good_css, by_id)["status"], "pass")
        res_bad = preflight._check_seams(bad_css, by_id)
        self.assertEqual(res_bad["status"], "fail")
        self.assertIn(".ghost", res_bad["detail"])

    def test_preflight_is_test_path_unit_lang_and_non_trailing_ts_defaults(self) -> None:
        self.assertTrue(preflight._is_test_path("crates/a/foo_test.rs"))
        self.assertTrue(preflight._is_test_path("pkg/test/foo.py"))
        self.assertTrue(preflight._is_test_path("src/__tests__/a.ts"))
        self.assertTrue(preflight._is_test_path("src/a_test.ts"))
        self.assertTrue(preflight._is_test_path("src/a_test.js"))
        self.assertFalse(preflight._is_test_path("src/test_helpers.ts"))

        # Non-trailing default does NOT add `?` (no TS1016!)
        fixed, dropped = preflight._without_ts_defaults("export function f(a: number = 1, b: string): void")
        self.assertEqual(fixed, "export function f(a: number | undefined, b: string): void")
        self.assertEqual(dropped, [("a", "1")])
        # Trailing default still uses `?`
        fixed2, _ = preflight._without_ts_defaults("export function g(a: string, b: number = 1): void")
        self.assertEqual(fixed2, "export function g(a: string, b?: number): void")

        # Unit-level language lookup including "js"
        cfg = SimpleNamespace(languages=["python"], language_for_unit=lambda u: "js" if u == "web" else "python")
        card = Card(
            "J1", "js card", fields={"unit": "web"},
            done_text="Export `export function hi(x: number = 1, y: string): void`.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            res = preflight._check_scaffold(card, cfg, Path(tmp))
            self.assertEqual(res["status"], "fail")
            self.assertIn("x: number | undefined, y: string", res["fix"]["done_text"])

    def test_blocked_files_now_guard_tests_settle_propose_run_check_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "api.ts").write_text("export const x = 1;\n", encoding="utf-8")
            (root / "secret.env").write_text("SECRET=1\n", encoding="utf-8")
            (Path(tmp) / "outside.txt").write_text("OUTSIDE\n", encoding="utf-8")

            cfg = SimpleNamespace(never_touch=["secret.env"], forbidden_files=[])
            out = blocked._files_now(
                root,
                {"touches": ["secret.env", "../outside.txt"], "reference": "src/api.ts"},
                cfg=cfg,
            )
            self.assertIn("src/api.ts", out)
            self.assertIn("export const x = 1;", out)
            self.assertNotIn("SECRET=1", out)
            self.assertNotIn("OUTSIDE", out)

            # guard_tests: numeric sort (2 before 10), clears on later pass, checks partial/
            state = Path(tmp) / "state"
            rec = {"card_id": "C1", "run_id": "run-1"}
            g2 = state / "cards" / "C1" / "partial" / "run-1" / "gates" / "2"
            g10 = state / "cards" / "C1" / "partial" / "run-1" / "gates" / "10"
            g2.mkdir(parents=True)
            g10.mkdir(parents=True)
            (g2 / "fail-before.json").write_text(json.dumps({
                "result": "fail", "evidence": {"passing_on_base": ["guard_one"]},
            }), encoding="utf-8")
            self.assertEqual(blocked.guard_tests(state, rec), ["guard_one"])
            (g10 / "fail-before.json").write_text(json.dumps({"result": "pass"}), encoding="utf-8")
            self.assertEqual(blocked.guard_tests(state, rec), [])

            # settle_question consistent keys + case-insensitive target.upper()
            class FakeStore:
                def __init__(self) -> None:
                    self.retried = False
                    self.src = SimpleNamespace(_write=lambda *_a, **_k: None)

                def read(self, _cid: str):
                    return ({"id": "C1", "title": "t", "touches": []}, "body")

                def change_card(self, _cid: str, action: str, **_k):
                    if action == "retry":
                        self.retried = True

                def states(self) -> dict[str, str]:
                    return {"C1": "blocked"}

            store = FakeStore()
            with mock.patch("openfarm_core.findings.decide_finding"):
                res_tool = blocked.settle_question(
                    root, SimpleNamespace(state_dir=state), store,
                    {"id": "F1", "from_card": "c1", "title": "openfarm harness", "body": "harness cue"},
                )
                self.assertEqual(res_tool, {"for": "nobody", "answer": "", "retried": False, "stuck": False})
                res_owner = blocked.settle_question(
                    root, SimpleNamespace(state_dir=state), store,
                    {"id": "F2", "from_card": "c1", "title": "color?", "body": "red or blue?"},
                    backend=None,
                )
                self.assertEqual(res_owner, {"for": "owner", "answer": "", "retried": False, "stuck": False})
                res_arch = blocked.settle_question(
                    root, SimpleNamespace(state_dir=state, docs_dir="docs"), store,
                    {"id": "F3", "from_card": "c1", "title": "format?", "body": "which format?"},
                    backend=lambda _p: '{"for": "architect", "answer": "Use JSON."}',
                )
                self.assertEqual(res_arch["for"], "architect")
                self.assertTrue(res_arch["retried"])
                self.assertTrue(store.retried)

            # propose_fix handles non-dict tests entries safely
            (g10 / "fail-before.json").unlink()
            (state / "cards" / "C1" / "partial" / "run-1" / "card-record.json").write_text(
                json.dumps({"card_id": "C1", "run_id": "run-1", "claimed_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            fake_store_pf = SimpleNamespace(
                read=lambda _cid: ({"id": "C1", "title": "t", "tests": ["guard_one", {"name": "real_test", "file": "tests/t.py"}]}, "body"),
                cards=lambda: [],
            )
            pf_cfg = SimpleNamespace(state_dir=state, languages=["python"], never_touch=[], forbidden_files=[])
            with mock.patch("openfarm_core.preflight.check_card", return_value=[]):
                fix = blocked.propose_fix(root, pf_cfg, fake_store_pf, "C1")
            self.assertIsNotNone(fix)
            self.assertEqual(fix["header"]["tests"], [{"name": "real_test", "file": "tests/t.py"}])

            # run_check calls pack.configure and pack.prepare_worktree
            git = _git_init(root)
            (root / ".openfarm").mkdir()
            (root / ".openfarm" / "config.toml").write_text(
                '[runner]  # inline comment\ntypescript = "node"\n', encoding="utf-8",
            )
            git.commit_all("init")
            git.create_branch("openfarm/C9-demo", "main")

            events: list[str] = []

            class FakePack:
                def apply_settings(self, _cfg) -> None:
                    events.append("apply_settings")

                def configure(self, _wt: Path) -> None:
                    events.append("configure")

                def prepare_worktree(self, _wt: Path) -> None:
                    events.append("prepare_worktree")

                def commands(self, *_a, **_k) -> dict:
                    return {"argv": [sys.executable, "-c", "print('ok')"]}

            rc_cfg = SimpleNamespace(
                languages=["python"], cargo_flags=[], pack_adapter=None, runner_python="",
                language_for_unit=lambda _u: "python",
            )
            with mock.patch("openfarm_core.packs.base.pack_for", return_value=FakePack()):
                rc = blocked.run_check(root, rc_cfg, Card("C9", "demo"))
            self.assertEqual(rc["exit"], 0)
            self.assertEqual(events, ["apply_settings", "configure", "prepare_worktree"])

            # set_project_setting handles inline table comment and backslashes in value
            blocked.set_project_setting(root, "runner", "python", r"C:\new\test\python.exe", "test")
            parsed_cfg = tomllib.loads((root / ".openfarm" / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed_cfg["runner"]["python"], r"C:\new\test\python.exe")

    def test_tryit_browser_leak_guard_and_how_to_start_defaults(self) -> None:
        # _Browser.__init__ cleans up pipe FDs and temp dir when Popen fails
        pipe_fds: list[int] = []
        closed_fds: list[int] = []
        orig_pipe = os.pipe
        orig_close = os.close

        def track_pipe() -> tuple[int, int]:
            r, w = orig_pipe()
            pipe_fds.extend([r, w])
            return r, w

        def track_close(fd: int) -> None:
            closed_fds.append(fd)
            orig_close(fd)

        created_dirs: list[Path] = []
        orig_td = tempfile.TemporaryDirectory

        class TrackedTD(orig_td):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created_dirs.append(Path(self.name))

        with mock.patch("openfarm_core.tryit.subprocess.Popen", side_effect=OSError("no bash")), \
             mock.patch("openfarm_core.tryit.os.pipe", side_effect=track_pipe), \
             mock.patch("openfarm_core.tryit.os.close", side_effect=track_close), \
             mock.patch("openfarm_core.tryit.tempfile.TemporaryDirectory", TrackedTD):
            with self.assertRaises(OSError):
                tryit._Browser(Path("/nonexistent/chrome"))
        self.assertEqual(len(pipe_fds), 4)
        self.assertTrue(set(pipe_fds) <= set(closed_fds))
        self.assertTrue(created_dirs and not created_dirs[0].exists())

        # how_to_start preserves pack port/path defaults and normalizes missing leading /
        fake_pack = SimpleNamespace(try_it=lambda _r: {"kind": "web", "start": ["node", "srv.js"], "port": 3000, "path": "/app"})
        cfg_empty = SimpleNamespace(try_it={}, languages=["typescript"], cargo_flags=[], pack_adapter=None, runner_python="")
        with mock.patch("openfarm_core.packs.base.pack_for", return_value=fake_pack):
            plan = tryit.how_to_start(Path("."), cfg_empty)
        self.assertEqual(plan["port"], 3000)
        self.assertEqual(plan["path"], "/app")

        cfg_rel = SimpleNamespace(try_it={"path": "health"}, languages=["typescript"], cargo_flags=[], pack_adapter=None, runner_python="")
        with mock.patch("openfarm_core.packs.base.pack_for", return_value=fake_pack):
            plan2 = tryit.how_to_start(Path("."), cfg_rel)
        self.assertEqual(plan2["port"], 3000)
        self.assertEqual(plan2["path"], "/health")

    def test_acceptance_cli_empty_cmd_timeout_and_http_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Empty CLI command guarded
            rep_empty = acceptance.run_acceptance_checks(root, [{"name": "empty", "kind": "cli", "cmd": []}])
            self.assertFalse(rep_empty["ok"])
            self.assertIn("empty", rep_empty["results"][0]["error"])

            # CLI timeout kills process group cleanly without leaking subprocesses
            rep_to = acceptance.run_acceptance_checks(
                root,
                [{"name": "slow", "kind": "cli", "cmd": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_s": 1.0}],
            )
            self.assertFalse(rep_to["ok"])
            self.assertEqual(rep_to["results"][0]["exit_code"], -1)

            # HTTP check catches OSError on nonexistent start_cmd binary
            rep_http = acceptance.run_acceptance_checks(
                root,
                [{"name": "bad-bin", "kind": "http", "start_cmd": ["/no/such/openfarm-test-binary"], "startup_timeout_s": 1.0}],
            )
            self.assertFalse(rep_http["ok"])
            self.assertTrue(rep_http["results"][0]["error"])

    def test_invariants_imports_exports_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "app" / "views").mkdir(parents=True)
            (root / "app" / "views" / "home.py").write_text(
                "from . import secret\nfrom ..db import session\nfrom pkg import submod\n",
                encoding="utf-8",
            )
            (root / "web.js").write_text("const fs = require('node:fs');\n", encoding="utf-8")
            (root / "lib.rs").write_text(
                "pub(crate) use crate::db::Pool;\n"
                "pub fn borrow<'a>(s: &'a str) -> &'a str { s }\n"
                "// pub fn FakeExport() {}\n"
                'const NOTE: &str = "GhostExport";\n',
                encoding="utf-8",
            )
            (root / "main.go").write_text(
                'package main\n'
                'import h "net/http"\n'
                'func f(s string) {\n'
                '  switch s {\n'
                '  case "database/sql":\n'
                '    _ = h.StatusOK\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "exp.py").write_text(
                '# def CommentOnly(): pass\n'
                'DOC = "StringOnly"\n'
                'def RealExport():\n'
                '    return 1\n',
                encoding="utf-8",
            )
            (Path(tmp) / "outside.py").write_text("def Secret(): pass\n", encoding="utf-8")

            inv_cfg = SimpleNamespace(
                invariants=SimpleNamespace(
                    has_rules=True,
                    forbidden_imports=[
                        {"path_glob": "app/**/*.py", "disallow": ["secret", "app.db", "pkg.submod"]},
                        {"path_glob": "*.js", "disallow": ["node:fs"]},
                        {"path_glob": "*.rs", "disallow": ["crate::db"]},
                        {"path_glob": "*.go", "disallow": ["net/http", "database/sql"]},
                    ],
                    max_file_bytes=[],
                    require_exports=[
                        {"path": "exp.py", "symbols": ["RealExport", "CommentOnly", "StringOnly"]},
                        {"path": "lib.rs", "symbols": ["borrow", "FakeExport", "GhostExport"]},
                        {"path": "../outside.py", "symbols": ["Secret"]},
                    ],
                )
            )
            rep = invariants.check_architectural_invariants(root, inv_cfg)
            self.assertFalse(rep["ok"])
            by_path = {}
            for v in rep["violations"]:
                by_path.setdefault(v["path"], []).append(v["detail"])

            self.assertIn("secret", by_path["app/views/home.py"][0])
            self.assertIn("app.db", by_path["app/views/home.py"][0])
            self.assertIn("pkg.submod", by_path["app/views/home.py"][0])
            self.assertIn("node:fs", by_path["web.js"][0])
            self.assertIn("crate::db", by_path["lib.rs"][0])
            # Go: catches aliased net/http, does NOT false-positive on case "database/sql":
            self.assertIn("net/http", by_path["main.go"][0])
            self.assertNotIn("database/sql", by_path["main.go"][0])
            # Comment/string-safe require_exports
            exp_details = " ".join(by_path["exp.py"])
            self.assertNotIn("RealExport", exp_details)
            self.assertIn("CommentOnly", exp_details)
            self.assertIn("StringOnly", exp_details)
            rs_details = " ".join(by_path["lib.rs"])
            self.assertNotIn("borrow", rs_details)
            self.assertIn("FakeExport", rs_details)
            self.assertIn("GhostExport", rs_details)
            # Path escape rejected
            self.assertIn("escapes", by_path["../outside.py"][0])

    def test_dag_blocks_missing_or_split_deps_and_excludes_dropped_from_conflicts(self) -> None:
        c_split = Card("C-SPLIT", "split parent", state="split", touches=["src/a.py"])
        c_dropped = Card("C-DROP", "dropped", state="dropped", touches=["src/shared.py"])
        c_active = Card("C-ACT", "active", state="ready", touches=["src/shared.py"])
        c_wait_split = Card("C-W1", "waits split", state="ready", deps=["C-SPLIT"], touches=["src/b.py"])
        c_wait_missing = Card("C-W2", "waits missing", state="ready", deps=["C-NOPE"], touches=["src/c.py"])

        res = dag.build_card_dag([c_split, c_dropped, c_active, c_wait_split, c_wait_missing])
        # Dropped card excluded from DAG & conflicts
        self.assertEqual(res["conflicts"], [])
        self.assertNotIn("C-DROP", [c["id"] for c in res["cards"]])
        # Missing / split dependencies are NOT silently dropped -> blocked_by_deps, not ready_now
        self.assertEqual(res["ready_now"], ["C-ACT"])
        self.assertEqual(set(res["blocked_by_deps"]), {"C-W1", "C-W2"})
        self.assertEqual(res["unmet_deps"]["C-W1"], ["C-SPLIT"])
        self.assertEqual(res["unmet_deps"]["C-W2"], ["C-NOPE"])


if __name__ == "__main__":
    unittest.main()
