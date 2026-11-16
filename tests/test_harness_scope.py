from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.harness_scope import project_markers, scope_of


class HarnessScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_markers_hold_name_paths_file_names_and_long_stems(self) -> None:
        markers = project_markers(
            self.root,
            "Widget",
            ["src/pkg/harness_memory.py", "cli.py"],
        )
        self.assertIn("widget", markers)
        self.assertIn("src/pkg/harness_memory.py", markers)
        self.assertIn("harness_memory.py", markers)
        self.assertIn("harness_memory", markers)
        self.assertIn("cli.py", markers)
        self.assertNotIn("cli", markers)

    def test_markers_hold_distinctive_top_level_names(self) -> None:
        (self.root / "app.py").write_text(
            "def load_settings():\n"
            "    pass\n"
            "class Run:\n"
            "    pass\n"
            "def go():\n"
            "    pass\n"
            "class SessionStore:\n"
            "    pass\n",
            encoding="utf-8",
        )
        markers = project_markers(self.root, "x", ["app.py"])
        self.assertIn("load_settings", markers)
        self.assertIn("sessionstore", markers)
        self.assertNotIn("run", markers)
        self.assertNotIn("go", markers)

    def test_a_missing_file_is_skipped(self) -> None:
        self.assertEqual(
            project_markers(self.root, "x", ["gone.py"]),
            {"x", "gone.py"},
        )
        self.assertEqual(
            project_markers(self.root, "x", ["pkg/gone_module.py"]),
            {"x", "pkg/gone_module.py", "gone_module.py", "gone_module"},
        )

    def test_a_lesson_naming_a_marker_is_project(self) -> None:
        self.assertEqual(
            scope_of({"cue": "Call load_settings() before the run."}, {"load_settings"}),
            ("project", "names 'load_settings'"),
        )

    def test_a_marker_inside_a_longer_word_does_not_count(self) -> None:
        self.assertEqual(
            scope_of(
                {"cue": "Use reloaded values.", "scope_reason": "any project"},
                {"load"},
            ),
            ("general", "any project"),
        )

    def test_card_ids_and_file_hints_are_project(self) -> None:
        self.assertEqual(
            scope_of({"lesson": "As in CARD-12, check twice."}, set()),
            ("project", "names a card"),
        )
        self.assertEqual(
            scope_of(
                {"cue": "x", "proposed_patch": {"context_files": ["a.py"]}},
                set(),
            ),
            ("project", "names files"),
        )

    def test_a_clean_lesson_is_general(self) -> None:
        self.assertEqual(
            scope_of({"title": "t", "cue": "Check empty lists first."}, {"widget"}),
            ("general", "names nothing of this project"),
        )


if __name__ == "__main__":
    unittest.main()