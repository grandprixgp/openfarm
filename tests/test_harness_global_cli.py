from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from openfarm_core.config_template import render_config
from openfarm_core.harness_global import GlobalLibrary


class HarnessGlobalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.global_dir = self.tmp / "global"
        self.key_file = self.tmp / "k"
        self.key_file.write_text("", encoding="utf-8")

        project_harness = self.state_dir / "harness"
        project_harness.mkdir(parents=True)
        self.project_marker = project_harness / "keep.txt"
        self.project_marker.write_text("project harness", encoding="utf-8")

        config_dir = self.repo / ".openfarm"
        config_dir.mkdir()
        config = render_config(
            name="demo",
            language="python",
            base_branch="main",
            state_dir=self.state_dir,
            key_file=self.key_file,
        )
        config = config.rstrip() + "\n" + f'global_dir = "{self.global_dir}"\n'
        (config_dir / "config.toml").write_text(config, encoding="utf-8")

    def _lesson(self) -> dict:
        return {
            "title": "Prefer pathlib",
            "surface": "cli",
            "role": "worker",
            "language": "python",
            "kind": "style",
            "failure_class": "readability",
            "tags": ["paths"],
            "cue": "when touching file paths",
            "lesson": "Use pathlib.Path instead of os.path strings.",
            "proposed_patch": "Replace os.path usage with Path usage.",
            "confidence": 0.9,
        }

    def _run_cli(self, *args: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--repo", str(self.repo), "--json", *args])
        text = stdout.getvalue()
        payload = json.loads(text) if text.strip() else {}
        return code, payload, text, stderr.getvalue()

    def test_global_status_lists_the_library(self) -> None:
        added = GlobalLibrary(self.global_dir).add(self._lesson(), "alpha")
        self.assertEqual(added["sources"], ["alpha"])

        code, payload, _stdout, _stderr = self._run_cli(
            "harness", "status", "--global"
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["active"], 1)
        self.assertEqual(payload["dir"], str(self.global_dir))
        self.assertEqual(payload["entries"][0]["sources"], ["alpha"])

    def test_global_demote_retires_and_promote_restores(self) -> None:
        added = GlobalLibrary(self.global_dir).add(self._lesson(), "alpha")
        gid = added["id"]

        code, payload, _stdout, _stderr = self._run_cli(
            "harness", "demote", gid, "--global", "--reason", "noisy"
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["status"], "retired")
        self.assertEqual(payload["retire_reason"], "noisy")
        self.assertEqual(GlobalLibrary(self.global_dir).get(gid)["status"], "retired")

        code, payload, _stdout, _stderr = self._run_cli(
            "harness", "promote", gid, "--global"
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["status"], "active")
        self.assertEqual(GlobalLibrary(self.global_dir).get(gid)["status"], "active")

    def test_an_unknown_global_id_is_refused(self) -> None:
        code, payload, _stdout, _stderr = self._run_cli(
            "harness", "demote", "g-0000000000", "--global"
        )

        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(payload["kind"], "refused")
        self.assertEqual(payload["code"], "unknown-global-entry")

    def test_other_actions_do_not_take_global(self) -> None:
        code, payload, _stdout, _stderr = self._run_cli(
            "harness", "reset", "--yes", "--global"
        )

        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(payload["error"], "harness reset does not take --global")
        self.assertEqual(self.project_marker.read_text(encoding="utf-8"),
                         "project harness")
        self.assertTrue((self.state_dir / "harness").is_dir())
        self.assertFalse(self.global_dir.exists())