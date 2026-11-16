"""Claude ACP OAuth discovery, login, probing, and repo config helper (`openfarm acp`).

Keeps the frontier supervisor on native Claude ACP via OAuth by default while
leaving lesser coding workers on OpenAI-compatible API keys.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

from .adapters.acp import ClaudeAcpAdapter, resolve_acp_command
from .config import CONFIG_PATH, Config, ConfigError, load_config
from .errors import Infra, Refused, UsageError

ACP_ACTIONS = ("status", "login", "configure", "probe")


def _safe_claude_auth_status(
    claude_bin: str,
    *,
    home: Path,
    timeout_s: float = 5.0,
) -> dict:
    """Query `claude auth status` while stripping personal email/org fields."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT")
        and not k.startswith("CLAUDE_CODE_")
    }
    env["HOME"] = str(home)
    try:
        proc = subprocess.run(
            [claude_bin, "auth", "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "loggedIn": False}

    raw = (proc.stdout or "").strip()
    if not raw:
        return {"available": True, "loggedIn": False, "exitCode": proc.returncode}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"available": True, "loggedIn": False, "exitCode": proc.returncode}
    if not isinstance(parsed, dict):
        return {"available": True, "loggedIn": False, "exitCode": proc.returncode}

    # Redact email/org identifiers so diagnostics stay clean.
    safe_keys = (
        "loggedIn",
        "authMethod",
        "apiProvider",
        "subscriptionType",
        "planTier",
        "loginExpiresAt",
        "storedClaudeAiLogin",
    )
    out: dict[str, object] = {"available": True, "exitCode": proc.returncode}
    for key in safe_keys:
        if key in parsed and parsed[key] is not None:
            out[key] = parsed[key]
    out.setdefault("loggedIn", proc.returncode == 0)
    return out


def inspect_acp_status(
    repo: Path | None = None,
    *,
    home: Path | None = None,
    claude_bin: str | None = None,
    acp_bin: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Inspect local Claude OAuth readiness, ACP transport availability, and repo config."""
    resolved_home = (
        Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    )
    resolved_claude = claude_bin if claude_bin is not None else (shutil.which("claude") or "")
    resolved_acp = acp_bin if acp_bin is not None else (shutil.which("claude-agent-acp") or "")

    creds_file = resolved_home / ".claude" / ".credentials.json"
    creds_present = creds_file.is_file()
    creds_mode = oct(stat.S_IMODE(creds_file.stat().st_mode)) if creds_present else None

    source_env = os.environ if env is None else env
    oauth_token_env = bool(str(source_env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip())

    cli_status = (
        _safe_claude_auth_status(resolved_claude, home=resolved_home)
        if resolved_claude
        else {"available": False, "loggedIn": False}
    )

    if resolved_acp:
        resolved_transport = "claude-agent-acp"
        resolved_cmd = [resolved_acp]
    elif resolved_claude:
        resolved_transport = "claude-native-acp"
        resolved_cmd = resolve_acp_command(["claude"])
        # Preserve caller-provided explicit claude binary path if overridden in tests.
        resolved_cmd[-1] = resolved_claude
    else:
        resolved_transport = "missing"
        resolved_cmd = []

    oauth_ready = bool(
        cli_status.get("loggedIn") or creds_present or oauth_token_env
    )

    payload: dict[str, object] = {
        "ok": True,
        "resolved_transport": resolved_transport,
        "resolved_command": resolved_cmd,
        "claude_cli": resolved_claude or None,
        "claude_agent_acp": resolved_acp or None,
        "oauth_ready": oauth_ready,
        "oauth_token_env": oauth_token_env,
        "credentials_file_present": creds_present,
        "credentials_mode": creds_mode,
        "cli_auth": cli_status,
    }

    if repo is not None:
        repo_path = Path(repo).resolve()
        cfg_file = repo_path / CONFIG_PATH
        if cfg_file.is_file():
            cfg = load_config(repo_path)
            payload["repo_frontier"] = {
                "adapter": cfg.micromanager_adapter,
                "mode": cfg.micromanager.mode,
                "enabled": cfg.micromanager_enabled,
                "auth": cfg.micromanager.auth,
                "binary": cfg.micromanager.binary,
                "model": cfg.micromanager.model,
                "effort": cfg.micromanager.effort,
                "tool_policy": cfg.micromanager.tool_policy,
                "auth_file": (
                    str(cfg.micromanager.auth_file)
                    if cfg.micromanager.auth_file is not None
                    else None
                ),
            }
            payload["repo_workers"] = {
                "adapter": cfg.model_adapter,
                "endpoint": cfg.endpoint,
                "key_file": str(cfg.key_file) if cfg.key_file is not None else None,
                "key_file_present": bool(cfg.key_file and cfg.key_file.is_file()),
            }

    return payload


def run_acp_login(
    *,
    mode: str = "claudeai",
    sso: bool = False,
    email: str = "",
    setup_token: bool = False,
    claude_bin: str | None = None,
    home: Path | None = None,
) -> dict:
    """Invoke native `claude auth login` or `claude setup-token` for OAuth setup."""
    resolved_claude = claude_bin if claude_bin is not None else shutil.which("claude")
    if not resolved_claude:
        raise Infra(
            "claude-cli-missing",
            "Claude CLI (`claude`) was not found on PATH; install Claude Code first.",
        )
    if mode not in ("claudeai", "console"):
        raise UsageError(f"invalid login mode {mode!r}: choose 'claudeai' or 'console'")

    if setup_token:
        argv = [resolved_claude, "setup-token"]
    else:
        flag = "--console" if mode == "console" else "--claudeai"
        argv = [resolved_claude, "auth", "login", flag]
        if sso:
            argv.append("--sso")
        if email.strip():
            argv.extend(["--email", email.strip()])

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT")
        and not k.startswith("CLAUDE_CODE_")
    }
    if home is not None:
        env["HOME"] = str(Path(home).expanduser().resolve())

    try:
        proc = subprocess.run(argv, env=env)
    except OSError as exc:
        raise Infra("acp-login-spawn-failed", f"failed running {argv[0]}: {exc}") from exc

    if proc.returncode != 0:
        raise Refused(
            "acp-login-failed",
            f"{' '.join(argv)} exited with code {proc.returncode}",
        )

    status = inspect_acp_status(home=home, claude_bin=resolved_claude)
    return {
        "ok": True,
        "action": "login",
        "login_command": argv,
        **status,
    }


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _upsert_toml_table(
    text: str,
    table_name: str,
    updates: dict[str, object],
    remove_keys: tuple[str, ...] = (),
) -> str:
    """Update or insert `table_name` keys while preserving unrelated TOML sections."""
    if not updates and not remove_keys:
        return text

    lines = text.splitlines(keepends=True)
    header_re = re.compile(rf"^\s*\[{re.escape(table_name)}\]\s*(?:#.*)?$")
    any_header_re = re.compile(r"^\s*\[[^\n]+\]\s*(?:#.*)?$")

    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if header_re.match(line):
            start_idx = idx
            break

    if start_idx is None:
        if not updates:
            return text
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        if out and not out.endswith("\n\n"):
            out += "\n"
        out += f"[{table_name}]\n"
        for key, val in updates.items():
            out += f"{key} = {_toml_literal(val)}\n"
        return out

    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        if any_header_re.match(lines[idx]):
            end_idx = idx
            break

    section_lines = lines[start_idx + 1 : end_idx]
    key_re = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    seen_keys: set[str] = set()
    new_section: list[str] = []

    for line in section_lines:
        m = key_re.match(line)
        if m:
            k = m.group(1)
            if k in remove_keys and k not in updates:
                continue
            if k in updates:
                seen_keys.add(k)
                new_section.append(f"{k} = {_toml_literal(updates[k])}\n")
                continue
        new_section.append(line)

    # Insert missing keys before trailing blank lines of the section.
    trailing_blanks: list[str] = []
    while new_section and not new_section[-1].strip():
        trailing_blanks.insert(0, new_section.pop())
    for key, val in updates.items():
        if key not in seen_keys:
            new_section.append(f"{key} = {_toml_literal(val)}\n")
    new_section.extend(trailing_blanks)

    return "".join(lines[: start_idx + 1] + new_section + lines[end_idx:])


def configure_acp_repo(
    repo: Path,
    *,
    mode: str | None = None,
    auth: str | None = None,
    binary: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    tool_policy: str | None = None,
    auth_file: str | None = None,
    clear_auth_file: bool = False,
) -> dict:
    """Update Frontier `[adapters] micromanager` + `[micromanager]` in config.toml."""
    repo_path = Path(repo).resolve()
    cfg_path = repo_path / CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"missing {cfg_path}; run `openfarm init` first")

    if mode is not None and mode not in ("active", "advisory", "off"):
        raise UsageError(f"invalid --mode {mode!r}: choose active, advisory, or off")
    if auth is not None and auth not in ("oauth", "api-key", "auto"):
        raise UsageError(f"invalid --auth {auth!r}: choose oauth, api-key, or auto")
    if effort is not None and effort not in ("low", "medium", "high"):
        raise UsageError(f"invalid --effort {effort!r}: choose low, medium, or high")
    if tool_policy is not None and tool_policy not in ("none", "read-only"):
        raise UsageError(
            f"invalid --tool-policy {tool_policy!r}: choose none or read-only"
        )

    raw_text = cfg_path.read_text(encoding="utf-8")

    adapter_updates: dict[str, object] = {}
    if mode == "off":
        adapter_updates["micromanager"] = "off"
    elif (
        mode in ("active", "advisory")
        or auth is not None
        or binary is not None
        or auth_file is not None
    ):
        adapter_updates["micromanager"] = "claude-acp"

    mm_updates: dict[str, object] = {}
    remove_mm: list[str] = []
    if mode is not None:
        mm_updates["mode"] = mode
    if auth is not None:
        mm_updates["auth"] = auth
        if auth == "oauth" and auth_file is None:
            remove_mm.append("auth_file")
    if clear_auth_file:
        remove_mm.append("auth_file")
    if binary is not None:
        mm_updates["binary"] = binary
    if model is not None:
        mm_updates["model"] = model
    if effort is not None:
        mm_updates["effort"] = effort
    if tool_policy is not None:
        mm_updates["tool_policy"] = tool_policy
    if auth_file is not None:
        mm_updates["auth_file"] = auth_file
        if auth is None:
            mm_updates["auth"] = "api-key"

    updated_text = _upsert_toml_table(raw_text, "adapters", adapter_updates)
    updated_text = _upsert_toml_table(
        updated_text,
        "micromanager",
        mm_updates,
        remove_keys=tuple(remove_mm),
    )

    try:
        parsed = tomllib.loads(updated_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"cannot render valid TOML: {exc}") from exc

    # Fail closed before writing if any resulting config setting is invalid.
    cfg = Config(parsed, repo_path, path=cfg_path)
    cfg_path.write_text(updated_text, encoding="utf-8")

    status = inspect_acp_status(repo_path)
    status["action"] = "configure"
    status["config_path"] = str(cfg_path)
    status["micromanager_enabled"] = cfg.micromanager_enabled
    if cfg.micromanager_enabled and cfg.micromanager.auth == "oauth" and not status["oauth_ready"]:
        status["hint"] = "Run `openfarm acp login` or `claude auth login --claudeai` to authenticate Frontier OAuth."
    return status


def probe_acp(
    repo: Path | None = None,
    *,
    command: str | list[str] | None = None,
    auth_mode: str | None = None,
    isolate_home: bool | None = None,
    auth_file: Path | str | None = None,
    timeout_s: float = 15.0,
) -> dict:
    """Launch configured ACP transport, run zero-token `initialize()`, and report result."""
    repo_path = Path(repo).resolve() if repo is not None else Path.cwd().resolve()
    cmd: str | list[str] = "auto"
    effective_auth = "oauth"
    effective_iso = True
    effective_auth_file = auth_file

    if (repo_path / CONFIG_PATH).is_file():
        cfg = load_config(repo_path)
        raw_bin = cfg.micromanager.binary or "auto"
        cmd = [raw_bin, *cfg.micromanager.args] if cfg.micromanager.args else raw_bin
        effective_auth = cfg.micromanager.auth
        effective_iso = cfg.micromanager.isolate_home
        if effective_auth_file is None:
            effective_auth_file = cfg.micromanager.auth_file

    if command is not None:
        cmd = command
    if auth_mode is not None:
        effective_auth = auth_mode
    if isolate_home is not None:
        effective_iso = isolate_home

    adapter = ClaudeAcpAdapter(
        command=cmd,
        cwd=repo_path,
        isolate_home=effective_iso,
        auth_file=effective_auth_file,
        auth_mode=effective_auth,
    )
    try:
        init_res = adapter.initialize(timeout_s=float(timeout_s))
        resolved_argv = list(adapter.argv)
        agent_caps = dict(adapter.agent_capabilities)
        auth_methods = list(adapter.auth_methods)
    finally:
        adapter.close()

    status = inspect_acp_status(repo_path)
    return {
        **status,
        "ok": True,
        "action": "probe",
        "protocol_version": init_res.get("protocolVersion"),
        "resolved_argv": resolved_argv,
        "agent_capabilities": agent_caps,
        "auth_methods": auth_methods,
    }
