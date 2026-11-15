"""Tests for OpenFarm v0.4 pack, edit, brief, eligibility, and gate updates."""

from __future__ import annotations

import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from openfarm_core.briefs import (
    DATA_FENCE,
    DATA_FENCE_END,
    BriefBook,
    fence,
    implementer_brief,
    test_writer_brief,
)
from openfarm_core.cards import Card, TestRef
from openfarm_core.config import Config
from openfarm_core.edits import Edit, apply_edits
from openfarm_core.errors import Refused
from openfarm_core.gates.checks import (
    FAIL,
    INFRA,
    PASS,
    ignored_or_filtered,
    mutation_gate,
    suite_green,
)
from openfarm_core.gates.eligibility import (
    KNOWN_CATEGORIES,
    _units_of,
    check_eligibility,
    derive_cargo_unit,
)
from openfarm_core.packs.base import Pack, UnsupportedPack, pack_for
from openfarm_core.packs.rust import RustPack


def _cfg(**overrides) -> Config:
    data = {
        "project": {"name": "toy", "state_dir": "/tmp/openfarm-v04-state"},
        "adapters": {"cards": "files:cards/", "languages": ["rust"]},
        "policy": {
            "never_touch": ["src/auth/**"],
            "forbidden_files": ["**/Cargo.toml"],
            "categories": list(KNOWN_CATEGORIES),
        },
        "eligibility": {
            "max_files": 6,
            "max_units": 1,
            "require_reference": ["pattern", "small-feature"],
            "refuse_touches_prefixes": ["seam:"],
        },
    }
    for k, v in overrides.items():
        data.setdefault(k, {}).update(v)
    return Config(data, Path("/tmp/toy-v04-repo"))


def _card(**overrides) -> Card:
    payload = dict(
        id="CARD-V04",
        title="Add v0.4 feature",
        category="feature",
        touches=["src/lib.rs"],
        tests=[TestRef("works", "tests/it.rs", "returns Ok(42)")],
        done_text="returns `Ok(42)` on valid input.",
        reference="",
        new_symbols=False,
        fields={},
    )
    payload.update(overrides)
    fields = {
        "category": payload["category"],
        "tests": [t.as_dict() for t in payload["tests"]],
        "unit": "toy",
    }
    if payload["reference"]:
        fields["reference"] = payload["reference"]
    if payload["new_symbols"]:
        fields["new_symbols"] = True
    fields.update(payload["fields"])
    return Card(
        id=payload["id"],
        title=payload["title"],
        done_text=payload["done_text"],
        touches=payload["touches"],
        fields=fields,
    )


class PackForCmdTests(unittest.TestCase):
    def test_falls_back_gracefully_when_cmd_pack_not_wired(self) -> None:
        pack = pack_for("rust", spec="cmd:python3 fake_pack.py")
        self.assertIsInstance(pack, UnsupportedPack)
        self.assertIn("cmd:python3 fake_pack.py", pack.name)

    def test_uses_cmd_pack_when_available_in_protocol(self) -> None:
        class FakeCmdPack(Pack):
            def __init__(self, spec: str) -> None:
                self.name = f"fake:{spec}"

        import openfarm_core.protocol as proto

        with patch.object(proto, "CmdPack", FakeCmdPack, create=True):
            pack = pack_for("python", spec="cmd:./custom-pack")
        self.assertIsInstance(pack, FakeCmdPack)
        self.assertEqual(pack.name, "fake:cmd:./custom-pack")


class RustPackC1ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()

    def test_real_cargo_split_stdout_and_stderr(self) -> None:
        stderr = (
            "   Compiling toy v0.1.0 (/tmp/toy)\n"
            "    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.20s\n"
            "     Running unittests src/lib.rs (/tmp/toy/target/debug/deps/toy-111)\n"
            "     Running tests/it.rs (/tmp/toy/target/debug/deps/it-222)\n"
        )
        stdout = (
            "running 1 test\n"
            "test unit_a ... ok\n\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n\n"
            "running 2 tests\n"
            "test it_ok - should panic ... ok\n"
            'test it_fail - should panic(expected = "boom") ... FAILED\n\n'
            "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"
        )
        parsed = self.pack.parse_tests(stdout, stderr)
        self.assertEqual(parsed["missing_footers"], [])
        self.assertEqual(len(parsed["footers"]), 2)
        self.assertEqual(parsed["footers"][0]["binary"], "src/lib.rs")
        self.assertEqual(parsed["footers"][0]["status"], "ok")
        self.assertEqual(parsed["footers"][1]["binary"], "tests/it.rs")
        self.assertEqual(parsed["footers"][1]["status"], "FAILED")
        by_name = {r["name"]: r for r in parsed["results"]}
        self.assertEqual(by_name["unit_a"]["binary"], "src/lib.rs")
        self.assertEqual(by_name["it_ok"]["binary"], "tests/it.rs")
        self.assertEqual(by_name["it_ok"]["status"], "ok")
        self.assertEqual(by_name["it_fail"]["binary"], "tests/it.rs")
        self.assertEqual(by_name["it_fail"]["status"], "FAILED")

    def test_missing_footer_detected_from_stderr_headers(self) -> None:
        stderr = (
            "     Running unittests src/lib.rs (/tmp/toy-111)\n"
            "     Running tests/crash.rs (/tmp/crash-222)\n"
        )
        stdout = (
            "running 1 test\n"
            "test unit_a ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
            "running 1 test\n"
            "test before_crash ... ok\n"
        )
        parsed = self.pack.parse_tests(stdout, stderr)
        self.assertEqual(parsed["missing_footers"], ["tests/crash.rs"])
        by_name = {r["name"]: r for r in parsed["results"]}
        self.assertEqual(by_name["before_crash"]["binary"], "tests/crash.rs")


class RustPackM5M6H2L7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = RustPack()
        self.tmp = Path(tempfile.mkdtemp())

    def test_classify_paths_normalises_preserving_keys(self) -> None:
        paths = ["./src/lib.rs", "crates/a/src/../tests/it.rs", r"crates\b\src\mod.rs"]
        res = self.pack.classify_paths(paths)
        self.assertEqual(res["./src/lib.rs"], "production")
        self.assertEqual(res["crates/a/src/../tests/it.rs"], "test")
        self.assertEqual(res[r"crates\b\src\mod.rs"], "production")

    def test_append_stub_validates_panicking_body_and_braces(self) -> None:
        f = self.tmp / "lib.rs"
        f.write_text("pub fn a() {}\n")
        with self.assertRaises(Refused) as ctx1:
            self.pack.append_stub(f, "t", "pub fn stub() {}")
        self.assertEqual(ctx1.exception.code, "non-panicking-stub")

        with self.assertRaises(Refused) as ctx2:
            self.pack.append_stub(f, "t", "// todo!()\npub fn stub() {}")
        self.assertEqual(ctx2.exception.code, "non-panicking-stub")

        with self.assertRaises(Refused) as ctx3:
            self.pack.append_stub(f, "t", "pub fn stub() { todo!()")
        self.assertEqual(ctx3.exception.code, "non-panicking-stub")

        self.pack.append_stub(f, "t", 'pub fn stub() -> &\'static str { let _ = "}"; unimplemented!() }')
        self.assertIn("unimplemented!()", f.read_text())

    def test_patterns_ignores_comments_and_counts_multiple_per_line(self) -> None:
        lines = [
            {"path": "src/a.rs", "line": 1, "text": "    // x.unwrap() todo!()"},
            {"path": "src/a.rs", "line": 2, "text": '    let url = "https://example.com"; // y.unwrap()'},
            {"path": "src/a.rs", "line": 3, "text": "    let z = a.unwrap() + b.unwrap(); // c.unwrap()"},
            {"path": "src/a.rs", "line": 4, "text": "    sleep(1); sleep(2); // sleep(3);"},
        ]
        found = self.pack.patterns(lines, ["sleep("], {"src/a.rs"})
        unwrap_hits = [b for b in found["banned"] if b["pattern"] == "production-unwrap"]
        self.assertEqual(len(unwrap_hits), 2)
        self.assertEqual(len(found["flagged"]), 2)

    def test_signatures_strips_fn_bodies_and_extracts_rich_types(self) -> None:
        rs = self.tmp / "rich.rs"
        rs.write_text(
            "pub fn secret() -> u32 { 42 }\n\n"
            "pub fn multi(\n"
            "    x: i32, // { fake brace in comment\n"
            ") -> i32 {\n"
            "    fn hidden_inner() { 99 }\n"
            "    x + 1\n"
            "}\n\n"
            "pub struct Config {\n"
            "    pub port: u16,\n"
            "    host: String,\n"
            "}\n\n"
            "pub enum Mode {\n"
            "    Fast,\n"
            "    Custom { threads: usize },\n"
            "}\n\n"
            "pub trait Store {\n"
            "    type Item;\n"
            "    fn get(&self, id: u64) -> Option<Self::Item>;\n"
            "    fn is_empty(&self) -> bool {\n"
            "        let secret_default = 777;\n"
            "        secret_default == 0\n"
            "    }\n"
            "}\n\n"
            "impl Store for Config {\n"
            "    type Item = String;\n"
            "    fn get(&self, _id: u64) -> Option<Self::Item> {\n"
            "        Some(self.host.clone())\n"
            "    }\n"
            "}\n"
        )
        sigs = "\n\n".join(self.pack.signatures(rs)["signatures"])
        self.assertIn("pub fn secret() -> u32 { ... }", sigs)
        self.assertNotIn("42", sigs)
        self.assertNotIn("99", sigs)
        self.assertNotIn("hidden_inner", sigs)
        self.assertNotIn("777", sigs)
        self.assertNotIn("self.host.clone()", sigs)
        self.assertIn("pub port: u16,", sigs)
        self.assertIn("Custom { threads: usize },", sigs)
        self.assertIn("type Item;", sigs)
        self.assertIn("fn get(&self, id: u64) -> Option<Self::Item>;", sigs)
        self.assertIn("fn is_empty(&self) -> bool { ... }", sigs)
        self.assertIn("impl Store for Config {", sigs)

    def test_error_signature_distinguishes_test_failures_and_normalises_noise(self) -> None:
        err1 = (
            "test alpha ... FAILED\n"
            "thread 'alpha' panicked at /tmp/wt-1/tests/it.rs:12:5:\n"
            "assertion `left == right` failed at 0x7fff1234 in 0.01s\n"
        )
        err2 = (
            "test alpha ... FAILED\n"
            "thread 'alpha' panicked at /tmp/wt-9/tests/it.rs:88:19:\n"
            "assertion `left == right` failed at 0xdeadbeef in 1.25s\n"
        )
        err3 = (
            "test beta ... FAILED\n"
            "thread 'beta' panicked at /tmp/wt-1/tests/it.rs:12:5:\n"
            "assertion `left == right` failed at 0x7fff1234 in 0.01s\n"
        )
        sig1 = self.pack.error_signature(err1)["signature"]
        sig2 = self.pack.error_signature(err2)["signature"]
        sig3 = self.pack.error_signature(err3)["signature"]
        self.assertEqual(sig1, sig2)
        self.assertNotEqual(sig1, sig3)


class EditsV04Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.wt = self.tmp / "repo"
        (self.wt / "src" / "auth").mkdir(parents=True)
        (self.wt / ".openfarm").mkdir(parents=True)
        (self.wt / "src" / "a.rs").write_text("one\ntwo\n")
        (self.wt / "src" / "auth" / "secret.rs").write_text("secret\n")
        (self.wt / ".openfarm" / "config.toml").write_text("[project]\n")

    def test_symlinks_to_forbidden_and_never_touch_are_refused(self) -> None:
        (self.wt / "src" / "link_cfg.rs").symlink_to(self.wt / ".openfarm" / "config.toml")
        (self.wt / "src" / "link_auth.rs").symlink_to(self.wt / "src" / "auth" / "secret.rs")
        (self.wt / "src" / "link_dir").symlink_to(self.wt / "src" / "auth")

        r1 = apply_edits(self.wt, [Edit("whole_file", "src/link_cfg.rs", content="x")],
                         ["src/**"], ["src/auth/**"], [".openfarm/**"])
        self.assertEqual(r1.refused[0]["code"], "forbidden-file")

        r2 = apply_edits(self.wt, [Edit("whole_file", "src/link_auth.rs", content="x")],
                         ["src/**"], ["src/auth/**"], [".openfarm/**"])
        self.assertEqual(r2.refused[0]["code"], "never-touch")

        r3 = apply_edits(self.wt, [Edit("whole_file", "src/link_dir/new.rs", content="x")],
                         ["src/**"], ["src/auth/**"], [".openfarm/**"])
        self.assertEqual(r3.refused[0]["code"], "never-touch")

        self.assertEqual((self.wt / ".openfarm" / "config.toml").read_text(), "[project]\n")
        self.assertEqual((self.wt / "src" / "auth" / "secret.rs").read_text(), "secret\n")

    def test_atomic_batch_rolls_back_all_edits_on_failure(self) -> None:
        edits = [
            Edit("search_replace", "src/a.rs", search="one", replace="ONE"),
            Edit("whole_file", "src/new_dir/b.rs", content="brand new\n"),
            Edit("search_replace", "src/a.rs", search="MISSING", replace="X"),
        ]
        report = apply_edits(self.wt, edits, ["src/**"], [], [])
        self.assertFalse(report.ok)
        self.assertEqual((self.wt / "src" / "a.rs").read_text(), "one\ntwo\n")
        self.assertFalse((self.wt / "src" / "new_dir").exists())
        self.assertEqual(report.applied, [])

    def test_atomic_batch_with_append_stub_rolls_back_on_failure(self) -> None:
        edits = [
            Edit("append_stub", "src/a.rs", content="pub fn s() { todo!() }", test_name="t"),
            Edit("search_replace", "src/a.rs", search="NO_MATCH", replace="x"),
        ]
        report = apply_edits(
            self.wt, edits, ["src/**"], [], [],
            allow_stubs=True,
            stub_inserter=lambda p, _n, b: p.write_text(p.read_text() + b + "\n"),
        )
        self.assertFalse(report.ok)
        self.assertEqual((self.wt / "src" / "a.rs").read_text(), "one\ntwo\n")
        self.assertEqual(report.stubs, [])

    def test_multi_edit_same_file_sees_prior_edit(self) -> None:
        edits = [
            Edit("search_replace", "src/a.rs", search="one", replace="step1"),
            Edit("search_replace", "src/a.rs", search="step1\ntwo", replace="step1\nstep2"),
        ]
        report = apply_edits(self.wt, edits, ["src/**"], [], [])
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual((self.wt / "src" / "a.rs").read_text(), "step1\nstep2\n")


class BriefsV04Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.wt = self.tmp / "repo"
        (self.wt / "src" / "auth").mkdir(parents=True)
        (self.wt / "tests").mkdir(parents=True)
        (self.wt / ".openfarm").mkdir(parents=True)
        (self.wt / "src" / "lib.rs").write_text("pub fn secret_impl() -> u32 { 42 }\n")
        (self.wt / "src" / "ctx.rs").write_text("pub struct Ctx { pub id: u32 }\nfn priv_fn() -> u32 { 7 }\n")
        (self.wt / "src" / "auth" / "s.rs").write_text("pub fn auth_secret() {}\n")
        (self.wt / "tests" / "it.rs").write_text("#[test]\nfn t1() {}\n")
        (self.wt / ".openfarm" / "conventions.md").write_text("Always use assert_eq!\n")
        (self.wt / ".openfarm" / "config.toml").write_text("secret_key = 'xyz'\n")

    def test_fence_strips_ansi_and_preserves_text_after_unterminated_osc(self) -> None:
        raw = f"hi \x1b[31mred\x1b[0m \x1b]0;title\x07 \x1b]8;;http://x\nafter_osc {DATA_FENCE_END}"
        fenced = fence(raw, "note")
        self.assertNotIn("\x1b", fenced)
        self.assertIn("red", fenced)
        self.assertIn("after_osc", fenced)
        self.assertEqual(fenced.count(DATA_FENCE), 1)
        self.assertEqual(fenced.count(DATA_FENCE_END), 1)

    def test_briefbook_allowed_extra_reads_and_symlink_containment(self) -> None:
        book = BriefBook(
            self.wt,
            never_touch=["src/auth/**", "src/auth_never.md"],
            forbidden=[".openfarm/**"],
            allowed_extra_reads=[".openfarm/conventions.md", "src/auth_never.md"],
        )
        self.assertEqual(book.guarded_read(".openfarm/conventions.md"), "Always use assert_eq!\n")
        with self.assertRaises(PermissionError):
            book.guarded_read(".openfarm/config.toml")
        (self.wt / "src" / "auth_never.md").write_text("nope")
        with self.assertRaises(PermissionError):
            book.guarded_read("src/auth_never.md")

        (self.wt / "src" / "sym.rs").symlink_to(self.wt / ".openfarm" / "config.toml")
        self.assertTrue(book.is_hidden("src/sym.rs"))

    def test_test_writer_brief_no_touches_fallback_and_type_changes(self) -> None:
        pack = RustPack()
        book = BriefBook(self.wt, ["src/auth/**"], [".openfarm/**"])
        card = _card(
            touches=["src/lib.rs"],
            tests=[TestRef("t1", "", "assert_eq!(1, 1)")],
            fields={"context_files": ["src/ctx.rs", "src/auth/s.rs"]},
        )
        brief = test_writer_brief(card, book, pack, guidance="focus on boundary").body
        self.assertNotIn("secret_impl() -> u32 { 42 }", brief)
        self.assertIn("pub fn secret_impl() -> u32 { ... }", brief)
        self.assertIn("pub struct Ctx { pub id: u32 }", brief)
        self.assertNotIn("{ 7 }", brief)
        self.assertNotIn("auth_secret", brief)
        self.assertIn("focus on boundary", brief)

        card_tc = _card(touches=["src/lib.rs"], fields={"type_changes": True})
        brief_tc = test_writer_brief(card_tc, book, pack).body
        self.assertIn("Base contract in touches (no new implementation; type declarations only)", brief_tc)
        self.assertIn("pub fn secret_impl() -> u32 { 42 }", brief_tc)

    def test_implementer_brief_attempt2_includes_current_files_and_context(self) -> None:
        book = BriefBook(self.wt, ["src/auth/**"], [".openfarm/**"])
        card = _card(touches=["src/lib.rs"], fields={"context_files": ["src/ctx.rs"]})
        brief = implementer_brief(
            card, book, {"tests/it.rs": "#[test]\nfn t1() {}\n"},
            attempt=2, diff="--- a/src/lib.rs", failure="error[E0308]",
            guidance="check return type", step_context="step 1 done",
        ).body
        self.assertIn("Current files in touches:", brief)
        self.assertIn("pub fn secret_impl() -> u32 { 42 }", brief)
        self.assertIn("The frozen tests (you must not edit these):", brief)
        self.assertIn("Read-only context files (do not edit):", brief)
        self.assertIn("pub struct Ctx", brief)
        self.assertIn("check return type", brief)
        self.assertIn("step 1 done", brief)


class EligibilityV04Tests(unittest.TestCase):
    def test_categories_frontier_type_changes_and_context_limits(self) -> None:
        cfg = _cfg()
        for cat in ("feature", "refactor", "integration", "port"):
            c = _card(category=cat, new_symbols=True)
            self.assertTrue(check_eligibility(c, cfg).ok, cat)

        bad_fe = _card()
        bad_fe.frontier_estimate_valid = False
        self.assertIn("frontier-estimate", [f["rule"] for f in check_eligibility(bad_fe, cfg).failures])

        too_many_ctx = _card(fields={"context_files": [f"src/c{i}.rs" for i in range(9)]})
        self.assertIn("max-context-files", [f["rule"] for f in check_eligibility(too_many_ctx, cfg).failures])

        cfg_no_tc = copy.deepcopy(cfg)
        cfg_no_tc.allow_type_changes = False
        c_tc = _card(fields={"type_changes": True})
        self.assertIn("type-changes", [f["rule"] for f in check_eligibility(c_tc, cfg_no_tc).failures])

    def test_multi_file_new_symbols_allowed_with_type_changes_or_scaffold(self) -> None:
        cfg = _cfg()
        c_multi = _card(touches=["src/a.rs", "src/b.rs"], new_symbols=True)
        self.assertIn("new-symbols-touches", [f["rule"] for f in check_eligibility(c_multi, cfg).failures])

        c_tc = _card(touches=["src/a.rs", "src/b.rs"], new_symbols=True, fields={"type_changes": True})
        self.assertTrue(check_eligibility(c_tc, cfg).ok)

        cfg_mm = _cfg()
        cfg_mm.micromanager_adapter = "claude-acp"
        cfg_mm.micromanager = types.SimpleNamespace(scaffold_contracts=True)
        self.assertTrue(check_eligibility(c_multi, cfg_mm).ok)

    def test_path_safety_on_all_fields_including_symlinks(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "src" / "auth").mkdir(parents=True)
        (tmp / "tests").mkdir(parents=True)
        (tmp / "src" / "auth" / "s.rs").write_text("x")
        (tmp / "tests" / "link.rs").symlink_to(tmp / "src" / "auth" / "s.rs")
        cfg = _cfg()
        card = _card(
            tests=[TestRef("t", "tests/link.rs", "ok")],
            fields={
                "test_edits": ["../outside.rs"],
                "context_files": ["src/auth/s.rs"],
            },
        )
        rules = [f["rule"] for f in check_eligibility(card, cfg, worktree=tmp).failures]
        self.assertIn("never-touch", rules)
        self.assertIn("path-escape", rules)

    def test_m10_units_and_cargo_unit_derivation(self) -> None:
        self.assertEqual(_units_of(["crates/a/src/sub/mod.rs"]), {"crates/a"})
        self.assertEqual(_units_of(["src/sub/mod.rs"]), {"."})

        tmp = Path(tempfile.mkdtemp())
        (tmp / "crates" / "alpha" / "src" / "sub").mkdir(parents=True)
        (tmp / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/alpha"]\n')
        (tmp / "crates" / "alpha" / "Cargo.toml").write_text('[package]\nname = "alpha-pkg"\nversion = "0.1.0"\n')
        (tmp / "crates" / "alpha" / "src" / "sub" / "mod.rs").write_text("")

        self.assertEqual(_units_of(["crates/alpha/src/sub/mod.rs"], tmp), {"crates/alpha"})
        self.assertEqual(derive_cargo_unit(["crates/alpha/src/sub/mod.rs"], tmp), "alpha-pkg")

        cfg = _cfg()
        bad_unit = _card(touches=["crates/alpha/src/sub/mod.rs"], fields={"unit": "wrong-pkg"})
        rules = [f["rule"] for f in check_eligibility(bad_unit, cfg, worktree=tmp).failures]
        self.assertIn("unit-mismatch", rules)

        good_unit = _card(touches=["crates/alpha/src/sub/mod.rs"], fields={"unit": "alpha-pkg"})
        self.assertTrue(check_eligibility(good_unit, cfg, worktree=tmp).ok)


class ChecksV04Tests(unittest.TestCase):
    def test_suite_green_crash_signal_and_footer_mismatch(self) -> None:
        crashed = {"results": [], "footers": [], "missing_footers": ["tests/x.rs"], "compile_errors": []}
        self.assertEqual(suite_green(crashed, [], run_cls=FAIL, exit_code=-11).result, FAIL)
        self.assertEqual(suite_green(crashed, [], run_cls=FAIL, exit_code=-9).result, INFRA)
        self.assertEqual(suite_green(crashed, [], run_cls=INFRA, exit_code=-11).result, INFRA)

        unmatched_footer = {
            "results": [{"name": "ok_one", "status": "ok"}],
            "footers": [{"binary": "src/lib.rs", "status": "FAILED", "passed": 1, "failed": 1, "ignored": 0, "filtered": 0}],
            "missing_footers": [],
            "compile_errors": [],
        }
        self.assertEqual(suite_green(unmatched_footer, []).result, FAIL)

    def test_mutation_gate_required_for(self) -> None:
        parsed = {"killed": 1, "survived": 9, "total": 10}
        self.assertEqual(mutation_gate(parsed, 0.8, "report", "small-feature").result, PASS)
        self.assertEqual(
            mutation_gate(parsed, 0.8, "report", "small-feature", required_for=("test-backfill", "small-feature")).result,
            FAIL,
        )

    def test_ignored_or_filtered_handles_duplicate_names_across_binaries(self) -> None:
        parsed = {
            "results": [
                {"name": "dup_test", "binary": "src/lib.rs", "status": "ok"},
                {"name": "dup_test", "binary": "tests/it.rs", "status": "ignored"},
            ]
        }
        self.assertEqual(ignored_or_filtered(parsed, ["dup_test"]).result, FAIL)
        self.assertEqual(ignored_or_filtered(parsed, [("src/lib.rs", "dup_test")]).result, PASS)
        self.assertEqual(ignored_or_filtered(parsed, [("tests/it.rs", "dup_test")]).result, FAIL)


if __name__ == "__main__":
    unittest.main()
