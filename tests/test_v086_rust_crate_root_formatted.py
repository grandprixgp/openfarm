"""Pilot, 18 Nov 2026: a crate root was never formatted.

Whether a touched file was format-clean at base was judged by copying it out on
its own and running `rustfmt --check` on the copy. A file with `mod x;` cannot
be read that way (rustfmt looks for x.rs beside it), so src/lib.rs always read
as dirty at base, was left alone, and the card's own lines in it stayed
unformatted (pilot card F1, run again on v0.8.6).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.packs.rust import RustPack

CLEAN = "mod display;\n\npub fn f(a: u8, b: u8) -> bool {\n    a == b\n}\n"
DIRTY = "mod display;\n\npub fn f(a: u8, b: u8) -> bool {\n    a\n        == b\n}\n"


@unittest.skipUnless(shutil.which("rustfmt"), "rustfmt is not installed")
class CrateRootFormatCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="of-v086-fmt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "Cargo.toml").write_text('[package]\nname = "t"\nversion = "0.1.0"\nedition = "2021"\n')
        self.pack = RustPack()

    def test_a_clean_crate_root_reads_clean_without_its_modules(self) -> None:
        self.assertIs(self.pack.format_clean_text(CLEAN, self.tmp), True)

    def test_a_dirty_one_reads_dirty(self) -> None:
        self.assertIs(self.pack.format_clean_text(DIRTY, self.tmp), False)

    def test_text_rustfmt_cannot_parse_gives_no_verdict(self) -> None:
        self.assertIsNone(self.pack.format_clean_text("fn (", self.tmp))


if __name__ == "__main__":
    unittest.main()
