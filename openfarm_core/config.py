"""Reading and validating `.openfarm/config.toml`.

One file per repository, on its base branch. Everything project-specific lives
here; the core names no project. Unknown top-level tables are an error, because a
silently ignored key is a gate whose meaning the operator thinks they changed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import tomllib
from pathlib import Path

from .errors import ConfigError
from .globs import escapes, resolves_within

CONFIG_PATH = Path(".openfarm") / "config.toml"

#: Tables a repo may have at the top level of the config.
KNOWN_TABLES = {
    "project", "adapters", "model", "policy", "eligibility", "brief",
    "runner", "budget", "gates", "cards", "micromanager", "state", "harness",
}

_TABLE_KEYS = {
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
_ROLE_MODEL_KEYS = {"model", "temperature", "vary_seed"}
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

    __slots__ = ("model", "temperature", "vary_seed")

    def __init__(self, model: str = "", temperature: float = 0.2,
                 vary_seed: bool = False) -> None:
        self.model = model
        self.temperature = temperature
        self.vary_seed = vary_seed

    def as_dict(self) -> dict:
        return {"model": self.model, "temperature": self.temperature,
                "vary_seed": self.vary_seed}


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
        "auth_file", "isolate_home", "shape_cards", "scaffold_contracts",
        "coach_on_stall", "adjudicate_counter_tests", "pre_review",
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


VALID_HARNESS_CAPTURE_ON = ("retry_recovery", "fail", "counter_fail", "pass")
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
    editable_surfaces: tuple[str, ...] = (
        "test_writer_guidance",
        "implementer_guidance",
        "planner_guidance",
        "context_hints",
        "micromanager_context",
    )

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
            "editable_surfaces": list(self.editable_surfaces),
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
            "off", ("off", "claude-acp", "scripted"))
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
            budget.get("implementer_attempts"), "[budget] implementer_attempts", 3)
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
        self.mutation_min_kill_rate = _as_float(
            gates.get("mutation_min_kill_rate"),
            "[gates] mutation_min_kill_rate", 0.8)
        self.counter_tests = _as_choice(
            gates.get("counter_tests"), "[gates] counter_tests", "report",
            ("report", "reject"))
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
            editable_surfaces=editable_surfaces,
        )

    def _parse_micromanager(self, mm: dict) -> MicromanagerConfig:
        if "timeout_s" in mm and "timeout" in mm:
            raise ConfigError("[micromanager] specify either timeout_s or timeout, not both")
        raw_timeout = mm.get("timeout_s", mm.get("timeout", 180.0))
        timeout_s = parse_duration(raw_timeout, "[micromanager] timeout_s")
        if timeout_s <= 0:
            raise ConfigError("[micromanager] timeout_s must be > 0")
        auth_file = mm.get("auth_file")
        resolved_auth = (
            self._outside_repo(auth_file, "[micromanager] auth_file")
            if auth_file else None
        )
        return MicromanagerConfig(
            mode=_as_choice(mm.get("mode"), "[micromanager] mode",
                            "active", ("off", "advisory", "active")),
            binary=str(mm.get("binary", "claude-agent-acp")),
            args=_as_str_list(mm.get("args", []), "[micromanager] args"),
            model=str(mm.get("model", "")),
            effort=_as_choice(mm.get("effort"), "[micromanager] effort",
                              "medium", ("low", "medium", "high")),
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
        )

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
        return RoleModel(
            model=model_name,
            temperature=_as_float(value.get("temperature"),
                                  f"[model] {role}.temperature", default_temp),
            vary_seed=_as_bool(value.get("vary_seed"),
                               f"[model] {role}.vary_seed", default_vary),
        )

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

    def known_failing_hash(self) -> str:
        """The cache key component that changes when `known_failing` changes."""
        return hashlib.sha256(
            "\n".join(sorted(self.known_failing)).encode("utf-8")).hexdigest()

    def env_hash(self, unit: str) -> str:
        """The cache key component for a unit's environment."""
        payload = {"unit": unit, "env": self.runner_env.get(unit, {}),
                   "cargo_flags": self.cargo_flags, "languages": self.languages}
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
