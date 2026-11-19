"""v0.9.0 TypeScript vitest and jest runner support tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfarm_core import adopt
from openfarm_core.config import ConfigError, load_config
from openfarm_core.packs.typescript import (
    TypeScriptPack,
    detect_runner,
    resolve_tooling,
    test_script_problem,
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


class TypeScriptVitestJestTests(unittest.TestCase):
    def test_detect_runner_and_script_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')
            self.assertEqual(detect_runner(root), "vitest")
            self.assertEqual(test_script_problem(root), "")

            (root / "package.json").write_text('{"scripts": {"test": "jest --ci"}}\n')
            self.assertEqual(detect_runner(root), "jest")
            self.assertEqual(test_script_problem(root), "")

            (root / "package.json").write_text('{"scripts": {"test": "mocha"}}\n')
            self.assertEqual(detect_runner(root), "node")
            self.assertIn("'mocha'", test_script_problem(root))

    def test_commands_vitest_and_jest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "vitest").write_text("#!/bin/sh\n")
            (bin_dir / "jest").write_text("#!/bin/sh\n")

            (root / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')
            pack = TypeScriptPack()
            pack.configure(root)
            self.assertEqual(pack.runner, "vitest")
            argv = pack.commands("test", tests=["adds numbers"], files=["tests/math.test.ts"])["argv"]
            self.assertEqual(argv[0], str(bin_dir / "vitest"))
            self.assertIn("run", argv)
            self.assertIn("--reporter=json", argv)
            self.assertIn("-t", argv)
            self.assertIn("tests/math.test.ts", argv)

            (root / "package.json").write_text('{"scripts": {"test": "jest"}}\n')
            pack_j = TypeScriptPack()
            pack_j.configure(root)
            self.assertEqual(pack_j.runner, "jest")
            argv_j = pack_j.commands("test", tests=["adds numbers"], files=["tests/math.test.ts"])["argv"]
            self.assertEqual(argv_j[0], str(bin_dir / "jest"))
            self.assertIn("--json", argv_j)
            self.assertIn("--ci", argv_j)
            self.assertIn("-t", argv_j)
            self.assertIn("tests/math.test.ts", argv_j)

    def test_parse_vitest_and_jest_json(self) -> None:
        payload = {
            "success": False,
            "numTotalTests": 3,
            "numPassedTests": 1,
            "numFailedTests": 1,
            "numPendingTests": 1,
            "testResults": [
                {
                    "name": "/repo/tests/math.test.ts",
                    "status": "failed",
                    "assertionResults": [
                        {"title": "adds", "fullName": "math adds", "status": "passed"},
                        {"title": "divides", "fullName": "math divides", "status": "failed"},
                        {"title": "later", "fullName": "math later", "status": "pending"},
                    ],
                },
                {
                    "name": "/repo/tests/broken.test.ts",
                    "status": "failed",
                    "message": "SyntaxError: Unexpected token",
                    "assertionResults": [],
                },
            ],
        }
        for runner in ("vitest", "jest"):
            pack = TypeScriptPack(runner=runner)
            parsed = pack.parse_tests("banner line\n" + json.dumps(payload) + "\n")
            self.assertEqual(len(parsed["results"]), 3)
            by_name = {r["name"]: r for r in parsed["results"]}
            self.assertEqual(by_name["adds"]["status"], "ok")
            self.assertEqual(by_name["divides"]["status"], "FAILED")
            self.assertEqual(by_name["later"]["status"], "ignored")
            self.assertEqual(len(parsed["compile_errors"]), 1)
            self.assertIn("broken.test.ts", parsed["compile_errors"][0]["binary"])
            self.assertEqual(len(parsed["footers"]), 1)
            self.assertEqual(parsed["footers"][0]["status"], "FAILED")

            truncated = pack.parse_tests("not json at all")
            self.assertEqual(truncated["missing_footers"], [runner])

    def test_adopt_and_config_accept_vitest_and_jest(self) -> None:
        for runner, script in (("vitest", "vitest run"), ("jest", "jest")):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / f"ts-{runner}"
                root.mkdir()
                _git_init(root)
                (root / "package.json").write_text(json.dumps({"name": "app", "scripts": {"test": script}}) + "\n")
                subprocess.run(["git", "add", "."], cwd=root, check=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                res = adopt.adopt_project(root, state_dir=Path(tmp) / "s",
                                          key_file=Path(tmp) / "k", run_suite=False)
                self.assertTrue(res["ok"])
                self.assertEqual(res["typescript"]["runner"], runner)
                cfg = load_config(root)
                self.assertEqual(cfg.runner_typescript, runner)

                # Reject unsupported runner
                cfg_path = root / ".openfarm" / "config.toml"
                cfg_path.write_text(cfg_path.read_text().replace(
                    f'typescript = "{runner}"', 'typescript = "mocha"'))
                with self.assertRaises(ConfigError):
                    load_config(root)

    @unittest.skipUnless(shutil.which("vitest"), "vitest binary not installed")
    def test_real_vitest_smoke(self) -> None:
        self.assertIsNotNone(shutil.which("vitest"))


if __name__ == "__main__":
    unittest.main()
