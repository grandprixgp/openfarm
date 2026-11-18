"""Second outside review, 6 and 7 (TypeScript).

6: touches with a wildcard got past the refusal of unchecked JSX. 7: two tests
with the same name in different files were read as one test with mixed results.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openfarm_core.cards import Card
from openfarm_core.gates.checks import combined_status, rows_for_test
from openfarm_core.gates.eligibility import check_eligibility
from tests.test_v081b_ts_parser import NODE, _project, _run
from tests.test_v081b_ts_syntax import _cfg, _tmp


def _card(touch: str) -> Card:
    return Card("C-1", "t", done_text="`f()` returns `1`", touches=[touch],
                fields={"category": "small-feature",
                        "tests": [{"name": "f", "file": "tests/f.test.ts"}]})


class WildcardTouchesAndJsxTests(unittest.TestCase):
    def _rules(self, touch: str) -> dict[str, str]:
        root = _tmp(self) / "r"
        root.mkdir()
        return {f["rule"]: f["detail"] for f in
                check_eligibility(_card(touch), _cfg(root), worktree=root).failures}

    def test_a_pattern_that_can_match_jsx_is_refused_while_syntax_only(self) -> None:
        for touch in ("src/**", "src/*", "**", "src/ui/*.tsx", "src/**/*.jsx"):
            with self.subTest(touch=touch):
                rules = self._rules(touch)
                self.assertIn("typescript-jsx-unchecked", rules)
        self.assertIn("Narrow the touches", self._rules("src/**")["typescript-jsx-unchecked"])

    def test_a_pattern_that_cannot_match_jsx_is_allowed(self) -> None:
        for touch in ("src/**/*.ts", "src/f.ts", "src/*.mjs"):
            with self.subTest(touch=touch):
                self.assertNotIn("typescript-jsx-unchecked", self._rules(touch))


@unittest.skipUnless(NODE, "needs node")
class SameNameInTwoFilesTests(unittest.TestCase):
    FILES = {
        "package.json": '{"name": "a", "type": "module"}\n',
        "tests/a.test.js": """
            import { test } from "node:test";
            import assert from "node:assert";
            test("adds", () => { assert.equal(1 + 1, 3); });
        """,
        "tests/b.test.js": """
            import { test } from "node:test";
            test("adds", () => {});
            test("only here", () => {});
        """,
    }

    def test_each_row_knows_its_file_and_the_cards_test_is_its_own(self) -> None:
        root = _project(self, self.FILES)
        pack, _done, parsed = _run(root)
        rows = [r for r in parsed["results"] if r["name"] == "adds"]
        self.assertEqual(sorted((pack.row_file(r), r["status"]) for r in rows),
                         [("tests/a.test.js", "FAILED"), ("tests/b.test.js", "ok")])
        mine = rows_for_test(parsed["results"], "adds", "tests/a.test.js", pack.row_file)
        self.assertEqual(combined_status(mine), "FAILED")
        theirs = rows_for_test(parsed["results"], "adds", "tests/b.test.js", pack.row_file)
        self.assertEqual(combined_status(theirs), "ok")
        # Without the file both rows are the test's, which is what read as mixed.
        self.assertEqual(combined_status(rows_for_test(parsed["results"], "adds")), "mixed")

    def test_a_listing_row_still_names_its_file(self) -> None:
        pack, _done, _parsed = _run(_project(self, self.FILES))
        self.assertEqual(pack.row_file({"name": "adds", "binary": "tests/a.test.js"}),
                         "tests/a.test.js")
        self.assertIsNone(pack.row_file({"name": "adds", "binary": ""}))


if __name__ == "__main__":
    unittest.main()
