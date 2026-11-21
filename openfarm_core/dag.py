"""Cards-as-the-DAG scheduler & ASCII visualizer for Openfarm v0.11.0.

Derives dependency cycles, topological wave tiers, and within-tier non-colliding
parallel batches directly from `cards/*.md` headers (`wave` + `depends` + `touches`).
"""

from __future__ import annotations

from .errors import Refused
from .findings import is_finding_id_or_header
from .swarm import cards_collide, collision_reason


def _card_wave(card) -> int:
    fields = getattr(card, "fields", None) or {}
    try:
        return max(1, int(fields.get("wave") or 1))
    except (TypeError, ValueError):
        return 1


def build_card_dag(cards: list) -> dict:
    """Validate dependencies (cycle check), group into topological tiers, and partition parallel batches."""
    work_cards = [
        c for c in cards
        if not is_finding_id_or_header(c.id, getattr(c, "fields", None))
        and str(getattr(c, "state", "ready")).lower() not in ("split", "dropped", "dismissed", "folded", "promoted")
    ]
    by_id = {c.id: c for c in work_cards}
    edges: list[dict] = []
    deps_map: dict[str, list[str]] = {}
    raw_deps_map: dict[str, list[str]] = {}
    unmet_deps: dict[str, list[str]] = {}
    for c in work_cards:
        raw_deps = [str(d) for d in (getattr(c, "deps", None) or []) if str(d)]
        raw_deps_map[c.id] = raw_deps
        valid_deps = [d for d in raw_deps if d in by_id]
        missing_deps = [d for d in raw_deps if d not in by_id]
        deps_map[c.id] = valid_deps
        if missing_deps:
            unmet_deps[c.id] = missing_deps
        for d in valid_deps:
            edges.append({"from": d, "to": c.id})

    # Cycle detection via 3-color DFS
    color: dict[str, int] = {cid: 0 for cid in by_id}
    stack: list[str] = []

    def _dfs(cid: str) -> None:
        color[cid] = 1
        stack.append(cid)
        for dep in deps_map[cid]:
            if color[dep] == 1:
                idx = stack.index(dep)
                cycle = " -> ".join(stack[idx:] + [dep])
                raise Refused("cyclic-dag", f"cyclic card dependency detected: {cycle}")
            if color[dep] == 0:
                _dfs(dep)
        stack.pop()
        color[cid] = 2

    for cid in sorted(by_id):
        if color[cid] == 0:
            _dfs(cid)

    # Compute topological depth (0 = no in-graph deps)
    depth: dict[str, int] = {}

    def _depth(cid: str) -> int:
        if cid in depth:
            return depth[cid]
        if not deps_map[cid]:
            depth[cid] = 0
        else:
            depth[cid] = 1 + max(_depth(d) for d in deps_map[cid])
        return depth[cid]

    for cid in by_id:
        _depth(cid)

    tier_buckets: dict[int, list] = {}
    for cid in sorted(by_id, key=lambda k: (_card_wave(by_id[k]), depth[k], k)):
        tier_buckets.setdefault(depth[cid], []).append(by_id[cid])

    tiers: list[dict] = []
    all_conflicts: list[dict] = []
    for tier_idx in sorted(tier_buckets):
        t_cards = tier_buckets[tier_idx]
        # Partition this tier into non-colliding parallel batches
        batches: list[list[str]] = []
        t_conflicts: list[dict] = []
        for i, ca in enumerate(t_cards):
            for cb in t_cards[i + 1 :]:
                reason = collision_reason(ca, cb)
                if reason:
                    c_entry = {"a": ca.id, "b": cb.id, "reason": reason, "tier": tier_idx + 1}
                    t_conflicts.append(c_entry)
                    all_conflicts.append(c_entry)
        for card in t_cards:
            placed = False
            for batch in batches:
                if all(not cards_collide(card, by_id[other_id]) for other_id in batch):
                    batch.append(card.id)
                    placed = True
                    break
            if not placed:
                batches.append([card.id])
        tiers.append({
            "tier": tier_idx + 1,
            "cards": [c.id for c in t_cards],
            "batches": batches,
            "conflicts": t_conflicts,
            "waves": sorted({_card_wave(c) for c in t_cards}),
        })

    done_ids = {c.id for c in work_cards if str(c.state).lower() == "done"}

    def _deps_met(cid: str) -> bool:
        return not unmet_deps.get(cid) and all(d in done_ids for d in deps_map[cid])

    ready_now = [
        c.id for c in work_cards
        if str(c.state).lower() == "ready" and _deps_met(c.id)
    ]
    blocked_by_deps = [
        c.id for c in work_cards
        if str(c.state).lower() == "ready" and not _deps_met(c.id)
    ]

    return {
        "card_count": len(work_cards),
        "tiers": tiers,
        "edges": edges,
        "conflicts": all_conflicts,
        "ready_now": ready_now,
        "blocked_by_deps": blocked_by_deps,
        "unmet_deps": unmet_deps,
        "cards": [
            {
                "id": c.id,
                "title": c.title,
                "state": str(c.state).lower(),
                "wave": _card_wave(c),
                "tier": depth[c.id] + 1,
                "depends": raw_deps_map[c.id],
                "unmet_depends": unmet_deps.get(c.id, []),
                "touches": list(c.touches),
            }
            for c in work_cards
        ],
    }


def render_dag_ascii(dag: dict) -> str:
    """Render a crisp multi-wave ASCII DAG & parallel batch schedule."""
    if not dag.get("tiers"):
        return "(no active cards in DAG)"
    cards_by_id = {c["id"]: c for c in (dag.get("cards") or [])}
    lines = [
        f"Factory DAG: {dag['card_count']} card(s) · {len(dag['tiers'])} tier(s) · "
        f"{len(dag['ready_now'])} runnable now · {len(dag['conflicts'])} touches conflict(s)"
    ]
    for tier in dag["tiers"]:
        lines.append(f"  Tier {tier['tier']} (wave {','.join(str(w) for w in tier['waves'])}):")
        for b_idx, batch in enumerate(tier["batches"], start=1):
            batch_desc = "  ∥  ".join(
                f"{cid} [{cards_by_id.get(cid, {}).get('state', '?')}]" for cid in batch
            )
            lines.append(f"    Batch {b_idx}: {batch_desc}")
        for conf in tier["conflicts"]:
            lines.append(f"    ! serialized: {conf['a']} ↔ {conf['b']} ({conf['reason']})")
    return "\n".join(lines)


__all__ = ["build_card_dag", "render_dag_ascii"]
