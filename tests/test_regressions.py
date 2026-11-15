"""Regressions for the bugs a review found in the first implementation.

Each case here exists because the code was wrong in a way that a gate would not have
noticed. They are the cheapest possible insurance against those bugs coming back.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from openfarm_core.cards import Card
from openfarm_core.config import Config
from openfarm_core.edits import Edit, apply_edits
from openfarm_core.gates.eligibility import _units_of, check_eligibility
from openfarm_core.git import Git, GitRefused, _check_subcommand
from openfarm_core.globs import escapes, normalise, matches
from openfarm_core.packs.rust import RustPack


class PathEscapeTests(unittest.TestCase):
    """A path that climbs out of the repository must never be written."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.wt = self.tmp / "repo"
        (self.wt / "crates" / "x" / "src").mkdir(parents=True)

    def test_dot_dot_is_collapsed(self) -> None:
        self.assertEqual(normalise("a/b/../c.rs"), "a/c.rs")
        self.assertEqual(normalise("./a/./b.rs"), "a/b.rs")
        self.assertEqual(normalise("a/../../b.rs"), "../b.rs")

    def test_escapes_is_reported(self) -> None:
        self.assertTrue(escapes("../outside.txt"))
        self.assertTrue(escapes("a/../../outside.txt"))
        self.assertFalse(escapes("a/b/c.rs"))

    def test_a_climbing_path_does_not_match_a_glob_for_its_prefix(self) -> None:
        # `crates/x/src/../../../x.txt` normalises out of the prefix entirely.
        self.assertFalse(matches("crates/x/src/../../../x.txt", "crates/x/**"))

    def test_an_edit_with_a_dot_dot_cannot_land_outside_the_worktree(self) -> None:
        """`..` collapses when the edit is read, so it cannot climb out.

        `crates/x/src/../../../escaped.txt` becomes `escaped.txt`: still inside the
        worktree, and outside `crates/x/**`, so it is refused as outside touches
        rather than written to the parent directory.
        """
        edit = Edit("whole_file", "crates/x/src/../../../escaped.txt",
                    content="gone")
        self.assertEqual(edit.path, "escaped.txt",
                         "the path is normalised when the edit is read")
        report = apply_edits(self.wt, [edit], ["crates/x/**"], [], [])
        self.assertFalse((self.tmp / "escaped.txt").exists(),
                         "nothing may be written beside the worktree")
        self.assertFalse((self.wt.parent / "escaped.txt").exists())
        self.assertIn("escaped.txt", report.outside_touches)

    def test_a_path_above_the_root_is_refused_outright(self) -> None:
        edit = Edit("whole_file", "../escaped.txt", content="gone")
        report = apply_edits(self.wt, [edit], ["**"], [], [])
        self.assertIn("path-escape",
                      [entry["code"] for entry in report.refused])
        self.assertFalse((self.tmp / "escaped.txt").exists())

    def test_a_symlink_out_of_the_worktree_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.wt / "crates" / "x" / "src" / "link").symlink_to(outside)
        edit = Edit("whole_file", "crates/x/src/link/escaped.txt", content="x")
        report = apply_edits(self.wt, [edit], ["crates/x/**"], [], [])
        self.assertIn("path-escape",
                      [entry["code"] for entry in report.refused])
        self.assertFalse((outside / "escaped.txt").exists())


class ProductionWriteTests(unittest.TestCase):
    """The test writer writes tests, not production code."""

    def setUp(self) -> None:
        self.wt = Path(tempfile.mkdtemp())
        (self.wt / "src").mkdir()
        (self.wt / "src" / "lib.rs").write_text("pub fn a() {}\n")
        (self.wt / "tests").mkdir()

    def test_the_test_writer_may_not_rewrite_production(self) -> None:
        edit = Edit("whole_file", "src/lib.rs", content="pub fn evil() {}\n")
        report = apply_edits(self.wt, [edit], ["src/lib.rs", "tests/a.rs"], [], [],
                             production_paths={"src/lib.rs"},
                             forbid_production=True)
        self.assertIn("production-not-writable",
                      [entry["code"] for entry in report.refused])
        self.assertEqual((self.wt / "src" / "lib.rs").read_text(),
                         "pub fn a() {}\n")

    def test_an_append_stub_is_still_allowed_on_a_new_symbols_card(self) -> None:
        edit = Edit("append_stub", "src/lib.rs",
                    content="pub fn b() { todo!() }", test_name="t")
        report = apply_edits(
            self.wt, [edit], ["src/lib.rs"], [], [],
            additions_only={"src/lib.rs"},
            allow_stubs=True,
            production_paths={"src/lib.rs"},
            forbid_production=False,
            stub_inserter=lambda target, name, body:
                target.write_text(target.read_text() + body + "\n"))
        self.assertTrue(report.ok, report.as_dict())
        self.assertIn("pub fn b()", (self.wt / "src" / "lib.rs").read_text())

    def test_additions_only_works_for_a_glob(self) -> None:
        edit = Edit("search_replace", "src/lib.rs",
                    search="pub fn a() {}\n",
                    replace="pub fn a() {}\npub fn c() {}\n")
        report = apply_edits(self.wt, [edit], ["src/**"], [], [],
                             additions_only={"src/**"})
        self.assertEqual(report.not_additions_only, [])

    def test_additions_only_still_catches_a_rewrite_through_a_glob(self) -> None:
        edit = Edit("whole_file", "src/lib.rs", content="entirely new\n")
        report = apply_edits(self.wt, [edit], ["src/**"], [], [],
                             additions_only={"src/**"})
        self.assertEqual(report.not_additions_only, ["src/lib.rs"])


class WorkspaceClassifyTests(unittest.TestCase):
    """A crate under a workspace folder is still production code."""

    def setUp(self) -> None:
        self.pack = RustPack()

    def test_a_workspace_crate_is_production(self) -> None:
        result = self.pack.classify_paths([
            "crates/store/src/log.rs", "crates/store/tests/log.rs",
            "crates/api/src/gesture.rs", "src/lib.rs", "tests/it.rs"])
        self.assertEqual(result["crates/store/src/log.rs"], "production")
        self.assertEqual(result["crates/store/tests/log.rs"], "test")
        self.assertEqual(result["crates/api/src/gesture.rs"], "production")
        self.assertEqual(result["src/lib.rs"], "production")
        self.assertEqual(result["tests/it.rs"], "test")

    def test_a_production_unwrap_in_a_workspace_crate_is_banned(self) -> None:
        found = self.pack.patterns(
            [{"path": "crates/store/src/log.rs", "line": 3,
              "text": "    let x = y.unwrap();"}], [], {"crates/store/src/log.rs"})
        self.assertTrue(any(entry["pattern"] == "production-unwrap"
                            for entry in found["banned"]))


class UnitTests(unittest.TestCase):
    """max_units counts crates, not first path segments."""

    def test_three_workspace_crates_are_three_units(self) -> None:
        units = _units_of(["crates/store/src/log.rs",
                           "crates/runs/src/x.rs",
                           "crates/term/src/y.rs"])
        self.assertEqual(len(units), 3)

    def test_one_crate_at_depth_is_one_unit(self) -> None:
        units = _units_of(["crates/store/src/log.rs",
                           "crates/store/src/commit.rs",
                           "crates/store/tests/log.rs"])
        self.assertEqual(units, {"crates/store"})

    def test_a_flat_crate_is_one_unit(self) -> None:
        self.assertEqual(_units_of(["src/lib.rs", "tests/it.rs"]), {"."})

    def test_a_workspace_card_spanning_crates_is_refused(self) -> None:
        cfg = Config({
            "project": {"name": "t", "state_dir": "/tmp/openfarm-units-state"},
            "adapters": {"languages": ["rust"]},
            "policy": {"categories": ["pinned-bugfix"]},
            "eligibility": {"max_units": 1, "max_files": 6},
        }, Path("/tmp/none"))
        card = Card("CARD-1", "T", done_text="returns `Err(Full)`.",
                    touches=["crates/store/src/log.rs", "crates/api/src/gesture.rs"],
                    fields={"category": "pinned-bugfix",
                            "tests": [{"name": "a", "file": "tests/a.rs",
                                       "asserts": "Err(Full)"}]})
        result = check_eligibility(card, cfg)
        self.assertIn("max-units", [f["rule"] for f in result.failures])


class GitAllowlistTests(unittest.TestCase):
    """No argv shape may reach a forbidden subcommand."""

    def test_a_flag_value_does_not_hide_a_push(self) -> None:
        with self.assertRaises(GitRefused):
            _check_subcommand(["--namespace", "config", "push"])
        with self.assertRaises(GitRefused):
            _check_subcommand(["-C", "somewhere", "push"])
        with self.assertRaises(GitRefused):
            _check_subcommand(["--git-dir", "x", "fetch"])

    def test_an_allowed_subcommand_still_passes(self) -> None:
        self.assertEqual(_check_subcommand(["-C", "x", "status"]), "status")
        self.assertEqual(_check_subcommand(["--no-pager", "diff", "HEAD"]), "diff")

    def test_a_real_push_is_refused_and_does_nothing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
        git = Git(tmp)
        (tmp / "a.txt").write_text("one\n")
        git.commit_all("one")
        with self.assertRaises(GitRefused):
            git._run(["push", "origin", "main"])


class SeamPrefixTests(unittest.TestCase):
    """A seam prefix is caught however the path is written."""

    def test_a_dot_slash_prefixed_seam_is_caught(self) -> None:
        cfg = Config({
            "project": {"name": "t", "state_dir": "/tmp/openfarm-seam-state"},
            "adapters": {"languages": ["rust"]},
            "policy": {"categories": ["pinned-bugfix"]},
            "eligibility": {"refuse_touches_prefixes": ["seam:"]},
        }, Path("/tmp/none"))
        for touch in ("seam:gestures", "./seam:gestures", "/seam:gestures"):
            card = Card("CARD-1", "T", done_text="returns `Err(Full)`.",
                        touches=[touch],
                        fields={"category": "pinned-bugfix",
                                "tests": [{"name": "a", "file": "tests/a.rs",
                                           "asserts": "Err(Full)"}]})
            result = check_eligibility(card, cfg)
            self.assertIn("seam-prefix",
                          [f["rule"] for f in result.failures], touch)


if __name__ == "__main__":
    unittest.main()
