"""The generic card source: card files in a folder, state on its own branch.

A card file is Markdown with a TOML header between `+++` lines, so a person can
read it and a machine can read it too:

    +++
    id = "CARD-1"
    title = "Parse the fascicle split"
    state = "ready"
    touches = ["crates/parse/src/fascicle.rs"]
    depends = []
    category = "pattern"
    reference = "crates/parse/src/spine.rs"
    +++

    ## Done

    ...

State lives on a branch of its own (`openfarm-cards` by default), never on a work
branch, so concurrent cards can't conflict and every lane sees the same claims.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

from ..cards import Card, hash_value
from ..errors import Infra, Invalid, Refused, Unsupported
from ..git import Git
from ..state import utc_after, utc_now

HEADER_RE = re.compile(r"^\+\+\+\s*\n(.*?)\n\+\+\+\s*\n?", re.DOTALL)

#: States a card can be in.
STATES = ("backlog", "ready", "in_progress", "in_review", "blocked", "done", "dropped")
READY = "ready"
DONE_STATES = ("done",)


def parse_card_file(text: str) -> tuple[dict, str]:
    """The TOML header and the Markdown body of a card file."""
    match = HEADER_RE.match(text)
    if not match:
        raise Invalid("no-header", "a card file needs a +++ TOML header")
    try:
        header = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise Invalid("bad-header", f"card header is not TOML: {exc}") from exc
    if not isinstance(header, dict):
        raise Invalid("bad-header", "card header must be a table")
    return header, text[match.end():]


def render_card_file(header: dict, body: str) -> str:
    lines = ["+++"]
    for key in sorted(header):
        value = header[key]
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("+++")
    lines.append("")
    return "\n".join(lines) + body.lstrip("\n")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_value(item)}"
                                for key, item in sorted(value.items())) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value)
    # TOML basic strings have no `\n` escape, so text with newlines (a note, a done
    # text) goes in a multi-line literal string instead.
    if "\n" in text:
        return "'''\n" + text.replace("'''", "''\\'") + "'''"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FileCardSource:
    """Card source over a folder on a dedicated cards branch."""

    name = "files"

    def __init__(self, repo: Path, folder: str, branch: str = "openfarm-cards",
                 worktree: Path | None = None, base_branch: str = "main") -> None:
        self.repo = Path(repo)
        self.folder = folder.strip("/")
        self.branch = branch
        self.base_branch = base_branch
        self.worktree = Path(worktree) if worktree else None
        self.git = Git(self.repo)

    # -- setup ---------------------------------------------------------------

    def hello(self) -> dict:
        return {"name": self.name,
                "versions": [1],
                "ops": ["hello", "next", "read", "hash", "claim", "heartbeat",
                        "move", "release", "finding", "stale"]}

    def prepare(self) -> Path:
        """Check the cards branch out in its own worktree, creating it if needed.

        When the branch is new, any card files already on the base branch are copied
        in, so a repository can commit its cards beside its code and let the farm's
        branch take over from there.
        """
        if self.worktree is None:
            raise Invalid("no-worktree", "the file card source needs a worktree path")
        created = False
        if not self.git.branch_exists(self.branch):
            start = (self.base_branch if self.git.ref_exists(
                f"refs/heads/{self.base_branch}") else "HEAD")
            self.git.create_branch(self.branch, start)
            created = True
        if not self.worktree.exists():
            self.git.worktree_add(self.worktree, self.branch)
        else:
            checker = Git(self.worktree)
            if checker.current_branch() != self.branch:
                # A worktree on the wrong branch may be holding an old head of the
                # cards branch, and git will not move a branch a worktree has out.
                self.git.worktree_remove(self.worktree)
                self.git.worktree_add(self.worktree, self.branch)
            else:
                # The cards branch may have moved since this worktree was made,
                # because another lane or a person edits cards. A stale worktree
                # would show a card as missing, so it is fast-forwarded first.
                head = checker.head()
                target = self.git.head(f"refs/heads/{self.branch}")
                if head != target:
                    checker.reset_hard(target)

        (self.worktree / self.folder).mkdir(parents=True, exist_ok=True)
        if created:
            self._seed_from_base()
        return self.worktree

    def _seed_from_base(self) -> None:
        """Copy card files the base branch already holds onto the cards branch."""
        if self.worktree is None:
            return
        source = self.repo / self.folder
        if not source.is_dir():
            return
        copied = 0
        for path in sorted(source.glob("*.md")):
            target = self.worktree / self.folder / path.name
            if target.exists():
                continue
            shutil.copy2(path, target)
            copied += 1
        if copied:
            Git(self.worktree).commit_all(
                f"openfarm: seed {copied} card(s) from the base branch")

    @property
    def cards_dir(self) -> Path:
        if self.worktree is None:
            raise Invalid("no-worktree", "the file card source needs a worktree path")
        return self.worktree / self.folder

    # -- reading -------------------------------------------------------------

    def _path(self, card_id: str) -> Path:
        return self.cards_dir / f"{card_id}.md"

    def _read_raw(self, card_id: str) -> tuple[dict, str, Path]:
        path = self._path(card_id)
        if not path.exists():
            raise Refused("not-found", f"no card {card_id} in {self.cards_dir}")
        header, body = parse_card_file(path.read_text())
        if not header.get("id"):
            header["id"] = path.stem
        return header, body, path

    def _card_from(self, header: dict, body: str, path: Path) -> Card:
        fields = {k: header[k] for k in
                  ("category", "reference", "tests", "test_edits",
                   "frontier_estimate", "new_symbols", "unit") if k in header}
        return Card(
            id=str(header.get("id", path.stem)),
            title=str(header.get("title", "")),
            state=str(header.get("state", "backlog")),
            done_text=body.strip() or str(header.get("done", "")),
            touches=[str(t) for t in header.get("touches", []) or []],
            deps=[str(d) for d in header.get("depends", header.get("deps", [])) or []],
            fields=fields,
            raw={"id": str(header.get("id", path.stem)),
                 "title": str(header.get("title", "")),
                 "state": str(header.get("state", "backlog")),
                 "done_text": body.strip(),
                 "touches": [str(t) for t in header.get("touches", []) or []],
                 "deps": [str(d) for d in header.get("depends", []) or []],
                 "fields": fields},
        )

    def read(self, card_id: str) -> dict:
        header, body, path = self._read_raw(card_id)
        return {"card": self._card_from(header, body, path).as_dict()}

    def hash(self, card_id: str) -> dict:
        header, body, path = self._read_raw(card_id)
        return {"hash": self._card_from(header, body, path).hash()}

    def all_cards(self) -> list[Card]:
        if not self.cards_dir.exists():
            return []
        out = []
        for path in sorted(self.cards_dir.glob("*.md")):
            try:
                header, body = parse_card_file(path.read_text())
            except Invalid:
                continue
            if not header.get("id"):
                header["id"] = path.stem
            out.append(self._card_from(header, body, path))
        return out

    def next(self, lane: str = "", limit: int = 10) -> dict:
        ready: list[str] = []
        by_id = {card.id: card for card in self.all_cards()}
        done_ids = {cid for cid, card in by_id.items()
                    if card.state in DONE_STATES}
        for card_id, card in by_id.items():
            if card.state != READY or _holder(card.raw):
                continue
            if lane and _lane(card.raw) and _lane(card.raw) != lane:
                continue
            if any(dep not in done_ids for dep in card.deps):
                continue
            ready.append(card_id)
        return {"cards": ready[:limit]}

    def stale(self, lane: str = "") -> dict:
        now = utc_now()
        out = []
        for card in self.all_cards():
            holder = _holder(card.raw)
            expires = _lease_expiry(card.raw)
            if holder and expires and expires < now:
                out.append({"id": card.id, "holder": holder,
                            "expires_at": expires})
        return {"cards": out}

    # -- writing -------------------------------------------------------------

    def _write(self, card_id: str, header: dict, body: str, message: str) -> str:
        path = self._path(card_id)
        path.write_text(render_card_file(header, body))
        git = Git(self.worktree)
        git.commit_all(message)
        return git.head()

    def claim(self, card_id: str, holder: str,
              lease_ttl_s: float = 1200) -> dict:
        header, body, _ = self._read_raw(card_id)
        current = _holder(header)
        if current and current != holder:
            expires = _lease_expiry(header)
            if not expires or expires > utc_now():
                raise Refused("already-claimed",
                              f"{card_id} is held by {current}")
        if header.get("state") != READY:
            raise Refused("not-ready", f"{card_id} is {header.get('state')}")
        for dep in header.get("depends", []) or []:
            dep_header, _, _ = self._read_raw(str(dep))
            if dep_header.get("state") not in DONE_STATES:
                raise Refused("not-ready", f"{card_id} waits on {dep}")

        lease_id = f"L-{hash_value((card_id, holder, utc_now()))[:10]}"
        header["state"] = "in_progress"
        header["holder"] = holder
        header["lease_id"] = lease_id
        header["lease_expires_at"] = utc_after(lease_ttl_s)
        header["claimed_at"] = utc_now()
        self._write(card_id, header, body,
                    f"{card_id}: claimed by {holder}")
        return {"lease_id": lease_id, "expires_at": header["lease_expires_at"]}

    def heartbeat(self, card_id: str, lease_id: str,
                  lease_ttl_s: float = 1200) -> dict:
        header, body, _ = self._read_raw(card_id)
        if header.get("lease_id") != lease_id:
            raise Refused("stale-lease", f"{card_id} is not held on {lease_id}")
        header["lease_expires_at"] = utc_after(lease_ttl_s)
        self._write(card_id, header, body, f"{card_id}: heartbeat")
        return {"expires_at": header["lease_expires_at"]}

    def move(self, card_id: str, lease_id: str, to: str, note_md: str = "",
             stop_code: str = "") -> dict:
        if to not in ("in-review", "blocked"):
            raise Refused("bad-state",
                          f"the farm moves a card to in-review or blocked, not {to}")
        header, body, _ = self._read_raw(card_id)
        if lease_id and header.get("lease_id") != lease_id:
            raise Refused("stale-lease", f"{card_id} is not held on {lease_id}")
        header["state"] = "in_review" if to == "in-review" else "blocked"
        header["holder"] = ""
        header["lease_id"] = ""
        header["lease_expires_at"] = ""
        header["moved_at"] = utc_now()
        if stop_code:
            header["stop_code"] = stop_code
        if note_md:
            header["note"] = note_md
        self._write(card_id, header, body,
                    f"{card_id}: {header['state']}"
                    + (f" ({stop_code})" if stop_code else ""))
        return {"state": header["state"]}

    def release(self, card_id: str, lease_id: str = "", reason: str = "",
                stop_code: str = "") -> dict:
        header, body, _ = self._read_raw(card_id)
        if lease_id and header.get("lease_id") not in ("", lease_id):
            raise Refused("stale-lease", f"{card_id} is not held on {lease_id}")
        header["state"] = READY
        header["holder"] = ""
        header["lease_id"] = ""
        header["lease_expires_at"] = ""
        header["released_at"] = utc_now()
        if stop_code:
            header["stop_code"] = stop_code
        if reason:
            header["release_reason"] = reason
        self._write(card_id, header, body,
                    f"{card_id}: released" + (f" ({reason})" if reason else ""))
        return {"state": READY}

    def finding(self, from_card: str, title: str, body_md: str = "") -> dict:
        existing = {card.id for card in self.all_cards()}
        index = 1
        while f"FIND-{index}" in existing:
            index += 1
        card_id = f"FIND-{index}"
        header = {
            "id": card_id,
            "title": title,
            "state": "backlog",
            "touches": [],
            "depends": [],
            "from_card": from_card,
            "created_at": utc_now(),
        }
        self._write(card_id, header, body_md, f"{card_id}: finding from {from_card}")
        return {"id": card_id}

    # -- test helpers --------------------------------------------------------

    def add_card(self, card: Card, commit: bool = True) -> Path:
        """Write a card straight in, for fixtures and the toy repository."""
        path = self._path(card.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "id": card.id,
            "title": card.title,
            "state": card.state,
            "touches": list(card.touches),
            "depends": list(card.deps),
        }
        for key in ("category", "reference", "test_edits", "frontier_estimate",
                    "new_symbols", "unit"):
            if key in card.fields:
                header[key] = card.fields[key]
        if "tests" in card.fields:
            header["tests"] = [
                {"name": t.name, "file": t.file, "asserts": t.asserts}
                for t in card.tests]
        path.write_text(render_card_file(header, card.done_text))
        if commit:
            Git(self.worktree).commit_all(f"{card.id}: add")
        return path

    def delete_card(self, card_id: str) -> None:
        path = self._path(card_id)
        if path.exists():
            path.unlink()
            Git(self.worktree).commit_all(f"{card_id}: remove")


def _holder(raw: dict) -> str:
    return str(raw.get("holder", "") or "")


def _lane(raw: dict) -> str:
    return str(raw.get("lane", "") or "")


def _lease_expiry(raw: dict) -> str:
    return str(raw.get("lease_expires_at", "") or "")


def open_file_source(cfg, state_root: Path) -> FileCardSource:
    """Build the file card source from config."""
    spec = cfg.cards_adapter
    if not spec.startswith("files:"):
        raise Infra("bad-adapter", f"{spec} is not a files: card source")
    folder = spec[len("files:"):].strip() or "cards"
    folder = folder.rstrip("/")
    worktree = Path(state_root) / "cards-source"
    return FileCardSource(
        repo=cfg.repo,
        folder=folder,
        branch=getattr(cfg, "cards_branch", "openfarm-cards"),
        worktree=worktree,
        base_branch=cfg.base_branch,
    )
