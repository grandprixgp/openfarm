"""One home for a project's cards and planning documents: the cards branch.

Cards used to live twice, on the base branch's `cards/` folder and on the
`openfarm-cards` branch, and an edit to one copy could miss the other. Here the
cards branch is the only home. The spec, the goals and the decisions log sit
beside the cards in `project/`, and each card has a history in `history/`.

Every change a person or the Architect makes to a card goes through
`Store.change_card`, which writes the card, records who, when, what and why, and
runs the plan checks on the result.

A project made before this still has `cards/` on its base branch. Until
`Store.migrate` runs, that folder seeds cards the branch does not have yet and
the documents stay in `.openfarm/`. After it, nothing reads either place.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import shutil
from pathlib import Path

from .adapters.files import FileCardSource, open_file_source, parse_card_file, render_card_file
from .cards import Card
from .errors import Invalid, Refused
from .git import Git
from .state import utc_now

#: Document name -> (file on the cards branch, file a project kept before migration).
DOCS = {
    "spec": ("project/PROJECT_SPEC.md", ".openfarm/PROJECT_SPEC.md"),
    "chunks": ("project/CHUNKS.json", ".openfarm/CHUNKS.json"),
    "decisions": ("project/DECISIONS.md", ".openfarm/GRILL_DECISIONS.md"),
}

#: The card fields a person writes. The rest (state, holder, lease, timestamps) is the farm's.
CONTENT_FIELDS = ("title", "category", "touches", "depends", "tests", "wave", "reference", "new_symbols", "network", "test_edits")

#: What each kind of change may start from. `create` has no card yet.
_EDITABLE = ("ready", "backlog", "blocked")
_REFUSALS = {
    "in_progress": "is building; stop it first",
    "in_review": "is waiting for review; decide it first",
    "done": "is done; its text can't change now",
    "dropped": "was dropped; its text can't change now",
    "split": "was split; its text can't change now",
}


def person(root: Path) -> str:
    """Who is making a change: `person/<git user.name or login>`."""
    try:
        name = Git(root)._run(["config", "user.name"], check=False).stdout.strip()
    except Exception:  # noqa: BLE001 - a folder that is not a repository still has a login
        name = ""
    return f"person/{name or getpass.getuser()}"


def card_from(header: dict, body: str) -> Card:
    return Card(
        id=str(header.get("id") or ""),
        title=str(header.get("title") or header.get("id") or ""),
        state=str(header.get("state") or "ready"),
        done_text=body,
        touches=[str(t) for t in (header.get("touches") or [])],
        deps=[str(d) for d in (header.get("depends") or header.get("deps") or [])],
        fields=dict(header),
    )


def _content(header: dict, body: str) -> dict:
    out = {key: header.get(key) for key in CONTENT_FIELDS if header.get(key) not in (None, "", [])}
    if "depends" not in out and header.get("deps"):
        out["depends"] = header["deps"]
    out["done_text"] = body.strip()
    return out


def doc_path(root: Path, cfg, name: str) -> Path:
    """Where a planning document is right now, without opening the store.

    On the cards branch once the project is migrated; in `.openfarm/` before that,
    and for a folder with no project yet.
    """
    store_rel, legacy_rel = DOCS[name]
    root = Path(root)
    if cfg is not None:
        worktree = Path(cfg.state_dir) / "cards-source"
        try:
            migrated = bool(json.loads((worktree / "store.json").read_text(encoding="utf-8")).get("migrated"))
        except (OSError, ValueError, AttributeError):
            migrated = False
        if migrated:
            return worktree / store_rel
    return root / legacy_rel


def read_doc(root: Path, cfg, name: str) -> str:
    path = doc_path(root, cfg, name)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


class Store:
    """The project's cards and planning documents on the cards branch."""

    def __init__(self, root: Path, cfg) -> None:
        self.root = Path(root).expanduser().resolve()
        self.cfg = cfg
        self.src: FileCardSource = open_file_source(cfg, cfg.state_dir)
        with self.src._lock:
            self.src.prepare()
            if not self.src.migrated and not self._base_card_files() and not self._tracked_legacy_docs():
                # A new project: nothing on the base branch to take over, so the branch is home at once.
                self._finish_migration(commit_base=False)

    # -- documents -------------------------------------------------------------

    def doc_path(self, name: str) -> Path:
        store_rel, legacy_rel = DOCS[name]
        return (self.src.worktree / store_rel) if self.src.migrated else (self.root / legacy_rel)

    def read_doc(self, name: str) -> str:
        with self.src._lock:
            try:
                return self.doc_path(name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

    def write_doc(self, name: str, text: str, why: str = "") -> Path:
        with self.src._lock:
            path = self.doc_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if self.src.migrated:
                Git(self.src.worktree).commit_all(f"{Path(DOCS[name][0]).name}: {why or 'updated'}")
            return path

    def log_decision(self, kind: str, text: str, *, question: str = "") -> bool:
        """Append one dated entry to the decisions log. Nothing in the log is ever replaced.

        `kind` is `idea`, `answer`, `iteration` or `feature`. An entry whose text is
        already in the log is not written twice. Returns whether it was added.
        """
        with self.src._lock:
            old = self.read_doc("decisions")
            new = decision_entry(old, kind, text, question)
            if new == old:
                return False
            self.write_doc("decisions", new, why=f"{kind} added")
            return True

    # -- cards -----------------------------------------------------------------

    def cards(self) -> list[Card]:
        return self.src.all_cards()

    def states(self) -> dict[str, str]:
        """`{CARD-ID upper: state}` for every card on record."""
        return {card.id.upper(): str(card.state).lower() for card in self.cards()}

    def read(self, card_id: str) -> tuple[dict, str]:
        with self.src._lock:
            header, body, _path = self.src._read_raw(card_id)
            return header, body

    def history(self, card_id: str) -> list[dict]:
        with self.src._lock:
            path = self.src.worktree / "history" / f"{card_id}.jsonl"
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
            out = []
            for line in lines:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out

    def _note(self, card_id: str, kind: str, by: str, why: str, changed: dict) -> None:
        path = self.src.worktree / "history" / f"{card_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": utc_now(), "by": by, "kind": kind, "why": why, "changed": changed}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def change_card(self, card_id: str, kind: str, *, by: str = "", why: str = "",
                    apply=None, header: dict | None = None, body: str = "") -> dict:
        """The one way a card is created or changed by a person or the Architect.

        `kind` is one of create, edit, split, release, retry, accept, skip, drop.
        `create` takes `header` and `body`; `edit` takes `apply(header, body) ->
        (header, body)`. The change and its reason are added to the card's history in
        the same commit, and the plan checks run on the result.

        Returns `{"id", "state", "checks": [...], "ok": bool}`.
        """
        from .preflight import check_card

        by = by or person(self.root)
        with self.src._lock:
            if kind == "create":
                assert header is not None
                card_id = str(header.get("id") or card_id)
                if card_id.upper() in self.states():
                    raise Refused("card-exists", f"{card_id} already exists")
                header = dict(header, id=card_id)
                header.setdefault("state", "ready")
                header.setdefault("created_at", utc_now())
                header, checks = self._park_or_ready(header, body)
                msg = f"{card_id}: created by {by}"
                self.src._write(card_id, header, body, msg, commit=False)
                self._note(card_id, kind, by, why, _content(header, body))
                Git(self.src.worktree).commit_all(msg)
                return self._outcome(card_id, header, checks)
            else:
                old_header, old_body = self.read(card_id)
                state = str(old_header.get("state") or "ready")
                before = _content(old_header, old_body)
                if kind in ("edit", "split", "skip", "drop"):
                    from .adapters.files import _holder, _lease_expiry

                    held = bool(_holder(old_header)) and _lease_expiry(old_header) > utc_now()
                    if state in _REFUSALS or held:
                        raise Refused("card-not-editable",
                                      f"{card_id} {_REFUSALS.get(state, _REFUSALS['in_progress'])}")
                if kind == "edit":
                    header, body = apply(dict(old_header), old_body)
                    header, checks = self._park_or_ready(header, body)
                    after = _content(header, body)
                    changed = {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
                               if before.get(k) != after.get(k)}
                    if changed:
                        header["edited_at"] = utc_now()  # lets a reader tell "changed since its last run" from "the same task"
                    msg = f"{card_id}: edited by {by}"
                    self.src._write(card_id, header, body, msg, commit=False)
                    self._note(card_id, kind, by, why, changed)
                    Git(self.src.worktree).commit_all(msg)
                    return self._outcome(card_id, header, checks)
                elif kind in ("skip", "drop", "split"):
                    new_state = "split" if kind == "split" else "dropped"
                    header, body = dict(old_header), old_body
                    header.update(state=new_state, dropped_at=utc_now(), drop_reason=why or kind,
                                  holder="", lease_id="", lease_expires_at="")
                    msg = f"{card_id}: {new_state} ({why or kind})"
                    self.src._write(card_id, header, body, msg, commit=False)
                    self._note(card_id, kind, by, why, {"state": [state, new_state]})
                    Git(self.src.worktree).commit_all(msg)
                elif kind in ("release", "retry"):
                    self.src.release(card_id, reason=why)
                    header, body = self.read(card_id)
                    parked, _checks = self._park_or_ready(header, body)
                    if parked.get("state") != header.get("state"):  # still can't be built: don't queue it
                        header = parked
                        msg = f"{card_id}: parked, it still fails the plan checks"
                        self.src._write(card_id, header, body, msg, commit=False)
                        self._note(card_id, kind, by, why, {"state": [state, "ready"]})
                        Git(self.src.worktree).commit_all(msg)
                    else:
                        self._note(card_id, kind, by, why, {"state": [state, "ready"]})
                        Git(self.src.worktree).commit_all(f"{card_id}: {kind} history")
                elif kind == "accept":
                    if not why.strip():
                        raise Refused("note-required", "accepting a blocked card needs a note saying what was checked")
                    self.src.accept_blocked(card_id, by=by, note=why)
                    header, body = self.read(card_id)
                    self._note(card_id, kind, by, why, {"state": [state, "in_review"]})
                    Git(self.src.worktree).commit_all(f"{card_id}: accept history")
                else:
                    raise Invalid("bad-change", f"unknown card change {kind!r}")
            checks = check_card(card_from(header, body), self.cfg, self.root, self.cards())
        return self._outcome(card_id, header, checks)

    @staticmethod
    def _outcome(card_id: str, header: dict, checks: list[dict]) -> dict:
        return {"id": card_id, "state": str(header.get("state") or ""), "checks": checks,
                "ok": not any(c["status"] == "fail" for c in checks)}

    def _park_or_ready(self, header: dict, body: str) -> tuple[dict, list[dict]]:
        """Run the plan checks on a card about to be written.

        A ready card that fails goes to `backlog` with the reasons in `preflight`, so
        the farm never claims a card it can't build. A card parked that way comes back
        to `ready` once it passes. Any other state is left alone.
        """
        from .preflight import check_card

        checks = check_card(card_from(header, body), self.cfg, self.root, self.cards())
        failed = [f"{c['check']}: {c['detail']}" for c in checks if c["status"] == "fail"]
        state = str(header.get("state") or "ready")
        header = dict(header)
        if failed and state in ("ready", "backlog"):
            header.update(state="backlog", preflight=failed)
        elif not failed and header.pop("preflight", None) and state == "backlog":
            header["state"] = "ready"
        return header, checks

    def next_card_id(self, wave: int) -> str:
        return self.src.next_id(f"W{int(wave)}-", 2)

    # -- taking over a project that kept cards on its base branch --------------------

    def _base_card_files(self) -> list[Path]:
        folder = self.src.legacy_base_cards()
        return sorted(p for p in folder.glob("*.md") if p.is_file() and not p.is_symlink()) if folder else []

    def _tracked_legacy_docs(self) -> list[str]:
        rels = [legacy for _store, legacy in DOCS.values() if (self.root / legacy).is_file()]
        if not rels or not (self.root / ".git").exists():
            return []
        out = Git(self.root)._run(["ls-files", "--", *rels], check=False).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    def migration(self) -> dict:
        """What `migrate` would do: `{"needed", "cards", "differences": [{"id", "main", "state"}], "unparseable": [...]}`.

        A difference is a card whose text on the base branch is not the text on the
        cards branch. State, leases and timestamps are the farm's and are not compared.
        """
        with self.src._lock:
            if self.src.migrated:
                return {"needed": False, "cards": 0, "differences": [], "unparseable": []}
            differences = []
            unparseable = []
            files = self._base_card_files()
            for path in files:
                try:
                    b_header, b_body = parse_card_file(path.read_text(encoding="utf-8", errors="replace"))
                except (Invalid, OSError) as exc:
                    unparseable.append({"file": path.name, "error": getattr(exc, "message", str(exc))})
                    continue
                try:
                    s_header, s_body = self.read(str(b_header.get("id") or path.stem))
                except (Invalid, Refused, OSError):
                    continue  # opening the store already copied over any card only the base branch had
                if _content(b_header, b_body) != _content(s_header, s_body):
                    differences.append({
                        "id": str(s_header.get("id") or path.stem),
                        "main": render_card_file({k: v for k, v in b_header.items() if k in ("id", *CONTENT_FIELDS)}, b_body),
                        "state": render_card_file({k: v for k, v in s_header.items() if k in ("id", *CONTENT_FIELDS)}, s_body),
                    })
            return {"needed": True, "cards": len(files), "differences": differences, "unparseable": unparseable}

    def migrate(self, take: dict[str, str] | None = None) -> dict:
        """Make the cards branch the only home, then remove the base branch's copies in one commit.

        `take` says, for each card that differs, whether to keep the `main` text or the
        `state` text. A difference with no choice refuses, so nothing is lost by default.
        """
        from .adapters.files import _holder, _lease_expiry

        with self.src._lock:
            status = self.migration()
            if not status["needed"]:
                return {"migrated": False, "reason": "already done"}
            if status.get("unparseable"):
                bad = ", ".join(f"{u['file']} ({u['error']})" for u in status["unparseable"])
                raise Refused(
                    "migration-unparseable",
                    f"legacy card file(s) could not be parsed and would be lost on migrate: {bad}",
                )
            norm_take = {str(k).upper(): str(v).strip().lower() for k, v in (take or {}).items()}
            bad_choices = [f"{k}={v}" for k, v in norm_take.items() if v not in ("main", "state")]
            if bad_choices:
                raise Refused(
                    "migration-choice-invalid",
                    f"invalid migration choice(s): {', '.join(bad_choices)}; expected main or state",
                )
            missing = [d["id"] for d in status["differences"] if norm_take.get(d["id"].upper()) not in ("main", "state")]
            if missing:
                raise Refused("migration-choice-needed",
                              f"{', '.join(missing)} differ between main and the cards branch; choose main or state for each")

            # Pre-validate all `take=main` mutations before modifying any card so migration
            # never aborts mid-loop leaving a partially mutated cards branch.
            planned_main: list[tuple[str, dict, str, dict, str, bool]] = []
            for diff in status["differences"]:
                if norm_take[diff["id"].upper()] != "main":
                    continue
                old_header, old_body = self.read(diff["id"])
                state = str(old_header.get("state") or "ready")
                held = bool(_holder(old_header)) and _lease_expiry(old_header) > utc_now()
                if state == "in_progress" or held:
                    raise Refused("card-not-editable", f"{diff['id']} {_REFUSALS['in_progress']}")
                b_header, b_body = parse_card_file(diff["main"])
                candidate = dict(old_header)
                for key in CONTENT_FIELDS:
                    candidate.pop(key, None)
                candidate.pop("deps", None)
                candidate.update({k: v for k, v in b_header.items() if k in CONTENT_FIELDS})
                parse_card_file(render_card_file(candidate, b_body))
                planned_main.append((diff["id"], b_header, b_body, old_header, old_body, state not in _REFUSALS))

            by = person(self.root)
            why = "kept the base branch's text when the cards moved"
            for card_id, b_header, b_body, old_header, old_body, editable in planned_main:
                if editable:
                    def _use_main(header: dict, body: str, b_header=b_header, b_body=b_body) -> tuple[dict, str]:
                        for key in CONTENT_FIELDS:
                            header.pop(key, None)
                        header.pop("deps", None)
                        header.update({k: v for k, v in b_header.items() if k in CONTENT_FIELDS})
                        return header, b_body

                    self.change_card(card_id, "edit", by=by, why=why, apply=_use_main)
                else:
                    # Terminal/review cards (`done`, `in_review`, `dropped`, `split`) keep their
                    # farm state while adopting main's content fields + body as explicitly chosen.
                    before = _content(old_header, old_body)
                    header = dict(old_header)
                    for key in CONTENT_FIELDS:
                        header.pop(key, None)
                    header.pop("deps", None)
                    header.update({k: v for k, v in b_header.items() if k in CONTENT_FIELDS})
                    after = _content(header, b_body)
                    changed = {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
                               if before.get(k) != after.get(k)}
                    if changed:
                        header["edited_at"] = utc_now()
                    msg = f"{card_id}: migrated text from base branch by {by}"
                    self.src._write(card_id, header, b_body, msg, commit=False)
                    self._note(card_id, "edit", by, why, changed)
                    Git(self.src.worktree).commit_all(msg)

            commit = self._finish_migration(commit_base=True)
            return {"migrated": True, "cards": status["cards"],
                    "kept_main": sorted(k for k, v in norm_take.items() if v == "main"),
                    "base_commit": commit}

    def _finish_migration(self, *, commit_base: bool) -> str:
        """Move the documents over, clear the base branch's copies, and mark the branch as home."""
        with self.src._lock:
            worktree = self.src.worktree
            doc_payloads: list[tuple[Path, bytes]] = []
            removed: list[str] = []
            for store_rel, legacy_rel in DOCS.values():
                legacy, target = self.root / legacy_rel, worktree / store_rel
                if legacy.is_file():  # until now the base branch's copy was the live one
                    doc_payloads.append((target, legacy.read_bytes()))
                    removed.append(legacy_rel)
            folder = self.src.legacy_base_cards()
            if folder is not None and commit_base:
                removed.append(self.src.folder)

            git = Git(self.root) if (self.root / ".git").exists() else None
            tracked = bool(removed and git and git._run(["ls-files", "--", *removed], check=False).stdout.strip())
            on_base = bool(git and git.ref_exists("HEAD") and git.current_branch() == self.cfg.base_branch)

            # Remove tracked copies from the base branch BEFORE marking `migrated = True`
            # on the cards branch, so a failed `git rm`/`git commit` never strands `cards/`
            # on main behind a `migrated = True` flag.
            commit = ""
            if tracked and on_base:
                git._run(["rm", "-r", "-q", "--ignore-unmatch", "--", *removed])
                git._run(["commit", "-q", "-m", "openfarm: cards and spec now live on the cards branch", "--", *removed])
                commit = git.head()

            for target, payload in doc_payloads:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            meta = self.src.meta()
            meta.update(migrated=True, migrated_at=utc_now())
            self.src.save_meta(meta, "openfarm: this branch is now the only home of the cards and the spec")

            if not removed or (tracked and not on_base):
                return commit
            for rel in removed:
                path = self.root / rel
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            return commit


def open_store(root: Path, cfg) -> Store:
    return Store(root, cfg)


def record(src, root: Path, card_id: str, kind: str, why: str, changed: dict, by: str = "") -> None:
    """Add one line to a card's history for a change the farm's own commands just made."""
    worktree = getattr(src, "worktree", None)
    if worktree is None:
        return  # a card source without a branch of its own keeps no history
    lock = getattr(src, "_lock", None)
    with (lock if lock is not None else contextlib.nullcontext()):
        path = Path(worktree) / "history" / f"{card_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": utc_now(), "by": by or person(root), "kind": kind, "why": why, "changed": changed}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        Git(worktree).commit_all(f"{card_id}: {kind} ({why or 'no reason given'})")


def store_or_none(root: Path, cfg) -> Store | None:
    """The store, for a folder that is a git repository with an openfarm config."""
    if cfg is None or not (Path(root) / ".git").exists():
        return None
    return Store(root, cfg)


def decision_entry(old: str, kind: str, text: str, question: str = "") -> str:
    """The decisions log with one dated entry added, or `old` when the text is already in it."""
    text = str(text or "").strip()
    if not text or text in old:
        return old
    label = {"idea": "The idea", "answer": question.strip() or "Answer",
             "iteration": "Something's wrong", "feature": "New feature"}.get(kind, kind)
    head = "# Decisions\n\nEverything the owner said, in order. Entries are added, never changed.\n"
    return (old.rstrip("\n") + "\n" if old.strip() else head) + f"\n## {utc_now()[:10]} · {label}\n\n{text}\n"


def write_doc(root: Path, cfg, name: str, text: str, why: str = "") -> Path:
    """Write a planning document where it lives now (see `doc_path`)."""
    store = store_or_none(root, cfg)
    if store is not None:
        return store.write_doc(name, text, why)
    path = Path(root) / DOCS[name][1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def log_decisions(root: Path, cfg, entries: list[tuple[str, str, str]]) -> tuple[Path, str]:
    """Append `(kind, question, text)` entries to the decisions log. Returns its path and full text."""
    store = store_or_none(root, cfg)
    if store is not None:
        with store.src._lock:
            path = store.doc_path("decisions")
            old = store.read_doc("decisions")
            text = old
            for kind, question, body in entries:
                text = decision_entry(text, kind, body, question)
            if text != old:
                store.write_doc("decisions", text, why="entries added")
            return path, text
    path = Path(root) / DOCS["decisions"][1]
    try:
        old = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        old = ""
    text = old
    for kind, question, body in entries:
        text = decision_entry(text, kind, body, question)
    if text != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path, text
