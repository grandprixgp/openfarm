from __future__ import annotations

import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_config_text
from openfarm_core.config import load_config
from openfarm_core.errors import Refused


class AdoptConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.state = self.tmp / "state"
        self.key = self.tmp / "key.json"

    def test_python_config_loads(self) -> None:
        text = adopt_config_text(
            self.root,
            "python",
            base_branch="trunk",
            state_dir=self.state,
            key_file=self.key,
        )
        config_path = self.root / ".openfarm" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(text, encoding="utf-8")

        config = load_config(self.root)
        self.assertEqual(config.languages, ["python"])
        self.assertEqual(config.base_branch, "trunk")

    def test_rust_config_forbids_manifests(self) -> None:
        text = adopt_config_text(
            self.root,
            "rust",
            base_branch="main",
            state_dir=self.state,
            key_file=self.key,
        )
        parsed = tomllib.loads(text)
        self.assertEqual(
            parsed["policy"]["forbidden_files"],
            ["**/Cargo.toml", "Cargo.lock", ".openfarm/**", ".claude/**"],
        )

    def test_python_config_forbids_manifests(self) -> None:
        text = adopt_config_text(
            self.root,
            "python",
            base_branch="main",
            state_dir=self.state,
            key_file=self.key,
        )
        parsed = tomllib.loads(text)
        self.assertEqual(
            parsed["policy"]["forbidden_files"],
            ["pyproject.toml", "setup.py", ".openfarm/**", ".claude/**"],
        )
        self.assertEqual(parsed["policy"]["never_touch"], [])

    def test_state_inside_the_repo_is_refused(self) -> None:
        with self.assertRaises(Refused) as raised:
            adopt_config_text(
                self.root,
                "python",
                base_branch="main",
                state_dir=self.root / "state",
                key_file=self.key,
            )
        self.assertEqual(raised.exception.code, "state-inside-repo")