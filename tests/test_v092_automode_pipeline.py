"""v0.9.2 real in-TUI Automode pipeline engine tests."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from openfarm_core.automode import (
    compute_pipeline_stages,
    list_existing_key_files,
    reuse_existing_key_file,
    run_grill_and_architect,
    save_worker_key_atomic,
)
from openfarm_core.errors import Refused
from tests.test_python_e2e import build_python_toy


class _FakeArchitect:
    def ask(self, prompt: str) -> str:
        return json.dumps({
            "files": {
                ".openfarm/PROJECT_SPEC.md": "# Spec\n\nDetailed specification.\n\n## Invariants\n- Never lose data.\n\n## Slice\n- Golden slice.\n",
                "cards/CARD-001.md": "---\nid: CARD-001\ntitle: First slice\nstate: ready\n---\n\nDone: Works.\n",
                # Must be ignored (protected config & path traversal)
                ".openfarm/config.toml": "hacked = true\n",
                "../escape.txt": "nope\n",
            }
        })


class AutomodePipelineTests(unittest.TestCase):
    def test_pipeline_stages_empty_and_adopted_progression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            p0 = compute_pipeline_stages(empty)
            self.assertEqual(p0["current_stage"]["id"], "workspace")
            self.assertEqual(p0["current_stage"]["action"], "init")

        toy = build_python_toy()
        self.addCleanup(toy.cleanup)
        # With scripted model + off micromanager, auth & worker_key are done -> spec_and_slice is current
        p1 = compute_pipeline_stages(toy.root, acp_status={"oauth_ready": True})
        self.assertEqual(p1["current_stage"]["id"], "spec_and_slice")

        # After Grill & Architect writes spec + CARD-001, wave stage becomes current
        res = run_grill_and_architect(
            toy.root,
            answers=[{"id": "goal", "question": "Goal", "answer": "Build photos of the day"}],
            backend=_FakeArchitect(),
        )
        self.assertIn(".openfarm/PROJECT_SPEC.md", res["written"])
        self.assertIn("cards/CARD-001.md", res["written"])
        self.assertNotIn(".openfarm/config.toml", res["written"])
        self.assertFalse((toy.root.parent / "escape.txt").exists())

        p2 = compute_pipeline_stages(toy.root, acp_status={"oauth_ready": True})
        self.assertEqual(p2["current_stage"]["id"], "wave")

    def test_save_and_reuse_worker_key_atomic_0600_and_symlink_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            k1 = root / "keys" / "proj-a.key"
            save_worker_key_atomic(k1, "sk-secret-value-123")
            self.assertEqual(stat.S_IMODE(k1.stat().st_mode), 0o600)
            self.assertEqual(k1.read_text(), "sk-secret-value-123\n")

            found = list_existing_key_files(root / "keys")
            self.assertEqual(found, [k1])

            k2 = root / "keys" / "proj-b.key"
            reuse_existing_key_file(k1, k2)
            self.assertEqual(stat.S_IMODE(k2.stat().st_mode), 0o600)
            self.assertEqual(k2.read_text(), "sk-secret-value-123\n")

            victim = root / "victim.txt"
            victim.write_text("safe\n")
            sym = root / "keys" / "evil.key"
            os.symlink(victim, sym)
            with self.assertRaises(Refused):
                save_worker_key_atomic(sym, "sk-overwrite")
            self.assertEqual(victim.read_text(), "safe\n")


if __name__ == "__main__":
    unittest.main()
