"""The four model roles.

Each is a stateless call with a fresh context. The harness builds the brief from
files and command output, never from another model's notes.
"""

from __future__ import annotations

import inspect
import json
import os
import random
import re
from pathlib import Path

from ..briefs import (Brief, checker_pass1_brief, checker_pass2_brief,
                      implementer_brief, planner_brief, test_writer_brief)
from ..cards import Card
from ..edits import Edit, parse_edits
from ..errors import Invalid, Refused
from ..globs import matches_any, normalise


class RoleCall:
    """One model call, and what it returned."""

    def __init__(self, role: str, brief: Brief, content: str,
                 model: str = "", temperature: float = 0.0,
                 seed: int | None = None, tokens_in: int = 0,
                 tokens_out: int = 0, spend: float = 0.0,
                 finish_reason: str = "stop",
                 effort: str = "", effort_param: str = "") -> None:
        self.role = role
        self.brief = brief
        self.content = content
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.spend = spend
        self.finish_reason = finish_reason
        self.effort = effort
        self.effort_param = effort_param

    @property
    def cut_off(self) -> bool:
        """The answer stopped at the token limit, so its last block is unfinished."""
        return self.finish_reason == "length"

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "model": self.model,
                "temperature": self.temperature, "seed": self.seed,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "spend": self.spend, "finish_reason": self.finish_reason,
                "effort": self.effort}


class Roles:
    """Calling the backend for each role, with its own model and temperature."""

    def __init__(self, cfg, backend, card_id: str = "",
                 rng_seed: int | None = None) -> None:
        self.cfg = cfg
        self.backend = backend
        self.card_id = card_id
        seed_value = (rng_seed if rng_seed is not None
                      else int.from_bytes(os.urandom(16), "big"))
        self._rng = random.Random(seed_value)
        self.calls: list[RoleCall] = []

    def _settings(self, role: str):
        # The config names a `checker`, but the flow calls the two passes by their
        # own names. Without this the checker would silently run on the implementer's
        # model and temperature, which is the one thing the spec is explicit about.
        aliases = {"checker_pass1": "checker", "checker_pass2": "checker"}
        role_model = self.cfg.role_models.get(
            aliases.get(role, role))
        if role_model is None:
            role_model = self.cfg.role_models.get("implementer")
        model = role_model.model if role_model else ""
        temperature = role_model.temperature if role_model else 0.2
        vary_seed = role_model.vary_seed if role_model else False
        seed = self._rng.randrange(1, 2**31 - 1) if vary_seed else 7
        return model, temperature, seed

    def _complete(self, *, role: str, model: str, temperature: float,
                  messages: list[dict], seed: int, max_tokens: int,
                  response_format: str | None, attempt: int,
                  effort: str, effort_param: str):
        base = {
            "role": role,
            "model": model,
            "temperature": temperature,
            "messages": messages,
            "seed": seed,
            "max_tokens": max_tokens,
            "response_format": response_format,
        }
        extras = {
            "card_id": self.card_id,
            "attempt": attempt,
            "effort": effort,
            "effort_param": effort_param,
        }
        try:
            sig = inspect.signature(self.backend.complete)
            params = sig.parameters
            has_varkw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values()
            )
            chosen = {k: v for k, v in extras.items() if has_varkw or k in params}
        except (ValueError, TypeError):
            chosen = None

        if chosen is not None:
            try:
                return self.backend.complete(**base, **chosen)
            except TypeError:
                pass

        for subset in (
            ("card_id", "attempt", "effort", "effort_param"),
            ("effort", "effort_param"),
            ("card_id", "attempt"),
        ):
            try:
                return self.backend.complete(
                    **base, **{k: extras[k] for k in subset}
                )
            except TypeError:
                continue
        return self.backend.complete(**base)

    def call(self, role: str, brief: Brief, attempt: int = 1,
             response_format: str | None = None,
             model: str | None = None,
             effort: str | None = None,
             effort_param: str | None = None) -> RoleCall:
        default_model, temperature, seed = self._settings(role)
        effective_model = model if model is not None else default_model
        if effort is None and effort_param is None:
            if hasattr(self.cfg, "effort_for_role"):
                eff, eff_param = self.cfg.effort_for_role(role)
            else:
                eff, eff_param = ("", "")
        else:
            eff = effort or ""
            if effort_param is not None:
                eff_param = effort_param
            elif eff and hasattr(self.cfg, "tier_for_model"):
                eff_param = self.cfg.tier_for_model(effective_model).effort_param
            else:
                eff_param = "reasoning_effort" if eff else ""
        messages = [{"role": "user", "content": brief.body}]
        max_tokens = getattr(self.cfg, "model_max_tokens", 4096)
        completion = self._complete(
            role=role, model=effective_model, temperature=temperature,
            messages=messages, seed=seed, max_tokens=max_tokens,
            response_format=response_format, attempt=attempt,
            effort=eff, effort_param=eff_param,
        )

        record = RoleCall(role, brief, completion.content, model=effective_model,
                          temperature=temperature, seed=seed,
                          tokens_in=completion.tokens_in,
                          tokens_out=completion.tokens_out,
                          spend=completion.cost,
                          finish_reason=str(getattr(completion, "finish_reason",
                                                    "stop") or "stop"),
                          effort=eff, effort_param=eff_param)
        self.calls.append(record)
        return record


# -- parsing what a role returned ---------------------------------------------

def parse_answer_edits(call: RoleCall) -> list[Edit]:
    """The edits in a worker's answer, refusing one the token limit cut off.

    A cut-off answer can still parse: its last file simply ends mid-line. Applying
    it would freeze a broken test file and blame the worker for a syntax error.
    """
    if call.cut_off:
        raise Invalid(
            "answer-cut-off",
            f"the answer was cut off at the token limit ({call.tokens_out} tokens "
            "out) before it finished; write less, or raise [model] max_tokens")
    return parse_edits(call.content)


class Plan:
    """A planner's split: 2 to 5 sequential steps."""

    def __init__(self, steps: list[dict], cannot_split: bool = False) -> None:
        self.steps = steps
        self.cannot_split = cannot_split

    def as_dict(self) -> dict:
        return {"steps": self.steps, "cannot_split": self.cannot_split}


def parse_plan(content: str, card: Card | None = None) -> Plan:
    text = content.strip()
    payload = _json_block(text)
    if text == "CANNOT_SPLIT" or (payload is None and "CANNOT_SPLIT" in text):
        return Plan([], cannot_split=True)
    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list) or not 2 <= len(steps) <= 5:
        raise Invalid("bad-plan",
                      "a plan needs 2 to 5 steps, or exactly CANNOT_SPLIT")
    allowed_tests = set(card.test_names) if card is not None else None
    allowed_touches = list(card.touches) if card is not None else []
    cleaned = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise Invalid("bad-plan", f"step {index} is not an object")
        tests = [str(t) for t in step.get("tests", []) or []]
        if allowed_tests is not None and allowed_tests:
            if not tests or any(t not in allowed_tests for t in tests):
                raise Invalid(
                    "bad-plan-test",
                    f"step {index} tests {tests!r} must be non-empty and in {sorted(allowed_tests)!r}",
                )
        entry: dict = {
            "n": int(step.get("n", index)),
            "does": str(step.get("does", "")),
            "tests": tests,
        }
        if "touches" in step and step["touches"] is not None:
            raw_touches = step["touches"]
            if not isinstance(raw_touches, list):
                raise Invalid("bad-plan", f"step {index} touches must be a list")
            step_touches = [normalise(str(t)) for t in raw_touches]
            if allowed_touches:
                for t in step_touches:
                    if not matches_any(t, allowed_touches):
                        raise Invalid(
                            "bad-plan-touch",
                            f"step {index} touch {t!r} is outside card.touches {allowed_touches!r}",
                        )
            entry["touches"] = step_touches
        cleaned.append(entry)
    return Plan(cleaned)


def wants_planner(card: Card, mode: str = "auto") -> bool:
    """Planner on only when mode/card criteria indicate multi-step splitting."""
    if mode == "off":
        return False
    if mode == "on":
        return len(card.tests) >= 2
    if len(card.tests) >= 4 and len({t.file for t in card.tests if t.file}) >= 2:
        return True
    if len(card.touches) >= 3 and len(card.tests) >= 2:
        return True
    text = (card.done_text or "")
    groups = re.findall(r"^\s*(?:[-*]|\d+[.)])\s+", text, flags=re.MULTILINE)
    return len(groups) >= 2 and len(card.tests) >= 2


class Restatement:
    """The checker's own words for the done text, and how far they drifted."""

    def __init__(self, text: str, mismatch: bool, overlap: float) -> None:
        self.text = text
        self.mismatch = mismatch
        self.overlap = overlap

    def as_dict(self) -> dict:
        return {"text": self.text, "mismatch": self.mismatch,
                "overlap": round(self.overlap, 3)}


def parse_restatement(content: str, card: Card) -> Restatement:
    """Compare the checker's restatement with the named tests.

    A shared misreading is the one thing a same-model checker cannot remove, so a
    restatement that drifts from the tests is flagged in the dossier.
    """
    text = content.strip()
    marker = re.search(r"RESTATEMENT\s*:\s*(.+)", content,
                       flags=re.IGNORECASE | re.DOTALL)
    if marker:
        text = marker.group(1).strip()

    test_words = set(_words(" ".join(card.test_names)) + _words(card.done_text))
    said = set(_words(text))
    if not test_words:
        return Restatement(text[:600], mismatch=False, overlap=0.0)
    overlap = len(said & test_words) / max(1, len(test_words))
    mismatch = overlap < 0.25
    return Restatement(text[:600], mismatch=mismatch, overlap=overlap)


def _words(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", text)
            if len(w) > 3]


class CounterTests:
    """The checker's counter-tests: report-only in the first pilot batch."""

    def __init__(self, tests: list[dict], raw: str = "") -> None:
        self.tests = tests
        self.raw = raw

    def as_dict(self) -> dict:
        return {"tests": self.tests}


def parse_counter_tests(content: str) -> CounterTests:
    """Read up to two counter-tests out of pass 2's answer."""
    payload = _json_block(content)
    tests = []
    if isinstance(payload, dict):
        for entry in payload.get("counter_tests", []) or []:
            if isinstance(entry, dict) and entry.get("name"):
                tests.append({"name": str(entry["name"]),
                              "file": str(entry.get("file", "")),
                              "content": str(entry.get("content", ""))})
            if len(tests) == 2:
                break
    if not tests:
        for block in re.finditer(
                r"COUNTER_TEST\s*:\s*(?P<name>\S+)\s*\n"
                r"(?P<content>.*?)(?=\nCOUNTER_TEST|\Z)",
                content, flags=re.DOTALL):
            tests.append({"name": block.group("name"),
                          "file": "", "content": block.group("content").strip()})
            if len(tests) == 2:
                break
    return CounterTests(tests, raw=content[:4000])


def _json_block(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# -- briefs, built here so the flow doesn't need to know the shapes ------------

def build_planner_brief(card, book, harness_guidance: str = "") -> Brief:
    return planner_brief(card, book, harness_guidance=harness_guidance)


def build_test_writer_brief(card, book, pack, conventions="", attempt=1,
                            previous_failure="", guidance: str = "",
                            context_files: list[str] | None = None,
                            harness_guidance: str = "") -> Brief:
    return test_writer_brief(card, book, pack, conventions, attempt,
                             previous_failure, guidance=guidance,
                             context_files=context_files,
                             harness_guidance=harness_guidance)


def build_implementer_brief(card, book, frozen_tests, attempt=1, diff="",
                            failure="", counter_tests="", step=None,
                            guidance: str = "", step_context: str = "",
                            context_files: list[str] | None = None,
                            harness_guidance: str = "") -> Brief:
    return implementer_brief(card, book, frozen_tests, attempt, diff, failure,
                             counter_tests, step, guidance=guidance,
                             step_context=step_context,
                             context_files=context_files,
                             harness_guidance=harness_guidance)


def build_checker_pass1_brief(card, test_diff) -> Brief:
    return checker_pass1_brief(card, test_diff)


def build_checker_pass2_brief(card, full_diff, gate_results,
                              modified_files) -> Brief:
    return checker_pass2_brief(card, full_diff, gate_results, modified_files)
