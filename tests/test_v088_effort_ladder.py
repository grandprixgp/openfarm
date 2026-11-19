"""v0.8.8: configurable worker retry effort ladder (medium -> high -> max) and 3-retry default."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.config import ConfigError, DEFAULT_EFFORT_LADDER, load_config
from openfarm_core.config_template import render_config
from openfarm_core.flow import CardRun


class EffortLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v088-ladder-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def _write_cfg(self, model_extra: str = "", budget_extra: str = "") -> Path:
        of = self.repo / ".openfarm"
        of.mkdir(parents=True, exist_ok=True)
        state = self.tmp / "state"
        key = self.tmp / "key.txt"
        key.write_text("k\n")
        (of / "config.toml").write_text(
            f"""
[project]
name = "demo"
state_dir = "{state}"

[adapters]
model = "openai-compatible"
micromanager = "off"

[model]
endpoint = "https://api.example.invalid/v1"
allowed_endpoints = ["https://api.example.invalid/v1"]
key_file = "{key}"
implementer = {{ model = "m" }}
{model_extra}

[policy]
categories = ["pinned-bugfix"]

[budget]
{budget_extra}
""".strip() + "\n"
        )
        return self.repo

    def test_default_implementer_attempts_gives_at_least_three_retries(self) -> None:
        cfg = load_config(self._write_cfg())
        # 1 initial attempt + 3 retries minimum = 4 total attempts.
        self.assertGreaterEqual(cfg.implementer_attempts, 4)

    def test_model_level_effort_ladder_escalates_medium_high_max(self) -> None:
        self.assertEqual(DEFAULT_EFFORT_LADDER, ("medium", "high", "max"))
        cfg = load_config(self._write_cfg('effort_ladder = ["medium", "high", "max"]\n'))
        self.assertEqual(cfg.effort_for_role("implementer", 1), ("medium", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("implementer", 2), ("high", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("implementer", 3), ("max", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("implementer", 4), ("max", "reasoning_effort"))

    def test_role_level_effort_ladder_overrides_model_ladder(self) -> None:
        cfg = load_config(self._write_cfg(
            'effort_ladder = ["medium", "high", "max"]\n'
            'test_writer = { model = "m", effort_ladder = ["low", "high"] }\n'
        ))
        self.assertEqual(cfg.effort_for_role("test_writer", 1), ("low", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("test_writer", 2), ("high", "reasoning_effort"))
        self.assertEqual(cfg.effort_for_role("test_writer", 3), ("high", "reasoning_effort"))

    def test_coordinator_cannot_lower_effort_below_ladder(self) -> None:
        cfg = load_config(self._write_cfg('effort_ladder = ["medium", "high", "max"]\n'))
        flow = object.__new__(CardRun)
        flow.cfg = cfg
        # Attempt 2 ladder is high; coordinator passing "low" still gets "high".
        self.assertEqual(flow._retry_effort("implementer", "low", 2), "high")
        # Attempt 3 ladder is max; coordinator passing "high" still gets "max".
        self.assertEqual(flow._retry_effort("implementer", "high", 3), "max")

    def test_invalid_effort_in_ladder_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self._write_cfg('effort_ladder = ["medium", "turbo"]\n'))

    def test_render_config_includes_default_ladder_and_four_attempts(self) -> None:
        rendered = render_config(
            name="demo", language="python", base_branch="main",
            state_dir=self.tmp / "st", key_file=self.tmp / "k",
        )
        self.assertIn('effort_ladder = ["medium", "high", "max"]', rendered)
        self.assertIn("implementer_attempts = 4", rendered)


if __name__ == "__main__":
    unittest.main()
