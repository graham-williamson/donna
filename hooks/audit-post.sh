#!/usr/bin/env python3
"""audit-post.sh — Donna security v1.1 Phase 0 PostToolUse stub.

Spec: /Users/grahamwilliamson/.claude/plans/donna-security-v1.md §13.5, §17

Emits one JSONL line per tool call to
/Users/grahamwilliamson/donna/.claude/audit-fallback.log with fields:
  ts, tool, args_redacted, exit_code

args_redacted is the list of tool_input keys only — values are stripped
because a PostToolUse stub must not become an exfil channel for email
bodies, Notion pages, or command stdout. Phase 1 extends this to a
broker audit-result call that stores params_hash + resolved_summary.

Never blocks. Any error during logging is swallowed so a hook failure
cannot break the tool pipeline.
"""
import json
import os
import sys
import time

LOG_PATH = "/Users/grahamwilliamson/donna/.claude/audit-fallback.log"


def derive_exit_code(tool_response):
    if not isinstance(tool_response, dict):
        return 0
    # Claude Code surfaces both isError and is_error across tool types.
    for key in ("isError", "is_error", "error"):
        v = tool_response.get(key)
        if v:
            return 1
    return 0


def redact_args(tool_input):
    # Preserve shape (list of keys) without leaking values. Phase 1 replaces
    # this with params_hash via the broker; Phase 0 logs only what a reader
    # needs to reconstruct the call type.
    if isinstance(tool_input, dict):
        return sorted(tool_input.keys())
    return []


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": data.get("tool_name") or "",
        "args_redacted": redact_args(data.get("tool_input")),
        "exit_code": derive_exit_code(data.get("tool_response")),
    }

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        # Never block a tool pipeline because audit failed.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
