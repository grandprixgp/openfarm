"""J3: an Antigravity prompt too large for one argv word is refused before the spawn."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openfarm_core import confine as confine_lib
from openfarm_core import driver as driver_mod
from openfarm_core.adapters.agy_native_acp import MAX_ARG_BYTES
from openfarm_core.config import load_config
from openfarm_core.driver import DriverAgent, drive_run
from openfarm_core.errors import Refused
from openfarm_core.state import StateDir
from tests.test_python_e2e import GOOD_IMPLEMENTATION, GOOD_TEST, build_python_toy

TEST_CONFINE = "auto" if confine_lib.bwrap_status()[0] else "off"

_AGY = """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
print(json.dumps({"response": json.dumps({"actions": [], "answers": []}),
                  "usage": {"promptTokenCount": 2, "candidatesTokenCount": 1}}))
"""


class AgyPromptLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v081c-d2-agy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agy = self.tmp / "bin" / "agy"
        self.agy.parent.mkdir()
        self.agy.write_text(_AGY)
        self.agy.chmod(0o755)
        self.spawned: list[list[str]] = []
        real_popen = driver_mod._POPEN

        def _recording_popen(argv, **kwargs):
            self.spawned.append(list(argv))
            return real_popen(argv, **kwargs)

        patcher = mock.patch.object(driver_mod, "_POPEN", _recording_popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_huge_prompt_is_refused_before_spawn_and_a_small_one_runs(self) -> None:
        with DriverAgent(agent="antigravity", cwd=self.tmp, driver_bin=str(self.agy),
                         env={"HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")},
                         confine=TEST_CONFINE) as drv:
            with self.assertRaises(Refused) as caught:
                drv.complete("x" * 200_000, system_prompt="SYS")
            self.assertEqual(caught.exception.code, "agy-prompt-too-large")
            msg = caught.exception.message
            self.assertIn("the prompt is 200005 bytes", msg)
            self.assertIn("agy takes it as one command-line argument (-p)", msg)
            self.assertIn(f"Linux caps one argument at {MAX_ARG_BYTES} bytes", msg)
            self.assertEqual(self.spawned, [], "agy must not be started")
            resp = drv.complete("small", system_prompt="SYS")
            self.assertEqual(len(self.spawned), 1)
            self.assertIn("actions", resp.text)

    def test_the_limit_is_the_bridges(self) -> None:
        # 131071 bytes plus the NUL fit; one more does not.
        self.assertEqual(driver_mod.agy_prompt_too_large("a" * (MAX_ARG_BYTES - 1)), "")
        self.assertTrue(driver_mod.agy_prompt_too_large("a" * MAX_ARG_BYTES))


class DriveFollowsTheTimeoutRuleTests(unittest.TestCase):
    """A refused turn is a failed turn: its work goes the `on_external_timeout` way."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix", unit=".",
            touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push", "file": "tests/test_full.py",
                    "asserts": "push raises FullError"}],
            done_text="push on a full bundle raises `FullError`.",
        )
        self.toy.commit("add CARD-1")
        cfg = self.toy.root / ".openfarm" / "config.toml"
        cfg.write_text(cfg.read_text() + f'\n[micromanager]\nconfine = "{TEST_CONFINE}"\n')
        self.toy.commit("confine")
        self.log = Path(self.toy.base) / "agy.log"
        self.agy = Path(self.toy.base) / "agy"
        self.agy.write_text(
            "#!/bin/sh\necho called >> " + str(self.log) + "\n"
            + 'echo \'{"response": "{}"}\'\n')
        self.agy.chmod(0o755)
        self.worker = Path(self.toy.base) / "worker.json"
        self.worker.write_text(json.dumps({
            "CARD-1/test_writer": "=== whole_file: tests/test_full.py\n" + GOOD_TEST,
            "CARD-1/implementer": GOOD_IMPLEMENTATION,
            "CARD-1/checker_pass1": "RESTATEMENT: full bundle refuses push with FullError",
            "CARD-1/checker_pass2": "{}",
        }))

    def test_every_too_large_turn_fails_and_falls_back(self) -> None:
        # A small limit stands in for a large card: every driver prompt is over it.
        with mock.patch.object(driver_mod, "AGY_MAX_ARG_BYTES", 600):
            res = drive_run(self.toy.root, load_config(self.toy.root),
                            StateDir(self.toy.state_dir), agent="antigravity", seat="both",
                            card_ids=["CARD-1"], driver_bin=str(self.agy),
                            script=self.worker)
        self.assertFalse(self.log.exists(), "agy must never be started")
        self.assertTrue(res["driver_errors"])
        self.assertEqual({e["code"] for e in res["driver_errors"]}, {"agy-prompt-too-large"})
        self.assertIn("agy takes it as one command-line argument", res["driver_errors"][0]["message"])
        self.assertTrue(res["timeouts"])
        self.assertTrue(all(t["policy"] == "fallback" for t in res["timeouts"]))
        self.assertTrue(any("agy-prompt" in t["reason"] or "one command-line argument" in t["reason"]
                            for t in res["timeouts"]), res["timeouts"])
        self.assertEqual(res["answers_sent"], [])
        self.assertEqual(res["outcomes"][0]["final_state"], "in-review")


if __name__ == "__main__":
    unittest.main()
