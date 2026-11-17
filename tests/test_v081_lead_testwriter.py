"""Lead: what the bench on the final code showed about the test writer.

Four of five farm losses with live workers came from the same two gaps. A Rust
test writer shown only signatures did not know the crate's name, pulled the
source in with `#[path]`, and the copy's unused items then failed the lint gate,
which the implementer may not fix. And a test build that failed was reported as
"not reported by the test binary" with no compiler error, so the second try was
a guess.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.briefs import BriefBook, test_writer_brief
from openfarm_core.cards import Card
from openfarm_core.packs.base import Pack
from openfarm_core.packs.python import PythonPack
from openfarm_core.packs.rust import RustPack
from tests import test_e2e

LIB = "pub mod stack;\n"


class RustTestWriterNoteTests(unittest.TestCase):
    def _tree(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp(prefix="openfarm-twnote-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return root

    def test_names_the_library_crate_for_an_integration_test(self) -> None:
        root = self._tree({"Cargo.toml": '[package]\nname = "stacks"\n', "src/lib.rs": LIB})
        note = RustPack().test_writer_note(root, ["tests/pop_n.rs"])
        self.assertIn("`use stacks::...;`", note)
        self.assertIn("tests/pop_n.rs", note)
        self.assertIn("Never pull a source file in", note)
        self.assertIn("#[path", note)

    def test_a_hyphen_in_the_package_name_is_an_underscore_in_code(self) -> None:
        root = self._tree({"Cargo.toml": '[package]\nname = "my-stacks"\n', "src/lib.rs": LIB})
        self.assertIn("`use my_stacks::...;`", RustPack().test_writer_note(root, ["tests/a.rs"]))

    def test_a_lib_table_wins_over_the_package_name(self) -> None:
        root = self._tree({
            "Cargo.toml": '[package]\nname = "pkg"\n[lib]\nname = "core_lib"\npath = "lib/main.rs"\n',
            "lib/main.rs": LIB})
        self.assertIn("`use core_lib::...;`", RustPack().test_writer_note(root, ["tests/a.rs"]))

    def test_a_workspace_member_gets_its_own_crate_name(self) -> None:
        root = self._tree({
            "Cargo.toml": '[workspace]\nmembers = ["crates/inner"]\n',
            "crates/inner/Cargo.toml": '[package]\nname = "inner"\n',
            "crates/inner/src/lib.rs": LIB})
        note = RustPack().test_writer_note(root, ["crates/inner/tests/a.rs"])
        self.assertIn("`use inner::...;`", note)

    def test_says_nothing_where_there_is_nothing_true_to_say(self) -> None:
        pack = RustPack()
        binary_only = self._tree({"Cargo.toml": '[package]\nname = "tool"\n',
                                  "src/main.rs": "fn main() {}\n"})
        self.assertEqual(pack.test_writer_note(binary_only, ["tests/a.rs"]), "")
        lib = self._tree({"Cargo.toml": '[package]\nname = "stacks"\n', "src/lib.rs": LIB})
        # A unit test inside `src/` is already in the crate.
        self.assertEqual(pack.test_writer_note(lib, ["src/stack.rs"]), "")
        self.assertEqual(pack.test_writer_note(lib, []), "")
        no_manifest = self._tree({"src/lib.rs": LIB})
        self.assertEqual(pack.test_writer_note(no_manifest, ["tests/a.rs"]), "")
        broken = self._tree({"Cargo.toml": "[package\n", "src/lib.rs": LIB})
        self.assertEqual(pack.test_writer_note(broken, ["tests/a.rs"]), "")
        odd = self._tree({"Cargo.toml": '[package]\nname = "a b`c"\n', "src/lib.rs": LIB})
        self.assertEqual(pack.test_writer_note(odd, ["tests/a.rs"]), "")

    def test_never_reads_a_manifest_outside_the_worktree(self) -> None:
        outer = self._tree({"Cargo.toml": '[package]\nname = "outside"\n', "src/lib.rs": LIB,
                            "wt/placeholder": ""})
        self.assertEqual(RustPack().test_writer_note(outer / "wt", ["../tests/a.rs"]), "")

    def test_the_brief_carries_the_note_and_other_packs_add_nothing(self) -> None:
        root = self._tree({"Cargo.toml": '[package]\nname = "stacks"\n', "src/lib.rs": LIB,
                           "src/stack.rs": "pub fn pop_n() {}\n"})
        card = Card(id="C-1", title="t", fields={
            "category": "small-feature",
            "tests": [{"name": "pops", "file": "tests/pop_n.rs", "asserts": "x"}]},
            touches=["src/stack.rs"])
        body = test_writer_brief(card, BriefBook(root, [], []), RustPack()).body
        self.assertEqual(body.count("which is named `stacks`"), 1, body)
        self.assertEqual(Pack.test_writer_note(PythonPack(ruff=False), root, ["tests/a.py"]), "")

    def test_a_card_with_new_symbols_is_told_not_to_stub_what_is_declared(self) -> None:
        root = self._tree({"Cargo.toml": '[package]\nname = "stacks"\n', "src/lib.rs": LIB,
                           "src/stack.rs": "pub fn pop_n() {}\n"})
        card = Card(id="C-1", title="t", fields={
            "category": "small-feature", "new_symbols": ["pop_n"],
            "tests": [{"name": "pops", "file": "tests/pop_n.rs", "asserts": "x"}]},
            touches=["src/stack.rs"])
        body = test_writer_brief(card, BriefBook(root, [], []), RustPack()).body
        self.assertIn("do not stub it again", body)


RUSTC_DUPLICATE = """\
   Compiling settings v0.1.0 (/x/worktrees/CARD-1)
error[E0428]: the name `parse_line` is defined multiple times
  --> src/config.rs:67:1
   |
15 | pub fn parse_line(raw: &str) {
   | ---------------------------- previous definition of the value `parse_line` here
warning: unused variable: `raw`
  --> src/config.rs:15:19
error: cannot find value `nope` in this scope
 --> tests/a.rs:3:5
For more information about this error, try `rustc --explain E0428`.
error: could not compile `settings` (lib) due to 2 previous errors; 2 warnings emitted
error: could not compile `settings` (lib test) due to 2 previous errors
"""


class BuildErrorsTests(unittest.TestCase):
    def test_rustc_errors_with_their_places_and_nothing_else(self) -> None:
        text = RustPack().build_errors("", RUSTC_DUPLICATE)
        self.assertEqual(
            text,
            "error[E0428]: the name `parse_line` is defined multiple times (src/config.rs:67:1)"
            " | error: cannot find value `nope` in this scope (tests/a.rs:3:5)")

    def test_no_errors_no_text(self) -> None:
        self.assertEqual(RustPack().build_errors("ok", "warning: unused\n"), "")
        self.assertEqual(Pack.build_errors(PythonPack(ruff=False), "", "error: x"), "")

    def test_a_long_list_is_cut_to_the_first_few(self) -> None:
        many = "".join(f"error: problem {n}\n --> src/a.rs:{n}:1\n" for n in range(1, 9))
        self.assertEqual(RustPack().build_errors("", many).count(" | "), 3)


@unittest.skipUnless(test_e2e.ENABLED, "set OPENFARM_E2E=1")
class UnprovableTestsSayWhyTests(test_e2e.GamingTrickTests):
    """Only the one new case runs here; the parent's cases run in their own module."""

    def test_a_test_that_does_not_compile_says_what_the_compiler_said(self) -> None:
        self.plant("CARD-COMPILE")
        outcome = self.run_card("CARD-COMPILE", test_e2e.script(**{
            "CARD-COMPILE/test_writer": "=== whole_file: tests/full.rs\n"
                                        + test_e2e.BAD_TEST_COMPILE_ERROR}))
        self.assertEqual(outcome["stop_code"], "tests-unprovable")
        gates = sorted((self.toy.state_dir / "cards" / "CARD-COMPILE").glob(
            "*/gates/*/named-tests-exist.json"))
        self.assertTrue(gates)
        gate = json.loads(gates[-1].read_text())
        self.assertIn("the test build failed: error[E0599]", gate["detail"])
        self.assertIn("push_twice", gate["evidence"]["build_errors"])
        self.assertIn("(tests/full.rs:", gate["evidence"]["build_errors"])


def load_tests(loader, tests, pattern):
    """Keep the inherited end-to-end cases from running a second time here."""
    suite = unittest.TestSuite()
    for case in (RustTestWriterNoteTests, BuildErrorsTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    suite.addTest(UnprovableTestsSayWhyTests(
        "test_a_test_that_does_not_compile_says_what_the_compiler_said"))
    return suite


if __name__ == "__main__":
    unittest.main()
