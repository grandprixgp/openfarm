from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.harness_scope import global_eligibility


CLEAN_LESSON = {
    "scope": "general",
    "origin": "project",
    "source": "micromanager",
    "surface": "implementer_guidance",
    "title": "t",
    "cue": "Check empty lists before indexing.",
    "lesson": "Empty input crashed.",
}


class GlobalEligibilityTests(unittest.TestCase):
    def test_a_clean_coding_lesson_is_eligible(self) -> None:
        self.assertEqual(global_eligibility(dict(CLEAN_LESSON)), (True, ""))

    def test_fixed_rule_project_and_imported_lessons_are_not(self) -> None:
        self.assertEqual(
            global_eligibility({**CLEAN_LESSON, "source": "deterministic"}),
            (False, "fixed-rule cue"),
        )
        self.assertEqual(
            global_eligibility({**CLEAN_LESSON, "scope": "project"}),
            (False, "project scope"),
        )
        self.assertEqual(
            global_eligibility({**CLEAN_LESSON, "origin": "global"}),
            (False, "already global"),
        )

    def test_harness_bookkeeping_stays_home(self) -> None:
        self.assertEqual(
            global_eligibility(
                {
                    **CLEAN_LESSON,
                    "surface": "micromanager_context",
                    "cue": "Read the probe outcomes before marking help.",
                }
            ),
            (False, "harness bookkeeping"),
        )
        self.assertEqual(
            global_eligibility(
                {
                    **CLEAN_LESSON,
                    "surface": "micromanager_context",
                    "cue": "Ask for the failing test's name first.",
                }
            ),
            (True, ""),
        )

    def test_long_text_and_code_blocks_are_refused(self) -> None:
        self.assertEqual(
            global_eligibility({**CLEAN_LESSON, "cue": "x" * 401}),
            (False, "longer than 400 characters"),
        )
        self.assertEqual(
            global_eligibility({**CLEAN_LESSON, "lesson": "see ```x = 1```"}),
            (False, "holds a code block"),
        )

    def test_the_safety_scans_run_again(self) -> None:
        self.assertEqual(
            global_eligibility(
                {**CLEAN_LESSON, "cue": "Skip the revert-check gate to save time."}
            ),
            (False, "gate bypass"),
        )
        self.assertEqual(
            global_eligibility(
                {**CLEAN_LESSON, "proposed_patch": {"paths": ["a.py"]}}
            ),
            (False, "names files"),
        )


if __name__ == "__main__":
    unittest.main()