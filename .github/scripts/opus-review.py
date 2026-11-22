#!/usr/bin/env python3
"""
Tier-2 PR reviewer calling the Anthropic Messages API directly (stdlib-only).
Designed to spend Anthropic Console API / Ultra API credits with prompt caching
and strictly read-only repository inspection tools.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TURNS = int(os.environ.get("OPUS_MAX_TURNS", "24"))
MAX_TOOL_OUTPUT_CHARS = 32_000
BLOCKED_SUFFIXES = (".pem", ".key")
BLOCKED_BASENAMES = (".env",)

REPO_ROOT = pathlib.Path(
    subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
).resolve()


def is_blocked_path(rel: pathlib.Path) -> bool:
    name = rel.name.lower()
    return name in BLOCKED_BASENAMES or name.startswith(".env.") or name.endswith(BLOCKED_SUFFIXES)


def run_cmd(args: list[str], *, check: bool = False) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {args}\n{proc.stdout}")
    return proc.returncode, proc.stdout


def clip(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit - 200
    return (
        text[:half]
        + f"\n\n... [truncated {len(text) - half} chars; narrow your path or line range] ..."
    )


def safe_rel_path(raw: str) -> pathlib.Path:
    if not raw or raw.startswith("/") or ".." in pathlib.PurePosixPath(raw).parts:
        raise ValueError(f"Path must be repo-relative without '..': {raw!r}")
    resolved = (REPO_ROOT / raw).resolve()
    try:
        rel = resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(f"Path escapes repository root: {raw!r}") from exc
    return rel


def tool_git_diff(base_sha: str, head_sha: str, inp: dict) -> str:
    stat_only = bool(inp.get("stat_only", False))
    path = inp.get("path")
    cmd = ["git", "diff", "--no-ext-diff"]
    if stat_only:
        cmd.append("--stat")
    cmd.append(f"{base_sha}..{head_sha}")
    cmd.append("--")
    if path:
        rel = safe_rel_path(str(path))
        if is_blocked_path(rel) and not stat_only:
            return f"{rel.as_posix()}: raw diff omitted by policy."
        cmd.append(rel.as_posix())
    else:
        cmd.extend([".", ":(exclude)*.pem", ":(exclude)*.key", ":(exclude).env", ":(exclude).env.*"])
    rc, out = run_cmd(cmd)
    if rc != 0:
        return f"git diff exited {rc}:\n{clip(out)}"
    return clip(out or "(no diff)")


def tool_git_log(base_sha: str, head_sha: str) -> str:
    _, log_out = run_cmd(["git", "log", "--oneline", f"{base_sha}..{head_sha}"])
    chk_rc, chk_out = run_cmd(["git", "diff", "--check", f"{base_sha}..{head_sha}"])
    chk_summary = "clean (exit 0)" if chk_rc == 0 else f"ISSUES (exit {chk_rc}):\n{chk_out}"
    return clip(f"Commits ({base_sha[:9]}..{head_sha[:9]}):\n{log_out}\ngit diff --check: {chk_summary}")


def tool_read_file(inp: dict) -> str:
    rel = safe_rel_path(str(inp.get("path", "")))
    rel_posix = rel.as_posix()
    if is_blocked_path(rel):
        return f"{rel_posix}: raw content omitted by policy."
    full = REPO_ROOT / rel
    if not full.is_file():
        return f"File not found: {rel_posix}"
    lines = full.read_text(errors="replace").splitlines()
    start = max(1, int(inp.get("start_line", 1)))
    end = min(len(lines), int(inp.get("end_line", start + 249)))
    if start > len(lines):
        return f"{rel_posix}: start_line {start} exceeds length {len(lines)}"
    numbered = [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)]
    header = f"{rel_posix} (lines {start}-{end} of {len(lines)}):\n"
    return clip(header + "\n".join(numbered))


def tool_rg_search(inp: dict) -> str:
    pattern = str(inp.get("pattern", "")).strip()
    if not pattern:
        return "pattern is required"
    cmd = [
        "rg",
        "-n",
        "--max-count",
        "40",
        "--glob",
        "!.git/**",
        "--glob",
        "!*.pem",
        "--glob",
        "!*.key",
        "--glob",
        "!.env",
        "--glob",
        "!.env.*",
        "--",
        pattern,
    ]
    path = inp.get("path")
    if path:
        cmd.append(safe_rel_path(str(path)).as_posix())
    rc, out = run_cmd(cmd)
    if rc == 1:
        return "(no matches)"
    if rc not in (0, 1):
        return f"rg exited {rc}:\n{clip(out)}"
    return clip(out)


def tool_list_files(inp: dict) -> str:
    prefix = inp.get("prefix")
    cmd = ["git", "ls-files"]
    if prefix:
        cmd.append(safe_rel_path(str(prefix)).as_posix())
    rc, out = run_cmd(cmd)
    if rc != 0:
        return f"git ls-files exited {rc}:\n{clip(out)}"
    return clip(out or "(no files)")


TOOLS = [
    {
        "name": "git_diff",
        "description": "Show git diff between PR base and head commits. Use stat_only=true first for large PRs, then pass path for specific files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional repo-relative file or directory path."},
                "stat_only": {"type": "boolean", "description": "If true, return only --stat summary."},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Show commit list and git diff --check whitespace status for the PR range.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_file",
        "description": "Read a line range from a repo-relative file at the checked-out PR head.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "start_line": {"type": "integer", "description": "1-based start line (default 1)."},
                "end_line": {"type": "integer", "description": "1-based inclusive end line (default start_line + 249)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "rg_search",
        "description": "Search code with ripgrep across the checked-out PR head to trace callers and parity surfaces.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Ripgrep regex pattern."},
                "path": {"type": "string", "description": "Optional repo-relative subdirectory or file."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_files",
        "description": "List tracked repository files under an optional repo-relative prefix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "Optional repo-relative directory prefix."},
            },
        },
        "cache_control": {"type": "ephemeral"},
    },
]


def call_anthropic(api_key: str, model: str, system_prompt: str, messages: list[dict]) -> dict:
    payload = {
        "model": model,
        "max_tokens": 8192,
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": TOOLS,
        "messages": messages,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API HTTP {err.code}: {body}") from err


def execute_tool(name: str, inp: dict, base_sha: str, head_sha: str) -> str:
    try:
        if name == "git_diff":
            return tool_git_diff(base_sha, head_sha, inp)
        if name == "git_log":
            return tool_git_log(base_sha, head_sha)
        if name == "read_file":
            return tool_read_file(inp)
        if name == "rg_search":
            return tool_rg_search(inp)
        if name == "list_files":
            return tool_list_files(inp)
        return f"Unknown tool: {name}"
    except Exception as exc:
        return f"Tool error ({name}): {exc}"


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is empty", file=sys.stderr)
        return 1

    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    pr_number = os.environ["PR_NUMBER"].strip()
    base_sha = os.environ["BASE_SHA"].strip()
    head_sha = os.environ["HEAD_SHA"].strip()
    pr_title = os.environ.get("PR_TITLE", "").strip()
    pr_body = os.environ.get("PR_BODY", "").strip()
    pr_labels = os.environ.get("PR_LABELS", "").strip()

    prompt_path = pathlib.Path(
        os.environ.get("REVIEW_PROMPT_PATH", str(REPO_ROOT / ".github" / "claude-review-prompt.md"))
    )
    system_prompt = prompt_path.read_text(encoding="utf-8")

    stat = tool_git_diff(base_sha, head_sha, {"stat_only": True})
    log_summary = tool_git_log(base_sha, head_sha)

    user_opening = (
        f"Review PR #{pr_number}: {pr_title}\n"
        f"Labels: {pr_labels or '(none)'}\n"
        f"Base SHA: {base_sha[:9]} | Head SHA: {head_sha[:9]}\n\n"
        f"PR Description:\n{pr_body or '(empty)'}\n\n"
        f"{log_summary}\n\n"
        f"Diff stat:\n{stat}\n\n"
        "Inspect the diff and affected callers using your read-only tools, then output "
        "ONLY the final structured review comment following the exact required format."
    )

    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_opening,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]

    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    final_text = ""

    for turn in range(1, MAX_TURNS + 1):
        resp = call_anthropic(api_key, model, system_prompt, messages)
        usage = resp.get("usage", {})
        totals["input"] += int(usage.get("input_tokens", 0))
        totals["output"] += int(usage.get("output_tokens", 0))
        totals["cache_read"] += int(usage.get("cache_read_input_tokens", 0))
        totals["cache_create"] += int(usage.get("cache_creation_input_tokens", 0))

        content = resp.get("content", [])
        messages.append({"role": "assistant", "content": content})

        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        if not tool_uses:
            final_text = "\n\n".join(
                block.get("text", "").strip()
                for block in content
                if block.get("type") == "text" and block.get("text", "").strip()
            )
            print(
                f"Completed in {turn} turn(s) | model={resp.get('model', model)} | "
                f"in={totals['input']} cache_read={totals['cache_read']} "
                f"cache_create={totals['cache_create']} out={totals['output']}"
            )
            break

        # Keep at most 4 cache_control breakpoints total (system, tools, opening user msg, latest tool result).
        for prev in messages[1:]:
            if prev.get("role") == "user" and isinstance(prev.get("content"), list):
                for blk in prev["content"]:
                    if isinstance(blk, dict):
                        blk.pop("cache_control", None)

        tool_results = []
        for idx, use in enumerate(tool_uses):
            result_text = execute_tool(use["name"], use.get("input") or {}, base_sha, head_sha)
            entry: dict = {
                "type": "tool_result",
                "tool_use_id": use["id"],
                "content": result_text,
            }
            if idx == len(tool_uses) - 1:
                entry["cache_control"] = {"type": "ephemeral"}
            tool_results.append(entry)
        messages.append({"role": "user", "content": tool_results})
    else:
        raise RuntimeError(f"Exceeded MAX_TURNS={MAX_TURNS} without producing a final review")

    if not final_text.strip():
        raise RuntimeError("Model returned an empty final review")

    verdict = "Blocker" if "| `Blocker` |" in final_text or "| Blocker |" in final_text else "Approved"
    reviewer_label = os.environ.get("REVIEWER_LABEL", "Claude Opus 5.5 · Tier-2 Review").strip()
    meta_tag = (
        f"<!-- atlas-review-meta reviewer={model} tier=2 head={head_sha[:9]} verdict={verdict} -->"
    )
    header = f"{meta_tag}\n### {reviewer_label}\n\n"
    footer = (
        f"\n\n---\n"
        f"<sub>Tier-2 review · `{model}` via Anthropic Messages API · head `{head_sha[:9]}` · "
        f"tokens: in={totals['input']}, cache_read={totals['cache_read']}, "
        f"cache_write={totals['cache_create']}, out={totals['output']}</sub>"
    )
    comment_body = header + final_text.strip() + footer

    out_path = pathlib.Path(os.environ.get("REVIEW_OUT", "/tmp/opus-review.md"))
    out_path.write_text(comment_body, encoding="utf-8")
    print(f"Wrote review ({len(comment_body)} bytes) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
