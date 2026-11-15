"""The four model roles.

Each is a stateless call with a fresh context. The harness builds the brief from
files and command output, never from another model's notes.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from ..briefs import (Brief, checker_pass1_brief, checker_pass2_brief,
                      implementer_brief, planner_brief, test_writer_brief)
from ..cards import Card
from ..edits import Edit, parse_edits
from ..errors import Invalid, Refused


class RoleCall:
    """One model call, and what it returned."""

    def __init__(self, role: str, brief: Brief, content: str,
                 model: str = "", temperature: float = 0.0,
                 seed: int | None = None, tokens_in: int = 0,
                 tokens_out: int = 0, spend: float = 0.0) -> None:
        self.role = role
        self.brief = brief
        self.content = content
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.spend = spend

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "model": self.model,
                "temperature": self.temperature, "seed": self.seed,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "spend": self.spend}


class Roles:
    """Calling the backend for each role, with its own model and temperature."""

    def __init__(self, cfg, backend, card_id: str = "") -> None:
        self.cfg = cfg
        self.backend = backend
        self.card_id = card_id
        self._rng = random.Random(0)
        self.calls: list[RoleCall] = []

    def _settings(self, role: str):
        role_model = self.cfg.role_models.get(role)
        if role_model is None:
            role_model = self.cfg.role_models.get("implementer")
        model = role_model.model if role_model else ""
        temperature = role_model.temperature if role_model else 0.2
        vary_seed = role_model.vary_seed if role_model else False
        seed = self._rng.randrange(1, 2**31 - 1) if vary_seed else 7
        return model, temperature, seed

    def call(self, role: str, brief: Brief, attempt: int = 1,
             response_format: str | None = None) -> RoleCall:
        model, temperature, seed = self._settings(role)
        messages = [{"role": "user", "content": brief.body}]
        try:
            completion = self.backend.complete(
                role=role, model=model, temperature=temperature,
                messages=messages, seed=seed,
                response_format=response_format, card_id=self.card_id,
                attempt=attempt)
        except TypeError:
            # A backend that does not take card_id/attempt (a plain stub).
            completion = self.backend.complete(
                role=role, model=model, temperature=temperature,
                messages=messages, seed=seed, response_format=response_format)

        record = RoleCall(role, brief, completion.content, model=model,
                          temperature=temperature, seed=seed,
                          tokens_in=completion.tokens_in,
                          tokens_out=completion.tokens_out,
                          spend=completion.cost)
        self.calls.append(record)
        return record


# -- parsing what a role returned ---------------------------------------------

class Plan:
    """A planner's split: 2 to 5 sequential steps."""

    def __init__(self, steps: list[dict], cannot_split: bool = False) -> None:
        self.steps = steps
        self.cannot_split = cannot_split

    def as_dict(self) -> dict:
        return {"steps": self.steps, "cannot_split": self.cannot_split}


def parse_plan(content: str) -> Plan:
    text = content.strip()
    if "CANNOT_SPLIT" in text:
        return Plan([], cannot_split=True)
    payload = _json_block(text)
    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list) or not 2 <= len(steps) <= 5:
        raise Invalid("bad-plan",
                      "a plan needs 2 to 5 steps, or exactly CANNOT_SPLIT")
    cleaned = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise Invalid("bad-plan", f"step {index} is not an object")
        cleaned.append({
            "n": int(step.get("n", index)),
            "does": str(step.get("does", "")),
            "tests": [str(t) for t in step.get("tests", []) or []],
        })
    return Plan(cleaned)


def wants_planner(card: Card) -> bool:
    """Planner on only when the done text names two or more independent groups."""
    if len(card.tests) >= 4 and len({t.file for t in card.tests if t.file}) >= 2:
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

def build_planner_brief(card, book) -> Brief:
    return planner_brief(card, book)


def build_test_writer_brief(card, book, pack, conventions="", attempt=1,
                            previous_failure="") -> Brief:
    return test_writer_brief(card, book, pack, conventions, attempt,
                            previous_failure)


def build_implementer_brief(card, book, frozen_tests, attempt=1, diff="",
                            failure="", counter_tests="", step=None) -> Brief:
    return implementer_brief(card, book, frozen_tests, attempt, diff, failure,
                             counter_tests, step)


def build_checker_pass1_brief(card, test_diff) -> Brief:
    return checker_pass1_brief(card, test_diff)


def build_checker_pass2_brief(card, full_diff, gate_results,
                              modified_files) -> Brief:
    return checker_pass2_brief(card, full_diff, gate_results, modified_files)
