"""U5: bare `openfarm bench` outside the openfarm checkout.

v0.8 exited 3 with "bench: no such directory", without saying what bench needs or
how to point it at a folder of tasks; `bench list` and `bench run` did the same.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cli import EXIT_USAGE, main  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _cli(*argv: str, cwd: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class BareBenchOutsideTheRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(os.environ.get("TMPDIR") or "/var/tmp")
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081-bench-", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_bare_bench_says_what_it_needs_with_the_usage_code(self) -> None:
        code, out, err = _cli("bench", cwd=self.tmp)
        self.assertEqual(code, EXIT_USAGE, out + err)
        self.assertNotIn("no such directory", err)
        self.assertIn("bench needs a folder of bench tasks", err)
        self.assertIn("--tasks", err)
        self.assertIn("task.toml", err)

    def test_bench_list_without_a_folder_says_so_plainly(self) -> None:
        code, out, err = _cli("bench", "list", cwd=self.tmp)
        self.assertEqual(code, EXIT_USAGE, out + err)
        self.assertIn("no bench tasks folder at bench", err)
        self.assertIn("--tasks", err)
        code, out, _ = _cli("--json", "bench", "list", "--tasks", str(self.tmp / "nope"),
                            cwd=self.tmp)
        self.assertEqual(code, EXIT_USAGE)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "usage error")
        self.assertIn("no bench tasks folder", payload["error"])

    def test_bench_run_without_a_folder_says_so_plainly(self) -> None:
        code, out, err = _cli("bench", "run", "--dry-run", cwd=self.tmp)
        self.assertEqual(code, EXIT_USAGE, out + err)
        self.assertIn("no bench tasks folder", err)

    def test_tasks_points_it_at_a_folder_and_dir_still_works(self) -> None:
        for flag in ("--tasks", "--dir"):
            with self.subTest(flag=flag):
                code, out, err = _cli("--json", "bench", "list", flag, str(ROOT / "bench"),
                                      cwd=self.tmp)
                self.assertEqual(code, 0, err)
                self.assertTrue(json.loads(out)["tasks"])

    def test_a_file_is_not_a_folder(self) -> None:
        f = self.tmp / "file.txt"
        f.write_text("x", encoding="utf-8")
        code, _out, err = _cli("bench", "list", "--tasks", str(f), cwd=self.tmp)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("is not a folder", err)


if __name__ == "__main__":
    unittest.main()
