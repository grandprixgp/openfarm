"""v0.9.0: counter_tests = 'reject' default and named supervision profiles."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.config import (
    ConfigError,
    SUPERVISION_PROFILES,
    apply_supervision_profile,
    load_config,
)
from openfarm_core.config_template import render_config


class DefaultsAndProfilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v090-prof-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        of = self.repo / ".openfarm"
        of.mkdir()
        key = self.tmp / "key.txt"
        key.write_text("k\n")
        (of / "config.toml").write_text(
            f"""
[project]
name = "demo"
state_dir = "{self.tmp / 'state'}"

[adapters]
model = "scripted"
micromanager = "scripted"

[model]
key_file = "{key}"

[policy]
categories = ["pinned-bugfix"]
""".strip() + "\n"
        )

    def test_counter_tests_defaults_to_reject(self) -> None:
        cfg = load_config(self.repo)
        self.assertEqual(cfg.counter_tests, "reject")
        rendered = render_config(
            name="demo", language="python", base_branch="main",
            state_dir=self.tmp / "st", key_file=self.tmp / "k",
        )
        self.assertIn('counter_tests = "reject"', rendered)

    def test_supervision_profiles_configure_expected_knobs(self) -> None:
        self.assertEqual(SUPERVISION_PROFILES, ("lean", "balanced", "strict"))

        cfg_lean = load_config(self.repo)
        apply_supervision_profile(cfg_lean, "lean")
        self.assertEqual(cfg_lean.micromanager.shape_when, "new-symbols")
        self.assertEqual(cfg_lean.micromanager.pre_review_when, "on-flag")
        self.assertEqual(cfg_lean.checker_mode, "single-pass")
        self.assertEqual(cfg_lean.counter_tests, "reject")

        cfg_bal = load_config(self.repo)
        apply_supervision_profile(cfg_bal, "balanced")
        self.assertEqual(cfg_bal.micromanager.shape_when, "always")
        self.assertEqual(cfg_bal.micromanager.pre_review_when, "always")
        self.assertEqual(cfg_bal.checker_mode, "two-pass")
        self.assertEqual(cfg_bal.counter_tests, "reject")

        cfg_strict = load_config(self.repo)
        apply_supervision_profile(cfg_strict, "strict")
        self.assertEqual(cfg_strict.mutation, "required")
        self.assertEqual(cfg_strict.runner_confine, "bwrap")
        self.assertEqual(cfg_strict.runner_confine_network, "off")
        self.assertEqual(cfg_strict.counter_tests, "reject")

        with self.assertRaises(ConfigError):
            apply_supervision_profile(cfg_strict, "unknown")


if __name__ == "__main__":
    unittest.main()
