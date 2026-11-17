"""Swarm roster, collision detection, and checked coordinator actions for v0.8."""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Iterable

from .adapters.acp import is_acp_adapter
from .board import CODE_AUTHOR_NAMES, Board, is_stop_request, normalize_role
from .cards import Card
from .errors import Refused
from .globs import globs_overlap

WORKER_ROLES: tuple[str, ...] = ("planner", "test-writer", "implementer", "checker")
SEAT_ROLES: tuple[str, ...] = ("coordinator", "supervisor")
VALID_SEAT_OWNER_KINDS: tuple[str, ...] = ("human", "agent")


def collision_reason(card_a: Card, card_b: Card) -> str:
    """Return a non-empty reason if `card_a` and `card_b` cannot run together."""
    if card_a.id == card_b.id:
        return f"same card {card_a.id}"
    if card_b.id in (card_a.deps or ()):
        return f"{card_a.id} depends on {card_b.id}"
    if card_a.id in (card_b.deps or ()):
        return f"{card_b.id} depends on {card_a.id}"
    for pat_a in card_a.touches or ():
        for pat_b in card_b.touches or ():
            if globs_overlap(pat_a, pat_b):
                return f"overlapping touches {pat_a!r} and {pat_b!r}"
    return ""


def cards_collide(card_a: Card, card_b: Card) -> bool:
    """True when two cards depend on each other or have overlapping touches."""
    return bool(collision_reason(card_a, card_b))


def worker_member_name(card_id: str, role: str) -> str:
    """Deterministic board member name for one role on one card."""
    return f"{card_id}-{normalize_role(role)}"


def worker_kind_for_config(cfg) -> str:
    """Board member kind (`api`, `acp`, or `scripted`) for configured worker backend."""
    adapter = str(getattr(cfg, "model_adapter", "openai-compatible") or "")
    if adapter in ("scripted", "stub"):
        return "scripted"
    if is_acp_adapter(adapter):
        return "acp"
    return "api"


def parse_team_spec(raw: str | None) -> dict[str, dict[str, str]]:
    """Parse `--team role=tier:effort,...` into `{role: {tier?, effort?}}`."""
    if raw is None or not str(raw).strip():
        return {}
    team: dict[str, dict[str, str]] = {}
    for entry in str(raw).split(","):
        item = entry.strip()
        if not item:
            continue
        if "=" not in item:
            raise Refused(
                "bad-team-spec",
                f"team item {item!r} must have form role=tier:effort",
            )
        raw_role, rhs = item.split("=", 1)
        role = normalize_role(raw_role)
        if role not in WORKER_ROLES:
            raise Refused(
                "bad-team-role",
                f"team role {raw_role!r} must be one of {', '.join(WORKER_ROLES)}",
            )
        if ":" in rhs:
            tier_part, effort_part = rhs.split(":", 1)
            if ":" in effort_part:
                raise Refused(
                    "bad-team-spec",
                    f"team item {item!r} has extra ':'",
                )
            spec: dict[str, str] = {}
            if tier_part.strip():
                spec["tier"] = tier_part.strip()
            if effort_part.strip():
                spec["effort"] = effort_part.strip()
            elif rhs.endswith(":"):
                spec["effort"] = ""
            team[role] = spec
        else:
            if not rhs.strip():
                raise Refused(
                    "bad-team-spec",
                    f"team item {item!r} is missing tier or effort",
                )
            team[role] = {"tier": rhs.strip()}
    return team


def _refuse(
    board: Board,
    caller: str,
    code: str,
    message: str,
    *,
    card: str = "",
) -> Refused:
    if board.board_dir.exists():
        to = [caller] if caller and board.get_member(caller) is not None else ["coordinator"]
        try:
            board.post(
                author="system",
                to=to,
                kind="action",
                body=f"refused ({code}): {message}",
                card=card,
                trusted=True,
            )
        except Refused:
            pass
    return Refused(code, message)


def _require_coordinator(
    board: Board, caller: str, *, card: str = "", outside: bool = False
) -> dict:
    """The caller's roster entry, if it holds the coordinator seat.

    `outside=True` is for a caller that named itself (`--as` on the command
    line): only an external member that joined the seat passes then, so
    typing the name of the code coordinator proves nothing.
    """
    clean = str(caller or "").strip()
    if not clean:
        raise _refuse(
            board, clean, "not-coordinator", "caller name (--as) is required", card=card
        )
    member = board.get_member(clean)
    if member is None or member.get("state") == "removed":
        raise _refuse(
            board,
            clean,
            "not-coordinator",
            f"{clean!r} is not an active member on the board",
            card=card,
        )
    if normalize_role(str(member.get("role", ""))) != "coordinator":
        raise _refuse(
            board,
            clean,
            "not-coordinator",
            f"{clean!r} (role {member.get('role')!r}) does not hold the coordinator seat",
            card=card,
        )
    if outside and member.get("kind") != "external":
        raise _refuse(
            board,
            clean,
            "not-coordinator",
            f"{clean!r} is a {member.get('kind')!r} member; from outside, only a member "
            "that joined the coordinator seat (openfarm swarm join) can act",
            card=card,
        )
    return member


def _load_cards_map(source=None, cards_by_id: dict[str, Card] | None = None) -> dict[str, Card]:
    if cards_by_id is not None:
        return dict(cards_by_id)
    if source is not None and hasattr(source, "all_cards"):
        return {card.id: card for card in source.all_cards()}
    return {}


def _is_start_action(post: dict, members: dict[str, dict]) -> bool:
    """A `start CARD` action posted by a holder of the coordinator seat."""
    if post.get("kind") != "action" or not post.get("card"):
        return False
    if not str(post.get("body", "") or "").startswith("start "):
        return False
    member = members.get(str(post.get("from", "") or ""))
    return member is not None and normalize_role(str(member.get("role", ""))) == "coordinator"


class CardProgress:
    """What the board says about each card: started, finished, asked to stop.

    A card is finished only on its own final post (code, author `gates`, kind
    `done` or `blocked`), or when the coordinator blocked it before it ever
    started. A stop request on a running card leaves it running until its
    thread ends and posts that final post.
    """

    def __init__(self) -> None:
        self.started: set[str] = set()
        self.final: set[str] = set()
        self.stop_requested: set[str] = set()

    def feed(self, posts: Iterable[dict], members: dict[str, dict]) -> None:
        for post in posts:
            cid = str(post.get("card", "") or "")
            if not cid:
                continue
            author = str(post.get("from", "") or "")
            kind = post.get("kind")
            if author == "gates" and kind in ("done", "blocked"):
                self.final.add(cid)
            elif _is_start_action(post, members):
                self.started.add(cid)
            elif is_stop_request(post, members):
                self.stop_requested.add(cid)

    def note_members(self, members: Iterable[dict]) -> None:
        # A card with team members on the roster was started, whatever path
        # registered them.
        for member in members:
            cid = str(member.get("card", "") or "")
            if cid:
                self.started.add(cid)

    @property
    def finished(self) -> set[str]:
        return self.final | (self.stop_requested - self.started)

    @property
    def stopping(self) -> set[str]:
        """Running cards the coordinator asked to stop."""
        return (self.stop_requested & self.started) - self.final


def _card_progress(board: Board) -> CardProgress:
    progress = CardProgress()
    if not board.board_dir.exists():
        return progress
    roster = board.list_members(include_removed=True)
    members = {str(m.get("name", "")): m for m in roster}
    progress.note_members(roster)
    progress.feed(board.read(as_name="*", since=0, limit=None, for_model=False), members)
    return progress


def _running_cards_from_board(board: Board) -> set[str]:
    finished = _finished_cards_from_board(board)
    return {
        str(m.get("card", ""))
        for m in board.list_members(include_removed=False)
        if m.get("card") and str(m.get("card", "")) not in finished
    }


def _finished_cards_from_board(board: Board) -> set[str]:
    return _card_progress(board).finished


def swarm_tiers(cfg) -> dict:
    """Configured tiers and default tier."""
    return {
        "default_tier": cfg.swarm.default_tier,
        "tiers": {name: tier.as_dict() for name, tier in cfg.tiers.items()},
    }


def swarm_status(board: Board, cfg) -> dict:
    """Board summary, swarm settings, configured tiers, and non-removed roster."""
    members = board.list_members(include_removed=False)
    progress = _card_progress(board)
    running_cards = sorted(
        {
            str(m.get("card", ""))
            for m in members
            if m.get("card") and str(m.get("card", "")) not in progress.finished
        }
    )
    return {
        "board": board.status(),
        "swarm": cfg.swarm.as_dict(),
        "tiers": {name: tier.as_dict() for name, tier in cfg.tiers.items()},
        "members": members,
        "running_cards": running_cards,
        "stopping_cards": sorted(set(running_cards) & progress.stopping),
    }


def swarm_join(
    board: Board,
    cfg,
    *,
    name: str,
    seat: str,
    owner_kind: str = "human",
) -> dict:
    """Claim an external coordinator or supervisor seat on the board."""
    clean_seat = normalize_role(seat)
    if clean_seat not in SEAT_ROLES:
        raise Refused("bad-seat", f"seat must be 'coordinator' or 'supervisor', got {seat!r}")
    if owner_kind not in VALID_SEAT_OWNER_KINDS:
        raise Refused(
            "bad-owner-kind",
            f"owner kind must be 'human' or 'agent', got {owner_kind!r}",
        )
    configured_seat = (
        cfg.swarm.coordinator if clean_seat == "coordinator" else cfg.swarm.supervisor
    )
    if configured_seat != "external":
        raise Refused(
            "seat-not-external",
            f"{clean_seat} seat is {configured_seat!r}, not 'external'",
        )

    clean_name = str(name or "").strip()
    if clean_name in CODE_AUTHOR_NAMES:
        raise Refused(
            "reserved-name",
            f"{clean_name!r} is a name only code uses; join under another name",
        )

    board.ensure()
    same_name = board.get_member(clean_name)
    if same_name is not None and same_name.get("kind") != "external":
        raise Refused(
            "name-taken",
            f"{clean_name!r} is already a {same_name.get('kind')!r} member "
            f"({same_name.get('role')!r}); join under another name",
        )
    active_members = board.list_members(include_removed=False)
    for existing in active_members:
        # A code member (the code fallback acting for a silent external
        # coordinator) never holds a seat against an outside member joining it.
        if existing.get("kind") == "code":
            continue
        if normalize_role(str(existing.get("role", ""))) == clean_seat and existing["name"] != clean_name:
            raise Refused(
                "seat-occupied",
                f"{clean_seat} seat is already held by {existing['name']!r}",
            )
    if not any(m["name"] == clean_name for m in active_members):
        if len(active_members) >= cfg.swarm.max_members:
            raise Refused(
                "max-members",
                f"roster already has {len(active_members)} active members (limit {cfg.swarm.max_members})",
            )

    member = board.upsert_member(
        clean_name,
        kind="external",
        role=clean_seat,
        state="active",
        added_by="cli",
        owner_kind=owner_kind,
    )
    board.post(
        author="system",
        to=["all"],
        kind="roster",
        body=f"{clean_name} joined seat {clean_seat} ({owner_kind})",
        trusted=True,
    )
    return member


def _resolve_role_assignment(cfg, role: str, spec: dict | None) -> tuple[str, str, str]:
    """`(tier, model, effort)` for one role of a started card.

    A tier named in the team spec gives its model. Otherwise the role keeps
    its configured model (`Config.member_defaults`), as with the swarm off.
    """
    spec_dict = dict(spec or {})
    raw_tier = spec_dict.get("tier")
    if raw_tier:
        tier_name = str(raw_tier).strip()
        if tier_name not in cfg.tiers:
            raise Refused("unknown-tier", f"unknown tier {tier_name!r}")
        tier_cfg = cfg.tiers[tier_name]
        model_name = tier_cfg.model
    else:
        tier_name, model_name = cfg.member_defaults(role)
        if tier_name not in cfg.tiers:
            raise Refused("unknown-tier", f"unknown tier {tier_name!r}")
        tier_cfg = cfg.tiers[tier_name]

    if "effort" in spec_dict and spec_dict["effort"] is not None:
        effort = str(spec_dict["effort"])
        if effort == "":
            if tier_cfg.effort_param and tier_cfg.efforts:
                raise Refused(
                    "bad-effort",
                    f"effort '' is not in tier {tier_cfg.name!r} efforts {list(tier_cfg.efforts)!r}",
                )
        elif effort not in tier_cfg.efforts:
            raise Refused(
                "bad-effort",
                f"effort {effort!r} is not in tier {tier_cfg.name!r} efforts {list(tier_cfg.efforts)!r}",
            )
    else:
        default_effort, _ = cfg.effort_for_role(role)
        effort = default_effort
        if effort and effort not in tier_cfg.efforts:
            raise Refused(
                "bad-effort",
                f"default effort {effort!r} for {role} is not in tier {tier_cfg.name!r} "
                f"efforts {list(tier_cfg.efforts)!r}",
            )
    return tier_cfg.name, model_name, effort


def swarm_start(
    board: Board,
    cfg,
    *,
    caller: str,
    card_id: str,
    team: dict[str, dict[str, str]] | None = None,
    source=None,
    cards_by_id: dict[str, Card] | None = None,
    running_card_ids: Iterable[str] | None = None,
    tokens_used: int = 0,
    spend_used: float = 0.0,
    member_kind: str | None = None,
    outside: bool = False,
) -> dict:
    """Validate and start a ready card with a team of worker members."""
    _require_coordinator(board, caller, card=card_id, outside=outside)

    cards_map = _load_cards_map(source, cards_by_id)
    card = cards_map.get(card_id)
    if card is None and source is not None and hasattr(source, "read"):
        try:
            card = Card.from_dict(source.read(card_id)["card"])
            cards_map[card_id] = card
        except Refused:
            card = None
    if card is None:
        raise _refuse(board, caller, "unknown-card", f"unknown card {card_id!r}", card=card_id)

    running = set(running_card_ids or ()) | _running_cards_from_board(board)
    if card_id in running:
        raise _refuse(
            board, caller, "already-running", f"card {card_id!r} is already running", card=card_id
        )
    if card_id in _finished_cards_from_board(board):
        raise _refuse(
            board,
            caller,
            "already-finished",
            f"card {card_id!r} has already finished on the board",
            card=card_id,
        )
    if card.state != "ready":
        raise _refuse(
            board,
            caller,
            "not-ready",
            f"card {card_id!r} is in state {card.state!r}, not 'ready'",
            card=card_id,
        )
    for dep in card.deps:
        dep_card = cards_map.get(dep)
        if dep_card is None or dep_card.state != "done":
            raise _refuse(
                board,
                caller,
                "deps-not-done",
                f"card {card_id!r} waits on dependency {dep!r}",
                card=card_id,
            )

    max_slots = max(1, int(getattr(cfg, "concurrent_cards", 1) or 1))
    if len(running) >= max_slots:
        raise _refuse(
            board,
            caller,
            "slot-limit",
            f"{len(running)} cards running, at concurrent_cards limit {max_slots}",
            card=card_id,
        )

    for other_id in sorted(running):
        other = cards_map.get(other_id)
        if other is not None:
            reason = collision_reason(card, other)
            if reason:
                raise _refuse(
                    board,
                    caller,
                    "card-collision",
                    f"card {card_id!r} collides with running card {other_id!r}: {reason}",
                    card=card_id,
                )

    spend_cap = float(getattr(cfg, "spend_per_night", 0.0) or 0.0)
    if spend_cap > 0 and spend_used >= spend_cap:
        raise _refuse(
            board,
            caller,
            "budget-exceeded",
            f"night spend {spend_used:.4f} reached cap {spend_cap:.4f}",
            card=card_id,
        )
    tokens_cap = int(getattr(cfg, "tokens_per_card", 0) or 0)
    if tokens_cap > 0 and tokens_used >= tokens_cap:
        raise _refuse(
            board,
            caller,
            "budget-exceeded",
            f"tokens used {tokens_used} reached cap {tokens_cap}",
            card=card_id,
        )

    normalized_team: dict[str, dict[str, str]] = {}
    for raw_role, spec in (team or {}).items():
        norm = normalize_role(raw_role)
        if norm not in WORKER_ROLES:
            raise _refuse(
                board,
                caller,
                "bad-team-role",
                f"role {raw_role!r} must be one of {', '.join(WORKER_ROLES)}",
                card=card_id,
            )
        normalized_team[norm] = dict(spec or {})

    resolved: dict[str, tuple[str, str, str]] = {}
    for role in WORKER_ROLES:
        try:
            resolved[role] = _resolve_role_assignment(cfg, role, normalized_team.get(role))
        except Refused as exc:
            raise _refuse(board, caller, exc.code, exc.message, card=card_id) from exc

    active_members = board.list_members(include_removed=False)
    existing_names = {m["name"] for m in active_members}
    new_names = [
        worker_member_name(card_id, role)
        for role in WORKER_ROLES
        if worker_member_name(card_id, role) not in existing_names
    ]
    if len(active_members) + len(new_names) > cfg.swarm.max_members:
        raise _refuse(
            board,
            caller,
            "max-members",
            f"adding {len(new_names)} members exceeds max_members {cfg.swarm.max_members}",
            card=card_id,
        )

    kind = member_kind or worker_kind_for_config(cfg)
    created: dict[str, dict] = {}
    for role in WORKER_ROLES:
        tier_name, model_name, effort_val = resolved[role]
        mname = worker_member_name(card_id, role)
        created[role] = board.upsert_member(
            mname,
            kind=kind,
            role=role,
            card=card_id,
            tier=tier_name,
            model=model_name,
            effort=effort_val,
            state="active",
            added_by=caller,
        )

    summary = ", ".join(
        f"{role}={resolved[role][0]}:{resolved[role][2] or 'default'}"
        for role in WORKER_ROLES
    )
    models = ", ".join(f"{role}={resolved[role][1]}" for role in WORKER_ROLES)
    board.post(
        author=caller,
        to=["all"],
        kind="action",
        card=card_id,
        body=f"start {card_id} ({summary}; models: {models})",
    )
    return {"action": "start", "card": card_id, "team": created}


def swarm_set(
    board: Board,
    cfg,
    *,
    caller: str,
    member_name: str,
    tier: str | None = None,
    effort: str | None = None,
    reason: str = "",
    outside: bool = False,
) -> dict:
    """Change a member's tier and/or effort from its next call."""
    _require_coordinator(board, caller, outside=outside)
    if tier is None and effort is None:
        raise _refuse(board, caller, "empty-set", "set requires tier and/or effort")

    member = board.get_member(member_name)
    if member is None:
        raise _refuse(board, caller, "unknown-member", f"unknown member {member_name!r}")
    if member.get("state") == "removed":
        raise _refuse(
            board,
            caller,
            "removed-member",
            f"member {member_name!r} has been removed",
            card=str(member.get("card", "") or ""),
        )
    if member.get("kind") in ("code", "external"):
        raise _refuse(
            board,
            caller,
            "not-a-tier-member",
            f"cannot set tier/effort on {member.get('kind')!r} member {member_name!r}",
            card=str(member.get("card", "") or ""),
        )

    target_tier_name = tier if tier is not None else str(member.get("tier") or cfg.swarm.default_tier)
    if target_tier_name not in cfg.tiers:
        raise _refuse(
            board,
            caller,
            "unknown-tier",
            f"unknown tier {target_tier_name!r}",
            card=str(member.get("card", "") or ""),
        )
    tier_cfg = cfg.tiers[target_tier_name]

    if effort is not None:
        target_effort = str(effort)
        if target_effort == "":
            if tier_cfg.effort_param or tier_cfg.efforts:
                raise _refuse(
                    board,
                    caller,
                    "bad-effort",
                    f"effort '' is not in tier {tier_cfg.name!r} efforts {list(tier_cfg.efforts)!r}",
                    card=str(member.get("card", "") or ""),
                )
        elif target_effort not in tier_cfg.efforts:
            raise _refuse(
                board,
                caller,
                "bad-effort",
                f"effort {target_effort!r} is not in tier {tier_cfg.name!r} efforts {list(tier_cfg.efforts)!r}",
                card=str(member.get("card", "") or ""),
            )
    else:
        current_effort = str(member.get("effort", "") or "")
        if not tier_cfg.efforts and tier_cfg.effort_param == "":
            target_effort = ""
        elif current_effort and current_effort not in tier_cfg.efforts:
            raise _refuse(
                board,
                caller,
                "bad-effort",
                f"existing effort {current_effort!r} is not in tier {tier_cfg.name!r} "
                f"efforts {list(tier_cfg.efforts)!r}; pass --effort",
                card=str(member.get("card", "") or ""),
            )
        else:
            target_effort = current_effort

    # Naming a tier gives the tier's model; changing only the effort keeps the
    # model the member is really called with (a role's own model, say).
    target_model = (
        tier_cfg.model
        if tier is not None
        else str(member.get("model", "") or "") or tier_cfg.model
    )
    updated = board.update_member(
        member_name,
        tier=tier_cfg.name,
        model=target_model,
        effort=target_effort,
    )
    msg = f"set {member_name}: tier={tier_cfg.name}, effort={target_effort or 'none'}"
    if reason:
        msg += f" ({reason})"
    board.post(
        author=caller,
        to=[member_name],
        kind="action",
        card=str(member.get("card", "") or ""),
        body=msg,
    )
    return {"action": "set", "member": updated, "reason": reason}


def swarm_hint(
    board: Board,
    cfg,
    *,
    caller: str,
    member_name: str,
    body: str,
    outside: bool = False,
) -> dict:
    """Post a short note addressed to a member."""
    _require_coordinator(board, caller, outside=outside)
    member = board.get_member(member_name)
    if member is None:
        raise _refuse(board, caller, "unknown-member", f"unknown member {member_name!r}")
    if member.get("state") == "removed":
        raise _refuse(
            board,
            caller,
            "removed-member",
            f"member {member_name!r} has been removed",
            card=str(member.get("card", "") or ""),
        )
    text = str(body or "").strip()
    if not text:
        raise _refuse(
            board,
            caller,
            "empty-hint",
            "hint body must not be empty",
            card=str(member.get("card", "") or ""),
        )
    post = board.post(
        author=caller,
        to=[member_name],
        kind="note",
        card=str(member.get("card", "") or ""),
        body=text,
    )
    return {"action": "hint", "post": post}


def swarm_ask(
    board: Board,
    cfg,
    *,
    caller: str,
    body: str,
    card: str = "",
    attempt: int = 0,
    expects: dict | None = None,
    evidence: list[str] | None = None,
    outside: bool = False,
) -> dict:
    """Post a question to the supervisor seat."""
    _require_coordinator(board, caller, card=card, outside=outside)
    if cfg.swarm.supervisor == "off":
        raise _refuse(
            board, caller, "supervisor-off", "supervisor seat is 'off'", card=card
        )
    text = str(body or "").strip()
    if not text:
        raise _refuse(
            board, caller, "empty-question", "question body must not be empty", card=card
        )
    supervisors = [
        m["name"]
        for m in board.list_members(include_removed=False)
        if normalize_role(str(m.get("role", ""))) == "supervisor"
    ]
    recipients = supervisors or ["supervisor"]
    post = board.post(
        author=caller,
        to=recipients,
        kind="question",
        body=text,
        card=card,
        attempt=attempt,
        evidence=evidence,
        expects=expects,
    )
    return {"action": "ask", "post": post}


def swarm_block(
    board: Board,
    cfg,
    *,
    caller: str,
    card_id: str,
    reason: str,
    source=None,
    cards_by_id: dict[str, Card] | None = None,
    outside: bool = False,
) -> dict:
    """Stop a card with a reason for the owner."""
    _require_coordinator(board, caller, card=card_id, outside=outside)
    text = str(reason or "").strip()
    if not text:
        raise _refuse(
            board, caller, "empty-reason", "block reason must not be empty", card=card_id
        )
    cards_map = _load_cards_map(source, cards_by_id)
    card = cards_map.get(card_id)
    if card is None and source is not None and hasattr(source, "read"):
        try:
            card = Card.from_dict(source.read(card_id)["card"])
        except Refused:
            card = None
    has_members = any(
        m.get("card") == card_id for m in board.list_members(include_removed=True)
    )
    if card is None and (cards_map or source is not None) and not has_members:
        raise _refuse(
            board, caller, "unknown-card", f"unknown card {card_id!r}", card=card_id
        )
    if card is not None and card.state in ("done", "in_review", "blocked", "dropped"):
        raise _refuse(
            board,
            caller,
            "already-finished",
            f"card {card_id!r} is already in state {card.state!r}",
            card=card_id,
        )
    progress = _card_progress(board)
    if card_id in progress.finished:
        raise _refuse(
            board,
            caller,
            "already-finished",
            f"card {card_id!r} has already finished on the board",
            card=card_id,
        )
    if card_id in progress.stopping:
        raise _refuse(
            board,
            caller,
            "already-stopping",
            f"card {card_id!r} was already asked to stop; it is still running until it ends",
            card=card_id,
        )
    running = card_id in progress.started
    if running:
        # A stop request: the card's thread sees it before its next model call
        # and ends with its own final post. Until then it still counts as
        # running for collision and slot checks.
        body = f"stop requested for {card_id} (it keeps running until it ends): {text}"
        message = (
            f"asked {card_id} to stop; it is still running until it ends "
            "and stays in its slot until then"
        )
    else:
        body = text
        message = f"blocked {card_id} before it started"
    post = board.post(
        author=caller,
        to=["all"],
        kind="blocked",
        card=card_id,
        body=body,
    )
    return {
        "action": "block",
        "card": card_id,
        "reason": text,
        "running": running,
        "stop_requested": running,
        "message": message,
        "post": post,
    }


def swarm_remove(
    board: Board,
    cfg,
    *,
    caller: str,
    member_name: str,
    finished_cards: Iterable[str] | None = None,
    cards_by_id: dict[str, Card] | None = None,
    source=None,
    outside: bool = False,
) -> dict:
    """Remove a member whose card is finished."""
    _require_coordinator(board, caller, outside=outside)
    member = board.get_member(member_name)
    if member is None:
        raise _refuse(board, caller, "unknown-member", f"unknown member {member_name!r}")
    if member.get("state") == "removed":
        raise _refuse(
            board, caller, "already-removed", f"member {member_name!r} is already removed"
        )
    if member_name == caller:
        raise _refuse(
            board, caller, "cannot-remove-self", "coordinator cannot remove itself"
        )
    if member.get("kind") == "code":
        raise _refuse(
            board,
            caller,
            "protected-member",
            f"cannot remove code member {member_name!r}",
        )

    card_id = str(member.get("card", "") or "")
    if card_id:
        finished = set(finished_cards or ()) | _finished_cards_from_board(board)
        if card_id not in finished:
            cards_map = _load_cards_map(source, cards_by_id)
            card = cards_map.get(card_id)
            if card is None and source is not None and hasattr(source, "read"):
                try:
                    card = Card.from_dict(source.read(card_id)["card"])
                except Refused:
                    card = None
            if card is None or card.state not in ("done", "in_review", "blocked", "dropped"):
                raise _refuse(
                    board,
                    caller,
                    "card-not-finished",
                    f"cannot remove {member_name!r}: card {card_id!r} is not finished",
                    card=card_id,
                )

    updated = board.update_member(member_name, state="removed")
    board.post(
        author=caller,
        to=["all"],
        kind="roster",
        card=card_id,
        body=f"removed {member_name}",
    )
    return {"action": "remove", "member": updated}


def swarm_wait(
    board: Board,
    cfg,
    *,
    caller: str,
    reason: str = "",
    outside: bool = False,
) -> dict:
    """Validate and record a coordinator wait action without waking the caller."""
    _require_coordinator(board, caller, outside=outside)
    board.post(
        author=caller,
        to=[],
        kind="action",
        body=f"wait: {reason}" if reason else "wait",
    )
    return {"action": "wait", "reason": reason}


_INFRA_ROLES = frozenset({"gates", "monitor", "system"})


class StallMonitor:
    """Periodic stall detector (`role = "monitor"`, `kind = "code"`).

    Triggers when the run has not ended, no card thread is actively executing,
    no worker of an unfinished card is active (seats only act when asked and
    never count), unfinished work remains, and nothing non-`stall` has been
    posted within `[swarm] stall_after`. Emits at most one `kind = "stall"` post per board
    activity sequence until non-`stall` activity advances the board.
    """

    def __init__(
        self,
        board: Board,
        cfg=None,
        *,
        stall_after_s: float | None = None,
        unfinished_fn: Callable[[], bool] | None = None,
        active_work_fn: Callable[[], int] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if stall_after_s is None:
            stall_after_s = float(
                getattr(getattr(cfg, "swarm", None), "stall_after_s", 300.0) or 300.0
            )
        self.board = board
        self.cfg = cfg
        self.stall_after_s = max(0.01, float(stall_after_s))
        self.poll_interval_s = min(30.0, max(0.05, self.stall_after_s / 2.0))
        self._unfinished_fn = unfinished_fn
        self._active_work_fn = active_work_fn
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_seq: int = 0
        self._last_non_stall_seq: int = 0
        self._last_activity_at: float | None = None
        self._stalled_for_seq: int = -1
        # Bounded state: unanswered questions by seq and what the board says
        # about each card, not every post of the run.
        self._open_questions: dict[int, dict] = {}
        self._progress = CardProgress()
        self._seed_initial_activity()

    def _seed_initial_activity(self) -> None:
        cur_now = float(self._clock())
        with self._lock:
            if self.board.posts_path.exists():
                self._sync_posts_unlocked(cur_now)
            else:
                self._last_activity_at = cur_now

    def ensure_member(self) -> dict:
        """Ensure `"monitor"` (`kind="code"`, `role="monitor"`) is registered."""
        self.board.ensure()
        existing = self.board.get_member("monitor")
        if existing is not None:
            return existing
        return self.board.upsert_member(
            "monitor",
            kind="code",
            role="monitor",
            added_by="system",
        )

    def note_activity(self, *, now: float | None = None) -> None:
        """Record non-stall activity at `now` (used for deterministic clocks)."""
        cur_now = float(self._clock() if now is None else now)
        with self._lock:
            self._sync_posts_unlocked(cur_now)
            self._last_activity_at = cur_now

    def _track_post_unlocked(self, post: dict, members: dict[str, dict] | None) -> None:
        seq = int(post.get("seq", 0) or 0)
        kind = post.get("kind")
        if kind == "question":
            self._open_questions[seq] = post
        elif kind == "answer" and post.get("ref"):
            self._open_questions.pop(int(post.get("ref", 0) or 0), None)
        if post.get("card") and kind in ("done", "blocked", "action"):
            if members is None:
                members = {
                    str(m.get("name", "")): m
                    for m in self.board.list_members(include_removed=True)
                }
            self._progress.feed([post], members)

    def _sync_posts_unlocked(self, cur_now: float) -> None:
        new_posts = self.board.read(
            as_name="*", since=self._scan_seq, limit=None, for_model=False
        )
        members: dict[str, dict] | None = None
        if any(p.get("card") and p.get("kind") in ("done", "blocked", "action") for p in new_posts):
            members = {
                str(m.get("name", "")): m
                for m in self.board.list_members(include_removed=True)
            }
        for post in new_posts:
            seq = int(post.get("seq", 0) or 0)
            if seq > self._scan_seq:
                self._scan_seq = seq
            self._track_post_unlocked(post, members)
            if post.get("kind") != "stall":
                if seq > self._last_non_stall_seq:
                    self._last_non_stall_seq = seq
                self._last_activity_at = cur_now
                self._stalled_for_seq = -1
        if self._last_activity_at is None:
            self._last_activity_at = cur_now

    def _finished_cards_unlocked(self, roster: list[dict]) -> set[str]:
        self._progress.note_members(roster)
        return self._progress.finished

    def _every_member_waiting_or_idle_unlocked(self) -> bool:
        """True when no member is doing work of its own.

        A seat (coordinator, supervisor) only acts when asked, whatever fills
        it (code, a tier, a config supervisor, an outside member), so it never
        counts as working: whether a card thread is really busy is answered by
        `active_work_fn`. A worker of a finished card is done working.
        """
        roster = self.board.list_members(include_removed=False)
        finished = self._finished_cards_unlocked(roster)
        for member in roster:
            name = str(member.get("name", "") or "")
            role = normalize_role(str(member.get("role", "") or ""))
            if name in _INFRA_ROLES or role in _INFRA_ROLES:
                continue
            if role in SEAT_ROLES:
                continue
            if member.get("state") == "waiting":
                continue
            if member.get("kind") == "external":
                continue
            cid = str(member.get("card", "") or "")
            if cid and cid in finished:
                continue
            return False
        return True

    def _oldest_unanswered_question_unlocked(self) -> dict | None:
        if not self._open_questions:
            return None
        return self._open_questions[min(self._open_questions)]

    def _has_unfinished_work_unlocked(self) -> bool:
        if self._unfinished_fn is not None:
            return bool(self._unfinished_fn())
        roster = self.board.list_members(include_removed=False)
        finished_cards = self._finished_cards_unlocked(roster)
        for member in roster:
            cid = str(member.get("card", "") or "")
            if cid and cid not in finished_cards:
                return True
        return self._oldest_unanswered_question_unlocked() is not None

    def _stall_targets(self, oldest_q: dict | None) -> list[str]:
        targets: list[str] = []
        if oldest_q is not None:
            for recip in oldest_q.get("to") or []:
                rs = str(recip or "").strip()
                if rs and rs not in ("monitor", "all") and rs not in targets:
                    targets.append(rs)
        if "coordinator" not in targets:
            targets.append("coordinator")
        return targets

    def check_once(self, *, now: float | None = None) -> dict | None:
        """Inspect board/worker state once; post at most one `stall` if stalled."""
        cur_now = float(self._clock() if now is None else now)
        if not self.board.board_dir.exists() or self.board.is_ended():
            return None

        with self._lock:
            self._sync_posts_unlocked(cur_now)
            if self._active_work_fn is not None and int(self._active_work_fn()) > 0:
                return None
            if not self._every_member_waiting_or_idle_unlocked():
                return None
            if not self._has_unfinished_work_unlocked():
                return None
            if self._stalled_for_seq == self._last_non_stall_seq:
                return None
            anchor = self._last_activity_at if self._last_activity_at is not None else cur_now
            idle_s = cur_now - anchor
            if idle_s < self.stall_after_s:
                return None

            oldest_q = self._oldest_unanswered_question_unlocked()
            targets = self._stall_targets(oldest_q)
            ref_seq = int(oldest_q.get("seq", 0) or 0) if oldest_q is not None else None
            card_id = str(oldest_q.get("card", "") or "") if oldest_q is not None else ""
            if ref_seq:
                body = (
                    f"stall detected after {idle_s:.1f}s quiet: "
                    f"unanswered question #{ref_seq} waiting on {', '.join(targets)}"
                )
            else:
                body = (
                    f"stall detected after {idle_s:.1f}s quiet: "
                    "unfinished work remains with no active member"
                )
            stalled_seq = self._last_non_stall_seq
            self.ensure_member()
            post = self.board.post(
                author="monitor",
                to=targets,
                kind="stall",
                body=body,
                card=card_id,
                ref=ref_seq,
                trusted=True,
            )
            self._scan_seq = max(self._scan_seq, int(post.get("seq", 0) or 0))
            self._stalled_for_seq = stalled_seq
            return post

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.poll_interval_s):
            if self.board.is_ended():
                break
            try:
                self.check_once()
            except (Refused, OSError, ValueError) as exc:
                print(f"monitor check error: {exc}", file=sys.stderr)

    def start(self) -> None:
        """Register `"monitor"` and start the background check loop."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.ensure_member()
            self._thread = threading.Thread(
                target=self._run_loop, name="openfarm-stall-monitor", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout_s: float = 2.0) -> None:
        """Signal the background monitor thread to exit and join it."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
