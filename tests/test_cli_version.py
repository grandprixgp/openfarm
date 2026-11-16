from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openfarm_core
from openfarm_core.cli import main


class TestCliVersion(unittest.TestCase):
    def test_version_flag_prints_the_package_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(
            output.getvalue(),
            f"openfarm {openfarm_core.__version__}\n",
        )

    def test_version_flag_needs_no_repository(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="openfarm-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(tmpdir)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(
            output.getvalue(),
            f"openfarm {openfarm_core.__version__}\n",
        )
