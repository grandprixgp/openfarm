"""The card model, its canonical JSON, and its hash.

A card is whatever the card source's `read` op returned. The harness hashes it at
claim so a card edited mid-run can be spotted (`card-changed`).
"""

from __future__ import annotations

import hashlib
import json


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
        if value is None or value == "":
            return None
        return float(value)

    @property
    def new_symbols(self) -> bool:
        return bool(self.fields.get("new_symbols", False))

    @property
    def unit(self) -> str:
        """The one crate, package or view folder the card works in."""
        return str(self.fields.get("unit", "") or "")

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
