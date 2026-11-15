"""The model backend.

The allowlist is how "nothing from the repository goes to an outside service" is
enforced in code: the backend refuses any endpoint not on the list, and refuses to
follow a redirect off it. The key is read from a file outside the repository, and
is never written into a message, a log or a response.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..errors import Infra, Invalid, Refused


class Completion:
    """One model answer, and what it cost."""

    __slots__ = ("content", "tokens_in", "tokens_out", "model", "finish_reason",
                 "latency_s", "cost")

    def __init__(self, content: str, tokens_in: int = 0, tokens_out: int = 0,
                 model: str = "", finish_reason: str = "stop",
                 latency_s: float = 0.0, cost: float = 0.0) -> None:
        self.content = content
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.model = model
        self.finish_reason = finish_reason
        self.latency_s = latency_s
        self.cost = cost

    def as_dict(self) -> dict:
        return {"content": self.content, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "model": self.model,
                "finish_reason": self.finish_reason,
                "latency_s": self.latency_s, "cost": self.cost}


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise Invalid("bad-url", f"{url!r} is not an absolute URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _within(url: str, allowed: list[str]) -> bool:
    """True when `url` is one of the allowed endpoints or sits under one."""
    target = url.rstrip("/")
    for entry in allowed:
        base = entry.rstrip("/")
        if target == base:
            return True
        if target.startswith(base + "/"):
            return True
    return False


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect off the allowlist."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = list(allowed)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _within(newurl, self.allowed):
            raise Refused(
                "redirect-off-allowlist",
                f"the endpoint tried to redirect to {newurl}, which is not on the "
                f"allowlist; nothing was sent")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class OpenAICompatibleBackend:
    """One chat-completion call per role, to an OpenAI-compatible endpoint."""

    name = "openai-compatible"

    def __init__(self, endpoint: str, allowed_endpoints: list[str],
                 key_file: Path | None = None, timeout_s: float = 300.0,
                 cost_per_1k_in: float = 0.0, cost_per_1k_out: float = 0.0) -> None:
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
        self._opener = urllib.request.build_opener(
            _AllowlistRedirectHandler(self.allowed_endpoints))

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "complete"]}

    def _key(self) -> str:
        if self.key_file is None:
            return ""
        try:
            return self.key_file.read_text().strip()
        except OSError as exc:
            raise Infra("key-unreadable",
                        f"cannot read the key file {self.key_file}: {exc}") from exc

    def complete(self, role: str, model: str, temperature: float,
                 messages: list[dict], seed: int | None = None,
                 max_tokens: int = 4096,
                 response_format: str | None = None) -> Completion:
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

        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._key()}"},
            method="POST")

        import time
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise Infra("http-error", f"the endpoint answered {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise Infra("endpoint-unreachable", f"{exc.reason}") from exc
        except TimeoutError as exc:
            raise Infra("timeout", "the endpoint did not answer in time") from exc
        latency = time.monotonic() - started

        try:
            answer = json.loads(body)
            choice = answer["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise Invalid("bad-response",
                          "the endpoint did not answer an OpenAI-shaped completion"
                          ) from exc

        usage = answer.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        cost = (tokens_in / 1000.0 * self.cost_per_1k_in
                + tokens_out / 1000.0 * self.cost_per_1k_out)
        return Completion(
            content=str(content or ""),
            tokens_in=tokens_in, tokens_out=tokens_out,
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

    def hello(self) -> dict:
        return {"name": self.name, "versions": [1], "ops": ["hello", "complete"]}

    def complete(self, role: str, model: str, temperature: float,
                 messages: list[dict], seed: int | None = None,
                 max_tokens: int = 4096,
                 response_format: str | None = None,
                 card_id: str = "", attempt: int = 1) -> Completion:
        call = {"role": role, "card_id": card_id, "attempt": attempt,
                "messages": messages, "seed": seed, "model": model,
                "temperature": temperature}
        self.calls.append(call)

        entry = None
        for key in (f"{card_id}/{role}/{attempt}", f"{card_id}/{role}",
                    role, "*"):
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
    return OpenAICompatibleBackend(
        endpoint=cfg.endpoint,
        allowed_endpoints=cfg.allowed_endpoints,
        key_file=cfg.key_file,
    )
