from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bench import BenchTask, load_task
from openfarm_core.errors import Invalid

_VALID_TOML = (
    'id = "py-full"\n'
    'language = "python"\n'
    'category = "pinned-bugfix"\n'
    'card = "card.md"\n'
    'hidden_tests = ["hidden/test_full.py"]\n'
)


class BenchTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def _write_task(self, toml: str | None = None) -> None:
        self._write("task.toml", _VALID_TOML if toml is None else toml)
        self._write("card.md", "# the card\n")
        self._write("hidden/test_full.py", "def test_full():\n    pass\n")

    def test_a_valid_task_loads(self) -> None:
        self._write_task()

        task = load_task(self.root)

        self.assertIsInstance(task, BenchTask)
        self.assertEqual(task.id, "py-full")
        self.assertEqual(task.language, "python")
        self.assertEqual(task.category, "pinned-bugfix")
        self.assertEqual(task.card.resolve(), (self.root / "card.md").resolve())
        self.assertEqual(
            tuple(path.resolve() for path in task.hidden_tests),
            ((self.root / "hidden/test_full.py").resolve(),),
        )
        self.assertEqual(task.root.resolve(), self.root.resolve())

    def test_a_missing_key_is_named(self) -> None:
        self._write_task(
            'id = "py-full"\n'
            'language = "python"\n'
            'card = "card.md"\n'
            'hidden_tests = ["hidden/test_full.py"]\n'
        )

        with self.assertRaises(Invalid) as caught:
            load_task(self.root)

        self.assertEqual(caught.exception.code, "bad-bench-task")
        self.assertIn("category", str(caught.exception))

    def test_a_missing_hidden_test_file_is_refused(self) -> None:
        self._write_task(
            'id = "py-full"\n'
            'language = "python"\n'
            'category = "pinned-bugfix"\n'
            'card = "card.md"\n'
            'hidden_tests = ["hidden/missing.py"]\n'
        )

        with self.assertRaises(Invalid) as caught:
            load_task(self.root)

        self.assertEqual(caught.exception.code, "bad-bench-task")
        self.assertIn("hidden/missing.py", str(caught.exception))

    def test_an_unknown_language_is_refused(self) -> None:
        self._write_task(
            'id = "go-full"\n'
            'language = "go"\n'
            'category = "pinned-bugfix"\n'
            'card = "card.md"\n'
            'hidden_tests = ["hidden/test_full.py"]\n'
        )

        with self.assertRaises(Invalid) as caught:
            load_task(self.root)

        self.assertEqual(caught.exception.code, "bad-bench-task")


if __name__ == "__main__":
    unittest.main()
