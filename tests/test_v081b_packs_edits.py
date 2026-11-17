"""The `append_stub` name line (v0.8.1 fix wave 2, P7, rev-ts finding 16).

Since the first line of an `append_stub` block became the test's name, a stub sent
without a name line lost its first line to the name and was refused later by the
pack with a message about the stub. A first line of only spaces was refused where
v0.8 accepted it. Now the parser refuses a block whose first line is the stub's own
code, saying the block needs the test's name there, for every language; blank lines
before the name are skipped again.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.edits import apply_edits, parse_edits
from openfarm_core.errors import Invalid
from openfarm_core.packs.python import PythonPack
from openfarm_core.packs.rust import RustPack

NAMELESS = {
    "python": ("tally/bounded.py",
               "def remaining(self) -> int:\n    raise NotImplementedError\n"),
    "python-class": ("tally/bounded.py",
                     "class Room:\n    def size(self):\n"
                     "        raise NotImplementedError\n"),
    "rust": ("src/fascicle.rs",
             "pub fn remaining(&self) -> usize {\n    todo!()\n}\n"),
    "rust-attr": ("src/fascicle.rs",
                  "#[must_use]\npub fn remaining(&self) -> usize {\n    todo!()\n}\n"),
    "typescript": ("src/slug.ts",
                   "export function boundedSlug(s: string): string {\n"
                   "  throw new Error(\"not implemented\");\n}\n"),
    "typescript-arrow": ("src/slug.ts",
                         "export const boundedSlug = (s: string): string => {\n"
                         "  throw new Error(\"not implemented\");\n};\n"),
}


class StubNameLineTests(unittest.TestCase):
    def test_a_block_without_a_name_line_is_refused_when_parsed(self) -> None:
        for language, (path, stub) in NAMELESS.items():
            with self.subTest(language):
                with self.assertRaises(Invalid) as caught:
                    parse_edits(f"=== append_stub: {path}\n{stub}")
                self.assertEqual(caught.exception.code, "bad-block")
                message = caught.exception.message
                self.assertIn("needs the test's name on its first line", message)
                self.assertIn("is code, not the name", message)
                self.assertIn(stub.splitlines()[0][:40], message)

    def test_blank_lines_before_the_name_are_skipped(self) -> None:
        for lead in ("\n", "   \n", "\t\n  \n", " \r\n\n"):
            with self.subTest(lead=repr(lead)):
                edits = parse_edits("=== append_stub: src/a.rs\n" + lead
                                    + "a_named_test\npub fn missing() { todo!() }\n")
                self.assertEqual(edits[0].test_name, "a_named_test")
                self.assertEqual(edits[0].content.strip(),
                                 "pub fn missing() { todo!() }")

    def test_spaces_only_lines_before_the_name_inside_a_fence(self) -> None:
        # The header swallows plain blank lines; behind a model's code fence they
        # reach the name parser, which read a line of spaces as the name.
        for lead in ("   \n  \n", "\t\n \n\n"):
            with self.subTest(lead=repr(lead)):
                edits = parse_edits("=== append_stub: src/a.rs\n```rust\n" + lead
                                    + "a_named_test\npub fn missing() { todo!() }\n"
                                    "```\n")
                self.assertEqual(edits[0].test_name, "a_named_test")
                self.assertEqual(edits[0].content.strip(),
                                 "pub fn missing() { todo!() }")

    def test_names_are_kept_whole_and_stripped(self) -> None:
        cases = {
            "  a_named_test  ": "a_named_test",
            "boundedSlug trims and drops trailing dash":
                "boundedSlug trims and drops trailing dash",
            "class names are kebab-cased": "class names are kebab-cased",
            "function returns its input": "function returns its input",
            "throws on bad input": "throws on bad input",
            "import works": "import works",
            "test_param[1-2]": "test_param[1-2]",
        }
        for line, wanted in cases.items():
            with self.subTest(line):
                edits = parse_edits("=== append_stub: src/slug.ts\n" + line + "\n"
                                    "export function x(): string {\n"
                                    "  throw new Error(\"not implemented\");\n}\n")
                self.assertEqual(edits[0].test_name, wanted)

    def test_a_name_alone_or_an_empty_block_is_refused(self) -> None:
        for body, words in (("only_test_name\n", "only the name"),
                            ("\n   \n", "the block is empty")):
            with self.subTest(body=repr(body)):
                with self.assertRaises(Invalid) as caught:
                    parse_edits("=== append_stub: src/a.rs\n" + body)
                self.assertIn("needs the test's name on its first line",
                              caught.exception.message)
                self.assertIn(words, caught.exception.message)

    def test_a_named_block_still_reaches_the_pack(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="openfarm-stub-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "tally").mkdir()
        (root / "tally" / "bounded.py").write_text("class Bounded:\n    pass\n")
        (root / "src").mkdir()
        (root / "src" / "fascicle.rs").write_text("pub struct F;\n")
        text = ("=== append_stub: tally/bounded.py\n  \n"
                "test_a_full_bundle_reports_its_room\n"
                "def remaining(bundle) -> int:\n    raise NotImplementedError\n"
                "=== append_stub: src/fascicle.rs\n"
                "remaining_room_is_reported\n"
                "pub fn remaining() -> usize {\n    todo!()\n}\n")
        edits = parse_edits(text)
        for edit, pack in zip(edits, (PythonPack(ruff=False), RustPack())):
            report = apply_edits(root, [edit], [edit.path], [], [],
                                 allow_stubs=True, stub_inserter=pack.append_stub)
            self.assertTrue(report.ok, report.as_dict())
        self.assertIn("raise NotImplementedError",
                      (root / "tally" / "bounded.py").read_text())
        self.assertIn("todo!()", (root / "src" / "fascicle.rs").read_text())


if __name__ == "__main__":
    unittest.main()
