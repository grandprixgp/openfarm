"""v0.8.8: single-pass checker mode (`[gates] checker_mode = "single-pass"`)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openfarm_core.config import ConfigError, load_config


class SinglePassCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v088-chk-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def _write_cfg(self, gates_extra: str = "", model_extra: str = "") -> Path:
        of = self.repo / ".openfarm"
        of.mkdir(parents=True, exist_ok=True)
        state = self.tmp / "state"
        key = self.tmp / "key.txt"
        key.write_text("k\n")
        (of / "config.toml").write_text(
            f"""
[project]
name = "demo"
state_dir = "{state}"

[adapters]
model = "scripted"
micromanager = "off"

[model]
key_file = "{key}"
{model_extra}

[policy]
categories = ["pinned-bugfix"]

[gates]
{gates_extra}
""".strip() + "\n"
        )
        return self.repo

    def test_config_default_and_validation(self) -> None:
        cfg = load_config(self._write_cfg())
        self.assertEqual(cfg.checker_mode, "two-pass")

        cfg2 = load_config(self._write_cfg(
            'checker_mode = "single-pass"\n',
            'checker = { model = "m", effort = "medium" }\n',
        ))
        self.assertEqual(cfg2.checker_mode, "single-pass")
        self.assertEqual(cfg2.role_models["checker"].effort, "medium")

        with self.assertRaises(ConfigError):
            load_config(self._write_cfg('checker_mode = "zero-pass"\n'))

    def test_single_pass_checker_skips_pass1_and_runs_pass2(self) -> None:
        from tests.test_python_e2e import (
            GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy, run_cli, script,
        )

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        cfg_p = toy.root / ".openfarm" / "config.toml"
        cfg_p.write_text(cfg_p.read_text() + '\nchecker_mode = "single-pass"\n')
        # Put checker_mode inside [gates]
        text = (toy.root / ".openfarm" / "config.toml").read_text()
        text = text.replace('\nchecker_mode = "single-pass"\n', '')
        text = text.replace('[gates]\n', '[gates]\nchecker_mode = "single-pass"\n', 1)
        cfg_p.write_text(text)

        toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.",
        )
        toy.commit("add CARD-1 with single-pass checker")
        # Notice: NO CARD-1/checker_pass1 key in script — if pass1 were called, it would fail!
        sp = script(**{
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass2": "VERDICT: accept\nREASONS: matches Done\nTRACEABILITY:\n- Done -> test\nEDGE_CASES: none\n",
        })
        result = run_cli(toy.root, "run", "--card", "CARD-1", "--script", str(sp), "--cards", "1")
        outcome = json.loads(result.stdout)["outcomes"][0]
        self.assertEqual(outcome["final_state"], "in-review", outcome.get("detail"))
        run_dir = next((toy.state_dir / "cards" / "CARD-1").iterdir())
        gate1 = json.loads(next(run_dir.glob("gates/*/checker-pass1.json")).read_text())
        self.assertIn("single-pass", gate1["detail"])


if __name__ == "__main__":
    unittest.main()
