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
    if not entries:
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


def is_placeholder_endpoint(url: str) -> bool:
    """True when `[model] endpoint` is still the init/adopt placeholder or empty."""
    cleaned = str(url or "").strip().lower()
    return (not cleaned) or ("api.example.invalid" in cleaned)


def configure_worker_endpoint(config_path: Path, endpoint: str, model: str = "") -> Path:
    """Update `[model]` endpoint, allowed_endpoints, and optional role models in `.openfarm/config.toml`."""
    import re

    ep = str(endpoint or "").strip().rstrip("/")
    if not (ep.startswith("https://") or ep.startswith("http://")) or is_placeholder_endpoint(ep):
        raise Refused("bad-endpoint", "Worker endpoint must be a real http(s):// URL")
    mdl = str(model or "").strip()
    cfg_p = Path(config_path).expanduser().resolve()
    raw = cfg_p.read_text(encoding="utf-8")
    out_lines: list[str] = []
    in_model = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_model = stripped == "[model]"
            out_lines.append(line)
            continue
        if in_model:
            if re.match(r"^\s*endpoint\s*=", line):
                out_lines.append(f'endpoint = "{ep}"')
                continue
            if re.match(r"^\s*allowed_endpoints\s*=", line):
                out_lines.append(f'allowed_endpoints = ["{ep}"]')
                continue
            if mdl and re.match(r"^\s*(test_writer|implementer|checker|planner)\s*=", line):
                line = re.sub(r'model\s*=\s*"[^"]*"', f'model = "{mdl}"', line)
        out_lines.append(line)
    updated = "\n".join(out_lines) + ("\n" if raw.endswith("\n") else "")
    cfg_p.write_text(updated, encoding="utf-8")
    # Verify the updated config still parses cleanly.
    load_config(cfg_p.parent.parent, cfg_p)
    return cfg_p


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
    worker_prompt = "Configure Worker API key, endpoint & model in-TUI"
    if cfg is not None and getattr(cfg, "model_adapter", "") == "openai-compatible":
        kf = getattr(cfg, "key_file", None)
        key_file_path = str(kf or "")
        key_present = bool(kf and Path(kf).is_file() and Path(kf).stat().st_size > 0)
        ep_ok = not is_placeholder_endpoint(str(getattr(cfg, "endpoint", "") or ""))
        key_done = key_present and ep_ok
        if key_present and not ep_ok:
            worker_prompt = "Configure Worker endpoint & model in-TUI (currently placeholder)"
        else:
            worker_prompt = f"Set Worker API key & endpoint in-TUI ({key_file_path or 'key_file'})"

    counts = ws.get("counts") or {}
    spec_done = _spec_and_cards_ready(root_p, counts)

    raw_stages = [
        {"id": "workspace", "label": "Workspace", "done": True, "action": "init", "prompt": "Workspace configured"},
        {"id": "auth", "label": "Auth", "done": auth_done, "action": "auth",
         "prompt": f"Sign in to Frontier {auth_provider} OAuth in-TUI", "provider": auth_provider},
        {"id": "worker_key", "label": "Worker Key", "done": key_done, "action": "worker_key",
         "prompt": worker_prompt, "key_file": key_file_path},
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


class _AcpArchitectBackend:
    """Thin Frontier ACP Architect wrapper over `open_acp_worker`."""

    def __init__(self, worker, model: str = "") -> None:
        self._worker = worker
        self._model = model

    def ask(self, prompt: str) -> str:
        comp = self._worker.complete(
            "architect",
            self._model,
            0.2,
            [{"role": "user", "content": prompt}],
            system_prompt=(
                "You are a senior Frontier Software Architect inside Openfarm. "
                "Return ONLY valid JSON of shape {\"files\": {\"<repo-relative-path>\": \"<content>\"}} "
                "matching the repository's configured language stack. Never emit files for another language."
            ),
        )
        return str(comp.content or "")

    def close(self) -> None:
        self._worker.close()


def open_architect_backend(repo: Path, cfg=None) -> tuple[object | None, str]:
    """Open a live Frontier ACP Architect backend if OAuth is ready (returns `(backend, reason)`)."""
    from .adapters.acp import is_acp_adapter, normalize_acp_provider
    from .adapters.claude_worker import open_acp_worker
    from .providers import load_global_providers

    root = Path(repo).expanduser().resolve()
    if cfg is None:
        try:
            cfg = load_config(root)
        except (ConfigError, OSError) as exc:
            return None, f"config unavailable: {exc}"

    mm_adapter = str(getattr(cfg, "micromanager_adapter", "") or "")
    if is_acp_adapter(mm_adapter):
        provider = normalize_acp_provider(mm_adapter)
    else:
        gp = load_global_providers()
        provider = str(gp.get("frontier", {}).get("provider") or "claude").strip().lower()

    if provider == "antigravity":
        return None, "antigravity architect requires explicit interactive run"

    try:
        status = inspect_acp_status(root, provider=provider)
    except Exception as exc:
        return None, f"{provider} status check failed: {exc}"
    if not (status.get("oauth_ready") or status.get("ok")):
        return None, f"{provider} ACP OAuth is not signed in"

    try:
        worker = open_acp_worker(cfg, provider=provider)
        mm_model = str(getattr(getattr(cfg, "micromanager", None), "model", "") or "")
        return _AcpArchitectBackend(worker, model=mm_model), f"{provider} ACP"
    except Exception as exc:
        return None, f"{provider} ACP unavailable: {exc}"


def run_grill_and_architect(
    repo: Path,
    *,
    answers: list[dict],
    backend=None,
) -> dict:
    """Write `.openfarm/GRILL_DECISIONS.md` and expand spec/cards via Frontier backend."""
    from .globs import matches_any

    root = Path(repo).expanduser().resolve()
    cfg = None
    try:
        cfg = load_config(root)
    except (ConfigError, OSError):
        cfg = None
    lang = (cfg.languages[0] if (cfg and cfg.languages) else "rust").lower()
    never_touch = list(getattr(cfg, "never_touch", ()) or ()) if cfg else []
    forbidden = list(getattr(cfg, "forbidden_files", ()) or ()) if cfg else []

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
        existing_files = [
            p.relative_to(root).as_posix()
            for p in sorted(root.rglob("*"))
            if p.is_file()
            and ".git" not in p.parts
            and ".openfarm" not in p.parts
            and "node_modules" not in p.parts
            and "target" not in p.parts
        ][:40]
        prompt = (
            f"Expand this {lang} Openfarm repository from the owner's Grill decisions.\n"
            f"Language stack: {lang} (STRICT: generate ONLY {lang} source/test files — NEVER emit .rs or Cargo.toml in a non-Rust project).\n"
            f"Locked never_touch files (DO NOT modify or list in card touches): {never_touch}\n"
            f"Existing repository files: {existing_files}\n"
            "Return ONLY a JSON object: {\"files\": {\"<repo-relative-path>\": \"<content>\", ...}}.\n"
            "Include:\n"
            "1. `.openfarm/PROJECT_SPEC.md` (concrete architecture & vertical slice)\n"
            f"2. One clean {lang} golden reference vertical slice file and passing baseline test matching the stack\n"
            "3. 2 to 5 scoped, non-overlapping Wave 1 cards under `cards/W1-01.md`.. using TOML +++ headers "
            "(id, title, state=\"ready\", category=\"small-feature\", touches, reference, new_symbols=true, depends, tests=[{name, file, asserts}]) "
            "and concrete `## Done` sections without vague adjectives.\n"
            "Never modify `.openfarm/config.toml` or `never_touch` files.\n\n"
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
                if lang != "rust" and (rel_s.endswith(".rs") or rel_s.endswith("Cargo.toml") or rel_s.endswith("Cargo.lock")):
                    continue
                if matches_any(rel_s, never_touch) or (not rel_s.startswith(".openfarm/") and matches_any(rel_s, forbidden)):
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
    existing_md = [p for p in cards_dir.glob("*.md") if p.is_file() and not p.stem.upper().startswith("FIND-")]
    should_write_fallback = not existing_md
    if not should_write_fallback and backend is None and len(existing_md) == 1 and existing_md[0].name == "W1-01.md":
        try:
            from .adapters.files import parse_card_file
            hdr, _ = parse_card_file(existing_md[0].read_text(encoding="utf-8", errors="replace"))
            if str(hdr.get("state") or "ready").lower() in ("ready", "backlog"):
                should_write_fallback = True
        except (OSError, ValueError):
            should_write_fallback = False
    if should_write_fallback:
        from .adapters.files import render_card_file

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


def _default_touch_and_test(root: Path, lang: str, card_id: str) -> tuple[str, dict]:
    """Choose a non-invariant default touch file and valid named test entry for `lang`."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", str(card_id or "wave1").lower()).strip("_") or "wave1"
    if lang == "rust":
        touch = "src/lib.rs" if (root / "src" / "lib.rs").is_file() else (
            "src/main.rs" if (root / "src" / "main.rs").is_file() else "src/lib.rs"
        )
        return touch, {
            "name": f"{slug}_golden_slice_works",
            "file": f"tests/{slug}_golden_slice.rs",
            "asserts": "Core workflow returns expected output and rejects invalid input with an error",
        }
    if lang in ("python", "py"):
        py_files = [
            p.relative_to(root).as_posix()
            for p in sorted(root.rglob("*.py"))
            if not p.name.startswith("test_")
            and "invariants.py" not in p.name
            and "tests/" not in p.relative_to(root).as_posix()
            and ".openfarm" not in p.parts
        ]
        touch = py_files[0] if py_files else "app.py"
        return touch, {
            "name": f"test_{slug}_works",
            "file": f"tests/test_{slug}.py",
            "asserts": "Core workflow returns expected output and rejects invalid input with an error",
        }
    if lang == "go":
        return "main.go", {
            "name": f"Test{slug.title().replace('_', '')}Works",
            "file": f"{slug}_test.go",
            "asserts": "Core workflow returns expected output and rejects invalid input with an error",
        }
    return "src/index.ts", {
        "name": f"{slug}_works",
        "file": f"tests/{slug}.test.ts",
        "asserts": "Core workflow returns expected output and rejects invalid input with an error",
    }


def _sanitize_done_prose(text: str) -> str:
    import re
    from .gates.eligibility import DESIGN_WORDS, VAGUE_WORDS

    cleaned = str(text or "").strip()
    for phrase in DESIGN_WORDS:
        cleaned = re.sub(re.escape(phrase), "layout", cleaned, flags=re.IGNORECASE)
    for word in VAGUE_WORDS:
        cleaned = re.sub(rf"\b{re.escape(word)}\b", "deterministic", cleaned, flags=re.IGNORECASE)
    return cleaned


def _repair_card_for_eligibility(
    root: Path,
    cfg,
    header: dict,
    body: str,
) -> tuple[dict, str, bool]:
    """Repair a card header + Done body deterministically until `check_eligibility` passes."""
    from .cards import Card
    from .gates.eligibility import KNOWN_CATEGORIES, check_eligibility
    from .globs import matches_any

    h = dict(header)
    b = str(body or "").strip()
    cid = str(h.get("id") or "W1-01")
    lang = (cfg.languages[0] if (cfg and cfg.languages) else "rust").lower()
    default_touch, default_test = _default_touch_and_test(root, lang, cid)

    def _to_card(hd: dict, bd: str) -> Card:
        return Card(
            id=str(hd.get("id") or cid),
            title=str(hd.get("title") or cid),
            state=str(hd.get("state") or "ready"),
            done_text=bd,
            touches=[str(t) for t in (hd.get("touches") or [])],
            deps=[str(d) for d in (hd.get("depends") or hd.get("deps") or [])],
            fields=dict(hd),
        )

    elig = check_eligibility(_to_card(h, b), cfg, worktree=root)
    if elig.ok:
        return h, b, False

    allowed_cats = list(cfg.categories) if (cfg and cfg.categories) else ["small-feature"]
    cat = str(h.get("category") or "")
    if cat not in KNOWN_CATEGORIES or cat not in allowed_cats:
        cat = next(
            (c for c in ("small-feature", "feature", "pattern", "test-backfill") if c in allowed_cats),
            allowed_cats[0],
        )
        h["category"] = cat

    touches = [
        str(t) for t in (h.get("touches") or [])
        if str(t).strip()
        and not matches_any(str(t), getattr(cfg, "never_touch", []) or [])
        and not matches_any(str(t), getattr(cfg, "forbidden_files", []) or [])
    ]
    if not touches:
        touches = [default_touch]
    if h.get("new_symbols") and len(touches) > 1:
        touches = [touches[0]]
    h["touches"] = touches[: max(1, int(getattr(cfg, "max_files", 6) or 6))]

    if cat in (getattr(cfg, "require_reference", ()) or ()) and not h.get("reference"):
        h["reference"] = h["touches"][0]
    if h.get("reference") and (
        matches_any(str(h["reference"]), getattr(cfg, "never_touch", []) or [])
        or matches_any(str(h["reference"]), getattr(cfg, "forbidden_files", []) or [])
    ):
        h["reference"] = h["touches"][0]

    if cat != "mechanical" and not (h.get("tests") or []):
        h["tests"] = [default_test]
    if cat in ("pinned-bugfix", "test-backfill") and h.get("new_symbols"):
        h.pop("new_symbols", None)

    b = _sanitize_done_prose(b)
    if not b:
        b = f"## Done\n\n- Implement `{h['touches'][0]}` so it returns expected values and returns an error on invalid input."
    elif not any(m in b for m in ("`", "==", "->", "returns", "assert", "error", "matches!", "equals")):
        b = b.rstrip() + f"\n- `{h['touches'][0]}` returns expected output and returns an error on invalid input.\n"

    return h, b, True


def draft_wave_plan(
    repo: Path,
    *,
    answers: list[dict],
    backend=None,
    max_repairs: int = 2,
    include_picked_findings: bool = True,
) -> dict:
    """Run Grill + Architect, incorporate picked findings, auto-repair eligibility, and triage cards."""
    from .adapters.files import parse_card_file, render_card_file
    from .cards import Card
    from .findings import decide_finding, is_finding_id_or_header, picked_findings_for_planning
    from .gates.eligibility import check_eligibility
    from .triage import triage_card_heuristic

    root = Path(repo).expanduser().resolve()
    res = run_grill_and_architect(root, answers=answers, backend=backend)
    written = list(res.get("written") or [])

    cfg = load_config(root)
    cards_dir = root / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    lang = (cfg.languages[0] if cfg.languages else "rust").lower()

    promoted_findings: list[str] = []
    if include_picked_findings:
        picked = picked_findings_for_planning(root, cfg)
        for item in picked:
            fid = item["id"]
            new_cid = f"W-{fid}"
            card_path = cards_dir / f"{new_cid}.md"
            if not card_path.exists():
                touch, test_entry = _default_touch_and_test(root, lang, new_cid)
                allowed_cats = list(cfg.categories) or ["small-feature"]
                cat = next(
                    (c for c in ("pinned-bugfix", "small-feature", "feature") if c in allowed_cats),
                    allowed_cats[0],
                )
                header: dict = {
                    "id": new_cid,
                    "title": str(item["title"])[:80],
                    "state": "ready",
                    "touches": [touch],
                    "depends": [],
                    "category": cat,
                    "tests": [test_entry],
                }
                if cat in (getattr(cfg, "require_reference", ()) or ()):
                    header["reference"] = touch
                body = (
                    "## Done\n\n"
                    f"- Address worker finding `{fid}` (from `{item['from_card']}`): {_sanitize_done_prose(item['title'])}.\n"
                    f"- Details: {_sanitize_done_prose(item['body'] or item['title'])}\n"
                    f"- `{test_entry['name']}` returns expected values and verifies an error on invalid input.\n"
                )
                card_path.write_text(render_card_file(header, body), encoding="utf-8")
                written.append(f"cards/{new_cid}.md")
            try:
                decide_finding(root, cfg, fid, "promoted", promoted_card=new_cid)
            except (Refused, OSError):
                pass
            promoted_findings.append(fid)

    planned_cards: list[dict] = []
    triage_counts = {"lean": 0, "balanced": 0, "strict": 0}
    repaired_count = 0
    all_ok = True

    for md in sorted(cards_dir.glob("*.md")):
        try:
            header, body = parse_card_file(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        cid = str(header.get("id") or md.stem)
        if is_finding_id_or_header(cid, header):
            continue
        if str(header.get("state") or "ready").lower() not in ("ready", "backlog"):
            continue

        repaired_this = False
        for _attempt in range(max(1, int(max_repairs))):
            h_new, b_new, did_repair = _repair_card_for_eligibility(root, cfg, header, body)
            if did_repair:
                header, body = h_new, b_new
                repaired_this = True
                md.write_text(render_card_file(header, body), encoding="utf-8")
            card_obj = Card(
                id=str(header.get("id") or cid),
                title=str(header.get("title") or cid),
                state=str(header.get("state") or "ready"),
                done_text=body,
                touches=[str(t) for t in (header.get("touches") or [])],
                deps=[str(d) for d in (header.get("depends") or header.get("deps") or [])],
                fields=dict(header),
            )
            elig = check_eligibility(card_obj, cfg, worktree=root)
            if elig.ok:
                break

        if repaired_this:
            repaired_count += 1
        card_obj = Card(
            id=str(header.get("id") or cid),
            title=str(header.get("title") or cid),
            state=str(header.get("state") or "ready"),
            done_text=body,
            touches=[str(t) for t in (header.get("touches") or [])],
            deps=[str(d) for d in (header.get("depends") or header.get("deps") or [])],
            fields=dict(header),
        )
        elig = check_eligibility(card_obj, cfg, worktree=root)
        if not elig.ok:
            all_ok = False
        tri = triage_card_heuristic(card_obj)
        prof = str(tri.get("recommended_profile") or "balanced")
        if prof in triage_counts:
            triage_counts[prof] += 1
        planned_cards.append({
            "id": card_obj.id,
            "title": card_obj.title,
            "category": card_obj.category,
            "touches": list(card_obj.touches),
            "depends": list(card_obj.deps),
            "tests": [t.as_dict() for t in card_obj.tests],
            "done_text": body,
            "complexity": str(tri.get("complexity") or "medium"),
            "profile": prof,
            "eligible": bool(elig.ok),
            "failures": list(elig.failures),
            "repaired": repaired_this,
            "path": str(md),
        })

    if (root / ".git").exists() and cfg is not None:
        from .adapters.files import FileCardSource

        try:
            ensure_baseline_committed(root, extra_allowed=written)
            card_src = FileCardSource(
                root,
                "cards",
                worktree=cfg.state_dir / "cards-source",
                base_branch=cfg.base_branch,
            )
            card_src.sync_ready_cards_from_base()
        except (OSError, Refused, AdapterError, ConfigError) as exc:
            sync_note = str(exc)
            _ = sync_note

    return {
        "written": written,
        "decisions_path": res.get("decisions_path", ""),
        "cards": planned_cards,
        "triage_counts": triage_counts,
        "eligibility_ok": all_ok and len(planned_cards) > 0,
        "repaired_count": repaired_count,
        "promoted_findings": promoted_findings,
    }


def ensure_baseline_committed(repo: Path, *, extra_allowed: list[str] | None = None) -> bool:
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
    allowed_extra = {str(p).strip().lstrip("/") for p in (extra_allowed or ()) if str(p).strip()}
    paths = [line[3:].strip().split(" -> ")[-1].strip('"') for line in lines if len(line) > 3]
    if paths and all(
        p.startswith(".openfarm/")
        or p.startswith("cards/")
        or p in (".openfarm", "cards")
        or p in allowed_extra
        for p in paths
    ):
        git.commit_all("chore: commit openfarm spec and cards")
        return True
    return False
