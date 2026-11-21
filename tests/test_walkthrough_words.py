from __future__ import annotations

import re
import unittest
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "FROM-IDEA-TO-SHIPPED-APP.md"
OLDER = r"\b(cards?|waves?|frontier|workers?|lesser models?|micromanager|supervis\w+|dossiers?|verdict)\b"


class WalkthroughWords(unittest.TestCase):
    def test_part_2_says_task_goal_architect_and_builders(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        part = text[text.index("## Part 2"):text.index("## Part 3")]
        prose, fence = [], False
        for line in part.split("\n"):
            if line.startswith("```"):
                fence = not fence
            elif not fence:
                # commands, file names and settings keep their real names: they are in code spans and link targets
                prose.append(re.sub(r"`[^`]*`|\]\([^)]*\)", "", line))
        found = sorted({m.group(0).lower() for m in re.finditer(OLDER, "\n".join(prose), re.I)})
        self.assertEqual(found, [])
        for word in ("task", "goal", "Architect", "builders", "accept"):
            self.assertIn(word, "\n".join(prose))


if __name__ == "__main__":
    unittest.main()
