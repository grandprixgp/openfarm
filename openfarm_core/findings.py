"""Findings Inbox core (v0.10.0): inspect, filter, decide, fold, and promote worker findings.

Worker-filed findings live as `cards/FIND-<n>.md` on the `openfarm-cards` branch (or
under `cards/` before the first run). Decisions (`next-wave`, `dismissed`, `folded`,
`promoted`) are stored durably in each finding's TOML header so they survive restarts
and keep dismissed/folded findings out of the active backlog.
"""

from __future__ import annotations

import json
import difflib
import re
from pathlib import Path
from types import SimpleNamespace

from .adapters.files import FileCardSource, parse_card_file
from .config import Config
from .errors import Invalid, Refused
from .git import Git
from .state import StateDir

_FIND_NUM_RE = re.compile(r"^FIND-(\d+)$")


def is_finding_id_or_header(card_id: str, fields: dict | None = None) -> bool:
    if str(card_id or "").startswith("FIND-"):
        return True
    if isinstance(fields, dict) and str(fields.get("from_card") or "").strip():
        return True
    return False


def _finding_sort_key(item: dict) -> tuple[str, int, str]:
    created = str(item.get("created_at") or "")
    cid = str(item.get("id") or "")
    m = _FIND_NUM_RE.match(cid)
    num = int(m.group(1)) if m else 0
    return (created, num, cid)


def _latest_run_context(state: StateDir) -> tuple[str, set[str]]:
    """Return `(latest_started_at, finding_ids_from_latest_run)` from state_dir."""
    latest_started = ""
    latest_ids: set[str] = set()
    latest_run_id = ""
    if state.runs.is_dir():
        run_dirs = sorted([p for p in state.runs.iterdir() if p.is_dir()], key=lambda p: p.name)
        for rdir in reversed(run_dirs):
            summary_p = rdir / "summary.json"
            if summary_p.is_file():
                try:
                    data = json.loads(summary_p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    latest_run_id = str(data.get("run_id") or rdir.name)
                    latest_started = str(data.get("started_at") or data.get("finished_at") or "")
                    break
            else:
                latest_run_id = rdir.name
                break
    if state.cards.is_dir() and latest_run_id:
        for rec_p in state.cards.glob("*/" + latest_run_id + "/card-record.json"):
            try:
                rec = json.loads(rec_p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict):
                for fid in rec.get("findings_filed") or []:
                    if str(fid).strip():
                        latest_ids.add(str(fid).strip())
    return latest_started, latest_ids


def _read_raw_cards_readonly(repo: Path, cfg: Config) -> list[dict]:
    """Read raw card headers & bodies without mutating git worktrees."""
    spec = cfg.cards_adapter
    folder = (spec[len("files:") :].strip() or "cards").rstrip("/") if spec.startswith("files:") else "cards"
    state = StateDir(cfg.state_dir)
    git = Git(repo)
    cards_branch = getattr(cfg, "cards_branch", "openfarm-cards")

    entries: list[dict] = []
    if git.ref_exists(f"refs/heads/{cards_branch}"):
        ls = git._run(["ls-tree", "--name-only", f"refs/heads/{cards_branch}", f"{folder}/"], check=False)
        if ls.returncode == 0:
            for rel in sorted(line.strip() for line in ls.stdout.splitlines() if line.strip().endswith(".md")):
                blob = git._run(["show", f"{cards_branch}:{rel}"], check=False)
                if blob.returncode != 0:
                    continue
                try:
                    header, body = parse_card_file(blob.stdout)
                except (Invalid, OSError):
                    continue
                cid = str(header.get("id") or Path(rel).stem)
                entries.append({"id": cid, "header": header, "body": body})
        return entries

    scan_dir = (state.root / "cards-source" / folder)
    if not scan_dir.is_dir():
        scan_dir = repo / folder
    if not scan_dir.is_dir():
        return []
    for path in sorted(scan_dir.glob("*.md")):
        try:
            header, body = parse_card_file(path.read_text(encoding="utf-8", errors="replace"))
        except (Invalid, OSError):
            continue
        cid = str(header.get("id") or path.stem)
        entries.append({"id": cid, "header": header, "body": body})
    return entries


def _body_preview(body: str, limit: int = 140) -> str:
    flat = " ".join(line.strip() for line in str(body or "").splitlines() if line.strip())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)].rstrip() + "…"


def list_findings(
    repo: Path,
    cfg: Config,
    *,
    state_filter: str = "waiting",
    from_card: str = "",
    query: str = "",
    deduplicate: bool = False,
) -> dict:
    """List findings newest-first with waiting / new / picked / dismissed / folded counts."""
    raw_cards = _read_raw_cards_readonly(repo, cfg)
    titles_by_id = {c["id"]: str(c["header"].get("title") or c["id"]) for c in raw_cards}
    states_by_id = {c["id"]: str(c["header"].get("state") or "").strip().lower().replace("-", "_")
                    for c in raw_cards}
    deps_by_id = {c["id"]: [str(d) for d in (c["header"].get("depends") or [])] for c in raw_cards}

    def holds_up(card_id: str) -> list[str]:
        """The blocked card a finding came from, then the cards waiting behind that one."""
        if states_by_id.get(card_id) != "blocked":
            return []
        return [card_id] + [cid for cid, deps in deps_by_id.items()
                            if card_id in deps and states_by_id.get(cid) in ("ready", "backlog")]
    state = StateDir(cfg.state_dir)
    latest_started, latest_ids = _latest_run_context(state)

    all_findings: list[dict] = []
    counts = {
        "waiting": 0,
        "new_since_last_run": 0,
        "picked": 0,
        "dismissed": 0,
        "folded": 0,
        "promoted": 0,
        "total": 0,
    }

    for entry in raw_cards:
        cid = entry["id"]
        hdr = entry["header"]
        if not is_finding_id_or_header(cid, hdr):
            continue
        body = str(entry["body"] or "").strip()
        raw_state = str(hdr.get("state") or "backlog").strip().lower()
        decision = str(hdr.get("finding_decision") or "").strip().lower()
        if not decision:
            if raw_state in ("dismissed", "folded", "promoted"):
                decision = raw_state
            elif raw_state == "finding-picked":
                decision = "next-wave"
            else:
                decision = "waiting"

        created_at = str(hdr.get("created_at") or "")
        is_new = (cid in latest_ids) or (bool(latest_started) and bool(created_at) and created_at >= latest_started)
        src_id = str(hdr.get("from_card") or "")
        item = {
            "id": cid,
            "title": str(hdr.get("title") or cid),
            "body": body,
            "body_preview": _body_preview(body),
            "from_card": src_id,
            "from_card_title": titles_by_id.get(src_id, src_id),
            "from_card_state": states_by_id.get(src_id, ""),
            "holds_up": holds_up(src_id),
            "created_at": created_at,
            "state": raw_state,
            "decision": decision,
            "reason": str(hdr.get("finding_reason") or ""),
            "folded_into": str(hdr.get("folded_into") or ""),
            "promoted_card": str(hdr.get("promoted_card") or ""),
            "decided_at": str(hdr.get("finding_decided_at") or ""),
            "is_new": bool(is_new),
        }
        all_findings.append(item)
        counts["total"] += 1
        if decision == "waiting":
            counts["waiting"] += 1
            if is_new:
                counts["new_since_last_run"] += 1
        elif decision == "next-wave":
            counts["picked"] += 1
        elif decision == "dismissed":
            counts["dismissed"] += 1
        elif decision == "folded":
            counts["folded"] += 1
        elif decision == "promoted":
            counts["promoted"] += 1

    # Sort newest first
    all_findings.sort(key=_finding_sort_key, reverse=True)

    filt = str(state_filter or "waiting").strip().lower()
    src_filter = str(from_card or "").strip()
    q = str(query or "").strip().lower()

    filtered: list[dict] = []
    for item in all_findings:
        if filt == "waiting" and item["decision"] != "waiting":
            continue
        elif filt == "picked" and item["decision"] != "next-wave":
            continue
        elif filt in ("dismissed", "folded", "promoted") and item["decision"] != filt:
            continue
        if src_filter and item["from_card"] != src_filter:
            continue
        if q:
            hay = f"{item['id']} {item['from_card']} {item['from_card_title']} {item['title']} {item['body']}".lower()
            if q not in hay:
                continue
        filtered.append(item)

    if deduplicate:
        # A retried card files the same finding again in slightly different words
        # ("is unspecified" / "is not specified"), so near-identical titles from one
        # card fold into the newest. 0.65 sits between real repeats (0.70 and up) and
        # different findings from one card (0.51 and down) in the dogfood run.
        def norm(title: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()

        deduped: list[dict] = []
        for item in filtered:
            title = norm(str(item["title"]))
            group = next(
                (g for g in deduped if g["from_card"] == item["from_card"] and any(
                    difflib.SequenceMatcher(None, title, norm(seen)).ratio() >= 0.65
                    for seen in g["duplicate_titles"])),
                None)
            if group is None:
                group = dict(item, duplicate_count=0, duplicate_ids=[], duplicate_titles=[])
                deduped.append(group)
            group["duplicate_count"] += 1
            group["duplicate_ids"].append(item["id"])
            group["duplicate_titles"].append(str(item["title"]))
        filtered = deduped

    return {
        "findings": filtered,
        "counts": counts,
    }


def findings_for_card(repo: Path, cfg: Config, card_id: str) -> list[dict]:
    """Return every finding filed by `card_id` (newest first) for display in Review."""
    return list_findings(repo, cfg, state_filter="all", from_card=card_id)["findings"]


def picked_findings_for_planning(repo: Path, cfg: Config) -> list[dict]:
    """Return findings picked for the next wave (`decision == 'next-wave'`)."""
    return list_findings(repo, cfg, state_filter="picked")["findings"]


def _open_file_source(repo: Path, cfg: Config) -> FileCardSource:
    spec = cfg.cards_adapter
    if not spec.startswith("files:"):
        raise Refused("unsupported-card-source", f"findings decisions require files: card adapter, got {spec!r}")
    folder = (spec[len("files:") :].strip() or "cards").rstrip("/")
    state = StateDir(cfg.state_dir)
    src = FileCardSource(
        repo,
        folder,
        branch=getattr(cfg, "cards_branch", "openfarm-cards"),
        worktree=state.root / "cards-source",
        base_branch=cfg.base_branch,
        fields_block=getattr(cfg, "fields_block", ""),
    )
    src.prepare()
    return src


def decide_finding(
    repo: Path,
    cfg: Config,
    finding_id: str,
    decision: str,
    *,
    reason: str = "",
    target_card: str = "",
    promoted_card: str = "",
) -> dict:
    """Persist a decision (`next-wave`, `dismissed`, `folded`, `promoted`) on a finding."""
    src = _open_file_source(repo, cfg)
    return src.decide_finding(
        finding_id,
        decision,
        reason=reason,
        target_card=target_card,
        promoted_card=promoted_card,
    )


def bulk_decide_findings(
    repo: Path,
    cfg: Config,
    *,
    decision: str,
    from_card: str = "",
    finding_ids: list[str] | None = None,
    reason: str = "",
) -> dict:
    """Apply `decision` (`next-wave` or `dismissed`) across matching waiting findings."""
    if decision not in ("next-wave", "dismissed"):
        raise Refused("bad-bulk-decision", f"bulk decision must be 'next-wave' or 'dismissed', got {decision!r}")
    if finding_ids:
        targets = [str(fid).strip() for fid in finding_ids if str(fid).strip()]
    else:
        waiting = list_findings(repo, cfg, state_filter="waiting", from_card=from_card)["findings"]
        targets = [item["id"] for item in waiting]
    src = _open_file_source(repo, cfg)
    decided: list[dict] = []
    for fid in targets:
        decided.append(src.decide_finding(fid, decision, reason=reason))
    return {"decision": decision, "count": len(decided), "findings": decided}
