"""The card model, its canonical JSON, and its hash.

A card is whatever the card source's `read` op returned. The harness hashes it at
claim so a card edited mid-run can be spotted (`card-changed`).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib

from .errors import Invalid


def canonical_json(value: object) -> str:
    """One stable spelling of a value, so its hash is stable too."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_value(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class TestRef:
    """One named test the card requires."""

    __slots__ = ("name", "file", "asserts")

    def __init__(self, name: str, file: str = "", asserts: str = "") -> None:
        self.name = name
        self.file = file
        self.asserts = asserts

    def as_dict(self) -> dict:
        return {"name": self.name, "file": self.file, "asserts": self.asserts}

    @classmethod
    def from_dict(cls, data: dict) -> "TestRef":
        return cls(
            name=str(data.get("name", "")),
            file=str(data.get("file", "")),
            asserts=str(data.get("asserts", "")),
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TestRef) and self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return f"TestRef({self.name!r})"


#: Field names the spec requires a card source to report under `fields`.
CARD_FIELDS = (
    "category",
    "reference",
    "tests",
    "test_edits",
    "frontier_estimate",
    "new_symbols",
)

#: Optional field names a card source may also report under `fields`.
OPTIONAL_CARD_FIELDS = (
    "unit",
    "context_files",
    "type_changes",
    "replan_if",
    "complexity",
    "profile",
    "network",
    "taken_off",
    "wave",
    "from_card",
    "created_at",
    "finding_decision",
    "finding_reason",
    "folded_into",
    "finding_decided_at",
    "promoted_card",
)

_OPENFARM_BLOCK_RE = re.compile(
    r"^[ \t]*```openfarm[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*(?:\r?\n|$)",
    re.MULTILINE | re.DOTALL,
)
_TOML_BLOCK_RE = re.compile(
    r"^[ \t]*```toml[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*(?:\r?\n|$)",
    re.MULTILINE | re.DOTALL,
)


def extract_fenced_fields(done_text: str, block_kind: str = "toml") -> tuple[dict, str]:
    """Parse a fenced ```openfarm or ```toml metadata block from card body text."""
    if block_kind != "toml" or not done_text:
        return {}, done_text
    match = _OPENFARM_BLOCK_RE.search(done_text) or _TOML_BLOCK_RE.search(done_text)
    if not match:
        return {}, done_text
    try:
        parsed = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise Invalid("bad-fields-block",
                      f"fenced fields block is not valid TOML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Invalid("bad-fields-block", "fenced fields block must be a TOML table")
    cleaned = re.sub(
        r"\n{3,}", "\n\n", done_text[:match.start()] + done_text[match.end():]
    ).strip()
    return parsed, cleaned


class Card:
    """A card as read from the card source at claim."""

    def __init__(
        self,
        id: str,
        title: str = "",
        state: str = "ready",
        done_text: str = "",
        touches: list[str] | None = None,
        deps: list[str] | None = None,
        fields: dict | None = None,
        raw: dict | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.state = state
        self.done_text = done_text
        self.touches = list(touches or [])
        self.deps = list(deps or [])
        self.fields = dict(fields or {})
        #: The exact payload the card source returned, hashed at claim.
        self.raw = raw if raw is not None else self.as_dict()

    # -- fields with defaults -------------------------------------------------

    @property
    def category(self) -> str:
        return str(self.fields.get("category", ""))

    @property
    def reference(self) -> str:
        return str(self.fields.get("reference", "") or "")

    @property
    def tests(self) -> list[TestRef]:
        out = []
        for entry in self.fields.get("tests", []) or []:
            if isinstance(entry, dict):
                out.append(TestRef.from_dict(entry))
            else:
                out.append(TestRef(name=str(entry)))
        return out

    @property
    def test_names(self) -> list[str]:
        return [t.name for t in self.tests]

    @property
    def test_edits(self) -> list[str]:
        return [str(p) for p in self.fields.get("test_edits", []) or []]

    @property
    def frontier_estimate(self) -> float | None:
        value = self.fields.get("frontier_estimate")
        if value is None or value == "" or isinstance(value, bool):
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return num if math.isfinite(num) else None

    @property
    def frontier_estimate_valid(self) -> bool:
        value = self.fields.get("frontier_estimate")
        if value is None or value == "":
            return True
        if isinstance(value, bool):
            return False
        try:
            num = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(num)

    @property
    def network(self) -> bool:
        """The plan says this task itself must use the network (a server, a client of a remote API)."""
        return self.fields.get("network") is True

    @property
    def new_symbols(self) -> bool:
        return bool(self.fields.get("new_symbols", False))

    @property
    def unit(self) -> str:
        """The one crate, package or view folder the card works in."""
        return str(self.fields.get("unit", "") or "")

    @property
    def context_files(self) -> list[str]:
        return [str(p) for p in self.fields.get("context_files", []) or []]

    @property
    def type_changes(self) -> bool:
        return bool(self.fields.get("type_changes", False))

    @property
    def replan_if(self) -> list[str]:
        raw = self.fields.get("replan_if")
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]

    @property
    def replan_if_valid(self) -> bool:
        raw = self.fields.get("replan_if")
        if raw is None:
            return True
        return isinstance(raw, list) and all(isinstance(item, str) for item in raw)

    # -- serialisation --------------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "done_text": self.done_text,
            "touches": list(self.touches),
            "deps": list(self.deps),
            "fields": dict(self.fields),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Card":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            state=str(data.get("state", "ready")),
            done_text=str(data.get("done_text", "")),
            touches=[str(t) for t in data.get("touches", []) or []],
            deps=[str(d) for d in data.get("deps", []) or []],
            fields=dict(data.get("fields", {}) or {}),
            raw=data,
        )

    def hash(self) -> str:
        """The hash recorded at claim, from the payload as read."""
        return hash_value(self.raw)

    def slug(self) -> str:
        """A short words-only form for the branch name."""
        words = []
        current = []
        for ch in self.title.lower():
            if ch.isalnum():
                current.append(ch)
            elif current:
                words.append("".join(current))
                current = []
        if current:
            words.append("".join(current))
        slug = "-".join(words[:4])
        return slug or "card"

    def __repr__(self) -> str:
        return f"Card({self.id!r}, {self.category!r})"


def waiting_on(cards) -> dict[str, list[tuple[str, str]]]:
    """Ready cards that can't start yet: for each, the cards it waits for and their state.

    A card is claimed only once every card it depends on is done, so a ready card
    behind a blocked or unfinished one is waiting, whatever its own state says.
    """
    by_id = {card.id: card for card in cards}
    out: dict[str, list[tuple[str, str]]] = {}
    for card in cards:
        if card.state != "ready":
            continue
        unmet = [(dep, by_id[dep].state if dep in by_id else "missing")
                 for dep in card.deps if dep not in by_id or by_id[dep].state != "done"]
        if unmet:
            out[card.id] = unmet
    return out


def waiting_text(waiting: dict[str, list[tuple[str, str]]]) -> str:
    """`W1-03 waits for W1-01 (blocked); W1-04 waits for W1-03 (ready)`."""
    return "; ".join(
        f"{cid} waits for " + ", ".join(f"{dep} ({state.replace('_', ' ')})" for dep, state in unmet)
        for cid, unmet in waiting.items())
