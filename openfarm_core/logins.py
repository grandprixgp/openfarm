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
_MAX_BYTES = {"claude": 1 << 20, "codex": 1 << 20, "antigravity": 64 * 1024}
BACKUP_SUFFIX = ".openfarm-backup"


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


def read_isolated(isolated_home: Path | str, rel_parts: tuple[str, ...],
                  max_bytes: int = 1 << 20) -> tuple[bytes | None, str]:
    """Read a file under `isolated_home` without following any link on the way.

    Every folder on the way and the file itself are opened with `O_NOFOLLOW`; the
    file must be a regular file no bigger than `max_bytes`. Returns `(bytes, "")`
    or `(None, reason)`.
    """
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        dir_fd = os.open(str(isolated_home), flags_dir)
    except OSError as exc:
        return None, f"isolated home cannot be opened ({errno.errorcode.get(exc.errno, exc.errno)})"
    fds = [dir_fd]
    try:
        for part in rel_parts[:-1]:
            try:
                dir_fd = os.open(part, flags_dir, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    return None, f"{part} is not a plain folder (a link?)"
                return None, f"{part} is missing"
            fds.append(dir_fd)
        try:
            fd = os.open(rel_parts[-1], flags_file, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None, "the credential is a symlink"
            if exc.errno == errno.ENOENT:
                return None, "the credential is missing"
            return None, f"the credential cannot be opened ({errno.errorcode.get(exc.errno, exc.errno)})"
        fds.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "the credential is not a regular file"
        if st.st_size > max_bytes:
            return None, f"the credential is {st.st_size} bytes, over the {max_bytes}-byte limit"
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None, f"the credential is over the {max_bytes}-byte limit"
            chunks.append(chunk)
        return b"".join(chunks), ""
    finally:
        for f in reversed(fds):
            try:
                os.close(f)
            except OSError:
                pass


# -- shape checks ------------------------------------------------------------------


def _json_object(raw: bytes) -> dict | None:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def check_shape(provider: str, raw: bytes, original: bytes | None) -> str:
    """Empty string when `raw` is a plausible refreshed login for the same account."""
    if not raw:
        return "the refreshed credential is empty"
    if b"\x00" in raw:
        return "the refreshed credential has NUL bytes"
    if provider == "claude":
        new = _json_object(raw)
        oauth = new.get("claudeAiOauth") if new is not None else None
        if not isinstance(oauth, dict):
            return "not a Claude credentials file (no claudeAiOauth object)"
        if not _nonempty_str(oauth.get("accessToken")):
            return "the Claude credentials have no access token"
        if "refreshToken" in oauth and not _nonempty_str(oauth.get("refreshToken")):
            return "the Claude credentials have an empty refresh token"
        old = _json_object(original) if original else None
        old_oauth = old.get("claudeAiOauth") if old is not None else None
        if isinstance(old_oauth, dict):
            if _nonempty_str(old_oauth.get("refreshToken")) and not _nonempty_str(oauth.get("refreshToken")):
                return "the refreshed Claude login lost its refresh token"
            old_exp, new_exp = old_oauth.get("expiresAt"), oauth.get("expiresAt")
            if (isinstance(old_exp, (int, float)) and isinstance(new_exp, (int, float))
                    and not isinstance(old_exp, bool) and new_exp < old_exp):
                return "the refreshed Claude login expires before the one it replaces"
        return ""
    if provider == "codex":
        new = _json_object(raw)
        if new is None:
            return "not a Codex auth.json (not a JSON object)"
        tokens = new.get("tokens")
        if not isinstance(tokens, dict):
            return "the Codex auth.json has no tokens object"
        if not _nonempty_str(tokens.get("access_token")):
            return "the Codex auth.json has no access token"
        if "refresh_token" in tokens and not _nonempty_str(tokens.get("refresh_token")):
            return "the Codex auth.json has an empty refresh token"
        old = _json_object(original) if original else None
        if old is None:
            return "the original Codex login is not an OAuth auth.json, so nothing is copied back"
        old_tokens = old.get("tokens") if isinstance(old.get("tokens"), dict) else {}
        if _nonempty_str(old_tokens.get("refresh_token")) and not _nonempty_str(tokens.get("refresh_token")):
            return "the refreshed Codex login lost its refresh token"
        old_acct = old_tokens.get("account_id")
        if old_acct is not None and tokens.get("account_id") != old_acct:
            return "the refreshed Codex login belongs to another account"
        if old.get("auth_mode") is not None and new.get("auth_mode") != old.get("auth_mode"):
            return "the refreshed Codex login uses another auth mode"
        old_ts, new_ts = old.get("last_refresh"), new.get("last_refresh")
        if isinstance(old_ts, str) and isinstance(new_ts, str) and new_ts < old_ts:
            return "the refreshed Codex login is older than the one it replaces"
        return ""
    if provider == "antigravity":
        # The token's format is not documented; the code and tests treat it as text.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "the Antigravity token is not text"
        if not text.strip():
            return "the Antigravity token is blank"
        if original and _json_object(original) is not None and _json_object(raw) is None:
            return "the original Antigravity token was a JSON object and the new one is not"
        return ""
    return f"unknown provider {provider!r}"


# -- writing back ----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SyncResult:
    status: str  # "synced", "unchanged", "skipped" or "refused"
    reason: str = ""
    target: str = ""
    backup: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _write_atomic(directory: Path, name: str, data: bytes, prefix: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(data):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, directory / name)
        os.chmod(directory / name, 0o600)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise


def sync_back(
    provider: str,
    *,
    isolated_home: Path | str,
    target: Path | str,
    initial: bytes | None,
) -> SyncResult:
    """Copy a refreshed login from the isolated home back to `target`, safely.

    `target` is the file the login was copied from. The previous credential is kept
    beside it as `<name>.openfarm-backup` (mode 600). Anything that fails a check is
    left alone, and the result says why (never with the credential's content).
    """
    if provider not in PROVIDERS:
        return SyncResult("refused", f"unknown provider {provider!r}")
    iso = isolated_place(provider, isolated_home)
    rel = iso.credential.relative_to(Path(isolated_home)).parts
    raw, why = read_isolated(isolated_home, rel, _MAX_BYTES[provider])
    if raw is None:
        if why == "the credential is missing":
            return SyncResult("skipped", why)
        return SyncResult("refused", why)
    if initial is not None and raw == initial:
        return SyncResult("unchanged")
    problem = check_shape(provider, raw, initial)
    if problem:
        return SyncResult("refused", problem)
    dest = Path(target)
    if not dest.parent.is_dir():
        return SyncResult("refused", f"{dest.parent} is not a folder", str(dest))
    prefix = "." + dest.name + "-"
    backup = ""
    try:
        if dest.exists():
            try:
                previous = dest.read_bytes()
            except OSError as exc:
                return SyncResult("refused", f"cannot read the current credential to back it up ({exc.strerror})",
                                  str(dest))
            _write_atomic(dest.parent, dest.name + BACKUP_SUFFIX, previous, prefix)
            backup = str(dest.parent / (dest.name + BACKUP_SUFFIX))
        _write_atomic(dest.parent, dest.name, raw, prefix)
    except OSError as exc:
        return SyncResult("refused", f"write failed ({exc.strerror})", str(dest), backup)
    return SyncResult("synced", "", str(dest), backup)


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
