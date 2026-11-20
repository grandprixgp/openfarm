"""The gates on their own, fed synthetic input."""

from __future__ import annotations

import unittest

from openfarm_core.gates.checks import (FAIL, INFRA, PASS, banned_patterns_rust,
                                       diff_within_touches, extra_gate_result,
                                       frozen_check, ignored_or_filtered,
                                       mutation_gate, paths_touched_by_extra_gate,
                                       revert_check, suite_green, test_loss)


class FakeConfig:
    never_touch = ["src/auth/**"]
    forbidden_files = ["**/Cargo.toml"]
    flagged_patterns = ["sleep("]


class DiffWithinTouchesTests(unittest.TestCase):
    def test_all_inside(self) -> None:
        gate = diff_within_touches(["src/a.rs", "tests/a.rs"],
                                   ["src/**", "tests/**"], FakeConfig())
        self.assertEqual(gate.result, PASS)

    def test_outside_touches(self) -> None:
        gate = diff_within_touches(["src/a.rs", "src/other.rs"], ["src/a.rs"],
                                   FakeConfig())
        self.assertEqual(gate.result, FAIL)
        self.assertEqual(gate.evidence["outside"][0]["path"], "src/other.rs")

    def test_never_touch_beats_a_touch_entry(self) -> None:
        gate = diff_within_touches(["src/auth/login.rs"],
                                   ["src/auth/login.rs"], FakeConfig())
        self.assertEqual(gate.result, FAIL)
        self.assertIn("never-touch", gate.evidence["outside"][0]["reason"])

    def test_forbidden_path(self) -> None:
        gate = diff_within_touches(["Cargo.toml"], ["Cargo.toml"], FakeConfig())
        self.assertEqual(gate.result, FAIL)


class BannedPatternTests(unittest.TestCase):
    def test_clean_lines_pass(self) -> None:
        gate = banned_patterns_rust(
            [{"path": "src/a.rs", "line": 1, "text": "let x = 1;"}],
            FakeConfig(), {"src/a.rs"})
        self.assertEqual(gate.result, PASS)

    def test_a_stub_left_in_is_a_fail(self) -> None:
        gate = banned_patterns_rust(
            [{"path": "src/a.rs", "line": 9, "text": "    todo!()"}],
            FakeConfig(), {"src/a.rs"})
        self.assertEqual(gate.result, FAIL)
        self.assertTrue(any(b["pattern"] == "leftover-stub"
                            for b in gate.evidence["banned"]))

    def test_flagged_is_reported_not_failed(self) -> None:
        gate = banned_patterns_rust(
            [{"path": "tests/a.rs", "line": 1, "text": "sleep(1);"}],
            FakeConfig(), set())
        self.assertEqual(gate.result, PASS)
        self.assertEqual(gate.evidence["flagged_count"], 1)

    def test_a_task_whose_plan_allows_the_network_may_add_it_and_nothing_else(self) -> None:
        from openfarm_core.gates.checks import banned_patterns
        from openfarm_core.packs.typescript import TypeScriptPack

        added = [{"path": "src/server.ts", "line": 1, "text": 'import { createServer } from "node:http";'},
                 {"path": "src/server.ts", "line": 2, "text": "eval(x);"}]
        head = {"src/server.ts": {"network-call": 1, "dynamic-exec": 1}}
        refused = banned_patterns(added, FakeConfig(), {"src/server.ts"}, base_counts={}, head_counts=head,
                                  pack=TypeScriptPack())
        self.assertEqual({b["pattern"] for b in refused.evidence["banned"]}, {"network-call", "dynamic-exec"})
        gate = banned_patterns(added, FakeConfig(), {"src/server.ts"}, base_counts={}, head_counts=head,
                               pack=TypeScriptPack(), allowed=frozenset({"network-call"}))
        self.assertEqual(gate.result, FAIL)
        self.assertEqual([b["pattern"] for b in gate.evidence["banned"]], ["dynamic-exec"])
        self.assertTrue(any(f.get("allowed_by_plan") for f in gate.evidence["flagged"]))  # the reviewer still sees it
        only = banned_patterns(added[:1], FakeConfig(), {"src/server.ts"}, base_counts={},
                               head_counts={"src/server.ts": {"network-call": 1}}, pack=TypeScriptPack(),
                               allowed=frozenset({"network-call"}))
        self.assertEqual(only.result, PASS)


class SuiteGateTests(unittest.TestCase):
    def parsed(self, results, missing=None):
        return {"results": results, "footers": [], "missing_footers": missing or []}

    def test_green(self) -> None:
        gate = suite_green(self.parsed([{"name": "a", "status": "ok"}]), [])
        self.assertEqual(gate.result, PASS)

    def test_a_known_red_test_is_left_out(self) -> None:
        gate = suite_green(
            self.parsed([{"name": "a", "status": "ok"},
                         {"name": "flaky", "status": "FAILED"}]),
            ["flaky"])
        self.assertEqual(gate.result, PASS)
        self.assertEqual(gate.evidence["known-red"], ["flaky"])

    def test_an_unnamed_red_test_fails(self) -> None:
        gate = suite_green(
            self.parsed([{"name": "broken", "status": "FAILED"}]), ["flaky"])
        self.assertEqual(gate.result, FAIL)
        self.assertIn("broken", gate.evidence["unexpected"])

    def test_a_missing_footer_is_infra_not_fail(self) -> None:
        gate = suite_green(
            self.parsed([{"name": "a", "status": "ok"}], missing=["tests/x.rs"]),
            [])
        self.assertEqual(gate.result, INFRA)


class PassAfterTests(unittest.TestCase):
    def test_all_named_pass(self) -> None:
        parsed = {"results": [{"name": "a", "status": "ok"},
                              {"name": "b", "status": "ok"}]}
        self.assertEqual(ignored_or_filtered(parsed, ["a", "b"]).result, PASS)

    def test_an_ignored_named_test_is_not_a_pass(self) -> None:
        parsed = {"results": [{"name": "a", "status": "ignored"}]}
        gate = ignored_or_filtered(parsed, ["a"])
        self.assertEqual(gate.result, FAIL)
        self.assertIn("ignored", gate.detail)

    def test_a_missing_named_test_is_not_a_pass(self) -> None:
        parsed = {"results": [{"name": "other", "status": "ok"}]}
        gate = ignored_or_filtered(parsed, ["a"])
        self.assertEqual(gate.result, FAIL)


class FrozenAndRevertTests(unittest.TestCase):
    def test_frozen_match(self) -> None:
        expected = {"tests/a.rs": "abc"}
        self.assertEqual(frozen_check(expected, {"tests/a.rs": "abc"}).result, PASS)

    def test_frozen_change_fails(self) -> None:
        gate = frozen_check({"tests/a.rs": "abc"}, {"tests/a.rs": "def"})
        self.assertEqual(gate.result, FAIL)

    def test_a_new_test_file_fails_the_freeze(self) -> None:
        gate = frozen_check({"tests/a.rs": "abc"},
                            {"tests/a.rs": "abc", "tests/b.rs": "xyz"})
        self.assertEqual(gate.result, FAIL)

    def test_revert_check(self) -> None:
        self.assertEqual(revert_check("abc", "abc").result, PASS)
        self.assertEqual(revert_check("abc", "def").result, FAIL)

    def test_a_refrozen_set_passes_when_fail_before_ran_again(self) -> None:
        self.assertEqual(revert_check("abc", "def", refrozen=True).result, PASS)


class NoTestLossTests(unittest.TestCase):
    def before(self):
        return {"tests": [{"name": "one"}, {"name": "two"}]}

    def test_none_lost(self) -> None:
        after = {"tests": [{"name": "one"}, {"name": "two"}, {"name": "three"}]}
        self.assertEqual(test_loss(self.before(), after).result, PASS)

    def test_a_deleted_test_is_lost(self) -> None:
        gate = test_loss(self.before(), {"tests": [{"name": "one"}]})
        self.assertEqual(gate.result, FAIL)
        self.assertEqual(gate.evidence["lost"], ["two"])

    def test_a_renamed_test_is_a_loss_even_at_equal_count(self) -> None:
        gate = test_loss(self.before(), {"tests": [{"name": "one"}, {"name": "other"}]})
        self.assertEqual(gate.result, FAIL)

    def test_known_failing_are_left_out_of_the_count(self) -> None:
        gate = test_loss(self.before(), {"tests": [{"name": "one"}]},
                         known_failing=["two"])
        self.assertEqual(gate.result, PASS)


class MutationTests(unittest.TestCase):
    def test_required_and_met(self) -> None:
        gate = mutation_gate({"killed": 9, "survived": 1, "total": 10},
                             0.8, "required", "pattern")
        self.assertEqual(gate.result, PASS)

    def test_required_and_missed(self) -> None:
        gate = mutation_gate({"killed": 5, "survived": 5, "total": 10},
                             0.8, "required", "pattern")
        self.assertEqual(gate.result, FAIL)

    def test_report_mode_never_fails_a_non_backfill_card(self) -> None:
        gate = mutation_gate({"killed": 1, "survived": 9, "total": 10},
                             0.8, "report", "pattern")
        self.assertEqual(gate.result, PASS)
        self.assertIn("kill rate", gate.detail)

    def test_report_mode_still_blocks_test_backfill(self) -> None:
        gate = mutation_gate({"killed": 1, "survived": 9, "total": 10},
                             0.8, "report", "test-backfill")
        self.assertEqual(gate.result, FAIL)

    def test_capped_is_reported(self) -> None:
        gate = mutation_gate({"killed": 2, "survived": 2, "total": 4, "capped": True},
                             0.8, "required", "pattern")
        self.assertEqual(gate.result, FAIL)
        self.assertTrue(gate.evidence["capped"])

    def test_off_mode_passes(self) -> None:
        gate = mutation_gate({"killed": 0, "survived": 9, "total": 9},
                             0.8, "off", "test-backfill")
        self.assertEqual(gate.result, PASS)


class ExtraGateTests(unittest.TestCase):
    def test_pass(self) -> None:
        self.assertEqual(extra_gate_result("gen", True, "block").result, PASS)

    def test_fail_names_its_on_fail(self) -> None:
        gate = extra_gate_result("gen", False, "ineligible", "stale output")
        self.assertEqual(gate.result, FAIL)
        self.assertEqual(gate.evidence["on_fail"], "ineligible")

    def test_paths_limit_when_it_runs(self) -> None:
        self.assertTrue(paths_touched_by_extra_gate(
            ["src/types/a.rs"], ["src/types/**"]))
        self.assertFalse(paths_touched_by_extra_gate(
            ["src/other/a.rs"], ["src/types/**"]))
        self.assertTrue(paths_touched_by_extra_gate(["anything"], []))


if __name__ == "__main__":
    unittest.main()
