"""Every config file in this project and the second repository parses.

A config that stops matching the documented shape is a gate the operator thinks they
changed. These cases read the real files, not fixtures.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from openfarm_core.config import KNOWN_TABLES, load_config

REPO = Path(__file__).resolve().parent.parent
SECOND = Path.home() / "projects" / "openfarm-second"

#: Keys the spec's Config section documents, per table.
SPEC_KEYS = {
    "project": {"name", "lane", "base_branch", "state_dir"},
    "adapters": {"cards", "runner", "languages", "model",
                 "micromanager", "telemetry", "pack"},
    "model": {"endpoint", "allowed_endpoints", "key_file", "test_writer",
              "implementer", "checker", "planner", "cost_per_1k_in",
              "cost_per_1k_out"},
    "policy": {"never_touch", "forbidden_files", "known_failing",
               "flagged_patterns", "categories"},
    "eligibility": {"max_files", "max_units", "require_reference",
                    "refuse_touches_prefixes", "rust_tests_in_separate_files",
                    "allow_type_changes", "max_context_files"},
    "brief": {"test_conventions"},
    "runner": {"env", "cargo_flags"},
    "budget": {"implementer_attempts", "test_writer_attempts",
               "model_build_calls_per_card", "mutation_build_calls_per_card",
               "tokens_per_card", "wall_clock_per_card", "frontier_cost_ratio",
               "spend_per_night", "concurrent_builds", "concurrent_cards",
               "lease_ttl", "kill_switch"},
    "gates": {"revert_check", "mutation", "mutation_required_for",
              "mutation_min_kill_rate", "counter_tests", "coverage",
              "workspace_lint", "baseline_cache", "catch_up",
              "findings_per_card", "circuit_breaker", "extra", "planner"},
    "cards": {"fields_block", "required_fields"},
    "micromanager": {"mode", "binary", "args", "model", "effort",
                     "tool_policy", "timeout_s", "timeout",
                     "max_calls_per_card", "tokens_per_card",
                     "spend_per_night", "cost_per_1k_in", "cost_per_1k_out",
                     "auth_file", "isolate_home", "shape_cards",
                     "scaffold_contracts", "coach_on_stall",
                     "adjudicate_counter_tests", "pre_review"},
    "state": {"min_free_mb"},
    "harness": {"enabled", "dir", "capture_on", "max_cues_per_brief",
                "max_active_adaptations", "min_confidence", "probe_window",
                "auto_demote", "allow_micromanager_reflection",
                "editable_surfaces"},
}


class ConfigFileTests(unittest.TestCase):
    def test_the_documented_tables_are_the_only_tables(self) -> None:
        self.assertEqual(set(SPEC_KEYS), KNOWN_TABLES)

    def test_this_repositorys_config_parses(self) -> None:
        cfg = load_config(REPO)
        self.assertEqual(cfg.name, "openfarm")
        self.assertEqual(cfg.base_branch, "main")
        self.assertEqual(cfg.languages, ["rust"])

    def test_this_repositorys_config_keeps_its_own_gates_forbidden(self) -> None:
        cfg = load_config(REPO)
        self.assertTrue(cfg.is_forbidden(".openfarm/config.toml"))
        self.assertTrue(cfg.is_never_touch("openfarm_core/git.py"))
        self.assertTrue(cfg.is_never_touch("openfarm_core/gates/eligibility.py"))

    def test_the_second_repositorys_config_parses(self) -> None:
        if not (SECOND / ".openfarm" / "config.toml").exists():
            self.skipTest(f"the second repository is not set up at {SECOND}")
        cfg = load_config(SECOND)
        self.assertEqual(cfg.name, "anyhow")
        self.assertEqual(cfg.base_branch, "master")
        # A library with no committed lock file, so the pack will drop --locked and
        # say so. The config still names the flag.
        self.assertIn("--locked", cfg.cargo_flags)

    def test_the_documented_keys_are_the_ones_the_core_reads(self) -> None:
        """A documented key that the parser ignores would be a silent no-op."""
        import tomllib
        for path in (REPO / ".openfarm" / "config.toml",
                     SECOND / ".openfarm" / "config.toml"):
            if not path.exists():
                continue
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            for table, values in data.items():
                self.assertIn(table, KNOWN_TABLES, f"{path}: {table}")
                allowed = SPEC_KEYS.get(table, set())
                for key in values:
                    if table == "gates" and key == "extra":
                        continue
                    self.assertIn(key, allowed,
                                  f"{path}: [{table}] {key} is not documented")


if __name__ == "__main__":
    unittest.main()
