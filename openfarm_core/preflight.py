"""Plan preflight: check a card with the rules that will judge it, before anyone builds it.

Four cheap, deterministic checks, each run when a plan is drafted and again on
every card change:

- `scaffold`: every function signature the card's text names can be written as a
  scaffold under the language pack's rule (TypeScript: no parameter defaults).
- `touches`: every file the text names, and every placeholder the split made, is
  in `touches`, exists already, or comes from a card this one depends on.
- `depends`: the cards it waits for exist, can finish, and don't wait for it.
- `seams`: where one card serves a page and another styles it, the stylesheet
  card quotes the page's element ids exactly.

`rules` is the existing eligibility check, so one list holds every reason a card
can't be built. A check that can be put right without changing what the card
asks for carries a `fix`; `repair_card` applies those and says what it changed.
"""

from __future__ import annotations

import re
from pathlib import Path

from .cards import Card
from .globs import matches_any

CHECKS = ("scaffold", "touches", "depends", "seams", "rules")

_CODE = re.compile(r"`([^`\n]+)`")
_TS_FUNCTION = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[(<]")
_PY_FUNCTION = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
_PATH = re.compile(r"^(?:[\w.@-]+/)+[\w.@-]+\.[A-Za-z0-9]{1,5}$")
_ID_SELECTOR = re.compile(r"(?<![&/])#([A-Za-z][\w-]*)")
_HEX_COLOUR = re.compile(r"(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_CLASS_SELECTOR = re.compile(r"(?:^|[\s,>+~])\.([A-Za-z][\w-]*)")
_NOT_BUILDABLE = ("dropped", "split")


def _is_test_path(rel: str) -> bool:
    low, name = rel.lower(), Path(rel).name.lower()
    return (low.startswith(("tests/", "test/")) or "/tests/" in low or ".test." in name or ".spec." in name
            or name.startswith("test_") or name.endswith(("_test.go", "_test.py", "_tests.rs")))


def _language(cfg) -> str:
    langs = list(getattr(cfg, "languages", None) or [])
    return str(langs[0]).lower() if langs else ""


def _result(check: str, problems: list[str], fix: dict | None = None) -> dict:
    out = {"check": check, "status": "fail" if problems else "pass", "detail": "; ".join(problems)}
    if problems and fix:
        out["fix"] = fix
    return out


# -- scaffold ------------------------------------------------------------------


def _split_params(text: str) -> list[str]:
    """Split a parameter list on its top-level commas."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>" and not (ch == ">" and i and text[i - 1] == "="):
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _top_level_default(param: str) -> int:
    """Where a parameter's `= default` starts, or -1. `=>` in a function type is not a default."""
    depth = 0
    for i, ch in enumerate(param):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>" and not (ch == ">" and i and param[i - 1] == "="):
            depth -= 1
        elif ch == "=" and depth == 0 and param[i + 1:i + 2] not in (">", "=") and param[i - 1:i] not in ("=", "!", "<", ">"):
            return i
    return -1


def _without_ts_defaults(signature: str) -> tuple[str, list[tuple[str, str]]]:
    """Turn `name: T = value` parameters into `name?: T`. Returns the new text and what was dropped."""
    open_at = signature.find("(")
    depth, close_at = 0, -1
    for i in range(open_at, len(signature)):
        if signature[i] == "(":
            depth += 1
        elif signature[i] == ")":
            depth -= 1
            if depth == 0:
                close_at = i
                break
    if open_at < 0 or close_at < 0:
        return signature, []
    dropped, params = [], []
    for param in _split_params(signature[open_at + 1:close_at]):
        at = _top_level_default(param)
        if at < 0:
            params.append(param)
            continue
        head, default = param[:at].rstrip(), param[at + 1:].strip()
        name, colon, kind = head.partition(":")
        lead = param[:len(param) - len(param.lstrip())]
        name = name.strip()
        params.append(f"{lead}{name}{'' if name.endswith('?') else '?'}{colon}{kind}")
        dropped.append((name.rstrip("?"), default))
    return signature[:open_at + 1] + ",".join(params) + signature[close_at:], dropped


def _check_scaffold(card: Card, cfg, root: Path) -> dict:
    lang = _language(cfg)
    body = card.done_text or ""
    problems: list[str] = []
    new_body = body
    notes: list[str] = []
    if lang in ("typescript", "ts", "javascript"):
        from .packs.typescript import typescript_scaffold_problem

        def stub(sig: str) -> str:
            return sig.rstrip(" ;:") + ' { throw new Error("not built"); }'

        for sig in dict.fromkeys(_CODE.findall(body)):
            match = _TS_FUNCTION.match(sig.strip())
            if match is None:
                continue
            why = typescript_scaffold_problem(stub(sig.strip()))
            if not why:
                continue
            problems.append(f"`{match.group(1)}` can't be scaffolded: {why}")
            fixed, dropped = _without_ts_defaults(sig)
            if dropped and not typescript_scaffold_problem(stub(fixed.strip())):
                said = " and ".join(f"`{name}` is `{default}` when it is left out" for name, default in dropped)
                new_body = new_body.replace(f"`{sig}`", f"`{fixed}` ({said})")
                notes.append(f"`{match.group(1)}`: made {', '.join(f'`{n}`' for n, _ in dropped)} optional "
                             f"instead of giving a default in the signature, which a scaffold may not hold")
        for rel in card.touches:
            path = root / rel
            if path.suffix.lower() in (".ts", ".tsx", ".mts") and path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if "Placeholder module for card" in text and (why := typescript_scaffold_problem(text)):
                    problems.append(f"the placeholder `{rel}` is not a scaffold: {why}")
    elif lang in ("python", "py"):
        from .packs.python import valid_python_scaffold

        for sig in dict.fromkeys(_CODE.findall(body)):
            match = _PY_FUNCTION.match(sig.strip())
            if match and not valid_python_scaffold(sig.strip().rstrip(":") + ":\n    raise NotImplementedError\n"):
                problems.append(f"`{match.group(1)}` can't be scaffolded as written")
    fix = {"done_text": new_body, "notes": notes} if notes and new_body != body else None
    return _result("scaffold", problems, fix if fix and len(notes) == len(problems) else None)


# -- touches -------------------------------------------------------------------


def named_files(card: Card) -> list[str]:
    """Repository files the card's text names in backticks, such as `src/client/apply.ts`."""
    out: list[str] = []
    for code in _CODE.findall(card.done_text or ""):
        for word in code.replace(",", " ").split():
            word = word.strip("'\"();")
            if _PATH.match(word) and not word.startswith((".", "/")) and "://" not in word and word not in out:
                out.append(word)
    return out


def _upstream(card: Card, by_id: dict[str, Card]) -> list[Card]:
    seen: dict[str, Card] = {}
    todo = list(card.deps)
    while todo:
        dep = by_id.get(todo.pop())
        if dep is not None and dep.id not in seen and dep.id != card.id:
            seen[dep.id] = dep
            todo.extend(dep.deps)
    return list(seen.values())


def _really_exists(path: Path) -> bool:
    """A file that is there for real. A placeholder the split made still has to be written by a task."""
    if not path.is_file():
        return path.exists()
    try:
        return "Placeholder module for card" not in path.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return True


def _check_touches(card: Card, cfg, root: Path, by_id: dict[str, Card]) -> dict:
    never = list(getattr(cfg, "never_touch", ()) or ()) + list(getattr(cfg, "forbidden_files", ()) or ())
    upstream = [t for dep in _upstream(card, by_id) for t in dep.touches]
    missing = [
        rel for rel in named_files(card)
        if not _is_test_path(rel)
        and not matches_any(rel, list(card.touches))
        and not matches_any(rel, upstream)
        and not _really_exists(root / rel)
        and not matches_any(rel, never)
    ]
    problems = [f"the text names `{rel}`, which is not in touches and doesn't exist yet" for rel in missing]
    fix = {"touches": [*card.touches, *missing],
           "notes": [f"added {', '.join(f'`{m}`' for m in missing)} to the files this task may write"]} if missing else None
    return _result("touches", problems, fix)


# -- depends -------------------------------------------------------------------


def _check_depends(card: Card, by_id: dict[str, Card]) -> dict:
    problems: list[str] = []
    for dep in card.deps:
        other = by_id.get(dep)
        if other is None:
            problems.append(f"it waits for {dep}, which doesn't exist")
        elif str(other.state).lower() in _NOT_BUILDABLE:
            problems.append(f"it waits for {dep}, which was {other.state} and will never finish")
    if card.id in {up.id for dep in card.deps if (d := by_id.get(dep)) for up in [d, *_upstream(d, by_id)]}:
        problems.append("it waits for itself through the cards it depends on")
    return _result("depends", problems)


# -- seams ---------------------------------------------------------------------


def _ids(text: str) -> list[str]:
    found: list[str] = []
    for code in _CODE.findall(text):
        for name in _ID_SELECTOR.findall(code):
            if name not in found and not _HEX_COLOUR.fullmatch(name):
                found.append(name)
    return found


def _serves_markup(card: Card) -> bool:
    text = card.done_text or ""
    return len(_ids(text)) >= 2 and bool(re.search(r"<link|<script|\bHTML\b|\bshell\b|\.html\b", text))


def _stylesheet_section(card: Card) -> str:
    """The lines of the card that describe its stylesheet: the `.css` line and what is indented under it."""
    lines = (card.done_text or "").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not re.search(r"`[^`]*\.css`", line) or not line.lstrip().startswith(("-", "*")):
            continue
        indent = len(line) - len(line.lstrip())
        out.append(line)
        for later in lines[i + 1:]:
            if later.strip() and len(later) - len(later.lstrip()) <= indent:
                break
            out.append(later)
    return "\n".join(out)


def _check_seams(card: Card, by_id: dict[str, Card]) -> dict:
    if not any(str(t).lower().endswith(".css") for t in [*card.touches, *named_files(card)]):
        return _result("seams", [])
    section = _stylesheet_section(card)
    if not section:
        return _result("seams", [])
    problems: list[str] = []
    for owner in _upstream(card, by_id):
        if not _serves_markup(owner):
            continue
        owned = _ids(owner.done_text or "")
        styled = _ids(section)
        classes = [c for code in _CODE.findall(section) for c in _CLASS_SELECTOR.findall(code)]
        unstyled = [name for name in owned if name not in styled]
        unknown = [f"#{name}" for name in styled if name not in owned] + [f".{c}" for c in classes]
        if unstyled:
            problems.append(
                f"{owner.id} serves the page with {', '.join(f'`#{n}`' for n in owned)}, but this task's stylesheet "
                f"never names {', '.join(f'`#{n}`' for n in unstyled)}, so nothing says how they are shown")
        if unknown:
            problems.append(
                f"its stylesheet styles {', '.join(f'`{u}`' for u in unknown)}, which {owner.id}'s page doesn't have")
    return _result("seams", problems)


# -- the whole card --------------------------------------------------------------


def check_card(card: Card, cfg, root: Path, cards: list[Card] | None = None) -> list[dict]:
    """Every preflight check for one card: `[{"check", "status", "detail", "fix"?}]`.

    A check named in the card's `preflight_skip` field is reported as `skipped`,
    for the case where a person has read the warning and wants the card as written.
    """
    from .gates.eligibility import check_eligibility

    root = Path(root)
    by_id = {c.id: c for c in (cards or [])}
    by_id[card.id] = card
    skip = {str(s).lower() for s in (card.fields.get("preflight_skip") or [])}
    results = [
        _check_scaffold(card, cfg, root),
        _check_touches(card, cfg, root, by_id),
        _check_depends(card, by_id),
        _check_seams(card, by_id),
    ]
    try:
        failures = check_eligibility(card, cfg, worktree=root).failures
        results.append(_result("rules", [str(f.get("detail") or f.get("rule")) for f in failures]))
    except Exception as exc:  # noqa: BLE001 - an odd card is reported, not raised
        results.append(_result("rules", [f"could not be checked: {exc}"]))
    for item in results:
        if item["status"] == "fail" and item["check"] in skip:
            item.update(status="skipped", detail=f"kept as written ({item['detail']})")
            item.pop("fix", None)
    return results


def failures(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c["status"] == "fail"]


def apply_fixes(header: dict, body: str, checks: list[dict]) -> tuple[dict, str, list[str]]:
    """Apply each failed check's own fix to a card. Returns the card and one sentence per repair."""
    notes: list[str] = []
    for item in failures(checks):
        fix = item.get("fix")
        if not fix:
            continue
        if "done_text" in fix:
            body = fix["done_text"]
        if "touches" in fix:
            header = dict(header, touches=list(fix["touches"]))
        notes.extend(fix.get("notes") or [])
    return header, body, notes
