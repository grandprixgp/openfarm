"""Where each provider's login lives, and the one safe way to copy a refreshed one back.

Claude Code honours `CLAUDE_CONFIG_DIR` (then `.credentials.json` and `.claude.json`
both sit in that folder) and falls back to `$HOME/.claude` plus `$HOME/.claude.json`.
Codex honours `CODEX_HOME` and falls back to `$HOME/.codex`. Antigravity keeps its
token at `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`.

A child with an isolated home gets a copy of the login there, with its
`CLAUDE_CONFIG_DIR` / `CODEX_HOME` pointing inside the isolated home. When the
child refreshes the login, `sync_back` copies it to the place it was read from,
but only when the isolated file is a regular file opened without following
links and its content has the provider's real shape. Nothing here prints or
logs a credential's content.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import stat
import tempfile
from pathlib import Path

PROVIDERS: tuple[str, ...] = ("claude", "antigravity", "codex")


@dataclasses.dataclass(frozen=True)
class LoginPlace:
    """Where one provider's login is on disk."""

    provider: str
    config_dir: Path
    credential: Path
    meta: Path | None = None
    from_env: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "config_dir": str(self.config_dir),
                "credential": str(self.credential),
                "meta": str(self.meta) if self.meta is not None else None,
                "from_env": self.from_env}


def _layers(env, home) -> tuple[dict, Path]:
    """The environment to read and the home to fall back on.

    No arguments: this process's environment and home. An explicit `home` with
    no `env` means "look in that home" (a test or `--home`), so variables of this
    process are not consulted. An explicit `env` is read as given.
    """
    if env is None and home is None:
        source = dict(os.environ)
        base = source.get("HOME") or str(Path.home())
        return source, Path(base).expanduser()
    if env is None:
        return {}, Path(home).expanduser()
    source = {str(k): str(v) for k, v in dict(env).items()}
    base = home if home is not None else (source.get("HOME") or str(Path.home()))
    return source, Path(base).expanduser()


def login_place(provider: str, *, env: dict | None = None,
                home: Path | str | None = None) -> LoginPlace:
    """Where `provider`'s login lives: the environment first, `$HOME` second."""
    source, base = _layers(env, home)
    if provider == "claude":
        raw = str(source.get("CLAUDE_CONFIG_DIR") or "").strip()
        if raw:
            cfg = Path(raw).expanduser()
            return LoginPlace("claude", cfg, cfg / ".credentials.json", cfg / ".claude.json",
                              "CLAUDE_CONFIG_DIR")
        cfg = base / ".claude"
        return LoginPlace("claude", cfg, cfg / ".credentials.json", base / ".claude.json")
    if provider == "codex":
        raw = str(source.get("CODEX_HOME") or "").strip()
        if raw:
            cfg = Path(raw).expanduser()
            return LoginPlace("codex", cfg, cfg / "auth.json", None, "CODEX_HOME")
        cfg = base / ".codex"
        return LoginPlace("codex", cfg, cfg / "auth.json")
    if provider == "antigravity":
        cfg = base / ".gemini" / "antigravity-cli"
        return LoginPlace("antigravity", cfg, cfg / "antigravity-oauth-token")
    raise ValueError(f"unknown provider {provider!r}")


def isolated_place(provider: str, isolated_home: Path | str) -> LoginPlace:
    """Where the login sits inside an isolated home (always the `$HOME` layout)."""
    return login_place(provider, env={}, home=Path(isolated_home))


def isolated_login_env(provider: str, isolated_home: Path | str) -> dict[str, str]:
    """The child's login variables: they point inside the isolated home, never outside."""
    iso = Path(isolated_home)
    if provider == "claude":
        return {"CLAUDE_CONFIG_DIR": str(iso / ".claude")}
    if provider == "codex":
        return {"CODEX_HOME": str(iso / ".codex")}
    return {}


def login_env_layer(base_env: dict | None, extra_env: dict | None) -> dict:
    """The environment a child's login is looked up in.

    When the caller overrides HOME without naming a login folder, the override's
    home decides: a `CLAUDE_CONFIG_DIR` or `CODEX_HOME` of this process must not
    redirect a child that was pointed at another home.
    """
    source = dict(os.environ if base_env is None else base_env)
    extra = dict(extra_env or {})
    if "HOME" in extra:
        out = {"HOME": str(extra["HOME"])}
        for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            if extra.get(key):
                out[key] = str(extra[key])
        return out
    merged = {str(k): str(v) for k, v in source.items()}
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if extra.get(key):
            merged[key] = str(extra[key])
    return merged


# -- reading the isolated copy safely ---------------------------------------------


def copy_in(source: Path, dest: Path) -> bool:
    """Copy the user's own login into an isolated home (mode 600). False when absent."""
    try:
        if not source.is_file():
            return False
        data = source.read_bytes()
    except OSError:
        return False
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(data):
            written += os.write(fd, view[written:])
    finally:
        os.close(fd)
    return True
