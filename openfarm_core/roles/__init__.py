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
from ..edits import Edit, parse_edits, parse_handback
from ..errors import Invalid, Refused
from ..globs import matches_any, normalise


class RoleCall:
    """One model call, and what it returned."""

    def __init__(self, role: str, brief: Brief, content: str,
                 model: str = "", temperature: float = 0.0,
                 seed: int | None = None, tokens_in: int = 0,
                 tokens_out: int = 0, spend: float = 0.0,
                 finish_reason: str = "stop",
                 effort: str = "", effort_param: str = "",
                 tokens_in_cached: int = 0) -> None:
        self.role = role
        self.brief = brief
        self.content = content
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.tokens_in = tokens_in
        self.tokens_in_cached = max(0, int(tokens_in_cached or 0))
        self.tokens_out = tokens_out
        self.spend = spend
        self.finish_reason = finish_reason
        self.effort = effort
        self.effort_param = effort_param

    @property
    def tokens_in_fresh(self) -> int:
        return max(0, self.tokens_in - self.tokens_in_cached)

    @property
    def cut_off(self) -> bool:
        """The answer stopped at the token limit, so its last block is unfinished."""
        return self.finish_reason == "length"

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "model": self.model,
                "temperature": self.temperature, "seed": self.seed,
                "tokens_in": self.tokens_in,
                "tokens_in_fresh": self.tokens_in_fresh,
                "tokens_in_cached": self.tokens_in_cached,
                "tokens_out": self.tokens_out,
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
                try:
                    eff, eff_param = self.cfg.effort_for_role(role, attempt)
                except TypeError:  # a stand-in config without attempts
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
                          effort=eff, effort_param=eff_param,
                          tokens_in_cached=int(getattr(completion, "tokens_in_cached", 0) or 0))
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


def _plan_test_name(raw: str, card: Card | None) -> str:
    """A step's test as the card names it.

    The planner's brief lists each test as "`name` in file", and a planner may copy
    that form back. It is read as the bare name only when both the name and the
    file are the card's own; anything else is left as written, to be refused.
    """
    text = raw.strip().strip("`").strip()
    if card is None:
        return text
    if text in set(card.test_names):
        return text
    for test in card.tests:
        if not test.file:
            continue
        for form in (f"{test.name} in {test.file}", f"`{test.name}` in {test.file}",
                     f"{test.name} in `{test.file}`", f"`{test.name}` in `{test.file}`"):
            if raw.strip() == form or text == form:
                return test.name
    # The whole brief line copied back: "`name` in file — what it asserts" (pilot
    # card M3, 18 Nov 2026, on the plan fix above). Read as the name only when it
    # leads with one of the card's tests, names no file but that test's own, and
    # what follows is that line's own description.
    match = _LEADING_TEST.match(raw.strip())
    if match:
        name, rest = match.group("name"), match.group("rest").strip()
        for test in card.tests:
            if test.name != name:
                continue
            if rest.startswith("in "):
                after = rest[3:].lstrip()
                for spelled in (f"`{test.file}`", test.file):
                    if test.file and after.startswith(spelled):
                        rest = after[len(spelled):].strip()
                        break
                else:
                    break
            if not rest or rest[0] in "—–-:(":
                return test.name
    return text


_LEADING_TEST = re.compile(r"^`?(?P<name>[A-Za-z_][\w:]*)`?(?P<rest>(?:\s.*)?)$", re.S)


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
        tests = [_plan_test_name(str(t), card) for t in step.get("tests", []) or []]
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
    restatement that drifts from the tests is flagged in the dossier. `content`
    is the first pass's answer (`parse_pass1` reads the form and calls this with
    the restatement alone), or old text with a `RESTATEMENT:` marker.
    """
    text = content.strip()
    marker = re.search(r"RESTATEMENT\s*:\s*(.+)", content,
                       flags=re.IGNORECASE | re.DOTALL)
    if marker:
        text = marker.group(1).strip()
    return _compare_restatement(text, card)


def _compare_restatement(text: str, card: Card) -> Restatement:
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


# -- the first pass's answer ---------------------------------------------------
#
# As with the second pass, the brief sends `pass1_answer_form()` and
# `parse_pass1` reads that form. Older answers (a `RESTATEMENT:` marker anywhere,
# or bare text that is only a restatement) are still read as before.

#: Whether the tests would catch a bad implementation, as written in the answer.
CATCHES_YES = "yes"
CATCHES_PARTLY = "partly"
CATCHES_NO = "no"
PASS1_CATCHES = (CATCHES_YES, CATCHES_PARTLY, CATCHES_NO)
#: The section labels, in the order the answer gives them.
PASS1_SECTIONS = ("RESTATEMENT", "CATCHES", "MISSES")


def pass1_answer_form() -> str:
    """The exact form the checker's first pass answers in. `parse_pass1` reads it."""
    catches = " or ".join(f"`{v}`" for v in PASS1_CATCHES)
    return f"""How to answer. openfarm reads only this form. Give the three sections in
this order, each starting with its label at the start of a line. No code fences,
no other headings.

RESTATEMENT:
<the done text in your own words: what the code must do once the card is done.
Restate it from the card, not from the tests>

CATCHES: {CATCHES_YES}
  or
CATCHES: {CATCHES_PARTLY}
  or
CATCHES: {CATCHES_NO}

  One of {catches}, alone on the line: would these tests fail on a bad
  implementation of the done text? `{CATCHES_YES}` when every line of the done
  text is pinned by an assertion, `{CATCHES_NO}` when a bad implementation could
  pass them easily.

MISSES:
<what a bad implementation could do and still pass these tests, one per line,
naming the line of the done text it breaks; or `nothing`>

What you say about the tests is shown to the reviewer. It does not pass or block
the card."""


class Pass1Answer:
    """What the checker's first pass said, read from its answer.

    `readable` is False when no restatement could be told apart from the rest
    (an empty or cut-off answer, or a critique of the tests with no
    `RESTATEMENT:` section); `problem` says why. An unreadable answer is never
    taken as the restatement: the flow asks once more, then records it so.
    """

    def __init__(self, *, restatement: Restatement | None = None,
                 catches: str = "", misses: str = "", readable: bool = False,
                 problem: str = "", form: str = "", raw: str = "") -> None:
        self.restatement = restatement or Restatement("", mismatch=False, overlap=0.0)
        self.catches = catches
        self.misses = misses
        self.readable = readable
        self.problem = problem
        self.form = form
        self.raw = raw

    def as_dict(self) -> dict:
        return {"readable": self.readable, "problem": self.problem,
                "form": self.form, "catches": self.catches,
                "misses": self.misses[:4000]}


_PASS1_LABEL_RE = re.compile(
    r"^[ \t>#*_]*(?P<label>restatement(?:\s+of\s+(?:the\s+)?done[ -]text)?|"
    r"restated\s+done[ -]text|done[ -]text\s+restated|catch(?:es)?|miss(?:es)?)"
    r"(?:\s+in\s+my\s+own\s+words)?[ \t*_]*(?::|\s-|[ \t]*$)[ \t*_]*(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE)
#: Words that mark an answer as being about the tests rather than the done text.
_CRITIQUE_RE = re.compile(
    r"bad\s+implementations?|\bwould\s+(?:not\s+|n't\s+)?(?:catch|miss|detect)|"
    r"\b(?:these|the|this)\s+tests?\s+(?:would|will|are|is|only|cover|do|does|"
    r"catch|miss)|\bnot\s+(?:sufficient|enough)\b|```",
    re.IGNORECASE)


def _catches_of(text: str) -> str:
    """`yes`, `partly`, `no`, or "" for anything else, from a line's first words."""
    words = re.sub(r"[^a-z]+", " ", str(text or "").lower()).split()
    head = " ".join(words[:4])
    if head.startswith(("partly", "partially", "only partially", "partial",
                        "some", "somewhat", "in part")):
        return CATCHES_PARTLY
    if head.startswith(("no", "not", "none", "insufficient", "weak", "too weak")):
        return CATCHES_NO
    if head.startswith(("yes", "fully", "they would", "it would", "sufficient")):
        return CATCHES_YES
    return ""


def parse_pass1(content: str, card: Card, cut_off: bool = False) -> Pass1Answer:
    """Read the first pass's answer: the form, or an old answer.

    The restatement alone goes to the mismatch rule; what the answer says the
    tests miss is kept beside it, as an opinion that blocks nothing.
    """
    raw = str(content or "")
    text = _clean(raw)
    if cut_off:
        return Pass1Answer(problem="the answer was cut off before it ended",
                           raw=raw)
    if not text:
        return Pass1Answer(problem="the answer is empty", raw=raw)
    labels = list(_PASS1_LABEL_RE.finditer(text))
    sections: dict[str, str] = {}
    first_at: dict[str, int] = {}
    for index, match in enumerate(labels):
        label = match.group("label").lower()
        key = ("catches" if label.startswith("catch")
               else "misses" if label.startswith("miss") else "restatement")
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        body = (match.group("rest") + "\n" + text[match.end():end]).strip()
        if key not in sections:
            sections[key] = body
            first_at[key] = match.start()
    restatement_text = sections.get("restatement", "").strip()
    if restatement_text:
        catches_text = sections.get("catches", "")
        catches = _catches_of(catches_text.split("\n", 1)[0])
        misses = sections.get("misses", "").strip()
        if "\n" in catches_text and not misses:
            misses = catches_text.split("\n", 1)[1].strip()
        form = "form" if ("catches" in sections or "misses" in sections) else "marker"
        if form == "marker":
            # An old answer: what it says about the tests is the prose before
            # its restatement.
            before = text[:first_at["restatement"]].strip()
            misses = misses or before
            catches = catches or _catches_of(
                re.sub(r"^\W*(?:verdict|assessment)\W*", "", before,
                       flags=re.IGNORECASE))
        return Pass1Answer(restatement=_compare_restatement(restatement_text, card),
                           catches=catches, misses=misses, readable=True,
                           form=form, raw=raw)
    marker = re.search(r"RESTATEMENT\s*:\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)
    if marker:
        # The old marker anywhere in a line, read as it always was.
        return Pass1Answer(restatement=parse_restatement(text, card), readable=True,
                           form="marker", raw=raw)
    if _CRITIQUE_RE.search(text):
        return Pass1Answer(
            problem=("it has no `RESTATEMENT:` section, and it talks about the tests, "
                     "so no restatement of the done text can be told apart from it"),
            raw=raw)
    # Bare text that is only a restatement: the oldest answer form.
    return Pass1Answer(restatement=_compare_restatement(text, card), readable=True,
                       form="bare", raw=raw)


class CounterTests:
    """The checker's counter-tests: report-only in the first pilot batch."""

    def __init__(self, tests: list[dict], raw: str = "") -> None:
        self.tests = tests
        self.raw = raw

    def as_dict(self) -> dict:
        return {"tests": self.tests}


# -- the second pass's answer --------------------------------------------------
#
# The brief and the parser share everything below: the brief sends
# `pass2_answer_form(...)` and `parse_pass2` reads exactly that form, so the two
# cannot drift. Older answers (a JSON object with a `counter_tests` list, or
# `COUNTER_TEST: name` blocks) are still read.

#: The spec's limit on counter-tests per second pass.
MAX_COUNTER_TESTS = 2
#: The verdicts the second pass may state, as written in its answer.
VERDICT_ACCEPT = "accept"
VERDICT_CHANGES = "changes-requested"
PASS2_VERDICTS = (VERDICT_ACCEPT, VERDICT_CHANGES)
#: The section labels, in the order the answer gives them.
PASS2_SECTIONS = ("VERDICT", "REASONS", "TRACEABILITY", "EDGE CASES")
#: A counter-test block starts with this header and the test's name.
COUNTER_TEST_HEADER = "=== counter_test:"

_MIN_EDGE_CASES = 2

_RUNNERS = {
    ("python", "unittest"): (
        "Python's stdlib `unittest` (`python -m unittest -v <module>`; this project "
        "does not use pytest). The file needs its own imports and a `unittest.TestCase` class; "
        "the counter-test is a method of that class named exactly as in its header."),
    ("python", "pytest"): (
        "pytest (`python -m pytest -v <file>`). The file needs its own imports; the "
        "counter-test is a test function named exactly as in its header."),
    ("rust", ""): (
        "`cargo test --test <file name without .rs>`, as an integration test of the "
        "crate: `use {crate}::...;` for what it needs (the crate's public API only) "
        "and one `#[test] fn` named exactly as in its header."),
    ("typescript", "node"): (
        "Node's built-in `node:test` (`import test from \"node:test\"; import assert "
        "from \"node:assert/strict\";`, relative imports with explicit `.ts` "
        "extensions; no vitest or jest). The counter-test is the `test(...)` call "
        "whose title is exactly the name in its header."),
}


def _runner_line(language: str, runner: str, crate: str) -> str:
    text = (_RUNNERS.get((language, runner))
            or _RUNNERS.get((language, ""))
            or ("this project's own test runner. The file needs its own imports, and "
                "the counter-test is named exactly as in its header."))
    return text.replace("{crate}", crate or "<the crate's library name>")


def pass2_answer_form(language: str = "", runner: str = "",
                      paths: list[str] | None = None, crate: str = "") -> str:
    """The exact form the checker's second pass answers in. `parse_pass2` reads it."""
    saved = list(paths or [])
    where = "\n".join(f"  counter-test {index}: {path}"
                      for index, path in enumerate(saved, start=1))
    verdicts = " or ".join(f"`{v}`" for v in PASS2_VERDICTS)
    return f"""How to answer. openfarm reads only this form. Give the four sections in
this order, each starting with its label at the start of a line, then the
counter-test blocks. No code fences, no other headings.

VERDICT: {VERDICT_ACCEPT}
  or
VERDICT: {VERDICT_CHANGES}

  One of {verdicts}, alone on the line.
  `{VERDICT_ACCEPT}` when the diff meets every line of the done text,
  `{VERDICT_CHANGES}` when it breaks or misses one. The verdict is shown to the
  reviewer; it does not pass or block the card. Only a counter-test that fails
  on this diff can do that.

REASONS:
<a few lines: which line of the done text the diff breaks or misses, and how; or
why it meets them all>

TRACEABILITY:
<a table, one row per line of the done text: the requirement, where the diff meets
it, and whether it holds>

EDGE CASES:
<at least {_MIN_EDGE_CASES} edge cases, each traced step by step through the
changed code to the value it gives>

Then up to {MAX_COUNTER_TESTS} counter-tests: tests of a line of the done text that
you expect to FAIL on this diff. Write them only for a line you think the diff
breaks; when your verdict is `{VERDICT_ACCEPT}`, write none. Each counter-test is
one block:

{COUNTER_TEST_HEADER} <test name>
<a complete test file, every line of it>

A block runs from its header to the next `===` header or the end of your answer.
openfarm saves each counter-test as a new file and runs it alone, unchanged:
{where or "  (in this project's tests folder)"}
The runner is {_runner_line(language, runner, crate)}
A counter-test that does not compile, or whose name the runner does not report,
is dropped."""


class Pass2Answer:
    """What the checker's second pass said, read from its answer.

    `readable` is False when no verdict could be found, or counter-tests were
    written outside the form, or the answer was cut off; `problem` says which.
    An unreadable answer is never "no counter-tests": the flow asks once more,
    then records that nothing was checked.
    """

    def __init__(self, *, verdict: str = "", reasons: str = "",
                 traceability: str = "", edge_cases: str = "",
                 counter_tests: list[dict] | None = None,
                 dropped: list[dict] | None = None,
                 readable: bool = False, problem: str = "",
                 form: str = "", raw: str = "") -> None:
        self.verdict = verdict
        self.reasons = reasons
        self.traceability = traceability
        self.edge_cases = edge_cases
        self.counter_tests = list(counter_tests or [])
        self.dropped = list(dropped or [])
        self.readable = readable
        self.problem = problem
        self.form = form
        self.raw = raw

    @property
    def missing(self) -> list[str]:
        """The sections a readable answer left out."""
        out = []
        if self.readable and self.form in ("blocks", "json"):
            if not self.reasons:
                out.append("reasons")
            if not self.traceability:
                out.append("traceability")
            if not self.edge_cases:
                out.append("edge cases")
        return out

    def as_dict(self) -> dict:
        return {"readable": self.readable, "problem": self.problem,
                "form": self.form, "verdict": self.verdict,
                "reasons": self.reasons, "traceability": self.traceability,
                "edge_cases": self.edge_cases,
                "counter_tests": [{"name": t["name"], "file": t.get("file", "")}
                                  for t in self.counter_tests],
                "dropped": list(self.dropped), "missing": self.missing}


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")
_LABEL_RE = re.compile(
    r"^[ \t>#*_]*(?P<label>verdict|reasons?|traceability(?:\s+table)?|"
    r"(?:traced\s+)?edge[ _-]?cases?)[ \t*_]*(?::|\s-)[ \t*_]*(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE)
_COUNTER_HEADER_RE = re.compile(
    r"^[ \t]*===+[ \t]*counter[ _-]?tests?[ \t]*:[ \t]*(?P<name>.*?)[ \t]*=*[ \t]*$",
    re.IGNORECASE | re.MULTILINE)
#: The next header of any kind: `=== word:`. A code line that only starts with
#: `===` (a wrapped comparison in TypeScript) does not end a block.
_ANY_HEADER_RE = re.compile(r"^[ \t]*===+[ \t]*[A-Za-z][A-Za-z0-9 _-]*:", re.MULTILINE)
_LEGACY_BLOCK_RE = re.compile(
    r"COUNTER_TEST\s*:\s*(?P<name>\S+)\s*\n(?P<content>.*?)(?=\nCOUNTER_TEST|\Z)",
    re.DOTALL)
#: A test definition inside a code fence, after a line that talks about
#: counter-tests: counter-tests written outside the form.
_FENCED_COUNTER_RE = re.compile(
    r"counter[ _-]?tests?\b.*?```[^\n]*\n(?:(?!```).)*?"
    r"(?:\bdef\s+test|#\[test\]|\bfn\s+\w+\s*\(|\btest\s*\(|\bit\s*\()"
    r"(?:(?!```).)*?```",
    re.DOTALL | re.IGNORECASE)


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", str(text or ""))
    return _CONTROL_RE.sub("", text.replace("\r\n", "\n")).strip()


def _verdict_of(text: str) -> str:
    """`accept`, `changes-requested`, or "" for anything else."""
    words = re.sub(r"[^a-z]+", " ", text.lower()).split()
    head = " ".join(words[:3])
    for phrase in ("changes requested", "change requested", "request changes",
                   "requests changes", "requesting changes", "changes required",
                   "needs changes", "reject", "rejected"):
        if head.startswith(phrase):
            return VERDICT_CHANGES
    if head.startswith(("accept", "accepted", "accepts", "approve", "approved")):
        return VERDICT_ACCEPT
    return ""


def _unfence(text: str) -> str:
    """A block's text without a code fence wrapped around all of it."""
    lines = text.strip("\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2 and lines[0].strip().startswith("```") \
            and lines[-1].strip() == "```":
        lines = lines[1:-1]
    elif lines and lines[-1].strip() == "```" and not any(
            line.strip().startswith("```") for line in lines[:-1]):
        lines = lines[:-1]
    return "\n".join(lines).rstrip() + "\n" if lines else ""


#: After a test's name in a header: where it lives or what it does, not its name.
_PATH_TAIL_RE = re.compile(r"\s+(?:(?:in|at)\s+\S*[/.]\S*|\([^()]*[/.][^()]*\))\s*$")
_NAME_TAIL_RE = re.compile(
    r"\s+(?:(?:in|at)\s+\S*[/.]\S*|\(.*\)|[-–—:]\s.*|[-–—])\s*$")


def _test_name(raw: str) -> str:
    """The test's name from a header or a JSON entry.

    `name in tests/x.py`, `name (tests/x.py)` and `name - what it checks` give
    `name`. A name with spaces is kept whole: a `node:test` title is a sentence
    ("a value below the range becomes the bottom"), and the runner reports it so.
    """
    name = raw.strip().strip("`'\"*").strip()
    first, _, rest = name.partition(" ")
    if rest and re.fullmatch(r"[A-Za-z_][\w:.]*", first.strip("`'\"*")) \
            and _NAME_TAIL_RE.match(" " + rest.strip()):
        name = first
    else:
        name = _PATH_TAIL_RE.sub("", name)
    return name.strip().strip("`'\"*:").strip()


def _add_test(tests: list[dict], dropped: list[dict], name: str, content: str,
              file: str = "") -> None:
    if not name:
        dropped.append({"name": "", "why": "the counter-test has no name"})
    elif not content.strip():
        dropped.append({"name": name, "why": "the counter-test has no file content"})
    elif len(tests) >= MAX_COUNTER_TESTS:
        dropped.append({"name": name, "over_limit": True,
                        "why": f"over the limit of {MAX_COUNTER_TESTS} "
                               "counter-tests, so it was not run"})
    else:
        tests.append({"name": name, "file": file, "content": content})


def _parse_blocks(text: str) -> Pass2Answer | None:
    """The form `pass2_answer_form` describes, or None when it has no verdict."""
    first_block = _COUNTER_HEADER_RE.search(text)
    prose = text[:first_block.start()] if first_block else text
    labels = list(_LABEL_RE.finditer(prose))
    sections: dict[str, str] = {}
    for index, match in enumerate(labels):
        label = match.group("label").lower()
        key = ("verdict" if label.startswith("verdict")
               else "reasons" if label.startswith("reason")
               else "traceability" if label.startswith("trace")
               else "edge_cases")
        end = labels[index + 1].start() if index + 1 < len(labels) else len(prose)
        body = (match.group("rest") + "\n" + prose[match.end():end]).strip()
        if key not in sections:
            sections[key] = body
    verdict = _verdict_of(sections.get("verdict", "").split("\n", 1)[0])
    if not verdict:
        return None
    # Text after the verdict word on its own line belongs to the reasons.
    verdict_text = sections.get("verdict", "")
    extra = verdict_text.split("\n", 1)[1].strip() if "\n" in verdict_text else ""
    reasons = sections.get("reasons", "") or extra
    tests: list[dict] = []
    dropped: list[dict] = []
    for match in _COUNTER_HEADER_RE.finditer(text):
        following = _ANY_HEADER_RE.search(text, match.end())
        body = text[match.end():following.start() if following else len(text)]
        _add_test(tests, dropped, _test_name(match.group("name")), _unfence(body))
    return Pass2Answer(verdict=verdict, reasons=reasons,
                       traceability=sections.get("traceability", ""),
                       edge_cases=sections.get("edge_cases", ""),
                       counter_tests=tests, dropped=dropped, readable=True,
                       form="blocks")


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(_as_text(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_as_text(v)}" for k, v in value.items())
    return str(value)


def _parse_json(payload: dict, whole: bool) -> Pass2Answer | None:
    """A JSON answer: the same fields as the form, or the old `counter_tests` shape.

    An answer that is nothing but one JSON object is the old form even without a
    `counter_tests` list (`{}` was the old "no counter-tests"). A JSON object
    inside other text counts only when it has a verdict or a `counter_tests` list,
    so a snippet quoted in prose is never read as an answer.
    """
    verdict = _verdict_of(_as_text(payload.get("verdict")))
    entries = payload.get("counter_tests")
    if not verdict and not isinstance(entries, list) and not whole:
        return None
    tests: list[dict] = []
    dropped: list[dict] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            dropped.append({"name": "", "why": "a counter-test that is not an object"})
            continue
        name = _test_name(str(entry.get("name", "") or ""))
        if not name and not str(entry.get("content", "") or "").strip():
            dropped.append({"name": "", "why": "an empty counter-test"})
            continue
        _add_test(tests, dropped, name, str(entry.get("content", "") or ""),
                  file=str(entry.get("file", "") or ""))
    return Pass2Answer(
        verdict=verdict, reasons=_as_text(payload.get("reasons")),
        traceability=_as_text(payload.get("traceability")),
        edge_cases=_as_text(payload.get("edge_cases")),
        counter_tests=tests, dropped=dropped, readable=True,
        form="json" if verdict else "legacy-json")


def parse_pass2(content: str, cut_off: bool = False) -> Pass2Answer:
    """Read the checker's second pass, in the form or one of the old forms."""
    raw = str(content or "")
    text = _clean(raw)
    if cut_off:
        return Pass2Answer(readable=False, raw=raw[:4000], problem=(
            "the answer was cut off at the token limit before it finished"))
    if not text:
        return Pass2Answer(readable=False, raw=raw[:4000],
                           problem="the answer was empty")
    found = _parse_blocks(text)
    if found is None:
        payload = _json_block(text)
        if isinstance(payload, dict):
            bare = _unfence(text).strip()
            found = _parse_json(payload,
                                whole=bare.startswith("{") and bare.endswith("}"))
    if found is None and re.search(r"COUNTER_TEST\s*:", text):
        tests: list[dict] = []
        dropped: list[dict] = []
        for block in _LEGACY_BLOCK_RE.finditer(text):
            _add_test(tests, dropped, block.group("name"),
                      block.group("content").strip())
        if tests or dropped:
            found = Pass2Answer(counter_tests=tests, dropped=dropped,
                                readable=True, form="legacy-blocks")
    if found is None:
        return Pass2Answer(readable=False, raw=raw[:4000], problem=(
            "no `VERDICT:` line was found"))
    if (not found.counter_tests and not found.dropped
            and _FENCED_COUNTER_RE.search(text)):
        # The live failure: tests written as fenced snippets under a heading. They
        # cannot run as they are, and dropping them silently would read as "the
        # checker wrote nothing".
        return Pass2Answer(
            verdict=found.verdict, reasons=found.reasons,
            traceability=found.traceability, edge_cases=found.edge_cases,
            readable=False, form=found.form, raw=raw[:4000], problem=(
                "it has test code in code fences but no "
                f"`{COUNTER_TEST_HEADER} <name>` blocks"))
    found.raw = raw[:4000]
    return found


def parse_counter_tests(content: str) -> CounterTests:
    """Read up to two counter-tests out of pass 2's answer."""
    answer = parse_pass2(content)
    return CounterTests(list(answer.counter_tests), raw=answer.raw)


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
                              modified_files, answer_form: str = "",
                              retry_problem: str = "") -> Brief:
    return checker_pass2_brief(card, full_diff, gate_results, modified_files,
                               answer_form=answer_form, retry_problem=retry_problem)
