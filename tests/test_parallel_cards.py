"""Tests for Stage 7: parallel cards, collision scheduling, BuildLimiter, and thread locks."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adapters.direct import BuildLimiter, DirectRunner
from openfarm_core.adapters.files import FileCardSource
from openfarm_core.adapters.jsonl import JsonlTelemetry, MemoryTelemetry, TelemetrySink
from openfarm_core.adapters.openai_compat import ScriptedBackend
from openfarm_core.board import Board, board_dir_for
from openfarm_core.budget import Budget, NightBudget
from openfarm_core.cards import Card
from openfarm_core.cli import EXIT_OK, main as cli_main
from openfarm_core.git import Git
from openfarm_core.harness_global import GlobalLibrary
from openfarm_core.harness_memory import HarnessLibrary
from openfarm_core.micromanager import (
    ClaudeAcpMicromanager,
    Micromanager,
    MicromanagerBudget,
    ScriptedMicromanager,
)
from openfarm_core.state import StateDir, append_jsonl, read_json, write_json
from tests.test_python_e2e import build_python_toy
from tests.toy import CONFIG as RUST_TOY_CONFIG, ToyRepo, _run as _git_run, cargo_available


def _run_cli(repo: Path, *args: str) -> tuple[int, dict, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--repo", str(repo), "--json", *args])
    raw = out.getvalue().strip()
    payload = json.loads(raw) if raw else {}
    return code, payload, err.getvalue()


def _setup_three_module_toy(*, concurrent_cards: int, concurrent_builds: int):
    toy = build_python_toy()
    toy.write("tally/alpha.py", "def alpha_val(x: int) -> int:\n    return x\n")
    toy.write("tally/beta.py", "def beta_val(x: int) -> int:\n    return x\n")
    toy.write("tally/gamma.py", "def gamma_val(x: int) -> int:\n    return x\n")
    toy.commit("add alpha, beta, gamma modules")

    cfg_path = toy.root / ".openfarm" / "config.toml"
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace(
        "concurrent_builds = 2", f"concurrent_builds = {concurrent_builds}"
    )
    if "concurrent_cards =" in cfg_text:
        cfg_text = cfg_text.replace(
            "concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}"
        )
    else:
        cfg_text = cfg_text.replace(
            f"concurrent_builds = {concurrent_builds}",
            f"concurrent_builds = {concurrent_builds}\nconcurrent_cards = {concurrent_cards}",
        )
    cfg_text += (
        '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\n'
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")

    toy.write_card(
        "CARD-1",
        title="Double alpha value",
        category="pinned-bugfix",
        unit=".",
        touches=["tally/alpha.py"],
        tests=[{
            "name": "test_alpha_doubled",
            "file": "tests/test_alpha.py",
            "asserts": "alpha_val(3) == 6",
        }],
        done_text="`alpha_val(x)` returns `x * 2`.",
    )
    toy.write_card(
        "CARD-2",
        title="Triple beta value",
        category="pinned-bugfix",
        unit=".",
        touches=["tally/beta.py"],
        tests=[{
            "name": "test_beta_tripled",
            "file": "tests/test_beta.py",
            "asserts": "beta_val(3) == 9",
        }],
        done_text="`beta_val(x)` returns `x * 3`.",
    )
    toy.write_card(
        "CARD-3",
        title="Increment gamma value",
        category="pinned-bugfix",
        unit=".",
        touches=["tally/gamma.py"],
        tests=[{
            "name": "test_gamma_inc",
            "file": "tests/test_gamma.py",
            "asserts": "gamma_val(3) == 4",
        }],
        done_text="`gamma_val(x)` returns `x + 1`.",
    )
    toy.commit("add 3 independent cards and swarm config")

    script = {
        "CARD-1/test_writer": (
            "=== whole_file: tests/test_alpha.py\n"
            "import unittest\n"
            "from tally.alpha import alpha_val\n\n"
            "class AlphaTests(unittest.TestCase):\n"
            "    def test_alpha_doubled(self):\n"
            "        self.assertEqual(alpha_val(3), 6)\n"
        ),
        "CARD-1/implementer": (
            "=== whole_file: tally/alpha.py\n"
            "def alpha_val(x: int) -> int:\n"
            "    return x * 2\n"
        ),
        "CARD-1/checker_pass1": "RESTATEMENT: alpha_val doubles input integer",
        "CARD-1/checker_pass2": "{}",
        "CARD-2/test_writer": (
            "=== whole_file: tests/test_beta.py\n"
            "import unittest\n"
            "from tally.beta import beta_val\n\n"
            "class BetaTests(unittest.TestCase):\n"
            "    def test_beta_tripled(self):\n"
            "        self.assertEqual(beta_val(3), 9)\n"
        ),
        "CARD-2/implementer": (
            "=== whole_file: tally/beta.py\n"
            "def beta_val(x: int) -> int:\n"
            "    return x * 3\n"
        ),
        "CARD-2/checker_pass1": "RESTATEMENT: beta_val triples input integer",
        "CARD-2/checker_pass2": "{}",
        "CARD-3/test_writer": (
            "=== whole_file: tests/test_gamma.py\n"
            "import unittest\n"
            "from tally.gamma import gamma_val\n\n"
            "class GammaTests(unittest.TestCase):\n"
            "    def test_gamma_inc(self):\n"
            "        self.assertEqual(gamma_val(3), 4)\n"
        ),
        "CARD-3/implementer": (
            "=== whole_file: tally/gamma.py\n"
            "def gamma_val(x: int) -> int:\n"
            "    return x + 1\n"
        ),
        "CARD-3/checker_pass1": "RESTATEMENT: gamma_val increments input integer",
        "CARD-3/checker_pass2": "{}",
    }
    return toy, script


class ParallelCardsRunTests(unittest.TestCase):
    def test_serial_vs_three_at_once_yield_identical_states_gates_and_diffs(self) -> None:
        serial_toy, serial_script = _setup_three_module_toy(
            concurrent_cards=1, concurrent_builds=1
        )
        self.addCleanup(serial_toy.cleanup)
        serial_dir = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, serial_dir, True)
        s_script_path = serial_dir / "script.json"
        s_script_path.write_text(json.dumps(serial_script), encoding="utf-8")

        par_toy, par_script = _setup_three_module_toy(
            concurrent_cards=3, concurrent_builds=2
        )
        self.addCleanup(par_toy.cleanup)
        par_dir = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, par_dir, True)
        p_script_path = par_dir / "script.json"
        p_script_path.write_text(json.dumps(par_script), encoding="utf-8")

        code_s, out_s, err_s = _run_cli(
            serial_toy.root, "run", "--cards", "3", "--script", str(s_script_path)
        )
        self.assertEqual(code_s, EXIT_OK, err_s)

        code_p, out_p, err_p = _run_cli(
            par_toy.root, "run", "--cards", "3", "--script", str(p_script_path)
        )
        self.assertEqual(code_p, EXIT_OK, err_p)
        self.assertFalse(
            any("run one at a time" in note for note in out_p.get("notes", [])),
            out_p.get("notes"),
        )

        s_by_id = {o["card_id"]: o for o in out_s["outcomes"]}
        p_by_id = {o["card_id"]: o for o in out_p["outcomes"]}
        self.assertEqual(set(s_by_id.keys()), {"CARD-1", "CARD-2", "CARD-3"})
        self.assertEqual(set(p_by_id.keys()), {"CARD-1", "CARD-2", "CARD-3"})

        for cid in ("CARD-1", "CARD-2", "CARD-3"):
            self.assertEqual(s_by_id[cid]["final_state"], "in-review")
            self.assertEqual(p_by_id[cid]["final_state"], "in-review")
            s_dossier = read_json(
                Path(s_by_id[cid]["dossier_path"]).with_suffix(".json")
            )
            p_dossier = read_json(
                Path(p_by_id[cid]["dossier_path"]).with_suffix(".json")
            )
            self.assertEqual(
                [(g["name"], g["result"]) for g in s_dossier["2_gates"]],
                [(g["name"], g["result"]) for g in p_dossier["2_gates"]],
            )
            self.assertEqual(s_dossier["9_diffs"], p_dossier["9_diffs"])

    def test_overlapping_touches_globs_never_run_concurrently(self) -> None:
        toy, script = _setup_three_module_toy(
            concurrent_cards=3, concurrent_builds=3
        )
        self.addCleanup(toy.cleanup)

        # Make CARD-2 overlap CARD-1 via a glob (`tally/*.py` vs `tally/alpha.py`)
        # while CARD-3 touches `other/gamma.py`? Wait: rewrite CARD-2 touches to `tally/a*.py`.
        c2_path = toy.root / "cards" / "CARD-2.md"
        c2_raw = c2_path.read_text(encoding="utf-8")
        c2_path.write_text(
            c2_raw.replace('touches = ["tally/beta.py"]', 'touches = ["tally/*.py"]'),
            encoding="utf-8",
        )
        toy.commit("CARD-2 glob touches overlapping CARD-1")

        overlap_dir = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, overlap_dir, True)
        script_path = overlap_dir / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")

        from openfarm_core import flow as flow_mod

        lock = threading.Lock()
        active_impl: set[str] = set()
        concurrent_pairs: set[tuple[str, str]] = set()
        barrier = threading.Barrier(2, timeout=5.0)
        original_phase_b = flow_mod.CardRun._step_phase_b

        def tracked_phase_b(self_run, *args, **kwargs):
            with lock:
                for other in active_impl:
                    concurrent_pairs.add(tuple(sorted((self_run.card.id, other))))
                active_impl.add(self_run.card.id)
            if self_run.card.id in ("CARD-1", "CARD-3"):
                barrier.wait()
            try:
                return original_phase_b(self_run, *args, **kwargs)
            finally:
                with lock:
                    active_impl.discard(self_run.card.id)

        flow_mod.CardRun._step_phase_b = tracked_phase_b
        self.addCleanup(setattr, flow_mod.CardRun, "_step_phase_b", original_phase_b)

        code, out, err = _run_cli(
            toy.root, "run", "--cards", "3", "--script", str(script_path)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(
            {o["card_id"]: o["final_state"] for o in out["outcomes"]},
            {"CARD-1": "in-review", "CARD-2": "in-review", "CARD-3": "in-review"},
        )
        # CARD-1 (tally/alpha.py) and CARD-2 (tally/*.py) never ran simultaneously,
        # nor did CARD-2 and CARD-3 (tally/*.py vs tally/gamma.py).
        # Meanwhile CARD-1 and CARD-3 DID run simultaneously across the barrier!
        self.assertNotIn(("CARD-1", "CARD-2"), concurrent_pairs)
        self.assertNotIn(("CARD-2", "CARD-3"), concurrent_pairs)
        self.assertIn(("CARD-1", "CARD-3"), concurrent_pairs)


class BuildLimiterAndLocksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_build_limiter_caps_active_builds_and_pauses_wall_clock(self) -> None:
        limiter = BuildLimiter(1)
        gate_release = threading.Event()
        first_entered = threading.Event()
        errors: list[BaseException] = []
        budgets = [Budget(wall_clock_s=60.0) for _ in range(3)]

        def worker(idx: int) -> None:
            try:
                with limiter.slot(budgets[idx]):
                    if idx == 0:
                        first_entered.set()
                        gate_release.wait(timeout=5.0)
                    else:
                        first_entered.wait(timeout=5.0)
            except BaseException as exc:
                errors.append(exc)

        t0 = threading.Thread(target=worker, args=(0,))
        t0.start()
        self.assertTrue(first_entered.wait(timeout=5.0))
        others = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for t in others:
            t.start()
        gate_release.set()
        for t in (t0, *others):
            t.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(limiter.max_active, 1)
        self.assertEqual(limiter.active, 0)

        # DirectRunner enforces concurrent_builds through limiter.
        runner = DirectRunner(self.tmp, limiter=BuildLimiter(1), budget=budgets[0])
        res = getattr(runner, "exec")("list", [sys.executable, "-c", "print('ok')"])
        self.assertEqual(res.cls, "pass")
        self.assertEqual(runner.limiter.max_active, 1)

    def test_shared_state_budgets_telemetry_and_libraries_are_thread_safe(self) -> None:
        # 1. NightBudget across 6 threads
        night = NightBudget(spend_cap=100.0)
        threads = []
        for _ in range(6):
            def bump_night() -> None:
                for _ in range(20):
                    night.note_card()
                    night.note_spend(0.25)
                    night.note_micromanager(0.1)
            threads.append(threading.Thread(target=bump_night))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(night.cards_claimed, 120)
        self.assertAlmostEqual(night.spend, 30.0, places=5)
        self.assertAlmostEqual(night.micromanager_spend, 12.0, places=5)

        # 2. StateDir write_json + append_jsonl + breaker under 8 writers
        state = StateDir(self.tmp / "state")
        jsonl_path = self.tmp / "events.jsonl"
        target_json = self.tmp / "shared.json"

        def spam_state(tid: int) -> None:
            for j in range(15):
                append_jsonl(jsonl_path, {"t": tid, "j": j})
                write_json(target_json, {"t": tid, "j": j})
                state.write_breaker(False, [f"CARD-{tid}"])
                _ = state.read_breaker()

        wthreads = [threading.Thread(target=spam_state, args=(i,)) for i in range(8)]
        for t in wthreads:
            t.start()
        for t in wthreads:
            t.join(timeout=5.0)
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 120)
        self.assertIsInstance(read_json(target_json), dict)

        # 3. Telemetry classes under 8 threads
        jtel = JsonlTelemetry(self.tmp / "run1")
        mtel = MemoryTelemetry()
        sink = TelemetrySink(jtel)

        def spam_tel(tid: int) -> None:
            for j in range(10):
                sink.write("attempt", {"card_id": f"C-{tid}", "attempt": j})
                mtel.write("card", {"card_id": f"C-{tid}", "j": j})

        tthreads = [threading.Thread(target=spam_tel, args=(i,)) for i in range(8)]
        for t in tthreads:
            t.start()
        for t in tthreads:
            t.join(timeout=5.0)
        sink.flush()
        self.assertEqual(len(mtel.cards()), 80)
        attempt_lines = (
            self.tmp / "run1" / "telemetry" / "attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(attempt_lines), 80)

        # 4. GlobalLibrary & HarnessLibrary reentrant locks across multiple threads
        glob = GlobalLibrary(self.tmp / "global")
        hlib = HarnessLibrary(self.tmp / "harness")

        def spam_libs(tid: int) -> None:
            with glob._locked():
                with glob._locked():
                    glob._event("probe", {"t": tid})
            with hlib._locked():
                with hlib._locked():
                    append_jsonl(hlib.harness_dir / "ledger.jsonl", {"t": tid})

        lthreads = [threading.Thread(target=spam_libs, args=(i,)) for i in range(6)]
        for t in lthreads:
            t.start()
        for t in lthreads:
            t.join(timeout=5.0)
        self.assertEqual(len(glob.ledger_path.read_text(encoding="utf-8").splitlines()), 6)

    def test_claude_acp_backend_and_mm_budget_isolate_parallel_cards(self) -> None:
        class _FakePromptRes:
            def __init__(self, card_id: str, tok_in: int, tok_out: int) -> None:
                self.session_id = "sess-1"
                self.text = json.dumps({
                    "verdict": "ready",
                    "clarified_notes": f"note-{card_id}",
                    "extra_context_files": [],
                    "steps": [],
                    "findings": [],
                })
                self.tokens_in = tok_in
                self.tokens_out = tok_out
                self.vendor_cost_usd = 0.0
                self.latency_s = 0.01
                self.stop_reason = "end_turn"
                self.permission_events = []
                self.rejected_methods = []

        class _FakeAcpAdapter:
            def __init__(self) -> None:
                self._initialized = True
                self._proc = None
                self.last_usage = None
                self._guard = threading.Lock()
                self.active = 0
                self.max_active = 0

            def new_session(self, **_kw) -> str:
                return "sess-1"

            def prompt(self, _sid: str, prompt_text: str, **_kw):
                with self._guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    cid = "CARD-1" if "CARD-1" in prompt_text else (
                        "CARD-2" if "CARD-2" in prompt_text else "CARD-3"
                    )
                    base = {"CARD-1": (100, 10), "CARD-2": (200, 20), "CARD-3": (300, 30)}
                    tin, tout = base[cid]
                    return _FakePromptRes(cid, tin, tout)
                finally:
                    with self._guard:
                        self.active -= 1

            def close(self) -> None:
                pass

        fake_acp = _FakeAcpAdapter()
        shared_backend = ClaudeAcpMicromanager(
            cwd=self.tmp,
            state_dir=self.tmp / "state",
            adapter=fake_acp,
            cost_per_1k_in=0.01,
            cost_per_1k_out=0.02,
        )
        shared_budget = MicromanagerBudget(max_calls_per_card=4, tokens_per_card=100_000)
        errors: list[BaseException] = []

        def run_mm(cid: str) -> None:
            try:
                mm = Micromanager(
                    shared_backend,
                    budget=shared_budget,
                    card_dir=self.tmp / "cards" / cid,
                    mode="active",
                )
                card = Card(
                    id=cid,
                    title=f"Card {cid}",
                    state="ready",
                    done_text="done",
                    touches=["a.py"],
                )
                dec = mm.shape_and_plan(card, {}, {})
                self.assertEqual(dec.clarified_notes, f"note-{cid}")
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run_mm, args=(f"CARD-{i}",))
            for i in (1, 2, 3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(fake_acp.max_active, 1)
        self.assertEqual(shared_budget.calls, 3)
        self.assertEqual(shared_budget.usage_for("CARD-1")["tokens_in"], 100)
        self.assertEqual(shared_budget.usage_for("CARD-2")["tokens_in"], 200)
        self.assertEqual(shared_budget.usage_for("CARD-3")["tokens_in"], 300)


def _setup_three_module_rust_toy(
    *, concurrent_cards: int, concurrent_builds: int
) -> tuple[ToyRepo, dict[str, str]]:
    """Create a 3-module Rust crate with Cargo.lock and 3 independent cards."""
    base = Path(tempfile.mkdtemp(prefix="openfarm-rust-par-"))
    root = base / "repo"
    state_dir = base / "repo-state"
    root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    toy = ToyRepo(root, state_dir, base=base)

    toy.write(
        "Cargo.toml",
        '[package]\nname = "parallel_toy"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
    )
    toy.write(
        "src/lib.rs",
        "//! A three-module Rust crate for parallel cargo gate tests.\n\n"
        "pub mod alpha;\npub mod beta;\npub mod gamma;\n",
    )
    toy.write(
        "src/alpha.rs",
        "//! Alpha arithmetic module.\n\n"
        "/// Double `value`.\n"
        "pub fn alpha_val(value: i32) -> i32 {\n"
        "    value\n"
        "}\n",
    )
    toy.write(
        "src/beta.rs",
        "//! Beta arithmetic module.\n\n"
        "/// Triple `value`.\n"
        "pub fn beta_val(value: i32) -> i32 {\n"
        "    value\n"
        "}\n",
    )
    toy.write(
        "src/gamma.rs",
        "//! Gamma arithmetic module.\n\n"
        "/// Increment `value` by one.\n"
        "pub fn gamma_val(value: i32) -> i32 {\n"
        "    value\n"
        "}\n",
    )
    toy.write(
        "tests/existing.rs",
        "use parallel_toy::alpha::alpha_val;\n\n"
        "#[test]\n"
        "fn existing_smoke_zero_stays_zero() {\n"
        "    assert_eq!(alpha_val(0), 0);\n"
        "}\n",
    )
    toy.write(".gitignore", "/target\n")

    cfg_text = RUST_TOY_CONFIG.format(state_dir=state_dir)
    cfg_text = cfg_text.replace('name = "toy"', 'name = "parallel_toy"')
    cfg_text = cfg_text.replace(
        "concurrent_builds = 1", f"concurrent_builds = {concurrent_builds}"
    )
    cfg_text = cfg_text.replace(
        "concurrent_cards = 1", f"concurrent_cards = {concurrent_cards}"
    )
    cfg_text = cfg_text.replace('mutation = "report"', 'mutation = "off"')
    cfg_text += (
        '\n[swarm]\nenabled = true\ncoordinator = "code"\nsupervisor = "off"\n'
    )
    toy.write(".openfarm/config.toml", cfg_text)

    lock_res = getattr(DirectRunner(root), "exec")(
        "build", ["cargo", "generate-lockfile"], cwd=root
    )
    if lock_res.exit_code != 0:
        raise RuntimeError(f"cargo generate-lockfile failed:\n{lock_res.stderr}")

    _git_run(["git", "init", "-q", "-b", "main"], root)
    toy.commit("initial three-module rust crate")

    toy.write_card(
        "CARD-1",
        title="Double alpha value",
        category="pinned-bugfix",
        unit="parallel_toy",
        touches=["src/alpha.rs"],
        tests=[{
            "name": "alpha_doubles_nonzero",
            "file": "tests/alpha_tests.rs",
            "asserts": "alpha_val(3) == 6",
        }],
        done_text="`alpha_val(value)` returns `value * 2`.",
    )
    toy.write_card(
        "CARD-2",
        title="Triple beta value",
        category="pinned-bugfix",
        unit="parallel_toy",
        touches=["src/beta.rs"],
        tests=[{
            "name": "beta_triples_nonzero",
            "file": "tests/beta_tests.rs",
            "asserts": "beta_val(3) == 9",
        }],
        done_text="`beta_val(value)` returns `value * 3`.",
    )
    toy.write_card(
        "CARD-3",
        title="Increment gamma value",
        category="pinned-bugfix",
        unit="parallel_toy",
        touches=["src/gamma.rs"],
        tests=[{
            "name": "gamma_increments_nonzero",
            "file": "tests/gamma_tests.rs",
            "asserts": "gamma_val(3) == 4",
        }],
        done_text="`gamma_val(value)` returns `value + 1`.",
    )
    toy.commit("add 3 independent rust cards")

    script = {
        "CARD-1/test_writer": (
            "=== whole_file: tests/alpha_tests.rs\n"
            "use parallel_toy::alpha::alpha_val;\n\n"
            "#[test]\n"
            "fn alpha_doubles_nonzero() {\n"
            "    assert_eq!(alpha_val(3), 6);\n"
            "}\n"
        ),
        "CARD-1/implementer": (
            "=== whole_file: src/alpha.rs\n"
            "//! Alpha arithmetic module.\n\n"
            "/// Double `value`.\n"
            "pub fn alpha_val(value: i32) -> i32 {\n"
            "    value * 2\n"
            "}\n"
        ),
        "CARD-1/checker_pass1": "RESTATEMENT: alpha_val doubles input i32",
        "CARD-1/checker_pass2": "{}",
        "CARD-2/test_writer": (
            "=== whole_file: tests/beta_tests.rs\n"
            "use parallel_toy::beta::beta_val;\n\n"
            "#[test]\n"
            "fn beta_triples_nonzero() {\n"
            "    assert_eq!(beta_val(3), 9);\n"
            "}\n"
        ),
        "CARD-2/implementer": (
            "=== whole_file: src/beta.rs\n"
            "//! Beta arithmetic module.\n\n"
            "/// Triple `value`.\n"
            "pub fn beta_val(value: i32) -> i32 {\n"
            "    value * 3\n"
            "}\n"
        ),
        "CARD-2/checker_pass1": "RESTATEMENT: beta_val triples input i32",
        "CARD-2/checker_pass2": "{}",
        "CARD-3/test_writer": (
            "=== whole_file: tests/gamma_tests.rs\n"
            "use parallel_toy::gamma::gamma_val;\n\n"
            "#[test]\n"
            "fn gamma_increments_nonzero() {\n"
            "    assert_eq!(gamma_val(3), 4);\n"
            "}\n"
        ),
        "CARD-3/implementer": (
            "=== whole_file: src/gamma.rs\n"
            "//! Gamma arithmetic module.\n\n"
            "/// Increment `value` by one.\n"
            "pub fn gamma_val(value: i32) -> i32 {\n"
            "    value + 1\n"
            "}\n"
        ),
        "CARD-3/checker_pass1": "RESTATEMENT: gamma_val increments input i32",
        "CARD-3/checker_pass2": "{}",
    }
    return toy, script


@unittest.skipUnless(
    os.environ.get("OPENFARM_E2E") == "1", "set OPENFARM_E2E=1"
)
class ParallelRustCargoE2ETests(unittest.TestCase):
    """Real cargo gate verification across serial vs parallel cards."""

    def _verify_branch_cargo_gates(self, repo: Path, branch: str) -> None:
        wt_parent = Path(tempfile.mkdtemp(prefix="openfarm-rust-check-"))
        self.addCleanup(shutil.rmtree, wt_parent, True)
        wt = wt_parent / "wt"
        git = Git(repo)
        git.worktree_add(wt, branch)
        try:
            runner = DirectRunner(wt)
            run_exec = getattr(runner, "exec")
            test_res = run_exec("test", ["cargo", "test", "--locked"], cwd=wt)
            self.assertEqual(
                test_res.exit_code, 0, test_res.stdout + test_res.stderr
            )
            clippy_res = run_exec(
                "lint",
                ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                cwd=wt,
            )
            self.assertEqual(
                clippy_res.exit_code, 0, clippy_res.stdout + clippy_res.stderr
            )
            rs_files = [
                str(p)
                for p in sorted(list((wt / "src").glob("*.rs")) + list((wt / "tests").glob("*.rs")))
            ]
            fmt_res = run_exec(
                "extra",
                ["rustfmt", "--edition", "2021", "--check", *rs_files],
                cwd=wt,
            )
            self.assertEqual(
                fmt_res.exit_code, 0, fmt_res.stdout + fmt_res.stderr
            )
        finally:
            git.worktree_remove(wt)

    def test_serial_vs_three_way_parallel_rust_cargo_gates(self) -> None:
        self.assertTrue(cargo_available(), "cargo is required when OPENFARM_E2E=1")

        serial_toy, serial_script = _setup_three_module_rust_toy(
            concurrent_cards=1, concurrent_builds=1
        )
        self.addCleanup(serial_toy.cleanup)
        s_script_dir = Path(tempfile.mkdtemp(prefix="openfarm-rust-script-"))
        self.addCleanup(shutil.rmtree, s_script_dir, True)
        s_script_path = s_script_dir / "script.json"
        s_script_path.write_text(json.dumps(serial_script), encoding="utf-8")

        par_toy, par_script = _setup_three_module_rust_toy(
            concurrent_cards=3, concurrent_builds=2
        )
        self.addCleanup(par_toy.cleanup)
        p_script_dir = Path(tempfile.mkdtemp(prefix="openfarm-rust-script-"))
        self.addCleanup(shutil.rmtree, p_script_dir, True)
        p_script_path = p_script_dir / "script.json"
        p_script_path.write_text(json.dumps(par_script), encoding="utf-8")

        code_s, out_s, err_s = _run_cli(
            serial_toy.root, "run", "--cards", "3", "--script", str(s_script_path)
        )
        self.assertEqual(code_s, EXIT_OK, err_s)

        from openfarm_core import cli as cli_mod
        from openfarm_core import flow as flow_mod

        created_limiters: list[BuildLimiter] = []
        orig_limiter_cls = cli_mod.BuildLimiter

        def tracked_limiter(limit: int) -> BuildLimiter:
            lim = orig_limiter_cls(limit)
            created_limiters.append(lim)
            return lim

        lock = threading.Lock()
        active_impl: set[str] = set()
        concurrent_pairs: set[tuple[str, str]] = set()
        barrier = threading.Barrier(3, timeout=30.0)
        orig_phase_b = flow_mod.CardRun._step_phase_b

        def tracked_phase_b(self_run, *args, **kwargs):
            with lock:
                for other in active_impl:
                    concurrent_pairs.add(tuple(sorted((self_run.card.id, other))))
                active_impl.add(self_run.card.id)
            barrier.wait()
            try:
                return orig_phase_b(self_run, *args, **kwargs)
            finally:
                with lock:
                    active_impl.discard(self_run.card.id)

        cli_mod.BuildLimiter = tracked_limiter
        self.addCleanup(setattr, cli_mod, "BuildLimiter", orig_limiter_cls)
        flow_mod.CardRun._step_phase_b = tracked_phase_b
        self.addCleanup(setattr, flow_mod.CardRun, "_step_phase_b", orig_phase_b)

        code_p, out_p, err_p = _run_cli(
            par_toy.root, "run", "--cards", "3", "--script", str(p_script_path)
        )
        self.assertEqual(code_p, EXIT_OK, err_p)
        self.assertEqual(len(created_limiters), 1)
        self.assertEqual(created_limiters[0].limit, 2)
        self.assertGreaterEqual(created_limiters[0].max_active, 1)
        self.assertLessEqual(created_limiters[0].max_active, 2)
        self.assertEqual(created_limiters[0].active, 0)
        self.assertEqual(
            concurrent_pairs,
            {("CARD-1", "CARD-2"), ("CARD-1", "CARD-3"), ("CARD-2", "CARD-3")},
        )

        s_by_id = {o["card_id"]: o for o in out_s["outcomes"]}
        p_by_id = {o["card_id"]: o for o in out_p["outcomes"]}
        self.assertEqual(set(s_by_id.keys()), {"CARD-1", "CARD-2", "CARD-3"})
        self.assertEqual(set(p_by_id.keys()), {"CARD-1", "CARD-2", "CARD-3"})

        branch_names = par_toy.git.branches("openfarm/*")

        for cid in ("CARD-1", "CARD-2", "CARD-3"):
            self.assertEqual(s_by_id[cid]["final_state"], "in-review")
            self.assertEqual(p_by_id[cid]["final_state"], "in-review")
            s_dossier = read_json(
                Path(s_by_id[cid]["dossier_path"]).with_suffix(".json")
            )
            p_dossier = read_json(
                Path(p_by_id[cid]["dossier_path"]).with_suffix(".json")
            )
            self.assertEqual(s_dossier["1_risk"]["unformatted"], [])
            self.assertEqual(p_dossier["1_risk"]["unformatted"], [])
            self.assertTrue(p_dossier["2_gates"])
            self.assertTrue(
                all(g["result"] == "pass" for g in p_dossier["2_gates"]),
                p_dossier["2_gates"],
            )
            self.assertEqual(
                [(g["name"], g["result"]) for g in s_dossier["2_gates"]],
                [(g["name"], g["result"]) for g in p_dossier["2_gates"]],
            )
            self.assertEqual(s_dossier["9_diffs"], p_dossier["9_diffs"])

            matching = [b for b in branch_names if b.startswith(f"openfarm/{cid}-")]
            self.assertEqual(len(matching), 1, branch_names)
            self._verify_branch_cargo_gates(par_toy.root, matching[0])

    def test_overlapping_touches_rust_cards_never_run_concurrently(self) -> None:
        self.assertTrue(cargo_available(), "cargo is required when OPENFARM_E2E=1")
        toy, script = _setup_three_module_rust_toy(
            concurrent_cards=3, concurrent_builds=2
        )
        self.addCleanup(toy.cleanup)

        c2_path = toy.root / "cards" / "CARD-2.md"
        c2_raw = c2_path.read_text(encoding="utf-8")
        c2_path.write_text(
            c2_raw.replace('touches = ["src/beta.rs"]', 'touches = ["src/*.rs"]'),
            encoding="utf-8",
        )
        toy.commit("CARD-2 glob touches overlapping CARD-1 and CARD-3")

        script_dir = Path(tempfile.mkdtemp(prefix="openfarm-rust-script-"))
        self.addCleanup(shutil.rmtree, script_dir, True)
        script_path = script_dir / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")

        from openfarm_core import flow as flow_mod

        lock = threading.Lock()
        active_impl: set[str] = set()
        concurrent_pairs: set[tuple[str, str]] = set()
        barrier = threading.Barrier(2, timeout=30.0)
        orig_phase_b = flow_mod.CardRun._step_phase_b

        def tracked_phase_b(self_run, *args, **kwargs):
            with lock:
                for other in active_impl:
                    concurrent_pairs.add(tuple(sorted((self_run.card.id, other))))
                active_impl.add(self_run.card.id)
            if self_run.card.id in ("CARD-1", "CARD-3"):
                barrier.wait()
            try:
                return orig_phase_b(self_run, *args, **kwargs)
            finally:
                with lock:
                    active_impl.discard(self_run.card.id)

        flow_mod.CardRun._step_phase_b = tracked_phase_b
        self.addCleanup(setattr, flow_mod.CardRun, "_step_phase_b", orig_phase_b)

        code, out, err = _run_cli(
            toy.root, "run", "--cards", "3", "--script", str(script_path)
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(
            {o["card_id"]: o["final_state"] for o in out["outcomes"]},
            {"CARD-1": "in-review", "CARD-2": "in-review", "CARD-3": "in-review"},
        )
        self.assertNotIn(("CARD-1", "CARD-2"), concurrent_pairs)
        self.assertNotIn(("CARD-2", "CARD-3"), concurrent_pairs)
        self.assertIn(("CARD-1", "CARD-3"), concurrent_pairs)


if __name__ == "__main__":
    unittest.main()
