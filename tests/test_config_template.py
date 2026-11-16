"""`openfarm init` and `openfarm adopt` write their config from one template."""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_config_text
from openfarm_core.bootstrap import _render_config_toml


def _keys(text: str) -> dict:
    return {table: set(body) if isinstance(body, dict) else None
            for table, body in tomllib.loads(text).items()}


class ConfigTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.init = _render_config_toml(
            project_slug="calorie-log", base_branch="main",
            state_dir=Path("/srv/state"), endpoint="https://api.example.invalid/v1",
            key_file=Path("/srv/key"), model="m", mm_adapter="claude-acp",
            mm_mode="active", mutation="off")

    def test_init_and_adopt_set_the_same_keys(self) -> None:
        adopt = adopt_config_text(Path("/srv/repo"), "rust", base_branch="main",
                                  state_dir=Path("/srv/state"),
                                  key_file=Path("/srv/key"))
        self.assertEqual(_keys(self.init), _keys(adopt))

    def test_the_supervisor_gets_ten_minutes(self) -> None:
        # 180s was too short live: planning a bigger card took longer (CARD-008).
        self.assertEqual(tomllib.loads(self.init)["micromanager"]["timeout_s"], 600.0)

    def test_python_gets_python_rules(self) -> None:
        config = tomllib.loads(adopt_config_text(
            Path("/srv/repo"), "python", base_branch="main",
            state_dir=Path("/srv/state"), key_file=Path("/srv/key")))
        self.assertTrue(config["eligibility"]["python_tests_in_test_files"])
        self.assertNotIn("rust_tests_in_separate_files", config["eligibility"])
        self.assertEqual(config["runner"]["python"], "")
        self.assertIn("pyproject.toml", config["policy"]["forbidden_files"])


if __name__ == "__main__":
    unittest.main()
