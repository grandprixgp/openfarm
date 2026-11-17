"""v0.8.1c glue, G7: a test process leaves nothing in its temp folder.

`tests/__init__.py` gives each test process a temp folder of its own, used by the
process and every program it starts, and removes it at exit; `ToyRepo.cleanup`
removes everything a run left beside the toy's repo. Each case runs a child Python
with `$TMPDIR` pointed at a fresh empty folder and looks at that folder afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import remove_tree

LEAKY = textwrap.dedent("""\
    import os, subprocess, sys, tempfile
    from pathlib import Path
    import tests
    # A folder nobody removes, a read-only one, and a child program's own.
    Path(tempfile.mkdtemp(prefix="openfarm-toy-"), "f").write_text("x")
    locked = Path(tempfile.mkdtemp())
    (locked / "inner").mkdir()
    (locked / "inner" / "f").write_text("x")
    (locked / "inner").chmod(0o500)
    locked.chmod(0o500)
    subprocess.run([sys.executable, "-c",
                    "import tempfile; open(tempfile.mkstemp()[1], 'w').write('x')"],
                   check=True)
    print(os.environ["TMPDIR"])
""")


class TestProcessTempFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-tmp-"))
        self.addCleanup(remove_tree, self.tmp)

    def _child(self, code: str, **env: str) -> subprocess.CompletedProcess:
        full = {k: v for k, v in os.environ.items() if k != "OPENFARM_KEEP_TEST_TMP"}
        full.update(TMPDIR=str(self.tmp), **env)
        return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=full,
                              capture_output=True, text=True, timeout=60)

    def test_what_a_test_process_forgets_is_removed_at_exit(self) -> None:
        result = self._child(LEAKY)
        self.assertEqual(result.returncode, 0, result.stderr)
        used = Path(result.stdout.strip())
        self.assertEqual(used.parent, self.tmp)
        self.assertTrue(used.name.startswith("openfarm-tests-"))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])

    def test_the_folder_can_be_kept_to_look_at(self) -> None:
        result = self._child(LEAKY, OPENFARM_KEEP_TEST_TMP="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        used = Path(result.stdout.strip())
        self.assertIn(f"kept the temp folder {used}", result.stderr)
        self.assertTrue(any(p.name.startswith("openfarm-toy-") for p in used.iterdir()))


class ToyCleanupTests(unittest.TestCase):
    def test_cleanup_removes_what_a_run_left_beside_the_repo(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        from tests.test_python_e2e import build_python_toy
        toy = build_python_toy()
        self.addCleanup(shutil.rmtree, toy.base, True)
        (toy.base / "wt-run-1").mkdir()
        (toy.base / "wt-run-1" / "a.py").write_text("x = 1\n")
        (toy.base / "script.json").write_text("{}")
        toy.cleanup()
        self.assertFalse(toy.base.exists())


if __name__ == "__main__":
    unittest.main()
