from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cli import main


def write_task(root: Path, name: str, *, task_id: str | None,
               language: str, category: str) -> Path:
    task_dir = root / name
    task_dir.mkdir()
    (task_dir / "card.md").write_text("# card\n", encoding="utf-8")
    (task_dir / "hidden.py").write_text("# hidden\n", encoding="utf-8")
    lines: list[str] = []
    if task_id is not None:
        lines.append(f'id = "{task_id}"')
    lines.append(f'language = "{language}"')
    lines.append(f'category = "{category}"')
    lines.append('card = "card.md"')
    lines.append('hidden_tests = ["hidden.py"]')
    (task_dir / "task.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return task_dir


class BenchCliTest(unittest.TestCase):
    def test_bench_list_prints_tasks_sorted(self) -> None:
        tasks_dir = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tasks_dir, True)
        write_task(tasks_dir, "a-first", task_id="zeta",
                   language="python", category="small-feature")
        write_task(tasks_dir, "b-second", task_id="alpha",
                   language="rust", category="pattern")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--json", "bench", "list", "--dir", str(tasks_dir)])

        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload, {
            "tasks": [
                {"id": "alpha", "language": "rust", "category": "pattern"},
                {"id": "zeta", "language": "python",
                 "category": "small-feature"},
            ]
        })

    def test_a_bad_task_fails_the_listing(self) -> None:
        tasks_dir = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, tasks_dir, True)
        task_dir = write_task(tasks_dir, "bad-task", task_id=None,
                              language="python", category="small-feature")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--json", "bench", "list", "--dir", str(tasks_dir)])

        self.assertNotEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["kind"], "invalid")
        self.assertIn(str(task_dir), payload["error"])


if __name__ == "__main__":
    unittest.main()