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

import os
import tomllib
from pathlib import Path

ROLES = ("architect", "reviewer", "builder")
ROLE_WORDS = {"architect": "Architect", "reviewer": "Reviewer", "builder": "Builders"}
FRONTIER_EFFORTS = ("low", "medium", "high", "xhigh", "max")
BUILDER_EFFORTS = ("low", "medium", "high", "max")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
    return choices


def _config_values(data: dict) -> dict[str, dict[str, str]]:
    mm = data.get("micromanager") if isinstance(data.get("micromanager"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    builder = model.get("implementer") if isinstance(model.get("implementer"), dict) else {}
    ladder = builder.get("effort_ladder") or model.get("effort_ladder") or []
    frontier = {"model": str(mm.get("model") or ""), "effort": str(mm.get("effort") or "medium")}
    return {"architect": dict(frontier), "reviewer": dict(frontier),
            "builder": {"model": str(builder.get("model") or ""),
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
            if isinstance(model.get(name), dict):
                model[name] = dict(model[name], model=builder["model"])
    if builder["effort_from"] != "config":
        # the first try uses the chosen effort; a retry still climbs from there
        ladder = list(BUILDER_EFFORTS[BUILDER_EFFORTS.index(builder["effort"]):])
        model["effort_ladder"] = ladder
        for name in BUILDER_ROLES:
            if isinstance(model.get(name), dict):
                model[name] = {k: v for k, v in model[name].items()
                               if k not in ("effort", "retry_effort", "effort_ladder")}
    data["model"] = model
    return data, resolved
