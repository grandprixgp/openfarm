"""Config, globs and card hashing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openfarm_core.cards import Card, TestRef, canonical_json, hash_value
from openfarm_core.config import Config, ConfigError, load_config, parse_duration
from openfarm_core.globs import matches, matches_any


BASE = """\
[project]
name = "example"
lane = "farm"
base_branch = "main"
state_dir = "{state}"

[adapters]
cards = "files:cards/"
runner = "direct"
languages = ["rust"]

[model]
endpoint = "https://x.invalid/v1"
allowed_endpoints = ["https://x.invalid/v1"]

[policy]
never_touch = ["src/auth/**"]
categories = ["pattern"]
"""


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".openfarm").mkdir(parents=True)
        self.state = self.tmp / "state"
        self.write(BASE.format(state=self.state))

    def write(self, text: str) -> None:
        (self.repo / ".openfarm" / "config.toml").write_text(text)

    def test_reads_the_documented_keys(self) -> None:
        cfg = load_config(self.repo)
        self.assertEqual(cfg.name, "example")
        self.assertEqual(cfg.lane, "farm")
        self.assertEqual(cfg.base_branch, "main")
        self.assertEqual(cfg.state_dir, self.state.resolve())
        self.assertEqual(cfg.categories, ["pattern"])
        self.assertEqual(cfg.max_files, 6)
        self.assertEqual(cfg.implementer_attempts, 4)

    def test_state_dir_inside_the_repo_is_refused(self) -> None:
        self.write(BASE.format(state=self.repo / "state"))
        with self.assertRaises(ConfigError) as caught:
            load_config(self.repo)
        self.assertIn("outside the repository", str(caught.exception))

    def test_openfarm_config_is_always_forbidden(self) -> None:
        cfg = load_config(self.repo)
        self.assertTrue(cfg.is_forbidden(".openfarm/config.toml"))
        self.assertTrue(cfg.is_forbidden(".openfarm/anything/else.toml"))

    def test_unknown_table_is_an_error(self) -> None:
        self.write(BASE.format(state=self.state) + "\n[mystery]\nx = 1\n")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.repo)
        self.assertIn("mystery", str(caught.exception))

    def test_bad_choice_is_an_error(self) -> None:
        self.write(BASE.format(state=self.state) + "\n[gates]\nmutation = \"maybe\"\n")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.repo)
        self.assertIn("mutation", str(caught.exception))

    def test_durations(self) -> None:
        self.assertEqual(parse_duration("45m"), 2700.0)
        self.assertEqual(parse_duration("2h"), 7200.0)
        self.assertEqual(parse_duration("30s"), 30.0)
        self.assertEqual(parse_duration("1d"), 86400.0)
        self.assertEqual(parse_duration(90), 90.0)
        with self.assertRaises(ConfigError):
            parse_duration("soon")

    def test_known_failing_hash_changes_with_the_list(self) -> None:
        first = load_config(self.repo).known_failing_hash()
        base = BASE.format(state=self.state).replace(
            'never_touch = ["src/auth/**"]',
            'never_touch = ["src/auth/**"]\nknown_failing = ["a::b"]')
        self.write(base)
        second = load_config(self.repo).known_failing_hash()
        third = load_config(self.repo).known_failing_hash()
        self.assertEqual(second, third)
        self.assertNotEqual(first, second)

    def test_extra_gates_parse(self) -> None:
        self.write(BASE.format(state=self.state) + """
[[gates.extra]]
name = "generated-files-current"
run = "cargo run -p gen -- --check"
via = "runner"
paths = ["src/types/**"]
on_fail = "ineligible"
""")
        cfg = load_config(self.repo)
        self.assertEqual(len(cfg.extra_gates), 1)
        gate = cfg.extra_gates[0]
        self.assertEqual(gate.name, "generated-files-current")
        self.assertEqual(gate.on_fail, "ineligible")
        self.assertEqual(gate.paths, ["src/types/**"])

    def test_runner_env_per_unit(self) -> None:
        self.write(BASE.format(state=self.state) + """
[runner.env]
runs = { WS_REQUIRE_BWRAP = "1" }
""")
        cfg = load_config(self.repo)
        self.assertEqual(cfg.runner_env["runs"], {"WS_REQUIRE_BWRAP": "1"})


class GlobTests(unittest.TestCase):
    def test_tree_glob_does_not_match_a_prefix(self) -> None:
        self.assertTrue(matches("src/auth/login.rs", "src/auth/**"))
        self.assertTrue(matches("src/auth/deep/x.rs", "src/auth/**"))
        self.assertFalse(matches("src/authx.rs", "src/auth/**"))

    def test_double_star_matches_at_root_and_depth(self) -> None:
        self.assertTrue(matches("Cargo.toml", "**/Cargo.toml"))
        self.assertTrue(matches("a/b/Cargo.toml", "**/Cargo.toml"))

    def test_star_does_not_cross_a_slash(self) -> None:
        self.assertTrue(matches("src/a.rs", "src/*.rs"))
        self.assertFalse(matches("src/deep/a.rs", "src/*.rs"))

    def test_matches_any(self) -> None:
        self.assertTrue(matches_any("x/y.rs", ["nothing", "x/**"]))
        self.assertFalse(matches_any("x/y.rs", ["nothing"]))

    def test_lock_globs_named_exactly(self) -> None:
        # `**/*lock*` used to swallow test files such as core_lock.rs.
        self.assertFalse(matches("crates/core/tests/core_lock.rs", "Cargo.lock"))
        self.assertTrue(matches("Cargo.lock", "Cargo.lock"))


class CardTests(unittest.TestCase):
    def test_hash_is_stable_and_order_independent(self) -> None:
        first = Card("CARD-1", "Title", touches=["a.rs"], fields={"category": "pattern"})
        second = Card("CARD-1", "Title", touches=["a.rs"], fields={"category": "pattern"})
        self.assertEqual(first.hash(), second.hash())

    def test_hash_changes_with_the_done_text(self) -> None:
        first = Card("CARD-1", "Title", done_text="one")
        second = Card("CARD-1", "Title", done_text="two")
        self.assertNotEqual(first.hash(), second.hash())

    def test_canonical_json_sorts_keys(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_fields_default_safely(self) -> None:
        card = Card("CARD-1")
        self.assertEqual(card.category, "")
        self.assertEqual(card.tests, [])
        self.assertIsNone(card.frontier_estimate)
        self.assertFalse(card.new_symbols)

    def test_tests_accept_bare_strings_or_tables(self) -> None:
        card = Card("CARD-1", fields={"tests": ["a_b", {"name": "c_d"}]})
        self.assertEqual(card.test_names, ["a_b", "c_d"])

    def test_slug_is_words_only(self) -> None:
        card = Card("CARD-1", title="A full fascicle refuses a push")
        self.assertEqual(card.slug(), "a-full-fascicle-refuses")

    def test_round_trip(self) -> None:
        card = Card("CARD-1", "T", done_text="d", touches=["a.rs"], deps=["C-0"],
                    fields={"category": "pattern",
                            "tests": [{"name": "x", "file": "tests/x.rs",
                                       "asserts": "y"}]})
        again = Card.from_dict(card.as_dict())
        self.assertEqual(again.as_dict(), card.as_dict())
        self.assertEqual(again.hash(), card.hash())
        self.assertEqual(again.tests, [TestRef("x", "tests/x.rs", "y")])


if __name__ == "__main__":
    unittest.main()
