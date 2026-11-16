"""Workers are told the edit format openfarm reads, and fenced blocks still parse."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import EDIT_FORMAT, STUB_FORMAT
from openfarm_core.edits import parse_edits
from openfarm_core.errors import Invalid


class EditFormatTests(unittest.TestCase):
    def test_the_documented_format_parses(self) -> None:
        edits = parse_edits(EDIT_FORMAT)
        self.assertEqual([(e.format, e.path) for e in edits],
                         [("whole_file", "tests/test_example.py"),
                          ("search_replace", "path/to/module.py")])
        stubs = parse_edits(STUB_FORMAT)
        self.assertEqual((stubs[0].format, stubs[0].path), ("append_stub", "path/to/module.py"))

    def test_a_fenced_whole_file_loses_its_fence(self) -> None:
        text = "=== whole_file: tests/test_a.py\n```python\nimport unittest\n```\n"
        self.assertEqual(parse_edits(text)[0].content, "import unittest\n")

    def test_an_outer_fence_around_all_blocks_is_dropped(self) -> None:
        text = ("```\n=== search_replace: a.py\n<<< SEARCH\nx = 1\n>>> REPLACE\nx = 2\n```\n")
        edit = parse_edits(text)[0]
        self.assertEqual((edit.search, edit.replace), ("x = 1", "x = 2"))

    def test_a_diff_is_refused_with_a_pointer_to_the_format(self) -> None:
        text = "```diff\n--- /dev/null\n+++ b/tests/test_a.py\n@@ -0,0 +1 @@\n+x = 1\n```\n"
        with self.assertRaises(Invalid) as caught:
            parse_edits(text)
        self.assertIn("looks like a diff", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
