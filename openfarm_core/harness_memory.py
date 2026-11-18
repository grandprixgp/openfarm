"""Recursive harness efficiency subsystem: contrast capture, overlay library, and ledger.

Stores learned guidance, revisions, and empirical efficiency metrics strictly outside the
repository (typically under ``<state_dir>/harness/``). All adaptations are confined to
``EDITABLE_SURFACES``; deterministic verification gates, policy lists, touches, and
budgets are immutable and cannot be modified or bypassed by any lesson or reflection.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import NamedTuple

from .errors import AdapterError, Invalid, Refused, Unsupported
from .globs import escapes, matches_any, normalise
from .harness_global import GlobalLibrary, should_promote, should_veto
from .harness_scope import global_eligibility, scope_of
from .state import _safe_id, append_jsonl, read_json, utc_now

_HARNESS_LOCKS_GUARD = threading.Lock()
_HARNESS_LOCKS: dict[Path, threading.RLock] = {}


def _harness_thread_lock(root: Path) -> threading.RLock:
    key = Path(root).resolve()
    with _HARNESS_LOCKS_GUARD:
        lock = _HARNESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _HARNESS_LOCKS[key] = lock
        return lock

EDITABLE_SURFACES: tuple[str, ...] = (
    "test_writer_guidance",
    "implementer_guidance",
    "planner_guidance",
    "context_hints",
    "micromanager_context",
)

_DEFAULT_ROLE_FOR_SURFACE: dict[str, str] = {
    "test_writer_guidance": "test_writer",
    "implementer_guidance": "implementer",
    "planner_guidance": "planner",
    "context_hints": "*",
    "micromanager_context": "micromanager",
}

_VALID_ROLES = frozenset({"planner", "test_writer", "implementer", "micromanager", "*"})

IMMUTABLE_GATE_KEYS = frozenset({
    "gates", "revert_check", "mutation", "mutation_required_for",
    "mutation_min_kill_rate", "counter_tests", "coverage",
    "workspace_lint", "baseline_cache", "catch_up", "findings_per_card",
    "circuit_breaker", "extra", "extra_gates", "eligibility", "max_files",
    "max_units", "require_reference", "refuse_touches_prefixes",
    "rust_tests_in_separate_files", "allow_type_changes", "budget",
    "implementer_attempts", "test_writer_attempts",
    "model_build_calls_per_card", "mutation_build_calls_per_card",
    "tokens_per_card", "wall_clock_per_card", "frontier_cost_ratio",
    "spend_per_night", "concurrent_builds", "concurrent_cards",
    "lease_ttl", "kill_switch", "policy", "never_touch", "forbidden_files",
    "known_failing", "flagged_patterns", "touches", "test_edits",
    "cargo_test", "clippy", "mutants", "diff_check", "scope", "thresholds",
    "runner", "cargo_flags", "min_free_mb", "protocol",
})

_ALLOWED_PATCH_SUBKEYS = frozenset(EDITABLE_SURFACES) | frozenset({
    "cue", "guidance", "hint", "hints", "context_files",
    "note", "notes", "tags", "role", "failure_class", "title",
})

ALLOWED_FAILURE_CLASSES: frozenset[str] = frozenset({
    "",
    "type-mismatch",
    "borrow-checker",
    "missing-symbol",
    "trait-bound",
    "lint-clippy",
    "format-check",
    "counter-test",
    "revert-check",
    "runtime-assert",
    "other",
})

_HOMOGLYPH_MAP = str.maketrans({
    # Cyrillic lowercase
    "\u0430": "a",  # а
    "\u0435": "e",  # е
    "\u0451": "e",  # ё
    "\u043e": "o",  # о
    "\u0440": "p",  # р
    "\u0441": "c",  # с
    "\u0445": "x",  # х
    "\u0456": "i",  # і
    "\u0458": "j",  # ј
    "\u043a": "k",  # к
    "\u043c": "m",  # м
    "\u0442": "t",  # т
    "\u0432": "b",  # в
    "\u043d": "h",  # н
    "\u0443": "y",  # у
    "\u0455": "s",  # ѕ
    "\u0501": "d",  # ԁ
    "\u0261": "g",  # ɡ
    "\u04cf": "l",  # ӏ
    # Cyrillic uppercase
    "\u0410": "A",  # А
    "\u0412": "B",  # В
    "\u0415": "E",  # Е
    "\u0401": "E",  # Ё
    "\u041a": "K",  # К
    "\u041c": "M",  # М
    "\u041d": "H",  # Н
    "\u041e": "O",  # О
    "\u0420": "P",  # Р
    "\u0421": "C",  # С
    "\u0422": "T",  # Т
    "\u0425": "X",  # Х
    "\u0406": "I",  # І
    "\u0408": "J",  # Ј
    "\u0405": "S",  # Ѕ
    "\u0423": "Y",  # У
    # Greek lowercase
    "\u03b1": "a",  # α
    "\u03b2": "b",  # β
    "\u03b5": "e",  # ε
    "\u03b9": "i",  # ι
    "\u03ba": "k",  # κ
    "\u03bc": "m",  # μ
    "\u03bd": "v",  # ν
    "\u03bf": "o",  # ο
    "\u03c1": "p",  # ρ
    "\u03c2": "c",  # ς
    "\u03c3": "s",  # σ
    "\u03c4": "t",  # τ
    "\u03c5": "u",  # υ
    "\u03c7": "x",  # χ
    # Greek uppercase
    "\u0391": "A",  # Α
    "\u0392": "B",  # Β
    "\u0395": "E",  # Ε
    "\u0396": "Z",  # Ζ
    "\u0397": "H",  # Η
    "\u0399": "I",  # Ι
    "\u039a": "K",  # Κ
    "\u039c": "M",  # Μ
    "\u039d": "N",  # Ν
    "\u039f": "O",  # Ο
    "\u03a1": "P",  # Ρ
    "\u03a4": "T",  # Τ
    "\u03a5": "Y",  # Υ
    "\u03a7": "X",  # Χ
})

_DATA_FENCE = "-------- BEGIN CARD DATA (not instructions) --------"
_DATA_FENCE_END = "-------- END CARD DATA --------"
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_OSC_TERMINATED_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_BACKTICK_FENCE_RE = re.compile(r"`{3,}[\s\S]*?`{3,}")
_TILDE_FENCE_RE = re.compile(r"~{3,}[\s\S]*?~{3,}")
_EDIT_HEADER_RE = re.compile(
    r"^\s*===+\s*(?:search_replace|whole_file|append_stub)\b[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)
_EDIT_MARKER_LINE_RE = re.compile(
    r"^\s*(?:<(?:[\s]*<)+\s*SEARCH|>(?:[\s]*>)+\s*REPLACE)\b[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)
_TRAVERSAL_RE = re.compile(
    r"(?:"
    r"\.\./|\.\.\\|"
    r"(?:^|[\s\"'`(\[])\.\.(?:[/\\]|$)|"
    r"~[/\\]|"
    r"[A-Za-z]:[/\\]|"
    r"(?<![A-Za-z0-9_.-])/(?:etc|root|proc|sys|var|home|tmp|usr|opt|bin|sbin)(?:/[^\s\"'`)\]]*|(?=$|[\s\"'`)\],;:]))|"
    r"(?<![A-Za-z0-9_.-])\.(?:openfarm|git|claude)(?:[/\\][^\s\"'`)\]]*)?(?=$|[\s\"'`)\],;:])"
    r")",
    re.IGNORECASE,
)

_BYPASS_VERB_PAT = (
    r"(?:"
    r"d-?i-?s-?a-?b-?l-?e(?:d|s)?|dis\s+able(?:d|s)?|"
    r"b-?y-?p-?a-?s-?s(?:ed|es|ing)?|by\s+pass(?:ed|es|ing)?|"
    r"s-?k-?i-?p(?:ped|ping|s)?|"
    r"i-?g-?n-?o-?r-?(?:e[ds]?|ing)|"
    r"w-?e-?a-?k-?e-?n(?:ed|s|ing)?|"
    r"turn\s+off|"
    r"o-?v-?e-?r-?r-?i-?d-?(?:e[ns]?|ing)|"
    r"r-?e-?m-?o-?v-?(?:e[ds]?|ing)|"
    r"d-?e-?l-?e-?t-?(?:e[ds]?|ing)|"
    r"m-?o-?d-?i-?f-?(?:y|ied|ies|ying)|"
    r"r-?e-?w-?r-?i-?t-?(?:e[ns]?|ing)|"
    r"e-?d-?i-?t(?:ed|s|ing)?|"
    r"n-?e-?u-?t-?r-?a-?l-?i-?z-?(?:e[ds]?|ing)|"
    r"s-?u-?p-?p-?r-?e-?s-?s(?:ed|es|ing)?"
    r")"
)
_PASSIVE_VERB_PAT = (
    r"(?:"
    r"d-?i-?s-?a-?b-?l-?e-?d|dis\s+abled|"
    r"b-?y-?p-?a-?s-?s-?e-?d|by\s+passed|"
    r"s-?k-?i-?p-?p-?e-?d|"
    r"i-?g-?n-?o-?r-?e-?d|"
    r"w-?e-?a-?k-?e-?n-?e-?d|"
    r"turned\s+off|"
    r"o-?v-?e-?r-?r-?i-?d-?d-?e-?n|"
    r"r-?e-?m-?o-?v-?e-?d|"
    r"d-?e-?l-?e-?t-?e-?d|"
    r"m-?o-?d-?i-?f-?i-?e-?d|"
    r"r-?e-?w-?r-?i-?t-?t-?e-?n|"
    r"e-?d-?i-?t-?e-?d|"
    r"n-?e-?u-?t-?r-?a-?l-?i-?z-?e-?d|"
    r"s-?u-?p-?p-?r-?e-?s-?s-?e-?d"
    r")"
)
_BYPASS_TARGET_PAT = (
    r"(?:"
    r"cargo\s+test|"
    r"(?:the\s+|all\s+|existing\s+|frozen\s+)?tests?(?:\s+files?)?|"
    r"frozen(?:\s+tests?)?|"
    r"clippy|mutants?|mutation|"
    r"diff_check|diff\s+check|revert_check|revert\s+check|revert|"
    r"counter_tests?|counter-tests?|forbidden_files|never_touch|"
    r"eligibility|budget|scope|touches|gates?|thresholds?|"
    r"\.openfarm(?:/config\.toml)?|\.claude(?:/[^\s]*)?|\.git(?:/[^\s]*)?|openfarm_core/"
    r")"
)
_BYPASS_DIRECTIVE_RE = re.compile(
    rf"(?:"
    rf"\b{_BYPASS_VERB_PAT}\b[^.\n;]{{0,60}}\b{_BYPASS_TARGET_PAT}(?:\b|(?<=/))|"
    rf"\b{_BYPASS_TARGET_PAT}(?:\b|(?<=/))[^.\n;]{{0,60}}\b(?:should|can|could|may|might|must|need\s+to|ought\s+to|will|would|is|are|was|were|be|been|being|get|gets|getting)\s+(?:be\s+|been\s+|being\s+)?(?:easily\s+|safely\s+|temporarily\s+)?{_PASSIVE_VERB_PAT}\b|"
    rf"#!?\[\s*(?:allow|ignore)\b"
    rf")",
    re.IGNORECASE,
)
_ASSERT_LEAK_RE = re.compile(
    r"(?:"
    r"assertion\s+(?:failed|`[^`]+`\s+failed)|"
    r"left\s*:[\s\S]{0,120}\bright\s*:|"
    r"\bassert_(?:eq|ne)!\s*\(|"
    r"\bexpected\b[^\n]{0,80}\bgot\b|"
    r"==\s*(?:Err|Ok)\s*\(|"
    r"\basserts?\b[^\n]{0,120}=="
    r")",
    re.IGNORECASE,
)

_MULTI_EDIT_ANGLE_RE = re.compile(r"<{2,}|>{2,}")
_FENCE_MARKER_RE = re.compile(r"`{3,}|~{3,}")
_UNIX_ABS_PREFIXES = (
    "/etc/",
    "/root/",
    "/proc/",
    "/sys/",
    "/var/",
    "/home/",
    "/tmp/",
    "/usr/",
    "/opt/",
    "/bin/",
    "/sbin/",
)
_PROTECTED_DOTDIRS = frozenset({".openfarm", ".git", ".claude"})


def _strip_unicode_noise(text: str) -> str:
    """Apply NFKC normalization and drop Unicode format/zero-width (Cf) characters."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")


def _normalize_for_scan(text: str) -> str:
    """Normalize text and fold Cyrillic/Greek homoglyphs before security scanning."""
    return _strip_unicode_noise(text).translate(_HOMOGLYPH_MAP)


def _atomic_write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def _atomic_write_json(path: Path, payload: object) -> Path:
    return _atomic_write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


def sanitize_lesson_text(text: str, *, max_len: int = 600) -> str:
    """Strip edit markers, code fences, escape codes, path traversals, and gate bypasses."""
    cleaned = _OSC_TERMINATED_RE.sub("", str(text or ""))
    cleaned = _CSI_RE.sub("", cleaned)
    cleaned = cleaned.replace("\x1b", "")
    cleaned = _strip_unicode_noise(cleaned)
    cleaned = cleaned.replace(_DATA_FENCE, "")
    cleaned = cleaned.replace(_DATA_FENCE_END, "")
    cleaned = _BACKTICK_FENCE_RE.sub("", cleaned)
    cleaned = _TILDE_FENCE_RE.sub("", cleaned)
    cleaned = _FENCE_MARKER_RE.sub("", cleaned)
    cleaned = _EDIT_HEADER_RE.sub("", cleaned)
    cleaned = _EDIT_MARKER_LINE_RE.sub("", cleaned)
    cleaned = _MULTI_EDIT_ANGLE_RE.sub("", cleaned)
    if _BYPASS_DIRECTIVE_RE.search(_normalize_for_scan(cleaned)):
        if not _BYPASS_DIRECTIVE_RE.search(cleaned):
            cleaned = _normalize_for_scan(cleaned)
        cleaned = _BYPASS_DIRECTIVE_RE.sub("[removed-directive]", cleaned)
    if _TRAVERSAL_RE.search(_normalize_for_scan(cleaned)):
        if not _TRAVERSAL_RE.search(cleaned):
            cleaned = _normalize_for_scan(cleaned)
        cleaned = _TRAVERSAL_RE.sub("[removed-path]", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def is_gate_bypass_directive(text: str) -> bool:
    """True when text instructs disabling, bypassing, or modifying deterministic gates/scope."""
    scanned = _normalize_for_scan(text)
    return bool(_BYPASS_DIRECTIVE_RE.search(scanned))


def is_assertion_leak(text: str) -> bool:
    """True when text contains exact assertion or hidden-grader comparison leaks."""
    scanned = _normalize_for_scan(text)
    return bool(_ASSERT_LEAK_RE.search(scanned))


def _is_unsafe_path(
    raw_path: str,
    *,
    never_touch: tuple[str, ...] | list[str] = (),
    forbidden_files: tuple[str, ...] | list[str] = (),
) -> bool:
    text = _normalize_for_scan(raw_path).strip()
    if not text or "\x00" in text:
        return True
    if text.startswith("/") or text.startswith("\\") or text.startswith("~"):
        return True
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return True
    text_lower = text.lower().replace("\\", "/")
    if any(text_lower.startswith(prefix) or text_lower == prefix.rstrip("/") for prefix in _UNIX_ABS_PREFIXES):
        return True
    parts = [p for p in text_lower.split("/") if p and p != "."]
    if ".." in parts or any(p in _PROTECTED_DOTDIRS for p in parts):
        return True
    if escapes(text):
        return True
    norm = normalise(text)
    if not norm:
        return True
    norm_lower = norm.lower()
    if ".." in norm_lower.split("/") or any(p in _PROTECTED_DOTDIRS for p in norm_lower.split("/")):
        return True
    fb = [str(p).lower() for p in (forbidden_files or ())]
    for default_fb in (".openfarm/**", ".git/**", ".claude/**"):
        if default_fb not in fb:
            fb.append(default_fb)
    nt = [str(p).lower() for p in (never_touch or ())]
    if matches_any(norm_lower, fb) or matches_any(norm_lower, nt):
        return True
    return False


def _sanitize_patch_value(
    value,
    *,
    key_name: str = "",
    never_touch: tuple[str, ...] | list[str] = (),
    forbidden_files: tuple[str, ...] | list[str] = (),
) -> tuple[object, str]:
    if isinstance(value, str):
        if is_gate_bypass_directive(value):
            return None, f"gate-bypass directive in {key_name or 'proposed_patch'}"
        if _TRAVERSAL_RE.search(_normalize_for_scan(value)):
            return None, f"unsafe path reference in {key_name or 'proposed_patch'}"
        if is_assertion_leak(value):
            return None, f"assertion leak rejected in {key_name or 'proposed_patch'}"
        if key_name == "failure_class":
            fc = value.strip().lower()
            if fc not in ALLOWED_FAILURE_CLASSES:
                return None, f"disallowed failure_class {value!r} in proposed_patch"
            return fc, ""
        if key_name in ("path", "file"):
            if _is_unsafe_path(value, never_touch=never_touch, forbidden_files=forbidden_files):
                return None, f"unsafe path {value!r} in {key_name}"
            return normalise(_strip_unicode_noise(value)), ""
        return sanitize_lesson_text(value), ""
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and key_name in ("context_files", "paths", "files"):
                if _is_unsafe_path(item, never_touch=never_touch, forbidden_files=forbidden_files):
                    return None, f"unsafe path {item!r} in {key_name}"
                out.append(normalise(_strip_unicode_noise(item)))
            else:
                cleaned, err = _sanitize_patch_value(
                    item,
                    key_name=key_name,
                    never_touch=never_touch,
                    forbidden_files=forbidden_files,
                )
                if err:
                    return None, err
                if cleaned is not None and cleaned != "":
                    out.append(cleaned)
        return out, ""
    if isinstance(value, dict):
        out_dict = {}
        for sub_k, sub_v in value.items():
            k_str = str(sub_k).strip()
            if k_str in IMMUTABLE_GATE_KEYS or k_str.lower() in IMMUTABLE_GATE_KEYS:
                return None, f"immutable key {k_str!r} in proposed_patch"
            cleaned, err = _sanitize_patch_value(
                sub_v,
                key_name=k_str,
                never_touch=never_touch,
                forbidden_files=forbidden_files,
            )
            if err:
                return None, err
            out_dict[k_str] = cleaned
        return out_dict, ""
    if isinstance(value, (int, float, bool)) or value is None:
        return value, ""
    text_val = str(value)
    if is_assertion_leak(text_val):
        return None, f"assertion leak rejected in {key_name or 'proposed_patch'}"
    return sanitize_lesson_text(text_val), ""


def validate_harness_proposal(
    raw: dict,
    *,
    allowed_surfaces: tuple[str, ...] | list[str] = EDITABLE_SURFACES,
    never_touch: tuple[str, ...] | list[str] = (),
    forbidden_files: tuple[str, ...] | list[str] = (),
    default_language: str = "*",
    default_kind: str = "*",
) -> tuple[dict | None, str]:
    """Validate and sanitize one candidate adaptation proposal.

    Returns ``(cleaned_dict, "")`` when accepted or ``(None, reason)`` when rejected.
    """
    if not isinstance(raw, dict):
        return None, "proposal must be a JSON object"

    surface = str(raw.get("surface", "")).strip()
    if surface not in EDITABLE_SURFACES or surface not in allowed_surfaces:
        return None, f"disallowed surface {surface!r}"

    raw_patch = raw.get("proposed_patch")
    if raw_patch is None:
        raw_patch = {}
    if not isinstance(raw_patch, dict):
        return None, "proposed_patch must be a JSON object"

    cleaned_patch: dict = {}
    for k, v in raw_patch.items():
        k_str = str(k).strip()
        if k_str in IMMUTABLE_GATE_KEYS or k_str.lower() in IMMUTABLE_GATE_KEYS:
            return None, f"proposed_patch cannot touch immutable key {k_str!r}"
        if k_str not in _ALLOWED_PATCH_SUBKEYS:
            return None, f"proposed_patch has disallowed key {k_str!r}"
        if k_str in EDITABLE_SURFACES and k_str not in allowed_surfaces:
            return None, f"proposed_patch targets disallowed surface {k_str!r}"
        cleaned_v, err = _sanitize_patch_value(
            v,
            key_name=k_str,
            never_touch=never_touch,
            forbidden_files=forbidden_files,
        )
        if err:
            return None, err
        cleaned_patch[k_str] = cleaned_v

    for field_name in ("title", "cue", "lesson", "evidence"):
        val = raw.get(field_name, "")
        if val is None:
            val = ""
        val_str = str(val)
        if is_gate_bypass_directive(val_str):
            return None, f"gate-bypass directive rejected in {field_name}"
        if _TRAVERSAL_RE.search(_normalize_for_scan(val_str)):
            return None, f"unsafe path directive rejected in {field_name}"
        if is_assertion_leak(val_str):
            return None, f"assertion leak rejected in {field_name}"

    title = sanitize_lesson_text(str(raw.get("title", "")), max_len=140)
    cue = sanitize_lesson_text(str(raw.get("cue", "")), max_len=400)
    lesson = sanitize_lesson_text(str(raw.get("lesson", "")), max_len=600)
    evidence = sanitize_lesson_text(str(raw.get("evidence", "")), max_len=400)

    if not cue:
        patch_text = cleaned_patch.get(surface) or cleaned_patch.get("cue") or cleaned_patch.get("guidance")
        if isinstance(patch_text, str) and patch_text.strip():
            cue = sanitize_lesson_text(patch_text, max_len=400)
        elif lesson:
            cue = sanitize_lesson_text(lesson, max_len=400)
    if not lesson and cue:
        lesson = cue
    if not cue:
        return None, "empty cue after sanitization"
    if not title:
        title = cue[:80]

    role = str(raw.get("role", "")).strip() or _DEFAULT_ROLE_FOR_SURFACE[surface]
    if role not in _VALID_ROLES:
        role = _DEFAULT_ROLE_FOR_SURFACE[surface]

    raw_conf = raw.get("confidence", 0.65)
    if isinstance(raw_conf, bool):
        return None, f"invalid confidence {raw_conf!r}"
    try:
        conf = float(raw_conf)
    except (TypeError, ValueError):
        return None, f"invalid confidence {raw_conf!r}"
    if math.isnan(conf) or math.isinf(conf):
        return None, f"invalid confidence {raw_conf!r}"
    conf = round(max(0.0, min(1.0, conf)), 4)

    language = sanitize_lesson_text(
        str(raw.get("language", default_language) or default_language), max_len=40
    ) or "*"
    kind = sanitize_lesson_text(
        str(raw.get("kind", default_kind) or default_kind), max_len=60
    ) or "*"
    raw_fc = raw.get("failure_class", "")
    if raw_fc is None:
        raw_fc = ""
    if not isinstance(raw_fc, str):
        return None, f"invalid failure_class {raw_fc!r}"
    stripped_fc = _strip_unicode_noise(raw_fc).strip()
    if any(ch.isspace() for ch in stripped_fc):
        return None, f"invalid failure_class {raw_fc!r}"
    failure_class = stripped_fc.lower()
    if failure_class not in ALLOWED_FAILURE_CLASSES:
        return None, f"disallowed failure_class {raw_fc!r}"

    raw_tags = raw.get("tags") or []
    tags: list[str] = []
    if isinstance(raw_tags, (list, tuple)):
        for t in raw_tags:
            cleaned_t = sanitize_lesson_text(str(t), max_len=40)
            if cleaned_t and cleaned_t not in tags:
                tags.append(cleaned_t)

    return {
        "title": title,
        "surface": surface,
        "role": role,
        "language": language,
        "kind": kind,
        "failure_class": failure_class,
        "cue": cue,
        "lesson": lesson,
        "evidence": evidence,
        "confidence": conf,
        "tags": tags,
        "proposed_patch": cleaned_patch,
        "source": str(raw.get("source", "") or "micromanager"),
        # v0.7: the proposer's claim; `harness_scope.scope_of` checks it at capture.
        "scope": scope if (scope := str(raw.get("scope", "") or "").strip().lower())
        in ("project", "general") else "project",
        "scope_reason": sanitize_lesson_text(str(raw.get("scope_reason", "") or ""),
                                             max_len=200),
    }, ""


def _classify_failure(error_signature: str = "", stop_code: str = "", failure: str = "") -> str:
    combined = " ".join(
        part for part in (str(error_signature or ""), str(stop_code or ""), str(failure or "")) if part
    ).lower()
    if not combined.strip():
        return ""
    if any(k in combined for k in ("checker-reject", "counter-test", "counter_test", "counter test")):
        return "counter-test"
    if any(k in combined for k in ("revert-check", "revert_check", "passes without")):
        return "revert-check"
    if any(k in combined for k in ("e0382", "e0502", "e0499", "borrow", "moved value", "lifetime")):
        return "borrow-checker"
    if any(k in combined for k in ("e0308", "mismatched types", "type mismatch")):
        return "type-mismatch"
    if any(k in combined for k in ("e0412", "e0425", "e0433", "cannot find", "unresolved import")):
        return "missing-symbol"
    if any(k in combined for k in ("e0277", "trait bound", "is not implemented")):
        return "trait-bound"
    if any(k in combined for k in ("clippy", "workspace_lint", "lint")):
        return "lint-clippy"
    if any(k in combined for k in ("rustfmt", "format_check", "format check")):
        return "format-check"
    if any(k in combined for k in ("no-progress", "identical error")):
        return "other"
    if any(k in combined for k in ("error[e", "could not compile", "compile")):
        return "other"
    if any(k in combined for k in ("assert", "panicked at", "test failed", "fail-before", "tests-unprovable")):
        return "runtime-assert"
    return "other"


def _scrub_test_leaks(text: str) -> str:
    scanned = _normalize_for_scan(text)
    cleaned = _ASSERT_LEAK_RE.sub("[redacted-assertion-detail]", scanned)
    return sanitize_lesson_text(cleaned, max_len=240)


def extract_contrast(
    card_meta: dict,
    attempt_records: list[dict],
    failure: str = "",
) -> dict:
    """Extract deterministic contrast evidence from a card run without test answer leaks."""
    meta = dict(card_meta or {})
    records = [dict(r) for r in (attempt_records or []) if isinstance(r, dict)]

    card_id = str(meta.get("card_id") or meta.get("id") or "")
    kind = str(meta.get("kind") or meta.get("category") or "unknown")
    language = str(meta.get("language") or meta.get("unit") or "rust")
    final_state = str(meta.get("final_state") or "")
    stop_code = str(meta.get("stop_code") or "")

    counter_raw = meta.get("counter_tests")
    counter_failed_count = 0
    if isinstance(counter_raw, dict):
        counter_failed_count = int(counter_raw.get("failed", 0) or 0)
    elif isinstance(counter_raw, list):
        counter_failed_count = len(counter_raw)

    total_attempts = max(
        int(meta.get("attempts", 0) or 0),
        len(records),
        max((int(r.get("attempt", 0) or 0) for r in records), default=0),
    )
    if total_attempts == 0 and (final_state or records):
        total_attempts = 1

    failed_attempts = [
        r for r in records if str(r.get("outcome", "")).lower() in ("fail", "failed", "error", "refused")
    ]
    passed_attempts = [
        r for r in records if str(r.get("outcome", "")).lower() in ("pass", "passed", "ok", "success")
    ]

    passed = final_state in ("in-review", "done", "passed", "pass") or (
        bool(meta.get("passed")) and not stop_code
    )
    if not final_state and not stop_code and passed_attempts and (
        not failed_attempts or int(passed_attempts[-1].get("attempt", 0) or 0) >= int(failed_attempts[-1].get("attempt", 0) or 0)
    ):
        passed = True

    attempt_1_fail_class = ""
    attempt_1_phase = ""
    if failed_attempts:
        first_fail = min(failed_attempts, key=lambda r: int(r.get("attempt", 1) or 1))
        if int(first_fail.get("attempt", 1) or 1) == 1 or len(records) > 1:
            attempt_1_phase = str(first_fail.get("phase") or "")
            attempt_1_fail_class = _classify_failure(
                first_fail.get("error_signature", ""),
                first_fail.get("stop_code", ""),
                failure if not passed else "",
            )

    had_counter_gate_fail = counter_failed_count > 0 or stop_code == "checker-reject"
    if not had_counter_gate_fail:
        for r in records:
            for g in r.get("gates") or []:
                if isinstance(g, dict) and "counter" in str(g.get("name", "")).lower() and str(g.get("result", "")).lower() in ("fail", "failed", "reject"):
                    had_counter_gate_fail = True
                    break

    if passed:
        if len(failed_attempts) > 0 or total_attempts > 1:
            outcome_kind = "retry_recovery"
        else:
            outcome_kind = "pass"
    else:
        if had_counter_gate_fail:
            outcome_kind = "counter_fail"
        elif final_state in ("blocked", "failed", "fail", "") or stop_code:
            outcome_kind = "fail"
        else:
            outcome_kind = "other"

    recovery_attempt = None
    if outcome_kind == "retry_recovery":
        if passed_attempts:
            recovery_attempt = int(passed_attempts[-1].get("attempt", total_attempts) or total_attempts)
        else:
            recovery_attempt = max(2, total_attempts)

    final_fail_class = ""
    if not passed:
        last_sig = str(failed_attempts[-1].get("error_signature", "")) if failed_attempts else ""
        last_sc = str(failed_attempts[-1].get("stop_code", "")) if failed_attempts else stop_code
        final_fail_class = _classify_failure(last_sig, last_sc or stop_code, failure)

    failure_class = attempt_1_fail_class if outcome_kind == "retry_recovery" else (final_fail_class or attempt_1_fail_class)
    if outcome_kind == "counter_fail" and not failure_class:
        failure_class = "counter-test"

    phase_b_steps = sum(
        1 for r in records if str(r.get("phase", "")).lower() in ("implementer", "phase_b", "phase-b", "impl")
    )
    if phase_b_steps == 0:
        phase_b_steps = total_attempts

    tokens_in = int(meta.get("tokens_in", 0) or 0)
    tokens_out = int(meta.get("tokens_out", 0) or 0)
    tokens_total = tokens_in + tokens_out
    if tokens_total == 0 and records:
        tokens_total = sum(
            int(r.get("tokens_in", 0) or 0) + int(r.get("tokens_out", 0) or 0)
            for r in records
        )
    attempt_1_tokens = 0
    if records:
        r1 = min(records, key=lambda r: int(r.get("attempt", 1) or 1))
        attempt_1_tokens = int(r1.get("tokens_in", 0) or 0) + int(r1.get("tokens_out", 0) or 0)
    tokens_delta = max(0, tokens_total - attempt_1_tokens) if total_attempts > 1 else 0
    attempts_delta = max(0, total_attempts - 1)

    spend = float(meta.get("spend", 0.0) or 0.0)
    if spend == 0.0 and records:
        spend = sum(float(r.get("spend", 0.0) or 0.0) for r in records)

    counter_detail = ""
    if had_counter_gate_fail:
        counter_detail = _scrub_test_leaks(failure or "counter-test rejected candidate change")

    tags = [
        sanitize_lesson_text(str(t), max_len=40)
        for t in (meta.get("tags") or ())
        if str(t).strip()
    ]

    before_desc = (
        f"attempt 1 failed ({attempt_1_fail_class or failure_class or 'failure'})"
        if (failed_attempts or not passed)
        else "attempt 1 passed"
    )
    after_desc = (
        f"attempt {recovery_attempt or total_attempts} passed"
        if passed
        else f"stopped ({stop_code or final_fail_class or 'fail'})"
    )
    delta_desc = f"+{attempts_delta} attempts, +{tokens_delta} retry tokens"

    return {
        "card_id": card_id,
        "kind": kind,
        "language": language,
        "final_state": final_state,
        "stop_code": stop_code,
        "outcome_kind": outcome_kind,
        "passed": passed,
        "first_pass": outcome_kind == "pass",
        "retry_recovered": outcome_kind == "retry_recovery",
        "had_counter_fail": had_counter_gate_fail,
        "attempts": total_attempts,
        "phase_b_steps": phase_b_steps,
        "attempt_1_phase": attempt_1_phase,
        "attempt_1_fail_class": attempt_1_fail_class,
        "recovery_attempt": recovery_attempt,
        "final_fail_class": final_fail_class,
        "failure_class": failure_class,
        "counter_fail_detail": counter_detail,
        "tokens_total": tokens_total,
        "tokens_delta_vs_first_attempt": tokens_delta,
        "attempts_delta": attempts_delta,
        "spend": round(spend, 6),
        "tags": tags,
        "evidence_pair": {
            "before": before_desc,
            "after": after_desc,
            "delta": delta_desc,
        },
    }


def _summarize_outcomes(card_events: list[dict]) -> dict:
    total = len(card_events)
    if total == 0:
        return {
            "cards_total": 0,
            "cards_passed": 0,
            "first_pass_rate": 0.0,
            "retry_recovery_rate": 0.0,
            "pass_rate": 0.0,
            "avg_attempts": 0.0,
            "avg_tokens_per_pass": 0.0,
            "avg_spend_per_pass": 0.0,
            "counter_fail_rate": 0.0,
            "harness_efficiency": 0.0,
        }

    passed_events = [e for e in card_events if e.get("passed")]
    cards_passed = len(passed_events)
    first_passes = sum(1 for e in card_events if e.get("first_pass"))
    retry_recoveries = sum(1 for e in card_events if e.get("retry_recovered"))
    counter_fails = sum(1 for e in card_events if e.get("counter_failed"))

    pass_rate = cards_passed / total
    first_pass_rate = first_passes / total
    cards_needing_retry = total - first_passes
    retry_recovery_rate = (
        retry_recoveries / cards_needing_retry if cards_needing_retry > 0 else 0.0
    )
    avg_attempts = sum(float(e.get("attempts", 1) or 1) for e in card_events) / total
    avg_tokens_per_pass = (
        sum(float(e.get("tokens_total", 0) or 0) for e in passed_events) / cards_passed
        if cards_passed > 0
        else 0.0
    )
    avg_spend_per_pass = (
        sum(float(e.get("spend", 0.0) or 0.0) for e in passed_events) / cards_passed
        if cards_passed > 0
        else 0.0
    )
    counter_fail_rate = counter_fails / total

    if cards_passed == 0:
        efficiency = 0.0
    else:
        attempt_eff = 1.0 / max(1.0, avg_attempts)
        token_eff = 1.0 / (1.0 + avg_tokens_per_pass / 10_000.0)
        retry_component = retry_recovery_rate if cards_needing_retry > 0 else first_pass_rate
        raw_eff = (
            0.45 * pass_rate
            + 0.25 * first_pass_rate
            + 0.10 * retry_component
            + 0.12 * attempt_eff
            + 0.08 * token_eff
        )
        efficiency = max(0.0, min(1.0, raw_eff))

    return {
        "cards_total": total,
        "cards_passed": cards_passed,
        "first_pass_rate": round(first_pass_rate, 4),
        "retry_recovery_rate": round(retry_recovery_rate, 4),
        "pass_rate": round(pass_rate, 4),
        "avg_attempts": round(avg_attempts, 4),
        "avg_tokens_per_pass": round(avg_tokens_per_pass, 2),
        "avg_spend_per_pass": round(avg_spend_per_pass, 6),
        "counter_fail_rate": round(counter_fail_rate, 4),
        "harness_efficiency": round(efficiency, 4),
    }


class HarnessLedger:
    """Append-only event ledger and deterministic efficiency metrics calculator."""

    def __init__(self, harness_dir: Path | str, *, window_size: int = 10) -> None:
        self.harness_dir = Path(harness_dir)
        self.ledger_path = self.harness_dir / "ledger.jsonl"
        self.metrics_path = self.harness_dir / "metrics.json"
        self.window_size = max(1, int(window_size))

    def read_events(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        events: list[dict] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    def append_event(self, event_type: str, data: dict) -> dict:
        record = {"event": event_type, "at": utc_now(), **data}
        append_jsonl(self.ledger_path, record)
        return record

    def record_card_outcome(
        self,
        contrast: dict,
        *,
        revision: int,
        cues_applied: list[str] | None = None,
        surfaces_applied: list[str] | None = None,
    ) -> dict:
        return self.append_event(
            "card_outcome",
            {
                "card_id": str(contrast.get("card_id", "")),
                "kind": str(contrast.get("kind", "unknown")),
                "language": str(contrast.get("language", "rust")),
                "revision": int(revision),
                "cues_applied": list(cues_applied or ()),
                "surfaces_applied": list(surfaces_applied or ()),
                "outcome_kind": str(contrast.get("outcome_kind", "other")),
                "passed": bool(contrast.get("passed", False)),
                "first_pass": bool(contrast.get("first_pass", False)),
                "retry_recovered": bool(contrast.get("retry_recovered", False)),
                "counter_failed": bool(
                    contrast.get("outcome_kind") == "counter_fail"
                    or contrast.get("had_counter_fail", False)
                ),
                "attempts": int(contrast.get("attempts", 1) or 1),
                "tokens_total": int(contrast.get("tokens_total", 0) or 0),
                "spend": float(contrast.get("spend", 0.0) or 0.0),
                "failure_class": str(contrast.get("failure_class", "")),
                "stop_code": str(contrast.get("stop_code", "")),
            },
        )

    def record_lesson_added(
        self,
        entry_id: str,
        surface: str,
        confidence: float,
        revision: int,
        *,
        source: str = "deterministic",
        card_id: str = "",
        status: str = "probing",
    ) -> dict:
        return self.append_event(
            "lesson_added",
            {
                "entry_id": entry_id,
                "surface": surface,
                "confidence": round(float(confidence), 4),
                "revision": int(revision),
                "source": source,
                "card_id": card_id,
                "status": status,
            },
        )

    def record_revision_promoted(
        self,
        revision: int,
        parent_revision: int,
        entry_ids: list[str],
        *,
        reason: str = "captured_adaptation",
    ) -> dict:
        return self.append_event(
            "revision_promoted",
            {
                "revision": int(revision),
                "parent_revision": int(parent_revision),
                "entry_ids": list(entry_ids),
                "reason": reason,
            },
        )

    def record_revision_confirmed(
        self, revision: int, *, reason: str = "probe_window_held"
    ) -> dict:
        return self.append_event(
            "revision_confirmed",
            {"revision": int(revision), "reason": reason},
        )

    def record_demotion(
        self,
        target: str,
        revision: int,
        entry_ids: list[str],
        *,
        reason: str = "manual",
    ) -> dict:
        return self.append_event(
            "demotion",
            {
                "target": target,
                "revision": int(revision),
                "entry_ids": list(entry_ids),
                "reason": reason,
            },
        )

    def compute_metrics(
        self,
        events: list[dict] | None = None,
        *,
        window_size: int | None = None,
    ) -> dict:
        all_events = self.read_events() if events is None else list(events)
        win = self.window_size if window_size is None else max(1, int(window_size))
        card_events = [e for e in all_events if e.get("event") == "card_outcome"]

        overall = _summarize_outcomes(card_events)

        by_rev_groups: dict[str, list[dict]] = {}
        by_kind_groups: dict[str, list[dict]] = {}
        by_surface_groups: dict[str, list[dict]] = {}
        for ev in card_events:
            rev_key = str(ev.get("revision", 0))
            by_rev_groups.setdefault(rev_key, []).append(ev)
            kind_key = str(ev.get("kind", "unknown") or "unknown")
            by_kind_groups.setdefault(kind_key, []).append(ev)
            for surf in ev.get("surfaces_applied") or ():
                by_surface_groups.setdefault(str(surf), []).append(ev)

        lessons_by_surface: dict[str, int] = {}
        lessons_added_total = 0
        promotions_total = 0
        demotions_total = 0
        confirmations_total = 0
        for ev in all_events:
            et = ev.get("event")
            if et == "lesson_added":
                lessons_added_total += 1
                surf = str(ev.get("surface", ""))
                if surf:
                    lessons_by_surface[surf] = lessons_by_surface.get(surf, 0) + 1
            elif et == "revision_promoted":
                promotions_total += 1
            elif et == "demotion":
                demotions_total += 1
            elif et == "revision_confirmed":
                confirmations_total += 1

        by_revision = {
            k: _summarize_outcomes(by_rev_groups[k])
            for k in sorted(by_rev_groups, key=lambda x: int(x) if x.isdigit() else 0)
        }
        by_kind = {
            k: _summarize_outcomes(by_kind_groups[k])
            for k in sorted(by_kind_groups)
        }
        all_surfaces = sorted(set(by_surface_groups) | set(lessons_by_surface))
        by_surface = {}
        for surf in all_surfaces:
            summary = _summarize_outcomes(by_surface_groups.get(surf, []))
            summary["lessons_added"] = lessons_by_surface.get(surf, 0)
            by_surface[surf] = summary

        recent_events = card_events[-win:]
        recent_summary = _summarize_outcomes(recent_events)
        recent_summary["window_size"] = win

        return {
            "schema_version": 1,
            **overall,
            "lessons_added": lessons_added_total,
            "promotions": promotions_total,
            "confirmations": confirmations_total,
            "demotions": demotions_total,
            "by_revision": by_revision,
            "by_surface": by_surface,
            "by_kind": by_kind,
            "recent_window": recent_summary,
        }

    def refresh_metrics(self) -> dict:
        metrics = self.compute_metrics()
        _atomic_write_json(self.metrics_path, metrics)
        return metrics


class GuidanceSelection(NamedTuple):
    """Structured result returned by ``HarnessLibrary.select_guidance``."""

    cue_ids: list[str]
    brief_text: str
    micromanager_context: dict


_FALLBACK_CUES_BY_FAILURE: dict[str, tuple[str, str]] = {
    "type-mismatch": (
        "Verify exact function signatures, Result/Option error variants, and return types against the frozen tests before applying edits.",
        "Attempt failed with a type mismatch and recovered once return/parameter types were aligned.",
    ),
    "borrow-checker": (
        "Check ownership, lifetimes, and mutable borrow scopes before returning references or mutating collections.",
        "Attempt hit a borrow-checker failure and recovered after fixing ownership/borrow scopes.",
    ),
    "missing-symbol": (
        "Verify module exports, `use` imports, and exact symbol visibility across touched files before editing.",
        "Attempt failed on missing imports or symbol visibility and recovered once exports were aligned.",
    ),
    "trait-bound": (
        "Check required trait implementations and derive bounds (`Debug`, `Clone`, `PartialEq`, `Send`) on modified types.",
        "Attempt failed on missing trait bounds and recovered after satisfying trait constraints.",
    ),
    "lint-clippy": (
        "Keep production code free of clippy warnings, unused items, and banned `.unwrap()`/`.expect()` patterns.",
        "Attempt failed lint checks and recovered once clippy diagnostics were addressed.",
    ),
    "format-check": (
        "Ensure all modified files conform to repository formatting rules before finishing edits.",
        "Attempt failed formatting check and recovered after formatting.",
    ),
    "counter-test": (
        "Explicitly guard boundary inputs, empty/zero cases, and error paths so adversarial counter-tests cannot bypass assertions.",
        "Counter-test exposed an unhandled boundary condition.",
    ),
    "revert-check": (
        "Ensure named tests genuinely exercise the changed behavior and fail when the production fix is reverted.",
        "Revert check flagged a test that did not depend on the production change.",
    ),
}


class HarnessLibrary:
    """Manages adaptive harness overlays, experience entries, revisions, and probe state."""

    def __init__(self, harness_dir: Path | str, config=None) -> None:
        self.harness_dir = Path(harness_dir)
        self._cfg = config
        self.harness_cfg = self._resolve_harness_cfg(config)
        # v0.7: which lessons this run applies: "both", "project" or "global".
        self.learn_from = str(getattr(self.harness_cfg, "learn_from", "both") or "both")
        self.never_touch = list(getattr(config, "never_touch", ()) or ()) if config is not None else []
        self.forbidden_files = list(getattr(config, "forbidden_files", ()) or ()) if config is not None else []
        if ".openfarm/**" not in self.forbidden_files:
            self.forbidden_files.append(".openfarm/**")

        self.overlay_path = self.harness_dir / "active_overlay.json"
        self.index_path = self.harness_dir / "INDEX.md"
        self.revisions_dir = self.harness_dir / "revisions"
        self.entries_dir = self.harness_dir / "entries"
        self._lock_path = self.harness_dir / ".lock"
        self._local = threading.local()
        self.ledger = HarnessLedger(
            self.harness_dir,
            window_size=max(3, int(self.harness_cfg.probe_window) * 2),
        )
        self._ensure_initialized()

    @contextlib.contextmanager
    def _locked(self):
        """Acquire a per-root thread RLock outside advisory flock (reentrant)."""
        tlock = _harness_thread_lock(self.harness_dir)
        with tlock:
            depth = int(getattr(self._local, "depth", 0) or 0)
            if depth > 0:
                self._local.depth = depth + 1
                try:
                    yield
                finally:
                    self._local.depth = depth
                return
            self.harness_dir.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                self._local.depth = 1
                try:
                    yield
                finally:
                    self._local.depth = 0
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _resolve_harness_cfg(config):
        from .config import HarnessConfig

        if config is None:
            return HarnessConfig()
        if isinstance(config, HarnessConfig):
            return config
        nested = getattr(config, "harness", None)
        if isinstance(nested, HarnessConfig):
            return nested
        return HarnessConfig()

    def _empty_overlay(self, revision: int = 0, parent_revision: int = 0) -> dict:
        return {
            "schema_version": 1,
            "revision": revision,
            "parent_revision": parent_revision,
            "next_revision": max(1, revision + 1),
            "state": "confirmed",
            "probe_remaining": 0,
            "probe_baseline": {
                "cards_total": 0,
                "pass_rate": 0.0,
                "harness_efficiency": 0.0,
            },
            "probe_outcomes": [],
            "active_entry_ids": [],
            "surfaces": {s: [] for s in EDITABLE_SURFACES},
            "updated_at": utc_now(),
        }

    def _ensure_initialized(self) -> None:
        with self._locked():
            self.harness_dir.mkdir(parents=True, exist_ok=True)
            self.revisions_dir.mkdir(parents=True, exist_ok=True)
            self.entries_dir.mkdir(parents=True, exist_ok=True)
            if not self.overlay_path.exists():
                overlay = self._empty_overlay(revision=0, parent_revision=0)
                _atomic_write_json(self.overlay_path, overlay)
            if not (self.revisions_dir / "rev-0000.json").exists():
                _atomic_write_json(
                    self.revisions_dir / "rev-0000.json",
                    self._empty_overlay(revision=0, parent_revision=0),
                )
            if not self.ledger.metrics_path.exists():
                self.ledger.refresh_metrics()
            if not self.index_path.exists():
                self._rebuild_index()

    def _load_overlay(self) -> dict:
        data = read_json(self.overlay_path)
        if not isinstance(data, dict):
            data = self._empty_overlay()
        data.setdefault("schema_version", 1)
        data.setdefault("revision", 0)
        data.setdefault("parent_revision", 0)
        data.setdefault("next_revision", max(1, int(data.get("revision", 0)) + 1))
        data.setdefault("state", "confirmed")
        data.setdefault("probe_remaining", 0)
        data.setdefault("probe_baseline", {"cards_total": 0, "pass_rate": 0.0, "harness_efficiency": 0.0})
        data.setdefault("probe_outcomes", [])
        data.setdefault("active_entry_ids", [])
        surfaces = data.setdefault("surfaces", {})
        for s in EDITABLE_SURFACES:
            surfaces.setdefault(s, [])
        return data

    def _save_overlay(self, overlay: dict) -> dict:
        overlay["updated_at"] = utc_now()
        _atomic_write_json(self.overlay_path, overlay)
        return overlay

    def _next_revision_number(self, overlay: dict) -> int:
        highest = max(int(overlay.get("revision", 0)), int(overlay.get("next_revision", 1)) - 1)
        for p in self.revisions_dir.glob("rev-*.json"):
            stem = p.stem
            if stem.startswith("rev-") and stem[4:].isdigit():
                highest = max(highest, int(stem[4:]))
        next_rev = highest + 1
        overlay["next_revision"] = next_rev + 1
        return next_rev

    def _entry_dir(self, entry_id: str) -> Path:
        safe = _safe_id(entry_id, "entry_id")
        return self.entries_dir / safe

    def _load_entry(self, entry_id: str) -> dict | None:
        safe = _safe_id(entry_id, "entry_id")
        patch_path = self.entries_dir / safe / "patch.json"
        data = read_json(patch_path)
        return data if isinstance(data, dict) else None

    def _save_entry(self, entry: dict) -> dict:
        entry_id = _safe_id(str(entry["id"]), "entry_id")
        edir = self.entries_dir / entry_id
        edir.mkdir(parents=True, exist_ok=True)
        entry["updated_at"] = utc_now()
        _atomic_write_json(edir / "patch.json", entry)
        _atomic_write_text(edir / "EXPERIENCE.md", self.render_experience_md(entry))
        return entry

    def _rebuild_surfaces(self, entry_ids: list[str]) -> dict[str, list[dict]]:
        surfaces: dict[str, list[dict]] = {s: [] for s in EDITABLE_SURFACES}
        for eid in entry_ids:
            entry = self._load_entry(eid)
            if not entry or entry.get("status") not in ("active", "probing"):
                continue
            surf = entry.get("surface", "")
            if surf in surfaces:
                surfaces[surf].append({
                    "id": entry["id"],
                    "role": entry.get("role", "*"),
                    "language": entry.get("language", "*"),
                    "kind": entry.get("kind", "*"),
                    "failure_class": entry.get("failure_class", ""),
                    "cue": entry.get("cue", ""),
                    "confidence": entry.get("confidence", 0.0),
                })
        return surfaces

    def render_experience_md(self, entry: dict) -> str:
        stats = entry.get("stats") or {}
        lines = [
            f"# {entry.get('title', entry.get('id', 'Experience'))}",
            "",
            f"- **ID**: `{entry.get('id', '')}`",
            f"- **Status**: `{entry.get('status', '')}`",
            f"- **Surface**: `{entry.get('surface', '')}`",
            f"- **Role**: `{entry.get('role', '*')}`",
            f"- **Language**: `{entry.get('language', '*')}`",
            f"- **Kind**: `{entry.get('kind', '*')}`",
            f"- **Failure Class**: `{entry.get('failure_class', '') or 'general'}`",
            f"- **Confidence**: `{entry.get('confidence', 0.0)}`",
            f"- **Revision**: `{entry.get('revision', 0)}`",
            f"- **Source**: `{entry.get('source', 'deterministic')}`",
            f"- **Origin Card**: `{entry.get('card_id', '')}`",
            f"- **Stats**: applied={stats.get('applied', 0)}, helped={stats.get('helped', 0)}, hurt={stats.get('hurt', 0)}, neutral={stats.get('neutral', 0)}",
        ]
        if entry.get("demote_reason"):
            lines.append(f"- **Demote Reason**: `{entry['demote_reason']}`")
        lines.extend([
            "",
            "## Cue",
            "",
            str(entry.get("cue", "")),
            "",
            "## Lesson",
            "",
            str(entry.get("lesson", "")),
        ])
        if entry.get("evidence"):
            lines.extend(["", "## Evidence", "", str(entry["evidence"])])
        lines.append("")
        return "\n".join(lines)

    def format_index(self) -> str:
        overlay = self._load_overlay()
        metrics = read_json(self.ledger.metrics_path) or self.ledger.compute_metrics()
        entries = self.list_entries()

        lines = [
            "# Harness Efficiency Index",
            "",
            f"- **Revision**: `rev-{int(overlay.get('revision', 0)):04d}`",
            f"- **State**: `{overlay.get('state', 'confirmed')}`",
            f"- **Probe Remaining**: `{overlay.get('probe_remaining', 0)}`",
            f"- **Cards Total**: `{metrics.get('cards_total', 0)}`",
            f"- **Pass Rate**: `{metrics.get('pass_rate', 0.0)}`",
            f"- **First-Pass Rate**: `{metrics.get('first_pass_rate', 0.0)}`",
            f"- **Harness Efficiency**: `{metrics.get('harness_efficiency', 0.0)}`",
            "",
            "## Entries",
            "",
        ]
        if not entries:
            lines.append("_No harness adaptations recorded yet._")
        else:
            lines.append("| ID | Status | Surface | Role | Scope | Failure Class | Conf | Stats (H/X) | Cue |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for e in entries:
                stats = e.get("stats") or {}
                scope = f"{e.get('language', '*')}/{e.get('kind', '*')}"
                cue_preview = str(e.get("cue", "")).replace("|", "/").replace("\n", " ")[:90]
                lines.append(
                    f"| `{e.get('id', '')}` | `{e.get('status', '')}` | `{e.get('surface', '')}` "
                    f"| `{e.get('role', '*')}` | `{scope}` | `{e.get('failure_class', '') or '-'}` "
                    f"| `{e.get('confidence', 0.0):.2f}` | `+{stats.get('helped', 0)}/-{stats.get('hurt', 0)}` "
                    f"| {cue_preview} |"
                )
        lines.append("")
        return "\n".join(lines)

    def _rebuild_index(self) -> None:
        _atomic_write_text(self.index_path, self.format_index())

    # -- query / selection ----------------------------------------------------

    def list_entries(self, status: str | None = None) -> list[dict]:
        entries: list[dict] = []
        if not self.entries_dir.exists():
            return entries
        for edir in sorted(self.entries_dir.iterdir(), key=lambda p: p.name):
            if not edir.is_dir():
                continue
            data = read_json(edir / "patch.json")
            if not isinstance(data, dict):
                continue
            if status is not None and data.get("status") != status:
                continue
            entries.append(data)
        entries.sort(key=lambda e: (int(e.get("revision", 0)), str(e.get("id", ""))))
        return entries

    def show_entry(self, entry_id: str) -> dict | None:
        entry = self._load_entry(entry_id)
        if entry is None:
            return None
        md_path = self._entry_dir(entry_id) / "EXPERIENCE.md"
        result = dict(entry)
        result["experience_md"] = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        return result

    def status(self) -> dict:
        overlay = self._load_overlay()
        entries = self.list_entries()
        counts = {"active": 0, "probing": 0, "demoted": 0, "candidate": 0}
        for e in entries:
            st = str(e.get("status", ""))
            if st in counts:
                counts[st] += 1
        metrics = read_json(self.ledger.metrics_path) or self.ledger.compute_metrics()
        return {
            "enabled": self.harness_cfg.enabled,
            "revision": int(overlay.get("revision", 0)),
            "parent_revision": int(overlay.get("parent_revision", 0)),
            "state": str(overlay.get("state", "confirmed")),
            "probe_remaining": int(overlay.get("probe_remaining", 0)),
            "probe_baseline": dict(overlay.get("probe_baseline") or {}),
            "active_entry_ids": list(overlay.get("active_entry_ids") or ()),
            "active_count": counts["active"],
            "probing_count": counts["probing"],
            "candidate_count": counts["candidate"],
            "demoted_count": counts["demoted"],
            "total_entries": len(entries),
            "surfaces": dict(overlay.get("surfaces") or {}),
            "metrics": metrics,
        }

    def _applies_here(self, entry: dict) -> bool:
        """`learn_from` decides whether a project's own or imported global lessons apply."""
        is_global = entry.get("origin") == "global"
        if self.learn_from == "project":
            return not is_global
        if self.learn_from == "global":
            return is_global
        return True

    def _entry_matches_role(self, entry: dict, role: str) -> bool:
        entry_role = str(entry.get("role", "*"))
        surface = str(entry.get("surface", ""))
        if role == "micromanager":
            return surface == "micromanager_context" or entry_role == "micromanager" or (
                surface == "context_hints" and entry_role in ("*", "micromanager")
            )
        expected_surface = f"{role}_guidance"
        if surface == expected_surface:
            return entry_role in ("", "*", role)
        if surface == "context_hints":
            return entry_role in ("", "*", role)
        return entry_role == role and surface != "micromanager_context"

    def select_guidance(
        self,
        language: str,
        kind: str,
        role: str,
        *,
        failure_class: str = "",
        tags: list[str] | tuple[str, ...] = (),
        max_cues: int | None = None,
        min_confidence: float | None = None,
    ) -> GuidanceSelection:
        """Select matching, highest-ranked cues for a role brief and micromanager context."""
        if not self.harness_cfg.enabled:
            return GuidanceSelection([], "", {})

        limit = self.harness_cfg.max_cues_per_brief if max_cues is None else max(0, int(max_cues))
        min_conf = self.harness_cfg.min_confidence if min_confidence is None else float(min_confidence)
        tag_set = {str(t).strip().lower() for t in (tags or ()) if str(t).strip()}

        overlay = self._load_overlay()
        active_ids = list(overlay.get("active_entry_ids") or ())
        candidates: list[tuple[tuple, dict]] = []
        mm_candidates: list[tuple[tuple, dict]] = []

        for eid in active_ids:
            entry = self._load_entry(eid)
            if not entry or entry.get("status") not in ("active", "probing"):
                continue
            if not self._applies_here(entry):
                continue
            conf = float(entry.get("confidence", 0.0) or 0.0)
            if conf < min_conf:
                continue
            entry_lang = str(entry.get("language", "*") or "*")
            entry_kind = str(entry.get("kind", "*") or "*")
            if entry_lang not in ("*", language):
                continue
            if entry_kind not in ("*", kind):
                continue
            entry_fc = str(entry.get("failure_class", "") or "")
            if failure_class and entry_fc and entry_fc != failure_class:
                continue

            entry_tags = {str(t).strip().lower() for t in (entry.get("tags") or ()) if str(t).strip()}
            specificity = 0
            if failure_class and entry_fc == failure_class:
                specificity += 4
            if entry_lang == language:
                specificity += 2
            if entry_kind == kind:
                specificity += 2
            if str(entry.get("role", "*")) == role:
                specificity += 1
            specificity += len(tag_set & entry_tags)

            stats = entry.get("stats") or {}
            helpfulness = int(stats.get("helped", 0) or 0) - int(stats.get("hurt", 0) or 0)
            score = (
                specificity,
                0 if entry.get("origin") == "global" else 1,
                helpfulness,
                conf,
                int(entry.get("revision", 0) or 0),
                str(entry.get("id", "")),
            )

            if self._entry_matches_role(entry, role):
                candidates.append((score, entry))
            if (
                entry.get("surface") == "micromanager_context"
                or self._entry_matches_role(entry, "micromanager")
                or self._entry_matches_role(entry, role)
            ):
                mm_candidates.append((score, entry))

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = [entry for _, entry in candidates[:limit]] if limit > 0 else []

        cue_ids = [str(e["id"]) for e in selected]
        brief_lines = []
        for e in selected:
            safe_id = re.sub(r"\s+", " ", str(e["id"])).strip()
            safe_surf = re.sub(r"\s+", " ", str(e["surface"])).strip()
            safe_fc = re.sub(r"\s+", " ", str(e.get("failure_class") or "")).strip()
            safe_cue = re.sub(r"\s+", " ", str(e.get("cue") or "")).strip()
            fc_label = f", {safe_fc}" if safe_fc else ""
            brief_lines.append(f"- [{safe_id}] ({safe_surf}{fc_label}): {safe_cue}")
        brief_text = "\n".join(brief_lines)

        mm_candidates.sort(key=lambda item: item[0], reverse=True)
        seen_mm: set[str] = set()
        mm_cues: list[dict] = []
        # Prioritize explicit micromanager_context entries, then matching role cues.
        mm_sorted = sorted(
            mm_candidates,
            key=lambda item: (1 if item[1].get("surface") == "micromanager_context" else 0, item[0]),
            reverse=True,
        )
        for _, e in mm_sorted:
            eid = str(e["id"])
            if eid in seen_mm:
                continue
            seen_mm.add(eid)
            mm_cues.append({
                "id": eid,
                "surface": e["surface"],
                "role": e.get("role", "*"),
                "failure_class": re.sub(r"\s+", " ", str(e.get("failure_class") or "")).strip(),
                "cue": re.sub(r"\s+", " ", str(e.get("cue") or "")).strip(),
                "confidence": e["confidence"],
            })
            if limit > 0 and len(mm_cues) >= limit:
                break

        mm_context = (
            {
                "revision": int(overlay.get("revision", 0)),
                "state": str(overlay.get("state", "confirmed")),
                "cues": mm_cues,
            }
            if mm_cues
            else {}
        )
        return GuidanceSelection(cue_ids=cue_ids, brief_text=brief_text, micromanager_context=mm_context)

    def select_context_hints(
        self,
        language: str,
        kind: str,
        role: str,
        *,
        failure_class: str = "",
        tags: list[str] | tuple[str, ...] = (),
        max_files: int | None = None,
        min_confidence: float | None = None,
    ) -> tuple[list[str], list[str]]:
        """Return ``(cue_ids, relative_file_paths)`` from active/probing ``context_hints`` entries."""
        if not self.harness_cfg.enabled:
            return [], []

        cap = (
            int(getattr(self._cfg, "max_context_files", 8) or 8)
            if max_files is None
            else max(0, int(max_files))
        )
        if cap <= 0:
            return [], []
        min_conf = self.harness_cfg.min_confidence if min_confidence is None else float(min_confidence)
        tag_set = {str(t).strip().lower() for t in (tags or ()) if str(t).strip()}

        overlay = self._load_overlay()
        active_ids = list(overlay.get("active_entry_ids") or ())
        ranked: list[tuple[tuple, dict]] = []

        for eid in active_ids:
            entry = self._load_entry(eid)
            if not entry or entry.get("status") not in ("active", "probing"):
                continue
            if not self._applies_here(entry):
                continue
            if entry.get("surface") != "context_hints":
                continue
            entry_role = str(entry.get("role", "*") or "*")
            if entry_role not in ("", "*", role):
                continue
            conf = float(entry.get("confidence", 0.0) or 0.0)
            if conf < min_conf:
                continue
            entry_lang = str(entry.get("language", "*") or "*")
            entry_kind = str(entry.get("kind", "*") or "*")
            if entry_lang not in ("*", language):
                continue
            if entry_kind not in ("*", kind):
                continue
            entry_fc = str(entry.get("failure_class", "") or "")
            if failure_class and entry_fc and entry_fc != failure_class:
                continue

            entry_tags = {str(t).strip().lower() for t in (entry.get("tags") or ()) if str(t).strip()}
            specificity = 0
            if failure_class and entry_fc == failure_class:
                specificity += 4
            if entry_lang == language:
                specificity += 2
            if entry_kind == kind:
                specificity += 2
            if entry_role == role:
                specificity += 1
            specificity += len(tag_set & entry_tags)

            stats = entry.get("stats") or {}
            helpfulness = int(stats.get("helped", 0) or 0) - int(stats.get("hurt", 0) or 0)
            score = (
                specificity,
                0 if entry.get("origin") == "global" else 1,
                helpfulness,
                conf,
                int(entry.get("revision", 0) or 0),
                str(entry.get("id", "")),
            )
            ranked.append((score, entry))

        ranked.sort(key=lambda item: item[0], reverse=True)
        cue_ids: list[str] = []
        paths: list[str] = []
        seen_paths: set[str] = set()

        for _, entry in ranked:
            patch = entry.get("proposed_patch") or {}
            if not isinstance(patch, dict):
                continue
            raw_paths: list[str] = []
            for list_key in ("context_files", "paths", "files"):
                vals = patch.get(list_key)
                if isinstance(vals, (list, tuple)):
                    raw_paths.extend(str(v) for v in vals if isinstance(v, str))
            for single_key in ("path", "file"):
                val = patch.get(single_key)
                if isinstance(val, str) and val.strip():
                    raw_paths.append(val)

            added_from_entry = False
            for raw_p in raw_paths:
                if _is_unsafe_path(raw_p, never_touch=self.never_touch, forbidden_files=self.forbidden_files):
                    continue
                norm_p = normalise(_strip_unicode_noise(raw_p))
                if not norm_p or norm_p in seen_paths:
                    continue
                seen_paths.add(norm_p)
                paths.append(norm_p)
                added_from_entry = True
                if len(paths) >= cap:
                    break
            if added_from_entry:
                cue_ids.append(str(entry["id"]))
            if len(paths) >= cap:
                break

        return cue_ids, paths

    # -- deterministic capture & outcome recording ----------------------------

    def _deterministic_extract(self, card_meta: dict, contrast: dict, failure: str) -> list[dict]:
        outcome_kind = str(contrast.get("outcome_kind", ""))
        if outcome_kind not in ("retry_recovery", "fail", "counter_fail"):
            return []

        language = str(contrast.get("language") or "*")
        kind = str(contrast.get("kind") or "*")
        fc = str(contrast.get("failure_class") or "")
        phase = str(contrast.get("attempt_1_phase") or "").lower()
        card_id = str(contrast.get("card_id") or "")

        if outcome_kind == "counter_fail":
            surface = "implementer_guidance"
            role = "implementer"
            cue, lesson_base = _FALLBACK_CUES_BY_FAILURE["counter-test"]
            conf = 0.68
        else:
            surface = "test_writer_guidance" if "test" in phase else "implementer_guidance"
            role = "test_writer" if surface == "test_writer_guidance" else "implementer"
            if fc in _FALLBACK_CUES_BY_FAILURE:
                cue, lesson_base = _FALLBACK_CUES_BY_FAILURE[fc]
                conf = 0.72 if outcome_kind == "retry_recovery" else 0.62
            else:
                fc_label = fc or "execution failure"
                cue = (
                    f"Double-check constraints related to `{fc_label}` before finalizing edits "
                    f"so initial attempts avoid avoidable retries."
                )
                lesson_base = f"Card {card_id} experienced `{fc_label}` ({outcome_kind})."
                conf = 0.64 if outcome_kind == "retry_recovery" else 0.58

        ev_pair = contrast.get("evidence_pair") or {}
        evidence = (
            f"card={card_id}; before={ev_pair.get('before', '')}; "
            f"after={ev_pair.get('after', '')}; delta={ev_pair.get('delta', '')}"
        )
        return [
            {
                "title": f"{fc or outcome_kind}: {surface}",
                "surface": surface,
                "role": role,
                "language": language,
                "kind": kind,
                "failure_class": fc,
                "cue": cue,
                "lesson": lesson_base,
                "evidence": evidence,
                "confidence": conf,
                "tags": list(contrast.get("tags") or ()),
                "proposed_patch": {surface: cue},
                "source": "deterministic",
                "scope": "general",
                "scope_reason": "fixed-rule cue",
            }
        ]

    def _make_entry_id(self, candidate: dict, existing_ids: set[str]) -> str:
        fingerprint_src = "|".join([
            candidate["surface"],
            candidate["role"],
            candidate["language"],
            candidate["kind"],
            candidate["failure_class"],
            candidate["cue"].lower(),
        ])
        digest = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", (candidate["failure_class"] or candidate["surface"]).lower()).strip("-")[:20]
        base = f"exp-{slug}-{digest}" if slug else f"exp-{digest}"
        candidate_id = base
        idx = 2
        while candidate_id in existing_ids:
            candidate_id = f"{base}-{idx}"
            idx += 1
        return candidate_id

    @staticmethod
    def _cue_fingerprint(entry: dict) -> tuple[str, str, str, str, str, str]:
        return (
            str(entry.get("surface", "")),
            str(entry.get("role", "*")),
            str(entry.get("language", "*")),
            str(entry.get("kind", "*")),
            str(entry.get("failure_class", "")),
            re.sub(r"\s+", " ", str(entry.get("cue", "")).strip().lower()),
        )

    def _update_applied_stats(self, cues_applied: list[str], contrast: dict) -> list[str]:
        surfaces: list[str] = []
        outcome_kind = str(contrast.get("outcome_kind", ""))
        passed = bool(contrast.get("passed", False))
        for eid in cues_applied:
            try:
                entry = self._load_entry(eid)
            except Invalid:
                continue
            if not entry:
                continue
            stats = entry.setdefault("stats", {"applied": 0, "helped": 0, "hurt": 0, "neutral": 0})
            stats["applied"] = int(stats.get("applied", 0)) + 1
            if passed:
                stats["helped"] = int(stats.get("helped", 0)) + 1
            elif outcome_kind in ("fail", "counter_fail"):
                stats["hurt"] = int(stats.get("hurt", 0)) + 1
            else:
                stats["neutral"] = int(stats.get("neutral", 0)) + 1
            self._save_entry(entry)
            surf = str(entry.get("surface", ""))
            if surf and surf not in surfaces:
                surfaces.append(surf)
        return surfaces

    def _check_probe_window(self, overlay: dict, contrast: dict) -> dict | None:
        if overlay.get("state") != "probing":
            return None

        outcomes = list(overlay.get("probe_outcomes") or [])
        outcomes.append({
            "card_id": contrast.get("card_id", ""),
            "passed": bool(contrast.get("passed", False)),
            "first_pass": bool(contrast.get("first_pass", False)),
            "retry_recovered": bool(contrast.get("retry_recovered", False)),
            "counter_failed": bool(
                contrast.get("outcome_kind") == "counter_fail"
                or contrast.get("had_counter_fail", False)
            ),
            "attempts": int(contrast.get("attempts", 1) or 1),
            "tokens_total": int(contrast.get("tokens_total", 0) or 0),
            "spend": float(contrast.get("spend", 0.0) or 0.0),
        })
        overlay["probe_outcomes"] = outcomes
        remaining = max(0, int(overlay.get("probe_remaining", 1)) - 1)
        overlay["probe_remaining"] = remaining
        if remaining > 0:
            self._save_overlay(overlay)
            return None

        probe_summary = _summarize_outcomes(outcomes)
        baseline = dict(overlay.get("probe_baseline") or {})
        base_cards = int(baseline.get("cards_total", 0) or 0)
        base_pass = float(baseline.get("pass_rate", 0.0) or 0.0)
        base_eff = float(baseline.get("harness_efficiency", 0.0) or 0.0)

        tol = 0.05
        if base_cards > 0:
            regressed = (
                probe_summary["pass_rate"] + tol < base_pass
                or probe_summary["harness_efficiency"] + tol < base_eff
            )
        else:
            regressed = probe_summary["pass_rate"] == 0.0

        current_rev = int(overlay.get("revision", 0))
        parent_rev = int(overlay.get("parent_revision", 0))

        if regressed and self.harness_cfg.auto_demote:
            parent_snap = read_json(self.revisions_dir / f"rev-{parent_rev:04d}.json")
            parent_ids = set((parent_snap or {}).get("active_entry_ids") or ())
            demoted_ids: list[str] = []
            for eid in list(overlay.get("active_entry_ids") or ()):
                entry = self._load_entry(eid)
                if not entry:
                    continue
                if eid not in parent_ids or int(entry.get("revision", 0)) == current_rev:
                    entry["status"] = "demoted"
                    entry["demote_reason"] = "probe_regression"
                    self._save_entry(entry)
                    demoted_ids.append(eid)

            restored_ids = [
                eid for eid in ((parent_snap or {}).get("active_entry_ids") or ())
                if eid not in demoted_ids
            ]
            overlay["revision"] = parent_rev
            overlay["parent_revision"] = int((parent_snap or {}).get("parent_revision", 0))
            overlay["state"] = "confirmed"
            overlay["probe_remaining"] = 0
            overlay["probe_outcomes"] = []
            overlay["active_entry_ids"] = restored_ids
            overlay["surfaces"] = self._rebuild_surfaces(restored_ids)
            self._save_overlay(overlay)
            self.ledger.record_demotion(
                target=f"rev-{current_rev:04d}",
                revision=current_rev,
                entry_ids=demoted_ids,
                reason="probe_regression",
            )
            promoted = self._promote_buffered_candidates(overlay, self.ledger.compute_metrics())
            transition = {
                "action": "rolled_back",
                "failed_revision": current_rev,
                "restored_revision": parent_rev,
                "demoted_entry_ids": demoted_ids,
                "probe_summary": probe_summary,
            }
            if promoted is not None:
                transition["promoted_candidates"] = promoted
            return transition

        # Confirm probing revision (held or auto_demote disabled); keep rev-XXXX.json immutable.
        for eid in list(overlay.get("active_entry_ids") or ()):
            entry = self._load_entry(eid)
            if entry and entry.get("status") == "probing":
                entry["status"] = "active"
                self._save_entry(entry)
        overlay["state"] = "confirmed"
        overlay["probe_remaining"] = 0
        overlay["probe_outcomes"] = []
        overlay["surfaces"] = self._rebuild_surfaces(overlay["active_entry_ids"])
        self._save_overlay(overlay)
        self.ledger.record_revision_confirmed(current_rev)
        promoted = self._promote_buffered_candidates(overlay, self.ledger.compute_metrics())
        transition = {
            "action": "confirmed",
            "revision": current_rev,
            "probe_summary": probe_summary,
        }
        if promoted is not None:
            transition["promoted_candidates"] = promoted
        return transition

    def _promote_buffered_candidates(self, overlay: dict, baseline_metrics: dict) -> dict | None:
        if not self.harness_cfg.enabled or self.harness_cfg.max_active_adaptations <= 0:
            return None
        if overlay.get("state") == "probing":
            return None

        active_set = set(overlay.get("active_entry_ids") or ())
        eligible = [
            e
            for e in self.list_entries(status="candidate")
            if str(e.get("id", "")) not in active_set
            and float(e.get("confidence", 0.0) or 0.0) >= self.harness_cfg.min_confidence
            and str(e.get("surface", "")) in self.harness_cfg.editable_surfaces
        ]
        if not eligible:
            return None

        prev_rev = int(overlay.get("revision", 0))
        next_rev = self._next_revision_number(overlay)
        cand_ids: list[str] = []
        for entry in eligible:
            entry["status"] = "probing"
            entry["revision"] = next_rev
            self._save_entry(entry)
            cand_ids.append(str(entry["id"]))

        combined_ids = list(dict.fromkeys(list(overlay.get("active_entry_ids") or ()) + cand_ids))
        kept_ids, evicted_ids = self._enforce_active_cap(
            combined_ids, self.harness_cfg.max_active_adaptations
        )
        if evicted_ids:
            self.ledger.record_demotion(
                target="cap",
                revision=next_rev,
                entry_ids=evicted_ids,
                reason="evicted_by_cap",
            )
        surviving = [eid for eid in cand_ids if eid in kept_ids]
        if not surviving:
            overlay["active_entry_ids"] = kept_ids
            overlay["surfaces"] = self._rebuild_surfaces(kept_ids)
            self._save_overlay(overlay)
            return None

        overlay["parent_revision"] = prev_rev
        overlay["revision"] = next_rev
        overlay["state"] = "probing"
        overlay["probe_remaining"] = int(self.harness_cfg.probe_window)
        overlay["probe_baseline"] = {
            "cards_total": int(baseline_metrics.get("cards_total", 0)),
            "pass_rate": float(baseline_metrics.get("pass_rate", 0.0)),
            "harness_efficiency": float(baseline_metrics.get("harness_efficiency", 0.0)),
        }
        overlay["probe_outcomes"] = []
        overlay["active_entry_ids"] = kept_ids
        overlay["surfaces"] = self._rebuild_surfaces(kept_ids)
        self._save_overlay(overlay)
        _atomic_write_json(self.revisions_dir / f"rev-{next_rev:04d}.json", overlay)
        self.ledger.record_revision_promoted(
            revision=next_rev,
            parent_revision=prev_rev,
            entry_ids=surviving,
            reason="buffered_candidates",
        )
        return {
            "revision": next_rev,
            "parent_revision": prev_rev,
            "entry_ids": surviving,
        }

    def _enforce_active_cap(
        self,
        active_ids: list[str],
        cap: int,
        *,
        protected_ids: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[list[str], list[str]]:
        if len(active_ids) <= cap:
            return active_ids, []
        scored: list[tuple[tuple, str]] = []
        for eid in active_ids:
            entry = self._load_entry(eid)
            if not entry:
                continue
            stats = entry.get("stats") or {}
            utility = int(stats.get("helped", 0) or 0) - int(stats.get("hurt", 0) or 0)
            conf = float(entry.get("confidence", 0.0) or 0.0)
            rev = int(entry.get("revision", 0) or 0)
            is_protected = 1 if eid in protected_ids else 0
            scored.append(((is_protected, utility, conf, rev, eid), eid))
        scored.sort(key=lambda item: item[0], reverse=True)
        kept = [eid for _, eid in scored[:cap]]
        evicted = [eid for _, eid in scored[cap:]]
        for eid in evicted:
            entry = self._load_entry(eid)
            if entry:
                entry["status"] = "demoted"
                entry["demote_reason"] = "evicted_by_cap"
                self._save_entry(entry)
        return kept, evicted

    def record_card_outcome(
        self,
        card_meta: dict,
        attempt_records: list[dict],
        cues_applied: list[str] | None = None,
        failure: str = "",
        micromanager=None,
        reflect: bool = True,
        scope_markers: set[str] | None = None,
    ) -> dict:
        """Record a card run outcome, update probe state, capture adaptations, and refresh metrics."""
        with self._locked():
            contrast = extract_contrast(card_meta, attempt_records, failure=failure)
            overlay = self._load_overlay()
            cues_list = list(cues_applied or ())

            # Snapshot pre-card metrics before appending this card's outcome so
            # probe_baseline captures true pre-card performance.
            metrics_before_capture = self.ledger.compute_metrics()

            candidates: list[dict] = []
            rejected_proposals: list[dict] = []
            will_capture = (
                self.harness_cfg.enabled
                and contrast["outcome_kind"] in self.harness_cfg.capture_on
            )
            reflect_on = getattr(self.harness_cfg, "reflect_on", None)
            if reflect_on is None:
                reflect_on = self.harness_cfg.capture_on
            reflect_kind = contrast["outcome_kind"]
            if reflect_kind == "pass" and int(card_meta.get("handbacks", 0) or 0) > 0:
                # A worker that handed back and passed after a hint was stuck: not a
                # clean first-try pass, so it is reflected on as a recovery would be.
                reflect_kind = "retry_recovery"
            if (
                will_capture
                and reflect
                and reflect_kind in reflect_on
                and self.harness_cfg.allow_micromanager_reflection
                and micromanager is not None
                and getattr(micromanager, "enabled", False)
            ):
                try:
                    refl = micromanager.reflect_harness(
                        card_meta,
                        contrast,
                        overlay,
                        self.format_index(),
                        metrics_before_capture,
                        cfg=self._cfg,
                    )
                except (AdapterError, ValueError) as exc:
                    refl = {}
                    rejected_proposals.append({
                        "reason": f"reflect_harness error ({type(exc).__name__}): {exc}",
                        "raw": {},
                    })
                if isinstance(refl, dict):
                    for item in refl.get("lessons") or ():
                        if isinstance(item, dict):
                            candidates.append({**item, "source": item.get("source") or "micromanager"})
                    for rej in refl.get("rejected") or ():
                        if isinstance(rej, dict):
                            rejected_proposals.append(rej)

            surfaces_applied = self._update_applied_stats(cues_list, contrast)
            self.ledger.record_card_outcome(
                contrast,
                revision=int(overlay.get("revision", 0)),
                cues_applied=cues_list,
                surfaces_applied=surfaces_applied,
            )

            probe_transition = self._check_probe_window(overlay, contrast)
            overlay = self._load_overlay()

            lessons_added: list[str] = []
            buffered_candidates: list[str] = []

            if will_capture:

                if not candidates:
                    candidates = self._deterministic_extract(card_meta, contrast, failure)

                existing_entries = self.list_entries()
                existing_ids = {str(e["id"]) for e in existing_entries}
                existing_by_fp: dict[tuple, dict] = {
                    self._cue_fingerprint(e): e for e in existing_entries
                }

                is_probing = overlay.get("state") == "probing"
                new_entry_ids: list[str] = []
                next_rev = None

                for raw_cand in candidates:
                    cleaned, reason = validate_harness_proposal(
                        raw_cand,
                        allowed_surfaces=self.harness_cfg.editable_surfaces,
                        never_touch=self.never_touch,
                        forbidden_files=self.forbidden_files,
                        default_language=contrast["language"],
                        default_kind=contrast["kind"],
                    )
                    if cleaned is None:
                        rejected_proposals.append({"reason": reason, "raw": raw_cand})
                        continue
                    if cleaned["scope"] == "general":
                        # No markers means nothing to check the claim against.
                        cleaned["scope"], checked = (
                            scope_of(cleaned, set(scope_markers))
                            if scope_markers is not None
                            else ("project", "no project markers to check against"))
                        if cleaned["scope"] != "general":
                            cleaned["scope_reason"] = sanitize_lesson_text(checked, max_len=200)
                    if cleaned["confidence"] < self.harness_cfg.min_confidence:
                        rejected_proposals.append({
                            "reason": f"confidence {cleaned['confidence']} below min_confidence {self.harness_cfg.min_confidence}",
                            "raw": raw_cand,
                        })
                        continue

                    fp = self._cue_fingerprint(cleaned)
                    prior = existing_by_fp.get(fp)
                    if prior is not None:
                        if prior.get("status") == "demoted":
                            rejected_proposals.append({
                                "reason": f"matches demoted entry {prior['id']}",
                                "raw": raw_cand,
                            })
                            continue
                        # Reinforce existing entry rather than duplicating.
                        prior["confidence"] = round(
                            min(1.0, max(float(prior.get("confidence", 0.0)), cleaned["confidence"]) + 0.02),
                            4,
                        )
                        if cleaned["evidence"] and cleaned["evidence"] not in str(prior.get("evidence", "")):
                            prior["evidence"] = sanitize_lesson_text(
                                f"{prior.get('evidence', '')}\n{cleaned['evidence']}".strip(),
                                max_len=400,
                            )
                        self._save_entry(prior)
                        continue

                    entry_id = self._make_entry_id(cleaned, existing_ids)
                    existing_ids.add(entry_id)
                    now = utc_now()

                    if is_probing:
                        # Buffer as candidate without stacking or overwriting the active probing revision.
                        entry_record = {
                            "id": entry_id,
                            "title": cleaned["title"],
                            "surface": cleaned["surface"],
                            "role": cleaned["role"],
                            "language": cleaned["language"],
                            "kind": cleaned["kind"],
                            "failure_class": cleaned["failure_class"],
                            "tags": cleaned["tags"],
                            "cue": cleaned["cue"],
                            "lesson": cleaned["lesson"],
                            "evidence": cleaned["evidence"],
                            "proposed_patch": cleaned["proposed_patch"],
                            "confidence": cleaned["confidence"],
                            "status": "candidate",
                            "source": cleaned["source"],
                            "scope": cleaned["scope"],
                            "scope_reason": cleaned["scope_reason"],
                            "origin": "project",
                            "card_id": contrast["card_id"],
                            "revision": 0,
                            "created_at": now,
                            "updated_at": now,
                            "stats": {"applied": 0, "helped": 0, "hurt": 0, "neutral": 0},
                        }
                        self._save_entry(entry_record)
                        existing_by_fp[fp] = entry_record
                        buffered_candidates.append(entry_id)
                        lessons_added.append(entry_id)
                        self.ledger.record_lesson_added(
                            entry_id=entry_id,
                            surface=entry_record["surface"],
                            confidence=entry_record["confidence"],
                            revision=0,
                            source=entry_record["source"],
                            card_id=contrast["card_id"],
                            status="candidate",
                        )
                    else:
                        if next_rev is None:
                            next_rev = self._next_revision_number(overlay)
                        entry_record = {
                            "id": entry_id,
                            "title": cleaned["title"],
                            "surface": cleaned["surface"],
                            "role": cleaned["role"],
                            "language": cleaned["language"],
                            "kind": cleaned["kind"],
                            "failure_class": cleaned["failure_class"],
                            "tags": cleaned["tags"],
                            "cue": cleaned["cue"],
                            "lesson": cleaned["lesson"],
                            "evidence": cleaned["evidence"],
                            "proposed_patch": cleaned["proposed_patch"],
                            "confidence": cleaned["confidence"],
                            "status": "probing",
                            "source": cleaned["source"],
                            "scope": cleaned["scope"],
                            "scope_reason": cleaned["scope_reason"],
                            "origin": "project",
                            "card_id": contrast["card_id"],
                            "revision": next_rev,
                            "created_at": now,
                            "updated_at": now,
                            "stats": {"applied": 0, "helped": 0, "hurt": 0, "neutral": 0},
                        }
                        self._save_entry(entry_record)
                        existing_by_fp[fp] = entry_record
                        new_entry_ids.append(entry_id)
                        lessons_added.append(entry_id)
                        self.ledger.record_lesson_added(
                            entry_id=entry_id,
                            surface=entry_record["surface"],
                            confidence=entry_record["confidence"],
                            revision=next_rev,
                            source=entry_record["source"],
                            card_id=contrast["card_id"],
                            status="probing",
                        )

                if not is_probing and new_entry_ids and next_rev is not None:
                    prev_rev = int(overlay.get("revision", 0))
                    combined_ids = list(dict.fromkeys(list(overlay.get("active_entry_ids") or ()) + new_entry_ids))
                    kept_ids, evicted_ids = self._enforce_active_cap(
                        combined_ids, self.harness_cfg.max_active_adaptations
                    )
                    if evicted_ids:
                        self.ledger.record_demotion(
                            target="cap",
                            revision=next_rev,
                            entry_ids=evicted_ids,
                            reason="evicted_by_cap",
                        )

                    overlay["parent_revision"] = prev_rev
                    overlay["revision"] = next_rev
                    overlay["state"] = "probing"
                    overlay["probe_remaining"] = int(self.harness_cfg.probe_window)
                    overlay["probe_baseline"] = {
                        "cards_total": int(metrics_before_capture.get("cards_total", 0)),
                        "pass_rate": float(metrics_before_capture.get("pass_rate", 0.0)),
                        "harness_efficiency": float(metrics_before_capture.get("harness_efficiency", 0.0)),
                    }
                    overlay["probe_outcomes"] = []
                    overlay["active_entry_ids"] = kept_ids
                    overlay["surfaces"] = self._rebuild_surfaces(kept_ids)
                    self._save_overlay(overlay)
                    _atomic_write_json(self.revisions_dir / f"rev-{next_rev:04d}.json", overlay)
                    self.ledger.record_revision_promoted(
                        revision=next_rev,
                        parent_revision=prev_rev,
                        entry_ids=new_entry_ids,
                        reason=contrast["outcome_kind"],
                    )

            metrics = self.ledger.refresh_metrics()
            self._rebuild_index()
            final_overlay = self._load_overlay()
            return {
                "contrast": contrast,
                "revision": int(final_overlay.get("revision", 0)),
                "state": str(final_overlay.get("state", "confirmed")),
                "lessons_added": lessons_added,
                "buffered_candidates": buffered_candidates,
                "rejected": rejected_proposals,
                "probe_transition": probe_transition,
                "metrics": metrics,
            }

    # -- management / CLI control APIs ----------------------------------------

    def promote_entry(self, entry_id: str, reason: str = "manual") -> dict:
        with self._locked():
            safe = _safe_id(entry_id, "entry_id")
            entry = self._load_entry(safe)
            if entry is None:
                raise Invalid("unknown-harness-entry", f"harness entry {safe!r} does not exist")

            cleaned, err = validate_harness_proposal(
                entry,
                allowed_surfaces=self.harness_cfg.editable_surfaces,
                never_touch=self.never_touch,
                forbidden_files=self.forbidden_files,
            )
            if cleaned is None:
                raise Refused("unsafe-harness-entry", f"cannot promote {safe!r}: {err}")

            overlay = self._load_overlay()
            prev_rev = int(overlay.get("revision", 0))
            new_rev = self._next_revision_number(overlay)

            entry["status"] = "active"
            entry.pop("demote_reason", None)
            entry["revision"] = new_rev
            self._save_entry(entry)

            combined = list(dict.fromkeys(list(overlay.get("active_entry_ids") or ()) + [safe]))
            kept, evicted = self._enforce_active_cap(
                combined,
                self.harness_cfg.max_active_adaptations,
                protected_ids={safe} if self.harness_cfg.max_active_adaptations > 0 else frozenset(),
            )

            overlay["parent_revision"] = prev_rev
            overlay["revision"] = new_rev
            overlay["state"] = "confirmed"
            overlay["probe_remaining"] = 0
            overlay["probe_outcomes"] = []
            overlay["active_entry_ids"] = kept
            overlay["surfaces"] = self._rebuild_surfaces(kept)
            self._save_overlay(overlay)
            _atomic_write_json(self.revisions_dir / f"rev-{new_rev:04d}.json", overlay)
            self.ledger.record_revision_promoted(new_rev, prev_rev, [safe], reason=reason)
            if evicted:
                self.ledger.record_demotion("cap", new_rev, evicted, reason="evicted_by_cap")
            metrics = self.ledger.refresh_metrics()
            self._rebuild_index()
            return {"revision": new_rev, "entry_id": safe, "evicted": evicted, "metrics": metrics}

    def demote_entry_or_revision(self, target: str, reason: str = "manual") -> dict:
        with self._locked():
            text = str(target or "").strip()
            if not text:
                raise Invalid("bad-demote-target", "demote target must not be empty")

            overlay = self._load_overlay()
            demoted_ids: list[str] = []

            rev_num: int | None = None
            if text.startswith("rev-") and text[4:].isdigit():
                rev_num = int(text[4:])
            elif text.isdigit():
                rev_num = int(text)

            if rev_num is not None:
                rev_path = self.revisions_dir / f"rev-{rev_num:04d}.json"
                rev_snap = read_json(rev_path)
                if rev_num == 0 and rev_snap is None:
                    raise Invalid("unknown-harness-revision", "revision 0 snapshot missing")
                if rev_snap is None:
                    raise Invalid("unknown-harness-revision", f"revision {text!r} does not exist")

                for e in self.list_entries():
                    if int(e.get("revision", -1)) == rev_num and e.get("status") not in ("demoted", "candidate"):
                        e["status"] = "demoted"
                        e["demote_reason"] = reason
                        self._save_entry(e)
                        demoted_ids.append(str(e["id"]))

                active_ids = [
                    eid for eid in (overlay.get("active_entry_ids") or ())
                    if eid not in demoted_ids
                ]
                if int(overlay.get("revision", 0)) == rev_num:
                    parent_rev = int(rev_snap.get("parent_revision", 0))
                    parent_snap = read_json(self.revisions_dir / f"rev-{parent_rev:04d}.json") or {}
                    parent_ids = [
                        eid for eid in (parent_snap.get("active_entry_ids") or ())
                        if eid not in demoted_ids
                    ]
                    overlay["revision"] = parent_rev
                    overlay["parent_revision"] = int(parent_snap.get("parent_revision", 0))
                    overlay["state"] = "confirmed"
                    overlay["probe_remaining"] = 0
                    overlay["probe_outcomes"] = []
                    overlay["active_entry_ids"] = parent_ids
                    overlay["surfaces"] = self._rebuild_surfaces(parent_ids)
                else:
                    overlay["active_entry_ids"] = active_ids
                    overlay["surfaces"] = self._rebuild_surfaces(active_ids)

                self._save_overlay(overlay)
                self.ledger.record_demotion(f"rev-{rev_num:04d}", rev_num, demoted_ids, reason=reason)
                metrics = self.ledger.refresh_metrics()
                self._rebuild_index()
                return {
                    "target": f"rev-{rev_num:04d}",
                    "demoted_entry_ids": demoted_ids,
                    "revision": int(overlay["revision"]),
                    "metrics": metrics,
                }

            safe = _safe_id(text, "entry_id")
            entry = self._load_entry(safe)
            if entry is None:
                raise Invalid("unknown-harness-entry", f"harness entry {safe!r} does not exist")

            entry["status"] = "demoted"
            entry["demote_reason"] = reason
            self._save_entry(entry)
            demoted_ids.append(safe)

            active_ids = [eid for eid in (overlay.get("active_entry_ids") or ()) if eid != safe]
            overlay["active_entry_ids"] = active_ids
            overlay["surfaces"] = self._rebuild_surfaces(active_ids)
            if overlay.get("state") == "probing" and not any(
                (self._load_entry(eid) or {}).get("status") == "probing" for eid in active_ids
            ):
                overlay["state"] = "confirmed"
                overlay["probe_remaining"] = 0
                overlay["probe_outcomes"] = []
            self._save_overlay(overlay)
            self.ledger.record_demotion(
                safe, int(overlay.get("revision", 0)), demoted_ids, reason=reason
            )
            metrics = self.ledger.refresh_metrics()
            self._rebuild_index()
            return {
                "target": safe,
                "demoted_entry_ids": demoted_ids,
                "revision": int(overlay["revision"]),
                "metrics": metrics,
            }

    # -- v0.7: the global library -------------------------------------------------

    def _drop_from_overlay(self, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        overlay = self._load_overlay()
        kept = [eid for eid in (overlay.get("active_entry_ids") or ()) if eid not in entry_ids]
        if len(kept) == len(overlay.get("active_entry_ids") or ()):
            return
        overlay["active_entry_ids"] = kept
        overlay["surfaces"] = self._rebuild_surfaces(kept)
        if overlay.get("state") == "probing" and not any(
            (self._load_entry(eid) or {}).get("status") == "probing" for eid in kept
        ):
            overlay["state"] = "confirmed"
            overlay["probe_remaining"] = 0
            overlay["probe_outcomes"] = []
        self._save_overlay(overlay)

    def sync_from_global(self, glib: GlobalLibrary) -> dict:
        """Import new global lessons as candidates and drop retired ones.

        An imported lesson is an ordinary entry with `origin = "global"`, so this
        project's probe cycle and scores judge it like its own. With `learn_from =
        "project"` nothing is imported, but retired lessons are still dropped.
        """
        imported: list[str] = []
        dropped: list[str] = []
        with self._locked():
            local = self.list_entries()
            by_gid = {str(e["global_id"]): e for e in local if e.get("global_id")}
            fingerprints = {self._cue_fingerprint(e) for e in local}
            ids = {str(e["id"]) for e in local}
            for g in glib.list_entries():
                gid = str(g.get("id", ""))
                mine = by_gid.get(gid)
                if g.get("status") == "retired":
                    if (mine and mine.get("origin") == "global"
                            and mine.get("status") in ("active", "probing", "candidate")):
                        mine["status"] = "demoted"
                        mine["demote_reason"] = "global_retired"
                        self._save_entry(mine)
                        dropped.append(str(mine["id"]))
                    continue
                if self.learn_from == "project" or mine is not None:
                    continue
                if g.get("surface") not in self.harness_cfg.editable_surfaces:
                    continue
                if self._cue_fingerprint(g) in fingerprints:
                    continue
                entry_id = f"exp-{gid}"
                if entry_id in ids:
                    continue
                cleaned, _ = validate_harness_proposal(
                    g, allowed_surfaces=self.harness_cfg.editable_surfaces,
                    never_touch=self.never_touch, forbidden_files=self.forbidden_files)
                if cleaned is None:
                    continue
                now = utc_now()
                sources = [str(s.get("project", "")) for s in g.get("sources") or ()]
                record = {**cleaned, "id": entry_id, "status": "candidate",
                          "source": "global", "scope": "general",
                          "scope_reason": "imported from the global library",
                          "origin": "global", "global_id": gid,
                          "source_project": sources[0] if sources else "",
                          "card_id": "", "revision": 0, "created_at": now,
                          "updated_at": now,
                          "stats": {"applied": 0, "helped": 0, "hurt": 0, "neutral": 0}}
                self._save_entry(record)
                ids.add(entry_id)
                imported.append(entry_id)
                self.ledger.record_lesson_added(
                    entry_id=entry_id, surface=record["surface"],
                    confidence=record["confidence"], revision=0, source="global",
                    card_id="", status="candidate")
            self._drop_from_overlay(dropped)
            promoted = None
            if imported:
                promoted = self._promote_buffered_candidates(
                    self._load_overlay(), self.ledger.compute_metrics())
            if imported or dropped:
                if dropped:
                    self.ledger.record_demotion("global", int(self._load_overlay().get(
                        "revision", 0)), dropped, reason="global_retired")
                self.ledger.refresh_metrics()
                self._rebuild_index()
            return {"imported": imported, "dropped": dropped, "promoted": promoted}

    def report_to_global(self, glib: GlobalLibrary, project: str, *, share: bool) -> dict:
        """Send this project's scores for linked lessons, veto losers, promote winners."""
        reported: list[str] = []
        vetoed: list[str] = []
        promoted: list[str] = []
        refused: dict[str, str] = {}
        with self._locked():
            for entry in self.list_entries():
                gid = str(entry.get("global_id") or "")
                stats = entry.get("stats") or {}
                if gid:
                    veto = bool(entry.get("demote_reason") == "global_veto")
                    if not veto and should_veto(stats):
                        veto = True
                        if (entry.get("origin") == "global"
                                and entry.get("status") in ("active", "probing", "candidate")):
                            entry["status"] = "demoted"
                            entry["demote_reason"] = "global_veto"
                            self._save_entry(entry)
                            vetoed.append(str(entry["id"]))
                    if glib.report(gid, project, stats, vetoed=veto) is not None:
                        reported.append(gid)
                    continue
                if not share or entry.get("origin") == "global":
                    continue
                if not should_promote(entry):
                    continue
                ok, why = global_eligibility(entry)
                if not ok:
                    if entry.get("global_refused") != why:
                        entry["global_refused"] = why
                        self._save_entry(entry)
                    refused[str(entry["id"])] = why
                    continue
                g = glib.add(entry, project)
                entry["global_id"] = g["id"]
                entry.pop("global_refused", None)
                self._save_entry(entry)
                glib.report(g["id"], project, stats, vetoed=False)
                promoted.append(str(entry["id"]))
            if vetoed:
                self._drop_from_overlay(vetoed)
                self.ledger.record_demotion("global", int(self._load_overlay().get(
                    "revision", 0)), vetoed, reason="global_veto")
                self.ledger.refresh_metrics()
                self._rebuild_index()
            return {"reported": reported, "vetoed": vetoed, "promoted": promoted,
                    "refused": refused}

    def force_to_global(self, glib: GlobalLibrary, entry_id: str, project: str,
                        markers: set[str] | None) -> dict:
        """The owner sends one of this project's lessons to the global library now.

        It skips the proof (`should_promote`) and the lesson's own scope claim, never
        the safety checks: the project-name check runs again (no markers refuses),
        then `global_eligibility` as for any promotion.
        """
        with self._locked():
            entry = self._load_entry(entry_id)
            if not entry:
                raise Invalid("unknown-entry", f"lesson {entry_id!r} does not exist")
            if entry.get("origin") == "global":
                raise Refused("already-global", f"{entry_id} came from the global library")
            if markers is None:
                raise Refused("global-refused",
                              f"{entry_id}: this project's names could not be listed, "
                              f"so it cannot be checked")
            scope, why = scope_of(entry, markers)
            if scope != "general":
                raise Refused("global-refused", f"{entry_id}: {why}")
            ok, why = global_eligibility({**entry, "scope": "general"})
            if not ok:
                raise Refused("global-refused", f"{entry_id}: {why}")
            g = glib.add(entry, project)
            entry["global_id"] = g["id"]
            entry.pop("global_refused", None)
            self._save_entry(entry)
            glib.report(g["id"], project, entry.get("stats") or {}, vetoed=False)
            return {"entry_id": entry_id, "global_id": g["id"], "forced": True}

    def global_id_of(self, entry_id: str) -> str:
        """The global lesson linked to one of this project's lessons, or ""."""
        entry = self._load_entry(entry_id)
        if not entry:
            raise Invalid("unknown-entry", f"lesson {entry_id!r} does not exist")
        return str(entry.get("global_id") or "")

    def reset(self) -> dict:
        """Clear active overlay back to revision 0 and deactivate active/probing/candidate entries."""
        with self._locked():
            demoted_ids: list[str] = []
            for e in self.list_entries():
                if e.get("status") in ("active", "probing", "candidate"):
                    e["status"] = "demoted"
                    e["demote_reason"] = "reset"
                    self._save_entry(e)
                    demoted_ids.append(str(e["id"]))

            overlay = self._load_overlay()
            next_rev_counter = int(overlay.get("next_revision", 1))
            reset_overlay = self._empty_overlay(revision=0, parent_revision=0)
            reset_overlay["next_revision"] = next_rev_counter
            self._save_overlay(reset_overlay)
            if not (self.revisions_dir / "rev-0000.json").exists():
                _atomic_write_json(self.revisions_dir / "rev-0000.json", reset_overlay)
            self.ledger.append_event("reset", {"demoted_entry_ids": demoted_ids})
            metrics = self.ledger.refresh_metrics()
            self._rebuild_index()
            return {"revision": 0, "demoted_entry_ids": demoted_ids, "metrics": metrics}
