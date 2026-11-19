"""Pilot round 2, 18 Nov 2026: cargo-mutants scored the whole file.

Python and TypeScript already scope openfarm's mutation runner to the lines a
card changed (`--lines`). Rust passed only `--file`, so pre-existing survivors
in untouched functions dragged down the kill rate (card B4: 75% below 80%).
Passing `--in-diff` when the card changed production lines scopes cargo-mutants
to the card's own diff while leaving test-backfill cards mutating whole files.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.edits import diff_text
from openfarm_core.errors import Invalid
from openfarm_core.packs.rust import RustPack


class RustMutationDiffCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v087-mutdiff-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pack = RustPack()
        self.pack.mutation_output = self.tmp / "mut-out"

    def test_in_diff_flag_written_to_output_dir_when_diff_set(self) -> None:
        diff = "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-x\n+y\n"
        self.pack.mutation_diff = diff
        argv = self.pack.commands("mutation", files=["src/lib.rs"])["argv"]
        self.assertIn("--in-diff", argv)
        diff_path = Path(argv[argv.index("--in-diff") + 1])
        self.assertEqual(diff_path.parent, Path(argv[argv.index("--output") + 1]))
        self.assertEqual(diff_path.read_text(), diff)

    def test_no_in_diff_flag_when_diff_empty_or_none(self) -> None:
        for val in (None, "", "   \n"):
            self.pack.mutation_diff = val
            argv = self.pack.commands("mutation", files=["src/lib.rs"])["argv"]
            self.assertNotIn("--in-diff", argv)

    def test_zero_mutants_in_diff_reads_as_zero_not_empty_error(self) -> None:
        self.pack.commands("mutation", files=["src/lib.rs"])
        parsed = self.pack.read_mutation("", "INFO No mutants to filter\n", exit_code=0)
        self.assertEqual(parsed["total"], 0)
        self.assertEqual(parsed["judged"], 0)
        self.assertEqual(parsed["killed"], 0)
        self.assertEqual(parsed["survived"], 0)

    def test_truly_empty_output_still_raises_invalid(self) -> None:
        self.pack.commands("mutation", files=["src/lib.rs"])
        with self.assertRaises(Invalid) as ctx:
            self.pack.read_mutation("", "", exit_code=0)
        self.assertEqual(ctx.exception.code, "empty-mutation")


class FlowMutationDiffWiringTests(unittest.TestCase):
    def _run_gate(self, *, changed_prod: bool) -> list[str]:
        from types import SimpleNamespace
        from openfarm_core.flow import CardRun

        tmp = Path(tempfile.mkdtemp(prefix="of-v087-flow-mut-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "src").mkdir()
        (tmp / "src" / "lib.rs").write_text("pub fn f() -> i32 { 1 }\n")
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "base"],
        ):
            subprocess.run(cmd, cwd=tmp, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True, check=True).stdout.strip()
        if changed_prod:
            (tmp / "src" / "lib.rs").write_text("pub fn f() -> i32 { 2 }\n")

        pack = RustPack(cargo_flags=[])
        seen_argv: list[str] = []

        def fake_run(_kind, argv, **_kw):
            seen_argv.extend(argv)
            return SimpleNamespace(cls="pass", exit_code=0, stdout="1 mutants tested: 1 caught\n", stderr="")

        flow = object.__new__(CardRun)
        flow.cfg = SimpleNamespace(mutation="report", mutation_required_for=("test-backfill",), mutation_min_kill_rate=0.8)
        flow.card = SimpleNamespace(id="CARD-1", category="pinned-bugfix" if changed_prod else "test-backfill", test_names=["t"])
        flow.pack = pack
        flow.worktree = tmp
        flow.base_commit = base
        flow.card_layout = tmp / "state"
        flow.budget = SimpleNamespace(mutation_builds=0, mutation_capped=lambda: False)
        flow.mutation_summary = {}
        flow._record_gate = lambda g: g
        flow._mutated_unit_files = lambda: ["src/lib.rs"]
        flow._run = fake_run
        flow._output = lambda r: (r.stdout, r.stderr)
        flow.log = lambda _msg: None

        flow._mutation_gate(["src/lib.rs"] if changed_prod else ["tests/t.rs"])
        return seen_argv

    def test_flow_passes_in_diff_when_production_changed(self) -> None:
        argv = self._run_gate(changed_prod=True)
        self.assertIn("--in-diff", argv)

    def test_flow_omits_in_diff_for_test_backfill(self) -> None:
        argv = self._run_gate(changed_prod=False)
        self.assertNotIn("--in-diff", argv)


@unittest.skipUnless(
    shutil.which("cargo-mutants") and shutil.which("cargo") and shutil.which("git"),
    "cargo-mutants, cargo and git are required",
)
class RustMutationInDiffRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v087-mutdiff-real-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "crate"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "Cargo.toml").write_text(
            '[package]\nname = "mutdiff"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        (self.repo / "src" / "lib.rs").write_text(
            "pub fn untouched(x: i32) -> i32 {\n    x + 1\n}\n"
            + "\n" * 8
            + "pub fn changed(x: i32) -> i32 {\n    x + 2\n}\n"
            + "\n#[cfg(test)]\nmod tests {\n    use super::*;\n"
            + "    #[test]\n    fn test_changed() {\n        assert_eq!(changed(3), 6);\n    }\n}\n"
        )
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "base"],
        ):
            subprocess.run(cmd, cwd=self.repo, check=True)
        self.env = dict(os.environ)
        self.env["CARGO_TARGET_DIR"] = str(self.tmp / "target")

    def test_only_changed_function_is_mutated(self) -> None:
        (self.repo / "src" / "lib.rs").write_text(
            "pub fn untouched(x: i32) -> i32 {\n    x + 1\n}\n"
            + "\n" * 8
            + "pub fn changed(x: i32) -> i32 {\n    x + 3\n}\n"
            + "\n#[cfg(test)]\nmod tests {\n    use super::*;\n"
            + "    #[test]\n    fn test_changed() {\n        assert_eq!(changed(3), 6);\n    }\n}\n"
        )
        pack = RustPack(cargo_flags=[])
        pack.mutation_output = self.tmp / "out"
        pack.mutation_diff = diff_text(self.repo, ["src/lib.rs"], ref="HEAD")
        argv = pack.commands("mutation", files=["src/lib.rs"])["argv"]
        res = subprocess.run(argv, cwd=self.repo, env=self.env, capture_output=True, text=True)
        parsed = pack.read_mutation(res.stdout, res.stderr, res.returncode)
        self.assertGreater(parsed["killed"], 0)
        self.assertEqual(parsed["survived"], 0)

    def test_comment_only_diff_returns_zero_mutants(self) -> None:
        (self.repo / "src" / "lib.rs").write_text(
            "// just a comment\n"
            "pub fn untouched(x: i32) -> i32 {\n    x + 1\n}\n"
            + "\n" * 8
            + "pub fn changed(x: i32) -> i32 {\n    x + 2\n}\n"
            + "\n#[cfg(test)]\nmod tests {\n    use super::*;\n"
            + "    #[test]\n    fn test_changed() {\n        assert_eq!(changed(3), 5);\n    }\n}\n"
        )
        pack = RustPack(cargo_flags=[])
        pack.mutation_output = self.tmp / "out-comment"
        pack.mutation_diff = diff_text(self.repo, ["src/lib.rs"], ref="HEAD")
        argv = pack.commands("mutation", files=["src/lib.rs"])["argv"]
        res = subprocess.run(argv, cwd=self.repo, env=self.env, capture_output=True, text=True)
        parsed = pack.read_mutation(res.stdout, res.stderr, res.returncode)
        self.assertEqual(parsed["total"], 0)
        self.assertEqual(parsed["judged"], 0)


if __name__ == "__main__":
    unittest.main()
