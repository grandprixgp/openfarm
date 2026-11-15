#!/usr/bin/env python3
"""Stand-in ACP v1 NDJSON-RPC 2.0 agent over stdio for OpenFarm tests.

Supports normal sessions and hostile/adversarial protocol scenarios via CLI
flags or environment variables (``ACP_STANDIN_MODE``, ``ACP_STANDIN_SCRIPT``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_FORBIDDEN_ENV_EXACT = ("CLAUDECODE", "ANTHROPIC_AUTH_TOKEN", "AI_AGENT")


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read_msg() -> dict | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        return json.loads(line)


def _env_leaks() -> list[str]:
    leaks: list[str] = []
    for key in os.environ:
        if key in _FORBIDDEN_ENV_EXACT or key.startswith("CLAUDE_CODE_"):
            leaks.append(key)
    return sorted(leaks)


def _send_chunk(session_id: str, text: str, *, as_list: bool = False) -> None:
    content = [{"type": "text", "text": text}] if as_list else {"type": "text", "text": text}
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": content,
                },
            },
        }
    )


def _send_usage(
    session_id: str, tokens_in: int, tokens_out: int, cost_amount: float
) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "usage_update",
                    "inputTokens": tokens_in,
                    "outputTokens": tokens_out,
                    "cost": {"amount": cost_amount, "currency": "USD"},
                },
            },
        }
    )


def _rpc_roundtrip(req: dict) -> dict | None:
    _send(req)
    target_id = req["id"]
    while True:
        msg = _read_msg()
        if msg is None:
            return None
        if msg.get("id") == target_id and "method" not in msg:
            return msg
        # If client sends session/cancel while waiting, ignore or keep waiting.


def main() -> int:
    parser = argparse.ArgumentParser(description="ACP v1 test stand-in")
    parser.add_argument(
        "--mode",
        default=os.environ.get("ACP_STANDIN_MODE", "normal"),
        choices=(
            "normal",
            "hostile-perm",
            "hostile-fs",
            "dup-id",
            "elicitation",
            "replay",
            "unterminated-osc",
            "hang",
            "oversized",
            "script",
        ),
    )
    parser.add_argument(
        "--script",
        default=os.environ.get("ACP_STANDIN_SCRIPT", ""),
    )
    parser.add_argument("--perm-kind", default="execute")
    parser.add_argument("--perm-path", default="src/main.rs")
    parser.add_argument("--spoof-id", action="store_true")
    parser.add_argument("--omit-reject-once", action="store_true")
    parser.add_argument("--tokens-in", type=int, default=120)
    parser.add_argument("--tokens-out", type=int, default=45)
    parser.add_argument("--cost", type=float, default=0.0125)
    parser.add_argument("--hang-seconds", type=float, default=10.0)
    parser.add_argument("--ack-cancel", action="store_true")
    args = parser.parse_args()

    script_data: dict = {}
    if args.mode == "script" and args.script:
        script_data = json.loads(Path(args.script).read_text(encoding="utf-8"))

    replay_rejection_code = None
    last_session_meta: dict = {}
    turn_count = 0

    while True:
        msg = _read_msg()
        if msg is None:
            return 0

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            leaks = _env_leaks()
            if leaks:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32000,
                            "message": f"Forbidden env vars leaked: {', '.join(leaks)}",
                        },
                    }
                )
                return 2
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "home": os.environ.get("HOME", ""),
                        },
                    },
                }
            )
            continue

        if method == "session/new":
            last_session_meta = params.get("_meta") or {}
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"sessionId": "sess-standin-1"},
                }
            )
            continue

        if method == "session/load":
            sid = str(params.get("sessionId") or "sess-standin-1")
            if args.mode == "replay":
                _send_chunk(sid, "REPLAYED_HISTORY_NOISE")
                resp = _rpc_roundtrip(
                    {
                        "jsonrpc": "2.0",
                        "id": "replay-req-1",
                        "method": "fs/read_text_file",
                        "params": {"path": "src/lib.rs"},
                    }
                )
                if resp and "error" in resp:
                    replay_rejection_code = resp["error"].get("code")
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"sessionId": sid},
                }
            )
            continue

        if method == "session/close":
            if req_id is not None:
                _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return 0

        if method == "session/cancel":
            continue

        if method == "session/prompt":
            turn_count += 1
            sid = str(params.get("sessionId") or "sess-standin-1")
            prompt_items = params.get("prompt") or []
            prompt_text = ""
            if prompt_items and isinstance(prompt_items[0], dict):
                prompt_text = str(prompt_items[0].get("text", ""))

            if args.mode == "normal":
                leaks = _env_leaks()
                if leaks:
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32000,
                                "message": f"Env leaks: {leaks}",
                            },
                        }
                    )
                    return 2
                # Support micromanager-shaped JSON responses when called via
                # ClaudeAcpMicromanager, or a generic JSON echo otherwise.
                if "Operation: shape_and_plan" in prompt_text:
                    answer = {
                        "verdict": "ready",
                        "clarified_notes": "stand-in ok",
                        "extra_context_files": [],
                        "steps": [],
                        "findings": [],
                    }
                elif "Operation: coach" in prompt_text:
                    answer = {
                        "root_cause": "missing guard",
                        "guidance": "check empty input before indexing",
                        "abort": False,
                        "abort_reason": "",
                    }
                elif "Operation: adjudicate_checker" in prompt_text:
                    answer = {
                        "verdict": "uphold",
                        "rationale": "counter-test is valid",
                        "fix_hint": "handle boundary",
                    }
                elif "Operation: pre_review" in prompt_text:
                    answer = {
                        "verdict": "approve",
                        "reviewer_brief": "looks solid",
                        "missed_clause": "",
                        "findings": [],
                    }
                elif "Operation: reflect_harness" in prompt_text:
                    data_payload = {}
                    marker_start = "--- BEGIN DATA ---"
                    marker_end = "--- END DATA ---"
                    if marker_start in prompt_text and marker_end in prompt_text:
                        raw_json = (
                            prompt_text.split(marker_start, 1)[1]
                            .split(marker_end, 1)[0]
                            .strip()
                        )
                        try:
                            data_payload = json.loads(raw_json)
                        except ValueError:
                            data_payload = {}
                    contrast = data_payload.get("contrast") or {}
                    card_id = str(
                        data_payload.get("card_id") or contrast.get("card_id") or "CARD"
                    )
                    outcome_kind = str(contrast.get("outcome_kind") or "retry_recovery")
                    failure_class = str(contrast.get("failure_class") or "type-mismatch")
                    lang = str(contrast.get("language") or "rust")
                    kind = str(contrast.get("kind") or "pinned-bugfix")
                    if outcome_kind == "counter_fail":
                        surface = "implementer_guidance"
                        role = "implementer"
                        cue = (
                            f"Guard edge-case and boundary inputs before returning in {kind} changes "
                            f"so counter-tests cannot trigger uncaught branches."
                        )
                        lesson = f"Card {card_id} failed counter-test verification ({failure_class})."
                        conf = 0.78
                    elif outcome_kind == "retry_recovery":
                        surface = "implementer_guidance"
                        role = "implementer"
                        cue = (
                            f"Verify {failure_class} constraints and exact return variants on attempt 1 "
                            f"before submitting edits."
                        )
                        lesson = (
                            f"Card {card_id} hit {failure_class} on attempt 1 and recovered on retry."
                        )
                        conf = 0.84
                    elif outcome_kind == "fail":
                        surface = "planner_guidance"
                        role = "planner"
                        cue = (
                            f"Break down {kind} cards encountering {failure_class} into smaller explicit "
                            f"steps with verified helper signatures."
                        )
                        lesson = f"Card {card_id} failed with {failure_class}; finer scoping helps."
                        conf = 0.72
                    else:
                        surface = "context_hints"
                        role = "*"
                        cue = f"Keep {kind} changes minimal and check existing module conventions first."
                        lesson = f"Card {card_id} passed cleanly."
                        conf = 0.68
                    answer = {
                        "summary": f"Reflected on {outcome_kind} ({failure_class}) for {card_id}",
                        "lessons": [
                            {
                                "title": f"Reflect {outcome_kind}: {failure_class}",
                                "surface": surface,
                                "role": role,
                                "language": lang,
                                "kind": kind,
                                "failure_class": failure_class,
                                "cue": cue,
                                "lesson": lesson,
                                "evidence": str(
                                    (contrast.get("evidence_pair") or {}).get("delta", "")
                                ),
                                "confidence": conf,
                                "tags": (
                                    [outcome_kind, failure_class]
                                    if failure_class
                                    else [outcome_kind]
                                ),
                                "proposed_patch": {surface: cue},
                            }
                        ],
                    }
                else:
                    answer = {
                        "echo": prompt_text,
                        "home": os.environ.get("HOME", ""),
                        "meta": last_session_meta,
                    }
                payload_text = json.dumps(answer)
                mid = max(1, len(payload_text) // 2)
                _send_chunk(sid, payload_text[:mid], as_list=False)
                _send_chunk(sid, payload_text[mid:], as_list=True)
                _send_usage(sid, args.tokens_in, args.tokens_out, args.cost)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "hostile-perm":
                options = [
                    {
                        "optionId": "reject_once" if args.spoof_id else "opt-allow-always",
                        "kind": "allow_always",
                        "name": "Always Allow",
                    },
                    {
                        "optionId": "opt-allow-once",
                        "kind": "allow_once",
                        "name": "Allow Once",
                    },
                ]
                if not args.omit_reject_once:
                    options.append(
                        {
                            "optionId": "opt-reject-once",
                            "kind": "reject_once",
                            "name": "Reject Once",
                        }
                    )
                perm_resp = _rpc_roundtrip(
                    {
                        "jsonrpc": "2.0",
                        "id": f"perm-{turn_count}",
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": sid,
                            "toolCall": {
                                "kind": args.perm_kind,
                                "locations": [{"path": args.perm_path}],
                                "rawInput": {"file_path": args.perm_path},
                            },
                            "options": options,
                        },
                    }
                )
                _send_chunk(sid, json.dumps({"perm_response": perm_resp}))
                _send_usage(sid, args.tokens_in, args.tokens_out, args.cost)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "hostile-fs":
                results = {}
                for idx, mname in enumerate(
                    ("fs/write_text_file", "fs/read_text_file", "terminal/create", "mystery/op"),
                    start=1,
                ):
                    resp = _rpc_roundtrip(
                        {
                            "jsonrpc": "2.0",
                            "id": f"fs-{turn_count}-{idx}",
                            "method": mname,
                            "params": {"path": "src/lib.rs", "content": "pwn"},
                        }
                    )
                    results[mname] = resp
                _send_chunk(sid, json.dumps({"fs_responses": results}))
                _send_usage(sid, args.tokens_in, args.tokens_out, args.cost)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "dup-id":
                r1 = _rpc_roundtrip(
                    {
                        "jsonrpc": "2.0",
                        "id": "dup-1",
                        "method": "elicitation/create",
                        "params": {"message": "first"},
                    }
                )
                r2 = _rpc_roundtrip(
                    {
                        "jsonrpc": "2.0",
                        "id": "dup-1",
                        "method": "elicitation/create",
                        "params": {"message": "second"},
                    }
                )
                _send_chunk(sid, json.dumps({"first": r1, "second": r2}))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "elicitation":
                resp = _rpc_roundtrip(
                    {
                        "jsonrpc": "2.0",
                        "id": f"elicit-{turn_count}",
                        "method": "elicitation/create",
                        "params": {"message": "enter secret"},
                    }
                )
                _send_chunk(sid, json.dumps({"elicitation": resp}))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "replay":
                _send_chunk(
                    sid,
                    json.dumps(
                        {
                            "replay_rejection_code": replay_rejection_code,
                            "fresh": True,
                        }
                    ),
                )
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "unterminated-osc":
                payload = json.dumps(
                    {
                        "verdict": "ready",
                        "clarified_notes": "osc survived",
                        "extra_context_files": [],
                        "steps": [],
                        "findings": [],
                    }
                )
                _send_chunk(sid, "\x1b]8;;http://x\x1b[31m" + payload)
                _send_usage(sid, args.tokens_in, args.tokens_out, 0.0)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue

            if args.mode == "hang":
                _send_chunk(sid, "partial-before-hang")
                deadline = time.monotonic() + args.hang_seconds
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                    if args.ack_cancel:
                        # Non-blocking check isn't strictly needed if parent kills or we read.
                        import select

                        readable, _, _ = select.select([sys.stdin], [], [], 0.0)
                        if readable:
                            cancel_msg = _read_msg()
                            if cancel_msg and cancel_msg.get("method") == "session/cancel":
                                _send(
                                    {
                                        "jsonrpc": "2.0",
                                        "id": req_id,
                                        "result": {"stopReason": "cancelled"},
                                    }
                                )
                                break
                continue

            if args.mode == "oversized":
                big = "x" * (8 * 1024 * 1024 + 1024)
                _send({"jsonrpc": "2.0", "method": "session/update", "params": {"data": big}})
                continue

            if args.mode == "script":
                turns = script_data.get("turns") or []
                entry = turns[min(turn_count - 1, len(turns) - 1)] if turns else {}
                for req in entry.get("requests") or []:
                    _rpc_roundtrip(req)
                for chunk in entry.get("chunks") or [entry.get("text", "{}")]:
                    _send_chunk(sid, str(chunk))
                usage = entry.get("usage") or {}
                _send_usage(
                    sid,
                    int(usage.get("tokens_in", args.tokens_in)),
                    int(usage.get("tokens_out", args.tokens_out)),
                    float(usage.get("cost", args.cost)),
                )
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"stopReason": str(entry.get("stop_reason", "end_turn"))},
                    }
                )
                continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
