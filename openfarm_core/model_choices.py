"""Which model does which job, and how hard it thinks.

Three jobs: the `architect` plans, the `reviewer` checks and coaches the builders,
the `builder` writes tests and code. Each can be given a model and an effort in two
places, and the nearest one wins:

1. this project: `.openfarm/models.toml`
2. every project: `models.toml` beside the global `providers.toml`
3. the project's `config.toml`, which is what was there before anyone chose

Both files are written whole by `save_choice`, so `config.toml` and its comments
are never rewritten.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

ROLES = ("architect", "reviewer", "builder")
ROLE_WORDS = {"architect": "Architect", "reviewer": "Reviewer", "builder": "Builders"}
FRONTIER_EFFORTS = ("low", "medium", "high", "xhigh", "max")
BUILDER_EFFORTS = ("low", "medium", "high", "max")
_DEFAULT_TIER_EFFORTS = ("off", "low", "medium", "high", "xhigh", "max")
BUILDER_ROLES = ("test_writer", "implementer", "checker", "planner")
PROJECT_FILE = Path(".openfarm") / "models.toml"


def efforts_for(role: str) -> tuple[str, ...]:
    return BUILDER_EFFORTS if role == "builder" else FRONTIER_EFFORTS


def global_path() -> Path:
    """The file for every project (`OPENFARM_MODELS_FILE` overrides it)."""
    env = os.environ.get("OPENFARM_MODELS_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    from .providers import default_providers_path

    return default_providers_path().parent / "models.toml"


def project_path(repo: Path) -> Path:
    return Path(repo) / PROJECT_FILE


def load_choices(path: Path) -> dict[str, dict[str, str]]:
    """`{role: {"model", "effort"}}` from one file. A missing, broken or odd file is no choices."""
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for role in ROLES:
        table = data.get(role) if isinstance(data, dict) else None
        if not isinstance(table, dict):
            continue
        model = str(table.get("model") or "").strip()
        effort = str(table.get("effort") or "").strip().lower()
        if effort not in efforts_for(role):
            effort = ""
        if model or effort:
            out[role] = {"model": model, "effort": effort}
    return out


def save_choice(path: Path, role: str, *, model: str = "", effort: str = "") -> dict[str, dict[str, str]]:
    """Set one job's model and effort in one file. Both empty removes the choice. Returns the file's choices."""
    if role not in ROLES:
        raise ValueError(f"unknown job {role!r}: one of {', '.join(ROLES)}")
    model, effort = str(model or "").strip(), str(effort or "").strip().lower()
    if effort and effort not in efforts_for(role):
        raise ValueError(f"effort {effort!r} is not one of {', '.join(efforts_for(role))}")
    if any(ch in model for ch in '"\\\n\r'):
        raise ValueError("a model name can't hold quotes, backslashes or line breaks")
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"refusing to write symlink {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise OSError(f"refusing to write symlink {path}")
        choices = load_choices(path)
        if model or effort:
            choices[role] = {"model": model, "effort": effort}
        else:
            choices.pop(role, None)
        lines = ["# Which model does which job. Written by openfarm (`openfarm models`); see docs/models.md.", ""]
        for name in ROLES:
            if name in choices:
                lines.append(f"[{name}]")
                lines += [f'{key} = "{value}"' for key, value in choices[name].items() if value]
                lines.append("")
        payload = "\n".join(lines).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return choices
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _default_tier_name(data: dict) -> str:
    raw_tiers = data.get("tiers")
    if not isinstance(raw_tiers, dict) or not raw_tiers:
        return ""
    sw = data.get("swarm") if isinstance(data.get("swarm"), dict) else {}
    default_tier = sw.get("default_tier")
    if isinstance(default_tier, str) and default_tier in raw_tiers:
        return default_tier
    return next(iter(raw_tiers))


def _tier_allowed_efforts(data: dict, model_name: str) -> tuple[str, ...]:
    from .adapters.acp import is_acp_adapter

    adapters = data.get("adapters") if isinstance(data.get("adapters"), dict) else {}
    if is_acp_adapter(str(adapters.get("model") or "")):
        return FRONTIER_EFFORTS
    raw_tiers = data.get("tiers")
    if not isinstance(raw_tiers, dict) or not raw_tiers:
        return _DEFAULT_TIER_EFFORTS
    default_name = _default_tier_name(data)
    tier = raw_tiers.get(default_name)
    if model_name and not (isinstance(tier, dict) and tier.get("model") == model_name):
        for entry in raw_tiers.values():
            if isinstance(entry, dict) and entry.get("model") == model_name:
                tier = entry
                break
    if not isinstance(tier, dict):
        return _DEFAULT_TIER_EFFORTS
    if "effort_param" in tier and tier["effort_param"] == "":
        return ()
    if "efforts" in tier and tier["efforts"] is not None:
        raw_efforts = tier["efforts"]
        return tuple(str(e) for e in raw_efforts) if isinstance(raw_efforts, list) else ()
    return _DEFAULT_TIER_EFFORTS


def _config_values(data: dict) -> dict[str, dict[str, str]]:
    mm = data.get("micromanager") if isinstance(data.get("micromanager"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    raw_impl = model.get("implementer")
    builder = raw_impl if isinstance(raw_impl, dict) else {}
    impl_model = str(builder.get("model") or "") if isinstance(raw_impl, dict) else (
        str(raw_impl) if isinstance(raw_impl, str) else ""
    )
    if not impl_model:
        default_tier = _default_tier_name(data)
        raw_tiers = data.get("tiers") if isinstance(data.get("tiers"), dict) else {}
        if default_tier and isinstance(raw_tiers.get(default_tier), dict):
            impl_model = str(raw_tiers[default_tier].get("model") or "")
    ladder = builder.get("effort_ladder") or model.get("effort_ladder") or []
    frontier = {"model": str(mm.get("model") or ""), "effort": str(mm.get("effort") or "medium")}
    return {"architect": dict(frontier), "reviewer": dict(frontier),
            "builder": {"model": impl_model,
                        "effort": str(builder.get("effort") or (ladder[0] if ladder else ""))}}


def resolve(data: dict, repo: Path | None) -> dict[str, dict[str, str]]:
    """For each job: its model and effort, and where each came from (`project`, `global` or `config`)."""
    layers = [("global", load_choices(global_path()))]
    if repo is not None:
        layers.append(("project", load_choices(project_path(repo))))
    out = {role: {**values, "model_from": "config", "effort_from": "config"}
           for role, values in _config_values(data).items()}
    for source, choices in layers:
        for role, chosen in choices.items():
            for key in ("model", "effort"):
                if chosen.get(key):
                    out[role][key] = chosen[key]
                    out[role][f"{key}_from"] = source
    return out


def overlay(data: dict, repo: Path) -> tuple[dict, dict[str, dict[str, str]]]:
    """The config data with the choices laid over it, and what was resolved. `data` itself is not changed."""
    resolved = resolve(data, repo)
    if all(v["model_from"] == "config" and v["effort_from"] == "config" for v in resolved.values()):
        return data, resolved
    data = dict(data)
    reviewer = resolved["reviewer"]
    mm = dict(data.get("micromanager") or {})
    if reviewer["model_from"] != "config":
        mm["model"] = reviewer["model"]
    if reviewer["effort_from"] != "config":
        mm["effort"] = reviewer["effort"]
    data["micromanager"] = mm
    builder = resolved["builder"]
    model = dict(data.get("model") or {})
    if builder["model_from"] != "config":
        for name in BUILDER_ROLES:
            entry = model.get(name)
            if isinstance(entry, dict):
                model[name] = dict(entry, model=builder["model"])
            else:
                model[name] = builder["model"]
        default_tier = _default_tier_name(data)
        raw_tiers = data.get("tiers")
        if default_tier and isinstance(raw_tiers, dict) and isinstance(raw_tiers.get(default_tier), dict):
            tiers_copy = dict(raw_tiers)
            tiers_copy[default_tier] = dict(raw_tiers[default_tier], model=builder["model"])
            data["tiers"] = tiers_copy
    if builder["effort_from"] != "config":
        # the first try uses the chosen effort; a retry still climbs from there, bounded by each tier's allowed efforts
        raw_ladder = list(BUILDER_EFFORTS[BUILDER_EFFORTS.index(builder["effort"]):])

        def _role_model_name(name: str) -> str:
            entry = model.get(name)
            if isinstance(entry, dict):
                return str(entry.get("model") or "")
            return str(entry) if isinstance(entry, str) else ""

        impl_name = _role_model_name("implementer")
        allowed_sets = [
            _tier_allowed_efforts(data, _role_model_name(name) or impl_name)
            for name in BUILDER_ROLES
        ]
        ladder = [e for e in raw_ladder if all(e in allowed for allowed in allowed_sets)]
        if ladder:
            model["effort_ladder"] = ladder
            for name in BUILDER_ROLES:
                if isinstance(model.get(name), dict):
                    model[name] = {k: v for k, v in model[name].items()
                                   if k not in ("effort", "retry_effort", "effort_ladder")}
    data["model"] = model
    return data, resolved
