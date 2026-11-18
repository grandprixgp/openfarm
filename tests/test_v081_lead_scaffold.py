"""Lead: the supervisor's Rust scaffold, as a real supervisor writes it.

Bench round 2 with Claude supervising: two Rust cards with new symbols lost
their scaffold to "production scaffold must contain `todo!(` or
`unimplemented!(` stubs only", so no stub existed, the test could not build and
the card was blocked. Both scaffolds were stubs. One kept its parameters used
with `let _ = (a, b, c);` before `todo!()`, which a lint run with warnings as
errors needs; the other was anchored on the closing lines of the `impl` above,
so the kept lines were judged as if they were the scaffold.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.edits import FORMAT_SEARCH_REPLACE, Edit
from openfarm_core.micromanager import _scaffold_body_to_validate, _valid_production_scaffold

ANCHOR = '    fn name(&self) -> &\'static str {\n        "square"\n    }\n}\n'
TRIANGLE = '''
/// A triangle.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Triangle {
    a: f64,
    b: f64,
    c: f64,
}

impl Triangle {
    pub fn new(a: f64, b: f64, c: f64) -> Option<Triangle> {
        let _ = (a, b, c);
        todo!()
    }
}

impl Shape for Triangle {
    fn area(&self) -> f64 {
        todo!()
    }
}
'''


def _edit(search: str, replace: str) -> Edit:
    return Edit(format=FORMAT_SEARCH_REPLACE, path="src/polygon.rs", search=search,
                replace=replace, content=None, test_name="")


def _ok(search: str, replace: str) -> bool:
    return _valid_production_scaffold(_scaffold_body_to_validate(_edit(search, replace)))


class StubBodyTests(unittest.TestCase):
    def test_parameters_may_be_kept_used_before_the_panic(self) -> None:
        for body in ("let _ = a; todo!()",
                     "let _ = (a, b);\n    unimplemented!();",
                     "let _ = (raw, line,);\n    todo!()",
                     "let _ = &self;\n    let _ = (a, &b);\n    todo!()"):
            with self.subTest(body=body):
                self.assertTrue(_valid_production_scaffold(f"pub fn f(a: u8, b: u8) {{ {body} }}"))

    def test_anything_that_does_work_is_still_refused(self) -> None:
        for body in ("let _ = launch(a); todo!()",
                     "let x = a; todo!()",
                     'let _ = (a, std::fs::remove_file("x")); todo!()',
                     "let _ = a.clone(); todo!()",
                     "evil(); todo!()",
                     "todo!(); evil()",
                     "a + 1"):
            with self.subTest(body=body):
                self.assertFalse(_valid_production_scaffold(f"pub fn f(a: u8) -> u8 {{ {body} }}"))


class AnchoredOnTheEndOfABlockTests(unittest.TestCase):
    def test_stubs_added_after_the_end_of_an_impl_are_judged_alone(self) -> None:
        self.assertTrue(_ok(ANCHOR, ANCHOR + TRIANGLE))

    def test_stubs_added_before_the_start_of_an_item_are_judged_alone(self) -> None:
        anchor = "/// Loads.\npub fn load("
        added = "pub fn parse_line(raw: &str) -> Option<String> {\n    let _ = raw;\n    todo!()\n}\n\n"
        self.assertTrue(_ok(anchor, added + anchor))

    def test_real_code_added_there_is_refused(self) -> None:
        self.assertFalse(_ok(ANCHOR, ANCHOR + "\npub fn area(r: f64) -> f64 {\n    r * r\n}\n"))

    def test_a_statement_slipped_into_the_kept_function_is_refused(self) -> None:
        anchor = '    fn name(&self) -> &\'static str {\n        "square"'
        self.assertFalse(_ok(anchor, anchor + ";\n        std::process::exit(1)"))

    def test_added_text_that_closes_the_kept_block_itself_is_judged_whole(self) -> None:
        # The added text is not whole blocks, so the kept lines count: a real body.
        anchor = '    fn name(&self) -> &\'static str {\n        "square"'
        self.assertFalse(_ok(anchor, anchor + "\n    }\n    fn other(&self) {\n        todo!()"))

    def test_a_changed_anchor_is_judged_whole(self) -> None:
        self.assertFalse(_ok(ANCHOR, ANCHOR.replace('"square"', '"circle"') + TRIANGLE))


if __name__ == "__main__":
    unittest.main()
