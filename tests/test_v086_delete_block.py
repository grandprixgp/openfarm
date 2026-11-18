"""Pilot card M2, 18 Nov 2026: a block that deletes code was refused as malformed.

Moving two functions out of a file, the implementer ended its answer with a
block whose replacement is empty, so its last line was the `>>> REPLACE`
separator itself. The trailing-junk trimmer took that line for a stray marker
and removed it, and the block was refused ("needs <<< SEARCH and >>> REPLACE")
on all three attempts.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.edits import parse_edits


class DeleteBlockTests(unittest.TestCase):
    def test_a_block_with_an_empty_replacement_at_the_end(self) -> None:
        edits = parse_edits("=== search_replace: src/a.rs\n<<< SEARCH\nfn gone() {}\n>>> REPLACE\n")
        self.assertEqual((edits[0].search, edits[0].replace), ("fn gone() {}", ""))

    def test_the_same_without_a_final_newline_and_with_others_before(self) -> None:
        edits = parse_edits("=== whole_file: src/b.rs\nfn b() {}\n"
                            "=== search_replace: src/a.rs\n<<< SEARCH\nuse x;\n>>> REPLACE\nuse y;\n"
                            "=== search_replace: src/a.rs\n<<< SEARCH\nfn gone() {}\n>>> REPLACE")
        self.assertEqual([(e.path, e.replace) for e in edits[1:]],
                         [("src/a.rs", "use y;"), ("src/a.rs", "")])

    def test_stray_markers_after_a_replacement_are_still_trimmed(self) -> None:
        for tail in (">>> REPLACE\n", "```\n", ">>> END\n"):
            with self.subTest(tail=tail):
                edits = parse_edits("=== search_replace: a.py\n<<< SEARCH\nx = 1\n>>> REPLACE\n"
                                    "y = 2\n" + tail)
                self.assertEqual(edits[0].replace, "y = 2")


if __name__ == "__main__":
    unittest.main()
