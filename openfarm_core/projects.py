"""Global Openfarm projects registry (`~/.config/openfarm/projects.toml`).

Tracks Openfarm projects across directories so the TUI and CLI can list, switch,
edit, and remove projects explicitly instead of guessing solely from `cwd`.

Safety invariant: `remove_project()` NEVER touches or deletes user project files
inside `repo/`. Optional `clear_state_dir=True` only deletes Openfarm's external
state cache strictly inside `allowed_state_root` (default `~/.openfarm`) and
strictly disjoint from `repo/`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from .config import CONFIG_PATH, load_config
from .errors import ConfigError, Refused
from .state import utc_now

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


def default_projects_path() -> Path:
    """Resolve the global `projects.toml` path (overridable via `OPENFARM_PROJECTS_FILE`)."""
    env = os.environ.get("OPENFARM_PROJECTS_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "openfarm" / "projects.toml"


def _slugify(raw: str) -> str:
    cleaned = _SLUG_RE.sub("-", str(raw or "").strip().lower()).strip("-")
    return cleaned[:48] or "project"


def _toml_str(value: str) -> str:
    return json.dumps(str(value))


def _load_raw_registry(path: Path | None = None) -> dict[str, dict]:
    target = Path(path).expanduser() if path is not None else default_projects_path()
    if not target.is_file() or target.is_symlink():
        return {}
    try:
        parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    projects = parsed.get("projects")
    if not isinstance(projects, dict):
        return {}
    result: dict[str, dict] = {}
    for pid, entry in projects.items():
        if not isinstance(pid, str) or not isinstance(entry, dict):
            continue
        repo = str(entry.get("repo") or "").strip()
        if not repo:
            continue
        result[pid] = {
            "id": pid,
            "name": str(entry.get("name") or pid).strip() or pid,
            "repo": repo,
            "state_dir": str(entry.get("state_dir") or "").strip(),
            "language": str(entry.get("language") or "").strip(),
            "base_branch": str(entry.get("base_branch") or "main").strip() or "main",
            "idea": str(entry.get("idea") or "").strip(),
            "last_opened": str(entry.get("last_opened") or "").strip(),
        }
    return result


def _save_raw_registry(entries: dict[str, dict], path: Path | None = None) -> Path:
    target = Path(path).expanduser() if path is not None else default_projects_path()
    if target.is_symlink():
        raise Refused("symlink-projects", f"refusing to overwrite symlink {target}")

    lines = [
        "# Openfarm global projects registry (v0.10.1).",
        "# Removing an entry here NEVER deletes user project files on disk.",
        "",
    ]
    for pid in sorted(entries):
        item = entries[pid]
        lines.append(f"[projects.{_toml_str(pid)}]")
        lines.append(f"name = {_toml_str(item.get('name') or pid)}")
        lines.append(f"repo = {_toml_str(item.get('repo') or '')}")
        lines.append(f"state_dir = {_toml_str(item.get('state_dir') or '')}")
        lines.append(f"language = {_toml_str(item.get('language') or '')}")
        lines.append(f"base_branch = {_toml_str(item.get('base_branch') or 'main')}")
        lines.append(f"idea = {_toml_str(item.get('idea') or '')}")
        lines.append(f"last_opened = {_toml_str(item.get('last_opened') or '')}")
        lines.append("")
    text = "\n".join(lines)

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    if str(resolved_parent) not in ("/", "/tmp", "/var/tmp"):
        os.chmod(resolved_parent, 0o700)

    fd, tmp_name = tempfile.mkstemp(prefix=".openfarm-projects-", dir=str(resolved_parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target


def _read_repo_meta(repo_path: Path) -> dict:
    meta: dict = {"name": "", "state_dir": "", "language": "", "base_branch": ""}
    cfg_file = repo_path / CONFIG_PATH
    if cfg_file.is_file() and not cfg_file.is_symlink():
        try:
            data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        proj = data.get("project") if isinstance(data, dict) else {}
        adapters = data.get("adapters") if isinstance(data, dict) else {}
        if isinstance(proj, dict):
            meta["name"] = str(proj.get("name") or "").strip()
            meta["base_branch"] = str(proj.get("base_branch") or "").strip()
            sd = str(proj.get("state_dir") or "").strip()
            if sd:
                meta["state_dir"] = str(Path(sd).expanduser().resolve())
        if isinstance(adapters, dict):
            langs = adapters.get("languages")
            if isinstance(langs, list) and langs:
                meta["language"] = str(langs[0]).strip()
            elif isinstance(adapters.get("language"), str):
                meta["language"] = adapters["language"].strip()
    return meta


def _enrich_entry(entry: dict) -> dict:
    repo_path = Path(entry["repo"]).expanduser()
    state_str = str(entry.get("state_dir") or "").strip()
    state_path = Path(state_str).expanduser() if state_str else None
    repo_exists = repo_path.is_dir()
    configured = repo_exists and (repo_path / CONFIG_PATH).is_file()
    state_exists = bool(state_path is not None and state_path.is_dir())
    out = dict(entry)
    out["repo_exists"] = repo_exists
    out["configured"] = configured
    out["state_exists"] = state_exists
    return out


def _is_ephemeral_tmp_path(repo_path: Path) -> bool:
    """True when `repo_path` lives inside a temporary/test directory that should not auto-pollute the user's registry."""
    resolved = repo_path.expanduser().resolve()
    tmp_roots = [Path("/tmp").resolve(), Path("/var/tmp").resolve(), Path(tempfile.gettempdir()).resolve()]
    for root in tmp_roots:
        if resolved == root or resolved.is_relative_to(root):
            return True
    for part in resolved.parts:
        if part.startswith(("openfarm-tests-", "tmp-v0", "openfarm-v0")):
            return True
    return False


def register_project(
    repo: Path | str,
    *,
    project_id: str = "",
    name: str = "",
    state_dir: Path | str | None = None,
    language: str = "",
    base_branch: str = "",
    idea: str = "",
    auto: bool = False,
    path: Path | None = None,
) -> dict:
    """Register or refresh a project in `projects.toml` and return its entry."""
    repo_path = Path(repo).expanduser().resolve()
    meta = _read_repo_meta(repo_path)

    # Never auto-pollute the user's default ~/.config/openfarm/projects.toml from temp/test dirs.
    if auto and path is None and not os.environ.get("OPENFARM_PROJECTS_FILE", "").strip() and _is_ephemeral_tmp_path(repo_path):
        pid = _slugify(project_id or name or meta["name"] or repo_path.name)
        return _enrich_entry({
            "id": pid,
            "name": str(name or meta["name"] or repo_path.name).strip() or pid,
            "repo": str(repo_path),
            "state_dir": str(Path(state_dir).expanduser().resolve()) if state_dir else meta["state_dir"],
            "language": str(language or meta["language"] or "").strip(),
            "base_branch": str(base_branch or meta["base_branch"] or "main").strip() or "main",
            "idea": str(idea or "").strip(),
            "last_opened": utc_now(),
        })

    entries = _load_raw_registry(path)

    existing_id = ""
    for pid, item in entries.items():
        if Path(item["repo"]).expanduser().resolve() == repo_path:
            existing_id = pid
            break

    if existing_id and not project_id:
        pid = existing_id
    else:
        base_pid = _slugify(project_id or name or meta["name"] or repo_path.name)
        pid = base_pid
        suffix = 2
        while pid in entries and Path(entries[pid]["repo"]).expanduser().resolve() != repo_path:
            pid = f"{base_pid}-{suffix}"
            suffix += 1

    prev = entries.get(pid, {})
    resolved_state = ""
    if state_dir is not None and str(state_dir).strip():
        resolved_state = str(Path(state_dir).expanduser().resolve())
    elif meta["state_dir"]:
        resolved_state = meta["state_dir"]
    elif prev.get("state_dir"):
        resolved_state = str(prev["state_dir"])

    entries[pid] = {
        "id": pid,
        "name": str(name or prev.get("name") or meta["name"] or repo_path.name).strip() or pid,
        "repo": str(repo_path),
        "state_dir": resolved_state,
        "language": str(language or meta["language"] or prev.get("language") or "").strip(),
        "base_branch": str(base_branch or meta["base_branch"] or prev.get("base_branch") or "main").strip() or "main",
        "idea": str(idea or prev.get("idea") or "").strip(),
        "last_opened": utc_now(),
    }
    _save_raw_registry(entries, path)
    return _enrich_entry(entries[pid])


def list_projects(*, path: Path | None = None) -> list[dict]:
    """List all registered Openfarm projects (most recently opened first)."""
    entries = _load_raw_registry(path)
    items = [_enrich_entry(item) for item in entries.values()]
    items.sort(key=lambda item: (item.get("last_opened") or "", item["id"]), reverse=True)
    return items


def get_project(project_id: str, *, path: Path | None = None) -> dict | None:
    entries = _load_raw_registry(path)
    item = entries.get(str(project_id or "").strip())
    return _enrich_entry(item) if item is not None else None


def _update_project_section_in_config(repo_path: Path, updates: dict[str, str]) -> None:
    """Safely update ONLY `[project]` metadata keys (never touching `[policy]` or gates)."""
    allowed_keys = {"name", "base_branch"}
    filtered = {k: str(v).strip() for k, v in updates.items() if k in allowed_keys and str(v).strip()}
    if not filtered:
        return
    cfg_file = repo_path / CONFIG_PATH
    if not cfg_file.is_file() or cfg_file.is_symlink():
        return

    original = cfg_file.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    out_lines: list[str] = []
    in_project = False
    seen: set[str] = set()
    insert_idx: int | None = None

    for line in lines:
        sec_m = _SECTION_RE.match(line)
        if sec_m:
            if in_project:
                for key, val in filtered.items():
                    if key not in seen:
                        out_lines.append(f"{key} = {_toml_str(val)}\n")
                        seen.add(key)
            in_project = sec_m.group(1).strip() == "project"
            out_lines.append(line)
            if in_project:
                insert_idx = len(out_lines)
            continue

        if in_project:
            kv_m = _KV_RE.match(line)
            if kv_m and kv_m.group(2) in filtered:
                key = kv_m.group(2)
                nl = "\n" if line.endswith("\n") else ""
                out_lines.append(f"{kv_m.group(1)}{key}{kv_m.group(3)}{_toml_str(filtered[key])}{nl}")
                seen.add(key)
                continue
        out_lines.append(line)

    if in_project:
        for key, val in filtered.items():
            if key not in seen:
                out_lines.append(f"{key} = {_toml_str(val)}\n")
                seen.add(key)
    elif insert_idx is not None:
        missing = [f"{k} = {_toml_str(v)}\n" for k, v in filtered.items() if k not in seen]
        out_lines[insert_idx:insert_idx] = missing

    updated_text = "".join(out_lines)
    if updated_text == original:
        return
    cfg_file.write_text(updated_text, encoding="utf-8")
    try:
        load_config(repo_path, cfg_file)
    except (ConfigError, OSError):
        cfg_file.write_text(original, encoding="utf-8")
        raise


def edit_project(
    project_id: str,
    *,
    name: str | None = None,
    repo: Path | str | None = None,
    language: str | None = None,
    base_branch: str | None = None,
    idea: str | None = None,
    path: Path | None = None,
) -> dict:
    """Update project metadata in `projects.toml` (and `[project]` in config.toml when applicable)."""
    pid = str(project_id or "").strip()
    entries = _load_raw_registry(path)
    if pid not in entries:
        raise Refused("project-not-found", f"no registered project {pid!r}")

    item = dict(entries[pid])
    if name is not None and name.strip():
        item["name"] = name.strip()
    if repo is not None and str(repo).strip():
        item["repo"] = str(Path(repo).expanduser().resolve())
    if language is not None:
        item["language"] = language.strip()
    if base_branch is not None and base_branch.strip():
        item["base_branch"] = base_branch.strip()
    if idea is not None:
        item["idea"] = idea.strip()
    item["last_opened"] = utc_now()

    repo_path = Path(item["repo"]).expanduser()
    cfg_updates: dict[str, str] = {}
    if base_branch is not None and base_branch.strip():
        cfg_updates["base_branch"] = item["base_branch"]
    if name is not None and name.strip():
        cfg_updates["name"] = item["name"]
    if cfg_updates and repo_path.is_dir():
        _update_project_section_in_config(repo_path, cfg_updates)

    entries[pid] = item
    _save_raw_registry(entries, path)
    return _enrich_entry(item)


def remove_project(
    project_id: str,
    *,
    clear_state_dir: bool = False,
    allowed_state_root: Path | str | None = None,
    path: Path | None = None,
) -> dict:
    """Remove a project from Openfarm's registry WITHOUT touching any project files on disk.

    If `clear_state_dir=True`, Openfarm's external state cache (e.g. `~/.openfarm/<slug>`)
    is deleted ONLY when verified to live strictly inside `allowed_state_root` (default
    `~/.openfarm`) and strictly disjoint from the project's repository directory.
    """
    pid = str(project_id or "").strip()
    entries = _load_raw_registry(path)
    if pid not in entries:
        raise Refused("project-not-found", f"no registered project {pid!r}")

    entry = entries.pop(pid)
    _save_raw_registry(entries, path)

    state_cleared = False
    state_str = str(entry.get("state_dir") or "").strip()
    if clear_state_dir and state_str:
        raw_sd = Path(state_str).expanduser()
        if not raw_sd.is_symlink() and raw_sd.is_dir():
            sd = raw_sd.resolve()
            root = (
                Path(allowed_state_root).expanduser().resolve()
                if allowed_state_root is not None
                else (Path.home() / ".openfarm").resolve()
            )
            rp = Path(entry["repo"]).expanduser().resolve()
            inside_allowed_root = sd != root and sd.is_relative_to(root)
            disjoint_from_repo = (
                sd != rp
                and not sd.is_relative_to(rp)
                and not rp.is_relative_to(sd)
            )
            if inside_allowed_root and disjoint_from_repo:
                shutil.rmtree(sd)
                state_cleared = True

    return {
        "id": pid,
        "name": entry.get("name", pid),
        "repo": entry["repo"],
        "removed": True,
        "repo_touched": False,
        "state_dir": state_str,
        "state_cleared": state_cleared,
    }


def _detect_folder_language(folder: Path) -> str:
    if (folder / "Cargo.toml").is_file():
        return "rust"
    if (folder / "package.json").is_file() or (folder / "tsconfig.json").is_file():
        return "typescript"
    if (folder / "go.mod").is_file():
        return "go"
    if (folder / "pyproject.toml").is_file() or (folder / "setup.py").is_file() or (folder / "requirements.txt").is_file():
        return "python"
    return ""


def discover_workspace_projects(
    root: Path | str | None = None,
    *,
    registry_path: Path | None = None,
) -> list[dict]:
    """Read-only 1-level scan of a workspace root (default `~/projects`) for projects."""
    base = Path(root).expanduser().resolve() if root is not None else (Path.home() / "projects").resolve()
    if not base.is_dir():
        return []
    registered_repos = {
        Path(item["repo"]).expanduser().resolve()
        for item in _load_raw_registry(registry_path).values()
    }
    found: list[dict] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
            continue
        resolved = child.resolve()
        if (resolved / CONFIG_PATH).is_file():
            meta = _read_repo_meta(resolved)
            kind = "configured"
            lang = meta.get("language") or _detect_folder_language(resolved)
        elif (resolved / ".git").exists():
            kind = "git-repo"
            lang = _detect_folder_language(resolved)
        else:
            kind = "folder"
            lang = _detect_folder_language(resolved)
        found.append({
            "name": child.name,
            "repo": str(resolved),
            "kind": kind,
            "detected_language": lang,
            "registered": resolved in registered_repos,
        })
    return found


__all__ = [
    "default_projects_path",
    "discover_workspace_projects",
    "edit_project",
    "get_project",
    "list_projects",
    "register_project",
    "remove_project",
]
