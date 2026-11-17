"""Applying edits: exactly-once search/replace, whole files, additions only."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openfarm_core.edits import (Edit, apply_edits, line_counts, parse_edits,
                                 touched_paths)


class ParseEditsTests(unittest.TestCase):
    def test_search_replace_round_trip(self) -> None:
        text = (
            "first words\n"
            "=== search_replace: src/a.rs\n"
            "<<< SEARCH\n"
            "let x = 1;\n"
            ">>> REPLACE\n"
            "let x = 2;\n")
        edits = parse_edits(text)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].path, "src/a.rs")
        self.assertEqual(edits[0].search, "let x = 1;")
        self.assertEqual(edits[0].replace, "let x = 2;")

    def test_whole_file(self) -> None:
        text = "=== whole_file: src/b.rs\npub fn b() {}\n"
        edits = parse_edits(text)
        self.assertEqual(edits[0].format, "whole_file")
        self.assertEqual(edits[0].content, "pub fn b() {}\n")

    def test_two_blocks(self) -> None:
        text = (
            "=== search_replace: src/a.rs\n<<< SEARCH\none\n>>> REPLACE\ntwo\n"
            "=== whole_file: src/c.rs\nnew\n")
        edits = parse_edits(text)
        self.assertEqual([e.path for e in edits], ["src/a.rs", "src/c.rs"])

    def test_no_blocks_is_an_invalid_answer(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            parse_edits("just prose, no blocks")

    def test_malformed_block_is_an_invalid_answer(self) -> None:
        from openfarm_core.errors import Invalid
        with self.assertRaises(Invalid):
            parse_edits("=== search_replace: src/a.rs\nno markers at all\n")


class ApplyEditsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.rs").write_text("let x = 1;\nlet y = 2;\n")
        (self.tmp / "src" / "b.rs").write_text("pub fn b() {}\n")

    def test_search_and_replace_applies(self) -> None:
        edits = [Edit("search_replace", "src/a.rs", search="let x = 1;",
                      replace="let x = 9;")]
        report = apply_edits(self.tmp, edits, ["src/**"], [], [])
        self.assertTrue(report.ok)
        self.assertIn("let x = 9;", (self.tmp / "src" / "a.rs").read_text())

    def test_a_non_unique_match_is_refused(self) -> None:
        (self.tmp / "src" / "a.rs").write_text("same\nsame\n")
        edits = [Edit("search_replace", "src/a.rs", search="same", replace="other")]
        report = apply_edits(self.tmp, edits, ["src/**"], [], [])
        self.assertFalse(report.ok)
        self.assertEqual(report.refused[0]["code"], "no-unique-match")
        self.assertEqual((self.tmp / "src" / "a.rs").read_text(), "same\nsame\n")

    def test_a_missing_match_is_refused(self) -> None:
        edits = [Edit("search_replace", "src/a.rs", search="absent", replace="x")]
        report = apply_edits(self.tmp, edits, ["src/**"], [], [])
        self.assertEqual(report.refused[0]["code"], "no-unique-match")

    def test_never_touch_is_refused(self) -> None:
        edits = [Edit("whole_file", "src/seam.rs", content="x")]
        report = apply_edits(self.tmp, edits, ["src/seam.rs"], ["src/seam.rs"], [])
        self.assertEqual(report.refused[0]["code"], "never-touch")
        self.assertFalse((self.tmp / "src" / "seam.rs").exists())

    def test_forbidden_is_refused(self) -> None:
        edits = [Edit("whole_file", "Cargo.toml", content="x")]
        report = apply_edits(self.tmp, edits, ["Cargo.toml"], [],
                             ["**/Cargo.toml"])
        self.assertEqual(report.refused[0]["code"], "forbidden-file")

    def test_outside_touches_is_reported(self) -> None:
        edits = [Edit("whole_file", "src/other.rs", content="x")]
        report = apply_edits(self.tmp, edits, ["src/a.rs"], [], [])
        self.assertEqual(report.outside_touches, ["src/other.rs"])
        self.assertFalse((self.tmp / "src" / "other.rs").exists())

    def test_additions_only_is_enforced(self) -> None:
        edits = [Edit("search_replace", "src/b.rs",
                      search="pub fn b() {}\n",
                      replace="pub fn b() {}\npub fn c() {}\n")]
        report = apply_edits(self.tmp, edits, ["src/b.rs"], [], [],
                             additions_only={"src/b.rs"})
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.not_additions_only, [])
        self.assertIn("pub fn c()", (self.tmp / "src" / "b.rs").read_text())

    def test_additions_only_refuses_a_rewrite(self) -> None:
        edits = [Edit("search_replace", "src/b.rs",
                      search="pub fn b() {}", replace="pub fn z() {}")]
        report = apply_edits(self.tmp, edits, ["src/b.rs"], [], [],
                             additions_only={"src/b.rs"})
        self.assertEqual(report.not_additions_only, ["src/b.rs"])

    def test_whole_file_rewrite_fails_additions_only(self) -> None:
        edits = [Edit("whole_file", "src/b.rs", content="entirely new\n")]
        report = apply_edits(self.tmp, edits, ["src/b.rs"], [], [],
                             additions_only={"src/b.rs"})
        self.assertEqual(report.not_additions_only, ["src/b.rs"])


class DiffHelperTests(unittest.TestCase):
    def test_line_counts_ignore_the_file_markers(self) -> None:
        diff = "--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+extra\n"
        self.assertEqual(line_counts(diff), (2, 1))

    def test_touched_paths_include_both_sides_and_new_files(self) -> None:
        diff = ("diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n"
                "diff --git a/src/new.rs b/src/new.rs\n"
                "--- /dev/null\n+++ b/src/new.rs\n")
        self.assertEqual(touched_paths(diff), ["src/a.rs", "src/new.rs"])


class StubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.rs").write_text("pub fn a() {}\n")

    def _inserter(self):
        def insert(target: Path, test_name: str, body: str) -> None:
            target.write_text(target.read_text() + body + "\n")
        return insert

    def test_stub_is_applied_when_the_card_declares_new_symbols(self) -> None:
        edits = [Edit("append_stub", "src/a.rs",
                      content="pub fn missing() { todo!() }", test_name="t")]
        report = apply_edits(self.tmp, edits, ["src/a.rs"], [], [],
                             allow_stubs=True, stub_inserter=self._inserter())
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.stubs, ["src/a.rs"])
        self.assertIn("pub fn missing", (self.tmp / "src" / "a.rs").read_text())

    def test_stub_is_refused_when_not_declared(self) -> None:
        edits = [Edit("append_stub", "src/a.rs",
                      content="pub fn missing() {}", test_name="t")]
        report = apply_edits(self.tmp, edits, ["src/a.rs"], [], [],
                             allow_stubs=False, stub_inserter=self._inserter())
        self.assertEqual(report.refused[0]["code"], "stub-not-allowed")
        self.assertEqual((self.tmp / "src" / "a.rs").read_text(), "pub fn a() {}\n")

    def test_stub_block_parses_with_its_test_name(self) -> None:
        text = ("=== append_stub: src/a.rs\n"
                "a_named_test\n"
                "pub fn missing() { todo!() }\n")
        edits = parse_edits(text)
        self.assertEqual(edits[0].format, "append_stub")
        self.assertEqual(edits[0].test_name, "a_named_test")
        self.assertIn("pub fn missing", edits[0].content)

    def test_multiword_stub_name_ts_closing_tag_and_empty_body_refused(self) -> None:
        from openfarm_core.errors import Invalid

        ts_text = (
            "=== append_stub: src/slug.ts\n"
            "boundedSlug trims and drops trailing dash\n"
            "export function boundedSlug(): string {\n"
            '  throw new Error("not implemented");\n'
            "}\n"
            "</test>\n"
        )
        edits = parse_edits(ts_text)
        self.assertEqual(edits[0].test_name, "boundedSlug trims and drops trailing dash")
        self.assertNotIn("</test>", edits[0].content)

        with self.assertRaises(Invalid):
            parse_edits("=== append_stub: src/slug.ts\nonly_test_name\n")


if __name__ == "__main__":
    unittest.main()
