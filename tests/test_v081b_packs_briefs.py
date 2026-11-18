"""The test writer's "Test runner" line (v0.8.1 fix wave 2, P6, rev-ts finding 16).

The line says which runner the project is set up for and that the tests must be
written for it. It claims nothing about what is installed: the runner is unittest
because nothing configures pytest, which says nothing about the machine.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import BriefBook, test_writer_brief
from openfarm_core.cards import Card
from openfarm_core.packs.python import PythonPack
from openfarm_core.packs.rust import RustPack


def _card() -> Card:
    return Card(id="C-1", title="t", fields={
        "category": "pinned-bugfix",
        "tests": [{"name": "test_x", "file": "tests/test_x.py", "asserts": "x"}]},
        touches=["tally/x.py"])


class TestRunnerLineTests(unittest.TestCase):
    def _brief(self, files: dict[str, str], pack=None) -> str:
        root = Path(tempfile.mkdtemp(prefix="openfarm-brief-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        if pack is None:
            pack = PythonPack(ruff=False)
            pack.configure(root)
        return test_writer_brief(_card(), BriefBook(root, [], []), pack).body

    def _line(self, body: str) -> str:
        lines = [line for line in body.splitlines() if line.startswith("Test runner:")]
        self.assertEqual(len(lines), 1, body)
        return lines[0]

    def test_unittest_names_the_setup_and_claims_nothing_installed(self) -> None:
        line = self._line(self._brief({"tests/__init__.py": ""}))
        self.assertIn("set up for Python's stdlib `unittest`", line)
        self.assertIn("Write the tests for `unittest`", line)
        self.assertNotIn("installed", line)

    def test_pytest_names_the_setup(self) -> None:
        for name, text in (("pytest.ini", "[pytest]\n"),
                           ("pyproject.toml", "[tool.pytest.ini_options]\n")):
            with self.subTest(name):
                line = self._line(self._brief({name: text}))
                self.assertIn("set up for `pytest`", line)
                self.assertIn("Write the tests for pytest", line)
                self.assertNotIn("installed", line)

    def test_rust_has_no_runner_line(self) -> None:
        body = self._brief({"Cargo.toml": "[package]\n"}, pack=RustPack())
        self.assertNotIn("Test runner:", body)


if __name__ == "__main__":
    unittest.main()
