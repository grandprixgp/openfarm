"""The model backend.

The allowlist is how "nothing from the repository goes to an outside service" is
enforced in code: the backend refuses any endpoint not on the list, and refuses to
follow a redirect off it. The key is read from a file outside the repository, and
is never written into a message, a log or a response.
"""

from __future__ import annotations

import errno
import http.client
import json
import math
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import __version__
from ..errors import Infra, Invalid, Refused

_RETRYABLE_HTTP_CODES = frozenset({429, 502, 503, 504})
_MAX_RETRY_DELAY_S = 30.0
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024  # 32 MiB cap on completion responses
_RESERVED_EFFORT_PARAMS = frozenset({
    "model", "messages", "max_tokens", "temperature", "seed", "response_format",
})


class Completion:
    """One model answer, and what it cost."""

    __slots__ = ("content", "tokens_in", "tokens_out", "model", "finish_reason",
                 "latency_s", "cost", "tokens_in_cached")

    def __init__(self, content: str, tokens_in: int = 0, tokens_out: int = 0,
                 model: str = "", finish_reason: str = "stop",
                 latency_s: float = 0.0, cost: float = 0.0,
                 tokens_in_cached: int = 0) -> None:
        self.content = content
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.model = model
        self.finish_reason = finish_reason
        self.latency_s = latency_s
        self.cost = cost
        self.tokens_in_cached = max(0, int(tokens_in_cached or 0))

    @property
    def tokens_in_fresh(self) -> int:
        return max(0, self.tokens_in - self.tokens_in_cached)

    def as_dict(self) -> dict:
        return {"content": self.content, "tokens_in": self.tokens_in,
                "tokens_in_fresh": self.tokens_in_fresh,
                "tokens_in_cached": self.tokens_in_cached,
                "tokens_out": self.tokens_out, "model": self.model,
                "finish_reason": self.finish_reason,
                "latency_s": self.latency_s, "cost": self.cost}


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise Invalid("bad-url", f"{url!r} is not an absolute URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _within(url: str, allowed: list[str]) -> bool:
    """True when `url` is one of the allowed endpoints or sits strictly under one."""
    target = urllib.parse.urlsplit(url)
    target_scheme = target.scheme.lower()
    if target_scheme not in ("http", "https") or not target.netloc or target.fragment:
        return False
    if "\\" in target.path or "%2f" in target.path.lower() or "%5c" in target.path.lower():
        return False
    target_segs = [seg for seg in target.path.split("/") if seg]
    if any(urllib.parse.unquote(seg) in (".", "..") for seg in target_segs):
        return False
    target_netloc = target.netloc.lower()

    for entry in allowed:
        base = urllib.parse.urlsplit(entry)
        if not base.scheme or not base.netloc or base.fragment:
            continue
        if target_scheme != base.scheme.lower():
            continue
        if target_netloc != base.netloc.lower():
            continue
        if target.query and target.query != base.query:
            continue
        base_segs = [seg for seg in base.path.split("/") if seg]
        if len(target_segs) >= len(base_segs) and target_segs[:len(base_segs)] == base_segs:
            return True
    return False


def _retry_delay(attempt: int, headers=None) -> float:
    if headers is not None:
        raw = headers.get("Retry-After")
        if raw is not None:
            try:
                val = float(str(raw).strip())
                if math.isfinite(val):
                    return min(_MAX_RETRY_DELAY_S, max(0.0, val))
            except ValueError:
                pass
    return min(_MAX_RETRY_DELAY_S, 0.5 * (2 ** attempt))


def _safe_int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _effective_port(parts: urllib.parse.SplitResult) -> int | None:
    if parts.port is not None:
        return parts.port
    scheme = parts.scheme.lower()
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _same_origin(url_a: str, url_b: str) -> bool:
    a = urllib.parse.urlsplit(url_a)
    b = urllib.parse.urlsplit(url_b)
    return (
        a.scheme.lower() == b.scheme.lower()
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and _effective_port(a) == _effective_port(b)
    )


def _redact_secret(text: str, secret: str) -> str:
    if not secret or not text:
        return text
    out = text.replace(secret, "[REDACTED]")
    for line in secret.splitlines():
        token = line.strip()
        if token and token in out:
            out = out.replace(token, "[REDACTED]")
    return out


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect off the allowlist or downgrade HTTPS; strip auth cross-origin."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = list(allowed)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_url = urllib.parse.urljoin(req.full_url, newurl)
        if not _within(target_url, self.allowed):
            raise Refused(
                "redirect-off-allowlist",
                f"the endpoint tried to redirect to {target_url}, which is not on the "
                f"allowlist; nothing was sent")
        orig = urllib.parse.urlsplit(req.full_url)
        dest = urllib.parse.urlsplit(target_url)
        if orig.scheme.lower() == "https" and dest.scheme.lower() != "https":
            raise Refused(
                "redirect-downgrade",
                "refusing HTTPS-to-HTTP redirect; nothing was sent")
        new_req = super().redirect_request(req, fp, code, msg, headers, target_url)
        if new_req is not None and not _same_origin(req.full_url, target_url):
            for hdr_dict in (new_req.headers, new_req.unredirected_hdrs):
                for key in [k for k in hdr_dict if k.lower() in ("authorization", "proxy-authorization")]:
                    del hdr_dict[key]
        return new_req


class OpenAICompatibleBackend:
    """One chat-completion call per role, to an OpenAI-compatible endpoint."""

    name = "openai-compatible"

    def __init__(self, endpoint: str, allowed_endpoints: list[str],
                 key_file: Path | None = None, timeout_s: float = 300.0,
                 cost_per_1k_in: float = 0.0, cost_per_1k_out: float = 0.0,
                 retries: int = 2, sleep_fn=None,
                 headers: dict[str, str] | None = None) -> None:
        if not endpoint:
            raise Invalid("no-endpoint", "the model backend needs an endpoint")
        if not allowed_endpoints:
            raise Invalid(
                "no-allowlist",
                "the model backend refuses to run without allowed_endpoints: that "
                "list is how 'nothing leaves the repository' is enforced")
        _origin(endpoint)
        if not _within(endpoint, allowed_endpoints):
            raise Invalid("endpoint-not-allowed",
                          f"{endpoint} is not on the allowlist")
        self.endpoint = endpoint.rstrip("/")
        self.allowed_endpoints = list(allowed_endpoints)
        self.key_file = Path(key_file).expanduser() if key_file else None
        self.timeout_s = timeout_s
        self.cost_per_1k_in = cost_per_1k_in
        self.cost_per_1k_out = cost_per_1k_out
        self.retries = max(0, int(retries))
        self.headers = dict(headers or {})
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._opener = urllib.request.build_opener(
            _AllowlistRedirectHandler(self.allowed_endpoints))

    @property
    def prices_spend(self) -> bool:
        """True when this backend can report a cost per call.

        Without prices every `spend` is zero, so the budget's spend caps cannot
        fire. That is a property of the configuration, not of a night's luck, and
        the run record says so.
        """
        return self.cost_per_1k_in > 0 or self.cost_per_1k_out > 0

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "complete"]}

    def _key(self) -> str:
        if self.key_file is None:
            return ""
        p = self.key_file
        if p.is_symlink():
            raise Refused("symlink-key", f"refusing to read symlink key file {p}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(str(p), flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise Refused("symlink-key", f"refusing to read symlink key file {p}") from exc
            raise Infra("key-unreadable",
                        f"cannot read the key file {p}: {exc}") from exc
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                st = os.fstat(handle.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise Refused("key-not-regular", f"refusing to read non-regular key file {p}")
                key = handle.read().strip()
        except OSError as exc:
            raise Infra("key-unreadable",
                        f"cannot read the key file {p}: {exc}") from exc
        if "\r" in key or "\n" in key:
            raise Invalid("bad-key", f"key file {p} contains multiple lines or invalid CR/LF characters")
        return key

    def complete(self, role: str, model: str, temperature: float,
                 messages: list[dict], seed: int | None = None,
                 max_tokens: int = 4096,
                 response_format: str | None = None,
                 effort: str = "",
                 effort_param: str = "") -> Completion:
        url = f"{self.endpoint}/chat/completions"
        if not _within(url, self.allowed_endpoints):
            raise Refused("endpoint-not-allowed",
                          f"{url} is not on the allowlist")

        payload: dict = {"model": model, "temperature": temperature,
                         "messages": messages, "max_tokens": max_tokens}
        if seed is not None:
            payload["seed"] = seed
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if effort and effort_param:
            if (effort_param in _RESERVED_EFFORT_PARAMS
                    or any(ch.isspace() for ch in effort_param)):
                raise Invalid("bad-effort-param",
                              f"effort_param {effort_param!r} is invalid")
            payload[effort_param] = effort

        secret = self._key()
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            # Python's default User-Agent is blocked outright by some providers'
            # firewalls (Command Code answers 403, Cloudflare error 1010).
            headers={"User-Agent": f"openfarm/{__version__}",
                     **self.headers,
                     "Content-Type": "application/json",
                     "Authorization": f"Bearer {secret}"},
            method="POST")

        started = time.monotonic()
        body = ""
        for attempt in range(self.retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout_s) as response:
                    raw_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                        raise Infra("response-too-large",
                                    "the endpoint response exceeded the maximum allowed size")
                    body = raw_bytes.decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                if exc.code in _RETRYABLE_HTTP_CODES and attempt < self.retries:
                    self._sleep(_retry_delay(attempt, exc.headers))
                    continue
                raise Infra("http-error", f"the endpoint answered {exc.code}") from exc
            except urllib.error.URLError as exc:
                if attempt < self.retries:
                    self._sleep(_retry_delay(attempt))
                    continue
                raise Infra("endpoint-unreachable",
                            _redact_secret(str(exc.reason), secret)) from exc
            except TimeoutError as exc:
                if attempt < self.retries:
                    self._sleep(_retry_delay(attempt))
                    continue
                raise Infra("timeout", "the endpoint did not answer in time") from exc
            except (http.client.HTTPException, OSError) as exc:
                if attempt < self.retries:
                    self._sleep(_retry_delay(attempt))
                    continue
                raise Infra("endpoint-unreachable",
                            _redact_secret(str(exc), secret)) from exc
            except ValueError as exc:
                raise Infra("http-request-error",
                            _redact_secret(str(exc), secret)) from exc
        latency = time.monotonic() - started

        try:
            answer = json.loads(body)
            choice = answer["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise Invalid("bad-response",
                          "the endpoint did not answer an OpenAI-shaped completion"
                          ) from exc

        raw_usage = answer.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        tokens_in = _safe_int(usage.get("prompt_tokens"))
        tokens_out = _safe_int(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details")
        tokens_in_cached = (
            _safe_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        ) or _safe_int(usage.get("prompt_cache_hit_tokens"))
        cost = (tokens_in / 1000.0 * self.cost_per_1k_in
                + tokens_out / 1000.0 * self.cost_per_1k_out)
        return Completion(
            content=str(content or ""),
            tokens_in=tokens_in, tokens_out=tokens_out,
            tokens_in_cached=tokens_in_cached,
            model=str(answer.get("model", model)),
            finish_reason=str(choice.get("finish_reason", "stop")),
            latency_s=latency, cost=cost)


class ScriptedBackend:
    """A backend whose answers come from a script. Used by the tests and --dry-run.

    The script is a dict keyed by `"<card-id>/<role>/<attempt>"`, falling back to
    `"<role>"` and then `"*"`. A value is either a string (the content) or a dict
    with `content`, `tokens_in`, `tokens_out` and `fail` (an error kind to raise).
    """

    name = "scripted"

    def __init__(self, script: dict | None = None,
                 default: str = "") -> None:
        self.script = dict(script or {})
        self.default = default
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "complete"]}

    def complete(self, role: str, model: str, temperature: float,
                 messages: list[dict], seed: int | None = None,
                 max_tokens: int = 4096,
                 response_format: str | None = None,
                 card_id: str = "", attempt: int = 1,
                 effort: str = "", effort_param: str = "") -> Completion:
        call = {"role": role, "card_id": card_id, "attempt": attempt,
                "messages": messages, "seed": seed, "model": model,
                "temperature": temperature, "effort": effort,
                "effort_param": effort_param}
        with self._lock:
            self.calls.append(call)

        keys: list[str] = []
        if card_id:
            if effort:
                keys.append(f"{card_id}/{role}/{attempt}/{effort}")
                keys.append(f"{card_id}/{role}/{effort}")
            keys.append(f"{card_id}/{role}/{attempt}")
            keys.append(f"{card_id}/{role}")
        if effort:
            keys.append(f"{role}/{attempt}/{effort}")
            keys.append(f"{role}/{effort}")
        keys.extend((f"{role}/{attempt}", role, "*"))

        entry = None
        for key in keys:
            if key in self.script:
                entry = self.script[key]
                break
        if entry is None:
            entry = self.default

        if isinstance(entry, str):
            return Completion(content=entry, model=model or "scripted",
                              tokens_in=_rough_tokens(messages),
                              tokens_out=_rough_tokens_text(entry))
        if not isinstance(entry, dict):
            raise Invalid("bad-script", f"script entry {entry!r} is unusable")

        failure = entry.get("fail")
        if failure:
            kind = str(entry.get("kind", "infra"))
            cls = {"refused": Refused, "invalid": Invalid,
                   "infra": Infra}.get(kind, Infra)
            raise cls(str(entry.get("code", "scripted-failure")),
                      f"scripted {kind} for {role}")

        content = str(entry.get("content", ""))
        return Completion(
            content=content, model=model or "scripted",
            tokens_in=int(entry.get("tokens_in", _rough_tokens(messages))),
            tokens_out=int(entry.get("tokens_out", _rough_tokens_text(content))),
            finish_reason=str(entry.get("finish_reason", "stop")),
            cost=float(entry.get("cost", 0.0)))


def _rough_tokens(messages: list[dict]) -> int:
    text = " ".join(str(m.get("content", "")) for m in messages)
    return _rough_tokens_text(text)


def _rough_tokens_text(text: str) -> int:
    return max(1, len(str(text)) // 4)


def load_script(path: Path) -> dict:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise Invalid("bad-script", "a script must be a JSON object")
    return payload


def open_backend(cfg, script_path: Path | None = None):
    """The backend the config names, or a scripted one when asked."""
    if script_path is not None:
        return ScriptedBackend(load_script(script_path))
    if cfg.model_adapter in ("scripted", "stub"):
        return ScriptedBackend()
    from .acp import is_acp_adapter
    if is_acp_adapter(cfg.model_adapter):
        from .claude_worker import open_acp_worker
        return open_acp_worker(cfg)
    if cfg.model_adapter.startswith("cmd:"):
        from ..protocol import CmdModelBackend
        return CmdModelBackend(
            cfg.model_adapter,
            cfg.repo,
            cost_per_1k_in=getattr(cfg, "cost_per_1k_in", 0.0),
            cost_per_1k_out=getattr(cfg, "cost_per_1k_out", 0.0),
        )
    return OpenAICompatibleBackend(
        endpoint=cfg.endpoint,
        allowed_endpoints=cfg.allowed_endpoints,
        key_file=cfg.key_file,
        cost_per_1k_in=getattr(cfg, "cost_per_1k_in", 0.0),
        cost_per_1k_out=getattr(cfg, "cost_per_1k_out", 0.0),
        headers=getattr(cfg, "model_headers", {}),
    )
