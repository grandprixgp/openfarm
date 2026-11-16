"""The one `.openfarm/config.toml` template, for `openfarm init` and `openfarm adopt`.

Both used to carry their own copy of the text; they differ only in the values
passed here.
"""

from __future__ import annotations

import json
from pathlib import Path

CATEGORIES = ["test-backfill", "mechanical", "pattern", "pinned-bugfix",
              "small-feature", "feature", "refactor", "integration", "port"]
EDITABLE_SURFACES = ["test_writer_guidance", "implementer_guidance",
                     "planner_guidance", "context_hints", "micromanager_context"]


def toml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def toml_list(values: list[str]) -> str:
    return json.dumps([str(value) for value in values], ensure_ascii=False)


def known_failing_lines(proposed: list[str] | None, base_branch: str) -> list[str]:
    """`known_failing = []`, after a commented proposal when the suite had red tests.

    The proposal stays a comment: openfarm refuses a red base until the owner copies
    the names in, so no failing test is ever set aside without someone choosing to.
    """
    if not proposed:
        return ["known_failing = []"]
    return [
        f"# adopt ran the suite on {base_branch} and these tests failed. openfarm",
        "# refuses a red base, so to work with them failing, copy them into",
        "# known_failing below:",
        f"# {toml_list(proposed)}",
        "known_failing = []",
    ]


def render_config(*, name: str, language: str, base_branch: str, state_dir: Path,
                  key_file: Path, endpoint: str = "https://api.example.invalid/v1",
                  model: str = "lesser-coder", never_touch: list[str] | None = None,
                  test_conventions: str = "", cargo_flags: list[str] | None = None,
                  mutation: str = "off", mm_adapter: str = "claude-acp",
                  mm_mode: str = "active", mm_auth: str = "oauth",
                  mm_binary: str = "auto",
                  proposed_known_failing: list[str] | None = None) -> str:
    if language == "rust":
        forbidden_files = ["**/Cargo.toml", "Cargo.lock", ".openfarm/**", ".claude/**"]
        flagged_patterns = ["std::env::set_var", "sleep("]
        tests_rule = "rust_tests_in_separate_files = true"
        runner = [f"cargo_flags = {toml_list(cargo_flags or [])}"]
    else:
        forbidden_files = ["pyproject.toml", "setup.py", ".openfarm/**", ".claude/**"]
        flagged_patterns = []
        tests_rule = "python_tests_in_test_files = true"
        runner = ["# Empty: the interpreter openfarm itself runs under.", 'python = ""']
    mutation_required = ["test-backfill"] if mutation != "off" else []
    state_dir = Path(state_dir)

    lines = [
        "[project]",
        f"name = {toml_str(name)}",
        'lane = "farm"',
        f"base_branch = {toml_str(base_branch)}",
        f"state_dir = {toml_str(str(state_dir))}",
        "",
        "[adapters]",
        'cards = "files:cards/"',
        'runner = "direct"',
        f"languages = {toml_list([language])}",
        'model = "openai-compatible"',
        f"micromanager = {toml_str(mm_adapter)}",
        'telemetry = "jsonl"',
        'pack = ""',
        "",
        "[model]",
        f"endpoint = {toml_str(endpoint)}",
        f"allowed_endpoints = [{toml_str(endpoint)}]",
        f"key_file = {toml_str(str(key_file))}",
        "# Reasoning models think before they answer, and that counts against this.",
        "max_tokens = 32768",
        "cost_per_1k_in = 0.0005",
        "cost_per_1k_out = 0.0015",
        f"test_writer = {{ model = {toml_str(model)}, temperature = 0.2 }}",
        f"implementer = {{ model = {toml_str(model)}, temperature = 0.3, vary_seed = true }}",
        f"checker     = {{ model = {toml_str(model)}, temperature = 0.35, vary_seed = true }}",
        f"planner     = {{ model = {toml_str(model)}, temperature = 0.2 }}",
        "",
        "[policy]",
        f"never_touch = {toml_list(never_touch or [])}",
        f"forbidden_files = {toml_list(forbidden_files)}",
        *known_failing_lines(proposed_known_failing, base_branch),
        f"flagged_patterns = {toml_list(flagged_patterns)}",
        "categories = [",
        *[f"  {toml_str(category)}," for category in CATEGORIES],
        "]",
        "",
        "[eligibility]",
        "max_files = 6",
        "max_units = 1",
        'require_reference = ["pattern", "small-feature"]',
        'refuse_touches_prefixes = ["seam:"]',
        tests_rule,
        "allow_type_changes = true",
        "max_context_files = 8",
        "",
        "[brief]",
        f"test_conventions = {toml_str(test_conventions)}",
        "",
        "[runner]",
        *runner,
        "",
        "[runner.env]",
        "",
        "[budget]",
        "implementer_attempts = 3",
        "test_writer_attempts = 2",
        "model_build_calls_per_card = 10",
        "mutation_build_calls_per_card = 40",
        "tokens_per_card = 400_000",
        'wall_clock_per_card = "45m"',
        "frontier_cost_ratio = 0.30",
        "spend_per_night = 20.00",
        "concurrent_builds = 2",
        "concurrent_cards = 3",
        'lease_ttl = "20m"',
        f"kill_switch = {toml_str(str(state_dir / 'STOP'))}",
        "",
        "[gates]",
        'revert_check = "required"',
        f"mutation = {toml_str(mutation)}",
        f"mutation_required_for = {toml_list(mutation_required)}",
        "mutation_min_kill_rate = 0.8",
        'counter_tests = "report"',
        'coverage = "off"',
        "workspace_lint = true",
        "baseline_cache = true",
        'catch_up = "check"',
        "findings_per_card = 2",
        "circuit_breaker = 3",
        'planner = "auto"',
        "",
        "[cards]",
        'fields_block = "toml"',
        "required_fields = []",
        "",
        "[micromanager]",
        f"mode = {toml_str(mm_mode)}",
        f"auth = {toml_str(mm_auth)}",
        f"binary = {toml_str(mm_binary)}",
        "args = []",
        'model = ""',
        'effort = "medium"',
        'tool_policy = "none"',
        "# Planning a bigger card can take Claude several minutes.",
        "timeout_s = 600.0",
        "max_calls_per_card = 4",
        "tokens_per_card = 120_000",
        "spend_per_night = 10.0",
        "cost_per_1k_in = 0.003",
        "cost_per_1k_out = 0.015",
        "isolate_home = true",
        "shape_cards = true",
        "scaffold_contracts = true",
        "coach_on_stall = true",
        "adjudicate_counter_tests = true",
        "pre_review = true",
        "",
        "[state]",
        "min_free_mb = 256",
        "",
        "[harness]",
        "enabled = true",
        'dir = ""',
        'capture_on = ["retry_recovery", "fail", "counter_fail", "pass"]',
        "max_cues_per_brief = 4",
        "max_active_adaptations = 8",
        "min_confidence = 0.55",
        "probe_window = 3",
        "auto_demote = true",
        "allow_micromanager_reflection = true",
        f"editable_surfaces = {toml_list(EDITABLE_SURFACES)}",
    ]
    return "\n".join(lines) + "\n"
