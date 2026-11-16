from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GRILL_SKILL_PATHS = [
    ROOT / "openfarm_core/skill_assets/skills/openfarm-grill-me/SKILL.md",
    ROOT / "skills/openfarm-grill-me/SKILL.md",
]

INITIALIZE_SKILL_PATHS = [
    ROOT / "openfarm_core/skill_assets/skills/openfarm-initialize/SKILL.md",
    ROOT / "skills/openfarm-initialize/SKILL.md",
]


def read_skill(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def batch_section(text: str) -> str:
    marker = "## Batch Mode"
    start = text.find(marker)
    if start == -1:
        return ""
    next_heading = text.find("\n## ", start + len(marker))
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


class BatchGrillSkillTests(unittest.TestCase):
    def test_both_grill_copies_have_the_batch_section(self) -> None:
        for path in GRILL_SKILL_PATHS:
            with self.subTest(path=path):
                text = read_skill(path)
                self.assertIn("## Batch Mode (`--batch`)", text.splitlines())
                self.assertIn("needs you", text)
                self.assertIn("Confirmed", text)

    def test_the_batch_section_is_the_same_in_both_copies(self) -> None:
        sections = [batch_section(read_skill(path)) for path in GRILL_SKILL_PATHS]
        self.assertNotEqual(sections[0], "")
        self.assertNotEqual(sections[1], "")
        self.assertEqual(sections[0], sections[1])

    def test_initialize_offers_batch_grill(self) -> None:
        for path in INITIALIZE_SKILL_PATHS:
            with self.subTest(path=path):
                self.assertIn("--batch-grill", read_skill(path))