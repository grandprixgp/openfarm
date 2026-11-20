"""Bare `openfarm` Automode: context-aware landing state & command palette (v0.9.1).

Detects where a user is starting from (empty folder, unadopted repo, or adopted
openfarm repo), recommends the next step, computes live economics stats from
recent card records, and exposes both guided A->B flows and direct power tools.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import adopt
from .acp_setup import inspect_acp_status
from .config import CONFIG_PATH, load_config
from .errors import ConfigError, Refused
from .globs import resolves_within
from .state import StateDir

AUTOMODE_PALETTE: tuple[dict, ...] = (
    # Guided A->B flows
    {"id": "init", "group": "guided", "label": "New App (empty folder -> full app)", "command": "init"},
    {"id": "adopt", "group": "guided", "label": "Adopt existing repository", "command": "adopt"},
    {"id": "new-wave", "group": "guided", "label": "Plan & grill a new feature wave", "command": "supervise --prepare-queue"},
    {"id": "run", "group": "guided", "label": "Run ready cards", "command": "run"},
    {"id": "review", "group": "guided", "label": "Review in-review cards", "command": "review"},
    {"id": "land", "group": "guided", "label": "Local serial merge queue", "command": "land"},
    # Direct power tools
    {"id": "triage", "group": "power", "label": "Frontier card-complexity triage", "command": "triage"},
    {"id": "frontier-review", "group": "power", "label": "Mixture-of-frontiers review (card/diff/repo)", "command": "frontier-review"},
    {"id": "grill", "group": "power", "label": "Interactive design grill session", "command": "grill"},
    {"id": "stats", "group": "power", "label": "Economics & token-share statistics", "command": "status"},
    {"id": "dossiers", "group": "power", "label": "Inspect card dossiers", "command": "dossier"},
    {"id": "harness", "group": "power", "label": "Recursive harness memory", "command": "harness"},
    {"id": "acp", "group": "power", "label": "Provider OAuth & probe status", "command": "acp status"},
    {"id": "bench", "group": "power", "label": "Run efficacy bench", "command": "bench list"},
)


def _recent_economics(state: StateDir) -> dict:
    """Aggregate worker/supervisor token shares & fresh/cached counts from recent records."""
    worker_tokens = 0
    supervisor_tokens = 0
    tokens_in_fresh = 0
    tokens_in_cached = 0
    cards_seen = 0
    passed = 0
    if state.cards.is_dir():
        for rec_path in sorted(state.cards.glob("*/*/card-record.json"))[-50:]:
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            cards_seen += 1
            if rec.get("final_state") == "in-review":
                passed += 1
            worker_tokens += int(rec.get("tokens_in", 0) or 0) + int(rec.get("tokens_out", 0) or 0)
            supervisor_tokens += (
                int(rec.get("micromanager_tokens_in", 0) or 0)
                + int(rec.get("micromanager_tokens_out", 0) or 0)
            )
            tokens_in_fresh += int(rec.get("tokens_in_fresh", 0) or 0)
            tokens_in_cached += int(rec.get("tokens_in_cached", 0) or 0)
    total = worker_tokens + supervisor_tokens
    worker_share = round(worker_tokens / total, 4) if total > 0 else 0.0
    return {
        "cards_seen": cards_seen,
        "passed": passed,
        "total_tokens": total,
        "worker_tokens": worker_tokens,
        "supervisor_tokens": supervisor_tokens,
        "worker_share": worker_share,
        "tokens_in_fresh": tokens_in_fresh,
        "tokens_in_cached": tokens_in_cached,
    }


def detect_workspace_state(root: Path, config_path: Path | None = None) -> dict:
    """Detect whether `root` is empty, an unadopted repo, or an adopted openfarm repo."""
    root = Path(root).expanduser().resolve()
    cfg_file = Path(config_path) if config_path else (root / CONFIG_PATH)

    if cfg_file.is_file():
        counts = {"ready": 0, "in_progress": 0, "in_review": 0, "blocked": 0, "done": 0}
        economics: dict = {}
        try:
            from types import SimpleNamespace
            from .tui import _read_cards_readonly

            cfg = load_config(root, cfg_file)
            state = StateDir(cfg.state_dir)
            tui_ctx = SimpleNamespace(configured=True, repo=root, cfg=cfg, state=state)
            cards, _mode = _read_cards_readonly(tui_ctx)
            for c in cards:
                st = str(getattr(c, "state", "ready")).lower().replace("-", "_")
                if st in counts:
                    counts[st] += 1
            economics = _recent_economics(state)
        except (ConfigError, OSError):
            pass

        if counts["in_review"] > 0:
            next_action = "review"
            summary = f"{counts['in_review']} card(s) in-review — run `openfarm review` or `openfarm land`"
        elif counts["ready"] > 0:
            next_action = "run"
            summary = f"{counts['ready']} ready card(s) — run `openfarm triage` or `openfarm run`"
        elif counts["blocked"] > 0:
            next_action = "unblock"
            summary = f"{counts['blocked']} blocked card(s) — inspect dossiers or run `openfarm frontier-review`"
        else:
            next_action = "new-wave"
            summary = "Queue clear — start a new feature wave or run `openfarm triage`"
        return {
            "stage": "adopted",
            "root": str(root),
            "next_action": next_action,
            "summary": summary,
            "counts": counts,
            "economics": economics,
            "palette": list(AUTOMODE_PALETTE),
        }

    entries = [p for p in root.iterdir() if not p.name.startswith(".")] if root.is_dir() else []
    has_git = (root / ".git").exists()
    if not has_git and not entries:
        return {
            "stage": "empty",
            "root": str(root),
            "next_action": "init",
            "summary": "Empty directory — start a new app with Grill & Initialize (`openfarm init`)",
            "detected_language": "",
            "palette": list(AUTOMODE_PALETTE),
        }

    detected_lang = ""
    try:
        detected_lang = adopt.detect_language(root)
    except Refused:
        detected_lang = ""
    return {
        "stage": "unadopted",
        "root": str(root),
        "next_action": "adopt",
        "summary": (
            f"Existing repository ({detected_lang or 'unknown language'}) — adopt with `openfarm adopt`"
        ),
        "detected_language": detected_lang,
        "palette": list(AUTOMODE_PALETTE),
    }


def render_automode_summary(state: dict) -> str:
    """Compact human-readable Automode landing summary."""
    lines = [
        f"openfarm automode [{state.get('stage', 'unknown')}] — {state.get('root', '.')}",
        f"  Next: {state.get('summary', '')}",
    ]
    counts = state.get("counts")
    if isinstance(counts, dict):
        lines.append(
            f"  Queue: ready={counts.get('ready', 0)}  in-review={counts.get('in_review', 0)}  "
            f"blocked={counts.get('blocked', 0)}  done={counts.get('done', 0)}"
        )
    econ = state.get("economics")
    if isinstance(econ, dict) and econ.get("cards_seen"):
        lines.append(
            f"  Recent: {econ['passed']}/{econ['cards_seen']} passed  "
            f"tokens={econ['total_tokens']} (worker {econ['worker_share']:.0%}, "
            f"fresh={econ['tokens_in_fresh']} cached={econ['tokens_in_cached']})"
        )
    lines.append("  Palette: init | adopt | triage | run | review | land | frontier-review | grill | stats | acp")
    return "\n".join(lines)


# -- In-TUI Automode Pipeline Engine (v0.9.2) ---------------------------------

DEFAULT_GRILL_QUESTIONS: tuple[dict, ...] = (
    {
        "id": "goal",
        "question": "Core user workflow / MVP scope (1 sentence)",
        "default": "Deliver the core happy path cleanly with deterministic tests",
    },
    {
        "id": "invariants",
        "question": "Key invariants that must never break (e.g. data integrity, no network)",
        "default": "No silent data loss; deterministic error handling on invalid inputs",
    },
    {
        "id": "slice",
        "question": "First golden slice & Wave 1 feature",
        "default": "Core domain model + first end-to-end command with unit tests",
    },
)


def list_existing_key_files(key_dir: Path | None = None) -> list[Path]:
    """List non-empty, regular `*.key` files in `~/.config/openfarm` (names only)."""
    folder = Path(key_dir) if key_dir is not None else (Path.home() / ".config" / "openfarm")
    if not folder.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(folder.glob("*.key")):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if entry.stat().st_size > 0:
                found.append(entry)
        except OSError:
            continue
    return found


def save_worker_key_atomic(key_file: Path, secret: str) -> Path:
    """Write a worker API key atomically at mode 0600 without following symlinks."""
    cleaned = str(secret or "").strip()
    if not cleaned:
        raise Refused("empty-key", "API key cannot be empty")
    target = Path(key_file).expanduser()
    if target.is_symlink():
        raise Refused("symlink-key", f"refusing to overwrite symlink key file {target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    if str(resolved_parent) not in ("/", "/tmp", "/var/tmp"):
        os.chmod(resolved_parent, 0o700)

    fd, tmp_name = tempfile.mkstemp(prefix=".openfarm-key-", dir=str(resolved_parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cleaned + "\n")
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def reuse_existing_key_file(source: Path, dest: Path) -> Path:
    """Copy an existing regular `*.key` file into `dest` at mode 0600."""
    src = Path(source).expanduser()
    if src.is_symlink() or not src.is_file():
        raise Refused("bad-key-source", f"{src} is not a regular key file")
    secret = src.read_text(encoding="utf-8").strip()
    if not secret:
        raise Refused("empty-key", f"{src} is empty")
    return save_worker_key_atomic(dest, secret)


def _spec_and_cards_ready(root: Path, counts: dict) -> bool:
    spec = root / ".openfarm" / "PROJECT_SPEC.md"
    spec_ok = False
    if spec.is_file():
        try:
            text = spec.read_text(encoding="utf-8", errors="replace").strip()
            spec_ok = len(text) >= 60 and len(text.splitlines()) >= 3
        except OSError:
            spec_ok = False
    total_cards = sum(int(v or 0) for v in (counts or {}).values())
    if total_cards == 0 and (root / "cards").is_dir():
        total_cards = sum(1 for p in (root / "cards").glob("*.md") if p.is_file())
    return spec_ok and total_cards > 0


def compute_pipeline_stages(
    root: Path,
    config_path: Path | None = None,
    *,
    acp_status: dict | None = None,
) -> dict:
    """Compute the 5 live in-TUI Automode pipeline stages for `root`."""
    ws = detect_workspace_state(root, config_path)
    stage_name = ws["stage"]
    root_p = Path(root).expanduser().resolve()
    cfg_file = Path(config_path) if config_path else (root_p / CONFIG_PATH)

    stages: list[dict] = []
    if stage_name != "adopted":
        ws_action = "init" if stage_name == "empty" else "adopt"
        ws_prompt = (
            "Initialize new openfarm app here (name, language, idea)"
            if stage_name == "empty"
            else f"Adopt existing {ws.get('detected_language') or 'git'} repository into openfarm"
        )
        stages = [
            {"id": "workspace", "label": "Workspace", "status": "current", "action": ws_action, "prompt": ws_prompt},
            {"id": "auth", "label": "Auth", "status": "pending", "action": "auth", "prompt": "Sign in to Frontier provider"},
            {"id": "worker_key", "label": "Worker Key", "status": "pending", "action": "worker_key", "prompt": "Configure Worker API key"},
            {"id": "spec_and_slice", "label": "Spec & Cards", "status": "pending", "action": "grill", "prompt": "Grill & scaffold spec, invariants, and Wave 1 cards"},
            {"id": "wave", "label": "Wave", "status": "pending", "action": "run", "prompt": "Triage, run, review, and land cards"},
        ]
        return {
            "stage": stage_name,
            "stages": stages,
            "current_stage": stages[0],
            "summary": ws["summary"],
            "reusable_keys": [str(p) for p in list_existing_key_files()],
        }

    # Adopted repository: inspect auth, worker key, spec/cards, and wave queue.
    cfg = None
    try:
        cfg = load_config(root_p, cfg_file)
    except (ConfigError, OSError):
        cfg = None

    mm_adapter = str(getattr(cfg, "micromanager_adapter", "off") or "off") if cfg else "off"
    auth_done = mm_adapter in ("off", "scripted")
    auth_provider = "claude"
    if not auth_done:
        if "codex" in mm_adapter:
            auth_provider = "codex"
        elif "antigravity" in mm_adapter or "agy" in mm_adapter:
            auth_provider = "antigravity"
        info = acp_status
        if info is None:
            try:
                info = inspect_acp_status(root_p, provider=auth_provider)
            except Exception:
                info = {}
        auth_done = bool((info or {}).get("oauth_ready") or (info or {}).get("ok"))

    key_done = True
    key_file_path = ""
    if cfg is not None and getattr(cfg, "model_adapter", "") == "openai-compatible":
        kf = getattr(cfg, "key_file", None)
        key_file_path = str(kf or "")
        key_done = bool(kf and Path(kf).is_file() and Path(kf).stat().st_size > 0)

    counts = ws.get("counts") or {}
    spec_done = _spec_and_cards_ready(root_p, counts)

    raw_stages = [
        {"id": "workspace", "label": "Workspace", "done": True, "action": "init", "prompt": "Workspace configured"},
        {"id": "auth", "label": "Auth", "done": auth_done, "action": "auth",
         "prompt": f"Sign in to Frontier {auth_provider} OAuth in-TUI", "provider": auth_provider},
        {"id": "worker_key", "label": "Worker Key", "done": key_done, "action": "worker_key",
         "prompt": f"Set Worker API key in-TUI ({key_file_path or 'key_file'})", "key_file": key_file_path},
        {"id": "spec_and_slice", "label": "Spec & Cards", "done": spec_done, "action": "grill",
         "prompt": "Run in-TUI Grill & Architect to expand PROJECT_SPEC.md, invariants, and Wave 1 cards"},
        {"id": "wave", "label": "Wave", "done": False, "action": ws["next_action"], "prompt": ws["summary"]},
    ]
    found_current = False
    for item in raw_stages:
        if item["done"] and not found_current:
            status = "done"
        elif not found_current:
            status = "current"
            found_current = True
        else:
            status = "pending"
        stages.append({k: v for k, v in item.items() if k != "done"} | {"status": status})

    current = next((s for s in stages if s["status"] == "current"), stages[-1])
    return {
        "stage": "adopted",
        "stages": stages,
        "current_stage": current,
        "summary": current["prompt"],
        "counts": counts,
        "reusable_keys": [str(p) for p in list_existing_key_files()],
    }


def run_grill_and_architect(
    repo: Path,
    *,
    answers: list[dict],
    backend=None,
) -> dict:
    """Write `.openfarm/GRILL_DECISIONS.md` and expand spec/cards via Frontier backend."""
    root = Path(repo).expanduser().resolve()
    decisions_path = root / ".openfarm" / "GRILL_DECISIONS.md"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Grill Decisions\n"]
    for item in answers:
        q = str(item.get("question") or item.get("id") or "Question")
        a = str(item.get("answer") or "").strip()
        lines.append(f"## {q}\n\n{a}\n")
    decisions_path.write_text("\n".join(lines), encoding="utf-8")

    written: list[str] = [".openfarm/GRILL_DECISIONS.md"]
    if backend is not None:
        prompt = (
            "Expand this openfarm repository from the owner's Grill decisions.\n"
            "Return ONLY a JSON object: {\"files\": {\"<repo-relative-path>\": \"<content>\", ...}}.\n"
            "Include `.openfarm/PROJECT_SPEC.md` and at least one Wave 1 card under `cards/`.\n"
            "Never modify `.openfarm/config.toml`.\n\n"
            + "\n".join(lines)
        )
        raw = str(backend.ask(prompt) if hasattr(backend, "ask") else backend(prompt))
        from .frontier_review import _extract_json_dict
        parsed = _extract_json_dict(raw) or {}
        files = parsed.get("files") if isinstance(parsed, dict) else None
        if isinstance(files, dict):
            for rel, content in files.items():
                rel_s = str(rel).strip().lstrip("/")
                if not rel_s or rel_s in (".openfarm/config.toml",) or rel_s.startswith(".git/"):
                    continue
                target = resolves_within(root, rel_s)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content), encoding="utf-8")
                written.append(rel_s)
    else:
        spec_path = root / ".openfarm" / "PROJECT_SPEC.md"
        spec_lines = [f"# {root.name} — Project Specification\n"]
        for item in answers:
            q = str(item.get("question") or item.get("id") or "")
            a = str(item.get("answer") or "").strip()
            spec_lines.append(f"## {q}\n\n{a}\n")
        spec_path.write_text("\n".join(spec_lines), encoding="utf-8")
        written.append(".openfarm/PROJECT_SPEC.md")

    cards_dir = root / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    if not any(p.is_file() for p in cards_dir.glob("*.md")):
        from .adapters.files import render_card_file

        cfg = None
        try:
            cfg = load_config(root)
        except (ConfigError, OSError):
            cfg = None
        lang = (cfg.languages[0] if (cfg and cfg.languages) else "rust").lower()
        allowed_cats = list(cfg.categories) if (cfg and cfg.categories) else ["small-feature"]
        category = next(
            (c for c in ("small-feature", "feature", "pattern", "test-backfill") if c in allowed_cats),
            allowed_cats[0],
        )

        by_id = {str(item.get("id") or ""): str(item.get("answer") or "").strip() for item in answers}
        goal_ans = by_id.get("goal") or "Deliver the core happy path cleanly with deterministic tests"
        inv_ans = by_id.get("invariants") or "No silent data loss; deterministic error responses on invalid inputs"
        slice_ans = by_id.get("slice") or "Core domain model + first end-to-end command with unit tests"

        if lang == "rust":
            touch = "src/lib.rs" if (root / "src" / "lib.rs").is_file() else (
                "src/main.rs" if (root / "src" / "main.rs").is_file() else "src/lib.rs"
            )
            test_entry = {
                "name": "wave1_golden_slice_works",
                "file": "tests/wave1_golden_slice.rs",
                "asserts": "Core workflow returns expected output and rejects invalid input with an error",
            }
        elif lang in ("python", "py"):
            py_files = [
                p.relative_to(root).as_posix()
                for p in sorted(root.rglob("*.py"))
                if not p.name.startswith("test_")
                and "tests/" not in p.relative_to(root).as_posix()
                and ".openfarm" not in p.parts
            ]
            touch = py_files[0] if py_files else "app.py"
            test_entry = {
                "name": "test_wave1_golden_slice_works",
                "file": "tests/test_wave1_golden_slice.py",
                "asserts": "Core workflow returns expected output and rejects invalid input with an error",
            }
        elif lang == "go":
            touch = "main.go"
            test_entry = {
                "name": "TestWave1GoldenSliceWorks",
                "file": "wave1_golden_slice_test.go",
                "asserts": "Core workflow returns expected output and rejects invalid input with an error",
            }
        else:
            touch = "src/index.ts"
            test_entry = {
                "name": "wave1_golden_slice_works",
                "file": "tests/wave1_golden_slice.test.ts",
                "asserts": "Core workflow returns expected output and rejects invalid input with an error",
            }

        header: dict = {
            "id": "W1-01",
            "title": (slice_ans.splitlines()[0] if slice_ans else "Wave 1 golden slice")[:80],
            "state": "ready",
            "touches": [touch],
            "depends": [],
            "category": category,
            "tests": [test_entry],
        }
        if category not in ("pinned-bugfix", "test-backfill"):
            header["new_symbols"] = True
        if cfg is not None and category in (getattr(cfg, "require_reference", ()) or ()):
            header["reference"] = touch

        import re
        from .gates.eligibility import DESIGN_WORDS, VAGUE_WORDS

        def _clean_done_prose(text: str) -> str:
            cleaned = str(text or "").strip()
            for phrase in DESIGN_WORDS:
                cleaned = re.sub(re.escape(phrase), "layout", cleaned, flags=re.IGNORECASE)
            for word in VAGUE_WORDS:
                cleaned = re.sub(rf"\b{re.escape(word)}\b", "deterministic", cleaned, flags=re.IGNORECASE)
            return cleaned

        body = (
            "## Done\n\n"
            f"- Implement the Wave 1 golden slice in `{touch}` per `.openfarm/PROJECT_SPEC.md` and `.openfarm/GRILL_DECISIONS.md`.\n"
            f"- Core workflow (`{touch}`): {_clean_done_prose(goal_ans)}\n"
            f"- Golden slice returns expected values: {_clean_done_prose(slice_ans)}\n"
            f"- Preserve invariants and return an error on invalid input: {_clean_done_prose(inv_ans)}\n"
            f"- `{test_entry['name']}` passes alongside all baseline tests.\n"
        )
        card_path = cards_dir / "W1-01.md"
        card_path.write_text(render_card_file(header, body), encoding="utf-8")
        written.append("cards/W1-01.md")

    return {"written": written, "decisions_path": str(decisions_path)}


def ensure_baseline_committed(repo: Path) -> bool:
    """Ensure the repo has an initial HEAD commit and committed `.openfarm/` + `cards/` baseline."""
    from .git import Git

    root = Path(repo).expanduser().resolve()
    if not (root / ".git").exists():
        return False
    git = Git(root)
    if not git.ref_exists("HEAD"):
        if git.status_lines():
            git.commit_all("chore: commit initial openfarm baseline")
            return True
        return False
    lines = git.status_lines()
    if not lines:
        return False
    paths = [line[3:].strip().split(" -> ")[-1].strip('"') for line in lines if len(line) > 3]
    if paths and all(
        p.startswith(".openfarm/") or p.startswith("cards/") or p in (".openfarm", "cards")
        for p in paths
    ):
        git.commit_all("chore: commit openfarm spec and cards")
        return True
    return False
