"""v0.8.1 fix wave, unit drive, D2: the skill texts don't sell the driver's look as a verdict."""

from __future__ import annotations

import filecmp
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "openfarm"
ASSET = ROOT / "openfarm_core" / "skill_assets" / "skills" / "openfarm"


def _texts() -> dict[str, str]:
    return {str(p.relative_to(SKILL)): p.read_text(encoding="utf-8") for p in SKILL.rglob("*.md")}


class DriveSkillTextTests(unittest.TestCase):
    def test_wave_record_verdict_is_not_offered_instead_of_a_review(self) -> None:
        texts = _texts()
        skill = texts["SKILL.md"]
        operator = texts["prompts/wave_operator.md"]
        self.assertNotIn("--wave --record-verdict --json", skill)
        self.assertNotIn("or use `openfarm drive --wave --record-verdict`", operator)
        self.assertNotIn("or `--wave --record-verdict` in Driver mode", operator)
        self.assertIn("by: driver/<agent>", skill)
        self.assertIn("not a review", skill)
        self.assertIn("does not replace this review", operator)

    def test_cheat_sheet_names_turn_timeout(self) -> None:
        self.assertIn("--turn-timeout SECONDS", _texts()["references/cheat_sheet.md"])

    def test_both_copies_are_byte_identical(self) -> None:
        cmp = filecmp.dircmp(SKILL, ASSET)
        stack = [cmp]
        while stack:
            d = stack.pop()
            self.assertEqual((d.left_only, d.right_only, d.diff_files, d.funny_files), ([], [], [], []), d.left)
            _, mismatch, errors = filecmp.cmpfiles(d.left, d.right, d.common_files, shallow=False)
            self.assertEqual((mismatch, errors), ([], []), d.left)
            stack.extend(d.subdirs.values())


if __name__ == "__main__":
    unittest.main()
