from __future__ import annotations

import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.bench import arm_config_text, load_arm
from openfarm_core.config_template import render_config
from openfarm_core.errors import Invalid


class BenchArmHarnessTests(unittest.TestCase):
    def test_an_arm_may_set_learning_keys(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, directory, True)
        arm = directory / "arm.toml"
        text = '[harness]\nlearn_from = "global"\nshare = false\n'
        arm.write_text(text, encoding="utf-8")

        self.assertEqual(load_arm(arm), text)

    def test_other_harness_keys_are_refused(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, directory, True)
        arm = directory / "arm.toml"
        arm.write_text('[harness]\nenabled = false\n', encoding="utf-8")

        with self.assertRaises(Invalid) as caught:
            load_arm(arm)
        self.assertIn("enabled", str(caught.exception))

    def test_the_arm_replaces_or_adds_harness_lines(self) -> None:
        base = render_config(
            name="t",
            language="python",
            base_branch="main",
            state_dir=Path("/tmp/s"),
            key_file=Path("/tmp/k"),
        )

        merged = arm_config_text(
            base,
            '[harness]\nlearn_from = "project"\nglobal_dir = "/tmp/g"\n',
        )

        harness = tomllib.loads(merged)["harness"]
        self.assertEqual(
            {key: harness[key] for key in ("learn_from", "global_dir", "share", "probe_window")},
            {"learn_from": "project", "global_dir": "/tmp/g", "share": True, "probe_window": 3},
        )

    def test_an_arm_without_harness_leaves_it_alone(self) -> None:
        base = render_config(name="t", language="python", base_branch="main",
                             state_dir=Path("/tmp/s"), key_file=Path("/tmp/k"))
        merged = arm_config_text(base, '[model]\nmax_tokens = 10\n')
        self.assertEqual(tomllib.loads(merged)["harness"], tomllib.loads(base)["harness"])
