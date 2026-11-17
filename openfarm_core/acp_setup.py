"""Multi-provider ACP OAuth discovery, login, probing, and repo config helper (`openfarm acp`).

Supports Frontier supervisors across Claude (`claude-acp`), Antigravity
(`antigravity-acp` / `agy-acp`), and Codex (`codex-acp`), while keeping lesser
coding workers on OpenAI-compatible API keys by default.
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

from . import confine as confine_lib
from . import logins
from .adapters.acp import (
    ACP_PROVIDERS,
    ClaudeAcpAdapter,
    _bridge_provider,
    _sanitised_env,
    acp_adapter_for_provider,
    infer_acp_provider,
    is_acp_adapter,
    normalize_acp_provider,
    resolve_acp_command,
)
from .config import CONFIG_PATH, Config, ConfigError, load_config
from .errors import AdapterError, Infra, Invalid, Refused, UsageError

ACP_ACTIONS = ("status", "login", "configure", "probe")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _safe_claude_auth_status(
    claude_bin: str,
    *,
    home: Path,
    timeout_s: float = 5.0,
    config_dir: Path | None = None,
) -> dict:
    """Query `claude auth status` while stripping personal email/org fields."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT", "CLAUDE_CONFIG_DIR")
        and not k.startswith("CLAUDE_CODE_")
    }
    env["HOME"] = str(home)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
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


def _safe_codex_auth_status(
    codex_bin: str,
    *,
    home: Path,
    timeout_s: float = 5.0,
    codex_home: Path | None = None,
) -> dict:
    """Query `codex login status` without leaking email/account identifiers.

    The child gets the environment every Codex child gets (no Claude session
    variables, no Antigravity variables, no `OPENAI_API_KEY` in OAuth mode).
    """
    env, _ = _sanitised_env(provider="codex", isolate_home=False, auth_mode="oauth")
    env.pop("CODEX_HOME", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(codex_home if codex_home is not None else home / ".codex")
    try:
        proc = subprocess.run(
            [codex_bin, "login", "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "loggedIn": False}

    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    logged_in = proc.returncode == 0 and "not logged in" not in combined
    auth_method = "none"
    if logged_in:
        if "chatgpt" in combined:
            auth_method = "chatgpt"
        elif "api key" in combined or "api_key" in combined:
            auth_method = "api-key"
        else:
            auth_method = "codex"
    return {
        "available": True,
        "loggedIn": logged_in,
        "authMethod": auth_method,
        "exitCode": proc.returncode,
    }


def _repo_acp_provider(cfg: Config) -> str:
    for spec in (
        cfg.micromanager_adapter,
        cfg.model_adapter,
        getattr(getattr(cfg, "swarm", None), "coordinator", ""),
        getattr(getattr(cfg, "swarm", None), "supervisor", ""),
    ):
        if is_acp_adapter(spec):
            return normalize_acp_provider(spec)
    return "claude"


def _inspect_single_provider(
    provider: str,
    *,
    resolved_home: Path,
    source_env: dict[str, str] | os._Environ[str],
    place: logins.LoginPlace | None = None,
    claude_bin: str | None = None,
    acp_bin: str | None = None,
    agy_bin: str | None = None,
    antigravity_acp_bin: str | None = None,
    codex_bin: str | None = None,
    codex_acp_bin: str | None = None,
) -> dict[str, object]:
    if provider == "claude":
        resolved_claude = claude_bin if claude_bin is not None else (shutil.which("claude") or "")
        resolved_acp = acp_bin if acp_bin is not None else (shutil.which("claude-agent-acp") or "")
        place = place or logins.login_place("claude", env={}, home=resolved_home)
        creds_file = place.credential
        creds_present = creds_file.is_file()
        creds_mode = oct(stat.S_IMODE(creds_file.stat().st_mode)) if creds_present else None
        oauth_token_env = bool(str(source_env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip())
        cli_status = (
            _safe_claude_auth_status(resolved_claude, home=resolved_home,
                                     config_dir=place.config_dir if place.from_env else None)
            if resolved_claude
            else {"available": False, "loggedIn": False}
        )
        if resolved_acp:
            resolved_transport = "claude-agent-acp"
            resolved_cmd = [resolved_acp]
        elif resolved_claude:
            resolved_transport = "claude-native-acp"
            resolved_cmd = resolve_acp_command(["claude"], provider="claude")
            resolved_cmd[-1] = resolved_claude
        else:
            resolved_transport = "missing"
            resolved_cmd = []
        oauth_ready = bool(
            cli_status.get("loggedIn") or creds_present or oauth_token_env
        )
        return {
            "provider": "claude",
            "adapter": "claude-acp",
            "resolved_transport": resolved_transport,
            "resolved_command": resolved_cmd,
            "claude_cli": resolved_claude or None,
            "claude_agent_acp": resolved_acp or None,
            "cli_path": resolved_claude or None,
            "acp_bin": resolved_acp or None,
            "oauth_ready": oauth_ready,
            "oauth_token_env": oauth_token_env,
            "credentials_file_present": creds_present,
            "credentials_mode": creds_mode,
            "credentials_from": place.from_env or "HOME",
            "cli_auth": cli_status,
        }

    if provider == "antigravity":
        resolved_agy = agy_bin if agy_bin is not None else (shutil.which("agy") or "")
        resolved_acp = (
            antigravity_acp_bin
            if antigravity_acp_bin is not None
            else (shutil.which("antigravity-acp") or "")
        )
        place = place or logins.login_place("antigravity", env={}, home=resolved_home)
        token_file = place.credential
        token_present = token_file.is_file()
        token_nonempty = False
        token_mode = None
        if token_present:
            try:
                st = token_file.stat()
                token_nonempty = st.st_size > 0
                token_mode = oct(stat.S_IMODE(st.st_mode))
            except OSError:
                token_present = False
        api_key_env = bool(
            str(source_env.get("GEMINI_API_KEY") or source_env.get("GOOGLE_API_KEY") or "").strip()
        )
        if resolved_acp:
            resolved_transport = "antigravity-acp"
            resolved_cmd = [resolved_acp]
        elif resolved_agy:
            resolved_transport = "antigravity-native-acp"
            resolved_cmd = [resolved_agy, "acp"]
        else:
            resolved_transport = "missing"
            resolved_cmd = []
        oauth_ready = bool(token_nonempty)
        cli_status = {
            "available": bool(resolved_agy),
            "loggedIn": oauth_ready,
            "authMethod": "oauth-personal" if oauth_ready else "none",
        }
        return {
            "provider": "antigravity",
            "adapter": "antigravity-acp",
            "resolved_transport": resolved_transport,
            "resolved_command": resolved_cmd,
            "agy_cli": resolved_agy or None,
            "antigravity_acp": resolved_acp or None,
            "cli_path": resolved_agy or None,
            "acp_bin": resolved_acp or None,
            "oauth_ready": oauth_ready,
            "oauth_token_env": False,
            "api_key_env": api_key_env,
            "credentials_file_present": token_present,
            "credentials_mode": token_mode,
            "cli_auth": cli_status,
        }

    # codex
    resolved_codex = codex_bin if codex_bin is not None else (shutil.which("codex") or "")
    resolved_acp = (
        codex_acp_bin if codex_acp_bin is not None else (shutil.which("codex-acp") or "")
    )
    place = place or logins.login_place("codex", env={}, home=resolved_home)
    auth_file = place.credential
    auth_present = auth_file.is_file()
    auth_nonempty = False
    auth_mode = None
    if auth_present:
        try:
            st = auth_file.stat()
            auth_nonempty = st.st_size > 0
            auth_mode = oct(stat.S_IMODE(st.st_mode))
        except OSError:
            auth_present = False
    api_key_env = bool(str(source_env.get("OPENAI_API_KEY") or "").strip())
    cli_status = (
        _safe_codex_auth_status(resolved_codex, home=resolved_home, codex_home=place.config_dir)
        if resolved_codex
        else {"available": False, "loggedIn": False}
    )
    if resolved_acp:
        resolved_transport = "codex-acp"
        resolved_cmd = [resolved_acp]
    elif resolved_codex:
        resolved_transport = "codex-native-acp"
        resolved_cmd = resolve_acp_command(["codex"], provider="codex")
        resolved_cmd[-1] = resolved_codex
    else:
        resolved_transport = "missing"
        resolved_cmd = []
    oauth_ready = bool(cli_status.get("loggedIn") or auth_nonempty)
    return {
        "provider": "codex",
        "adapter": "codex-acp",
        "resolved_transport": resolved_transport,
        "resolved_command": resolved_cmd,
        "codex_cli": resolved_codex or None,
        "codex_acp": resolved_acp or None,
        "cli_path": resolved_codex or None,
        "acp_bin": resolved_acp or None,
        "oauth_ready": oauth_ready,
        "oauth_token_env": False,
        "api_key_env": api_key_env,
        "credentials_file_present": auth_present,
        "credentials_mode": auth_mode,
        "credentials_from": place.from_env or "HOME",
        "cli_auth": cli_status,
    }


def inspect_acp_status(
    repo: Path | None = None,
    *,
    provider: str = "all",
    home: Path | None = None,
    claude_bin: str | None = None,
    acp_bin: str | None = None,
    agy_bin: str | None = None,
    antigravity_acp_bin: str | None = None,
    codex_bin: str | None = None,
    codex_acp_bin: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Inspect local OAuth readiness, ACP transport availability, and repo config."""
    resolved_home = (
        Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    )
    source_env = os.environ if env is None else env
    # Where each login lives: the environment first, $HOME second; an explicit
    # `home` without an explicit `env` looks in that home only.
    login_env = None if (env is None and home is None) else (dict(env) if env is not None else None)

    cfg: Config | None = None
    repo_provider = "claude"
    if repo is not None:
        repo_path = Path(repo).resolve()
        cfg_file = repo_path / CONFIG_PATH
        if cfg_file.is_file():
            cfg = load_config(repo_path)
            repo_provider = _repo_acp_provider(cfg)

    raw_prov = str(provider or "all").strip().lower()
    if raw_prov == "all":
        wanted = ACP_PROVIDERS
        primary_provider = repo_provider
        requested_provider = "all"
    else:
        try:
            primary_provider = normalize_acp_provider(raw_prov)
        except Invalid as exc:
            raise UsageError(exc.message) from exc
        wanted = (primary_provider,)
        requested_provider = primary_provider

    providers: dict[str, dict[str, object]] = {}
    for item in wanted:
        providers[item] = _inspect_single_provider(
            item,
            resolved_home=resolved_home,
            source_env=source_env,
            place=logins.login_place(
                item, env=login_env,
                home=resolved_home if home is not None else None),
            claude_bin=claude_bin,
            acp_bin=acp_bin,
            agy_bin=agy_bin,
            antigravity_acp_bin=antigravity_acp_bin,
            codex_bin=codex_bin,
            codex_acp_bin=codex_acp_bin,
        )

    confinement = _confinement_report(cfg, providers)
    primary = providers[primary_provider]
    claude_info = providers.get("claude") or {}

    payload: dict[str, object] = {
        "ok": True,
        "provider": requested_provider,
        "primary_provider": primary_provider,
        "resolved_transport": primary["resolved_transport"],
        "resolved_command": primary["resolved_command"],
        "claude_cli": claude_info.get("claude_cli"),
        "claude_agent_acp": claude_info.get("claude_agent_acp"),
        "oauth_ready": primary["oauth_ready"],
        "oauth_token_env": primary["oauth_token_env"],
        "credentials_file_present": primary["credentials_file_present"],
        "credentials_mode": primary["credentials_mode"],
        "cli_auth": primary["cli_auth"],
        "providers": providers,
        "confinement": confinement,
    }
    if confinement.get("notes"):
        payload["hint"] = " ".join(str(n) for n in confinement["notes"])
    if "antigravity" in providers:
        payload["agy_cli"] = providers["antigravity"].get("agy_cli")
        payload["antigravity_acp"] = providers["antigravity"].get("antigravity_acp")
    if "codex" in providers:
        payload["codex_cli"] = providers["codex"].get("codex_cli")
        payload["codex_acp"] = providers["codex"].get("codex_acp")

    if cfg is not None:
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
        swarm_cfg = getattr(cfg, "swarm", None)
        if swarm_cfg is not None:
            payload["repo_swarm"] = {
                "enabled": swarm_cfg.enabled,
                "coordinator": swarm_cfg.coordinator,
                "supervisor": swarm_cfg.supervisor,
                "coordinator_effort": swarm_cfg.coordinator_effort,
                "supervisor_effort": swarm_cfg.supervisor_effort,
            }

    return payload


def _repo_confine(cfg: Config | None) -> confine_lib.ConfineSettings:
    if cfg is None:
        return confine_lib.ConfineSettings()
    return confine_lib.settings_from_config(cfg)


def _provider_transport(cfg: Config | None, provider: str, info: dict) -> str:
    """`bridge` when openfarm's own bridge would run this provider, else `server`."""
    cmd: list[str] | None = None
    if cfg is not None and is_acp_adapter(cfg.micromanager_adapter) \
            and normalize_acp_provider(cfg.micromanager_adapter) == provider:
        raw_bin = str(cfg.micromanager.binary or "auto")
        if raw_bin != "auto":
            try:
                cmd = resolve_acp_command([raw_bin, *cfg.micromanager.args], provider=provider)
            except (Invalid, ValueError):
                cmd = None
    if cmd is None:
        cmd = [str(c) for c in (info.get("resolved_command") or [])]
    return "bridge" if _bridge_provider(cmd) else "server"


def _confinement_report(cfg: Config | None, providers: dict[str, dict]) -> dict:
    """How each provider's child would be held, per tool policy, under the repo's setting."""
    settings = _repo_confine(cfg)
    needs_bwrap = settings.mode != "off" and (
        settings.mode == "bwrap" or any(p != "claude" for p in providers))
    usable, detail = confine_lib.bwrap_status() if needs_bwrap else (False, "not checked")
    notes: list[str] = []
    for name, info in providers.items():
        transport = _provider_transport(cfg, name, info)
        held: dict[str, object] = {"mode": settings.mode, "transport": transport}
        for policy in ("none", "read-only"):
            hold = confine_lib.decide(name, transport=transport, tool_policy=policy,
                                      mode=settings.mode, bwrap_usable=usable,
                                      bwrap_reason=detail)
            held[policy] = hold.describe()
            if hold.refused:
                held[f"{policy}_refused"] = hold.refused
            if hold.note and hold.note not in notes:
                notes.append(hold.note)
        info["confinement"] = held
    return {
        "setting": confine_lib.CONFINE_SETTING,
        "mode": settings.mode,
        "bwrap": {"usable": usable, "detail": detail},
        "read_roots": list(settings.read_roots),
        "notes": notes,
    }


def run_acp_login(
    *,
    provider: str = "claude",
    mode: str = "claudeai",
    sso: bool = False,
    email: str = "",
    setup_token: bool = False,
    device_auth: bool = False,
    claude_bin: str | None = None,
    agy_bin: str | None = None,
    codex_bin: str | None = None,
    home: Path | None = None,
) -> dict:
    """Invoke native provider OAuth login (`claude`, `agy acp --login`, or `codex login`)."""
    raw_prov = str(provider or "claude").strip().lower()
    if raw_prov == "all":
        raise UsageError("acp login requires a single provider: claude, antigravity, or codex")
    try:
        prov = normalize_acp_provider(raw_prov)
    except Invalid as exc:
        raise UsageError(exc.message) from exc

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT")
        and not k.startswith("CLAUDE_CODE_")
    }
    resolved_home: Path | None = (
        Path(home).expanduser().resolve() if home is not None else None
    )
    if resolved_home is not None:
        # An explicit home decides where the login goes, not this process's
        # CLAUDE_CONFIG_DIR / CODEX_HOME.
        env["HOME"] = str(resolved_home)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CODEX_HOME", None)

    if prov == "antigravity":
        resolved_agy = agy_bin if agy_bin is not None else shutil.which("agy")
        if not resolved_agy:
            raise Infra(
                "antigravity-cli-missing",
                "Antigravity CLI (`agy`) was not found on PATH; install Antigravity CLI first.",
            )
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
        argv = [resolved_agy, "acp", "--login"]
        try:
            proc = subprocess.run(argv, env=env)
        except OSError as exc:
            raise Infra("acp-login-spawn-failed", f"failed running {argv[0]}: {exc}") from exc
        if proc.returncode != 0:
            raise Refused(
                "acp-login-failed",
                f"{' '.join(argv)} exited with code {proc.returncode}",
            )
        status = inspect_acp_status(provider="antigravity", home=home, agy_bin=resolved_agy)
        return {
            "ok": True,
            "action": "login",
            "provider": "antigravity",
            "login_command": argv,
            **status,
        }

    if prov == "codex":
        resolved_codex = codex_bin if codex_bin is not None else shutil.which("codex")
        if not resolved_codex:
            raise Infra(
                "codex-cli-missing",
                "Codex CLI (`codex`) was not found on PATH; install Codex CLI first.",
            )
        env.pop("OPENAI_API_KEY", None)
        if resolved_home is not None:
            env["CODEX_HOME"] = str(resolved_home / ".codex")
        argv = [resolved_codex, "login"]
        if device_auth:
            argv.append("--device-auth")
        try:
            proc = subprocess.run(argv, env=env)
        except OSError as exc:
            raise Infra("acp-login-spawn-failed", f"failed running {argv[0]}: {exc}") from exc
        if proc.returncode != 0:
            raise Refused(
                "acp-login-failed",
                f"{' '.join(argv)} exited with code {proc.returncode}",
            )
        status = inspect_acp_status(provider="codex", home=home, codex_bin=resolved_codex)
        return {
            "ok": True,
            "action": "login",
            "provider": "codex",
            "login_command": argv,
            **status,
        }

    # claude
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

    try:
        proc = subprocess.run(argv, env=env)
    except OSError as exc:
        raise Infra("acp-login-spawn-failed", f"failed running {argv[0]}: {exc}") from exc

    if proc.returncode != 0:
        raise Refused(
            "acp-login-failed",
            f"{' '.join(argv)} exited with code {proc.returncode}",
        )

    status = inspect_acp_status(provider="claude", home=home, claude_bin=resolved_claude)
    return {
        "ok": True,
        "action": "login",
        "provider": "claude",
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
    provider: str | None = None,
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

    if provider is not None:
        if str(provider).strip().lower() == "all":
            raise UsageError("acp configure requires a single provider: claude, antigravity, or codex")
        try:
            resolved_provider = normalize_acp_provider(provider)
        except Invalid as exc:
            raise UsageError(exc.message) from exc
    else:
        resolved_provider = _repo_acp_provider(load_config(repo_path))

    if mode is not None and mode not in ("active", "advisory", "off"):
        raise UsageError(f"invalid --mode {mode!r}: choose active, advisory, or off")
    if auth is not None and auth not in ("oauth", "api-key", "auto"):
        raise UsageError(f"invalid --auth {auth!r}: choose oauth, api-key, or auto")
    if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise UsageError(
            f"invalid --effort {effort!r}: choose low, medium, high, xhigh, or max"
        )
    if tool_policy is not None and tool_policy not in ("none", "read-only"):
        raise UsageError(
            f"invalid --tool-policy {tool_policy!r}: choose none or read-only"
        )

    raw_text = cfg_path.read_text(encoding="utf-8")

    adapter_updates: dict[str, object] = {}
    if mode == "off":
        adapter_updates["micromanager"] = "off"
    elif (
        provider is not None
        or mode in ("active", "advisory")
        or auth is not None
        or binary is not None
        or auth_file is not None
    ):
        adapter_updates["micromanager"] = acp_adapter_for_provider(resolved_provider)

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

    status = inspect_acp_status(repo_path, provider=resolved_provider)
    status["action"] = "configure"
    status["provider"] = resolved_provider
    status["config_path"] = str(cfg_path)
    status["micromanager_enabled"] = cfg.micromanager_enabled
    if cfg.micromanager_enabled and cfg.micromanager.auth == "oauth" and not status["oauth_ready"]:
        if resolved_provider == "antigravity":
            status["hint"] = (
                "Run `openfarm acp login --provider antigravity` or `agy acp --login` "
                "to authenticate Antigravity OAuth."
            )
        elif resolved_provider == "codex":
            status["hint"] = (
                "Run `openfarm acp login --provider codex` or `codex login` "
                "to authenticate Codex OAuth."
            )
        else:
            status["hint"] = (
                "Run `openfarm acp login` or `claude auth login --claudeai` "
                "to authenticate Frontier OAuth."
            )
    return status


def _would_start_antigravity(cmd: str | list[str]) -> bool:
    """True when probing `cmd` as Antigravity would start an Antigravity program."""
    from .adapters.acp import _head_names_provider

    try:
        argv = resolve_acp_command(cmd, provider="antigravity")
    except (Invalid, ValueError):
        return True
    named, _word = _head_names_provider(argv)
    return named == "antigravity" or _bridge_provider(argv) == "antigravity"


_AGY_PROBE_HINT = (
    "probing Antigravity starts the agy program; it is only done when asked for by "
    "name: `openfarm acp probe --provider antigravity`"
)


def _probe_single_provider(
    repo_path: Path,
    *,
    provider: str,
    cmd: str | list[str],
    effective_auth: str,
    effective_iso: bool,
    effective_auth_file: Path | str | None,
    timeout_s: float,
    confine: confine_lib.ConfineSettings | None = None,
) -> dict[str, object]:
    adapter = ClaudeAcpAdapter(
        command=cmd,
        cwd=repo_path,
        provider=provider,
        isolate_home=effective_iso,
        auth_file=effective_auth_file,
        auth_mode=effective_auth,
        confine=confine,
    )
    try:
        init_res = adapter.initialize(timeout_s=float(timeout_s))
        resolved_argv = list(adapter.argv)
        agent_caps = dict(adapter.agent_capabilities)
        auth_methods = list(adapter.auth_methods)
        held = adapter.confinement_summary()
        notes = list(adapter.notes)
    finally:
        adapter.close()
    out: dict[str, object] = {
        "ok": True,
        "provider": provider,
        "protocol_version": init_res.get("protocolVersion"),
        "resolved_argv": resolved_argv,
        "agent_capabilities": agent_caps,
        "auth_methods": auth_methods,
        "confinement": held,
    }
    off_note = held["none"].get("note") if isinstance(held.get("none"), dict) else ""
    if off_note and off_note not in notes:
        notes.append(off_note)
    if notes:
        out["notes"] = notes
    return out


def probe_acp(
    repo: Path | None = None,
    *,
    provider: str | None = None,
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
    repo_provider = "claude"

    requested_prov: str | None = None
    if provider is not None and str(provider).strip().lower() != "all":
        try:
            requested_prov = normalize_acp_provider(provider)
        except Invalid as exc:
            raise UsageError(exc.message) from exc

    confine_settings = confine_lib.ConfineSettings()
    if (repo_path / CONFIG_PATH).is_file():
        cfg = load_config(repo_path)
        confine_settings = _repo_confine(cfg)
        repo_provider = _repo_acp_provider(cfg)
        effective_auth = cfg.micromanager.auth
        effective_iso = cfg.micromanager.isolate_home
        raw_bin = str(cfg.micromanager.binary or "auto")
        mismatch = (
            requested_prov is not None
            and is_acp_adapter(cfg.micromanager_adapter)
            and requested_prov != repo_provider
            and raw_bin != "auto"
            and infer_acp_provider(raw_bin) != requested_prov
        )
        if not mismatch:
            cmd = [raw_bin, *cfg.micromanager.args] if cfg.micromanager.args else raw_bin
            if effective_auth_file is None:
                effective_auth_file = cfg.micromanager.auth_file

    if command is not None:
        cmd = command
    if auth_mode is not None:
        effective_auth = auth_mode
    if isolate_home is not None:
        effective_iso = isolate_home

    if provider is not None and str(provider).strip().lower() == "all":
        status = inspect_acp_status(repo_path, provider="all")
        results: dict[str, dict[str, object]] = {}
        for item in ACP_PROVIDERS:
            item_cmd = command if command is not None else "auto"
            if item == "antigravity" and _would_start_antigravity(item_cmd):
                # `all` is not a request to start agy: that needs the provider by name.
                results[item] = {
                    "ok": None,
                    "skipped": True,
                    "provider": item,
                    "error": f"not probed: {_AGY_PROBE_HINT}",
                }
                continue
            try:
                results[item] = _probe_single_provider(
                    repo_path,
                    provider=item,
                    cmd=item_cmd,
                    effective_auth=effective_auth,
                    effective_iso=effective_iso,
                    effective_auth_file=effective_auth_file,
                    timeout_s=timeout_s,
                    confine=confine_settings,
                )
            except (AdapterError, OSError) as exc:
                results[item] = {
                    "ok": False,
                    "provider": item,
                    "error": getattr(exc, "message", str(exc)),
                }
        probed = [r for r in results.values() if not r.get("skipped")]
        failed = sorted(name for name, r in results.items()
                        if not r.get("skipped") and not r.get("ok"))
        payload = {
            **status,
            # ok only when every provider that was probed answered.
            "ok": bool(probed) and not failed,
            "action": "probe",
            "provider": "all",
            "results": results,
            "failed": failed,
            "skipped": sorted(n for n, r in results.items() if r.get("skipped")),
        }
        return payload

    if provider is not None:
        try:
            prov = normalize_acp_provider(provider)
        except Invalid as exc:
            raise UsageError(exc.message) from exc
    elif command is not None:
        prov = infer_acp_provider(command)
    else:
        prov = repo_provider

    if prov == "antigravity" and requested_prov != "antigravity" and _would_start_antigravity(cmd):
        raise UsageError(_AGY_PROBE_HINT)
    probe_res = _probe_single_provider(
        repo_path,
        provider=prov,
        cmd=cmd,
        effective_auth=effective_auth,
        effective_iso=effective_iso,
        effective_auth_file=effective_auth_file,
        timeout_s=timeout_s,
        confine=confine_settings,
    )
    status = inspect_acp_status(repo_path, provider=prov)
    return {
        **status,
        **probe_res,
        "action": "probe",
    }
