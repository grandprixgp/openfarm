"""Global provider profile (`~/.config/openfarm/providers.toml`) & live probe (v0.10.0).

Stores Frontier ACP defaults and Worker ZDR endpoint/model/key_file path once globally
so every new or adopted repository inherits working model settings without repetitive
per-project credential plumbing. Raw API keys are NEVER written into `providers.toml`;
they stay in atomic mode-0600 non-symlink `.key` files.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from .errors import Refused

VALID_FRONTIER_PROVIDERS: tuple[str, ...] = ("claude", "codex", "antigravity")

WORKER_PRESETS: tuple[dict, ...] = (
    {
        "id": "openrouter",
        "label": "OpenRouter ZDR",
        "endpoint": "https://openrouter.ai/api/v1",
        "default_model": "qwen/qwen3-coder",
        "zdr": True,
        "needs_key": True,
        "note": "recommended · ZDR routing",
    },
    {
        "id": "fireworks",
        "label": "Fireworks AI",
        "endpoint": "https://api.fireworks.ai/inference/v1",
        "default_model": "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct",
        "zdr": True,
        "needs_key": True,
        "note": "fast inference",
    },
    {
        "id": "together",
        "label": "Together AI",
        "endpoint": "https://api.together.xyz/v1",
        "default_model": "Qwen/Qwen2.5-Coder-32B-Instruct",
        "zdr": True,
        "needs_key": True,
        "note": "open weights",
    },
    {
        "id": "ollama",
        "label": "Local / Ollama",
        "endpoint": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5-coder:32b",
        "zdr": False,
        "needs_key": False,
        "note": "offline · no key required",
    },
    {
        "id": "custom",
        "label": "Custom OpenAI-Compat",
        "endpoint": "",
        "default_model": "",
        "zdr": True,
        "needs_key": True,
        "note": "bring your own endpoint",
    },
)

_PRESETS_BY_ID: dict[str, dict] = {item["id"]: item for item in WORKER_PRESETS}


def default_providers_path() -> Path:
    """Resolve the global `providers.toml` path (overridable via `OPENFARM_PROVIDERS_FILE`)."""
    env = os.environ.get("OPENFARM_PROVIDERS_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "openfarm" / "providers.toml"


def default_worker_key_path(providers_path: Path | None = None) -> Path:
    base = (providers_path or default_providers_path()).expanduser().parent
    return base / "worker.key"


def is_placeholder_endpoint(url: str) -> bool:
    """True when `[model] endpoint` is empty or the init/adopt placeholder."""
    cleaned = str(url or "").strip().lower()
    return (not cleaned) or ("api.example.invalid" in cleaned)


def _toml_str(value: str) -> str:
    return json.dumps(str(value))


def _write_key_atomic_0600(key_file: Path, secret: str) -> Path:
    cleaned = str(secret or "").strip()
    if not cleaned:
        raise Refused("empty-key", "API key cannot be empty")
    target = Path(key_file).expanduser()
    if target.is_symlink():
        raise Refused("symlink-key", f"refusing to overwrite symlink key file {target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    if str(resolved_parent) not in ("/", "/tmp", "/var/tmp"):
        os.chmod(resolved_parent, 0o700)

    fd, tmp_name = tempfile.mkstemp(prefix=".openfarm-key-", dir=str(resolved_parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cleaned + "\n")
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _read_key_safe(key_file: Path | None) -> str:
    if key_file is None:
        return ""
    p = Path(key_file).expanduser()
    if p.is_symlink():
        raise Refused("symlink-key", f"refusing to read symlink key file {p}")
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


def load_global_providers(path: Path | None = None) -> dict:
    """Load global provider settings from `providers.toml` (never returning raw secrets)."""
    target = Path(path).expanduser() if path is not None else default_providers_path()
    default_key = default_worker_key_path(target)
    base = {
        "configured": False,
        "path": str(target),
        "frontier": {"provider": "claude"},
        "worker": {
            "preset": "openrouter",
            "endpoint": "",
            "model": "",
            "key_file": str(default_key),
            "key_present": default_key.is_file() and not default_key.is_symlink() and default_key.stat().st_size > 0,
            "zdr": True,
        },
        "presets": [dict(p) for p in WORKER_PRESETS],
    }
    if not target.is_file() or target.is_symlink():
        return base
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return base
    if not isinstance(data, dict):
        return base

    fr = data.get("frontier") if isinstance(data.get("frontier"), dict) else {}
    wk = data.get("worker") if isinstance(data.get("worker"), dict) else {}
    fr_prov = str(fr.get("provider") or "claude").strip().lower()
    if fr_prov not in VALID_FRONTIER_PROVIDERS:
        fr_prov = "claude"

    preset = str(wk.get("preset") or "custom").strip().lower()
    if preset not in _PRESETS_BY_ID:
        preset = "custom"
    endpoint = str(wk.get("endpoint") or "").strip().rstrip("/")
    model = str(wk.get("model") or "").strip()
    kf_str = str(wk.get("key_file") or default_key).strip()
    kf = Path(kf_str).expanduser()
    key_present = bool(kf.is_file() and not kf.is_symlink() and kf.stat().st_size > 0)
    zdr = bool(wk.get("zdr", True))
    needs_key = bool(_PRESETS_BY_ID.get(preset, {}).get("needs_key", True))
    configured = (not is_placeholder_endpoint(endpoint)) and (key_present or not needs_key)

    return {
        "configured": configured,
        "path": str(target),
        "frontier": {"provider": fr_prov},
        "worker": {
            "preset": preset,
            "endpoint": endpoint,
            "model": model,
            "key_file": str(kf),
            "key_present": key_present,
            "zdr": zdr,
        },
        "presets": [dict(p) for p in WORKER_PRESETS],
    }


def save_global_providers(
    *,
    frontier_provider: str = "claude",
    worker_preset: str = "openrouter",
    endpoint: str = "",
    model: str = "",
    key_file: Path | None = None,
    api_key: str = "",
    zdr: bool | None = None,
    path: Path | None = None,
) -> dict:
    """Persist global Frontier + Worker settings atomically at mode 0600 (never storing raw secrets)."""
    target = Path(path).expanduser() if path is not None else default_providers_path()
    if target.is_symlink():
        raise Refused("symlink-providers", f"refusing to overwrite symlink {target}")

    fr_prov = str(frontier_provider or "claude").strip().lower()
    if fr_prov not in VALID_FRONTIER_PROVIDERS:
        raise Refused("bad-frontier-provider", f"unknown frontier provider {frontier_provider!r}")

    preset_id = str(worker_preset or "custom").strip().lower()
    if preset_id not in _PRESETS_BY_ID:
        raise Refused("bad-worker-preset", f"unknown worker preset {worker_preset!r}")
    preset = _PRESETS_BY_ID[preset_id]

    resolved_ep = str(endpoint or preset["endpoint"]).strip().rstrip("/")
    if not (resolved_ep.startswith("http://") or resolved_ep.startswith("https://")) or is_placeholder_endpoint(resolved_ep):
        raise Refused("bad-endpoint", "Worker endpoint must be a real http(s):// URL")

    resolved_model = str(model or preset["default_model"]).strip()
    resolved_zdr = bool(preset["zdr"] if zdr is None else zdr)
    resolved_kf = Path(key_file).expanduser() if key_file is not None else default_worker_key_path(target)
    if resolved_kf.is_symlink():
        raise Refused("symlink-key", f"refusing to use symlink key file {resolved_kf}")

    if str(api_key or "").strip():
        _write_key_atomic_0600(resolved_kf, api_key)

    toml_text = (
        "# Openfarm global provider profile (v0.10.0). Raw API keys are stored in key_file (mode 0600).\n"
        "[frontier]\n"
        f"provider = {_toml_str(fr_prov)}\n\n"
        "[worker]\n"
        f"preset = {_toml_str(preset_id)}\n"
        f"endpoint = {_toml_str(resolved_ep)}\n"
        f"model = {_toml_str(resolved_model)}\n"
        f"key_file = {_toml_str(str(resolved_kf))}\n"
        f"zdr = {'true' if resolved_zdr else 'false'}\n"
    )

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    if str(resolved_parent) not in ("/", "/tmp", "/var/tmp"):
        os.chmod(resolved_parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".openfarm-providers-", dir=str(resolved_parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(toml_text)
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return load_global_providers(target)


def probe_worker_endpoint(
    endpoint: str,
    *,
    key_file: Path | None = None,
    api_key: str = "",
    model: str = "",
    zdr: bool = True,
    timeout_s: float = 5.0,
) -> dict:
    """Run a lightweight stdlib GET <endpoint>/models probe with x-cmd-zdr: 1 (never leaking secrets)."""
    ep = str(endpoint or "").strip().rstrip("/")
    if not (ep.startswith("http://") or ep.startswith("https://")) or is_placeholder_endpoint(ep):
        return {
            "ok": False,
            "status": 0,
            "latency_ms": 0,
            "zdr_sent": bool(zdr),
            "model": str(model or "").strip(),
            "model_found": None,
            "models_count": 0,
            "error": "endpoint must be a real http(s):// URL",
        }

    secret = str(api_key or "").strip()
    if not secret and key_file is not None:
        try:
            secret = _read_key_safe(key_file)
        except Refused as exc:
            return {
                "ok": False,
                "status": 0,
                "latency_ms": 0,
                "zdr_sent": bool(zdr),
                "model": str(model or "").strip(),
                "model_found": None,
                "models_count": 0,
                "error": str(exc),
            }

    url = f"{ep}/models"
    headers = {
        "Accept": "application/json",
        "User-Agent": "openfarm-probe/0.10.0",
    }
    if zdr:
        headers["x-cmd-zdr"] = "1"
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    status = 0
    error = ""
    models: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read(512 * 1024).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict):
                items = parsed.get("data") if isinstance(parsed.get("data"), list) else parsed.get("models")
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            mid = str(item.get("id") or item.get("name") or "").strip()
                            if mid:
                                models.append(mid)
                        elif isinstance(item, str) and item.strip():
                            models.append(item.strip())
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        error = f"HTTP {status}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        error = str(getattr(exc, "reason", exc) or exc)

    if secret and secret in error:
        error = error.replace(secret, "[REDACTED]")
    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    wanted = str(model or "").strip()
    model_found: bool | None = None
    if wanted and models:
        model_found = wanted in models or any(m.endswith(f"/{wanted}") for m in models)

    return {
        "ok": 200 <= status < 300,
        "status": status,
        "latency_ms": elapsed_ms,
        "zdr_sent": bool(zdr),
        "model": wanted,
        "model_found": model_found,
        "models_count": len(models),
        "error": error,
    }
