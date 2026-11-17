from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.adopt import detect_language
from openfarm_core.errors import Refused


class AdoptDetectTests(unittest.TestCase):
    def new_root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_a_cargo_manifest_means_rust(self) -> None:
        root = self.new_root()
        (root / "Cargo.toml").write_text("")

        self.assertEqual(detect_language(root), "rust")

    def test_a_pyproject_or_setup_means_python(self) -> None:
        pyproject_root = self.new_root()
        (pyproject_root / "pyproject.toml").write_text("")
        self.assertEqual(detect_language(pyproject_root), "python")

        setup_root = self.new_root()
        (setup_root / "setup.py").write_text("")
        self.assertEqual(detect_language(setup_root), "python")

    def test_a_package_or_tsconfig_means_typescript(self) -> None:
        package_root = self.new_root()
        (package_root / "package.json").write_text("{}\n")
        self.assertEqual(detect_language(package_root), "typescript")

        ts_root = self.new_root()
        (ts_root / "tsconfig.json").write_text("{}\n")
        self.assertEqual(detect_language(ts_root), "typescript")

    def test_both_manifests_are_ambiguous(self) -> None:
        root = self.new_root()
        (root / "Cargo.toml").write_text("")
        (root / "pyproject.toml").write_text("")

        with self.assertRaises(Refused) as caught:
            detect_language(root)

        self.assertEqual(caught.exception.code, "ambiguous-language")

        ts_rust = self.new_root()
        (ts_rust / "Cargo.toml").write_text("")
        (ts_rust / "package.json").write_text("{}\n")
        with self.assertRaises(Refused) as caught2:
            detect_language(ts_rust)
        self.assertEqual(caught2.exception.code, "ambiguous-language")

        ts_py = self.new_root()
        (ts_py / "pyproject.toml").write_text("")
        (ts_py / "tsconfig.json").write_text("{}\n")
        with self.assertRaises(Refused) as caught3:
            detect_language(ts_py)
        self.assertEqual(caught3.exception.code, "ambiguous-language")

    def test_no_manifest_is_unsupported(self) -> None:
        empty_root = self.new_root()
        with self.assertRaises(Refused) as caught:
            detect_language(empty_root)
        self.assertEqual(caught.exception.code, "unsupported-language")