"""`openfarm adopt`: add openfarm to an existing repository without touching its source.

A skeleton for the v0.6 cards (CARD-003, CARD-004, CARD-005, CARD-007): each card
appends its function here. See `.openfarm/PROJECT_SPEC.md`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .errors import Refused
from .git import Git


def detect_language(root: Path) -> str:
    """Return "rust" or "python" from the manifests at the top level of `root`.

    Raises `openfarm_core.errors.Refused` with code "ambiguous-language" when both
    kinds of manifest are present, or "unsupported-language" when neither is.
    Only the top level of `root` is looked at; nothing is written.
    """
    root = Path(root)
    has_cargo = (root / "Cargo.toml").is_file()
    has_python = ((root / "pyproject.toml").is_file()
                  or (root / "setup.py").is_file())

    if has_cargo and has_python:
        raise Refused(
            "ambiguous-language",
            "both a Rust and a Python manifest are present; pass --lang rust or "
            "--lang python",
        )
    if has_cargo:
        return "rust"
    if has_python:
        return "python"
    if (root / "package.json").is_file():
        raise Refused(
            "unsupported-language",
            "no supported language manifest found; TypeScript has no pack yet",
        )
    raise Refused(
        "unsupported-language",
        "no supported language manifest found: expected Cargo.toml, pyproject.toml, "
        "or setup.py",
    )


def _toml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_list(values: list[str]) -> str:
    return json.dumps([str(value) for value in values], ensure_ascii=False)


def _known_failing_lines(proposed: list[str] | None, base_branch: str) -> list[str]:
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
        f"# {_toml_list(proposed)}",
        "known_failing = []",
    ]


def baseline_known_failing(root: Path, language: str, *, target_dir: Path,
                           timeout_s: float = 1800.0) -> dict:
    """Run the suite once on the clean base and propose its red tests.

    Returns {"ran": bool, "known_failing": [...], "notes": [...]}. A Python repo
    with no tests/ folder is not run. Raises `Refused` "suite-does-not-compile"
    when the suite does not build, or "suite-did-not-run" when it could not run
    or failed without naming a failed test.
    """
    from .packs.base import pack_for

    root = Path(root)
    cargo_flags = ["--locked"] if (root / "Cargo.lock").is_file() else []
    pack = pack_for(language, cargo_flags, cwd=root)
    pack.configure(root)
    notes = [str(note) for note in getattr(pack, "notes", [])]
    if (language == "python" and getattr(pack, "runner", "") == "unittest"
            and not (root / getattr(pack, "test_dir", "tests")).is_dir()):
        return {"ran": False, "known_failing": [], "notes": notes}

    argv = pack.commands("test")["argv"]
    env = dict(os.environ)
    # Cargo builds into openfarm's state, not into the repository being adopted.
    env["CARGO_TARGET_DIR"] = str(target_dir)
    try:
        done = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                              env=env, timeout=timeout_s, check=False)
    except FileNotFoundError as exc:
        raise Refused("suite-did-not-run",
                      f"could not run the suite: {argv[0]} was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise Refused("suite-did-not-run",
                      f"the suite took longer than {timeout_s:.0f}s; pass --no-suite "
                      "to adopt without running it") from exc
    parsed = pack.parse_tests(done.stdout, done.stderr)
    proposed = propose_known_failing(parsed)
    if done.returncode != 0 and not proposed:
        tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        raise Refused("suite-did-not-run",
                      "the suite failed without naming a failed test: "
                      + " | ".join(tail))
    return {"ran": True, "known_failing": proposed, "notes": notes}


def adopt_config_text(
    root: Path,
    language: str,
    *,
    base_branch: str,
    state_dir: Path,
    key_file: Path,
    proposed_known_failing: list[str] | None = None,
) -> str:
    """Return the text of `.openfarm/config.toml` for adopting `root` as `language`.

    Raises `openfarm_core.errors.Refused` with code "state-inside-repo" when
    `state_dir` or `key_file` is inside `root`, or "unsupported-language" when
    `language` is not "rust" or "python". Nothing is written.
    """
    if language not in ("rust", "python"):
        raise Refused(
            "unsupported-language",
            f"unsupported language {language!r}: expected 'rust' or 'python'",
        )

    resolved_root = Path(root).expanduser().resolve()
    resolved_state = Path(state_dir).expanduser().resolve()
    resolved_key = Path(key_file).expanduser().resolve()

    if resolved_state.is_relative_to(resolved_root):
        raise Refused(
            "state-inside-repo",
            f"state_dir must be outside the repository: {resolved_state}",
        )
    if resolved_key.is_relative_to(resolved_root):
        raise Refused(
            "state-inside-repo",
            f"key_file must be outside the repository: {resolved_key}",
        )

    if language == "rust":
        forbidden_files = [
            "**/Cargo.toml",
            "Cargo.lock",
            ".openfarm/**",
            ".claude/**",
        ]
        flagged_patterns = ["std::env::set_var", "sleep("]
        cargo_flags = ["--locked"] if (resolved_root / "Cargo.lock").is_file() else []
        runner_extra = []
    else:
        forbidden_files = [
            "pyproject.toml",
            "setup.py",
            ".openfarm/**",
            ".claude/**",
        ]
        flagged_patterns = []
        cargo_flags = []
        runner_extra = ['python = ""']

    categories = [
        "test-backfill",
        "mechanical",
        "pattern",
        "pinned-bugfix",
        "small-feature",
        "feature",
        "refactor",
        "integration",
        "port",
    ]
    editable_surfaces = [
        "test_writer_guidance",
        "implementer_guidance",
        "planner_guidance",
        "context_hints",
        "micromanager_context",
    ]

    lines = [
        "[project]",
        f"name = {_toml_str(resolved_root.name)}",
        'lane = "farm"',
        f"base_branch = {_toml_str(base_branch)}",
        f"state_dir = {_toml_str(str(resolved_state))}",
        "",
        "[adapters]",
        'cards = "files:cards/"',
        'runner = "direct"',
        f"languages = {_toml_list([language])}",
        'model = "openai-compatible"',
        'micromanager = "claude-acp"',
        'telemetry = "jsonl"',
        'pack = ""',
        "",
        "[model]",
        'endpoint = "https://api.example.invalid/v1"',
        'allowed_endpoints = ["https://api.example.invalid/v1"]',
        f"key_file = {_toml_str(str(resolved_key))}",
        "cost_per_1k_in = 0.0005",
        "cost_per_1k_out = 0.0015",
        'test_writer = { model = "lesser-coder", temperature = 0.2 }',
        'implementer = { model = "lesser-coder", temperature = 0.3, vary_seed = true }',
        'checker     = { model = "lesser-coder", temperature = 0.35, vary_seed = true }',
        'planner     = { model = "lesser-coder", temperature = 0.2 }',
        "",
        "[policy]",
        "never_touch = []",
        f"forbidden_files = {_toml_list(forbidden_files)}",
        *_known_failing_lines(proposed_known_failing, base_branch),
        f"flagged_patterns = {_toml_list(flagged_patterns)}",
        f"categories = {_toml_list(categories)}",
        "",
        "[eligibility]",
        "max_files = 6",
        "max_units = 1",
        'require_reference = ["pattern", "small-feature"]',
        'refuse_touches_prefixes = ["seam:"]',
        "rust_tests_in_separate_files = true",
        "allow_type_changes = true",
        "max_context_files = 8",
        "",
        "[brief]",
        'test_conventions = ""',
        "",
        "[runner]",
        f"cargo_flags = {_toml_list(cargo_flags)}",
        *runner_extra,
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
        f"kill_switch = {_toml_str(str(resolved_state / 'STOP'))}",
        "",
        "[gates]",
        'revert_check = "required"',
        'mutation = "off"',
        "mutation_required_for = []",
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
        'mode = "active"',
        'auth = "oauth"',
        'binary = "auto"',
        "args = []",
        'model = ""',
        'effort = "medium"',
        'tool_policy = "none"',
        "timeout_s = 180.0",
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
        f"editable_surfaces = {_toml_list(editable_surfaces)}",
    ]
    return "\n".join(lines) + "\n"


def adopt_project(
    root: Path,
    *,
    language: str | None = None,
    state_dir: Path | None = None,
    key_file: Path | None = None,
    run_suite: bool = False,
    suite_timeout_s: float = 1800.0,
) -> dict:
    """Adopt the existing git repository at `root` without touching its source.

    Raises `openfarm_core.errors.Refused` with code "not-a-git-repo" when
    `root/.git` does not exist, "dirty-worktree" when the worktree is not clean,
    or "already-adopted" when `root/.openfarm/config.toml` exists, in that order.
    Writes `.openfarm/config.toml`, `.openfarm/PROJECT_SPEC.md` and
    `cards/.gitkeep`, creates `state_dir` with mode 0o700, never commits, and
    returns {"ok": True, "language": ..., "written": [...]}.
    """
    root = Path(root).expanduser().resolve()

    if not (root / ".git").exists():
        raise Refused(
            "not-a-git-repo",
            f"{root} is not a git repository: .git is missing",
        )

    # Adopting never commits, so a second adopt always meets a dirty tree; the
    # already-adopted check has to come first to be the one it reports.
    config_path = root / ".openfarm" / "config.toml"
    spec_path = root / ".openfarm" / "PROJECT_SPEC.md"
    gitkeep_path = root / "cards" / ".gitkeep"
    # Checked before anything is written, so a refusal never leaves half an adoption.
    for path in (config_path, spec_path, gitkeep_path):
        if path.exists():
            raise Refused(
                "already-adopted",
                f"{root} is already adopted: {path} exists",
            )

    git = Git(root)
    if not git.is_clean():
        raise Refused(
            "dirty-worktree",
            f"{root} has uncommitted changes; commit or stash them before adopting",
        )

    resolved_language = language or detect_language(root)
    base_branch = git.current_branch()

    name = root.name
    if state_dir is None:
        resolved_state = (Path.home() / ".openfarm" / name).expanduser().resolve()
    else:
        resolved_state = Path(state_dir).expanduser().resolve()
    if key_file is None:
        resolved_key = (
            Path.home() / ".config" / "openfarm" / f"{name}.key"
        ).expanduser().resolve()
    else:
        resolved_key = Path(key_file).expanduser().resolve()

    baseline = None
    if run_suite:
        # Before anything is written: a suite that does not build refuses adoption.
        baseline = baseline_known_failing(
            root, resolved_language, target_dir=resolved_state / "adopt-target",
            timeout_s=suite_timeout_s)

    config_text = adopt_config_text(
        root,
        resolved_language,
        base_branch=base_branch,
        state_dir=resolved_state,
        key_file=resolved_key,
        proposed_known_failing=baseline["known_failing"] if baseline else None,
    )

    resolved_state.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved_state, 0o700)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("x", encoding="utf-8") as handle:
        handle.write(config_text)

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    with spec_path.open("x", encoding="utf-8") as handle:
        handle.write(f"# {name}\n")

    gitkeep_path.parent.mkdir(parents=True, exist_ok=True)
    with gitkeep_path.open("x", encoding="utf-8") as handle:
        handle.write("")

    result = {
        "ok": True,
        "language": resolved_language,
        "written": [
            ".openfarm/config.toml",
            ".openfarm/PROJECT_SPEC.md",
            "cards/.gitkeep",
        ],
    }
    if baseline is not None:
        result["suite_ran"] = baseline["ran"]
        result["known_failing_proposed"] = baseline["known_failing"]
        result["suite_notes"] = baseline["notes"]
    return result


def propose_known_failing(parsed: dict) -> list[str]:
    """Return the sorted, de-duplicated names of failed tests.

    Raises `openfarm_core.errors.Refused` with code "suite-does-not-compile"
    when the pack's parser reported compile errors.
    """
    compile_errors = parsed.get("compile_errors") or []
    if compile_errors:
        first = compile_errors[0]
        binary = first.get("binary", "") if isinstance(first, dict) else str(first)
        raise Refused(
            "suite-does-not-compile",
            f"the suite does not compile: {binary}",
        )

    names = [
        result["name"]
        for result in parsed.get("results") or []
        if result.get("status") == "FAILED"
    ]
    return sorted(set(names))
