"""Reading and validating `.openfarm/config.toml`.

One file per repository, on its base branch. Everything project-specific lives
here; the core names no project. Unknown top-level tables are an error, because a
silently ignored key is a gate whose meaning the operator thinks they changed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import re
import tomllib
from pathlib import Path

from .adapters.acp import ACP_ADAPTER_NAMES, is_acp_adapter
from .errors import ConfigError
from .globs import escapes, resolves_within

CONFIG_PATH = Path(".openfarm") / "config.toml"

#: Tables a repo may have at the top level of the config.
KNOWN_TABLES = {
    "project", "adapters", "model", "policy", "eligibility", "brief",
    "runner", "budget", "gates", "cards", "micromanager", "state", "harness",
    "swarm", "tiers",
}

DEFAULT_EFFORTS = ("off", "low", "medium", "high", "xhigh", "max")
_ACP_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT_LADDER = ("medium", "high", "max")
SUPERVISION_PROFILES = ("lean", "balanced", "strict")
_TIER_KEYS = {"model", "efforts", "effort_param"}
_FORBIDDEN_TIER_KEYS = ("endpoint", "allowed_endpoints", "key_file", "headers")
_RESERVED_EFFORT_PARAMS = frozenset({
    "model", "messages", "max_tokens", "temperature", "seed", "response_format",
})

_TABLE_KEYS = {
    "project": {"name", "lane", "base_branch", "state_dir"},
    "adapters": {"cards", "runner", "languages", "model",
                 "micromanager", "telemetry", "pack"},
    "model": {"endpoint", "allowed_endpoints", "key_file", "headers", "max_tokens",
              "effort_ladder", "test_writer",
              "implementer", "checker", "planner", "cost_per_1k_in",
              "cost_per_1k_out"},
    "policy": {"never_touch", "forbidden_files", "known_failing",
               "flagged_patterns", "categories"},
    "eligibility": {"max_files", "max_units", "require_reference",
                    "refuse_touches_prefixes", "rust_tests_in_separate_files",
                    "python_tests_in_test_files",
                    "allow_type_changes", "max_context_files"},
    "brief": {"test_conventions"},
    "runner": {"env", "cargo_flags", "python", "typescript", "typescript_typecheck",
               "typescript_format", "link_node_modules", "confine",
               "confine_network", "confine_write_roots"},
    "budget": {"implementer_attempts", "test_writer_attempts",
               "model_build_calls_per_card", "mutation_build_calls_per_card",
               "tokens_per_card", "wall_clock_per_card", "frontier_cost_ratio",
               "spend_per_night", "concurrent_builds", "concurrent_cards",
               "lease_ttl", "kill_switch"},
    "gates": {"revert_check", "mutation", "mutation_required_for",
              "mutation_min_kill_rate", "counter_tests", "checker_mode",
              "coverage", "workspace_lint", "baseline_cache", "catch_up",
              "findings_per_card", "circuit_breaker", "extra", "planner"},
    "cards": {"fields_block", "required_fields"},
    "micromanager": {"mode", "binary", "args", "model", "effort",
                     "tool_policy", "timeout_s", "timeout",
                     "max_calls_per_card", "tokens_per_card",
                     "spend_per_night", "cost_per_1k_in", "cost_per_1k_out",
                     "auth", "auth_file", "isolate_home", "shape_cards",
                     "scaffold_contracts", "coach_on_stall",
                     "adjudicate_counter_tests", "pre_review",
                     "confine", "confine_read_roots", "lean_prompt",
                     "combine_calls", "shape_when", "pre_review_when",
                     "pre_review_max_lines"},
    "state": {"min_free_mb"},
    "harness": {"enabled", "dir", "capture_on", "max_cues_per_brief",
                "max_active_adaptations", "min_confidence", "probe_window",
                "auto_demote", "allow_micromanager_reflection",
                "editable_surfaces", "global_dir", "learn_from", "share",
                "reflect_on"},
    "swarm": {"enabled", "coordinator", "supervisor", "coordinator_effort",
              "supervisor_effort", "default_tier", "escalate_after",
              "handbacks_per_card", "max_members", "coordinator_calls_per_run",
              "coordinator_tokens_per_run", "external_wait",
              "on_external_timeout", "stall_after"},
    "tiers": set(_TIER_KEYS),
}
_ROLE_MODEL_KEYS = {"model", "temperature", "vary_seed", "effort", "retry_effort", "effort_ladder"}
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
# openfarm sets these itself; a config may not replace the key or the body type.
_RESERVED_HEADERS = {"authorization", "content-type", "content-length", "host"}


def _model_headers(value) -> dict[str, str]:
    """`[model] headers`: extra request headers, such as a zero-retention opt-in."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("[model] headers must be a table of strings")
    headers: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(raw, str):
            raise ConfigError(f"[model] headers.{name} must be a string")
        if not _HEADER_NAME.match(name):
            raise ConfigError(f"[model] headers: {name!r} is not a header name")
        if name.lower() in _RESERVED_HEADERS:
            raise ConfigError(f"[model] headers: openfarm sets {name} itself")
        if "\r" in raw or "\n" in raw:
            raise ConfigError(f"[model] headers.{name} holds a line break")
        headers[name] = raw
    return headers
_EXTRA_GATE_KEYS = {"name", "run", "via", "paths", "on_fail"}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")
_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value, what: str = "duration") -> float:
    """Seconds from `45m`, `2h`, `30s`, `1d`, or a number of seconds.

    A bare string of digits is seconds, so `--for 60` and `--for 60s` agree.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        return float(text)
    match = _DURATION_RE.match(text)
    if not match:
        raise ConfigError(f"{what}: cannot read {value!r} as a duration")
    return float(match.group(1)) * _DURATION_UNITS[match.group(2)]


def _as_str_list(value, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{what}: expected a list of strings")
    return list(value)


def _as_int(value, what: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{what}: expected an integer")
    return value


def _as_float(value, what: str, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{what}: expected a number")
    return float(value)


def _as_bool(value, what: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{what}: expected true or false")
    return value


def _uses_typescript(languages) -> bool:
    return any(lang in ("typescript", "ts", "javascript", "js") for lang in languages)


def _as_choice(value, what: str, default: str, choices: tuple[str, ...]) -> str:
    if value is None:
        return default
    if value not in choices:
        raise ConfigError(f"{what}: {value!r} is not one of {', '.join(choices)}")
    return value


def _as_adapter(value, what: str, default: str, built_in: tuple[str, ...]) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{what}: expected a string")
    if value in built_in:
        return value
    if value.startswith("cmd:") and value[4:].strip():
        return value
    choices = ", ".join(repr(c) for c in built_in)
    raise ConfigError(f"{what}: {value!r} must be one of {choices} or 'cmd:<argv>'")


def _check_keys(table: dict, allowed: set[str], what: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(
            f"{what}: unknown key(s): {', '.join(sorted(unknown))}")


def _nonneg_int(value, what: str, default: int) -> int:
    result = _as_int(value, what, default)
    if result < 0:
        raise ConfigError(f"{what}: must be >= 0")
    return result


def _nonneg_float(value, what: str, default: float) -> float:
    result = _as_float(value, what, default)
    if result < 0.0:
        raise ConfigError(f"{what}: must be >= 0")
    return result


class RoleModel:
    """One model role's settings."""

    __slots__ = ("model", "temperature", "vary_seed", "effort", "retry_effort", "effort_ladder")

    def __init__(self, model: str = "", temperature: float = 0.2,
                 vary_seed: bool = False, effort: str = "",
                 retry_effort: str = "",
                 effort_ladder: tuple[str, ...] = ()) -> None:
        self.model = model
        self.temperature = temperature
        self.vary_seed = vary_seed
        self.effort = effort
        #: The effort for every attempt after the first (v0.8.5); empty keeps `effort`.
        self.retry_effort = retry_effort
        #: Optional per-attempt escalation ladder (v0.8.8), e.g. ("medium", "high", "max").
        self.effort_ladder = tuple(effort_ladder)

    def effort_for_attempt(self, attempt: int = 1) -> str:
        if self.effort_ladder:
            idx = min(max(int(attempt), 1) - 1, len(self.effort_ladder) - 1)
            return self.effort_ladder[idx]
        if attempt > 1 and self.retry_effort:
            return self.retry_effort
        return self.effort

    def as_dict(self) -> dict:
        return {"model": self.model, "temperature": self.temperature,
                "vary_seed": self.vary_seed, "effort": self.effort,
                "retry_effort": self.retry_effort,
                "effort_ladder": list(self.effort_ladder)}


@dataclasses.dataclass
class TierConfig:
    """One named model tier and its allowed effort settings."""

    name: str
    model: str
    efforts: tuple[str, ...] = DEFAULT_EFFORTS
    effort_param: str = "reasoning_effort"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "efforts": list(self.efforts),
            "effort_param": self.effort_param,
        }


@dataclasses.dataclass
class SwarmConfig:
    """Settings for the v0.8 board, seats, coordinator and parallel cards."""

    enabled: bool = False
    coordinator: str = "code"
    supervisor: str = "config"
    coordinator_effort: str = "max"
    supervisor_effort: str = "max"
    default_tier: str = "default"
    escalate_after: int = 2
    handbacks_per_card: int = 2
    max_members: int = 24
    coordinator_calls_per_run: int = 60
    coordinator_tokens_per_run: int = 600_000
    external_wait_s: float = 1800.0
    on_external_timeout: str = "fallback"
    stall_after_s: float = 300.0

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "coordinator": self.coordinator,
            "supervisor": self.supervisor,
            "coordinator_effort": self.coordinator_effort,
            "supervisor_effort": self.supervisor_effort,
            "default_tier": self.default_tier,
            "escalate_after": self.escalate_after,
            "handbacks_per_card": self.handbacks_per_card,
            "max_members": self.max_members,
            "coordinator_calls_per_run": self.coordinator_calls_per_run,
            "coordinator_tokens_per_run": self.coordinator_tokens_per_run,
            "external_wait_s": self.external_wait_s,
            "on_external_timeout": self.on_external_timeout,
            "stall_after_s": self.stall_after_s,
        }


class ExtraGate:
    """A named command that must pass, from `[[gates.extra]]`."""

    __slots__ = ("name", "run", "via", "paths", "on_fail")

    def __init__(self, name: str, run: str, via: str = "runner",
                 paths: list[str] | None = None, on_fail: str = "block") -> None:
        self.name = name
        self.run = run
        self.via = via
        self.paths = list(paths or [])
        self.on_fail = on_fail

    def as_dict(self) -> dict:
        return {"name": self.name, "run": self.run, "via": self.via,
                "paths": self.paths, "on_fail": self.on_fail}


class MicromanagerConfig:
    """Settings for the optional v0.4 micromanager supervisor."""

    __slots__ = (
        "mode", "binary", "args", "model", "effort", "tool_policy",
        "timeout_s", "max_calls_per_card", "tokens_per_card",
        "spend_per_night", "cost_per_1k_in", "cost_per_1k_out",
        "auth", "auth_file", "isolate_home", "shape_cards",
        "scaffold_contracts", "coach_on_stall",
        "adjudicate_counter_tests", "pre_review",
        "confine", "confine_read_roots", "lean_prompt", "combine_calls",
        "shape_when", "pre_review_when", "pre_review_max_lines",
    )

    def __init__(self, **kwargs) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs[slot])

    def as_dict(self) -> dict:
        return {
            slot: (str(getattr(self, slot))
                   if slot == "auth_file" and getattr(self, slot) is not None
                   else getattr(self, slot))
            for slot in self.__slots__
        }


VALID_LEARN_FROM = ("both", "project", "global")
VALID_HARNESS_CAPTURE_ON = ("retry_recovery", "fail", "counter_fail", "pass")
#: When the supervisor is asked to reflect on a card (v0.8.5). A clean first-try
#: pass is left out: across the v0.8.1 bench, 41 of 47 reflection calls were on
#: such cards and what they proposed was about openfarm's own bookkeeping.
DEFAULT_HARNESS_REFLECT_ON = ("retry_recovery", "fail", "counter_fail")
VALID_HARNESS_SURFACES = (
    "test_writer_guidance",
    "implementer_guidance",
    "planner_guidance",
    "context_hints",
    "micromanager_context",
)


@dataclasses.dataclass
class HarnessConfig:
    """Settings for the recursive harness efficiency subsystem."""

    enabled: bool = True
    dir: str = ""
    capture_on: tuple[str, ...] = ("retry_recovery", "fail", "counter_fail", "pass")
    max_cues_per_brief: int = 4
    max_active_adaptations: int = 8
    min_confidence: float = 0.55
    probe_window: int = 3
    auto_demote: bool = True
    allow_micromanager_reflection: bool = True
    reflect_on: tuple[str, ...] = ("retry_recovery", "fail", "counter_fail")
    editable_surfaces: tuple[str, ...] = (
        "test_writer_guidance",
        "implementer_guidance",
        "planner_guidance",
        "context_hints",
        "micromanager_context",
    )
    # v0.7 global learning. An empty global_dir means the default: the
    # OPENFARM_GLOBAL_HARNESS environment variable, else ~/.openfarm/global/harness.
    global_dir: str = ""
    learn_from: str = "both"
    share: bool = True

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dir": self.dir,
            "capture_on": list(self.capture_on),
            "max_cues_per_brief": self.max_cues_per_brief,
            "max_active_adaptations": self.max_active_adaptations,
            "min_confidence": self.min_confidence,
            "probe_window": self.probe_window,
            "auto_demote": self.auto_demote,
            "allow_micromanager_reflection": self.allow_micromanager_reflection,
            "reflect_on": list(self.reflect_on),
            "editable_surfaces": list(self.editable_surfaces),
            "global_dir": self.global_dir,
            "learn_from": self.learn_from,
            "share": self.share,
        }


class Config:
    """The repository's whole configuration, already validated."""

    def __init__(self, data: dict, repo: Path, path: Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.path = path
        self._data = data
        self._parse()

    # -- parsing --------------------------------------------------------------

    def _table(self, name: str) -> dict:
        value = self._data.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"[{name}] must be a table")
        if name in _TABLE_KEYS:
            _check_keys(value, _TABLE_KEYS[name], f"[{name}]")
        return value

    def _parse(self) -> None:
        unknown = set(self._data) - KNOWN_TABLES
        if unknown:
            raise ConfigError(
                "unknown config table(s): " + ", ".join(sorted(unknown)))

        project = self._table("project")
        self.name = str(project.get("name", ""))
        if not self.name:
            raise ConfigError("[project] name is required")
        self.lane = str(project.get("lane", "farm"))
        self.base_branch = str(project.get("base_branch", "main"))
        state_dir = project.get("state_dir")
        if not state_dir:
            raise ConfigError("[project] state_dir is required")
        self.state_dir = self._outside_repo(state_dir, "[project] state_dir")

        adapters = self._table("adapters")
        self.cards_adapter = str(adapters.get("cards", "files:cards/"))
        self.runner_adapter = str(adapters.get("runner", "direct"))
        self.languages = _as_str_list(adapters.get("languages", ["rust"]),
                                      "[adapters] languages") or ["rust"]
        self.model_adapter = str(adapters.get("model", "openai-compatible"))
        self.micromanager_adapter = _as_adapter(
            adapters.get("micromanager"), "[adapters] micromanager",
            "off", ("off", *ACP_ADAPTER_NAMES, "scripted"))
        self.telemetry_adapter = _as_adapter(
            adapters.get("telemetry"), "[adapters] telemetry",
            "jsonl", ("jsonl",))
        self.pack_adapter = _as_adapter(
            adapters.get("pack"), "[adapters] pack",
            "", ("",))

        model = self._table("model")
        self.endpoint = str(model.get("endpoint", ""))
        self.allowed_endpoints = _as_str_list(
            model.get("allowed_endpoints", []), "[model] allowed_endpoints")
        key_file = model.get("key_file")
        self.key_file = self._outside_repo(key_file, "[model] key_file") if key_file else None
        self.model_headers = _model_headers(model.get("headers"))
        # The most a worker may write in one answer. A model that reasons before it
        # answers spends part of this on thinking, so 4096 can cut a file off.
        self.model_max_tokens = _as_int(model.get("max_tokens"),
                                        "[model] max_tokens", 4096)
        if self.model_max_tokens <= 0:
            raise ConfigError("[model] max_tokens must be > 0")
        # Prices are what make a spend cap mean anything. Without them every call
        # reports a zero cost and no spend cap can fire, so the run record says so.
        self.cost_per_1k_in = _as_float(model.get("cost_per_1k_in"),
                                        "[model] cost_per_1k_in", 0.0)
        self.cost_per_1k_out = _as_float(model.get("cost_per_1k_out"),
                                         "[model] cost_per_1k_out", 0.0)
        implementer_role = self._role_model(model.get("implementer"), "implementer")
        self.role_models = {
            role: (
                implementer_role
                if role == "implementer"
                else self._role_model(
                    model.get(role), role, default_model=implementer_role.model
                )
            )
            for role in ("test_writer", "implementer", "checker", "planner")
        }

        policy = self._table("policy")
        self.never_touch = _as_str_list(policy.get("never_touch", []),
                                        "[policy] never_touch")
        forbidden = _as_str_list(policy.get("forbidden_files", []),
                                 "[policy] forbidden_files")
        # The config and ACP directories are forbidden to the farm whatever the
        # file says: a farm that could edit them could weaken any gate or hook.
        for always_forbidden in (".openfarm/**", ".claude/**"):
            if always_forbidden not in forbidden:
                forbidden.append(always_forbidden)
        self.forbidden_files = forbidden
        self.known_failing = _as_str_list(policy.get("known_failing", []),
                                          "[policy] known_failing")
        self.flagged_patterns = _as_str_list(policy.get("flagged_patterns", []),
                                             "[policy] flagged_patterns")
        self.categories = _as_str_list(policy.get("categories", []),
                                       "[policy] categories")
        if not self.categories:
            raise ConfigError("[policy] categories is required and must not be empty")

        elig = self._table("eligibility")
        self.max_files = _as_int(elig.get("max_files"), "[eligibility] max_files", 6)
        default_units = 1
        if len(self.languages) > 1:
            default_units = len(self.languages)
        self.max_units = _as_int(elig.get("max_units"), "[eligibility] max_units",
                                 default_units)
        self.require_reference = _as_str_list(
            elig.get("require_reference", ["pattern", "small-feature"]),
            "[eligibility] require_reference")
        self.refuse_touches_prefixes = _as_str_list(
            elig.get("refuse_touches_prefixes", []),
            "[eligibility] refuse_touches_prefixes")
        self.rust_tests_in_separate_files = _as_bool(
            elig.get("rust_tests_in_separate_files"),
            "[eligibility] rust_tests_in_separate_files", True)
        self.python_tests_in_test_files = _as_bool(
            elig.get("python_tests_in_test_files"),
            "[eligibility] python_tests_in_test_files", True)
        self.allow_type_changes = _as_bool(
            elig.get("allow_type_changes"),
            "[eligibility] allow_type_changes", True)
        self.max_context_files = _nonneg_int(
            elig.get("max_context_files"),
            "[eligibility] max_context_files", 8)

        brief = self._table("brief")
        conventions = brief.get("test_conventions")
        if conventions is None or conventions == "":
            self.test_conventions_path = None
        else:
            if not isinstance(conventions, str):
                raise ConfigError("[brief] test_conventions must be a path string")
            if (escapes(conventions)
                    or Path(conventions.replace("\\", "/")).is_absolute()
                    or resolves_within(self.repo, conventions) is None):
                raise ConfigError(
                    f"[brief] test_conventions {conventions!r} must stay within the repository"
                )
            self.test_conventions_path = Path(conventions)

        runner = self._table("runner")
        self.cargo_flags = _as_str_list(runner.get("cargo_flags", []),
                                        "[runner] cargo_flags")
        python = runner.get("python", "")
        if not isinstance(python, str) or "\n" in python:
            raise ConfigError("[runner] python must be one interpreter path or name")
        #: The interpreter the Python pack runs under; openfarm's own when empty.
        self.runner_python = python.strip()
        # The TypeScript pack's runner and tools, recorded by adopt.
        self.runner_typescript = _as_choice(
            runner.get("typescript"), "[runner] typescript", "node",
            ("node", "vitest", "jest"))
        self.typescript_typecheck = _as_choice(
            runner.get("typescript_typecheck"), "[runner] typescript_typecheck", "auto",
            ("auto", "tsc", "syntax"))
        self.typescript_format = _as_choice(
            runner.get("typescript_format"), "[runner] typescript_format", "auto",
            ("auto", "prettier", "off"))
        self.link_node_modules = _as_bool(
            runner.get("link_node_modules"), "[runner] link_node_modules", True)
        # v0.8.5: gate commands in a boundary where only the worktree is writable.
        self.runner_confine = _as_choice(
            runner.get("confine"), "[runner] confine", "off", ("off", "bwrap"))
        self.runner_confine_network = _as_choice(
            runner.get("confine_network"), "[runner] confine_network", "off",
            ("off", "on"))
        self.runner_write_roots = _as_str_list(
            runner.get("confine_write_roots", []), "[runner] confine_write_roots")
        env_table = runner.get("env") or {}
        if not isinstance(env_table, dict):
            raise ConfigError("[runner.env] must be a table")
        self.runner_env: dict[str, dict[str, str]] = {}
        for unit, values in env_table.items():
            if not isinstance(values, dict):
                raise ConfigError(f"[runner.env] {unit}: must be a table")
            self.runner_env[str(unit)] = {str(k): str(v) for k, v in values.items()}

        budget = self._table("budget")
        self.implementer_attempts = _as_int(
            budget.get("implementer_attempts"), "[budget] implementer_attempts", 4)
        self.test_writer_attempts = _as_int(
            budget.get("test_writer_attempts"), "[budget] test_writer_attempts", 2)
        self.model_build_calls_per_card = _as_int(
            budget.get("model_build_calls_per_card"),
            "[budget] model_build_calls_per_card", 10)
        self.mutation_build_calls_per_card = _as_int(
            budget.get("mutation_build_calls_per_card"),
            "[budget] mutation_build_calls_per_card", 40)
        self.tokens_per_card = _as_int(
            budget.get("tokens_per_card"), "[budget] tokens_per_card", 400_000)
        self.wall_clock_per_card = parse_duration(
            budget.get("wall_clock_per_card", "45m"),
            "[budget] wall_clock_per_card")
        self.frontier_cost_ratio = (
            _as_float(budget.get("frontier_cost_ratio"),
                      "[budget] frontier_cost_ratio", 0.0) or None)
        self.spend_per_night = _as_float(
            budget.get("spend_per_night"), "[budget] spend_per_night", 20.0)
        self.concurrent_builds = _as_int(
            budget.get("concurrent_builds"), "[budget] concurrent_builds", 2)
        self.concurrent_cards = _as_int(
            budget.get("concurrent_cards"), "[budget] concurrent_cards", 3)
        self.lease_ttl_s = parse_duration(
            budget.get("lease_ttl", "20m"), "[budget] lease_ttl")
        kill = budget.get("kill_switch")
        self.kill_switch = Path(kill) if kill else self.state_dir / "STOP"

        gates = self._table("gates")
        self.revert_check = _as_choice(
            gates.get("revert_check"), "[gates] revert_check", "required",
            ("required", "off"))
        self.mutation = _as_choice(
            gates.get("mutation"), "[gates] mutation", "report",
            ("off", "report", "required"))
        self.mutation_required_for = _as_str_list(
            gates.get("mutation_required_for", ["test-backfill"]),
            "[gates] mutation_required_for")
        # v0.8.5: TypeScript (and Python) cards are mutated by openfarm's own
        # runner, so "required" no longer has to be refused for them.
        self.mutation_min_kill_rate = _as_float(
            gates.get("mutation_min_kill_rate"),
            "[gates] mutation_min_kill_rate", 0.8)
        self.counter_tests = _as_choice(
            gates.get("counter_tests"), "[gates] counter_tests", "reject",
            ("report", "reject"))
        self.checker_mode = _as_choice(
            gates.get("checker_mode"), "[gates] checker_mode", "two-pass",
            ("two-pass", "single-pass"))
        self.coverage = _as_choice(
            gates.get("coverage"), "[gates] coverage", "off",
            ("off", "report", "required"))
        self.workspace_lint = _as_bool(
            gates.get("workspace_lint"), "[gates] workspace_lint", True)
        self.baseline_cache = _as_bool(
            gates.get("baseline_cache"), "[gates] baseline_cache", True)
        self.catch_up = _as_choice(
            gates.get("catch_up"), "[gates] catch_up", "check", ("off", "check"))
        self.findings_per_card = _as_int(
            gates.get("findings_per_card"), "[gates] findings_per_card", 2)
        self.circuit_breaker = _as_int(
            gates.get("circuit_breaker"), "[gates] circuit_breaker", 3)
        self.planner = _as_choice(
            gates.get("planner"), "[gates] planner", "auto", ("off", "auto", "on"))
        self.extra_gates = self._parse_extra_gates(gates.get("extra", []))

        cards = self._table("cards")
        self.fields_block = _as_choice(
            cards.get("fields_block"), "[cards] fields_block", "", ("", "toml"))
        self.required_fields = _as_str_list(
            cards.get("required_fields", []), "[cards] required_fields")

        state = self._table("state")
        self.min_free_mb = _nonneg_int(
            state.get("min_free_mb"), "[state] min_free_mb", 256)

        self.micromanager = self._parse_micromanager(self._table("micromanager"))
        self.micromanager_enabled = (
            self.micromanager_adapter != "off"
            and self.micromanager.mode != "off"
        )
        self.harness = self._parse_harness(self._table("harness"))
        self.tiers = self._parse_tiers(
            self._data.get("tiers"), self.role_models["implementer"].model
        )
        self.swarm = self._parse_swarm(self._table("swarm"))
        self.effort_ladder: tuple[str, ...] = tuple(
            _as_str_list(self._table("model").get("effort_ladder", []), "[model] effort_ladder")
        )
        for val in self.effort_ladder:
            if any(ch.isspace() for ch in val) or "\x00" in val or not val:
                raise ConfigError(f"[model] effort_ladder contains invalid effort {val!r}")
        for role_name, role_model in self.role_models.items():
            if role_model.retry_effort and not role_model.effort:
                raise ConfigError(
                    f"[model] {role_name}.retry_effort needs an effort for the first "
                    "attempt as well")
            to_check = [(k, getattr(role_model, k)) for k in ("effort", "retry_effort") if getattr(role_model, k)]
            for step in role_model.effort_ladder:
                to_check.append(("effort_ladder", step))
            if not to_check and self.effort_ladder:
                for step in self.effort_ladder:
                    to_check.append(("effort_ladder", step))
            for key, value in to_check:
                if is_acp_adapter(self.model_adapter):
                    if value not in _ACP_EFFORTS:
                        raise ConfigError(
                            f"[model] {role_name}.{key} {value!r} is not "
                            f"one of {list(_ACP_EFFORTS)!r}"
                        )
                else:
                    tier = self.tier_for_model(role_model.model)
                    if value not in tier.efforts:
                        raise ConfigError(
                            f"[model] {role_name}.{key} {value!r} is not "
                            f"in tier {tier.name!r} efforts {list(tier.efforts)!r}"
                        )

    def _parse_harness(self, h: dict) -> HarnessConfig:
        enabled = _as_bool(h.get("enabled"), "[harness] enabled", True)
        raw_dir = h.get("dir", "")
        if raw_dir is None:
            raw_dir = ""
        if not isinstance(raw_dir, str):
            raise ConfigError("[harness] dir: expected a string")
        if "\x00" in raw_dir:
            raise ConfigError("[harness] dir: invalid null byte")
        resolved_dir = ""
        if raw_dir.strip():
            resolved_dir = str(self._outside_repo(raw_dir.strip(), "[harness].dir"))

        if "capture_on" in h and h["capture_on"] is not None:
            raw_capture = _as_str_list(h["capture_on"], "[harness] capture_on")
            for item in raw_capture:
                if item not in VALID_HARNESS_CAPTURE_ON:
                    raise ConfigError(
                        f"[harness] capture_on: {item!r} is not one of "
                        f"{', '.join(VALID_HARNESS_CAPTURE_ON)}"
                    )
            capture_on = tuple(dict.fromkeys(raw_capture))
        else:
            capture_on = VALID_HARNESS_CAPTURE_ON

        max_cues = _nonneg_int(
            h.get("max_cues_per_brief"), "[harness] max_cues_per_brief", 4
        )
        max_active = _nonneg_int(
            h.get("max_active_adaptations"), "[harness] max_active_adaptations", 8
        )
        min_confidence = _as_float(
            h.get("min_confidence"), "[harness] min_confidence", 0.55
        )
        if not (0.0 <= min_confidence <= 1.0):
            raise ConfigError("[harness] min_confidence: must be between 0.0 and 1.0")
        probe_window = _as_int(h.get("probe_window"), "[harness] probe_window", 3)
        if probe_window < 1:
            raise ConfigError("[harness] probe_window: must be >= 1")
        auto_demote = _as_bool(h.get("auto_demote"), "[harness] auto_demote", True)
        allow_mm_refl = _as_bool(
            h.get("allow_micromanager_reflection"),
            "[harness] allow_micromanager_reflection",
            True,
        )

        if "reflect_on" in h and h["reflect_on"] is not None:
            raw_reflect = _as_str_list(h["reflect_on"], "[harness] reflect_on")
            for item in raw_reflect:
                if item not in VALID_HARNESS_CAPTURE_ON:
                    raise ConfigError(
                        f"[harness] reflect_on: {item!r} is not one of "
                        f"{', '.join(VALID_HARNESS_CAPTURE_ON)}"
                    )
            reflect_on = tuple(dict.fromkeys(raw_reflect))
        else:
            reflect_on = DEFAULT_HARNESS_REFLECT_ON

        if "editable_surfaces" in h and h["editable_surfaces"] is not None:
            raw_surfaces = _as_str_list(
                h["editable_surfaces"], "[harness] editable_surfaces"
            )
            for item in raw_surfaces:
                if item not in VALID_HARNESS_SURFACES:
                    raise ConfigError(
                        f"[harness] editable_surfaces: {item!r} is not one of "
                        f"{', '.join(VALID_HARNESS_SURFACES)}"
                    )
            editable_surfaces = tuple(dict.fromkeys(raw_surfaces))
        else:
            editable_surfaces = VALID_HARNESS_SURFACES

        raw_global = h.get("global_dir", "")
        if raw_global is None:
            raw_global = ""
        if not isinstance(raw_global, str) or "\x00" in raw_global:
            raise ConfigError("[harness] global_dir: expected a path string")
        global_dir = ""
        if raw_global.strip():
            global_dir = str(self._outside_repo(raw_global.strip(), "[harness] global_dir"))
        learn_from = _as_choice(h.get("learn_from"), "[harness] learn_from", "both",
                                VALID_LEARN_FROM)
        share = _as_bool(h.get("share"), "[harness] share", True)

        return HarnessConfig(
            enabled=enabled,
            dir=resolved_dir,
            capture_on=capture_on,
            max_cues_per_brief=max_cues,
            max_active_adaptations=max_active,
            min_confidence=min_confidence,
            probe_window=probe_window,
            auto_demote=auto_demote,
            allow_micromanager_reflection=allow_mm_refl,
            reflect_on=reflect_on,
            editable_surfaces=editable_surfaces,
            global_dir=global_dir,
            learn_from=learn_from,
            share=share,
        )

    def _parse_micromanager(self, mm: dict) -> MicromanagerConfig:
        if "timeout_s" in mm and "timeout" in mm:
            raise ConfigError("[micromanager] specify either timeout_s or timeout, not both")
        raw_timeout = mm.get("timeout_s", mm.get("timeout", 600.0))
        timeout_s = parse_duration(raw_timeout, "[micromanager] timeout_s")
        if timeout_s <= 0:
            raise ConfigError("[micromanager] timeout_s must be > 0")
        auth_file = mm.get("auth_file")
        resolved_auth = (
            self._outside_repo(auth_file, "[micromanager] auth_file")
            if auth_file else None
        )
        raw_auth_mode = mm.get("auth")
        if raw_auth_mode is None:
            auth_mode = "api-key" if resolved_auth is not None else "oauth"
        else:
            auth_mode = _as_choice(
                raw_auth_mode, "[micromanager] auth",
                "oauth", ("oauth", "api-key", "auto"))
        if auth_mode == "oauth" and resolved_auth is not None:
            raise ConfigError(
                "[micromanager] auth = 'oauth' cannot be combined with auth_file")
        raw_binary = mm.get("binary", "auto")
        if not isinstance(raw_binary, str) or not raw_binary.strip():
            raise ConfigError("[micromanager] binary: expected a non-empty string")
        return MicromanagerConfig(
            mode=_as_choice(mm.get("mode"), "[micromanager] mode",
                            "active", ("off", "advisory", "active")),
            binary=raw_binary.strip(),
            args=_as_str_list(mm.get("args", []), "[micromanager] args"),
            model=str(mm.get("model", "")),
            effort=_as_choice(mm.get("effort"), "[micromanager] effort",
                              "medium", _ACP_EFFORTS),
            tool_policy=_as_choice(mm.get("tool_policy"),
                                   "[micromanager] tool_policy",
                                   "none", ("none", "read-only")),
            timeout_s=timeout_s,
            max_calls_per_card=_nonneg_int(
                mm.get("max_calls_per_card"), "[micromanager] max_calls_per_card", 4),
            tokens_per_card=_nonneg_int(
                mm.get("tokens_per_card"), "[micromanager] tokens_per_card", 120_000),
            spend_per_night=_nonneg_float(
                mm.get("spend_per_night"), "[micromanager] spend_per_night", 10.0),
            cost_per_1k_in=_nonneg_float(
                mm.get("cost_per_1k_in"), "[micromanager] cost_per_1k_in", 0.0),
            cost_per_1k_out=_nonneg_float(
                mm.get("cost_per_1k_out"), "[micromanager] cost_per_1k_out", 0.0),
            auth=auth_mode,
            auth_file=resolved_auth,
            isolate_home=_as_bool(mm.get("isolate_home"),
                                  "[micromanager] isolate_home", True),
            shape_cards=_as_bool(mm.get("shape_cards"),
                                 "[micromanager] shape_cards", True),
            scaffold_contracts=_as_bool(mm.get("scaffold_contracts"),
                                        "[micromanager] scaffold_contracts", True),
            coach_on_stall=_as_bool(mm.get("coach_on_stall"),
                                    "[micromanager] coach_on_stall", True),
            adjudicate_counter_tests=_as_bool(
                mm.get("adjudicate_counter_tests"),
                "[micromanager] adjudicate_counter_tests", True),
            pre_review=_as_bool(mm.get("pre_review"),
                                "[micromanager] pre_review", True),
            # How provider children are held (see confine.py): auto, bwrap or off.
            confine=_as_choice(mm.get("confine"), "[micromanager] confine",
                               "auto", ("auto", "bwrap", "off")),
            confine_read_roots=self._confine_read_roots(mm.get("confine_read_roots")),
            # openfarm's own bridges replace the provider CLI's built-in prompt.
            lean_prompt=_as_bool(mm.get("lean_prompt"),
                                 "[micromanager] lean_prompt", True),
            # One supervisor call for shape and scaffold when a card needs both.
            combine_calls=_as_bool(mm.get("combine_calls"),
                                   "[micromanager] combine_calls", True),
            shape_when=_as_choice(mm.get("shape_when"),
                                  "[micromanager] shape_when", "always",
                                  ("always", "new-symbols", "off")),
            pre_review_when=_as_choice(mm.get("pre_review_when"),
                                       "[micromanager] pre_review_when", "always",
                                       ("always", "on-flag", "off")),
            pre_review_max_lines=_nonneg_int(
                mm.get("pre_review_max_lines"),
                "[micromanager] pre_review_max_lines", 60),
        )

    def _confine_read_roots(self, value) -> list[str]:
        roots = _as_str_list(value, "[micromanager] confine_read_roots")
        out: list[str] = []
        for raw in roots:
            path = Path(os.path.expanduser(raw))
            if "\x00" in raw or not path.is_absolute():
                raise ConfigError(
                    f"[micromanager] confine_read_roots: {raw!r} must be an absolute path")
            out.append(str(path))
        return out

    def _parse_extra_gates(self, value) -> list[ExtraGate]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigError("[[gates.extra]] must be an array of tables")
        out = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ConfigError("[[gates.extra]] entries must be tables")
            _check_keys(entry, _EXTRA_GATE_KEYS, "[[gates.extra]]")
            name = entry.get("name")
            run = entry.get("run")
            if not name or not run:
                raise ConfigError("[[gates.extra]] needs both name and run")
            out.append(ExtraGate(
                name=str(name),
                run=str(run),
                via=_as_choice(entry.get("via"), f"[[gates.extra]] {name} via",
                               "runner", ("runner", "local")),
                paths=_as_str_list(entry.get("paths", []),
                                   f"[[gates.extra]] {name} paths"),
                on_fail=_as_choice(entry.get("on_fail"),
                                   f"[[gates.extra]] {name} on_fail", "block",
                                   ("ineligible", "block")),
            ))
        return out

    def _role_model(self, value, role: str, default_model: str = "") -> RoleModel:
        default_temp = 0.35 if role == "checker" else 0.2
        default_vary = role == "checker"
        if value is None:
            return RoleModel(model=default_model, temperature=default_temp,
                             vary_seed=default_vary)
        if isinstance(value, str):
            return RoleModel(model=value or default_model,
                             temperature=default_temp,
                             vary_seed=default_vary)
        if not isinstance(value, dict):
            raise ConfigError(f"[model] {role} must be a table or a model name")
        _check_keys(value, _ROLE_MODEL_KEYS, f"[model.{role}]")
        raw_model = value.get("model")
        model_name = str(raw_model) if raw_model is not None else default_model
        if not model_name and default_model:
            model_name = default_model
        efforts = {}
        for key in ("effort", "retry_effort"):
            raw_effort = value.get(key)
            if raw_effort is None:
                efforts[key] = ""
                continue
            if not isinstance(raw_effort, str):
                raise ConfigError(f"[model] {role}.{key} must be a string")
            if any(ch.isspace() for ch in raw_effort) or "\x00" in raw_effort:
                raise ConfigError(
                    f"[model] {role}.{key} {raw_effort!r} must not contain whitespace"
                )
            efforts[key] = raw_effort
        raw_ladder = value.get("effort_ladder")
        ladder: tuple[str, ...] = ()
        if raw_ladder is not None:
            ladder_list = _as_str_list(raw_ladder, f"[model] {role}.effort_ladder")
            for step in ladder_list:
                if any(ch.isspace() for ch in step) or "\x00" in step or not step:
                    raise ConfigError(
                        f"[model] {role}.effort_ladder contains invalid effort {step!r}"
                    )
            ladder = tuple(ladder_list)
        effort = efforts["effort"]
        return RoleModel(
            model=model_name,
            temperature=_as_float(value.get("temperature"),
                                  f"[model] {role}.temperature", default_temp),
            vary_seed=_as_bool(value.get("vary_seed"),
                               f"[model] {role}.vary_seed", default_vary),
            effort=effort,
            retry_effort=efforts["retry_effort"],
            effort_ladder=ladder,
        )

    def _parse_tiers(self, raw_tiers, default_model: str) -> dict[str, TierConfig]:
        if raw_tiers is None:
            raw_tiers = {}
        if not isinstance(raw_tiers, dict):
            raise ConfigError("[tiers] must be a table")
        if not raw_tiers:
            return {
                "default": TierConfig(
                    name="default",
                    model=default_model,
                    efforts=DEFAULT_EFFORTS,
                    effort_param="reasoning_effort",
                )
            }
        tiers: dict[str, TierConfig] = {}
        for tier_name, entry in raw_tiers.items():
            if (not isinstance(tier_name, str) or not tier_name
                    or any(ch.isspace() for ch in tier_name)):
                raise ConfigError(f"[tiers] invalid tier name {tier_name!r}")
            if not isinstance(entry, dict):
                raise ConfigError(f"[tiers.{tier_name}] must be a table")
            for forbidden in _FORBIDDEN_TIER_KEYS:
                if forbidden in entry:
                    raise ConfigError(
                        f"[tiers.{tier_name}] cannot set {forbidden!r}: tiers share "
                        f"[model]'s endpoint, key_file and headers"
                    )
            _check_keys(entry, _TIER_KEYS, f"[tiers.{tier_name}]")
            raw_model = entry.get("model")
            if not isinstance(raw_model, str) or not raw_model.strip():
                raise ConfigError(
                    f"[tiers.{tier_name}] model is required and must be a non-empty string"
                )
            model_name = raw_model.strip()

            raw_ep = entry.get("effort_param")
            if raw_ep is None and "effort_param" not in entry:
                effort_param = "reasoning_effort"
            else:
                if not isinstance(raw_ep, str):
                    raise ConfigError(f"[tiers.{tier_name}] effort_param must be a string")
                effort_param = raw_ep
                if effort_param != "":
                    if any(ch.isspace() for ch in effort_param) or "\x00" in effort_param:
                        raise ConfigError(
                            f"[tiers.{tier_name}] effort_param must not contain whitespace"
                        )
                    if effort_param in _RESERVED_EFFORT_PARAMS:
                        raise ConfigError(
                            f"[tiers.{tier_name}] effort_param cannot use reserved key "
                            f"{effort_param!r}"
                        )

            if "efforts" not in entry or entry["efforts"] is None:
                efforts = DEFAULT_EFFORTS if effort_param else ()
            else:
                raw_efforts = _as_str_list(entry["efforts"], f"[tiers.{tier_name}] efforts")
                for item in raw_efforts:
                    if not item or any(ch.isspace() for ch in item) or "\x00" in item:
                        raise ConfigError(
                            f"[tiers.{tier_name}] efforts: invalid effort value {item!r}"
                        )
                efforts = tuple(dict.fromkeys(raw_efforts))

            tiers[tier_name] = TierConfig(
                name=tier_name,
                model=model_name,
                efforts=efforts,
                effort_param=effort_param,
            )
        return tiers

    def _parse_swarm(self, sw: dict) -> SwarmConfig:
        enabled = _as_bool(sw.get("enabled"), "[swarm] enabled", False)
        first_tier = next(iter(self.tiers))
        raw_default = sw.get("default_tier")
        if raw_default is None:
            default_tier = first_tier
        else:
            if not isinstance(raw_default, str) or raw_default not in self.tiers:
                raise ConfigError(
                    f"[swarm] default_tier {raw_default!r} is not a configured tier"
                )
            default_tier = raw_default
        default_tier_obj = self.tiers[default_tier]

        raw_coord = sw.get("coordinator")
        coordinator = "code" if raw_coord is None else raw_coord
        if not isinstance(coordinator, str):
            raise ConfigError("[swarm] coordinator must be a string")
        coord_tier: TierConfig | None = None
        coord_acp = coordinator in ACP_ADAPTER_NAMES
        if coordinator in ("code", "external") or coord_acp:
            pass
        elif coordinator.startswith("tier:"):
            tname = coordinator[5:]
            if not tname or tname not in self.tiers:
                raise ConfigError(
                    f"[swarm] coordinator references unknown tier {tname!r}"
                )
            coord_tier = self.tiers[tname]
        else:
            acp_list = ", ".join(repr(n) for n in ACP_ADAPTER_NAMES)
            raise ConfigError(
                f"[swarm] coordinator {coordinator!r} must be 'code', 'external', 'tier:<name>', or {acp_list}"
            )

        raw_sup = sw.get("supervisor")
        supervisor = "config" if raw_sup is None else raw_sup
        if not isinstance(supervisor, str):
            raise ConfigError("[swarm] supervisor must be a string")
        sup_tier: TierConfig | None = None
        sup_acp = supervisor in ACP_ADAPTER_NAMES
        if supervisor in ("config", "external", "off") or sup_acp:
            pass
        elif supervisor.startswith("tier:"):
            tname = supervisor[5:]
            if not tname or tname not in self.tiers:
                raise ConfigError(
                    f"[swarm] supervisor references unknown tier {tname!r}"
                )
            sup_tier = self.tiers[tname]
        else:
            acp_list = ", ".join(repr(n) for n in ACP_ADAPTER_NAMES)
            raise ConfigError(
                f"[swarm] supervisor {supervisor!r} must be 'config', 'external', 'off', 'tier:<name>', or {acp_list}"
            )

        coordinator_effort = self._seat_effort(
            sw, "coordinator_effort", coord_tier, default_tier_obj, is_acp=coord_acp
        )
        supervisor_effort = self._seat_effort(
            sw, "supervisor_effort", sup_tier, default_tier_obj, is_acp=sup_acp
        )

        escalate_after = _as_int(sw.get("escalate_after"), "[swarm] escalate_after", 2)
        if escalate_after < 1:
            raise ConfigError("[swarm] escalate_after must be >= 1")
        handbacks_per_card = _nonneg_int(
            sw.get("handbacks_per_card"), "[swarm] handbacks_per_card", 2
        )
        max_members = _as_int(sw.get("max_members"), "[swarm] max_members", 24)
        if max_members < 1:
            raise ConfigError("[swarm] max_members must be >= 1")
        coord_calls = _nonneg_int(
            sw.get("coordinator_calls_per_run"), "[swarm] coordinator_calls_per_run", 60
        )
        coord_tokens = _nonneg_int(
            sw.get("coordinator_tokens_per_run"),
            "[swarm] coordinator_tokens_per_run",
            600_000,
        )

        raw_wait = sw.get("external_wait")
        if isinstance(raw_wait, bool):
            raise ConfigError("[swarm] external_wait must be a duration")
        external_wait_s = parse_duration(
            "30m" if raw_wait is None else raw_wait, "[swarm] external_wait"
        )
        if not math.isfinite(external_wait_s) or external_wait_s <= 0:
            raise ConfigError("[swarm] external_wait must be > 0")

        on_external_timeout = _as_choice(
            sw.get("on_external_timeout"),
            "[swarm] on_external_timeout",
            "fallback",
            ("fallback", "block"),
        )

        raw_stall = sw.get("stall_after")
        if isinstance(raw_stall, bool):
            raise ConfigError("[swarm] stall_after must be a duration")
        stall_after_s = parse_duration(
            "5m" if raw_stall is None else raw_stall, "[swarm] stall_after"
        )
        if not math.isfinite(stall_after_s) or stall_after_s <= 0:
            raise ConfigError("[swarm] stall_after must be > 0")

        return SwarmConfig(
            enabled=enabled,
            coordinator=coordinator,
            supervisor=supervisor,
            coordinator_effort=coordinator_effort,
            supervisor_effort=supervisor_effort,
            default_tier=default_tier,
            escalate_after=escalate_after,
            handbacks_per_card=handbacks_per_card,
            max_members=max_members,
            coordinator_calls_per_run=coord_calls,
            coordinator_tokens_per_run=coord_tokens,
            external_wait_s=external_wait_s,
            on_external_timeout=on_external_timeout,
            stall_after_s=stall_after_s,
        )

    def _seat_effort(
        self,
        sw: dict,
        key: str,
        seat_tier: TierConfig | None,
        default_tier: TierConfig,
        *,
        is_acp: bool = False,
    ) -> str:
        explicit = key in sw and sw[key] is not None
        if explicit:
            raw = sw[key]
            if not isinstance(raw, str):
                raise ConfigError(f"[swarm] {key} must be a string")
            if any(ch.isspace() for ch in raw) or "\x00" in raw:
                raise ConfigError(f"[swarm] {key} {raw!r} must not contain whitespace")
            effort = raw
        elif seat_tier is not None and not seat_tier.efforts and seat_tier.effort_param == "":
            effort = ""
        else:
            effort = "max"

        if is_acp:
            if explicit and effort and effort not in _ACP_EFFORTS:
                raise ConfigError(
                    f"[swarm] {key} {effort!r} is not one of {list(_ACP_EFFORTS)!r}"
                )
            return effort

        if seat_tier is not None or explicit:
            target = seat_tier if seat_tier is not None else default_tier
            if effort and effort not in target.efforts:
                raise ConfigError(
                    f"[swarm] {key} {effort!r} is not in tier {target.name!r} "
                    f"efforts {list(target.efforts)!r}"
                )
        return effort

    def _outside_repo(self, value, what: str) -> Path:
        if not value:
            raise ConfigError(f"{what} is required")
        raw = str(value)
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = (self.repo / path)
        resolved = path.resolve()
        if resolved == self.repo or self.repo in resolved.parents:
            raise ConfigError(
                f"{what} must sit outside the repository, but {resolved} is inside it")
        return resolved

    # -- derived values -------------------------------------------------------

    def tier_for_model(self, model_name: str) -> TierConfig:
        """Return the tier matching `model_name`, falling back to `default_tier`."""
        default = self.tiers[self.swarm.default_tier]
        if model_name:
            if default.model == model_name:
                return default
            for tier in self.tiers.values():
                if tier.model == model_name:
                    return tier
        return default

    def member_defaults(self, role: str) -> tuple[str, str]:
        """`(tier name, model)` for a swarm member nobody named a tier for.

        A role with its own model in `[model]` (one that differs from
        `[model] implementer`, such as an independent checker) keeps that
        model, exactly as with the swarm off; its tier is the one that serves
        that model, else `[swarm] default_tier`, which also gives the
        `effort_param`. Every other role takes `[swarm] default_tier`. A tier
        named by the coordinator (`--team`, `swarm set`) replaces both.
        """
        aliases = {
            "checker_pass1": "checker",
            "checker_pass2": "checker",
            "test-writer": "test_writer",
        }
        key = aliases.get(role, role)
        default = self.tiers[self.swarm.default_tier]
        role_model = self.role_models.get(key)
        implementer = self.role_models.get("implementer")
        base_model = implementer.model if implementer is not None else ""
        if (
            key != "implementer"
            and role_model is not None
            and role_model.model
            and role_model.model != base_model
        ):
            return (self.tier_for_model(role_model.model).name, role_model.model)
        return (default.name, default.model)

    def effort_for_role(self, role: str, attempt: int = 1) -> tuple[str, str]:
        """Return `(effort, effort_param)` for a configured role, or `("", "")`.

        From the second attempt on, the role's `retry_effort` replaces `effort`.
        """
        aliases = {
            "checker_pass1": "checker",
            "checker_pass2": "checker",
            "test-writer": "test_writer",
        }
        role_model = self.role_models.get(aliases.get(role, role))
        if role_model is None:
            role_model = self.role_models.get("implementer")
        effort = role_model.effort_for_attempt(attempt) if role_model is not None else ""
        if not effort and self.effort_ladder:
            idx = min(max(int(attempt), 1) - 1, len(self.effort_ladder) - 1)
            effort = self.effort_ladder[idx]
        if not effort:
            return ("", "")
        if is_acp_adapter(self.model_adapter):
            return (effort, "effort")
        tier = self.tier_for_model(role_model.model)
        if not tier.effort_param:
            return ("", "")
        return (effort, tier.effort_param)

    def known_failing_hash(self) -> str:
        """The cache key component that changes when `known_failing` changes."""
        return hashlib.sha256(
            "\n".join(sorted(self.known_failing)).encode("utf-8")).hexdigest()

    def env_hash(self, unit: str) -> str:
        """The cache key component for a unit's environment."""
        payload = {"unit": unit, "env": self.runner_env.get(unit, {}),
                   "cargo_flags": self.cargo_flags, "languages": self.languages}
        if self.runner_python:
            payload["python"] = self.runner_python
        if _uses_typescript(self.languages):
            payload["typescript"] = (self.runner_typescript, self.typescript_typecheck,
                                     self.typescript_format)
        return hashlib.sha256(
            str(sorted(payload.items())).encode("utf-8")).hexdigest()[:16]

    def config_hash(self) -> str:
        if self.path and self.path.exists():
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        return hashlib.sha256(
            repr(sorted(self._data.items())).encode("utf-8")).hexdigest()

    def is_never_touch(self, path: str, globs=None) -> bool:
        from .globs import matches_any
        return matches_any(path, self.never_touch if globs is None else globs)

    def is_forbidden(self, path: str) -> bool:
        from .globs import matches_any
        return matches_any(path, self.forbidden_files)

    def __repr__(self) -> str:
        return f"Config({self.name!r}, {self.base_branch!r})"


def apply_supervision_profile(cfg: Config, profile: str) -> None:
    """Apply a named supervision preset (`lean`, `balanced`, or `strict`) to `cfg`."""
    name = str(profile or "").strip().lower()
    if name not in SUPERVISION_PROFILES:
        raise ConfigError(
            f"profile {profile!r} must be one of {list(SUPERVISION_PROFILES)!r}"
        )
    if name == "lean":
        cfg.micromanager.shape_when = "new-symbols"
        cfg.micromanager.pre_review_when = "on-flag"
        cfg.checker_mode = "single-pass"
        cfg.counter_tests = "reject"
        cfg.mutation = "report"
        cfg.runner_confine = "off"
    elif name == "balanced":
        cfg.micromanager.shape_when = "always"
        cfg.micromanager.pre_review_when = "always"
        cfg.checker_mode = "two-pass"
        cfg.counter_tests = "reject"
        cfg.mutation = "report"
        cfg.runner_confine = "off"
    elif name == "strict":
        cfg.micromanager.shape_when = "always"
        cfg.micromanager.pre_review_when = "always"
        cfg.checker_mode = "two-pass"
        cfg.counter_tests = "reject"
        cfg.mutation = "required"
        cfg.runner_confine = "bwrap"
        cfg.runner_confine_network = "off"


def load_config(repo: Path, path: Path | None = None) -> Config:
    """Read and validate the config for a repository."""
    repo = Path(repo).resolve()
    config_path = Path(path) if path else repo / CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"no config at {config_path}")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: not a TOML table")
    return Config(data, repo, config_path)
