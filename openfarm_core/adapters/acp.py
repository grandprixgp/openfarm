"""Stdlib ACP v1 NDJSON-RPC 2.0 over stdio client and Claude ACP dialect.

Implements the Agent Client Protocol (ACP) v1 stdio transport for Claude ACP
(`claude-agent-acp` by default) with strict security boundaries:

- Strips terminal control/escape sequences (including unterminated OSC) without
  truncating subsequent printable text.
- Sanitises inherited Claude/auth environment variables and optionally overlays an
  isolated ``0700`` ``HOME`` cleaned up on ``close()`` / ``__exit__()``.
- Enforces tool permissions strictly by option ``kind`` family, never selecting
  ``allow_always`` or ``*_always`` options, and enforcing containment inside
  ``allowed_root`` outside ``never_touch`` / ``forbidden_files`` for
  ``read-only`` sessions.
- Rejects unknown agent->client methods with JSON-RPC ``-32601``, duplicate
  request IDs with ``-32600``, declines ``elicitation/create``, and ignores
  history replay notifications/requests during ``session/load``.
"""

from __future__ import annotations

import atexit
import collections
import dataclasses
import errno
import io
import json
import math
import os
import queue
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .. import confine as confine_lib
from .. import logins
from ..errors import AdapterError, Infra, Invalid, Refused
from ..globs import escapes, matches_any, normalise

PROTOCOL_VERSION = 1
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024  # 8 MiB
_POLL_INTERVAL_S = 0.05
_CANCEL_DRAIN_S = 1.5

_DISALLOWED_NONE = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Read",
    "Glob",
    "Grep",
    "LS",
    "Task",
    "TodoWrite",
    "NotebookRead",
)
_DISALLOWED_READ_ONLY = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "LS",
    "Task",
    "TodoWrite",
    "NotebookRead",
)

_ISOLATED_HOMES_LOCK = threading.Lock()
_ACTIVE_ISOLATED_HOMES: set[Path] = set()


def _register_isolated_home(path: Path) -> None:
    with _ISOLATED_HOMES_LOCK:
        _ACTIVE_ISOLATED_HOMES.add(path)


def _unregister_isolated_home(path: Path) -> None:
    with _ISOLATED_HOMES_LOCK:
        _ACTIVE_ISOLATED_HOMES.discard(path)


def _cleanup_all_isolated_homes() -> None:
    with _ISOLATED_HOMES_LOCK:
        paths = list(_ACTIVE_ISOLATED_HOMES)
        _ACTIVE_ISOLATED_HOMES.clear()
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_all_isolated_homes)

_WARNED: set[str] = set()


def _warn_once(text: str) -> None:
    with _ISOLATED_HOMES_LOCK:
        if text in _WARNED:
            return
        _WARNED.add(text)
    print(f"openfarm: {text}", file=sys.stderr)

# Control sequence strippers. Order matters: complete sequences first, then
# lone unclosed prefixes so subsequent printable text is never truncated.
_OSC_COMPLETE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_COMPLETE_RE = re.compile(r"(?:\x1b\[|\x9b)[0-9;?]*[A-Za-z]")
_LONE_PREFIX_RE = re.compile(r"\x1b\]|\x1b\[|\x1b\\|\x1b|\x9b")
_C0_C1_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_STRIPPED_ENV_EXACT = frozenset({"CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT"})
_OAUTH_ENV_ALLOW = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
#: A variable whose name says it holds a credential. A provider child keeps its
#: own provider's variables and nothing else of this kind: a child with a shell
#: (or a model that asks it) would otherwise read a GitHub or cloud token, or
#: another provider's key, straight out of its environment.
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWD|PASSPHRASE|API_?KEY|APIKEY|"
    r"ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|CREDENTIALS|AUTH|COOKIE|SESSION_KEY)(?:_|$)",
    re.IGNORECASE)
_OWN_ENV_PREFIXES = {
    "claude": ("ANTHROPIC_", "CLAUDE_"),
    "codex": ("OPENAI_", "CODEX_"),
    "antigravity": ("GEMINI_", "GOOGLE_"),
}


def _others_secret(provider: str, key: str) -> bool:
    """True for a credential-named variable that is not this provider's own."""
    if not _SECRET_NAME_RE.search(key):
        return False
    return not key.upper().startswith(_OWN_ENV_PREFIXES.get(provider, ()))
_AUTH_ERROR_RE = re.compile(
    r"authentication_failed|authentication required|not authenticated|login required|"
    r"not logged in|please run /login|claude auth login|agy acp --login|codex login|"
    r"invalid bearer token|unauthorized|\b401\b|\boauth\b",
    re.IGNORECASE,
)

ACP_PROVIDERS: tuple[str, ...] = ("claude", "antigravity", "codex")
ACP_ADAPTER_NAMES: tuple[str, ...] = (
    "claude-acp",
    "antigravity-acp",
    "agy-acp",
    "codex-acp",
)
_DEFAULT_FORBIDDEN_FILES: tuple[str, ...] = (
    ".git/**",
    ".openfarm/**",
    ".claude/**",
    ".gemini/**",
    ".codex/**",
    ".agents/**",
)
CLAUDE_NATIVE_ACP = Path(__file__).resolve().with_name("claude_native_acp.py")
AGY_NATIVE_ACP = Path(__file__).resolve().with_name("agy_native_acp.py")
CODEX_NATIVE_ACP = Path(__file__).resolve().with_name("codex_native_acp.py")
_BRIDGE_SCRIPTS = {
    CLAUDE_NATIVE_ACP: "claude",
    AGY_NATIVE_ACP: "antigravity",
    CODEX_NATIVE_ACP: "codex",
}
# Providers whose children report a cost per turn (claude -p and claude-agent-acp do;
# codex exec --json and agy report none).
_COST_REPORTING_PROVIDERS = frozenset({"claude"})


def provider_reports_cost(provider: str) -> bool:
    """True when this provider's child reports a vendor cost per turn."""
    return normalize_acp_provider(provider) in _COST_REPORTING_PROVIDERS


def provider_prices_spend(provider: str, cost_per_1k_in: float = 0.0,
                          cost_per_1k_out: float = 0.0) -> bool:
    """Spend is measured when the provider reports cost or per-1k rates are set."""
    return provider_reports_cost(provider) or bool(cost_per_1k_in or cost_per_1k_out)


class AcpCommand(list):
    """An ACP command argv that carries its confinement settings along.

    The settings reach the adapter through code that only passes `command` on
    (the micromanager and the run's shared supervisor), so they ride with it.
    """

    confine: confine_lib.ConfineSettings = confine_lib.ConfineSettings()
    #: openfarm's own bridges replace the provider CLI's built-in system prompt
    #: (`[micromanager] lean_prompt`). A standalone server ignores it.
    lean_prompt: bool = True


def acp_command(command: str | list[str], confine, lean_prompt: bool = True) -> AcpCommand:
    """`command` as an argv list carrying `confine` (settings, a mode string or None)."""
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise Invalid("acp-bad-command", f"cannot parse ACP command {command!r}: {exc}") from exc
    else:
        argv = [str(part) for part in command]
    out = AcpCommand(argv)
    out.confine = confine_lib.as_settings(confine)
    out.lean_prompt = bool(lean_prompt)
    return out


def _head_names_provider(argv: list[str]) -> tuple[str, str]:
    """`(provider, word)` when the command's program names a provider, else `("", "")`.

    Only the program (or, for an interpreter, the script it runs) counts; a model
    name in the arguments never does.
    """
    if not argv:
        return "", ""
    words = [argv[0]]
    if Path(argv[0]).name.rstrip("0123456789.") in ("python", "node", "nodejs") and len(argv) > 1:
        words.append(argv[1])
    for word in words:
        name = Path(word).name.lower()
        try:
            res = Path(word).resolve()
        except (OSError, ValueError):
            res = None
        if res is not None and res in _BRIDGE_SCRIPTS:
            return _BRIDGE_SCRIPTS[res], word
        if name in ("agy", "agy-print", "agy-acp", "agy_native_acp.py") or name.startswith("antigravity"):
            return "antigravity", word
        if name in ("codex", "codex-acp", "codex-exec", "codex_native_acp.py"):
            return "codex", word
        if name in ("claude", "claude-native", "claude-agent-acp", "claude_native_acp.py"):
            return "claude", word
    return "", ""


def _bridge_provider(argv: list[str]) -> str:
    """The provider of openfarm's own bridge when `argv` runs one, else ``""``."""
    if len(argv) < 2:
        return ""
    try:
        return _BRIDGE_SCRIPTS.get(Path(argv[1]).resolve(), "")
    except (OSError, ValueError):
        return ""


def check_provider_matches(command: str | list[str] | None, provider: str, *,
                           where: str = "") -> None:
    """Refuse a command whose program names another provider than `provider`."""
    if command is None or not str(provider or "").strip():
        return
    prov = normalize_acp_provider(provider)
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
    else:
        argv = [str(p) for p in command]
    named, word = _head_names_provider(argv)
    if named and named != prov:
        place = f" ({where})" if where else ""
        raise Invalid(
            "acp-provider-mismatch",
            f"the ACP adapter is for {prov} but its binary {word!r} is {named}{place}; "
            f"set the binary to a {prov} program (or \"auto\"), or name the {named} adapter",
        )


def normalize_acp_provider(value: str) -> str:
    """Normalise an ACP provider or adapter name into ``claude|antigravity|codex``."""
    low = str(value or "").strip().lower()
    if low in ("claude", "claude-acp", "acp"):
        return "claude"
    if low in ("antigravity", "antigravity-acp", "agy", "agy-acp"):
        return "antigravity"
    if low in ("codex", "codex-acp"):
        return "codex"
    raise Invalid(
        "acp-bad-provider",
        f"unknown ACP provider {value!r}; choose from {', '.join(ACP_PROVIDERS)}",
    )


def acp_adapter_for_provider(provider: str) -> str:
    """Return canonical adapter name (``<provider>-acp``) for an ACP provider."""
    return f"{normalize_acp_provider(provider)}-acp"


def is_acp_adapter(spec: str) -> bool:
    """True when `spec` names any supported ACP adapter or provider alias."""
    low = str(spec or "").strip().lower()
    return low in (
        "claude-acp",
        "acp",
        "claude",
        "antigravity-acp",
        "antigravity",
        "agy-acp",
        "agy",
        "codex-acp",
        "codex",
    )


def infer_acp_provider(command: str | list[str] | None, provider: str = "") -> str:
    """Infer ACP provider from explicit `provider` or command tokens (default ``claude``)."""
    if str(provider or "").strip():
        return normalize_acp_provider(provider)
    if command is None:
        return "claude"
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
    else:
        tokens = [str(part) for part in command]

    for tok in tokens:
        low = tok.strip().lower()
        name = Path(tok).name.lower() if tok.strip() else ""
        if (
            low in ("--agy-bin", "agy", "antigravity", "antigravity-acp", "agy-acp", "agy-print", "antigravity-print")
            or name in ("agy", "antigravity", "antigravity-acp", "agy-acp", "agy-print", "antigravity-print", "agy_native_acp.py")
            or "antigravity" in name
        ):
            return "antigravity"
        if (
            low in ("--codex-bin", "codex", "codex-acp", "codex-exec")
            or name in ("codex", "codex-acp", "codex-exec", "codex_native_acp.py")
            or "codex" in name
        ):
            return "codex"
    return "claude"


def _auth_error_hint(provider: str, message: str) -> str:
    if provider == "antigravity":
        return (
            f"Antigravity ACP authentication failed ({message}); "
            "run `openfarm acp login --provider antigravity` or `agy acp --login`."
        )
    if provider == "codex":
        return (
            f"Codex ACP authentication failed ({message}); "
            "run `openfarm acp login --provider codex` or `codex login`."
        )
    return (
        f"Claude ACP OAuth authentication failed ({message}); "
        "run `openfarm acp login` or `claude auth login --claudeai`."
    )
_PATH_INPUT_KEYS = (
    "path",
    "file_path",
    "filePath",
    "file",
    "target",
    "dir",
    "directory",
    "cwd",
    "root",
    "paths",
    "files",
    "include",
    "glob",
)


def strip_control_sequences(text: str) -> str:
    """Strip ANSI CSI/SGR, C1, and OSC sequences without truncating printable text."""
    if not text:
        return ""
    cleaned = _OSC_COMPLETE_RE.sub("", str(text))
    cleaned = _CSI_COMPLETE_RE.sub("", cleaned)
    cleaned = _LONE_PREFIX_RE.sub("", cleaned)
    cleaned = _C0_C1_RE.sub("", cleaned)
    return cleaned


def _is_acp_auth_error(code: object, message: str, data: object) -> bool:
    if isinstance(data, dict):
        if str(data.get("errorKind") or "") == "authentication_failed":
            return True
        if str(data.get("code") or "") == "acp-auth-required":
            return True
    return bool(_AUTH_ERROR_RE.search(str(message or "")))


def resolve_acp_command(
    command: str | list[str],
    *,
    provider: str = "",
    python_exe: str | None = None,
) -> list[str]:
    """Resolve an ACP command string/argv across Claude, Antigravity, and Codex.

    One provider decides: an explicit `provider` is used as given (a binary that
    names another provider is refused), and without one it is inferred from the
    command.
    """
    if isinstance(command, str):
        argv = shlex.split(command)
    else:
        argv = [str(part) for part in command]
    if not argv:
        raise Invalid("acp-bad-command", "ACP adapter command must not be empty")

    head = argv[0]
    rest = argv[1:]
    head_name = Path(head).name
    py = python_exe or sys.executable

    if str(provider or "").strip():
        prov = normalize_acp_provider(provider)
        check_provider_matches(argv, prov)
    else:
        prov = infer_acp_provider(argv)

    if prov == "antigravity":
        if head in ("agy-print", "antigravity-print") or head_name in ("agy-print", "antigravity-print"):
            agy_path = shutil.which("agy") or "agy"
            return [py, str(AGY_NATIVE_ACP), "--agy-bin", agy_path, *rest]
        if head in ("auto", "agy", "antigravity", "antigravity-acp", "agy-acp") or head_name in ("agy", "antigravity"):
            if rest and rest[0] == "acp":
                resolved_agy = shutil.which(head) or head
                return [resolved_agy, *rest]
            if "/" not in head and "\\" not in head:
                acp_bin = shutil.which("antigravity-acp")
                if acp_bin:
                    return [acp_bin, *rest]
            target = "agy" if head in ("auto", "antigravity", "antigravity-acp", "agy-acp") else head
            agy_path = shutil.which(target) or target
            return [agy_path, "acp", *rest]
        return argv

    if prov == "codex":
        if head in ("auto", "codex", "codex-acp", "codex-exec") or head_name == "codex":
            # `auto` and `codex` mean openfarm's own bridge over `codex exec` when the
            # Codex CLI is there: it is the route seen working live, and the only one
            # that turns Codex's tools off for tool policy `none`. A standalone
            # `codex-acp` server is used when asked for by name, or when it is all
            # there is.
            wants_server = head == "codex-acp" or (
                head in ("auto", "codex") and not shutil.which("codex"))
            if wants_server and "/" not in head and "\\" not in head:
                acp_bin = shutil.which("codex-acp")
                if acp_bin:
                    return [acp_bin, *rest]
            target = "codex" if head in ("auto", "codex-acp", "codex-exec") else head
            codex_path = shutil.which(target) or target
            return [py, str(CODEX_NATIVE_ACP), "--codex-bin", codex_path, *rest]
        return argv

    native_script = str(CLAUDE_NATIVE_ACP)

    if head == "auto":
        acp_bin = shutil.which("claude-agent-acp")
        if acp_bin:
            return [acp_bin, *rest]
        claude_bin = shutil.which("claude")
        if claude_bin:
            return [py, native_script, "--claude-bin", claude_bin, *rest]
        return ["claude-agent-acp", *rest]

    if head in ("claude", "claude-native") or head_name == "claude":
        resolved_claude = shutil.which(head) or head
        return [py, native_script, "--claude-bin", resolved_claude, *rest]

    return argv


def _read_api_key_file(auth_file: Path | str) -> str:
    raw_path = Path(auth_file).expanduser()
    if raw_path.is_symlink():
        raise Refused("acp-auth-file-symlink", f"refusing to read symlink auth_file {raw_path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(raw_path), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused("acp-auth-file-symlink", f"refusing to read symlink auth_file {raw_path}") from exc
        raise Infra("acp-auth-file-unreadable", f"cannot read auth_file {raw_path}: {exc}") from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise Refused("acp-auth-file-not-regular", f"auth_file {raw_path} is not a regular file")
            key = handle.read().strip()
    except OSError as exc:
        raise Infra("acp-auth-file-unreadable", f"cannot read auth_file {raw_path}: {exc}") from exc
    if not key:
        raise Infra("acp-auth-file-empty", f"auth_file {raw_path} is empty")
    return key


def _sanitised_env(
    *,
    base_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    isolate_home: bool = True,
    auth_file: Path | str | None = None,
    auth_mode: str = "oauth",
    never_touch: list[str] | tuple[str, ...] = (),
    forbidden_files: list[str] | tuple[str, ...] = (),
    provider: str = "claude",
) -> tuple[dict[str, str], Path | None]:
    """Build a sanitised subprocess environment and optional isolated HOME dir.

    The login copied into the isolated home is read from where the provider keeps
    it (`logins.login_place`: `CLAUDE_CONFIG_DIR` / `CODEX_HOME` first, `$HOME`
    second), and the child's `CLAUDE_CONFIG_DIR` / `CODEX_HOME` point inside the
    isolated home, never at the real folders.
    """
    prov = normalize_acp_provider(provider or "claude")
    source = os.environ if base_env is None else base_env
    orig_home = (extra_env or {}).get("HOME") or source.get("HOME", "")
    allow_oauth = auth_file is None and auth_mode in ("oauth", "auto")
    login_env = logins.login_env_layer(base_env, extra_env)
    src_place = logins.login_place(prov, env=login_env)
    has_login_source = bool(orig_home or src_place.from_env)

    def _keep_var(key: str, val: object) -> bool:
        if key in _STRIPPED_ENV_EXACT or key.startswith("ANTIGRAVITY_"):
            return False
        if key.startswith("CLAUDE_CODE_"):
            # A run started from inside a Claude Code session carries that
            # session's ids and messaging token. No child gets them; only the
            # Claude provider may keep its own OAuth token.
            return (
                prov == "claude"
                and allow_oauth
                and key in _OAUTH_ENV_ALLOW
                and bool(str(val or "").strip())
            )
        if prov == "claude":
            if key == "ANTHROPIC_API_KEY" and (auth_mode == "oauth" or auth_file is not None):
                return False
        elif prov == "antigravity":
            if key in ("GEMINI_API_KEY", "GOOGLE_API_KEY") and (auth_mode == "oauth" or auth_file is not None):
                return False
        elif prov == "codex":
            if key == "OPENAI_API_KEY" and (auth_mode == "oauth" or auth_file is not None):
                return False
        return True

    env: dict[str, str] = {}
    for key, val in source.items():
        # Inherited variables only: what the caller passes in `extra_env` is its
        # own choice and is kept.
        if _keep_var(key, val) and not _others_secret(prov, key):
            env[key] = val

    if extra_env:
        for key, val in extra_env.items():
            if _keep_var(str(key), val):
                env[str(key)] = str(val)

    if auth_file is not None and prov in ("antigravity", "codex"):
        api_key = _read_api_key_file(auth_file)
        if prov == "antigravity":
            env["GEMINI_API_KEY"] = api_key
        else:
            env["OPENAI_API_KEY"] = api_key

    isolated_home: Path | None = None
    if isolate_home:
        # Resolve symlinks in TMPDIR so HOME/CODEX_HOME/apiKeyHelper match the
        # canonical path bind-mounted inside bwrap.
        isolated_home = Path(tempfile.mkdtemp(prefix="openfarm-acp-home-")).resolve()
        os.chmod(isolated_home, 0o700)
        _register_isolated_home(isolated_home)
        # The real login folders are never handed on; the provider's own variable
        # is set below to the copy inside the isolated home.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CODEX_HOME", None)
        env.update(logins.isolated_login_env(prov, isolated_home))
        try:
            if prov == "claude":
                claude_dir = isolated_home / ".claude"
                claude_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(claude_dir, 0o700)

                deny_patterns: list[str] = []
                for raw_pat in (
                    *_DEFAULT_FORBIDDEN_FILES,
                    *(forbidden_files or ()),
                    *(never_touch or ()),
                ):
                    norm_pat = normalise(str(raw_pat or ""))
                    if norm_pat and norm_pat not in deny_patterns:
                        deny_patterns.append(norm_pat)
                deny_rules = [
                    f"{tool}({pat})"
                    for pat in deny_patterns
                    for tool in ("Read", "Edit", "Write", "Grep", "Glob", "LS")
                ]

                settings: dict[str, object] = {
                    "disableAllHooks": True,
                    "hooks": {},
                    "permissions": {"deny": deny_rules},
                }
                if auth_file is not None:
                    # The helper runs inside the child. Under a boundary the child
                    # sees its isolated home but not the real key file, so the key
                    # travels as a private copy in that home, removed with it.
                    key_copy = claude_dir / "api-key"
                    fd = os.open(key_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(_read_api_key_file(auth_file))
                    helper_path = claude_dir / "api-key-helper.sh"
                    helper_path.write_text(
                        f"#!/bin/sh\ntr -d '\\r\\n' < {shlex.quote(str(key_copy))}\n",
                        encoding="utf-8",
                    )
                    os.chmod(helper_path, 0o700)
                    settings["apiKeyHelper"] = str(helper_path)
                else:
                    if has_login_source and allow_oauth:
                        try:
                            logins.copy_in(src_place.credential, claude_dir / ".credentials.json")
                        except OSError:
                            pass
                        claude_json_src = src_place.meta
                        try:
                            if claude_json_src is not None and claude_json_src.is_file():
                                raw_meta = json.loads(
                                    claude_json_src.read_text(encoding="utf-8")
                                )
                                if isinstance(raw_meta, dict) and (
                                    "oauthAccount" in raw_meta or "userID" in raw_meta
                                ):
                                    slim_meta: dict[str, object] = {}
                                    for k in ("oauthAccount", "userID"):
                                        if k in raw_meta:
                                            slim_meta[k] = raw_meta[k]
                                    slim_meta["hasCompletedOnboarding"] = True
                                    # $HOME/.claude.json, and the same file inside
                                    # the folder CLAUDE_CONFIG_DIR names.
                                    for claude_json_dst in (
                                        isolated_home / ".claude.json",
                                        claude_dir / ".claude.json",
                                    ):
                                        claude_json_dst.write_text(
                                            json.dumps(slim_meta) + "\n", encoding="utf-8"
                                        )
                                        os.chmod(claude_json_dst, 0o600)
                        except (OSError, ValueError):
                            pass

                settings_path = claude_dir / "settings.json"
                settings_path.write_text(
                    json.dumps(settings, indent=2) + "\n", encoding="utf-8"
                )
                os.chmod(settings_path, 0o600)
            elif prov == "antigravity":
                gemini_dir = isolated_home / ".gemini"
                gemini_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(gemini_dir, 0o700)
                ag_dir = gemini_dir / "antigravity-cli"
                ag_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(ag_dir, 0o700)
                if has_login_source and allow_oauth:
                    try:
                        logins.copy_in(src_place.credential, ag_dir / "antigravity-oauth-token")
                    except OSError:
                        pass
            elif prov == "codex":
                codex_dir = isolated_home / ".codex"
                codex_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(codex_dir, 0o700)
                if has_login_source and allow_oauth:
                    try:
                        logins.copy_in(src_place.credential, codex_dir / "auth.json")
                    except OSError:
                        pass

            env["HOME"] = str(isolated_home)
        except Exception:
            shutil.rmtree(isolated_home, ignore_errors=True)
            _unregister_isolated_home(isolated_home)
            raise

    return env, isolated_home


@dataclasses.dataclass
class AcpUsage:
    """Token and vendor-reported cost accounting for an ACP turn."""

    tokens_in: int = 0
    tokens_out: int = 0
    vendor_cost_usd: float = 0.0
    tokens_in_cached: int = 0

    @property
    def tokens_in_fresh(self) -> int:
        return max(0, self.tokens_in - self.tokens_in_cached)

    def as_dict(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_in_fresh": self.tokens_in_fresh,
            "tokens_in_cached": self.tokens_in_cached,
            "tokens_out": self.tokens_out,
            "vendor_cost_usd": self.vendor_cost_usd,
        }


@dataclasses.dataclass
class AcpResult:
    """Result of a completed ACP ``session/prompt`` turn."""

    text: str = ""
    stop_reason: str = ""
    session_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    vendor_cost_usd: float = 0.0
    latency_s: float = 0.0
    permission_events: list[dict] = dataclasses.field(default_factory=list)
    rejected_methods: list[dict] = dataclasses.field(default_factory=list)
    tokens_in_cached: int = 0

    @property
    def tokens_in_fresh(self) -> int:
        return max(0, self.tokens_in - self.tokens_in_cached)

    @property
    def usage(self) -> AcpUsage:
        return AcpUsage(
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            vendor_cost_usd=self.vendor_cost_usd,
            tokens_in_cached=self.tokens_in_cached,
        )

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "stop_reason": self.stop_reason,
            "session_id": self.session_id,
            "tokens_in": self.tokens_in,
            "tokens_in_fresh": self.tokens_in_fresh,
            "tokens_in_cached": self.tokens_in_cached,
            "tokens_out": self.tokens_out,
            "vendor_cost_usd": self.vendor_cost_usd,
            "latency_s": self.latency_s,
            "permission_events": list(self.permission_events),
            "rejected_methods": list(self.rejected_methods),
        }


def _is_always_kind(kind: str) -> bool:
    low = str(kind or "").strip().lower()
    return low.startswith("allow_always") or low.endswith("_always")


def _find_option_by_kind(options: list, target_kind: str) -> dict | None:
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind", "")).strip()
        if _is_always_kind(kind):
            continue
        if kind == target_kind and "optionId" in opt:
            return opt
    return None


def _extract_chunk_text(content: object) -> str:
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _extract_paths_from_tool_call(tool_call: dict) -> list[str]:
    paths: list[str] = []

    def _looks_like_path(val: str) -> bool:
        return (
            "\x00" in val
            or val.startswith("/")
            or val.startswith("~")
            or ".." in val.replace("\\", "/").split("/")
        )

    def _add(val: object) -> None:
        if isinstance(val, str) and val.strip():
            paths.append(val.strip())
        elif isinstance(val, list):
            for item in val:
                _add(item)
        elif isinstance(val, dict):
            _walk_dict(val, force=True)

    def _walk_dict(obj: dict, *, force: bool = False) -> None:
        for key, val in obj.items():
            if force or key in _PATH_INPUT_KEYS:
                _add(val)
            elif isinstance(val, dict):
                _walk_dict(val, force=False)
            elif isinstance(val, list):
                _walk_list(val)
            elif isinstance(val, str) and _looks_like_path(val):
                _add(val)

    def _walk_list(items: list) -> None:
        for item in items:
            if isinstance(item, dict):
                _walk_dict(item, force=False)
            elif isinstance(item, list):
                _walk_list(item)
            elif isinstance(item, str) and _looks_like_path(item):
                _add(item)

    for loc in tool_call.get("locations") or []:
        if isinstance(loc, dict):
            for key in ("path", "file", "file_path", "filePath"):
                if key in loc:
                    _add(loc[key])
        elif isinstance(loc, str):
            _add(loc)

    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        _walk_dict(raw_input, force=False)
    elif isinstance(raw_input, list):
        _walk_list(raw_input)
    elif isinstance(raw_input, str) and raw_input.strip():
        if (
            _looks_like_path(raw_input)
            or "/" in raw_input
            or "\\" in raw_input
            or ".." in raw_input
        ):
            _add(raw_input)

    return paths


class ClaudeAcpAdapter:
    """ACP v1 NDJSON-RPC 2.0 client over stdio targeting Claude, Antigravity, or Codex ACP."""

    name = "claude-acp"

    def __init__(
        self,
        command: str | list[str] = "claude-agent-acp",
        cwd: Path | str | None = None,
        *,
        provider: str = "",
        isolate_home: bool = True,
        auth_file: Path | str | None = None,
        auth_mode: str = "oauth",
        env: dict[str, str] | None = None,
        never_touch: list[str] | tuple[str, ...] = (),
        forbidden_files: list[str] | tuple[str, ...] = (),
        allowed_root: Path | str | None = None,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        stderr_tail_lines: int = 100,
        confine=None,
        lean_prompt: bool | None = None,
    ) -> None:
        if auth_mode not in ("oauth", "api-key", "auto"):
            raise Invalid(
                "acp-bad-auth-mode",
                f"auth_mode must be 'oauth', 'api-key', or 'auto', got {auth_mode!r}",
            )
        if str(provider or "").strip():
            check_provider_matches(command, provider)
        self.provider = infer_acp_provider(command, provider)
        self.name = f"{self.provider}-acp"
        self.argv = resolve_acp_command(command, provider=self.provider)
        if confine is None:
            confine = getattr(command, "confine", None)
        try:
            self.confine = confine_lib.as_settings(confine)
        except confine_lib.ConfineError as exc:
            raise Invalid(exc.code, exc.message) from exc
        self.transport = "bridge" if _bridge_provider(self.argv) else "server"
        if lean_prompt is None:
            lean_prompt = getattr(command, "lean_prompt", True)
        self.lean_prompt = bool(lean_prompt)
        self.process_hold: confine_lib.Hold | None = None
        self._boundary_root: Path | None = None
        self.notes: list[str] = []

        self.cwd = Path(cwd or Path.cwd())
        self.allowed_root = Path(allowed_root or self.cwd).resolve()
        self._session_roots: dict[str, Path] = {}
        self._cred_source: Path | None = None
        self.last_sync = None
        self.isolate_home = bool(isolate_home)
        self.auth_file = Path(auth_file) if auth_file is not None else None
        self.auth_mode = str(auth_mode)
        self.extra_env = dict(env) if env is not None else None
        self.never_touch = list(never_touch or ())
        self.forbidden_files = list(forbidden_files or ())
        for default_forbidden in _DEFAULT_FORBIDDEN_FILES:
            if default_forbidden not in self.forbidden_files:
                self.forbidden_files.append(default_forbidden)
        self.max_line_bytes = int(max_line_bytes)

        self._proc: subprocess.Popen[bytes] | None = None
        self._isolated_home: Path | None = None
        self._orig_home: str = ""
        self._initial_creds_bytes: bytes | None = None
        self._msg_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stderr_tail: collections.deque[str] = collections.deque(
            maxlen=max(1, int(stderr_tail_lines))
        )
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._turn_lock = threading.Lock()

        self._req_counter = 0
        self._initialized = False
        self._authenticated = False
        self._replaying_history = False
        self.agent_capabilities: dict = {}
        self.auth_methods: list[dict] = []
        self._session_policies: dict[str, str] = {}
        self._seen_request_ids: dict[str, set[tuple[type, object]]] = {}
        self._session_ids: list[str] = []
        self._session_cost_usd: float = 0.0
        #: Cumulative cost the server has reported, per session: sessions of
        #: parallel cards share this process and each counts from its own total.
        self._session_costs: dict[str, float] = {}
        self.last_usage: AcpUsage = AcpUsage()

    def __enter__(self) -> "ClaudeAcpAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def hello(self) -> dict:
        return {
            "name": self.name,
            "versions": [PROTOCOL_VERSION],
            "ops": [
                "hello",
                "initialize",
                "session/new",
                "session/prompt",
                "session/cancel",
                "session/close",
            ],
        }

    # -- subprocess & transport -----------------------------------------------

    def _next_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None and self._proc.poll() is not None:
            # Clean up previous dead process before respawning.
            self._cleanup_process_and_threads()

        self._msg_queue = queue.Queue()
        self._stderr_tail.clear()
        self._orig_home = (self.extra_env or {}).get("HOME") or os.environ.get("HOME", "")
        self._initial_creds_bytes = None
        hold = self._decide_hold("none")
        if self.transport == "server" and hold.refused:
            raise Refused("acp-confine-unavailable", hold.refused)
        login_place = logins.login_place(
            self.provider, env=logins.login_env_layer(None, self.extra_env))
        self._cred_source = login_place.credential
        if not (self._orig_home or login_place.from_env):
            self._cred_source = None
        env, isolated_home = _sanitised_env(
            extra_env=self.extra_env,
            isolate_home=self.isolate_home,
            auth_file=self.auth_file,
            auth_mode=self.auth_mode,
            never_touch=self.never_touch,
            forbidden_files=self.forbidden_files,
            provider=self.provider,
        )
        self._isolated_home = isolated_home
        if isolated_home is not None:
            iso_rel = self._isolated_cred_path(isolated_home).relative_to(isolated_home).parts
            initial, _why = logins.read_isolated(isolated_home, iso_rel)
            self._initial_creds_bytes = initial
            # Where the login really was when it was copied in: the copy-back
            # goes there or nowhere.
            self._cred_pin = (logins.pin_path(self._cred_source)
                              if self._cred_source is not None else None)
        argv = list(self.argv)
        if self.transport == "server":
            self.process_hold = hold
            self._note_hold(hold)
            if hold.boundary:
                try:
                    argv = confine_lib.boundary_argv(
                        argv,
                        session_dir=self.cwd,
                        home_dir=isolated_home,
                        hide_globs=self._hide_globs(),
                        hide_paths=self.confine.hide_paths,
                        read_roots=self.confine.read_roots,
                        env=env,
                    )
                except confine_lib.ConfineError as exc:
                    self._cleanup_isolated_home()
                    raise Refused("acp-confine-unavailable", exc.message) from exc
                self._boundary_root = self.cwd.resolve()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup_isolated_home()
            raise Infra(
                "acp-spawn-failed",
                f"{argv[0]} could not start: {exc}",
            ) from exc

        self._proc = proc
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(proc,), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, args=(proc,), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self, proc: subprocess.Popen[bytes]) -> None:
        stdout = proc.stdout
        if stdout is None:
            self._msg_queue.put(("_eof", None))
            return
        limit = self.max_line_bytes
        while True:
            try:
                raw = stdout.readline(limit + 1)
            except (OSError, ValueError):
                self._msg_queue.put(("_eof", None))
                break
            if not raw:
                self._msg_queue.put(("_eof", None))
                break
            if len(raw) > limit or (len(raw) == limit and not raw.endswith(b"\n")):
                self._msg_queue.put(("_oversized", len(raw)))
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):
                self._msg_queue.put(("_bad_json", line))
                break
            if not isinstance(msg, dict):
                self._msg_queue.put(("_bad_json", line))
                break
            self._msg_queue.put(("_json", msg))

    def _read_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        while True:
            try:
                raw = stderr.readline(65536)
            except (OSError, ValueError):
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                self._stderr_tail.append(line)

    def _stderr_summary(self, limit: int = 4096) -> str:
        if not self._stderr_tail:
            return ""
        joined = " | ".join(list(self._stderr_tail))
        return joined[-limit:] if len(joined) > limit else joined

    def _send_json(
        self,
        payload: dict,
        *,
        timeout_s: float = 30.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise Infra("acp-not-running", "ACP subprocess is not running")
        data = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            + b"\n"
        )
        effective_timeout = max(0.0, float(timeout_s))
        deadline = time.monotonic() + effective_timeout
        with self._write_lock:
            stdin = proc.stdin
            if stdin is None:
                self._terminate_process_group()
                raise Infra("acp-pipe-closed", "ACP subprocess stdin is closed")
            try:
                fd = stdin.fileno()
                os.set_blocking(fd, False)
            except (OSError, ValueError, io.UnsupportedOperation) as exc:
                self._terminate_process_group()
                raise Infra(
                    "acp-pipe-closed", f"failed preparing ACP stdin: {exc}"
                ) from exc

            view = memoryview(data)
            offset = 0
            total = len(data)
            while offset < total:
                if cancel_check is not None and cancel_check():
                    self._terminate_process_group()
                    raise Refused("acp-cancelled", "ACP stdin write was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process_group()
                    raise Infra(
                        "acp-stdin-timeout",
                        f"ACP stdin write timed out after {effective_timeout:.2f}s "
                        f"({offset}/{total} bytes written)",
                    )
                wait_s = min(_POLL_INTERVAL_S, remaining)
                try:
                    _, writable, _ = select.select([], [fd], [], wait_s)
                except (OSError, ValueError) as exc:
                    self._terminate_process_group()
                    raise Infra(
                        "acp-pipe-closed", f"failed waiting for ACP stdin: {exc}"
                    ) from exc
                if not writable:
                    continue
                try:
                    written = os.write(fd, view[offset:])
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    self._terminate_process_group()
                    raise Infra(
                        "acp-pipe-closed", f"failed writing to ACP stdin: {exc}"
                    ) from exc
                if written <= 0:
                    self._terminate_process_group()
                    raise Infra(
                        "acp-pipe-closed", "ACP stdin write returned 0 bytes"
                    )
                offset += written

    def _terminate_process_group(self) -> None:
        confine_lib.end_process_group(self._proc, grace_s=1.5)

    def _cleanup_process_and_threads(self) -> None:
        proc = self._proc
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            self._terminate_process_group()
        for th in (self._stdout_thread, self._stderr_thread):
            if th is not None and th.is_alive():
                th.join(timeout=1.0)
        self._stdout_thread = None
        self._stderr_thread = None
        self._proc = None

    def _cred_relpath(self) -> tuple[str, ...]:
        if self.provider == "antigravity":
            return (".gemini", "antigravity-cli", "antigravity-oauth-token")
        if self.provider == "codex":
            return (".codex", "auth.json")
        return (".claude", ".credentials.json")

    def _isolated_cred_path(self, home_dir: Path) -> Path:
        return home_dir.joinpath(*self._cred_relpath())

    # -- confinement ----------------------------------------------------------

    def _hide_globs(self) -> list[str]:
        out: list[str] = []
        for pat in (*_DEFAULT_FORBIDDEN_FILES, *self.forbidden_files, *self.never_touch):
            norm = normalise(str(pat or ""))
            if norm and norm not in out:
                out.append(norm)
        return out

    def _decide_hold(self, tool_policy: str) -> confine_lib.Hold:
        mode = self.confine.mode
        needs_check = not (mode == "off" or (mode == "auto" and self.provider == "claude"))
        usable, reason = confine_lib.bwrap_status() if needs_check else (False, "")
        nested, nested_why = (True, "")
        if usable and confine_lib.needs_nested(self.provider, self.transport, tool_policy, mode):
            nested, nested_why = confine_lib.nested_status()
        return confine_lib.decide(
            self.provider,
            transport=self.transport,
            tool_policy=tool_policy,
            mode=mode,
            bwrap_usable=usable,
            bwrap_reason=reason,
            nested_usable=nested,
            nested_reason=nested_why,
        )

    def _note_hold(self, hold: confine_lib.Hold) -> None:
        if hold.note and hold.note not in self.notes:
            self.notes.append(hold.note)
            _warn_once(hold.note)

    def confinement_summary(self) -> dict:
        """How this adapter's children are (or would be) held, per tool policy."""
        out: dict[str, object] = {"mode": self.confine.mode, "transport": self.transport}
        for policy in ("none", "read-only"):
            out[policy] = self._decide_hold(policy).as_dict()
        return out

    def _bridge_meta(self, tool_policy: str, hold: confine_lib.Hold) -> dict:
        return {
            "toolPolicy": tool_policy,
            "leanPrompt": self.lean_prompt,
            "confine": {
                "mode": self.confine.mode,
                "flags": hold.flags,
                "boundary": hold.boundary,
                "bwrap": confine_lib.find_bwrap() if hold.boundary else "",
                "hideGlobs": self._hide_globs(),
                "hidePaths": list(self.confine.hide_paths),
                "readRoots": list(self.confine.read_roots),
            },
        }

    def _check_session_inside_boundary(self, session_cwd: Path) -> None:
        root = self._boundary_root
        if root is None:
            return
        try:
            resolved = session_cwd.resolve()
        except (OSError, ValueError) as exc:
            raise Refused("acp-confine-session-outside", f"bad session folder {session_cwd}: {exc}") from exc
        if resolved != root and root not in resolved.parents:
            raise Refused(
                "acp-confine-session-outside",
                f"this {self.provider} ACP server was confined to {root} when it started; "
                f"a session at {resolved} would be outside its boundary. Use openfarm's own "
                "bridge ([micromanager] binary = \"codex-exec\" for Codex, \"agy-print\" for "
                "Antigravity), or set [micromanager] confine = \"off\".",
            )
        parts = resolved.relative_to(root).parts
        globs = self._hide_globs()
        if any(matches_any("/".join(parts[: i + 1]), globs) for i in range(len(parts))):
            raise Refused(
                "acp-confine-session-outside",
                f"session folder {resolved} is hidden inside this server's boundary "
                "(never_touch / forbidden_files)",
            )

    def _persist_refreshed_oauth_credentials(self) -> None:
        """Copy a refreshed login back to where it was read from, if it is safe to."""
        if (
            self._isolated_home is None
            or self.auth_file is not None
            or self.auth_mode not in ("oauth", "auto")
            or self._cred_source is None
        ):
            return
        result = logins.sync_back(
            self.provider,
            isolated_home=self._isolated_home,
            target=self._cred_source,
            initial=self._initial_creds_bytes,
            pinned=getattr(self, "_cred_pin", None),
        )
        self.last_sync = result
        if result.status == "refused":
            note = (
                f"the refreshed {self.provider} login was not copied back to "
                f"{self._cred_source}: {result.reason}"
            )
            self.notes.append(note)
            print(f"openfarm: {note}", file=sys.stderr)

    def _cleanup_isolated_home(self) -> None:
        if self._isolated_home is not None:
            path = self._isolated_home
            self._isolated_home = None
            shutil.rmtree(path, ignore_errors=True)
            _unregister_isolated_home(path)

    def close(self) -> None:
        """Close active ACP session, terminate child process, and clean temp HOME."""
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                for sid in list(self._session_ids):
                    try:
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/close",
                                "params": {"sessionId": sid},
                            },
                            timeout_s=1.0,
                        )
                    except (AdapterError, OSError):
                        break
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._terminate_process_group()
            self._cleanup_process_and_threads()
            self._persist_refreshed_oauth_credentials()
        finally:
            self._initialized = False
            self._authenticated = False
            self._session_ids.clear()
            self._session_roots.clear()
            self._session_cost_usd = 0.0
            self._session_costs.clear()
            self._initial_creds_bytes = None
            self._boundary_root = None
            self._cleanup_isolated_home()

    # -- ACP RPC methods ------------------------------------------------------

    def _wait_simple_response(
        self,
        req_id: int,
        timeout_s: float,
        *,
        reject_requests: bool = False,
        ignore_updates: bool = False,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_group()
                raise Infra(
                    "acp-timeout",
                    f"ACP request {req_id} timed out after {timeout_s:.1f}s",
                )
            try:
                kind, item = self._msg_queue.get(
                    timeout=min(_POLL_INTERVAL_S, remaining)
                )
            except queue.Empty:
                continue

            if kind == "_eof":
                raise Infra(
                    "acp-eof",
                    f"ACP subprocess closed stdout unexpectedly: {self._stderr_summary()}",
                )
            if kind == "_oversized":
                self._terminate_process_group()
                raise Infra(
                    "acp-oversized",
                    f"ACP message exceeded {self.max_line_bytes} bytes ({item})",
                )
            if kind == "_bad_json":
                self._terminate_process_group()
                raise Infra("acp-bad-json", f"ACP emitted invalid JSON: {str(item)[:200]}")

            msg = item
            assert isinstance(msg, dict)

            if "method" in msg and "id" in msg and msg["id"] is not None:
                # Agent -> Client request during initialize / session/new / session/load.
                in_id = msg["id"]
                if reject_requests or self._replaying_history:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32600,
                                "message": "Requests rejected during session replay",
                            },
                        },
                        timeout_s=max(0.05, remaining),
                    )
                else:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": in_id,
                            "error": {
                                "code": -32601,
                                "message": f"Method not found: {msg.get('method')}",
                            },
                        },
                        timeout_s=max(0.05, remaining),
                    )
                continue

            if "method" in msg:
                # Notification
                if ignore_updates or self._replaying_history:
                    continue
                continue

            if msg.get("id") == req_id:
                if msg.get("error") is not None:
                    err = msg["error"]
                    if isinstance(err, dict):
                        code = err.get("code", -1)
                        message = str(err.get("message", "ACP error"))
                        data = err.get("data")
                        if isinstance(data, dict) and data.get("kind") == "refused":
                            raise Refused(str(data.get("code", "acp-refused")), message)
                    else:
                        code = -1
                        message = str(err)
                        data = None
                    if _is_acp_auth_error(code, message, data):
                        raise Refused(
                            "acp-auth-required",
                            _auth_error_hint(self.provider, message),
                        )
                    raise Infra(f"acp-rpc-{code}", _rpc_error_text(message, data))
                result = msg.get("result")
                return result if isinstance(result, dict) else {}

    def initialize(self, timeout_s: float = 30.0) -> dict:
        self._ensure_started()
        req_id = self._next_id()
        deadline = time.monotonic() + float(timeout_s)
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {},
                },
            },
            timeout_s=timeout_s,
        )
        result = self._wait_simple_response(
            req_id, timeout_s=max(0.05, deadline - time.monotonic())
        )
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise Invalid(
                "acp-protocol-version",
                f"unsupported ACP protocolVersion: {result.get('protocolVersion')!r}",
            )
        caps = result.get("agentCapabilities")
        self.agent_capabilities = caps if isinstance(caps, dict) else {}
        raw_auth = result.get("authMethods")
        self.auth_methods = (
            [m for m in raw_auth if isinstance(m, dict)]
            if isinstance(raw_auth, list)
            else []
        )
        self._authenticated = False
        self._initialized = True
        return result

    def authenticate(
        self,
        method_id: str,
        params: dict | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        if not self._initialized:
            self.initialize(timeout_s=timeout_s)
        req_id = self._next_id()
        deadline = time.monotonic() + float(timeout_s)
        req_params: dict = {"methodId": str(method_id)}
        if params:
            req_params.update(params)
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "authenticate",
                "params": req_params,
            },
            timeout_s=timeout_s,
        )
        result = self._wait_simple_response(
            req_id, timeout_s=max(0.05, deadline - time.monotonic())
        )
        self._authenticated = True
        return result

    def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str,
        timeout_s: float = 15.0,
    ) -> dict:
        if not self._initialized:
            self.initialize(timeout_s=timeout_s)
        req_id = self._next_id()
        deadline = time.monotonic() + float(timeout_s)
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": str(session_id),
                    "configId": str(config_id),
                    "value": str(value),
                },
            },
            timeout_s=timeout_s,
        )
        return self._wait_simple_response(
            req_id, timeout_s=max(0.05, deadline - time.monotonic())
        )

    def _apply_config_options(
        self,
        session_id: str,
        config_options: list,
        *,
        model: str = "",
        effort: str = "",
        timeout_s: float = 15.0,
    ) -> None:
        targets: list[tuple[str, str, str]] = []
        for opt in config_options:
            if not isinstance(opt, dict):
                continue
            opt_id = str(opt.get("id") or "").strip()
            cat = str(opt.get("category") or "").strip().lower()
            if not opt_id:
                continue
            if model and (opt_id == "model" or cat == "model"):
                targets.append((opt_id, str(model), "model"))
            elif effort and (
                opt_id in ("thought_level", "effort", "reasoning_effort")
                or cat == "thought_level"
            ):
                resolved_eff = str(effort)
                raw_opts = opt.get("options")
                allowed_vals: set[str] = set()
                if isinstance(raw_opts, list):
                    for choice in raw_opts:
                        if isinstance(choice, dict) and choice.get("value") is not None:
                            allowed_vals.add(str(choice["value"]))
                        elif isinstance(choice, str):
                            allowed_vals.add(choice)
                if allowed_vals and resolved_eff not in allowed_vals:
                    if resolved_eff in ("xhigh", "max") and "high" in allowed_vals:
                        resolved_eff = "high"
                    elif resolved_eff == "off" and "low" in allowed_vals:
                        resolved_eff = "low"
                targets.append((opt_id, resolved_eff, "effort"))

        for opt_id, val, kind in targets:
            try:
                self.set_config_option(session_id, opt_id, val, timeout_s=timeout_s)
            except Infra as exc:
                if exc.code == "acp-rpc--32601":
                    continue
                if exc.code == "acp-rpc--32602" and kind == "effort":
                    # An effort the server refuses is a preference lost, not a
                    # card lost: codex-acp 0.16.0 refuses any effort for a model
                    # it has no preset for ("Reasoning effort can only be set for
                    # known model presets"), live on 18 Nov 2026. The model
                    # itself is still required.
                    note = (f"the {self.provider} server refused effort {val!r} "
                            f"({opt_id}), so its own default effort is used: {exc}")
                    if note not in self.notes:
                        self.notes.append(note)
                        print(f"openfarm: {note}", file=sys.stderr)
                    continue
                raise

    def new_session(
        self,
        cwd: Path | str | None = None,
        system_prompt: str = "",
        model: str = "",
        effort: str = "",
        tool_policy: str = "none",
        timeout_s: float = 30.0,
    ) -> str:
        if tool_policy not in ("none", "read-only"):
            raise Invalid(
                "acp-bad-tool-policy",
                f"tool_policy must be 'none' or 'read-only', got {tool_policy!r}",
            )
        session_path = Path(cwd) if cwd is not None else self.cwd
        hold = self._decide_hold(tool_policy)
        if hold.refused:
            raise Refused("acp-confine-unavailable", hold.refused)
        if not self._initialized:
            self.initialize(timeout_s=timeout_s)
        self._check_session_inside_boundary(session_path)
        if self.transport == "bridge":
            self._note_hold(hold)
        if (
            not self._authenticated
            and self.auth_methods
            and self.auth_mode in ("oauth", "auto")
        ):
            oauth_id = ""
            for method in self.auth_methods:
                m_type = str(method.get("type") or "").strip().lower()
                m_id = str(method.get("id") or method.get("methodId") or "").strip()
                if m_type == "oauth" or m_id.lower() in ("oauth", "claude-ai", "claude.ai"):
                    oauth_id = m_id or "oauth"
                    break
            if oauth_id:
                self.authenticate(oauth_id, timeout_s=min(30.0, float(timeout_s)))

        meta: dict[str, object] = {}
        if system_prompt:
            meta["systemPrompt"] = {"append": str(system_prompt)}

        if self.provider == "claude":
            options: dict[str, object] = {}
            if model:
                options["model"] = str(model)
            if effort:
                options["effort"] = str(effort)
            if tool_policy == "none":
                options["disallowedTools"] = list(_DISALLOWED_NONE)
            else:
                options["disallowedTools"] = list(_DISALLOWED_READ_ONLY)
            meta["claudeCode"] = {"options": options}
        elif self.provider == "antigravity":
            meta["antigravity"] = {"systemPrompt": {"append": str(system_prompt)}}
        elif self.provider == "codex":
            codex_opts: dict[str, object] = {"toolPolicy": tool_policy}
            if model:
                codex_opts["model"] = str(model)
            if effort:
                codex_opts["effort"] = str(effort)
            meta["codex"] = {"options": codex_opts}
        if self.transport == "bridge":
            # openfarm's own bridges apply the policy and the boundary per prompt.
            meta["openfarm"] = self._bridge_meta(tool_policy, hold)

        session_cwd = str(session_path)
        req_id = self._next_id()
        deadline = time.monotonic() + float(timeout_s)
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/new",
                "params": {
                    "cwd": session_cwd,
                    "mcpServers": [],
                    "_meta": meta,
                },
            },
            timeout_s=timeout_s,
        )
        result = self._wait_simple_response(
            req_id, timeout_s=max(0.05, deadline - time.monotonic())
        )
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise Invalid("acp-no-session-id", "session/new returned no sessionId")
        # A standalone server offers its model and effort as ACP config options
        # (`agy acp`, and the real `codex-acp`, which ignores `_meta.codex`: seen live).
        if self.provider == "antigravity" or (
                self.provider == "codex" and self.transport == "server"):
            config_options = result.get("configOptions")
            if isinstance(config_options, list) and config_options:
                self._apply_config_options(
                    session_id,
                    config_options,
                    model=model,
                    effort=effort,
                    timeout_s=min(15.0, float(timeout_s)),
                )
        self._session_costs[session_id] = 0.0
        self._session_policies[session_id] = tool_policy
        self._remember_session_root(session_id, cwd)
        self._seen_request_ids.setdefault(session_id, set())
        if session_id not in self._session_ids:
            self._session_ids.append(session_id)
        return session_id

    def _remember_session_root(self, session_id: str, cwd: Path | str | None) -> None:
        # A permission request is judged against its own session's folder; a
        # session opened without one falls back to the adapter's root.
        if cwd is None:
            self._session_roots.pop(session_id, None)
            return
        try:
            self._session_roots[session_id] = Path(cwd).resolve()
        except (OSError, ValueError):
            self._session_roots.pop(session_id, None)

    def load_session(
        self,
        session_id: str,
        cwd: Path | str | None = None,
        tool_policy: str = "none",
        timeout_s: float = 30.0,
    ) -> dict:
        if tool_policy not in ("none", "read-only"):
            raise Invalid(
                "acp-bad-tool-policy",
                f"tool_policy must be 'none' or 'read-only', got {tool_policy!r}",
            )
        hold = self._decide_hold(tool_policy)
        if hold.refused:
            raise Refused("acp-confine-unavailable", hold.refused)
        if not self._initialized:
            self.initialize(timeout_s=timeout_s)

        session_path = Path(cwd) if cwd is not None else self.cwd
        self._check_session_inside_boundary(session_path)
        session_cwd = str(session_path)
        req_id = self._next_id()
        deadline = time.monotonic() + float(timeout_s)
        self._replaying_history = True
        try:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "session/load",
                    "params": {
                        "sessionId": session_id,
                        "cwd": session_cwd,
                        "mcpServers": [],
                    },
                },
                timeout_s=timeout_s,
            )
            result = self._wait_simple_response(
                req_id,
                timeout_s=max(0.05, deadline - time.monotonic()),
                reject_requests=True,
                ignore_updates=True,
            )
        finally:
            self._replaying_history = False

        self._session_costs[session_id] = 0.0
        self._session_policies[session_id] = tool_policy
        self._remember_session_root(session_id, cwd)
        self._seen_request_ids.setdefault(session_id, set())
        if session_id not in self._session_ids:
            self._session_ids.append(session_id)
        return result

    def resume_session(
        self,
        session_id: str,
        cwd: Path | str | None = None,
        tool_policy: str = "none",
        timeout_s: float = 30.0,
    ) -> dict:
        return self.load_session(
            session_id, cwd=cwd, tool_policy=tool_policy, timeout_s=timeout_s
        )

    # -- permission & request enforcement -------------------------------------

    def _is_path_allowed_readonly(
        self, raw_path: str, root: Path | None = None
    ) -> tuple[bool, str]:
        allowed_root = Path(root) if root is not None else self.allowed_root
        raw_str = str(raw_path)
        if "\x00" in raw_str:
            return False, "nul-path"
        text = raw_str.strip()
        if not text:
            return False, "empty-path"
        if text.startswith("~"):
            return False, "tilde-path"

        try:
            candidate = Path(text)
            if not candidate.is_absolute():
                lexical_norm = normalise(text)
                if escapes(lexical_norm):
                    return False, "path-escape"
                if matches_any(lexical_norm, self.forbidden_files):
                    return False, "forbidden-file"
                if matches_any(lexical_norm, self.never_touch):
                    return False, "never-touch"
                resolved = (allowed_root / lexical_norm).resolve()
            else:
                resolved = candidate.resolve()

            root = allowed_root.resolve()
            if resolved != root and root not in resolved.parents:
                return False, "outside-allowed-root"

            rel = (
                "."
                if resolved == root
                else resolved.relative_to(root).as_posix()
            )
            if matches_any(rel, self.forbidden_files):
                return False, "forbidden-file"
            if matches_any(rel, self.never_touch):
                return False, "never-touch"
        except (OSError, ValueError, RuntimeError):
            return False, "invalid-path"
        return True, "ok"

    def _handle_permission(
        self,
        in_id: object,
        params: dict,
        tool_policy: str,
        permission_events: list[dict],
        *,
        timeout_s: float = 30.0,
        cancel_check: Callable[[], bool] | None = None,
        session_id: str = "",
    ) -> None:
        options = params.get("options")
        if not isinstance(options, list):
            options = []
        tool_call = params.get("toolCall")
        if not isinstance(tool_call, dict):
            tool_call = {}

        tool_kind = str(tool_call.get("kind", "")).strip().lower()
        extracted_paths = _extract_paths_from_tool_call(tool_call)
        session_root = self._session_roots.get(str(session_id or ""))
        named_sid = params.get("sessionId")

        allow = False
        reason = "policy-none"
        if session_id and named_sid is not None and str(named_sid) != str(session_id):
            # A request naming another session is judged by no one's folder.
            reason = "session-mismatch"
        elif tool_policy == "read-only":
            if tool_kind not in ("read", "search"):
                allow = False
                reason = f"tool-kind-{tool_kind or 'unknown'}-disallowed"
            elif not extracted_paths:
                allow = False
                reason = f"{tool_kind}-missing-path"
            else:
                all_ok = True
                for p in extracted_paths:
                    ok, why = self._is_path_allowed_readonly(p, session_root)
                    if not ok:
                        all_ok = False
                        reason = why
                        break
                    search_root = session_root or self.allowed_root
                    if tool_kind == "search" and search_root is not None:
                        try:
                            cand = Path(p)
                            if not cand.is_absolute():
                                cand = (search_root / cand).resolve()
                            else:
                                cand = cand.resolve()
                            if cand.is_dir():
                                rel = cand.relative_to(search_root.resolve()).as_posix()
                                tree_pat = "**" if rel in ("", ".") else f"{rel}/**"
                                from ..globs import globs_overlap
                                protected = (*_DEFAULT_FORBIDDEN_FILES, *self.forbidden_files, *self.never_touch)
                                if any(globs_overlap(tree_pat, pat) for pat in protected if pat):
                                    all_ok = False
                                    reason = "search-overlaps-protected"
                                    break
                        except (OSError, ValueError):
                            pass
                if all_ok:
                    allow = True
                    reason = "read-only-contained"

        selected_opt = None
        if allow:
            selected_opt = _find_option_by_kind(options, "allow_once")
            if selected_opt is None:
                allow = False
                reason = "no-allow-once-option"

        if allow and selected_opt is not None:
            outcome = {
                "outcome": "selected",
                "optionId": selected_opt["optionId"],
            }
            decision = "allow_once"
            option_id = selected_opt["optionId"]
        else:
            reject_opt = _find_option_by_kind(options, "reject_once")
            if reject_opt is not None:
                outcome = {
                    "outcome": "selected",
                    "optionId": reject_opt["optionId"],
                }
                decision = "reject_once"
                option_id = reject_opt["optionId"]
            else:
                outcome = {"outcome": "cancelled"}
                decision = "cancelled"
                option_id = None

        permission_events.append(
            {
                "id": in_id,
                "tool_kind": tool_kind,
                "tool_policy": tool_policy,
                "decision": decision,
                "reason": reason,
                "paths": extracted_paths,
                "option_id": option_id,
            }
        )
        self._send_json(
            {"jsonrpc": "2.0", "id": in_id, "result": {"outcome": outcome}},
            timeout_s=timeout_s,
            cancel_check=cancel_check,
        )

    def _cancel_and_terminate(self, session_id: str, req_id: int) -> None:
        try:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                },
                timeout_s=1.0,
            )
        except (AdapterError, OSError):
            pass

        drain_deadline = time.monotonic() + _CANCEL_DRAIN_S
        while True:
            rem = drain_deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                kind, item = self._msg_queue.get(timeout=min(_POLL_INTERVAL_S, rem))
            except queue.Empty:
                continue
            if kind in ("_eof", "_oversized"):
                break
            if kind == "_json" and isinstance(item, dict) and item.get("id") == req_id:
                break

        self._terminate_process_group()

    # -- prompt turn ----------------------------------------------------------

    def prompt(
        self,
        session_id: str,
        text: str,
        timeout_s: float = 180.0,
        cancel_check: Callable[[], bool] | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> AcpResult:
        if not self._turn_lock.acquire(blocking=False):
            raise Refused(
                "acp-concurrent-prompt",
                "only one active prompt turn is permitted at a time",
            )
        try:
            return self._prompt_locked(
                session_id=session_id,
                text=text,
                timeout_s=timeout_s,
                cancel_check=cancel_check,
                on_chunk=on_chunk,
            )
        finally:
            self._turn_lock.release()

    def _prompt_locked(
        self,
        *,
        session_id: str,
        text: str,
        timeout_s: float,
        cancel_check: Callable[[], bool] | None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> AcpResult:
        self.last_usage = AcpUsage()
        if self._proc is None or self._proc.poll() is not None:
            raise Infra("acp-not-running", "ACP subprocess is not running")
        if (self._isolated_home is not None and self._cred_source is not None
                and self.auth_file is None and self.auth_mode in ("oauth", "auto")):
            try:
                ref_res, new_bytes = logins.refresh_isolated(
                    self.provider,
                    isolated_home=self._isolated_home,
                    source=self._cred_source,
                    initial=self._initial_creds_bytes,
                    pinned=self._cred_pin,
                )
                if ref_res.status == "refreshed":
                    self._initial_creds_bytes = new_bytes
            except Exception as exc:  # noqa: BLE001 - best-effort mid-run refresh
                _ = exc

        req_id = self._next_id()
        started = time.monotonic()
        deadline = started + timeout_s
        tool_policy = self._session_policies.get(session_id, "none")
        seen_ids = self._seen_request_ids.setdefault(session_id, set())

        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            },
            timeout_s=timeout_s,
            cancel_check=cancel_check,
        )

        chunks: list[str] = []
        update_tokens_in: int | None = None
        update_tokens_out: int | None = None
        latest_cum_cost: float | None = None
        permission_events: list[dict] = []
        rejected_methods: list[dict] = []
        turn_completed = False

        try:
            while True:
                if cancel_check is not None and cancel_check():
                    self._cancel_and_terminate(session_id, req_id)
                    raise Refused("acp-cancelled", "ACP prompt turn was cancelled")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._cancel_and_terminate(session_id, req_id)
                    raise Infra(
                        "acp-timeout",
                        f"ACP prompt turn timed out after {timeout_s:.1f}s",
                    )

                try:
                    kind, item = self._msg_queue.get(
                        timeout=min(_POLL_INTERVAL_S, remaining)
                    )
                except queue.Empty:
                    continue

                if kind == "_eof":
                    raise Infra(
                        "acp-eof",
                        f"ACP subprocess exited before PromptResponse: {self._stderr_summary()}",
                    )
                if kind == "_oversized":
                    self._terminate_process_group()
                    raise Infra(
                        "acp-oversized",
                        f"ACP line exceeded {self.max_line_bytes} bytes ({item})",
                    )
                if kind == "_bad_json":
                    self._terminate_process_group()
                    raise Infra("acp-bad-json", f"ACP emitted invalid JSON: {str(item)[:200]}")

                msg = item
                assert isinstance(msg, dict)

                # Agent -> Client request (has both method and non-null id).
                if "method" in msg and "id" in msg and msg["id"] is not None:
                    in_id = msg["id"]
                    method = str(msg.get("method") or "")
                    params = msg.get("params")
                    if not isinstance(params, dict):
                        params = {}
                    resp_timeout = max(0.05, deadline - time.monotonic())

                    if self._replaying_history:
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": in_id,
                                "error": {
                                    "code": -32600,
                                    "message": "Requests rejected during history replay",
                                },
                            },
                            timeout_s=resp_timeout,
                            cancel_check=cancel_check,
                        )
                        rejected_methods.append(
                            {"id": in_id, "method": method, "code": -32600, "reason": "replay"}
                        )
                        continue

                    valid_id = (
                        in_id is None
                        or isinstance(in_id, str)
                        or (isinstance(in_id, int) and not isinstance(in_id, bool))
                        or (isinstance(in_id, float) and math.isfinite(in_id))
                    )
                    if not valid_id:
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {
                                    "code": -32600,
                                    "message": "Invalid request id",
                                },
                            },
                            timeout_s=resp_timeout,
                            cancel_check=cancel_check,
                        )
                        rejected_methods.append(
                            {
                                "id": in_id,
                                "method": method,
                                "code": -32600,
                                "reason": "invalid-id",
                            }
                        )
                        continue

                    id_key = (type(in_id), in_id)
                    if id_key in seen_ids:
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": in_id,
                                "error": {
                                    "code": -32600,
                                    "message": "Duplicate request id",
                                },
                            },
                            timeout_s=resp_timeout,
                            cancel_check=cancel_check,
                        )
                        rejected_methods.append(
                            {
                                "id": in_id,
                                "method": method,
                                "code": -32600,
                                "reason": "duplicate-id",
                            }
                        )
                        continue
                    seen_ids.add(id_key)

                    if method == "session/request_permission":
                        self._handle_permission(
                            in_id,
                            params,
                            tool_policy,
                            permission_events,
                            timeout_s=resp_timeout,
                            cancel_check=cancel_check,
                            session_id=session_id,
                        )
                    elif method == "elicitation/create":
                        permission_events.append(
                            {"id": in_id, "method": method, "decision": "decline"}
                        )
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": in_id,
                                "result": {"action": "decline"},
                            },
                            timeout_s=resp_timeout,
                            cancel_check=cancel_check,
                        )
                    else:
                        rejected_methods.append(
                            {
                                "id": in_id,
                                "method": method,
                                "code": -32601,
                                "reason": "method-not-found",
                            }
                        )
                        self._send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": in_id,
                                "error": {
                                    "code": -32601,
                                    "message": f"Method not found: {method}",
                                },
                            },
                            timeout_s=resp_timeout,
                            cancel_check=cancel_check,
                        )
                    continue

                # Notification (method without id).
                if "method" in msg:
                    if self._replaying_history:
                        continue
                    if msg.get("method") == "session/update":
                        params = msg.get("params")
                        if not isinstance(params, dict):
                            continue
                        msg_sid = params.get("sessionId")
                        if msg_sid is not None and str(msg_sid) != session_id:
                            continue
                        update = params.get("update")
                        if not isinstance(update, dict):
                            update = params
                        upd_kind = update.get("sessionUpdate")
                        if upd_kind == "agent_message_chunk":
                            chunk = strip_control_sequences(
                                _extract_chunk_text(update.get("content"))
                            )
                            if chunk:
                                chunks.append(chunk)
                                if on_chunk is not None:
                                    on_chunk(chunk)
                        elif upd_kind == "usage_update":
                            in_tok = _first_present(
                                update, ("inputTokens", "tokensIn", "promptTokens")
                            )
                            out_tok = _first_present(
                                update,
                                ("outputTokens", "tokensOut", "completionTokens"),
                            )
                            try:
                                if in_tok is not None:
                                    update_tokens_in = _parse_int_usage(
                                        in_tok, "usage_update.inputTokens"
                                    )
                                if out_tok is not None:
                                    update_tokens_out = _parse_int_usage(
                                        out_tok, "usage_update.outputTokens"
                                    )
                                if "cost" in update and update["cost"] is not None:
                                    cost_val = update["cost"]
                                    if isinstance(cost_val, dict):
                                        amt = cost_val.get("amount")
                                        if amt is not None:
                                            latest_cum_cost = _parse_float_usage(
                                                amt, "usage_update.cost.amount"
                                            )
                                    else:
                                        latest_cum_cost = _parse_float_usage(
                                            cost_val, "usage_update.cost"
                                        )
                            except Invalid:
                                self._terminate_process_group()
                                raise
                    continue

                # JSON-RPC Response to our session/prompt request.
                if msg.get("id") == req_id:
                    if msg.get("error") is not None:
                        err = msg["error"]
                        if isinstance(err, dict):
                            code = err.get("code", -1)
                            message = str(err.get("message", "ACP prompt error"))
                            data = err.get("data")
                            if isinstance(data, dict) and data.get("kind") == "refused":
                                raise Refused(str(data.get("code", "acp-refused")), message)
                        else:
                            code = -1
                            message = str(err)
                            data = None
                        if _is_acp_auth_error(code, message, data):
                            raise Refused(
                                "acp-auth-required",
                                _auth_error_hint(self.provider, message),
                            )
                        detail = _rpc_error_text(message, data)
                        stderr_tail = self._stderr_summary()
                        if stderr_tail and stderr_tail not in detail:
                            detail = f"{detail} [stderr: {stderr_tail[-600:]}]"
                        raise Infra(f"acp-rpc-{code}", detail)

                    result = msg.get("result")
                    if not isinstance(result, dict):
                        result = {}
                    stop_reason = str(result.get("stopReason", ""))
                    if stop_reason == "cancelled":
                        # Nobody here asked for a cancel (that path raises above):
                        # the child was stopped or interrupted, and whatever it
                        # had said so far is not an answer.
                        raise Infra(
                            "acp-turn-cancelled",
                            "the ACP child ended the turn as cancelled (it was stopped or "
                            "interrupted); what it had answered so far was not used",
                        )

                    resp_usage = result.get("usage")
                    if resp_usage is not None and not isinstance(resp_usage, dict):
                        self._terminate_process_group()
                        raise Invalid(
                            "acp-bad-usage",
                            f"PromptResponse.result.usage must be an object, got {type(resp_usage).__name__}",
                        )
                    resp_in_raw = (
                        _first_present(
                            resp_usage, ("inputTokens", "promptTokens", "tokensIn")
                        )
                        if isinstance(resp_usage, dict)
                        else None
                    )
                    resp_out_raw = (
                        _first_present(
                            resp_usage,
                            ("outputTokens", "completionTokens", "tokensOut"),
                        )
                        if isinstance(resp_usage, dict)
                        else None
                    )
                    try:
                        if resp_in_raw is not None:
                            tokens_in = _parse_int_usage(
                                resp_in_raw, "PromptResponse.usage.inputTokens"
                            )
                        elif update_tokens_in is not None:
                            tokens_in = update_tokens_in
                        else:
                            tokens_in = 0

                        if resp_out_raw is not None:
                            tokens_out = _parse_int_usage(
                                resp_out_raw, "PromptResponse.usage.outputTokens"
                            )
                        elif update_tokens_out is not None:
                            tokens_out = update_tokens_out
                        else:
                            tokens_out = 0
                        if resp_in_raw is not None:
                            tokens_in = _with_cached_input(resp_usage, tokens_in, tokens_out)
                        tokens_in_cached = _cached_input_from_usage(resp_usage)
                    except Invalid:
                        self._terminate_process_group()
                        raise

                    if latest_cum_cost is not None:
                        seen_cost = self._session_costs.get(session_id, 0.0)
                        turn_vendor_cost = max(0.0, latest_cum_cost - seen_cost)
                        self._session_costs[session_id] = max(seen_cost, latest_cum_cost)
                    else:
                        turn_vendor_cost = 0.0

                    self.last_usage = AcpUsage(
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        vendor_cost_usd=turn_vendor_cost,
                        tokens_in_cached=tokens_in_cached,
                    )
                    turn_completed = True
                    return AcpResult(
                        text="".join(chunks),
                        stop_reason=stop_reason,
                        session_id=session_id,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        vendor_cost_usd=turn_vendor_cost,
                        latency_s=time.monotonic() - started,
                        permission_events=permission_events,
                        rejected_methods=rejected_methods,
                        tokens_in_cached=tokens_in_cached,
                    )
        finally:
            if not turn_completed:
                seen_cost = self._session_costs.get(session_id, 0.0)
                partial_cost = (
                    max(0.0, latest_cum_cost - seen_cost)
                    if latest_cum_cost is not None
                    else 0.0
                )
                if latest_cum_cost is not None:
                    self._session_costs[session_id] = max(seen_cost, latest_cum_cost)
                self.last_usage = AcpUsage(
                    tokens_in=update_tokens_in or 0,
                    tokens_out=update_tokens_out or 0,
                    vendor_cost_usd=partial_cost,
                )



def _rpc_error_text(message: str, data: object, limit: int = 400) -> str:
    """A JSON-RPC error's message with the server's own detail, when it gave one.

    codex-acp answers a refused model with only "Internal error" in `message`
    and the provider's reason in `data` (live, 18 Nov 2026: "The 'gpt-5.5' model
    is not supported when using Codex with a ChatGPT account"). Without the
    detail a dossier said only "Internal error".
    """
    detail = ""
    if isinstance(data, str):
        detail = data
    elif isinstance(data, dict):
        raw = data.get("message") or data.get("detail") or ""
        detail = str(raw)
        try:
            nested = json.loads(detail) if detail.startswith("{") else None
        except ValueError:
            nested = None
        if isinstance(nested, dict):
            inner = nested.get("error")
            if isinstance(inner, dict) and inner.get("message"):
                detail = str(inner["message"])
                if inner.get("code"):
                    detail += f" ({inner['code']})"
            elif nested.get("message"):
                detail = str(nested["message"])
    detail = " ".join(detail.split())
    if not detail or detail == message:
        return message
    return f"{message}: {detail[:limit]}"

def _cached_input_from_usage(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("cachedInputTokens", "cachedReadTokens", "cache_read_input_tokens"):
        raw = usage.get(key)
        if not isinstance(raw, bool) and isinstance(raw, int) and raw >= 0:
            return raw
    return 0


def _with_cached_input(usage: dict, tokens_in: int, tokens_out: int) -> int:
    """Input tokens with the cached ones counted.

    ACP's usage block reports cached input apart (`cachedReadTokens`,
    `cachedWriteTokens`). The real Claude ACP server leaves them out of
    `inputTokens`: a turn that read 16,303 prompt tokens reported 2 (seen live),
    which would make a supervisor's share of a run look like nothing. A server
    whose `inputTokens` already holds them is told apart by its own total.
    """
    cached = 0
    for key in ("cachedReadTokens", "cachedWriteTokens"):
        raw = usage.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            continue
        cached += raw
    if not cached:
        return tokens_in
    total = usage.get("totalTokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0 \
            and tokens_in + tokens_out >= total:
        return tokens_in
    return tokens_in + cached


def _first_present(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _parse_int_usage(val: object, field: str) -> int:
    if isinstance(val, bool):
        raise Invalid("acp-bad-usage", f"boolean not allowed for {field}: {val!r}")
    if isinstance(val, int):
        parsed = val
    elif isinstance(val, float):
        if not math.isfinite(val) or not val.is_integer():
            raise Invalid("acp-bad-usage", f"invalid integer for {field}: {val!r}")
        try:
            parsed = int(val)
        except (ValueError, TypeError, OverflowError) as exc:
            raise Invalid("acp-bad-usage", f"invalid integer for {field}: {val!r}") from exc
    elif isinstance(val, str):
        try:
            parsed = int(val.strip())
        except (ValueError, TypeError, OverflowError) as exc:
            raise Invalid("acp-bad-usage", f"invalid integer for {field}: {val!r}") from exc
    else:
        raise Invalid("acp-bad-usage", f"invalid integer for {field}: {val!r}")
    if parsed < 0:
        raise Invalid("acp-bad-usage", f"negative value for {field}: {parsed!r}")
    return parsed


def _parse_float_usage(val: object, field: str) -> float:
    if isinstance(val, bool):
        raise Invalid("acp-bad-usage", f"boolean not allowed for {field}: {val!r}")
    if isinstance(val, (int, float)):
        try:
            parsed = float(val)
        except (ValueError, TypeError, OverflowError) as exc:
            raise Invalid("acp-bad-usage", f"invalid float for {field}: {val!r}") from exc
    elif isinstance(val, str):
        try:
            parsed = float(val.strip())
        except (ValueError, TypeError, OverflowError) as exc:
            raise Invalid("acp-bad-usage", f"invalid float for {field}: {val!r}") from exc
    else:
        raise Invalid("acp-bad-usage", f"invalid float for {field}: {val!r}")
    if not math.isfinite(parsed) or parsed < 0.0:
        raise Invalid(
            "acp-bad-usage", f"invalid non-negative finite float for {field}: {val!r}"
        )
    return parsed


AcpAdapter = ClaudeAcpAdapter
