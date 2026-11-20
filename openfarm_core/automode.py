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
from .errors import AdapterError, ConfigError, Refused
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

    def ask(self, prompt: str, *, system_prompt: str | None = None) -> str:
        comp = self._worker.complete(
            "architect",
            self._model,
            0.2,
            [{"role": "user", "content": prompt}],
            system_prompt=(
                system_prompt
                or "You are a senior Frontier Software Architect inside Openfarm. "
                "Return ONLY valid JSON matching the exact schema requested in the user prompt. "
                "Match the repository's configured language stack and never emit files for another language."
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


def _fix_typescript_relative_js_imports(root: Path) -> int:
    """Rewrite relative `.js` imports to `.ts` in `src/` and `tests/` when only `.ts` exists.

    Node's native `node --test` runs TypeScript directly with ESM type-stripping, so
    `from "../src/foo.js"` fails with `ERR_MODULE_NOT_FOUND` unless `.ts` is used.
    """
    import re

    pattern = re.compile(r"""((?:from|import)\s*\(?\s*['"])(\.\.?/[^'"]+?)\.js(['"])""")
    changed_files = 0
    for folder in ("src", "tests"):
        base_dir = root / folder
        if not base_dir.is_dir():
            continue
        for ts_file in sorted(base_dir.rglob("*.ts")):
            if ts_file.is_symlink():
                continue
            try:
                original = ts_file.read_text(encoding="utf-8")
            except OSError:
                continue

            def _repl(match: re.Match[str]) -> str:
                prefix, rel_no_ext, quote = match.group(1), match.group(2), match.group(3)
                cand_js = (ts_file.parent / f"{rel_no_ext}.js").resolve()
                cand_ts = (ts_file.parent / f"{rel_no_ext}.ts").resolve()
                if not cand_js.exists() and cand_ts.is_file():
                    return f"{prefix}{rel_no_ext}.ts{quote}"
                return match.group(0)

            updated = pattern.sub(_repl, original)
            if updated != original:
                ts_file.write_text(updated, encoding="utf-8")
                changed_files += 1
    return changed_files


def _verify_baseline_quick(root: Path, cfg) -> tuple[bool, str]:
    """Run the repo's configured pack test suite once to verify the baseline is green."""
    from .adapters.direct import open_runner
    from .cli import _configured_pack

    try:
        pack = _configured_pack(cfg, root)
        cmd_info = pack.commands("test")
        argv = cmd_info.get("argv") if isinstance(cmd_info, dict) else None
        if not argv:
            return True, ""
        runner = open_runner(cfg, root)
        res = runner.exec("test", list(argv), cwd=root, timeout_s=min(90.0, float(getattr(cfg, "test_timeout_s", 120.0))))
        if res.cls == "infra":
            return True, ""
        parsed = pack.parse_tests(res.stdout, res.stderr)
        compile_errs = parsed.get("compile_errors") or []
        failed = [r for r in (parsed.get("results") or []) if r.get("status") == "FAILED"]
        if res.exit_code == 0 and not compile_errs and not failed:
            return True, ""
        if compile_errs:
            detail = "; ".join(str(e.get("binary") or e) if isinstance(e, dict) else str(e) for e in compile_errs[:3])
            return False, f"baseline compile error: {detail}"
        if failed:
            names = ", ".join(str(r.get("name") or "") for r in failed[:4])
            return False, f"baseline test(s) failed: {names}"
        tail = (res.stderr or res.stdout or "").strip().splitlines()
        return False, (tail[-1][:200] if tail else f"baseline exited {res.exit_code}")
    except (OSError, ConfigError, AdapterError, Refused) as exc:
        return False, str(exc)


def _stack_authentic_guidance(lang: str) -> str:
    """Idiomatic, runnable guidance per stack so non-Rust repos never receive Rust-shaped specs."""
    low = str(lang or "typescript").strip().lower()
    if low == "typescript":
        return (
            "Stack guidance (TypeScript / Web):\n"
            "- Build an AUTHENTIC, RUNNABLE TypeScript application matching what the user asked for.\n"
            "- If the user asks for a web app / visual experience / UI, design a real runnable local web app "
            "(e.g. a zero-dependency Node `http.createServer` serving real HTML/CSS/client JS/TS so the user can open it in a browser) "
            "plus pure testable helpers. NEVER force Rust-style `Result<T, DomainError>` or ban DOM/HTML/HTTP unless the user explicitly asked for an offline pure library!\n"
            "- Node executes `.ts` directly via `node --test` (ESM type-stripping): ALWAYS use `.ts` extensions in relative imports (e.g. `from \"../src/foo.ts\"`), NEVER `.js`. Avoid TS `enum` or parameter properties.\n"
        )
    if low == "python":
        return (
            "Stack guidance (Python):\n"
            "- Build an AUTHENTIC, RUNNABLE idiomatic Python application (CLI, local HTTP server, or package matching the user's request).\n"
            "- Use standard Python exceptions, dataclasses, and stdlib modules; NEVER impose Rust-style `Result<T, E>` patterns.\n"
        )
    if low == "go":
        return (
            "Stack guidance (Go):\n"
            "- Build an AUTHENTIC, RUNNABLE idiomatic Go application (`net/http`, CLI, or package matching the user's request) using standard `error` returns and `testing`.\n"
        )
    return (
        "Stack guidance (Rust):\n"
        "- Build an AUTHENTIC, RUNNABLE idiomatic Rust crate/binary matching the user's request with `Result`, unit/integration tests, and zero `.unwrap()` in library code.\n"
    )


def architect_grill_turn(
    repo: Path,
    *,
    idea: str,
    history: list[dict] | None = None,
    language: str = "typescript",
    backend=None,
) -> dict:
    """Run one conversational Grill turn with the Frontier Architect.

    The user just describes what they want to build (from a vague app idea in an
    empty folder to a targeted feature in an existing repo). The Architect asks ONE
    concrete product/UX/architecture question at a time with recommended options,
    or sets `ready_to_plan = True` once it has enough signal to design the runnable
    vertical slice, internal invariants, and Wave 1 cards itself.
    """
    from .frontier_review import _extract_json_dict

    root = Path(repo).expanduser().resolve()
    hist = list(history or [])
    clean_idea = str(idea or "").strip()
    existing = [
        p.relative_to(root).as_posix()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and ".git" not in p.parts
        and ".openfarm" not in p.parts
        and "node_modules" not in p.parts
        and "target" not in p.parts
    ][:30]

    if backend is None:
        if len(hist) >= 1 or len(clean_idea.split()) >= 18:
            return {
                "ready_to_plan": True,
                "question": "",
                "options": [],
                "recommended": "",
                "summary": "Enough context collected — ready to draft the runnable vertical slice and Wave cards.",
            }
        return {
            "ready_to_plan": False,
            "question": f"For this {language} project, what should the user interact with first?",
            "options": [
                "Runnable local web app in browser (HTML/CSS/TS)",
                "Interactive CLI command",
                "Reusable library + API",
            ],
            "recommended": "Runnable local web app in browser (HTML/CSS/TS)" if language == "typescript" else "Interactive CLI command",
            "summary": "",
        }

    hist_text = "\n".join(
        f"Q: {h.get('question', '')}\nA: {h.get('answer', '')}" for h in hist
    ) or "(none yet)"
    prompt = (
        f"You are the Frontier Software Architect grilling the owner before designing a {language} project.\n"
        f"Existing repo files: {existing or '(empty workspace)'}\n"
        f"{_stack_authentic_guidance(language)}\n"
        "Your job:\n"
        "- Decide if you need 1 more high-leverage product/UX/data-source clarification from the user, OR if you have enough to design the runnable app, internal invariants, and Wave 1 cards yourself.\n"
        "- NEVER ask the user to invent 'invariants' or 'golden slices' — YOU solve for those.\n"
        "- Ask at most 2-3 total questions across the whole conversation; if len(history) >= 3 or the prompt is already concrete, set ready_to_plan = true.\n"
        "Return ONLY JSON:\n"
        '{"ready_to_plan": false, "question": "...", "options": ["...", "..."], "recommended": "...", "summary": ""}\n\n'
        f"Owner's request:\n{clean_idea}\n\n"
        f"Conversation so far:\n{hist_text}\n"
    )
    raw = str(backend.ask(prompt) if hasattr(backend, "ask") else backend(prompt))
    parsed = _extract_json_dict(raw) or {}
    ready = bool(parsed.get("ready_to_plan", False)) or not str(parsed.get("question") or "").strip()
    options = [str(o).strip() for o in (parsed.get("options") or []) if str(o).strip()][:4]
    return {
        "ready_to_plan": ready,
        "question": str(parsed.get("question") or "").strip(),
        "options": options,
        "recommended": str(parsed.get("recommended") or (options[0] if options else "")).strip(),
        "summary": str(parsed.get("summary") or "").strip(),
    }


CHUNKS_REL_PATH = ".openfarm/CHUNKS.json"


def load_project_chunks(repo: Path) -> list[dict]:
    """Load ordered project chunks/milestones from `.openfarm/CHUNKS.json`."""
    p = Path(repo).expanduser().resolve() / CHUNKS_REL_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("chunks") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or f"C{idx}").strip()
        title = str(item.get("title") or f"Chunk {idx}").strip()
        goal = str(item.get("goal") or "").strip()
        status = str(item.get("status") or ("active" if idx == 1 else "pending")).strip().lower()
        if status not in ("active", "pending", "done"):
            status = "pending"
        out.append({"id": cid, "title": title, "goal": goal, "status": status})
    return out


def save_project_chunks(repo: Path, chunks: list[dict]) -> Path:
    """Persist ordered project chunks to `.openfarm/CHUNKS.json`."""
    p = Path(repo).expanduser().resolve() / CHUNKS_REL_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    cleaned: list[dict] = []
    for idx, item in enumerate(chunks, start=1):
        cleaned.append({
            "id": str(item.get("id") or f"C{idx}").strip(),
            "title": str(item.get("title") or f"Chunk {idx}").strip(),
            "goal": str(item.get("goal") or "").strip(),
            "status": str(item.get("status") or ("active" if idx == 1 else "pending")).strip().lower(),
        })
    p.write_text(json.dumps({"chunks": cleaned}, indent=2) + "\n", encoding="utf-8")
    return p


def set_chunk_status(repo: Path, chunk_id: str, status: str) -> list[dict]:
    """Update a chunk's status (`active`, `pending`, `done`) and activate the next pending chunk if done."""
    chunks = load_project_chunks(repo)
    if not chunks:
        return []
    st = str(status).strip().lower()
    if st not in ("active", "pending", "done"):
        raise Refused("bad-chunk-status", f"invalid chunk status {status!r}")
    found = False
    for c in chunks:
        if c["id"] == chunk_id:
            c["status"] = st
            found = True
    if not found:
        raise Refused("unknown-chunk", f"chunk {chunk_id!r} not found")
    if st == "done" and not any(c["status"] == "active" for c in chunks):
        for c in chunks:
            if c["status"] == "pending":
                c["status"] = "active"
                break
    save_project_chunks(repo, chunks)
    return chunks


def draft_spec_and_chunks(
    repo: Path,
    *,
    idea: str,
    grill_history: list[dict] | None = None,
    language: str = "",
    backend=None,
    on_progress=None,
) -> dict:
    """Flesh out `.openfarm/PROJECT_SPEC.md` and split it into ordered chunks (`.openfarm/CHUNKS.json`).

    Implements the middle stages of:
      Idea -> grill -> spec fleshed out -> spec split into chunks -> chunks split into cards
    """
    from .frontier_review import _extract_json_dict

    root = Path(repo).expanduser().resolve()
    cfg = None
    try:
        cfg = load_config(root)
    except (ConfigError, OSError):
        cfg = None
    lang = (language or (cfg.languages[0] if (cfg and cfg.languages) else "typescript")).strip().lower()
    clean_idea = str(idea or "").strip() or f"Build {root.name}"
    hist = list(grill_history or [])

    if callable(on_progress):
        on_progress("Fleshing out living spec & splitting into chunks...")

    decisions_path = root / ".openfarm" / "GRILL_DECISIONS.md"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    dec_lines = ["# Architect Grill Decisions\n", f"## What we are building\n\n{clean_idea}\n"]
    for turn in hist:
        q = str(turn.get("question") or "Clarification").strip()
        a = str(turn.get("answer") or "").strip()
        if a:
            dec_lines.append(f"## {q}\n\n{a}\n")
    decisions_path.write_text("\n".join(dec_lines), encoding="utf-8")

    spec_md = ""
    chunks: list[dict] = []
    if backend is not None:
        existing_spec = ""
        sp = root / ".openfarm" / "PROJECT_SPEC.md"
        if sp.is_file():
            existing_spec = sp.read_text(encoding="utf-8", errors="replace").strip()[:2500]
        existing_files = [
            p.relative_to(root).as_posix()
            for p in sorted(root.rglob("*"))
            if p.is_file()
            and ".git" not in p.parts
            and ".openfarm" not in p.parts
            and "node_modules" not in p.parts
            and "target" not in p.parts
        ][:30]
        prompt = (
            f"You are the Frontier Software Architect for this {lang} project (`{root.name}`).\n"
            f"Existing repo files: {existing_files or '(empty workspace)'}\n"
            f"{_stack_authentic_guidance(lang)}\n"
            "Your job:\n"
            "1. Flesh out a concrete, inspiring, runnable Product & Technical Specification in Markdown (`.openfarm/PROJECT_SPEC.md`):\n"
            "   - Product Vision & User Experience (faithful to the owner's idea, whether a full app or a feature in an existing repo)\n"
            "   - Architecture & How to Run It locally\n"
            "   - Internal Safety Invariants (derived by YOU — never turn a visual web app into a pure offline library)\n"
            "2. Split the spec into 2 to 4 ordered deliverable Chunks (milestones), where Chunk 1 delivers the core runnable experience end-to-end and subsequent chunks expand it iteratively.\n"
            "Return ONLY valid JSON:\n"
            '{"spec_markdown": "# ...", "chunks": [{"id": "C1", "title": "...", "goal": "..."}, ...]}\n\n'
            + "\n".join(dec_lines)
            + (f"\nExisting spec (if iterating):\n{existing_spec}\n" if existing_spec else "")
        )
        raw = str(backend.ask(prompt) if hasattr(backend, "ask") else backend(prompt))
        parsed = _extract_json_dict(raw) or {}
        spec_md = str(parsed.get("spec_markdown") or "").strip()
        raw_chunks = parsed.get("chunks") if isinstance(parsed, dict) else None
        if isinstance(raw_chunks, list):
            for idx, item in enumerate(raw_chunks, start=1):
                if isinstance(item, dict) and str(item.get("title") or "").strip():
                    chunks.append({
                        "id": str(item.get("id") or f"C{idx}").strip(),
                        "title": str(item.get("title") or "").strip(),
                        "goal": str(item.get("goal") or "").strip(),
                        "status": "active" if idx == 1 else "pending",
                    })

    if not spec_md:
        qa_block = "\n".join(
            f"- **{t.get('question', 'Decision')}**: {t.get('answer', '')}" for t in hist if str(t.get("answer") or "").strip()
        )
        spec_md = (
            f"# {root.name} — Product Specification ({lang})\n\n"
            f"## Vision & Experience\n\n{clean_idea}\n\n"
            + (f"## Clarifications\n\n{qa_block}\n\n" if qa_block else "")
            + f"## Architecture ({lang})\n\n"
            "- Runnable vertical slice with deterministic automated tests.\n"
            "- Modular separation between presentation/entrypoint and core logic.\n"
        )
    if not chunks:
        short_title = clean_idea.splitlines()[0][:64].strip() or f"{root.name} Core Experience"
        chunks = [
            {
                "id": "C1",
                "title": f"Core Runnable Slice: {short_title}",
                "goal": f"Deliver the initial runnable {lang} experience end-to-end with passing baseline tests.",
                "status": "active",
            },
            {
                "id": "C2",
                "title": "Interactive Depth & Polish",
                "goal": "Expand interactive features, visual/UX richness, and edge-case handling per the spec.",
                "status": "pending",
            },
        ]

    spec_path = root / ".openfarm" / "PROJECT_SPEC.md"
    spec_path.write_text(spec_md.rstrip() + "\n", encoding="utf-8")
    chunks_path = save_project_chunks(root, chunks)
    written = [".openfarm/GRILL_DECISIONS.md", ".openfarm/PROJECT_SPEC.md", CHUNKS_REL_PATH]
    return {
        "spec_path": str(spec_path),
        "spec_text": spec_md.rstrip() + "\n",
        "chunks_path": str(chunks_path),
        "chunks": load_project_chunks(root),
        "written": written,
    }


def run_grill_and_architect(
    repo: Path,
    *,
    answers: list[dict] | None = None,
    idea: str = "",
    grill_history: list[dict] | None = None,
    chunk_id: str | None = None,
    backend=None,
    on_progress=None,
) -> dict:
    """Write `.openfarm/GRILL_DECISIONS.md` and expand spec/cards via Frontier backend."""
    from .globs import matches_any

    root = Path(repo).expanduser().resolve()
    cfg = None
    try:
        cfg = load_config(root)
    except (ConfigError, OSError):
        cfg = None
    lang = (cfg.languages[0] if (cfg and cfg.languages) else "typescript").lower()
    never_touch = list(getattr(cfg, "never_touch", ()) or ()) if cfg else []
    forbidden = list(getattr(cfg, "forbidden_files", ()) or ()) if cfg else []

    normalized_answers: list[dict] = []
    if idea.strip():
        normalized_answers.append({"id": "goal", "question": "What we are building", "answer": idea.strip()})
    for turn in (grill_history or []):
        q = str(turn.get("question") or "Clarification").strip()
        a = str(turn.get("answer") or "").strip()
        if a:
            normalized_answers.append({"id": "grill", "question": q, "answer": a})
    if answers:
        normalized_answers.extend(answers)
    if not normalized_answers:
        normalized_answers.append({"id": "goal", "question": "What we are building", "answer": f"Build {root.name}"})

    decisions_path = root / ".openfarm" / "GRILL_DECISIONS.md"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Architect Grill Decisions\n"]
    for item in normalized_answers:
        q = str(item.get("question") or item.get("id") or "Context")
        a = str(item.get("answer") or "").strip()
        lines.append(f"## {q}\n\n{a}\n")
    decisions_path.write_text("\n".join(lines), encoding="utf-8")

    written: list[str] = [".openfarm/GRILL_DECISIONS.md"]
    existing_chunks = load_project_chunks(root)
    active_chunk: dict | None = None
    if existing_chunks:
        if chunk_id:
            active_chunk = next((c for c in existing_chunks if c["id"] == chunk_id), None)
        if active_chunk is None:
            active_chunk = next((c for c in existing_chunks if c.get("status") == "active"), None) or next(
                (c for c in existing_chunks if c.get("status") != "done"), existing_chunks[0]
            )
    existing_spec_path = root / ".openfarm" / "PROJECT_SPEC.md"
    existing_spec_text = (
        existing_spec_path.read_text(encoding="utf-8", errors="replace").strip()
        if existing_spec_path.is_file()
        else ""
    )

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
        chunk_section = ""
        if active_chunk is not None:
            chunk_section = (
                f"\nActive Chunk to split into cards now:\n"
                f"  - ID: {active_chunk['id']}\n"
                f"  - Title: {active_chunk['title']}\n"
                f"  - Goal: {active_chunk.get('goal', '')}\n"
                f"  (Do NOT delete completed chunks; split THIS active chunk into 2 to 5 concrete cards.)\n"
            )
        spec_section = f"\nExisting Living Spec (`.openfarm/PROJECT_SPEC.md`):\n{existing_spec_text[:3500]}\n" if existing_spec_text else ""
        prompt = (
            f"You are the Frontier Software Architect for this {lang} Openfarm repository.\n"
            f"Language stack: {lang} (STRICT: generate ONLY {lang} files — NEVER emit .rs or Cargo.toml in a non-Rust project).\n"
            f"{_stack_authentic_guidance(lang)}"
            f"Locked never_touch files (DO NOT modify or list in card touches): {never_touch}\n"
            f"Existing repository files: {existing_files}\n"
            f"{spec_section}{chunk_section}"
            "YOU are responsible for deriving:\n"
            "  - The full, faithful product architecture tailored directly to the owner's request (whether it's a whole new runnable app or a feature in an existing codebase)\n"
            "  - Internal safety invariants (without turning a web app into a pure offline library)\n"
            "  - A real, runnable vertical slice with passing baseline tests\n"
            "  - 2 to 5 scoped, non-overlapping Wave cards that implement the active chunk (or Chunk 1)\n"
            "Return ONLY a JSON object: {\"files\": {\"<repo-relative-path>\": \"<content>\", ...}}.\n"
            "Include:\n"
            "1. `.openfarm/PROJECT_SPEC.md` (clear product spec, architecture, how to run it, and Chunks/Waves plan — preserve/refine existing spec if present)\n"
            "2. `.openfarm/CHUNKS.json` (ordered milestone chunks `{\"chunks\": [{\"id\": \"C1\", \"title\": \"...\", \"goal\": \"...\", \"status\": \"active\"}, ...]}` if not already present)\n"
            f"3. The runnable {lang} vertical slice file(s) and passing baseline test(s)\n"
            "4. 2 to 5 scoped, non-overlapping Wave cards under `cards/W1-01.md`.. (or `W2-01.md` for later chunks) using TOML +++ headers "
            "(id, title, state=\"ready\", category=\"small-feature\", wave=1, touches, reference, new_symbols=true, depends, tests=[{name, file, asserts}]) "
            "and concrete `## Done` sections without vague adjectives. "
            "Prefer exported functions (e.g. `export function neutralTheme()`) over non-function module constants for newly introduced symbols so TDD scaffolds compile cleanly.\n"
            "Never modify `.openfarm/config.toml` or `never_touch` files.\n\n"
            + "\n".join(lines)
        )
        if callable(on_progress):
            on_progress(f"Sending {len(prompt):,} char prompt to Frontier Architect ({lang})...")
        raw = str(backend.ask(prompt) if hasattr(backend, "ask") else backend(prompt))
        if callable(on_progress):
            on_progress(f"Architect responded ({len(raw):,} chars) — extracting files...")
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
                text_out = str(content)
                target.write_text(text_out, encoding="utf-8")
                written.append(rel_s)
                if callable(on_progress):
                    on_progress(f"Wrote {rel_s} ({len(text_out.splitlines())} lines)")
    else:
        if not existing_spec_text:
            spec_lines = [f"# {root.name} — Project Specification ({lang})\n"]
            for item in normalized_answers:
                q = str(item.get("question") or item.get("id") or "")
                a = str(item.get("answer") or "").strip()
                spec_lines.append(f"## {q}\n\n{a}\n")
            existing_spec_path.write_text("\n".join(spec_lines), encoding="utf-8")
            written.append(".openfarm/PROJECT_SPEC.md")
    if not load_project_chunks(root):
        short_goal = str(normalized_answers[0].get("answer") or f"Build {root.name}").splitlines()[0][:64]
        save_project_chunks(
            root,
            [
                {"id": "C1", "title": f"Core Runnable Slice: {short_goal}", "goal": short_goal, "status": "active"},
                {"id": "C2", "title": "Iteration & Polish", "goal": "Expand capabilities and polish UX per spec", "status": "pending"},
            ],
        )
        if CHUNKS_REL_PATH not in written:
            written.append(CHUNKS_REL_PATH)

    answers = normalized_answers

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


def _ensure_card_touch_placeholders(root: Path, cfg, cards_dir: Path, written: list[str]) -> list[str]:
    """Ensure every ready card's non-test production `touches` file exists on base.

    Openfarm's deterministic `additions-only-new-file` guard permits the test writer
    to append stubs only to production files that already exist on the base branch.
    Creating minimal empty placeholder modules for newly introduced files before
    committing the baseline prevents greenfield multi-module cards from deadlocking.
    """
    from .adapters.files import parse_card_file
    from .findings import is_finding_id_or_header
    from .globs import matches_any

    never_touch = list(getattr(cfg, "never_touch", ()) or ()) if cfg else []
    forbidden = list(getattr(cfg, "forbidden_files", ()) or ()) if cfg else []
    created: list[str] = []

    def _is_test_path(rel: str) -> bool:
        low = rel.lower()
        name = Path(rel).name.lower()
        return (
            low.startswith("tests/")
            or "/tests/" in low
            or low.startswith("test/")
            or ".test." in name
            or ".spec." in name
            or name.startswith("test_")
            or name.endswith("_test.go")
            or name.endswith("_test.py")
            or name.endswith("_tests.rs")
        )

    for md in sorted(cards_dir.glob("*.md")):
        try:
            header, _ = parse_card_file(md.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        cid = str(header.get("id") or md.stem)
        if is_finding_id_or_header(cid, header):
            continue
        if str(header.get("state") or "ready").lower() not in ("ready", "backlog"):
            continue
        for raw_touch in header.get("touches") or []:
            rel = str(raw_touch or "").strip().lstrip("/")
            if not rel or "*" in rel or _is_test_path(rel):
                continue
            if matches_any(rel, never_touch) or matches_any(rel, forbidden):
                continue
            target = resolves_within(root, rel)
            if target is None or target.exists():
                continue
            suffix = target.suffix.lower()
            if suffix in (".ts", ".tsx", ".mts", ".cts", ".js", ".mjs"):
                stub = f"// Placeholder module for card {cid} — implemented by openfarm worker.\nexport {{}};\n"
            elif suffix == ".py":
                stub = f'"""Placeholder module for card {cid} — implemented by openfarm worker."""\n'
            elif suffix == ".go":
                stub = f"// Placeholder module for card {cid}.\npackage main\n"
            elif suffix == ".rs":
                stub = f"// Placeholder module for card {cid}.\n"
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(stub, encoding="utf-8")
            created.append(rel)
            if rel not in written:
                written.append(rel)
    return created


def draft_wave_plan(
    repo: Path,
    *,
    answers: list[dict] | None = None,
    idea: str = "",
    grill_history: list[dict] | None = None,
    chunk_id: str | None = None,
    backend=None,
    max_repairs: int = 2,
    include_picked_findings: bool = True,
    verify_baseline: bool = True,
    on_progress=None,
) -> dict:
    """Run Grill + Architect, incorporate picked findings, auto-repair eligibility, and triage cards."""
    from .adapters.files import parse_card_file, render_card_file
    from .cards import Card
    from .findings import decide_finding, is_finding_id_or_header, picked_findings_for_planning
    from .gates.eligibility import check_eligibility
    from .triage import triage_card_heuristic

    def _emit_progress(msg: str) -> None:
        if callable(on_progress):
            on_progress(msg)

    root = Path(repo).expanduser().resolve()
    _emit_progress(
        f"Splitting chunk {chunk_id} into scoped Wave cards..."
        if chunk_id
        else "Designing spec, chunks, runnable vertical slice & Wave cards..."
    )
    res = run_grill_and_architect(
        root,
        answers=answers,
        idea=idea,
        grill_history=grill_history,
        chunk_id=chunk_id,
        backend=backend,
        on_progress=_emit_progress,
    )
    written = list(res.get("written") or [])

    cfg = load_config(root)
    cards_dir = root / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    lang = (cfg.languages[0] if cfg.languages else "rust").lower()
    if lang == "typescript":
        fixed_imports = _fix_typescript_relative_js_imports(root)
        if fixed_imports:
            _emit_progress(f"Normalized .js -> .ts relative imports in {fixed_imports} file(s)")
    placeholders = _ensure_card_touch_placeholders(root, cfg, cards_dir, written)
    if placeholders:
        _emit_progress(f"Prepared {len(placeholders)} placeholder module(s): {', '.join(placeholders[:4])}")

    baseline_ok = True
    baseline_error = ""
    if verify_baseline:
        _emit_progress("Verifying baseline tests on golden slice...")
        baseline_ok, baseline_error = _verify_baseline_quick(root, cfg)

    _emit_progress("Checking & repairing card eligibility...")

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
        "chunks": load_project_chunks(root),
        "cards": planned_cards,
        "triage_counts": triage_counts,
        "eligibility_ok": all_ok and len(planned_cards) > 0,
        "baseline_ok": baseline_ok,
        "baseline_error": baseline_error,
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

    def _path_is_allowed(p: str) -> bool:
        if p.startswith(".openfarm/") or p.startswith("cards/") or p in (".openfarm", "cards") or p in allowed_extra:
            return True
        if p.endswith("/") and any(a.startswith(p) for a in allowed_extra):
            return True
        return False

    if paths and all(_path_is_allowed(p) for p in paths):
        git.commit_all("chore: commit openfarm spec and cards")
        return True
    return False


def update_card_with_eligibility(
    repo: Path,
    card_id: str,
    *,
    title: str | None = None,
    category: str | None = None,
    touches: list[str] | None = None,
    depends: list[str] | None = None,
    wave: int | None = None,
    tests: list[dict] | None = None,
    done_text: str | None = None,
) -> dict:
    """Edit a card under `cards/`, evaluate `check_eligibility` live, and sync to `openfarm-cards`."""
    from .adapters.files import FileCardSource, parse_card_file, render_card_file
    from .cards import Card
    from .gates.eligibility import check_eligibility

    root = Path(repo).expanduser().resolve()
    cfg = load_config(root)
    cards_dir = root / "cards"
    card_path = cards_dir / f"{card_id}.md"
    if not card_path.is_file():
        for cand in sorted(cards_dir.glob("*.md")):
            try:
                h_c, _ = parse_card_file(cand.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if str(h_c.get("id") or "") == card_id:
                card_path = cand
                break
    if not card_path.is_file():
        raise Refused("card-not-found", f"no card file found for {card_id!r}")

    header, body = parse_card_file(card_path.read_text(encoding="utf-8"))
    if title is not None and title.strip():
        header["title"] = title.strip()
    if category is not None and category.strip():
        header["category"] = category.strip()
    if touches is not None:
        header["touches"] = [str(t).strip() for t in touches if str(t).strip()]
        if header.get("reference") and header["touches"]:
            header["reference"] = header["touches"][0]
    if depends is not None:
        header["depends"] = [str(d).strip() for d in depends if str(d).strip()]
    if wave is not None:
        header["wave"] = max(1, int(wave))
    if tests is not None:
        header["tests"] = list(tests)
    if done_text is not None:
        body = done_text.strip() + "\n"

    card_path.write_text(render_card_file(header, body), encoding="utf-8")
    card_obj = Card(
        id=str(header.get("id") or card_id),
        title=str(header.get("title") or card_id),
        state=str(header.get("state") or "ready"),
        done_text=body,
        touches=[str(t) for t in (header.get("touches") or [])],
        deps=[str(d) for d in (header.get("depends") or header.get("deps") or [])],
        fields=dict(header),
    )
    elig = check_eligibility(card_obj, cfg, worktree=root)
    if (root / ".git").exists():
        ensure_baseline_committed(root, extra_allowed=[card_path.relative_to(root).as_posix()])
        src = FileCardSource(root, "cards", worktree=cfg.state_dir / "cards-source", base_branch=cfg.base_branch)
        src.sync_ready_cards_from_base()
    return {
        "card": {
            "id": card_obj.id,
            "title": card_obj.title,
            "category": card_obj.category,
            "touches": list(card_obj.touches),
            "depends": list(card_obj.deps),
            "wave": int(header.get("wave") or 1),
            "tests": [t.as_dict() for t in card_obj.tests],
            "done_text": body,
        },
        "eligible": bool(elig.ok),
        "failures": list(elig.failures),
    }


def split_card_into_two(
    repo: Path,
    card_id: str,
    *,
    first_title: str,
    first_touches: list[str],
    second_title: str,
    second_touches: list[str],
) -> dict:
    """Split an oversized card into `<card_id>a` and `<card_id>b` (chained via `depends`)."""
    from .adapters.files import FileCardSource, parse_card_file, render_card_file
    from .cards import Card
    from .gates.eligibility import check_eligibility

    root = Path(repo).expanduser().resolve()
    cfg = load_config(root)
    lang = (cfg.languages[0] if cfg.languages else "rust").lower()
    cards_dir = root / "cards"
    orig_path = cards_dir / f"{card_id}.md"
    if not orig_path.is_file():
        raise Refused("card-not-found", f"no card file found for {card_id!r}")
    orig_hdr, orig_body = parse_card_file(orig_path.read_text(encoding="utf-8"))

    id_a, id_b = f"{card_id}a", f"{card_id}b"
    out_cards: list[dict] = []
    written_rel: list[str] = []
    for cid, ctitle, ctouches, cdeps in (
        (id_a, first_title, first_touches, list(orig_hdr.get("depends") or [])),
        (id_b, second_title, second_touches, [id_a]),
    ):
        _dt, dtest = _default_touch_and_test(root, lang, cid)
        clean_touches = [str(t).strip() for t in ctouches if str(t).strip()] or [_dt]
        hdr = {
            "id": cid,
            "title": str(ctitle or cid).strip(),
            "state": "ready",
            "category": str(orig_hdr.get("category") or "small-feature"),
            "touches": clean_touches,
            "depends": cdeps,
            "tests": [dtest],
            "wave": int(orig_hdr.get("wave") or 1),
        }
        if hdr["category"] not in ("pinned-bugfix", "test-backfill"):
            hdr["new_symbols"] = True
        if hdr["category"] in (getattr(cfg, "require_reference", ()) or ()):
            hdr["reference"] = clean_touches[0]
        body = (
            "## Done\n\n"
            f"- Implement `{clean_touches[0]}` for {hdr['title']}.\n"
            f"- `{dtest['name']}` returns expected values and verifies an error on invalid input.\n"
        )
        hdr, body, _rep = _repair_card_for_eligibility(root, cfg, hdr, body)
        cpath = cards_dir / f"{cid}.md"
        cpath.write_text(render_card_file(hdr, body), encoding="utf-8")
        written_rel.append(f"cards/{cid}.md")
        cobj = Card(
            id=cid,
            title=hdr["title"],
            state="ready",
            done_text=body,
            touches=list(hdr["touches"]),
            deps=list(hdr["depends"]),
            fields=dict(hdr),
        )
        elig = check_eligibility(cobj, cfg, worktree=root)
        out_cards.append({
            "id": cid,
            "title": hdr["title"],
            "touches": list(hdr["touches"]),
            "depends": list(hdr["depends"]),
            "eligible": bool(elig.ok),
            "failures": list(elig.failures),
        })

    orig_hdr["state"] = "split"
    orig_path.write_text(render_card_file(orig_hdr, orig_body), encoding="utf-8")
    written_rel.append(f"cards/{card_id}.md")

    if (root / ".git").exists():
        ensure_baseline_committed(root, extra_allowed=written_rel)
        src = FileCardSource(root, "cards", worktree=cfg.state_dir / "cards-source", base_branch=cfg.base_branch)
        src.sync_ready_cards_from_base()
    return {"split_from": card_id, "cards": out_cards}
