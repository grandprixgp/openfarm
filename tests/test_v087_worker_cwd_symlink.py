"""ACP worker empty cwd must resolve symlinks in TMPDIR before bwrap mounts it.

When TMPDIR sits behind a symlink (/var/tmp/openfarm-v085 ->
/scratch/openfarm-v085), bwrap mounts the canonical path while codex-exec
received `--cd /var/tmp/...` and failed with `No such file or directory (os error 2)`.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.adapters.claude_worker import ClaudeAcpWorkerBackend


class WorkerCwdSymlinkTests(unittest.TestCase):
    def test_own_cwd_resolves_symlinked_tmpdir(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="of-v087-wcwd-")).resolve()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        real_tmp = base / "real"
        real_tmp.mkdir()
        link_tmp = base / "link"
        link_tmp.symlink_to(real_tmp)

        backend = ClaudeAcpWorkerBackend(provider="codex")
        with mock.patch("tempfile.tempdir", str(link_tmp)):
            cwd = backend.cwd
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        self.assertEqual(cwd, cwd.resolve())
        self.assertTrue(str(cwd).startswith(str(real_tmp)))


if __name__ == "__main__":
    unittest.main()
