"""v0.7 decision 3: the owner forces a project's own lesson global, or takes it back.

Forcing skips the proof (3 helps) and the lesson's scope claim, never the safety
checks: a lesson naming the project, or failing eligibility, is still refused.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, main
from openfarm_core.config import load_config
from openfarm_core.config_template import render_config
from openfarm_core.harness_global import GlobalLibrary
from openfarm_core.harness_memory import HarnessLibrary

GENERAL = {
    "title": "empty input", "surface": "implementer_guidance", "role": "implementer",
    "language": "python", "kind": "small-feature", "failure_class": "",
    "cue": "Return early on an empty list before indexing its first item.",
    "lesson": "Empty lists reached an index lookup.", "evidence": "", "confidence": 0.8,
    "tags": [], "proposed_patch": {}, "source": "micromanager", "origin": "project",
    "scope": "project", "status": "candidate", "card_id": "CARD-1", "revision": 0,
    "stats": {"applied": 0, "helped": 0, "hurt": 0, "neutral": 0},
}


class ForceGlobalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-force-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / ".openfarm").mkdir(parents=True)
        self.global_dir = self.tmp / "global"
        config = render_config(name="demo", language="python", base_branch="main",
                               state_dir=self.tmp / "state", key_file=self.tmp / "k")
        config = config.rstrip() + "\n" + f'global_dir = "{self.global_dir}"\n'
        (self.repo / ".openfarm" / "config.toml").write_text(config, encoding="utf-8")
        (self.repo / "ledger.py").write_text("def merge_ledger_rows(rows):\n    return rows\n",
                                             encoding="utf-8")
        for argv in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *argv], check=True)
        cfg = load_config(self.repo)
        self.lib = HarnessLibrary(self.tmp / "state" / "harness", config=cfg)
        self.lib._save_entry({**GENERAL, "id": "exp-general"})
        self.lib._save_entry({**GENERAL, "id": "exp-tied",
                              "cue": "Call merge_ledger_rows before writing a row back."})

    def _cli(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--repo", str(self.repo), "--json", *args])
        return code, json.loads(out.getvalue() or "{}")

    def test_an_unproven_lesson_can_be_forced_global(self) -> None:
        code, res = self._cli("harness", "promote", "exp-general", "--global")
        self.assertEqual(code, EXIT_OK, res)
        gid = res["global_id"]
        self.assertEqual(GlobalLibrary(self.global_dir).get(gid)["sources"][0]["project"], "demo")
        self.assertEqual(self.lib.show_entry("exp-general")["global_id"], gid)

    def test_forcing_still_refuses_a_lesson_naming_the_project(self) -> None:
        code, res = self._cli("harness", "promote", "exp-tied", "--global")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("merge_ledger_rows", res["error"])
        self.assertFalse(self.global_dir.exists())

    def test_demote_by_the_project_lesson_id_retires_its_global_copy(self) -> None:
        _, res = self._cli("harness", "promote", "exp-general", "--global")
        code, _ = self._cli("harness", "demote", "exp-general", "--global")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(GlobalLibrary(self.global_dir).get(res["global_id"])["status"], "retired")
        code, _ = self._cli("harness", "demote", "exp-tied", "--global")
        self.assertEqual(code, EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()
