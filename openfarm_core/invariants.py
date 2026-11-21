"""Architectural Invariant Gate for Openfarm v0.12.0 (100% stdlib-only).

Deterministically enforces structural rules declared under `[invariants]` in
`.openfarm/config.toml`:
- `forbidden_imports`: disallow specific modules/packages inside files matching `path_glob`
- `max_file_bytes`: enforce maximum byte size on files matching `path_glob`
- `require_exports`: verify required symbols remain exported/defined in a target file
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from .globs import matches_any, resolves_within

_TS_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s+|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"]+)['"]"""
)
_RUST_USE_STMT_RE = re.compile(
    r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?use\s+([^;]+);",
    re.MULTILINE,
)
_GO_SINGLE_IMPORT_RE = re.compile(
    r'^\s*import\s+(?:(?:[A-Za-z_][A-Za-z0-9_]*|\.|_)\s+)?["`]([^"`]+)["`]',
    re.MULTILINE,
)
_GO_BLOCK_IMPORT_RE = re.compile(r"^\s*import\s*\((.*?)\)", re.MULTILINE | re.DOTALL)
_GO_BLOCK_LINE_RE = re.compile(
    r'^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*|\.|_)\s+)?["`]([^"`]+)["`]',
    re.MULTILINE,
)


_RUST_CHAR_LIT_RE = re.compile(
    r"'(?:[^'\\\n]|\\(?:[nrt0'\"\\]|x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}))'"
)


def _strip_c_comments(text: str, *, keep_strings: bool = True,
                      is_rust: bool = False, raw_backtick: bool = False) -> str:
    """Strip `//` and `/* ... */` comments (and optionally strings) in TS/JS/Rust/Go."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two == "//":
            end = text.find("\n", i)
            if end < 0:
                break
            out.append("\n")
            i = end + 1
            continue
        if two == "/*":
            end = text.find("*/", i + 2)
            chunk = text[i:] if end < 0 else text[i:end + 2]
            out.append("\n" * chunk.count("\n") or " ")
            i = n if end < 0 else end + 2
            continue
        if is_rust and ch == "'":
            cm = _RUST_CHAR_LIT_RE.match(text, i)
            if cm:
                out.append(cm.group(0) if keep_strings else " ")
                i = cm.end()
                continue
            # Rust lifetime (`'a`, `'static`), not a char literal.
            out.append(ch)
            i += 1
            continue
        if ch in ('"', "'", "`"):
            j = i + 1
            if ch == "`":
                while j < n and text[j] != "`":
                    j += 1 if raw_backtick else (2 if text[j] == "\\" else 1)
            else:
                while j < n and text[j] != ch:
                    if text[j] == "\n":
                        break
                    j += 2 if text[j] == "\\" else 1
            end = n if j >= n else j + 1
            if keep_strings:
                out.append(text[i:end])
            else:
                out.append(" " + "\n" * text[i:end].count("\n"))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _expand_rust_use(spec: str) -> list[str]:
    """Expand a Rust `use` tree like `crate::db::{Pool, sub::Conn}` into full paths."""
    spec = spec.strip()
    if spec.startswith("::"):
        spec = spec[2:].strip()
    if not spec:
        return []
    brace = spec.find("{")
    if brace < 0:
        clean = re.sub(r"\s+as\s+\S+$", "", spec).strip()
        return [clean] if clean else []
    prefix = spec[:brace].strip()
    depth = 0
    end = -1
    for i in range(brace, len(spec)):
        if spec[i] == "{":
            depth += 1
        elif spec[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    inner = spec[brace + 1:end] if end > brace else spec[brace + 1:]
    parts: list[str] = []
    cur: list[str] = []
    d = 0
    for ch in inner:
        if ch == "{":
            d += 1
            cur.append(ch)
        elif ch == "}":
            d -= 1
            cur.append(ch)
        elif ch == "," and d == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        for leaf in _expand_rust_use(part):
            if leaf == "self":
                base = prefix.rstrip(":").strip()
                if base:
                    out.append(base)
            else:
                out.append(f"{prefix}{leaf}" if prefix.endswith("::") or not prefix else f"{prefix}::{leaf}")
    return out


def _resolve_py_relative(rel: str, level: int, module: str | None) -> str:
    """Resolve a Python relative import against the file's repo-relative path."""
    if not rel or level <= 0:
        return ""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    pkg_parts = parts[:-1]  # directory containing the file
    up = level - 1
    if up > len(pkg_parts):
        return ""
    base = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
    if module:
        base = [*base, *module.split(".")]
    return ".".join(base)


def _extract_imports(path: Path, text: str, rel: str = "") -> set[str]:
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
            elif isinstance(node, ast.ImportFrom):
                dots = "." * (node.level or 0)
                resolved = _resolve_py_relative(rel, node.level or 0, node.module) if node.level else ""
                if node.module:
                    full_mod = f"{dots}{node.module}"
                    found.add(full_mod)
                    found.add(node.module)
                    found.add(node.module.split(".")[0])
                    if resolved:
                        found.add(resolved)
                        found.add(resolved.split(".")[0])
                    for alias in node.names:
                        if alias.name != "*":
                            found.add(f"{full_mod}.{alias.name}")
                            found.add(f"{node.module}.{alias.name}")
                            if resolved:
                                found.add(f"{resolved}.{alias.name}")
                elif node.level:
                    for alias in node.names:
                        if alias.name != "*":
                            found.add(f"{dots}{alias.name}")
                            found.add(alias.name)
                            if resolved:
                                found.add(f"{resolved}.{alias.name}")
    elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        cleaned = _strip_c_comments(text, keep_strings=True)
        for m in _TS_IMPORT_RE.finditer(cleaned):
            mod = m.group(1).strip()
            found.add(mod)
            if not mod.startswith("."):
                found.add(mod.split("/")[0])
    elif suffix == ".rs":
        cleaned = _strip_c_comments(text, keep_strings=False, is_rust=True)
        for m in _RUST_USE_STMT_RE.finditer(cleaned):
            for expanded in _expand_rust_use(m.group(1)):
                segs = [s.strip() for s in expanded.split("::") if s.strip()]
                for i in range(1, len(segs) + 1):
                    found.add("::".join(segs[:i]))
    elif suffix == ".go":
        cleaned = _strip_c_comments(text, keep_strings=True, raw_backtick=True)
        for m in _GO_SINGLE_IMPORT_RE.finditer(cleaned):
            mod = m.group(1).strip()
            found.add(mod)
            found.add(mod.split("/")[0])
        for blk in _GO_BLOCK_IMPORT_RE.finditer(cleaned):
            for m in _GO_BLOCK_LINE_RE.finditer(blk.group(1)):
                mod = m.group(1).strip()
                found.add(mod)
                found.add(mod.split("/")[0])
    return found


def _code_identifiers_for_exports(path: Path, text: str) -> set[str] | None:
    """Return identifier tokens outside comments/strings for `require_exports`."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            names: set[str] = set()
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.NAME:
                    names.add(tok.string)
            return names
        except (tokenize.TokenError, IndentationError):
            return None
    if suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".go"):
        code_only = _strip_c_comments(
            text, keep_strings=False,
            is_rust=(suffix == ".rs"),
            raw_backtick=(suffix == ".go"),
        )
        return set(re.findall(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", code_only))
    return None


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
            imports = _extract_imports(full, text, rel=rel)
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
        raw_rel = str(rule.get("path") or "").strip()
        rel = raw_rel.lstrip("/")
        symbols = [str(s).strip() for s in (rule.get("symbols") or []) if str(s).strip()]
        if not raw_rel or not symbols:
            continue
        if Path(raw_rel).is_absolute() or not resolves_within(root, rel):
            violations.append({
                "rule": "require_exports",
                "path": raw_rel,
                "detail": f"required file {raw_rel!r} escapes the worktree",
            })
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
        identifiers = _code_identifiers_for_exports(target, text)
        for sym in symbols:
            found_sym = (sym in identifiers) if identifiers is not None else bool(
                re.search(rf"\b{re.escape(sym)}\b", text)
            )
            if not found_sym:
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
