"""Reading and validating `.openfarm/config.toml`.

One file per repository, on its base branch. Everything project-specific lives
here; the core names no project. Unknown top-level tables are an error, because a
silently ignored key is a gate whose meaning the operator thinks they changed.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path

from .errors import ConfigError

CONFIG_PATH = Path(".openfarm") / "config.toml"

#: Tables a repo may have at the top level of the config.
KNOWN_TABLES = {
    "project", "adapters", "model", "policy", "eligibility", "brief",
    "runner", "budget", "gates", "cards",
}

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
        self.role_models = {
            role: self._role_model(model.get(role), role)
            for role in ("test_writer", "implementer", "checker", "planner")
        }

        policy = self._table("policy")
        self.never_touch = _as_str_list(policy.get("never_touch", []),
                                        "[policy] never_touch")
        forbidden = _as_str_list(policy.get("forbidden_files", []),
                                 "[policy] forbidden_files")
        # The config is forbidden to the farm whatever the file says: a farm that
        # could edit it could weaken any gate.
        if ".openfarm/**" not in forbidden:
            forbidden.append(".openfarm/**")
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

        brief = self._table("brief")
        conventions = brief.get("test_conventions")
        self.test_conventions_path = Path(conventions) if conventions else None

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
        self.extra_gates = self._parse_extra_gates(gates.get("extra", []))

        cards = self._table("cards")
        self.fields_block = str(cards.get("fields_block", ""))
        self.required_fields = _as_str_list(
            cards.get("required_fields", []), "[cards] required_fields")

    def _parse_extra_gates(self, value) -> list[ExtraGate]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigError("[[gates.extra]] must be an array of tables")
        out = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ConfigError("[[gates.extra]] entries must be tables")
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

    def _role_model(self, value, role: str) -> RoleModel:
        if value is None:
            return RoleModel(model="", temperature=0.2)
        if isinstance(value, str):
            return RoleModel(model=value)
        if not isinstance(value, dict):
            raise ConfigError(f"[model] {role} must be a table or a model name")
        return RoleModel(
            model=str(value.get("model", "")),
            temperature=_as_float(value.get("temperature"),
                                  f"[model] {role}.temperature", 0.2),
            vary_seed=_as_bool(value.get("vary_seed"),
                               f"[model] {role}.vary_seed", False),
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
