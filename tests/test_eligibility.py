"""The eligibility gate: every rule, and the ones that must fire first."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openfarm_core.cards import Card, TestRef
from openfarm_core.config import Config
from openfarm_core.gates.eligibility import check_eligibility

BASE = {
    "project": {"name": "toy", "state_dir": "/tmp/openfarm-eligibility-state"},
    "adapters": {"cards": "files:cards/", "languages": ["rust"]},
    "policy": {
        "never_touch": ["src/auth/**"],
        "forbidden_files": ["**/Cargo.toml"],
        "categories": ["pattern", "pinned-bugfix", "mechanical", "small-feature",
                       "test-backfill"],
    },
    "eligibility": {"max_files": 6, "max_units": 1,
                    "require_reference": ["pattern", "small-feature"],
                    "refuse_touches_prefixes": ["seam:"]},
}


def make_config(**overrides) -> Config:
    import copy
    data = copy.deepcopy(BASE)
    for key, value in overrides.items():
        data.setdefault(key, {}).update(value)
    return Config(data, Path("/tmp/toy-repo"))


def good_card(**overrides) -> Card:
    payload = dict(
        id="CARD-1",
        title="A full fascicle refuses a push",
        category="pinned-bugfix",
        touches=["src/fascicle.rs"],
        tests=[TestRef("a_full_fascicle_refuses_a_push", "tests/full.rs",
                       "returns Err(FascicleError::Full)")],
        done_text="push on a full fascicle returns `Err(FascicleError::Full)`.",
        reference="",
        new_symbols=False,
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
    return Card(
        id=payload["id"], title=payload["title"],
        done_text=payload["done_text"],
        touches=payload["touches"],
        fields=fields,
    )


class EligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = make_config()

    def test_a_good_card_passes(self) -> None:
        result = check_eligibility(good_card(), self.cfg)
        self.assertTrue(result.ok, result.failures)

    def test_a_seam_prefix_is_refused_first(self) -> None:
        card = good_card(touches=["seam:gestures"])
        result = check_eligibility(card, self.cfg)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0]["rule"], "seam-prefix")

    def test_never_touch_glob(self) -> None:
        card = good_card(touches=["src/auth/login.rs"])
        result = check_eligibility(card, self.cfg)
        rules = [f["rule"] for f in result.failures]
        self.assertIn("never-touch", rules)

    def test_forbidden_path(self) -> None:
        card = good_card(touches=["Cargo.toml"])
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("forbidden-file", rules)

    def test_reference_on_the_never_touch_list(self) -> None:
        card = good_card(category="pattern", reference="src/auth/seam.rs")
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("reference-never-touch", rules)

    def test_pattern_needs_a_reference(self) -> None:
        card = good_card(category="pattern", reference="")
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("reference-required", rules)

    def test_a_category_not_enabled(self) -> None:
        cfg = make_config(policy={"categories": ["pattern"]})
        rules = [f["rule"] for f in check_eligibility(good_card(), cfg).failures]
        self.assertIn("category", rules)

    def test_an_unknown_category(self) -> None:
        card = good_card(category="architecture")
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("category", rules)

    def test_too_many_files(self) -> None:
        card = good_card(touches=[f"src/f{index}.rs" for index in range(8)])
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("max-files", rules)

    def test_more_than_one_unit(self) -> None:
        card = good_card(touches=["src/a.rs", "other/src/b.rs"])
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("max-units", rules)

    def test_a_named_test_on_known_failing(self) -> None:
        cfg = make_config(policy={"known_failing":
                                  ["a_full_fascicle_refuses_a_push"]})
        rules = [f["rule"] for f in check_eligibility(good_card(), cfg).failures]
        self.assertIn("known-failing", rules)

    def test_known_failing_matches_exactly_not_by_substring(self) -> None:
        cfg = make_config(policy={"known_failing": ["refuses_a_push"]})
        result = check_eligibility(good_card(), cfg)
        self.assertTrue(result.ok, result.failures)

    def test_vague_done_text(self) -> None:
        card = good_card(
            done_text="The fascicle handles being full properly.")
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("vague-done", rules)

    def test_done_text_that_leaves_design(self) -> None:
        card = good_card(done_text="Design the refusal path.")
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("design-left", rules)

    def test_done_text_without_a_concrete_value(self) -> None:
        card = Card("CARD-1", "T", done_text="It should work as expected.",
                    touches=["src/fascicle.rs"],
                    fields={"category": "pinned-bugfix", "unit": "toy",
                            "tests": [{"name": "a_test", "file": "tests/a.rs"}]})
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("done-text-concrete", rules)

    def test_an_unfinished_dependency(self) -> None:
        rules = [f["rule"] for f in
                 check_eligibility(good_card(), self.cfg, deps_done=False).failures]
        self.assertIn("deps", rules)

    def test_rust_tests_must_be_in_separate_files(self) -> None:
        card = good_card(tests=[TestRef("a_test", "src/fascicle.rs",
                                        "returns Err(Full)")])
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("rust-tests-in-separate-files", rules)

    def test_new_symbols_on_a_backfill_card_is_refused(self) -> None:
        card = good_card(category="test-backfill", new_symbols=True)
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("new-symbols", rules)

    def test_no_named_tests(self) -> None:
        card = Card("CARD-1", "T", done_text="returns `Err(Full)`.",
                    touches=["src/fascicle.rs"],
                    fields={"category": "pinned-bugfix", "unit": "toy"})
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("named-tests", rules)

    def test_duplicate_test_names(self) -> None:
        card = good_card(tests=[
            TestRef("same_name", "tests/a.rs", "x"),
            TestRef("same_name", "tests/b.rs", "y")])
        rules = [f["rule"] for f in check_eligibility(card, self.cfg).failures]
        self.assertIn("duplicate-tests", rules)

    def test_the_result_names_every_failed_rule(self) -> None:
        card = good_card(category="pattern", touches=["src/auth/x.rs"])
        result = check_eligibility(card, self.cfg)
        self.assertGreaterEqual(len(result.failures), 2)
        for failure in result.failures:
            self.assertIn("rule", failure)
            self.assertIn("detail", failure)


if __name__ == "__main__":
    unittest.main()
