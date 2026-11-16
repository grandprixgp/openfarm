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

    def test_a_closing_marker_the_format_lacks_is_dropped(self) -> None:
        text = ("=== search_replace: a.py\n<<< SEARCH\nx = 1\n>>> REPLACE\nx = 2\n>>>\n"
                "=== search_replace: b.py\n<<< SEARCH\ny = 1\n>>> REPLACE\ny = 2\n>>> END\n")
        self.assertEqual([e.replace for e in parse_edits(text)], ["x = 2", "y = 2"])

    def test_leaked_tool_call_tags_are_dropped(self) -> None:
        text = ("=== whole_file: tests/test_a.py\nimport unittest\n"
                "</\uff5c\uff5cDSML\uff5c\uff5c parameter>\n</\uff5c\uff5cDSML\uff5c\uff5c invoke>\n"
                "</\uff5c\uff5cDSML\uff5c\uff5c calls>")
        self.assertEqual(parse_edits(text)[0].content, "import unittest\n")

    def test_real_code_at_the_end_is_kept(self) -> None:
        text = "=== whole_file: a.py\nx = a >> 3\nprint('>>>')\n"
        self.assertEqual(parse_edits(text)[0].content, "x = a >> 3\nprint('>>>')\n")

    def test_a_diff_is_refused_with_a_pointer_to_the_format(self) -> None:
        text = "```diff\n--- /dev/null\n+++ b/tests/test_a.py\n@@ -0,0 +1 @@\n+x = 1\n```\n"
        with self.assertRaises(Invalid) as caught:
            parse_edits(text)
        self.assertIn("looks like a diff", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
