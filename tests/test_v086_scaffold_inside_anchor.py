"""Pilot, 18 Nov 2026: a stub put inside the anchor, above an `impl`'s closing brace.

Card P5 (a new `BuildMetadata::identifiers`) was blocked as tests-unprovable. The
supervisor's scaffold anchored on the last three methods of `impl BuildMetadata`
and their closing brace, and put the stub between the last method and that
brace. Only text added before or after the whole anchor was recognised, so the
kept method bodies were judged as if they were the scaffold, the stub was
refused, the new method never existed and the named test could not build.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.edits import FORMAT_SEARCH_REPLACE, Edit
from openfarm_core.micromanager import _scaffold_body_to_validate, _valid_production_scaffold

# The pilot's anchor, as the supervisor sent it.
ANCHOR = (
    "    pub fn new(text: &str) -> Result<Self, Error> {\n"
    "        BuildMetadata::from_str(text)\n"
    "    }\n"
    "\n"
    "    pub fn as_str(&self) -> &str {\n"
    "        self.identifier.as_str()\n"
    "    }\n"
    "\n"
    "    pub fn is_empty(&self) -> bool {\n"
    "        self.identifier.is_empty()\n"
    "    }\n"
    "}\n"
)
STUB = (
    "\n"
    "    /// The dot-separated identifiers of this build metadata, in order.\n"
    "    pub fn identifiers(&self) -> Vec<&str> {\n"
    "        todo!()\n"
    "    }\n"
)
CLOSE = "}\n"


def _ok(search: str, replace: str) -> bool:
    edit = Edit(format=FORMAT_SEARCH_REPLACE, path="src/lib.rs", search=search,
                replace=replace, content=None, test_name="")
    return _valid_production_scaffold(_scaffold_body_to_validate(edit))


def _inside(anchor: str, block: str) -> str:
    """`anchor` with `block` put just above its final closing brace."""
    assert anchor.endswith(CLOSE)
    return anchor[: -len(CLOSE)] + block + CLOSE


class StubInsideTheAnchorTests(unittest.TestCase):
    def test_the_pilots_scaffold_is_taken(self) -> None:
        self.assertTrue(_ok(ANCHOR, _inside(ANCHOR, STUB)))

    def test_a_stub_between_two_kept_methods_is_taken(self) -> None:
        cut = ANCHOR.index("    pub fn as_str")
        self.assertTrue(_ok(ANCHOR, ANCHOR[:cut] + STUB.lstrip("\n") + "\n" + ANCHOR[cut:]))

    def test_real_code_put_there_is_refused(self) -> None:
        body = "\n    pub fn identifiers(&self) -> Vec<&str> {\n        self.as_str().split('.').collect()\n    }\n"
        self.assertFalse(_ok(ANCHOR, _inside(ANCHOR, body)))

    def test_a_stub_plus_a_changed_kept_body_is_refused(self) -> None:
        changed = _inside(ANCHOR, STUB).replace("BuildMetadata::from_str(text)",
                                                 "Ok(BuildMetadata::EMPTY)")
        self.assertFalse(_ok(ANCHOR, changed))

    def test_a_block_that_closes_a_kept_function_is_refused(self) -> None:
        # Put between a kept function's last line and its brace: not whole blocks.
        cut = ANCHOR.index("    }\n\n    pub fn as_str")
        sneaky = "        ;\n        std::process::exit(1)\n    }\n    fn other(&self) {\n        todo!()\n"
        self.assertFalse(_ok(ANCHOR, ANCHOR[:cut] + sneaky + ANCHOR[cut:]))

    def test_work_beside_the_stub_is_refused(self) -> None:
        self.assertFalse(_ok(ANCHOR, _inside(ANCHOR, "\n    pub const N: usize = boot();\n" + STUB)))


if __name__ == "__main__":
    unittest.main()
