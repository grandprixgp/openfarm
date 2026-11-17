"""The eligibility gate, and the checks that share its rules.

Eligibility runs on the card **as read at claim**, before any model call, and its
answer is final for that run: a rejected card is released, not blocked, because the
card was wrong and not the farm.

Order matters. The seam-prefix check runs first because it is cheap and certain;
globs are the backstop.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ..cards import Card
from ..globs import escapes, first_match, matches_any, normalise

#: Categories the diet allows at all.
KNOWN_CATEGORIES = (
    "test-backfill",
    "pattern",
    "pinned-bugfix",
    "mechanical",
    "small-feature",
    "feature",
    "refactor",
    "integration",
    "port",
)

#: Done text that promises rather than states, and so can't be tested.
VAGUE_WORDS = ("handle", "properly", "robust")

#: Ways of writing a done text that leaves design work to the model.
DESIGN_WORDS = ("decide how", "design", "choose an approach", "figure out how")

_SOURCE_CONTAINERS = ("src", "tests", "benches", "examples")
#: A folder holding one of these is a unit: a crate, a Python project, a package.
_UNIT_MANIFESTS = ("Cargo.toml", "package.json", "tsconfig.json", "pyproject.toml", "setup.py")


class EligibilityResult:
    """Every rule that failed, each named, plus whether the card may run."""

    def __init__(self) -> None:
        self.failures: list[dict] = []
        #: What the gate found out besides its rules, such as the TypeScript
        #: runner and tools; kept out of the dict when empty.
        self.info: dict = {}

    def fail(self, rule: str, detail: str) -> None:
        self.failures.append({"rule": rule, "detail": detail})

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        out = {"eligible": self.ok, "failures": list(self.failures)}
        if self.info:
            out.update(self.info)
        return out


def _check_path_field(entry: str, field_label: str, cfg, root: Path | None,
                      result: EligibilityResult,
                      never_rule: str = "never-touch",
                      forbidden_rule: str = "forbidden-file") -> None:
    if not entry:
        return
    if escapes(entry):
        result.fail("path-escape",
                    f"{field_label} {entry!r} climbs above the repository root")
        return

    norm = normalise(entry)
    pattern = first_match(norm, cfg.never_touch)
    if pattern:
        result.fail(never_rule,
                    f"{field_label} {entry!r} matches the never-touch glob "
                    f"{pattern!r}")
    if matches_any(norm, cfg.forbidden_files):
        result.fail(forbidden_rule,
                    f"{field_label} {entry!r} is a forbidden path")

    if root is not None:
        target = (root / norm).resolve()
        if not target.is_relative_to(root):
            result.fail("path-escape",
                        f"{field_label} {entry!r} resolves outside the worktree")
            return
        resolved_rel = normalise(target.relative_to(root).as_posix())
        if resolved_rel != norm:
            res_pattern = first_match(resolved_rel, cfg.never_touch)
            if res_pattern:
                result.fail(never_rule,
                            f"{field_label} {entry!r} resolves to {resolved_rel!r}, "
                            f"which matches never-touch glob {res_pattern!r}")
            if matches_any(resolved_rel, cfg.forbidden_files):
                result.fail(forbidden_rule,
                            f"{field_label} {entry!r} resolves to {resolved_rel!r}, "
                            f"which is a forbidden path")


def check_eligibility(card: Card, cfg, deps_done: bool = True,
                      card_count: int | None = None,
                      known_failing: list[str] | None = None,
                      worktree=None) -> EligibilityResult:
    """Every rule, in the order that catches the cheapest thing first."""
    result = EligibilityResult()
    known = list(cfg.known_failing if known_failing is None else known_failing)
    root = Path(worktree).resolve() if worktree is not None else None

    # 1. Seam prefixes in touches. Cheap and certain, and normalised first so
    # `./seam:gestures` cannot slip past a plain startswith.
    for entry in card.touches:
        candidate = normalise(entry)
        for prefix in cfg.refuse_touches_prefixes:
            if candidate.startswith(prefix) or entry.startswith(prefix):
                result.fail("seam-prefix",
                            f"touches entry {entry!r} starts with the refused "
                            f"prefix {prefix!r}")

    # 2. Category & frontier estimate validity.
    if not card.category:
        result.fail("category", "the card declares no category")
    elif card.category not in KNOWN_CATEGORIES:
        result.fail("category",
                    f"{card.category!r} is not a category openfarm knows")
    elif card.category not in cfg.categories:
        result.fail("category",
                    f"{card.category!r} is not enabled in this repository's config")

    if not getattr(card, "frontier_estimate_valid", True):
        result.fail("frontier-estimate",
                    "the card's frontier_estimate is invalid")

    # `replan_if` is only read with the swarm on. With it off the field is an
    # unknown one, as it was in v0.7, and never makes a card ineligible.
    swarm_on = bool(getattr(getattr(cfg, "swarm", None), "enabled", False))
    raw_replan_if = card.fields.get("replan_if") if swarm_on else None
    if raw_replan_if is not None and (
        not isinstance(raw_replan_if, list)
        or any(not isinstance(item, str) for item in raw_replan_if)
    ):
        result.fail("replan-if", "the card's replan_if must be a list of strings")

    # 3. Shape: one unit, a handful of files, at least one touch.
    if not card.touches:
        result.fail("no-touches", "the card names no paths in touches")
    if card.touches and len(card.touches) > cfg.max_files:
        result.fail("max-files",
                    f"{len(card.touches)} touches entries, over the limit of "
                    f"{cfg.max_files}")
    units = _units_of(card.touches, worktree)
    if len(units) > cfg.max_units:
        result.fail("max-units",
                    f"touches spans {len(units)} units ({', '.join(sorted(units))}), "
                    f"over the limit of {cfg.max_units}")
    if card.unit and worktree is not None and Path(worktree).is_dir():
        derived = derive_cargo_unit(card.touches, Path(worktree))
        if derived and card.unit != derived:
            result.fail(
                "unit-mismatch",
                f"card declares unit {card.unit!r}, but Cargo.toml at that unit "
                f"names package {derived!r}")

    # 4. Path safety (escapes, symlinks, never-touch, forbidden) on all path fields.
    for entry in card.touches:
        _check_path_field(entry, "touches entry", cfg, root, result)

    # 5. The reference example, checked against never-touch/forbidden/escape too.
    if card.reference:
        _check_path_field(card.reference, "the reference", cfg, root, result,
                          never_rule="reference-never-touch",
                          forbidden_rule="reference-forbidden")
    elif card.category in cfg.require_reference:
        result.fail("reference-required",
                    f"a {card.category} card needs a reference example")

    for test in card.tests:
        if test.file:
            _check_path_field(test.file, f"test {test.name!r} file", cfg, root, result)

    for entry in card.test_edits:
        _check_path_field(entry, "test_edits entry", cfg, root, result)

    context_files = getattr(card, "context_files", None)
    if context_files is None:
        context_files = card.fields.get("context_files", [])
    context_files = [str(p) for p in (context_files or [])]
    max_context = getattr(cfg, "max_context_files", 8)
    if len(context_files) > max_context:
        result.fail("max-context-files",
                    f"{len(context_files)} context_files entries, over the limit of "
                    f"{max_context}")
    for entry in context_files:
        _check_path_field(entry, "context_files entry", cfg, root, result)

    type_changes = bool(getattr(card, "type_changes",
                                card.fields.get("type_changes", False)))
    if type_changes and not getattr(cfg, "allow_type_changes", True):
        result.fail("type-changes",
                    "type_changes is not enabled in this repository's config")

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

    # 9. Pilot & v0.4 rules: no new symbols unless declared, and Rust tests in files.
    if card.new_symbols and card.category in ("pinned-bugfix", "test-backfill"):
        result.fail("new-symbols",
                    "a pinned-bugfix or test-backfill card may not declare new "
                    "symbols: only pattern, mechanical, small-feature, feature, "
                    "refactor, integration and port may")
    if cfg.rust_tests_in_separate_files and "rust" in cfg.languages:
        for test in card.tests:
            if test.file and not _is_integration_test_file(test.file):
                result.fail(
                    "rust-tests-in-separate-files",
                    f"test {test.name!r} is declared in {test.file!r}; pilot tests "
                    f"go in a crate's tests/ folder")
    if getattr(cfg, "python_tests_in_test_files", True) and "python" in cfg.languages:
        for test in card.tests:
            if test.file and not _is_python_test_file(test.file):
                result.fail(
                    "python-tests-in-test-files",
                    f"test {test.name!r} is declared in {test.file!r}; Python card "
                    f"tests go in tests/test_<subject>.py")
    if _uses_typescript(cfg):
        _check_typescript(card, cfg, root, result)
        from ..packs.typescript import test_file_problem

        for test in card.tests:
            problem = test_file_problem(normalise(test.file)) if test.file else ""
            if problem:
                result.fail(
                    "typescript-tests-in-test-files",
                    f"test {test.name!r} is declared in {problem}")
    allow_multi_new_symbols = type_changes or (
        getattr(cfg, "micromanager_adapter", "off") != "off"
        and getattr(getattr(cfg, "micromanager", None), "scaffold_contracts", False)
    )
    if card.new_symbols and len(card.touches) > 1 and not allow_multi_new_symbols:
        result.fail(
            "new-symbols-touches",
            "a card that declares new symbols must touch exactly one file, because "
            "a stub is only safe where the test writer can see the file")

    return result


def _uses_typescript(cfg) -> bool:
    return any(lang in cfg.languages for lang in ("typescript", "ts", "javascript", "js"))


def _check_typescript(card: Card, cfg, root: Path | None,
                      result: EligibilityResult) -> None:
    """The runner and tools the TypeScript pack will judge this card with.

    The same decision the pack makes from the base checkout, so `check` refuses
    what a run would only find out after spending its calls.
    """
    if root is None or not root.is_dir():
        return
    from ..packs.typescript import resolve_tooling, settings_from_config

    tooling = resolve_tooling(root, settings_from_config(cfg))
    result.info["typescript"] = {key: tooling[key] for key in
                                 ("runner", "typecheck", "syntax_only", "tsc", "format",
                                  "prettier", "notes")}
    if tooling["test_script"]:
        result.fail("typescript-test-runner", tooling["test_script"])
    for problem in tooling["problems"]:
        result.fail("typescript-tools", problem)
    if tooling["syntax_only"]:
        jsx = [path for path in [*card.touches, *(t.file for t in card.tests if t.file)]
               if normalise(path).endswith((".tsx", ".jsx"))]
        if jsx:
            result.fail("typescript-jsx-unchecked",
                        f"{jsx[0]} is a .tsx/.jsx file and there is no tsc to check it: "
                        "node cannot parse JSX, so a card there could reach review "
                        "with code that does not parse. Set up tsc and a tsconfig.json")


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


def _units_of(touches: list[str], worktree=None) -> set[str]:
    """The crate, package or view folder each touch belongs to.

    A repository can be flat (one crate at the root) or a workspace (many crates
    under a folder). The unit is the crate: the directory holding `Cargo.toml` (or a
    Python project's `pyproject.toml`/`setup.py`, or `package.json`), or
    the segment before `src`/`tests` when the tree is not readable. Counting the
    first path segment would make every workspace crate one unit and let a card span
    three crates at `max_units = 1`.
    """
    units: set[str] = set()
    base = Path(worktree) if worktree is not None else None
    has_tree = base is not None and base.is_dir()

    for entry in touches:
        path = normalise(entry)
        parts = [part for part in path.split("/") if part and part != "**"]
        if not parts:
            units.add(".")
            continue

        raw_dir = parts[:-1]
        if has_tree and base is not None:
            found = None
            for depth in range(len(raw_dir), -1, -1):
                candidate = base / "/".join(raw_dir[:depth]) if depth > 0 else base
                if any((candidate / manifest).exists()
                       for manifest in _UNIT_MANIFESTS):
                    found = "/".join(raw_dir[:depth]) if depth > 0 else "."
                    break
            if found is not None:
                units.add(found)
                continue

        directory = list(raw_dir)
        for idx, seg in enumerate(directory):
            if seg in _SOURCE_CONTAINERS:
                directory = directory[:idx]
                break
        if not directory:
            units.add(".")
            continue
        units.add("/".join(directory))
    return units or {"."}


def derive_cargo_unit(touches: list[str], worktree: Path | None) -> str:
    """Read `[package].name` from the crate's Cargo.toml for single-unit touches."""
    if worktree is None or not touches:
        return ""
    base = Path(worktree)
    if not base.is_dir():
        return ""
    units = _units_of(touches, base)
    if len(units) != 1:
        return ""
    unit_dir = next(iter(units))
    manifest = (base / "Cargo.toml") if unit_dir == "." else (base / unit_dir / "Cargo.toml")
    if not manifest.is_file():
        return ""
    try:
        data = tomllib.loads(manifest.read_text(errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    package = data.get("package")
    if not isinstance(package, dict):
        return ""
    return str(package.get("name") or "").strip()


def _is_integration_test_file(path: str) -> bool:
    text = normalise(path)
    return text.startswith("tests/") or "/tests/" in text


def _is_python_test_file(path: str) -> bool:
    """`tests/**/test_*.py`: the files unittest discovery and pytest both find."""
    text = normalise(path)
    name = text.rsplit("/", 1)[-1]
    return _is_integration_test_file(text) and name.startswith("test_") \
        and name.endswith(".py")


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
