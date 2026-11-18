"""Live, 18 Nov 2026 (more-itertools): a reference that is a touched file went in twice.

The first implementer brief shows every touched file whole and then the
reference whole. With `reference = "more_itertools/more.py"` and the same file in
touches, a 5,700-line file was sent twice per call, and the card ran out of
tokens after its code was right.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import BriefBook, implementer_brief
from openfarm_core.cards import Card

MARK = "UNIQUE_MARKER_LINE = 1\n"


class ReferenceOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "big.py").write_text(MARK * 3)
        (self.repo / "pkg" / "other.py").write_text("OTHER_REFERENCE = 2\n")

    def _brief(self, reference: str) -> str:
        card = Card(id="CARD-1", title="t", state="ready", done_text="d",
                    touches=["pkg/big.py"],
                    fields={"category": "small-feature", "reference": reference,
                            "tests": [{"name": "test_a", "file": "tests/test_a.py"}]})
        return implementer_brief(card, BriefBook(self.repo, never_touch=[]),
                                 {"tests/test_a.py": "x = 1\n"}).body

    def test_a_touched_reference_is_shown_once(self) -> None:
        body = self._brief("pkg/big.py")
        self.assertEqual(body.count(MARK), 3)
        self.assertIn("Reference: pkg/big.py (shown above)", body)

    def test_a_separate_reference_is_still_shown_whole(self) -> None:
        body = self._brief("pkg/other.py")
        self.assertIn("### Reference: pkg/other.py\nOTHER_REFERENCE = 2", body)
        self.assertEqual(body.count(MARK), 3)


if __name__ == "__main__":
    unittest.main()
