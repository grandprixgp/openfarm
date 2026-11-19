"""v0.9.1 language packs, polyglot eligibility, mutation, and banned-patterns tests."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from openfarm_core import mutate
from openfarm_core.cards import Card, TestRef
from openfarm_core.config import load_config
from openfarm_core.config_template import render_config
from openfarm_core.gates.checks import mutation_gate
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.packs.go import GoPack
from openfarm_core.packs.rust import RustPack
from openfarm_core.packs.typescript import TypeScriptPack


class PacksPolyglotMutationTests(unittest.TestCase):
    def test_vitest_t_regex_matches_describe_fullname_and_json_noise(self) -> None:
        pack = TypeScriptPack(runner="vitest")
        argv = pack.commands("test", tests=["adds numbers"])["argv"]
        pattern = argv[argv.index("-t") + 1]
        self.assertIsNotNone(re.search(pattern, "math adds numbers"))
        self.assertIsNotNone(re.search(pattern, "adds numbers"))
        self.assertIsNone(re.search(pattern, "adds numbers extra"))

        noisy = '{"debug": 1}\n' + json.dumps({
            "success": True,
            "testResults": [{
                "name": "tests/math.test.ts",
                "status": "passed",
                "assertionResults": [
                    {"title": "adds numbers", "fullName": "math adds numbers", "status": "passed"},
                ],
            }],
        })
        parsed = pack.parse_tests(noisy)
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(len(parsed["results"]), 1)
        self.assertEqual(parsed["results"][0]["status"], "ok")

    def test_polyglot_eligibility_uses_unit_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "api").mkdir(parents=True)
            (root / "api" / "go.mod").write_text("module example.com/api\n\ngo 1.22\n")
            (root / "api" / "handler.go").write_text("package api\n")
            text = render_config(
                name="poly", language="python", base_branch="main",
                state_dir=Path(tmp) / "state", key_file=Path(tmp) / "key",
            ) + '\n[units.api]\nlanguage = "go"\n'
            cfg_path = root / ".openfarm" / "config.toml"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(text)
            cfg = load_config(root)

            card = Card.from_dict({
                "id": "CARD-1", "title": "Go handler", "state": "ready",
                "unit": "api", "touches": ["api/handler.go"],
                "tests": [{"name": "TestHandler", "file": "api/handler_test.go"}],
                "fields": {"Category": "pinned-bugfix", "Done": "Handler returns 200."},
            })
            elig = check_eligibility(card, cfg, worktree=root)
            rules = [f["rule"] for f in elig.failures]
            self.assertNotIn("python-tests-in-test-files", rules, elig.failures)

    def test_go_subtest_run_pattern_and_block_comment_bypass(self) -> None:
        pack = GoPack()
        argv = pack.commands("test", tests=["TestFoo/sub"])["argv"]
        run_pat = argv[argv.index("-run") + 1]
        self.assertEqual(run_pat, "^TestFoo$/^sub$")

        # Block comment containing // must NOT hide panic() or os/exec
        res = pack.patterns([
            {"path": "calc.go", "line": 5, "text": '/* // */ panic("boom")'},
            {"path": "calc.go", "line": 6, "text": '/* // */ import "os/exec"'},
        ])
        labels = {b["pattern"] for b in res["banned"]}
        self.assertIn("leftover-stub", labels)
        self.assertIn("process-call", labels)

    def test_rust_iterator_skip_allowed_and_block_comment_bypass_caught(self) -> None:
        pack = RustPack()
        ok_res = pack.patterns([
            {"path": "src/lib.rs", "line": 10, "text": "let xs: Vec<_> = items.iter().skip(1).collect();"},
        ])
        self.assertEqual(ok_res["banned"], [])

        bad_res = pack.patterns([
            {"path": "src/lib.rs", "line": 11, "text": "/* // */ unsafe { todo!() }"},
        ])
        labels = {b["pattern"] for b in bad_res["banned"]}
        self.assertIn("unsafe", labels)

    def test_mutation_timeout_zero_judged_fails_and_delete_only_diff_skips(self) -> None:
        gate = mutation_gate(
            {"killed": 0, "missed": 0, "timeout": 3, "unviable": 0, "scoped": True},
            min_kill_rate=0.75, mode="required", category="pinned-bugfix",
            required_for=["test-backfill"],
        )
        self.assertEqual(gate.result, "fail")

        # Delete-only diff: lines=[] in --lines must produce 0 mutants, not whole-file!
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "a.py"
            f.write_text("def f(x):\n    return x + 1\n")
            out_dir = root / "out"
            import os, sys
            old = Path.cwd()
            try:
                os.chdir(root)
                rc = mutate.main([
                    "--lang", "python", "--out", str(out_dir),
                    "--test-argv", json.dumps([sys.executable, "-c", "pass"]),
                    "--lines", json.dumps({"a.py": []}),
                    "a.py",
                ])
            finally:
                os.chdir(old)
            self.assertEqual(rc, mutate.EXIT_ALL_CAUGHT)
            outcomes = mutate.read_outcomes(out_dir, "", "", rc)
            self.assertEqual(outcomes["total"], 0)
            self.assertTrue(outcomes.get("scoped"))


if __name__ == "__main__":
    unittest.main()
