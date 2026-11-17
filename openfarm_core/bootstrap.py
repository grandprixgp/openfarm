"""Project bootstrap (`openfarm init`) and skill bundle installer (`openfarm install-skill`).

Pure Python 3.11+ standard library only so a single repository or package install
can deploy frontier agent skills and scaffold verified project skeletons offline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from .adapters.acp import acp_adapter_for_provider, normalize_acp_provider
from .config_template import render_config
from .config import load_config
from .errors import ConfigError, Invalid, Refused, UsageError
from .git import Git

INSTALL_TARGETS = ("claude", "antigravity", "codex", "agents", "workspace", "all")
PROFILES = ("auto", "solo", "advisory", "active")
MUTATION_MODES = ("auto", "off", "report", "required")

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

#: Rust 2021 strict, reserved, and weak keywords disallowed as crate identifiers.
_RUST_KEYWORDS = frozenset({
    "abstract", "as", "async", "await", "become", "box", "break", "const",
    "continue", "crate", "do", "dyn", "else", "enum", "extern", "false",
    "final", "fn", "for", "gen", "if", "impl", "in", "let", "loop", "macro",
    "match", "mod", "move", "mut", "override", "priv", "pub", "ref", "return",
    "self", "Self", "static", "struct", "super", "trait", "true", "try",
    "type", "typeof", "unsafe", "unsized", "use", "virtual", "where", "while",
    "yield",
})


def skill_assets_root() -> Path:
    """Return the packaged `skill_assets/` directory inside `openfarm_core`."""
    return Path(__file__).resolve().parent / "skill_assets"


def validate_project_name(raw: str) -> tuple[str, str]:
    """Validate `raw` and return `(project_slug, crate_name)`.

    `project_slug` uses lowercase ASCII letters, digits, and `-`. `crate_name`
    uses `_` and is rejected if it matches a Rust keyword.
    """
    text = str(raw or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        raise UsageError(f"invalid project name {raw!r}: must match ^[A-Za-z][A-Za-z0-9_-]{{0,63}}$")
    if not _NAME_RE.match(text):
        raise UsageError(
            f"invalid project name {raw!r}: start with an ASCII letter and use "
            "only letters, digits, '-', or '_' (max 64 chars)"
        )
    slug = text.lower().replace("_", "-").strip("-")
    if not slug or "--" in slug:
        slug = re.sub(r"-{2,}", "-", text.lower().replace("_", "-")).strip("-")
    crate_name = slug.replace("-", "_")
    if not crate_name or not crate_name[0].isalpha():
        raise UsageError(f"invalid project name {raw!r}: derived crate name is invalid")
    if crate_name in _RUST_KEYWORDS:
        raise UsageError(f"invalid project name {raw!r}: {crate_name!r} is a Rust keyword")
    return slug, crate_name


def _copy_tree(src_dir: Path, dst_dir: Path, *, dry_run: bool) -> list[str]:
    """Copy every regular file under `src_dir` into `dst_dir` and return relative paths."""
    if not src_dir.is_dir():
        raise UsageError(f"missing bundled skill asset directory: {src_dir}")
    copied: list[str] = []
    for item in sorted(src_dir.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(src_dir)
        out = dst_dir / rel
        if not dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
        copied.append(str(out))
    return copied


def _copy_file(src_file: Path, dst_file: Path, *, dry_run: bool) -> str:
    """Copy one file into `dst_file` unless `dry_run` is set."""
    if not src_file.is_file():
        raise UsageError(f"missing bundled skill asset file: {src_file}")
    if not dry_run:
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
    return str(dst_file)


def _install_bin_shim(bin_dir: Path, *, dry_run: bool) -> str:
    """Write an executable `openfarm` wrapper script inside `bin_dir`."""
    dest_dir = Path(bin_dir).expanduser().resolve()
    shim_path = dest_dir / "openfarm"
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    body = (
        "#!/usr/bin/env python3\n"
        '"""Openfarm CLI launcher installed by `openfarm install-skill`."""\n\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        f"_PKG_PARENT = Path({pkg_parent!r})\n"
        'if (_PKG_PARENT / "openfarm_core").is_dir() and str(_PKG_PARENT) not in sys.path:\n'
        "    sys.path.insert(0, str(_PKG_PARENT))\n\n"
        "from openfarm_core.cli import main  # noqa: E402\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    )
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shim_path.write_text(body, encoding="utf-8")
        mode = shim_path.stat().st_mode
        shim_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(shim_path)


def install_skill_bundle(
    target: str = "all",
    dest: Path | None = None,
    install_bin: Path | None = None,
    dry_run: bool = False,
    home: Path | None = None,
) -> dict:
    """Install bundled skills, slash command, and plugin files for agent runtimes."""
    if target not in INSTALL_TARGETS:
        raise UsageError(
            f"unknown install target {target!r}: choose one of {', '.join(INSTALL_TARGETS)}"
        )

    assets = skill_assets_root()
    skills_src = assets / "skills"
    commands_src = assets / "commands"
    plugin_src = assets / "plugin"

    base_home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    explicit_dest = Path(dest).expanduser().resolve() if dest is not None else None

    targets_run = ("claude", "antigravity", "codex", "agents") if target == "all" else (target,)
    installed: dict[str, dict[str, list[str]]] = {}

    for item in targets_run:
        entry: dict[str, list[str]] = {"skills": [], "commands": [], "plugin": []}
        if item == "claude":
            claude_root = (
                explicit_dest
                if explicit_dest is not None and target == "claude"
                else (explicit_dest or base_home) / ".claude"
            )
            entry["skills"] = _copy_tree(skills_src, claude_root / "skills", dry_run=dry_run)
            entry["commands"] = _copy_tree(
                commands_src, claude_root / "commands", dry_run=dry_run
            )
        elif item == "antigravity":
            ag_root = (
                explicit_dest
                if explicit_dest is not None and target == "antigravity"
                else (explicit_dest or base_home) / ".gemini" / "config"
            )
            entry["skills"] = _copy_tree(skills_src, ag_root / "skills", dry_run=dry_run)
            plugin_dir = ag_root / "plugins" / "openfarm"
            entry["plugin"] = (
                _copy_tree(plugin_src, plugin_dir, dry_run=dry_run)
                + _copy_tree(skills_src, plugin_dir / "skills", dry_run=dry_run)
            )
        elif item == "codex":
            codex_root = (
                explicit_dest
                if explicit_dest is not None and target == "codex"
                else (explicit_dest or base_home) / ".codex"
            )
            entry["skills"] = _copy_tree(
                skills_src, codex_root / "skills", dry_run=dry_run
            )
            # Codex rules/*.rules are Starlark execpolicy, so place markdown rules at root.
            entry["plugin"] = [
                _copy_file(
                    plugin_src / "rules" / "AGENTS.md",
                    codex_root / "openfarm-AGENTS.md",
                    dry_run=dry_run,
                )
            ]
        elif item == "agents":
            agents_root = (
                explicit_dest
                if explicit_dest is not None and target == "agents"
                else (explicit_dest or base_home) / ".agents"
            )
            entry["skills"] = _copy_tree(skills_src, agents_root / "skills", dry_run=dry_run)
        elif item == "workspace":
            ws_root = explicit_dest if explicit_dest is not None else Path.cwd().resolve()
            entry["skills"] = (
                _copy_tree(skills_src, ws_root / ".agents" / "skills", dry_run=dry_run)
                + _copy_tree(skills_src, ws_root / ".codex" / "skills", dry_run=dry_run)
            )
            entry["commands"] = _copy_tree(
                commands_src, ws_root / ".claude" / "commands", dry_run=dry_run
            )
        installed[item] = entry

    bin_shim = ""
    if install_bin is not None:
        bin_shim = _install_bin_shim(install_bin, dry_run=dry_run)

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "target": target,
        "assets_root": str(assets),
        "installed": installed,
        "bin_shim": bin_shim or None,
    }


def _toml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render_config_toml(
    *,
    project_slug: str,
    base_branch: str,
    state_dir: Path,
    endpoint: str,
    key_file: Path,
    model: str,
    mm_adapter: str,
    mm_mode: str,
    mm_auth: str = "oauth",
    mm_binary: str = "auto",
    mutation: str,
) -> str:
    return render_config(
        name=project_slug, language="rust", base_branch=base_branch,
        state_dir=state_dir, key_file=key_file, endpoint=endpoint, model=model,
        never_touch=["src/invariants.rs"],
        test_conventions=".openfarm/test-conventions.md", cargo_flags=["--locked"],
        mutation=mutation, mm_adapter=mm_adapter, mm_mode=mm_mode, mm_auth=mm_auth,
        mm_binary=mm_binary)


def initialize_project(
    name: str,
    *,
    directory: Path | None = None,
    repo_base: Path | None = None,
    idea: str = "",
    profile: str = "auto",
    acp_provider: str = "claude",
    state_dir: Path | None = None,
    secrets_dir: Path | None = None,
    endpoint: str = "https://api.example.invalid/v1",
    model: str = "lesser-coder",
    key_file: Path | None = None,
    base_branch: str = "main",
    mutation: str = "auto",
    no_git: bool = False,
    commit: bool = False,
    force: bool = False,
) -> dict:
    """Scaffold an initialized Openfarm v0.5 Rust repository and validate its config."""
    project_slug, crate_name = validate_project_name(name)
    if profile not in PROFILES:
        raise UsageError(f"unknown profile {profile!r}: choose one of {', '.join(PROFILES)}")
    try:
        resolved_acp_provider = normalize_acp_provider(acp_provider)
    except Invalid as exc:
        raise UsageError(exc.message) from exc
    if mutation not in MUTATION_MODES:
        raise UsageError(
            f"unknown mutation mode {mutation!r}: choose one of {', '.join(MUTATION_MODES)}"
        )
    ep = str(endpoint or "").strip()
    if not ep or ("\n" in ep) or not (ep.startswith("https://") or ep.startswith("http://")):
        raise UsageError(f"invalid model endpoint {endpoint!r}: must be an HTTP(S) URL")
    branch = str(base_branch or "").strip() or "main"

    if directory is not None:
        target_dir = Path(directory).expanduser().resolve()
    else:
        base_parent = Path(repo_base).expanduser().resolve() if repo_base is not None else Path.cwd().resolve()
        target_dir = (base_parent / project_slug).resolve()

    if target_dir.exists():
        if not target_dir.is_dir():
            raise Refused("not-a-directory", f"{target_dir} exists and is not a directory")
        non_git = [p for p in target_dir.iterdir() if p.name != ".git"]
        if non_git and not force:
            raise Refused(
                "dir-not-empty",
                f"{target_dir} is not empty (pass --force to overwrite scaffold files); "
                f"to add openfarm to an existing project, run: openfarm adopt --dir {target_dir}",
            )

    resolved_state = (
        Path(state_dir).expanduser().resolve()
        if state_dir is not None
        else (Path.home() / ".openfarm" / project_slug).resolve()
    )
    resolved_secrets = (
        Path(secrets_dir).expanduser().resolve()
        if secrets_dir is not None
        else (Path.home() / ".config" / "openfarm").resolve()
    )
    resolved_key = (
        Path(key_file).expanduser().resolve()
        if key_file is not None
        else (resolved_secrets / f"{project_slug}.key").resolve()
    )

    if resolved_state.is_relative_to(target_dir):
        raise ConfigError("[project] state_dir must be outside the repository")
    if resolved_key.is_relative_to(target_dir):
        raise ConfigError("[model] key_file must be outside the repository")

    resolved_profile = "active" if profile == "auto" else profile
    mm_adapter = (
        "off"
        if resolved_profile == "solo"
        else acp_adapter_for_provider(resolved_acp_provider)
    )
    mm_mode = "off" if resolved_profile == "solo" else resolved_profile
    mm_auth = "oauth"
    mm_binary = "auto"

    resolved_mutation = (
        "report" if (mutation == "auto" and shutil.which("cargo-mutants")) else
        "off" if mutation == "auto" else mutation
    )

    target_dir.mkdir(parents=True, exist_ok=True)
    for folder in (
        target_dir / ".openfarm",
        target_dir / "cards",
        target_dir / "src",
        target_dir / "tests",
        resolved_state,
        resolved_key.parent,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved_state, 0o700)
    os.chmod(resolved_key.parent, 0o700)

    acp_label = {"claude": "Claude", "antigravity": "Antigravity", "codex": "Codex"}[resolved_acp_provider]
    acp_cmd_hint = (
        "`openfarm acp status` / `openfarm acp login`"
        if resolved_acp_provider == "claude"
        else f"`openfarm acp status --provider {resolved_acp_provider}` / `openfarm acp login --provider {resolved_acp_provider}`"
    )
    idea_text = str(idea or "").strip() or f"{project_slug} domain application"
    files_to_write: dict[str, str] = {
        "Cargo.toml": (
            "[package]\n"
            f"name = {_toml_str(crate_name)}\n"
            'version = "0.1.0"\n'
            'edition = "2021"\n\n'
            "[dependencies]\n"
        ),
        "Cargo.lock": (
            "# This file is automatically @generated by Cargo.\n"
            "# It is not intended for manual editing.\n"
            "version = 3\n\n"
            "[[package]]\n"
            f"name = {_toml_str(crate_name)}\n"
            'version = "0.1.0"\n'
        ),
        ".gitignore": "/target\n",
        "src/invariants.rs": (
            "//! Domain invariants protected by `[policy] never_touch`.\n"
            "//!\n"
            "//! Openfarm keeps this file out of lesser-model briefs and edits.\n\n"
            "pub const SCHEMA_VERSION: u32 = 1;\n\n"
            "pub fn ensure_non_empty_label(label: &str) -> Result<&str, &'static str> {\n"
            "    let trimmed = label.trim();\n"
            "    if trimmed.is_empty() {\n"
            '        return Err("label must not be empty");\n'
            "    }\n"
            "    Ok(trimmed)\n"
            "}\n"
        ),
        "src/lib.rs": (
            "pub mod invariants;\n\n"
            "pub fn project_name() -> &'static str {\n"
            f"    {_toml_str(project_slug)}\n"
            "}\n"
        ),
        "tests/baseline_tests.rs": (
            f"use {crate_name}::invariants::{{ensure_non_empty_label, SCHEMA_VERSION}};\n"
            f"use {crate_name}::project_name;\n\n"
            "#[test]\n"
            "fn baseline_schema_version_is_positive() {\n"
            "    assert_eq!(SCHEMA_VERSION, 1);\n"
            "    assert!(!project_name().is_empty());\n"
            "}\n\n"
            "#[test]\n"
            "fn baseline_invariants_reject_blank_labels() {\n"
            '    assert_eq!(ensure_non_empty_label("  daily  "), Ok("daily"));\n'
            '    assert_eq!(\n'
            '        ensure_non_empty_label("   "),\n'
            '        Err("label must not be empty")\n'
            '    );\n'
            "}\n"
        ),
        "cards/.gitkeep": "",
        ".openfarm/test-conventions.md": (
            f"# Test Conventions for {project_slug}\n\n"
            "- Place integration tests in `tests/<subject>_tests.rs` (one test file per card subject).\n"
            f"- Reach behavior only through `pub` items exported by crate `{crate_name}`.\n"
            "- Assert exact values and error variants (`assert_eq!`, `matches!`).\n"
            "- Never use `.unwrap()`, `.expect()`, `#[ignore]`, `#[allow(...)]`, or `dbg!()`.\n"
        ),
        ".openfarm/PROJECT_SPEC.md": (
            f"# {project_slug} Specification\n\n"
            f"- **Idea**: {idea_text}\n"
            f"- **Crate**: `{crate_name}` (Rust edition 2021, zero external crates by default)\n"
            f"- **Openfarm Profile**: `{resolved_profile}` (`micromanager = {mm_adapter}`, `auth = {mm_auth}`, `mutation = {resolved_mutation}`)\n\n"
            "## Protected Seam Invariants (`never_touch = [\"src/invariants.rs\"]`)\n\n"
            "- Document domain constants, checked arithmetic rules, and bounds locked in `src/invariants.rs`.\n"
            "- Never reference `src/invariants.rs` in card `touches`, `reference`, or `context_files`.\n\n"
            "## Golden Reference Vertical Slice\n\n"
            "- Architect pass adds one clean `pub` reference slice (plus `tests/baseline_tests.rs` coverage)\n"
            "  and verifies `cargo test`, `cargo clippy --all-targets -- -D warnings`, and `rustfmt --check`.\n\n"
            "## Wave Plan (`cards/CARD-001.md`..)\n\n"
            "- **Wave 1 (Contrast Seed)**: 2 foundational `small-feature` / `pattern` / `pinned-bugfix` cards.\n"
            "- **Wave 2 (Core Features)**: `feature` / `integration` / `port` cards building on Wave 1.\n"
            "- **Wave 3 (Polish)**: `refactor` / `test-backfill` cards.\n"
        ),
        "AGENTS.md": (
            f"# {project_slug} Frontier Agent Instructions\n\n"
            "1. Keep domain invariants in `src/invariants.rs` (`never_touch`) and never weaken `.openfarm/config.toml`.\n"
            "2. Ensure every integration-tested type or function is `pub` and re-exported in `src/lib.rs`.\n"
            f"3. Commit changes on `{branch}` before running `openfarm check` or `openfarm run`.\n"
            f"4. Frontier supervision defaults to native {acp_label} ACP via OAuth ({acp_cmd_hint}); lesser workers use the configured ZDR API key file.\n"
            "5. Run waves through `openfarm supervise --prepare-queue --apply` and `openfarm run --cards N --json`,\n"
            "   then review `openfarm dossier <ID>`, record `openfarm verdict`, and merge locally via `git merge --no-ff`.\n"
        ),
        ".openfarm/config.toml": _render_config_toml(
            project_slug=project_slug,
            base_branch=branch,
            state_dir=resolved_state,
            endpoint=ep,
            key_file=resolved_key,
            model=str(model or "lesser-coder"),
            mm_adapter=mm_adapter,
            mm_mode=mm_mode,
            mm_auth=mm_auth,
            mm_binary=mm_binary,
            mutation=resolved_mutation,
        ),
    }

    created_files: list[str] = []
    for rel, content in files_to_write.items():
        path = target_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created_files.append(rel)

    cfg = load_config(target_dir)
    if not no_git:
        git = Git(target_dir)
        if not (target_dir / ".git").exists():
            git.init(branch=branch)
        if commit:
            git.commit_all(f"chore: initialize {project_slug} openfarm skeleton")

    return {
        "ok": True,
        "project": project_slug,
        "crate_name": crate_name,
        "repo": str(target_dir),
        "base_branch": cfg.base_branch,
        "state_dir": str(cfg.state_dir),
        "secrets_dir": str(resolved_key.parent),
        "key_file": str(resolved_key),
        "key_file_present": resolved_key.is_file(),
        "profile": resolved_profile,
        "acp_provider": resolved_acp_provider,
        "micromanager_adapter": cfg.micromanager_adapter,
        "micromanager_mode": cfg.micromanager.mode,
        "micromanager_auth": cfg.micromanager.auth,
        "micromanager_binary": cfg.micromanager.binary,
        "mutation": cfg.mutation,
        "idea": idea_text,
        "config_valid": True,
        "created_files": created_files,
        "next_steps": [
            f"Ensure Frontier {acp_label} OAuth is signed in ({acp_cmd_hint}).",
            f"Place your Worker ZDR model API key in {resolved_key} (chmod 600).",
            "Expand .openfarm/PROJECT_SPEC.md, src/invariants.rs, a golden slice, and tests/baseline_tests.rs.",
            "Verify `cargo test`, `cargo clippy --all-targets -- -D warnings`, and `rustfmt --edition 2021 --check`.",
            "Author cards/CARD-001.md.. under cards/, commit on main, then run `openfarm check <ID>` and `openfarm run --dry-run`.",
        ],
    }
