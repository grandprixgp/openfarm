"""A retry shows the worker the files it must search, like the first attempt did."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import BriefBook, implementer_brief
from openfarm_core.cards import Card


class RetryBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="openfarm-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "pkg").mkdir()
        self.card = Card(id="CARD-1", title="Bench list", state="ready", done_text="d",
                         touches=["pkg/cli.py"],
                         fields={"category": "small-feature",
                                 "tests": [{"name": "test_a", "file": "tests/test_a.py"}]})

    def _retry(self) -> str:
        book = BriefBook(self.repo, never_touch=[])
        return implementer_brief(self.card, book, {"tests/test_a.py": "x = 1\n"},
                                 attempt=2, diff="-old\n+new\n",
                                 failure="search block matched 0 times").body

    def test_a_large_file_is_repeated_on_retry(self) -> None:
        # cli.py was 66k characters when CARD-011's retries lost it.
        line = "PRE_CONFIG_COMMANDS = {}  # marker\n"
        (self.repo / "pkg" / "cli.py").write_text("# filler line\n" * 5000 + line)
        body = self._retry()
        self.assertIn("### pkg/cli.py\n", body)
        self.assertIn(line, body)

    def test_a_file_too_big_even_for_that_is_named_not_dropped(self) -> None:
        (self.repo / "pkg" / "cli.py").write_text("# filler line\n" * 20000)
        body = self._retry()
        self.assertIn("### pkg/cli.py (280000 characters, too long to repeat", body)


if __name__ == "__main__":
    unittest.main()
