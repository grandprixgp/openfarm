"""v0.10.0 Pillar 2 tests: Multi-stack project initialization (TS/Web, Python, Rust, Go, Auto)."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openfarm_core.bootstrap import initialize_project
from openfarm_core.cli import main as cli_main
from openfarm_core.config import load_config


class MultiStackInitV010Tests(unittest.TestCase):
    def test_init_python_stack_and_green_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "py-demo"
            res = initialize_project(
                "py-demo",
                directory=root,
                language="python",
                idea="Track daily notes",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                no_git=True,
            )
            self.assertEqual(res["language"], "python")
            self.assertTrue((root / "pyproject.toml").is_file())
            self.assertTrue((root / "src" / "py_demo" / "invariants.py").is_file())
            self.assertTrue((root / "src" / "py_demo" / "__init__.py").is_file())
            self.assertTrue((root / "tests" / "test_baseline.py").is_file())
            cfg = load_config(root)
            self.assertEqual(cfg.languages, ["python"])
            self.assertIn("src/py_demo/invariants.py", cfg.never_touch)

            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=root,
                env={"PYTHONPATH": str(root / "src")},
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_init_typescript_stack_and_green_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photo-web"
            res = initialize_project(
                "photo-web",
                directory=root,
                language="typescript",
                idea="Image of the day website",
                state_dir=Path(tmp) / "state",
                secrets_dir=Path(tmp) / "secrets",
                no_git=True,
            )
            self.assertEqual(res["language"], "typescript")
            self.assertTrue((root / "package.json").is_file())
            self.assertTrue((root / "tsconfig.json").is_file())
            self.assertTrue((root / "src" / "invariants.ts").is_file())
            self.assertTrue((root / "src" / "index.ts").is_file())
            self.assertTrue((root / "tests" / "baseline.test.ts").is_file())
            cfg = load_config(root)
            self.assertEqual(cfg.languages, ["typescript"])
            self.assertIn("src/invariants.ts", cfg.never_touch)

            if shutil.which("node"):
                proc = subprocess.run(
                    ["node", "--test", "tests/baseline.test.ts"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_init_go_stack_and_auto_detect_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            go_root = Path(tmp) / "go-svc"
            res = initialize_project(
                "go-svc",
                directory=go_root,
                language="go",
                idea="Tiny Go service",
                state_dir=Path(tmp) / "state-go",
                secrets_dir=Path(tmp) / "secrets-go",
                no_git=True,
            )
            self.assertEqual(res["language"], "go")
            self.assertTrue((go_root / "go.mod").is_file())
            self.assertTrue((go_root / "invariants.go").is_file())
            self.assertTrue((go_root / "main.go").is_file())
            self.assertTrue((go_root / "baseline_test.go").is_file())
            cfg = load_config(go_root)
            self.assertEqual(cfg.languages, ["go"])
            self.assertIn("invariants.go", cfg.never_touch)

            auto_root = Path(tmp) / "web-auto"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main([
                    "--json", "init", "web-auto",
                    "--lang", "auto",
                    "--idea", "Interactive photo website with dark mode UI",
                    "--dir", str(auto_root),
                    "--state-dir", str(Path(tmp) / "state-auto"),
                    "--secrets-dir", str(Path(tmp) / "secrets-auto"),
                    "--no-git",
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["language"], "typescript")


if __name__ == "__main__":
    unittest.main()
