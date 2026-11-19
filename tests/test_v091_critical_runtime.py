"""v0.9.1 critical runtime fixes: git hooksPath, single-pass adjudicate, land safety."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfarm_core.git import Git
from tests.test_python_e2e import (
    GOOD_IMPLEMENTATION,
    GOOD_TEST,
    build_python_toy,
    run_cli,
    script,
)


def _add_card1(toy) -> None:
    toy.write_card(
        "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
        unit=".", touches=["tally/bounded.py"],
        tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
        done_text="`Bounded(1).push(8)` after one push raises `FullError`.",
    )
    toy.commit("add CARD-1")


class CriticalRuntimeTests(unittest.TestCase):
    def test_git_run_disables_repo_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            git = Git(root)
            git.init("main")
            marker = root / "hook-ran.txt"
            hook = root / ".git" / "hooks" / "pre-commit"
            hook.write_text(f"#!/bin/sh\necho pwned > {marker}\nexit 1\n")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

            (root / "a.txt").write_text("hello\n")
            git._run(["add", "a.txt"])
            git._run(["commit", "-m", "init"])
            self.assertFalse(marker.exists(), "git pre-commit hook should never run")

    def test_single_pass_checker_counter_test_adjudicate_no_unbound_local(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        _add_card1(toy)
        cfg_path = toy.root / ".openfarm" / "config.toml"
        text = cfg_path.read_text()
        text = text.replace('micromanager = "off"', 'micromanager = "scripted"')
        text = text.replace('mode = "off"', 'mode = "active"')
        text = text.replace('counter_tests = "report"', 'counter_tests = "reject"')
        if "checker_mode" not in text:
            text = text.replace("[gates]\n", '[gates]\nchecker_mode = "single-pass"\n')
        cfg_path.write_text(text)
        toy.commit("enable single-pass + mm")

        sp = script(**{
            "CARD-1/micromanager": json.dumps({"verdict": "ready", "plan": [], "notes": "ok"}),
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass2": (
                "VERDICT: changes-requested\n"
                "REASONS: edge case\n"
                "TRACEABILITY:\n- Done -> test\n"
                "EDGE_CASES: none\n"
                "=== whole_file: tests/test_counter_1.py\n"
                "import unittest\n"
                "class TestCounter(unittest.TestCase):\n"
                "    def test_counter_fail(self):\n"
                "        self.assertEqual(1, 2)\n"
            ),
            "CARD-1/micromanager_adjudicate": json.dumps({
                "verdict": "dismiss",
                "reason": "counter-test is out of scope",
            }),
            "CARD-1/micromanager_pre_review": json.dumps({
                "verdict": "approve",
                "findings": [],
            }),
        })
        res = run_cli(toy.root, "run", "--script", str(sp), "--cards", "1")
        self.assertNotIn("UnboundLocalError", res.stderr + res.stdout)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["outcomes"]), 1)

    def test_land_refuses_blocked_or_unapproved_or_bounced_card(self) -> None:
        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        _add_card1(toy)
        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full raises FullError",
            "CARD-1/checker_pass2": "VERDICT: accept\nREASONS: ok\nTRACEABILITY:\n- Done -> test\nEDGE_CASES: none\n",
        })
        res = run_cli(toy.root, "run", "--script", str(sp), "--cards", "1")
        self.assertEqual(json.loads(res.stdout)["outcomes"][0]["final_state"], "in-review")

        # 1. No verdict yet -> land MUST refuse
        land_no_verdict = run_cli(toy.root, "land", "CARD-1")
        self.assertNotEqual(land_no_verdict.returncode, 0, land_no_verdict.stdout)

        # 2. Bounced verdict -> land MUST refuse
        run_cli(toy.root, "review", "--verdict", "bounce", "--note", "needs rework")
        land_bounced = run_cli(toy.root, "land", "CARD-1")
        self.assertNotEqual(land_bounced.returncode, 0, land_bounced.stdout)

        # 3. Clean verdict -> land succeeds
        run_cli(toy.root, "review", "--verdict", "clean", "--note", "ok")
        land_ok = run_cli(toy.root, "land", "CARD-1")
        self.assertEqual(land_ok.returncode, 0, land_ok.stdout + land_ok.stderr)


if __name__ == "__main__":
    unittest.main()
