"""openfarm on itself: it never runs from code the card could edit."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core import __version__
from openfarm_core.selfhost import describe, orchestrator_identity, unpinned_reason
from tests.test_python_e2e import build_python_toy

ROOT = Path(__file__).resolve().parent.parent


class UnpinnedReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="openfarm-selfhost-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.repo = self.base / "repo"
        self.state = self.base / "state"
        self.repo.mkdir()
        self.state.mkdir()

    def test_core_inside_the_repo_is_refused(self) -> None:
        reason = unpinned_reason(self.repo, self.state, core_root=self.repo)
        self.assertIn("inside the repository it would edit", reason)
        reason = unpinned_reason(self.repo, self.state,
                                 core_root=self.repo / "vendor" / "openfarm")
        self.assertIn("inside the repository it would edit", reason)

    def test_core_inside_the_state_folder_is_refused(self) -> None:
        reason = unpinned_reason(self.repo, self.state,
                                 core_root=self.state / "worktrees" / "CARD-1")
        self.assertIn("state folder", reason)

    def test_a_pinned_copy_elsewhere_may_run(self) -> None:
        self.assertIsNone(unpinned_reason(self.repo, self.state,
                                          core_root=self.base / "runner"))

    def test_identity_names_version_and_commit(self) -> None:
        identity = orchestrator_identity(ROOT)
        self.assertEqual(identity["version"], __version__)
        self.assertEqual(identity["path"], str(ROOT))
        self.assertIn(f"openfarm {__version__} at {ROOT}", describe(identity))
        if identity["commit"]:
            self.assertIn(identity["commit"][:12], describe(identity))


class CliGuardTests(unittest.TestCase):
    """A real CLI run with openfarm's code copied inside the repo it is given."""

    def setUp(self) -> None:
        self.toy = build_python_toy()
        self.addCleanup(self.toy.cleanup)
        shutil.copytree(ROOT / "openfarm_core", self.toy.root / "openfarm_core",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "openfarm", self.toy.root / "openfarm")
        self.toy.write_card(
            "CARD-1", title="A full bundle refuses a push", category="pinned-bugfix",
            unit=".", touches=["tally/bounded.py"],
            tests=[{"name": "test_a_full_bundle_refuses_a_push",
                    "file": "tests/test_full.py"}],
            done_text="`Bounded(1).push(8)` after one push raises `FullError`.")
        self.toy.commit("tally: vendored openfarm and CARD-1")

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.toy.root / "openfarm"), "--repo",
             str(self.toy.root), "--json", *args], capture_output=True, text=True)

    def test_a_real_run_is_refused(self) -> None:
        result = self._cli("run", "--cards", "1")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["refused"], result.stdout)
        self.assertEqual(payload["code"], "self-host-unpinned")
        self.assertFalse(list((self.toy.state_dir).glob("cards/*")))

    def test_a_dry_run_is_allowed(self) -> None:
        result = self._cli("run", "--dry-run", "--cards", "1")
        payload = json.loads(result.stdout)
        self.assertNotEqual(payload.get("code"), "self-host-unpinned", result.stdout)


if __name__ == "__main__":
    unittest.main()
