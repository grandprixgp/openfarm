from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestReadmeGlobalLearning(unittest.TestCase):
    def test_the_readme_explains_global_learning(self) -> None:
        readme_path = Path(__file__).resolve().parent.parent / "README.md"
        lines = readme_path.read_text(encoding="utf-8").splitlines()

        global_heading = "## Global learning"
        efficacy_heading = "## Efficacy bench"

        self.assertIn(global_heading, lines)
        self.assertIn(efficacy_heading, lines)

        global_index = lines.index(global_heading)
        efficacy_index = lines.index(efficacy_heading)

        self.assertLess(global_index, efficacy_index)

        section = "\n".join(lines[global_index + 1 : efficacy_index])

        self.assertIn("learn_from", section)
        self.assertIn("share", section)
        self.assertIn("global_dir", section)
        self.assertIn("~/.openfarm/global/harness", section)
        self.assertIn("--learn-from", section)
        self.assertIn("harness status --global", section)
        self.assertIn("untested", section)
