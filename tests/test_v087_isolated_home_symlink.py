"""Isolated homes must resolve symlinks in TMPDIR before bwrap mounts them.

When TMPDIR sits behind a symlink (for example /var/tmp/openfarm-v085 ->
/scratch/openfarm-v085), bwrap bind-mounts the resolved target path while
HOME, CODEX_HOME and Claude's apiKeyHelper pointed at the unresolved symlink,
so confined children failed with FileNotFoundError.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.adapters.acp import _sanitised_env


class IsolatedHomeSymlinkTests(unittest.TestCase):
    def test_isolated_home_resolves_symlinked_tmpdir(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="of-v087-symtmp-")).resolve()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        real_tmp = base / "real"
        real_tmp.mkdir()
        link_tmp = base / "link"
        link_tmp.symlink_to(real_tmp)

        with mock.patch("tempfile.tempdir", str(link_tmp)):
            env, isolated_home = _sanitised_env(provider="codex", isolate_home=True)
        self.assertIsNotNone(isolated_home)
        self.addCleanup(shutil.rmtree, isolated_home, ignore_errors=True)
        self.assertEqual(isolated_home, isolated_home.resolve())
        self.assertTrue(str(isolated_home).startswith(str(real_tmp)))
        self.assertTrue(env["HOME"].startswith(str(real_tmp)))
        self.assertTrue(env["CODEX_HOME"].startswith(str(real_tmp)))


if __name__ == "__main__":
    unittest.main()
