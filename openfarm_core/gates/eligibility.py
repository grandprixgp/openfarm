"""The eligibility gate, and the checks that share its rules.

Eligibility runs on the card **as read at claim**, before any model call, and its
answer is final for that run: a rejected card is released, not blocked, because the
card was wrong and not the farm.

Order matters. The seam-prefix check runs first because it is cheap and certain;
globs are the backstop.
"""

from __future__ import annotations

from pathlib import Path

from ..cards import Card
from ..globs import first_match, matches_any, normalise

#: Categories the diet allows at all.
KNOWN_CATEGORIES = ("test-backfill", "pattern", "pinned-bugfix", "mechanical",
                    "small-feature")

#: Done text that promises rather than states, and so can't be tested.
VAGUE_WORDS = ("handle", "properly", "robust")

#: Ways of writing a done text that leaves design work to the model.
DESIGN_WORDS = ("decide how", "design", "choose an approach", "figure out how")


class EligibilityResult:
    """Every rule that failed, each named, plus whether the card may run."""

    def __init__(self) -> None:
        self.failures: list[dict] = []

    def fail(self, rule: str, detail: str) -> None:
        self.failures.append({"rule": rule, "detail": detail})

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {"eligible": self.ok, "failures": list(self.failures)}


def check_eligibility(card: Card, cfg, deps_done: bool = True,
                      card_count: int | None = None,
                      known_failing: list[str] | None = None) -> EligibilityResult:
    """Every rule, in the order that catches the cheapest thing first."""
    result = EligibilityResult()
    known = list(cfg.known_failing if known_failing is None else known_failing)

    # 1. Seam prefixes in touches. Cheap and certain.
    for entry in card.touches:
        for prefix in cfg.refuse_touches_prefixes:
            if entry.startswith(prefix):
                result.fail("seam-prefix",
                            f"touches entry {entry!r} starts with the refused "
                            f"prefix {prefix!r}")

    # 2. Category.
    if not card.category:
        result.fail("category", "the card declares no category")
    elif card.category not in KNOWN_CATEGORIES:
        result.fail("category",
                    f"{card.category!r} is not a category openfarm knows")
    elif card.category not in cfg.categories:
        result.fail("category",
                    f"{card.category!r} is not enabled in this repository's config")

    # 3. Shape: one unit, a handful of files, at least one touch.
    if not card.touches:
        result.fail("no-touches", "the card names no paths in touches")
    if card.touches and len(card.touches) > cfg.max_files:
        result.fail("max-files",
                    f"{len(card.touches)} touches entries, over the limit of "
                    f"{cfg.max_files}")
    units = _units_of(card.touches)
    if len(units) > cfg.max_units:
        result.fail("max-units",
                    f"touches spans {len(units)} units ({', '.join(sorted(units))}), "
                    f"over the limit of {cfg.max_units}")

    # 4. Never-touch and forbidden paths, against touches.
    for entry in card.touches:
        pattern = first_match(entry, cfg.never_touch)
        if pattern:
            result.fail("never-touch",
                        f"touches entry {entry!r} matches the never-touch glob "
                        f"{pattern!r}")
        if matches_any(entry, cfg.forbidden_files):
            result.fail("forbidden-file",
                        f"touches entry {entry!r} is a forbidden path")

    # 5. The reference example, checked against never-touch too.
    if card.reference:
        pattern = first_match(card.reference, cfg.never_touch)
        if pattern:
            result.fail("reference-never-touch",
                        f"the reference {card.reference!r} is on the never-touch "
                        f"list ({pattern!r})")
        if matches_any(card.reference, cfg.forbidden_files):
            result.fail("reference-forbidden",
                        f"the reference {card.reference!r} is a forbidden path")
    elif card.category in cfg.require_reference:
        result.fail("reference-required",
                    f"a {card.category} card needs a reference example")

    # 6. Named tests. A mechanical card is a compiler-guided edit, and the existing
    # suite judging it is the point; every other category must name its tests.
    if card.category != "mechanical" and not card.tests:
        result.fail("named-tests",
                    "the card names no tests, so nothing can judge it")
    for name in card.test_names:
        if name in known:
            result.fail("known-failing",
                        f"the card's test {name!r} is on the known_failing list")
    seen: set[str] = set()
    duplicates = [n for n in card.test_names
                  if n in seen or seen.add(n)]  # type: ignore[func-returns-value]
    if duplicates:
        result.fail("duplicate-tests",
                    f"the card names {', '.join(sorted(set(duplicates)))} twice")

    # 7. Done text.
    _check_done_text(card, result)

    # 8. Dependencies.
    if not deps_done:
        result.fail("deps", "a dependency is not done")

    # 9. Pilot rules: no new symbols unless declared, and Rust tests in files.
    if card.new_symbols and card.category in ("pinned-bugfix", "test-backfill"):
        result.fail("new-symbols",
                    "a pinned-bugfix or test-backfill card may not declare new "
                    "symbols: only pattern, mechanical and small-feature may")
    if cfg.rust_tests_in_separate_files and "rust" in cfg.languages:
        for test in card.tests:
            if test.file and not _is_integration_test_file(test.file):
                result.fail(
                    "rust-tests-in-separate-files",
                    f"test {test.name!r} is declared in {test.file!r}; pilot tests "
                    f"go in a crate's tests/ folder")
    if card.new_symbols and len(card.touches) > 1:
        result.fail(
            "new-symbols-touches",
            "a card that declares new symbols must touch exactly one file, because "
            "a stub is only safe where the test writer can see the file")

    return result


def _check_done_text(card: Card, result: EligibilityResult) -> None:
    text = (card.done_text or "").strip()
    if not text:
        result.fail("done-text", "the card has no done text")
        return
    lowered = text.lower()
    for word in VAGUE_WORDS:
        if _word_present(lowered, word):
            result.fail(
                "vague-done",
                f"the done text says {word!r} without naming the cases")
    for phrase in DESIGN_WORDS:
        if phrase in lowered:
            result.fail("design-left",
                        f"the done text leaves design work: {phrase!r}")

    # A done text the tests can check states concrete inputs or outputs. This is a
    # shape check, not a mind-reader: it looks for a code span, a value or a list.
    if card.tests and not any(marker in card.done_text for marker in
                              ("`", "==", "->", "returns", "assert", "error",
                               "matches!", "equals")):
        has_concrete = any(t.asserts for t in card.tests)
        if not has_concrete:
            result.fail(
                "done-text-concrete",
                "the done text and the named tests state no concrete value, so "
                "nothing judges what the tests must assert")


def _word_present(text: str, word: str) -> bool:
    import re
    return bool(re.search(rf"\b{re.escape(word)}\b", text))


def _units_of(touches: list[str]) -> set[str]:
    """The crate, package or view folder each touch belongs to."""
    units = set()
    for entry in touches:
        path = normalise(entry)
        parts = [p for p in path.split("/") if p and p != "**"]
        if not parts:
            continue
        stripped = path.rstrip("/")
        if stripped.endswith("/**"):
            stripped = stripped[:-3]
        parts = [p for p in stripped.split("/") if p and p != "**"]
        if parts and parts[0].endswith(".rs"):
            units.add(".")
            continue
        if parts:
            units.add(parts[0])
    return units or {"."}


def _is_integration_test_file(path: str) -> bool:
    text = normalise(path)
    return text.startswith("tests/") or "/tests/" in text


def is_eligible(card: Card, cfg, **kwargs) -> bool:
    return check_eligibility(card, cfg, **kwargs).ok


def file_is_blocked(path: str, cfg) -> str | None:
    """Why a path may not be touched, or None. Shared by every attempt's check."""
    pattern = first_match(path, cfg.never_touch)
    if pattern:
        return f"never-touch glob {pattern!r}"
    if matches_any(path, cfg.forbidden_files):
        return "a forbidden path"
    return None


def card_hash_matches(card: Card, source_hash: str) -> bool:
    return card.hash() == source_hash
