from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.harness_global import retire_reason, should_promote, should_veto


class GlobalRulesTests(unittest.TestCase):
    def test_a_proven_general_lesson_is_promoted(self):
        self.assertEqual(
            should_promote(
                {
                    "scope": "general",
                    "status": "active",
                    "stats": {"helped": 3, "hurt": 2},
                }
            ),
            True,
        )

    def test_unproven_or_project_lessons_are_not_promoted(self):
        self.assertEqual(
            should_promote(
                {
                    "scope": "general",
                    "status": "active",
                    "stats": {"helped": 3, "hurt": 0},
                }
            ),
            True,
        )
        self.assertEqual(
            should_promote(
                {
                    "scope": "general",
                    "status": "active",
                    "stats": {"helped": 2, "hurt": 0},
                }
            ),
            False,
        )
        self.assertEqual(
            should_promote(
                {
                    "scope": "general",
                    "status": "active",
                    "stats": {"helped": 3, "hurt": 3},
                }
            ),
            False,
        )
        self.assertEqual(
            should_promote(
                {
                    "scope": "project",
                    "status": "active",
                    "stats": {"helped": 3, "hurt": 0},
                }
            ),
            False,
        )
        self.assertEqual(
            should_promote(
                {
                    "scope": "general",
                    "status": "probing",
                    "stats": {"helped": 3, "hurt": 0},
                }
            ),
            False,
        )
        self.assertEqual(
            should_promote(
                {
                    "status": "active",
                    "stats": {"helped": 3, "hurt": 0},
                }
            ),
            False,
        )
        self.assertEqual(
            should_promote(
                {
                    "scope": "general",
                    "status": "active",
                }
            ),
            False,
        )

    def test_a_veto_needs_more_hurt_than_help(self):
        self.assertEqual(should_veto({"helped": 1, "hurt": 2}), True)
        self.assertEqual(should_veto({"helped": 2, "hurt": 2}), False)
        self.assertEqual(should_veto({}), False)

    def test_two_vetoes_retire_a_lesson(self):
        self.assertEqual(
            retire_reason(
                {
                    "projects": {
                        "a": {"helped": 5, "hurt": 0, "vetoed": True},
                        "b": {"helped": 5, "hurt": 0, "vetoed": True},
                    }
                }
            ),
            "vetoed by 2 projects",
        )

    def test_a_negative_combined_score_retires_a_lesson(self):
        self.assertEqual(
            retire_reason(
                {
                    "projects": {
                        "a": {"helped": 1, "hurt": 0, "vetoed": False},
                        "b": {"helped": 0, "hurt": 2, "vetoed": True},
                    }
                }
            ),
            "combined score -1",
        )

    def test_a_lesson_in_good_standing_is_kept(self):
        self.assertEqual(
            retire_reason(
                {
                    "projects": {
                        "a": {"helped": 2, "hurt": 1, "vetoed": True},
                        "b": {"helped": 0, "hurt": 0, "vetoed": True},
                    }
                }
            ),
            "vetoed by 2 projects",
        )
        self.assertEqual(
            retire_reason(
                {"projects": {"a": {"helped": 2, "hurt": 1, "vetoed": True}}}
            ),
            "",
        )
        self.assertEqual(retire_reason({}), "")


if __name__ == "__main__":
    unittest.main()