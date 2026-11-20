"""Architectural Invariant Gate for Openfarm v0.12.0 (100% stdlib-only).

Deterministically enforces structural rules declared under `[invariants]` in
`.openfarm/config.toml`:
- `forbidden_imports`: disallow specific modules/packages inside files matching `path_glob`
- `max_file_bytes`: enforce maximum byte size on files matching `path_glob`
- `require_exports`: verify required symbols remain exported/defined in a target file
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .globs import matches_any

_TS_IMPORT_RE = re.compile(r"""(?:from\s+|import\s*\(?\s*)['"]([^'"]+)['"]""")
_RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+([A-Za-z0-9_]+)(?:::|;)", re.MULTILINE)
_GO_IMPORT_RE = re.compile(r'^\s*(?:[A-Za-z0-9_.]+\s+)?"([^"]+)"', re.MULTILINE)


def _extract_imports(path: Path, text: str) -> set[str]:
    suffix = path.suffix.lower()
    found: set[str] = set()
    if suffix == ".py":
        try:
            tree = ast.parse(text, str(path))
        except SyntaxError:
            return found
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
                found.add(node.module)
    elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        for m in _TS_IMPORT_RE.finditer(text):
            mod = m.group(1).strip()
            found.add(mod)
            if not mod.startswith("."):
                found.add(mod.split("/")[0])
    elif suffix == ".rs":
        for m in _RUST_USE_RE.finditer(text):
            found.add(m.group(1))
    elif suffix == ".go":
        for m in _GO_IMPORT_RE.finditer(text):
            mod = m.group(1).strip()
            found.add(mod)
            found.add(mod.split("/")[0])
    return found


def _iter_repo_files(worktree: Path) -> list[tuple[str, Path]]:
    skip = {".git", "node_modules", "target", "__pycache__", ".venv", ".pytest_cache"}
    out: list[tuple[str, Path]] = []
    for p in sorted(worktree.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if any(part in skip for part in p.relative_to(worktree).parts):
            continue
        out.append((p.relative_to(worktree).as_posix(), p))
    return out


def check_architectural_invariants(worktree: Path, cfg) -> dict:
    """Evaluate all `[invariants]` rules against `worktree` deterministically."""
    inv = getattr(cfg, "invariants", None)
    if inv is None or not inv.has_rules:
        return {"ok": True, "violations": [], "checked_files": 0, "skipped": True}

    root = Path(worktree).expanduser().resolve()
    files = _iter_repo_files(root)
    violations: list[dict] = []

    for rule in inv.forbidden_imports:
        glob_pat = str(rule.get("path_glob") or "")
        disallowed = {str(x).strip() for x in (rule.get("disallow") or []) if str(x).strip()}
        if not glob_pat or not disallowed:
            continue
        for rel, full in files:
            if not matches_any(rel, [glob_pat]):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            imports = _extract_imports(full, text)
            bad = sorted(imports & disallowed)
            if bad:
                violations.append({
                    "rule": "forbidden_imports",
                    "path": rel,
                    "detail": f"disallowed import(s) {bad} under {glob_pat}",
                })

    for rule in inv.max_file_bytes:
        glob_pat = str(rule.get("path_glob") or "")
        limit = int(rule.get("limit") or 0)
        if not glob_pat or limit <= 0:
            continue
        for rel, full in files:
            if not matches_any(rel, [glob_pat]):
                continue
            try:
                sz = full.stat().st_size
            except OSError:
                continue
            if sz > limit:
                violations.append({
                    "rule": "max_file_bytes",
                    "path": rel,
                    "detail": f"file size {sz} bytes exceeds limit {limit} bytes",
                })

    for rule in inv.require_exports:
        rel = str(rule.get("path") or "").strip().lstrip("/")
        symbols = [str(s).strip() for s in (rule.get("symbols") or []) if str(s).strip()]
        if not rel or not symbols:
            continue
        target = root / rel
        if not target.is_file():
            violations.append({
                "rule": "require_exports",
                "path": rel,
                "detail": f"required file {rel!r} is missing",
            })
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        for sym in symbols:
            if not re.search(rf"\b{re.escape(sym)}\b", text):
                violations.append({
                    "rule": "require_exports",
                    "path": rel,
                    "detail": f"required exported symbol {sym!r} not found in {rel}",
                })

    return {
        "ok": not violations,
        "violations": violations,
        "checked_files": len(files),
        "skipped": False,
    }


__all__ = ["check_architectural_invariants"]
