"""`openfarm adopt` runs the suite once and proposes its red tests as known_failing."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import adopt_project
from openfarm_core.errors import Refused
from openfarm_core.git import Git

E2E = os.environ.get("OPENFARM_E2E") == "1"

RED_TESTS = '''import unittest


class Tests(unittest.TestCase):
    def test_green(self):
        self.assertTrue(True)

    def test_red(self):
        self.assertEqual(1, 2)
'''


class AdoptBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-",
                                         dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "widget"
        self.root.mkdir()

    def _commit(self, files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git = Git(self.root)
        git.init(branch="main")
        git.commit_all("initial")

    def _adopt(self) -> dict:
        return adopt_project(self.root, state_dir=self.tmp / "state",
                             key_file=self.tmp / "widget.key", run_suite=True)

    def _config(self) -> str:
        return (self.root / ".openfarm" / "config.toml").read_text()

    def test_red_tests_are_proposed_in_a_comment_not_set(self) -> None:
        self._commit({"pyproject.toml": '[project]\nname = "widget"\n',
                      "tests/__init__.py": "", "tests/test_widget.py": RED_TESTS})
        result = self._adopt()
        self.assertTrue(result["suite_ran"])
        self.assertEqual(result["known_failing_proposed"], ["test_red"])
        config = self._config()
        self.assertEqual(tomllib.loads(config)["policy"]["known_failing"], [])
        self.assertIn('# ["test_red"]', config)

    def test_a_green_suite_proposes_nothing(self) -> None:
        self._commit({"pyproject.toml": '[project]\nname = "widget"\n',
                      "tests/__init__.py": "",
                      "tests/test_widget.py": RED_TESTS.replace("1, 2", "1, 1")})
        result = self._adopt()
        self.assertEqual(result["known_failing_proposed"], [])
        self.assertNotIn("adopt ran the suite", self._config())

    def test_no_tests_folder_is_not_run(self) -> None:
        self._commit({"pyproject.toml": '[project]\nname = "widget"\n',
                      "app.py": "print('hi')\n"})
        result = self._adopt()
        self.assertFalse(result["suite_ran"])
        self.assertTrue(any("tests/" in note for note in result["suite_notes"]))

    @unittest.skipUnless(E2E, "cargo builds; set OPENFARM_E2E=1")
    def test_a_crate_that_does_not_build_is_refused_before_writing(self) -> None:
        self._commit({"Cargo.toml": '[package]\nname = "widget"\nversion = "0.1.0"\n'
                                    'edition = "2021"\n',
                      "src/lib.rs": "pub fn broken() -> u32 { \"no\" }\n"})
        with self.assertRaises(Refused) as caught:
            self._adopt()
        self.assertEqual(caught.exception.code, "suite-does-not-compile")
        self.assertFalse((self.root / ".openfarm").exists())
        self.assertFalse((self.root / "target").exists())


if __name__ == "__main__":
    unittest.main()
