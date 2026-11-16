"""`openfarm bench run`: arm files, the shipped tasks, and the zero-cost dry run.

The Python tasks dry-run by default (a second each). The Rust tasks build with
cargo, so they dry-run only under `OPENFARM_E2E=1`, like the other cargo cases.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bench import (_hidden_target, _suite_cmd, arm_config_text,
                                 card_id_of, load_arm, load_task, run_once)
from openfarm_core.cli import main
from openfarm_core.config import load_config
from openfarm_core.errors import Invalid

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
E2E = os.environ.get("OPENFARM_E2E") == "1"


def _tasks():
    return [load_task(d) for d in sorted(BENCH.iterdir())
            if (d / "task.toml").is_file()]


class ArmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _base(self, repo: Path) -> str:
        from openfarm_core.adopt import adopt_config_text
        return adopt_config_text(repo, "python", base_branch="main",
                                 state_dir=self.tmp / "state",
                                 key_file=self.tmp / "no.key")

    def _load(self, text: str):
        repo = self.tmp / "repo"
        (repo / ".openfarm").mkdir(parents=True, exist_ok=True)
        (repo / ".openfarm" / "config.toml").write_text(text)
        return load_config(repo)

    def test_the_arm_replaces_model_and_micromanager_only(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        arm = ('[adapters]\nmodel = "claude-acp"\nmicromanager = "off"\n\n'
               '[model]\nmax_tokens = 1000\n'
               'implementer = { model = "opus", temperature = 0.3 }\n')
        merged = tomllib.loads(arm_config_text(self._base(repo), arm))
        self.assertEqual(merged["adapters"]["model"], "claude-acp")
        self.assertEqual(merged["adapters"]["micromanager"], "off")
        self.assertEqual(merged["adapters"]["languages"], ["python"])
        self.assertEqual(merged["model"], {"max_tokens": 1000, "implementer": {
            "model": "opus", "temperature": 0.3}})
        # The arm named no [micromanager], so the base's stays.
        self.assertEqual(merged["micromanager"]["binary"], "auto")
        self.assertEqual(merged["policy"]["known_failing"], [])

    def test_both_example_arms_make_a_valid_config(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        for name, adapter in (("farm", "openai-compatible"), ("solo", "claude-acp")):
            with self.subTest(arm=name):
                text = arm_config_text(self._base(repo),
                                       load_arm(BENCH / "arms" / f"{name}.example.toml"))
                cfg = self._load(text)
                self.assertEqual(cfg.model_adapter, adapter)

    def test_an_arm_may_not_set_other_tables(self) -> None:
        path = self.tmp / "arm.toml"
        path.write_text('[policy]\nnever_touch = []\n')
        with self.assertRaises(Invalid) as caught:
            load_arm(path)
        self.assertIn("policy", caught.exception.message)
        path.write_text('[adapters]\nrunner = "direct"\n')
        with self.assertRaises(Invalid):
            load_arm(path)

    def test_card_ids_are_read_from_the_front_matter(self) -> None:
        self.assertEqual(card_id_of('+++\nid = "CARD-7"\n+++\n'), "CARD-7")
        with self.assertRaises(Invalid):
            card_id_of("no front matter")


class BenchCliTests(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--json", "bench", "run", "--dir", str(BENCH), *args])
        return code, json.loads(buffer.getvalue())

    def test_a_live_run_needs_an_arm(self) -> None:
        code, payload = self._run()
        self.assertNotEqual(code, 0)
        self.assertIn("--arm farm=FILE", payload["error"])

    def test_an_unknown_arm_name_is_refused(self) -> None:
        code, payload = self._run("--arm", "mixed=x.toml")
        self.assertNotEqual(code, 0)
        self.assertIn("farm=FILE or solo=FILE", payload["error"])

    def test_an_unknown_task_is_refused(self) -> None:
        code, payload = self._run("--dry-run", "--task", "nope")
        self.assertNotEqual(code, 0)
        self.assertIn("nope", payload["error"])


class ShippedTaskTests(unittest.TestCase):
    """The 12 tasks the spec promises: 6 Rust, 6 Python, all four categories."""

    def test_the_task_set(self) -> None:
        tasks = _tasks()
        self.assertEqual(len(tasks), 12)
        for language in ("rust", "python"):
            mine = [t for t in tasks if t.language == language]
            self.assertEqual(len(mine), 6, language)
            self.assertEqual({t.category for t in mine},
                             {"small-feature", "pinned-bugfix", "pattern", "refactor"})
        for task in tasks:
            with self.subTest(task=task.id):
                self.assertIsNotNone(task.repo)
                self.assertIsNotNone(task.script)
                for hidden in task.hidden_tests:
                    self.assertEqual(hidden.relative_to(task.root).parts[0], "hidden")


class DryRunTests(unittest.TestCase):
    """Each task's scripted answers pass its hidden tests; its untouched base fails them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-bench-",
                                         dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _check(self, language: str) -> None:
        for task in [t for t in _tasks() if t.language == language]:
            with self.subTest(task=task.id):
                record = run_once(task, "farm", None, 1, self.tmp / "runs",
                                  dry_run=True)
                self.assertTrue(record["passed"],
                                f"{record['final_state']} {record['stop_code']}: "
                                f"{record['run_dir']}")
                base = self.tmp / "base" / task.id
                shutil.copytree(task.repo, base)
                for hidden in task.hidden_tests:
                    target = base / _hidden_target(task, hidden)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(hidden, target)
                done = subprocess.run(_suite_cmd(language), cwd=base,
                                      capture_output=True, text=True, check=False)
                self.assertNotEqual(done.returncode, 0,
                                    "the hidden tests pass on the untouched base")
                shutil.rmtree(self.tmp / "runs" / task.id, True)
                shutil.rmtree(base, True)

    def test_python_tasks(self) -> None:
        self._check("python")

    @unittest.skipUnless(E2E, "cargo builds; set OPENFARM_E2E=1")
    def test_rust_tasks(self) -> None:
        self._check("rust")


if __name__ == "__main__":
    unittest.main()
