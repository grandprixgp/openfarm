"""Regressions for the bugs a review found in the first implementation.

Each case here exists because the code was wrong in a way that a gate would not have
noticed. They are the cheapest possible insurance against those bugs coming back.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_the_test_writer_may_not_create_a_production_file(self) -> None:
        """Every line of a new file is an "addition", so a whole-file edit would pass.

        A production file that does not exist yet is refused, because the only
        addition the test writer may make to production code is a stub on a file that
        already exists.
        """
        edit = Edit("whole_file", "src/brand_new.rs", content="pub fn new() {}")
        report = apply_edits(self.wt, [edit], ["src/**"], [], [],
                             additions_only={"src/**"},
                             production_paths={"src/**"})
        self.assertIn("additions-only-new-file",
                      [entry["code"] for entry in report.refused])
        self.assertFalse((self.wt / "src" / "brand_new.rs").exists())

    def test_a_directory_target_is_refused_not_a_crash(self) -> None:
        edit = Edit("whole_file", "src", content="x")
        report = apply_edits(self.wt, [edit], ["src"], [], [])
        self.assertIn("not-a-file", [entry["code"] for entry in report.refused])

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

    def test_a_path_named_push_is_not_a_subcommand(self) -> None:
        """A repository may hold a file called `push`. Nothing after `--` is one."""
        self.assertEqual(_check_subcommand(["diff", "HEAD", "--", "push"]), "diff")
        self.assertEqual(
            _check_subcommand(["ls-files", "--others", "--", "fetch"]), "ls-files")
        self.assertEqual(_check_subcommand(["add", "-A", "remote"]), "add")

    def test_a_commit_message_named_push_is_not_a_subcommand(self) -> None:
        self.assertEqual(
            _check_subcommand(["commit", "-q", "-m", "push the thing"]), "commit")
        self.assertEqual(_check_subcommand(["commit", "-m", "fetch"]), "commit")

    def test_every_argv_openfarm_builds_is_accepted(self) -> None:
        """The allowlist must not refuse the calls the core actually makes."""
        for argv in (
            ["add", "-A"],
            ["commit", "-q", "-m", "CARD-1: make the frozen tests pass"],
            ["reset", "--hard", "HEAD"],
            ["clean", "-fd"],
            ["checkout", "--detach", "openfarm/CARD-1-x"],
            ["branch", "--force", "openfarm/CARD-1-x", "abc123"],
            ["worktree", "add", "--force", "/tmp/wt", "openfarm/CARD-1-x"],
            ["worktree", "remove", "--force", "/tmp/wt"],
            ["worktree", "list", "--porcelain"],
            ["merge", "--no-commit", "--no-ff", "refs/heads/main"],
            ["merge", "--abort"],
            ["diff", "HEAD", "--", "src/a.rs"],
            ["diff", "--name-only", "HEAD"],
            ["diff", "--cached", "--quiet"],
            ["show", "abc123:src/a.rs"],
            ["ls-files", "--others", "--exclude-standard"],
            ["rev-parse", "--verify", "--quiet", "refs/heads/main"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
            ["rev-list", "--count", "main..HEAD"],
            ["for-each-ref"],
            ["status", "--porcelain"],
            ["merge-base", "HEAD", "main"],
            ["update-ref", "refs/heads/main", "abc123"],
        ):
            _check_subcommand(argv)


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


class CardFileRoundTripTests(unittest.TestCase):
    """A card the adapter writes must read back, or the card is unrecoverable."""

    def setUp(self) -> None:
        from openfarm_core.adapters.files import parse_card_file, render_card_file
        self.render = render_card_file
        self.parse = parse_card_file

    def _round_trip(self, header: dict) -> dict:
        text = self.render(header, "the body")
        parsed, _body = self.parse(text)
        return parsed

    def test_a_note_with_a_diff_round_trips(self) -> None:
        """A dossier's diff has `+++ b/path` lines, which must not end the header."""
        note = "MECH-1: in review\n\n```diff\n--- a/f.rs\n+++ b/f.rs\n-old\n+new\n```\n"
        parsed = self._round_trip({"id": "C", "note": note})
        self.assertEqual(parsed["note"], note)

    def test_a_note_with_backslashes_and_quotes_round_trips(self) -> None:
        note = 'a """quoted""" line\npath C:\\temp\\x\na tab\there\n'
        parsed = self._round_trip({"id": "C", "note": note})
        self.assertEqual(parsed["note"], note)

    def test_structured_test_fields_survive(self) -> None:
        parsed = self._round_trip({
            "id": "C",
            "tests": [{"name": "a_b", "file": "tests/a.rs", "asserts": "== 1"}],
        })
        self.assertEqual(parsed["tests"][0]["file"], "tests/a.rs")
        self.assertEqual(parsed["tests"][0]["asserts"], "== 1")

    def test_a_write_that_would_not_read_back_is_refused(self) -> None:
        """A note line that is only `+++` ends the header, so the write is refused.

        This is not hypothetical: a card's note carries the Markdown dossier, and a
        card written in a form its own parser cannot read is unrecoverable through
        the adapter. Nothing is written and the card stays as it was.
        """
        from openfarm_core.adapters.files import FileCardSource
        from openfarm_core.errors import Invalid
        import subprocess, tempfile
        from pathlib import Path as P

        root = P(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        (root / "cards").mkdir()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "one"],
                       cwd=str(root), check=True)
        source = FileCardSource(root, "cards", worktree=root)

        with self.assertRaises(Invalid) as caught:
            source._write("CARD-1", {"id": "CARD-1", "note": "a\n+++\nb"},
                          "body", "CARD-1: add")
        self.assertEqual(caught.exception.code, "unwritable-card")
        self.assertFalse((root / "cards" / "CARD-1.md").exists(),
                         "nothing may be written")

    def test_a_perfectly_ordinary_card_still_writes(self) -> None:
        from openfarm_core.adapters.files import FileCardSource
        import subprocess, tempfile
        from pathlib import Path as P

        root = P(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        (root / "cards").mkdir()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "one"],
                       cwd=str(root), check=True)
        source = FileCardSource(root, "cards", worktree=root)
        source._write("CARD-1", {"id": "CARD-1", "state": "ready"},
                      "A body.", "CARD-1: add")
        self.assertTrue((root / "cards" / "CARD-1.md").exists())


class PreExistingPatternTests(unittest.TestCase):
    """A pattern the file already had is flagged, not blamed on the card."""

    def setUp(self) -> None:
        self.cfg = Config({
            "project": {"name": "t", "state_dir": "/tmp/openfarm-pattern-state"},
            "adapters": {"languages": ["rust"]},
            "policy": {"categories": ["mechanical"]},
        }, Path("/tmp/none"))

    def test_a_moved_unwrap_is_flagged_not_banned(self) -> None:
        """The file's count stays at one, so the card did not introduce it."""
        from openfarm_core.gates.checks import banned_patterns_rust
        lines = [{"path": "src/a.rs", "line": 4,
                  "text": "        self.chain().next_back().unwrap()"}]
        gate = banned_patterns_rust(
            lines, self.cfg, {"src/a.rs"},
            base_counts={"src/a.rs": {"production-unwrap": 1}},
            head_counts={"src/a.rs": {"production-unwrap": 1}})
        self.assertEqual(gate.result, "pass", gate.detail)
        self.assertEqual(len(gate.evidence["pre_existing"]), 1)
        self.assertIn("already present at base", gate.detail)

    def test_a_file_that_gains_a_fourth_unwrap_is_banned(self) -> None:
        """Three at base, four at head: the card added one, whoever wrote the rest."""
        from openfarm_core.gates.checks import banned_patterns_rust
        lines = [{"path": "src/a.rs", "line": 40,
                  "text": "    let v = get().unwrap();"}]
        gate = banned_patterns_rust(
            lines, self.cfg, {"src/a.rs"},
            base_counts={"src/a.rs": {"production-unwrap": 3}},
            head_counts={"src/a.rs": {"production-unwrap": 4}})
        self.assertEqual(gate.result, "fail")
        self.assertEqual(len(gate.evidence["banned"]), 1)
        self.assertEqual(gate.evidence["banned"][0]["added"], 1)

    def test_a_new_unwrap_with_no_base_count_is_banned(self) -> None:
        from openfarm_core.gates.checks import banned_patterns_rust
        lines = [{"path": "src/a.rs", "line": 1,
                  "text": "    let x = y.unwrap();"}]
        gate = banned_patterns_rust(
            lines, self.cfg, {"src/a.rs"},
            base_counts={}, head_counts={"src/a.rs": {"production-unwrap": 1}})
        self.assertEqual(gate.result, "fail")

    def test_the_caller_s_counts_are_not_mutated(self) -> None:
        from openfarm_core.gates.checks import banned_patterns_rust
        base = {"src/a.rs": {"production-unwrap": 2}}
        head = {"src/a.rs": {"production-unwrap": 2}}
        lines = [{"path": "src/a.rs", "line": 2,
                  "text": "    let x = y.unwrap();"}]
        banned_patterns_rust(lines, self.cfg, {"src/a.rs"},
                             base_counts=base, head_counts=head)
        self.assertEqual(base, {"src/a.rs": {"production-unwrap": 2}},
                         "the base counts must survive the gate")


class V04ReviewFixRegressionTests(unittest.TestCase):
    """Regressions for M2, M8, M11, M12, H4, H6, L1, L2, and dossier accuracy."""

    def test_git_allowlist_blocks_global_config_flags_and_dangerous_config_keys(self) -> None:
        from openfarm_core.git import GitRefused, _check_subcommand

        for bad in (
            ["-c", "user.name=x", "status"],
            ["-calias.st=!sh", "status"],
            ["--config-env", "FOO=BAR", "status"],
            ["--config-env=FOO=BAR", "status"],
            ["--exec-path", "/tmp", "status"],
            ["--exec-path=/tmp", "status"],
            ["config", "--global", "user.name", "x"],
            ["config", "--system", "user.name", "x"],
            ["config", "alias.co", "checkout"],
            ["config", "Core.HooksPath", "/tmp/h"],
            ["config", "core.sshCommand", "evil"],
            ["config", "core.fsmonitor", "true"],
            ["config", "core.pager", "cat"],
            ["-C/tmp/repo", "push"],
        ):
            with self.assertRaises(GitRefused, msg=f"should refuse {bad!r}"):
                _check_subcommand(bad)

        self.assertEqual(_check_subcommand(["-C/tmp/repo", "status"]), "status")
        self.assertEqual(_check_subcommand(["config", "user.name"]), "config")

    def test_night_budget_tracks_micromanager_spend(self) -> None:
        from openfarm_core.budget import NightBudget

        night = NightBudget(1.00)
        night.note_spend(0.60)
        self.assertIsNone(night.exhausted())
        night.note_micromanager(0.45)
        self.assertAlmostEqual(night.total_spend(), 1.05, places=6)
        self.assertIsNotNone(night.exhausted())

    def test_roles_vary_seed_unpredictable_and_parse_plan_validations(self) -> None:
        from openfarm_core.cards import Card
        from openfarm_core.errors import Invalid
        from openfarm_core.roles import (Roles, build_implementer_brief,
                                         build_test_writer_brief, parse_plan,
                                         wants_planner)
        from openfarm_core.briefs import BriefBook
        from openfarm_core.packs.rust import RustPack
        from types import SimpleNamespace
        import tempfile, shutil

        cfg = Config({
            "project": {"name": "t", "state_dir": "/tmp/openfarm-seed-state"},
            "adapters": {"languages": ["rust"]},
            "policy": {"categories": ["pinned-bugfix"]},
            "model": {"implementer": {"model": "m", "temperature": 0.3,
                                      "vary_seed": True}},
        }, Path("/tmp/none"))

        class DummyBackend:
            def complete(self, **kwargs):
                return SimpleNamespace(content="ok", tokens_in=1, tokens_out=1, cost=0.0)

        seeds_a = [Roles(cfg, DummyBackend(), "C1")._settings("implementer")[2]
                   for _ in range(4)]
        seeds_b = [Roles(cfg, DummyBackend(), "C1")._settings("implementer")[2]
                   for _ in range(4)]
        self.assertNotEqual(seeds_a, seeds_b)
        self.assertEqual(
            Roles(cfg, DummyBackend(), "C1", rng_seed=99)._settings("implementer")[2],
            Roles(cfg, DummyBackend(), "C1", rng_seed=99)._settings("implementer")[2],
        )

        card = Card(
            id="C-1", title="t", state="ready",
            touches=["src/a.rs", "src/b.rs", "src/c.rs"],
            fields={
                "category": "pinned-bugfix",
                "tests": [
                    {"name": "t1", "file": "tests/a.rs"},
                    {"name": "t2", "file": "tests/a.rs"},
                ],
                "context_files": ["src/ctx.rs"],
            },
        )
        # JSON step containing the string CANNOT_SPLIT is not treated as CANNOT_SPLIT
        plan = parse_plan(
            '{"steps":[{"n":1,"does":"note CANNOT_SPLIT","tests":["t1"],"touches":["src/a.rs"]},'
            '{"n":2,"does":"second","tests":["t2"]}]}',
            card=card,
        )
        self.assertFalse(plan.cannot_split)
        self.assertEqual(plan.steps[0]["touches"], ["src/a.rs"])

        with self.assertRaises(Invalid):
            parse_plan(
                '{"steps":[{"n":1,"does":"a","tests":["unknown_test"]},'
                '{"n":2,"does":"b","tests":["t2"]}]}',
                card=card,
            )
        with self.assertRaises(Invalid):
            parse_plan(
                '{"steps":[{"n":1,"does":"a","tests":["t1"],"touches":["src/outside.rs"]},'
                '{"n":2,"does":"b","tests":["t2"]}]}',
                card=card,
            )

        self.assertFalse(wants_planner(card, "off"))
        self.assertTrue(wants_planner(card, "on"))
        self.assertTrue(wants_planner(card, "auto"))

        tmp = Path(tempfile.mkdtemp(prefix="openfarm-brief-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "src").mkdir()
        (tmp / "src" / "a.rs").write_text("pub fn a() {}\n")
        (tmp / "src" / "b.rs").write_text("pub fn b() {}\n")
        (tmp / "src" / "c.rs").write_text("pub fn c() {}\n")
        (tmp / "src" / "ctx.rs").write_text("pub fn helper() {}\n")
        book = BriefBook(tmp, [], [], allowed_extra_reads={"src/ctx.rs"})
        tw = build_test_writer_brief(card, book, RustPack(), guidance="use strict asserts",
                                     context_files=["src/ctx.rs"])
        self.assertIn("use strict asserts", tw.body)
        self.assertIn("pub fn helper()", tw.body)
        imp = build_implementer_brief(card, book, {}, guidance="fix guard",
                                      step_context="Step 1 done",
                                      context_files=["src/ctx.rs"])
        self.assertIn("fix guard", imp.body)
        self.assertIn("Step 1 done", imp.body)

    def test_dossier_uses_gate_attempt_test_maps_and_mutation_required_for(self) -> None:
        from openfarm_core.cards import Card
        from openfarm_core.dossier import build_dossier, render_markdown
        from openfarm_core.gates.checks import GateResult
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="openfarm-dos-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        g1 = GateResult("suite", "fail", "red")
        g1.attempt = 1
        g2 = GateResult("suite", "pass", "green")
        g2.attempt = 2
        cfg = Config({
            "project": {"name": "t", "state_dir": str(tmp / "state")},
            "adapters": {"languages": ["rust"]},
            "policy": {"categories": ["pinned-bugfix", "test-backfill"]},
            "gates": {"mutation": "report", "mutation_required_for": ["test-backfill"]},
        }, Path("/tmp/none"))
        card = Card(
            id="C-DOS", title="t", state="ready",
            fields={"category": "test-backfill",
                    "tests": [{"name": "t_one", "file": "tests/a.rs"},
                              {"name": "t_two", "file": "tests/a.rs"}]},
        )
        run = SimpleNamespace(
            card=card, cfg=cfg, pack=SimpleNamespace(notes=[]),
            card_layout=tmp, run_id="r1", attempt=2, stop_code=None,
            gates=[g1, g2],
            counter_summary={"written": 1, "dropped_uncompiled": 0,
                             "passed": 0, "failed": 0, "not_run": 1},
            mutation_summary={"killed": 1, "survived": 0, "total": 1, "capped": False},
            checker_restatement=None,
            checker_adjudication={"verdict": "dismiss_false_reject",
                                  "rationale": "out of scope"},
            micromanager_summary={"mode": "active", "calls": 2, "spend": 0.05,
                                  "false_rejects_dismissed": 1,
                                  "reviewer_brief": "clean fix"},
            existing_tests_changed=[], unformatted=[], catch_up_result="skipped",
            known_failing_excluded=[], current_diff="",
            frozen_hash="abcdef1234567890",
            failed_on_base_by_test={"t_one": "ok"},
            head_status_by_test={"t_one": "ok"},
            budget=SimpleNamespace(implementer_attempts=2, test_writer_attempts=1),
            planner_used=False,
        )
        dos = build_dossier(run, "in-review")
        self.assertEqual([g["attempt"] for g in dos["2_gates"]], [1, 2])
        self.assertEqual(dos["4_red_green"][0]["status_at_head"], "pass")
        self.assertEqual(dos["4_red_green"][1]["status_at_head"], "unknown")
        self.assertNotIn("mutation is report-only for this category",
                         dos["8_not_checked"])
        md = render_markdown(dos)
        self.assertIn("1 not-run", md)
        self.assertIn("dismiss_false_reject", md)
        self.assertIn("clean fix", md)

    def test_cli_normalise_argv_and_json_errors_and_release_order(self) -> None:
        from openfarm_core.cli import _normalise_argv, cmd_release, main
        from openfarm_core.errors import Refused, UsageError
        import io, contextlib, tempfile, shutil

        self.assertEqual(
            _normalise_argv(["check", "C-1", "--", "--repo", "other"]),
            ["check", "C-1", "--", "--repo", "other"],
        )
        with self.assertRaises(UsageError):
            _normalise_argv(["status", "--repo"])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["status", "--repo", "--json"])
        self.assertEqual(code, 2)
        err = json.loads(buf.getvalue())
        self.assertIn("error", err)
        self.assertEqual(err["kind"], "usage error")

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code2 = main(["--config", "/tmp/does-not-exist-openfarm.toml",
                          "status", "--json"])
        self.assertEqual(code2, 2)
        self.assertEqual(json.loads(buf2.getvalue())["kind"], "config error")

        tmp = Path(tempfile.mkdtemp(prefix="openfarm-rel-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        card_state = tmp / "cards" / "CARD-1" / "run-1"
        card_state.mkdir(parents=True)
        (card_state / "note.txt").write_text("keep me")

        class RefuseSource:
            def prepare(self):
                pass

            def release(self, card_id, reason="", stop_code=""):
                raise Refused("cannot-release", "still active")

        fake_ctx = SimpleNamespace(
            cards=lambda: RefuseSource(),
            state=SimpleNamespace(cards=tmp / "cards"),
            emit=lambda payload: None,
        )
        rc = cmd_release(fake_ctx, SimpleNamespace(card="CARD-1", reason="test"))
        self.assertEqual(rc, 1)
        self.assertTrue((card_state / "note.txt").exists(),
                        "must not archive state when source.release is refused")


if __name__ == "__main__":
    unittest.main()
