#!/usr/bin/env python3
"""audit-post-phase1.py — Donna security v1.1 Phase 1 PostToolUse hook.

Spec: security-v1.1 §13.5 (hook contracts), §10 (failure semantics).

Routes every tool call's outcome to `donna-broker audit-result` so
the broker owns the immutable audit chain. Broker unreachable or slow
(>2s) → falls back to the local audit-fallback.log so no call goes
unrecorded. Never blocks the tool pipeline regardless of outcome.

Replaces hooks/audit-post.sh once the broker is live.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


BROKER_BIN = "/usr/local/bin/donna-broker"
BROKER_TIMEOUT_SECONDS = 2.0  # §13.5
FALLBACK_LOG = "/Users/grahamwilliamson/donna/.claude/audit-fallback.log"


def redact_args(tool_input):
    if isinstance(tool_input, dict):
        return sorted(tool_input.keys())
    return []


def derive_outcome(tool_response) -> str:
    if not isinstance(tool_response, dict):
        return "succeeded"
    for key in ("isError", "is_error", "error"):
        if tool_response.get(key):
            return "failed"
    return "succeeded"


def fallback_log(event: dict) -> None:
    try:
        Path(FALLBACK_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(FALLBACK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        # Never propagate — PostToolUse must not block.
        pass


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_name": data.get("tool_name") or "",
        "outcome": derive_outcome(data.get("tool_response")),
        "args_redacted": redact_args(data.get("tool_input")),
        # Optional — the broker uses this to close out executing rows.
        "request_id": data.get("tool_input", {}).get("_donna_request_id"),
    }

    try:
        result = subprocess.run(
            ["sudo", "-u", "donna-broker", BROKER_BIN, "audit-result"],
            input=json.dumps(entry).encode("utf-8"),
            capture_output=True,
            timeout=BROKER_TIMEOUT_SECONDS,
        )
        if result.returncode not in (0, 1):
            entry["broker_error"] = result.stderr.decode(
                "utf-8", errors="replace"
            )[:300]
            fallback_log(entry)
    except subprocess.TimeoutExpired:
        entry["broker_error"] = "timeout"
        fallback_log(entry)
    except FileNotFoundError:
        entry["broker_error"] = "broker_not_installed"
        fallback_log(entry)
    except Exception as e:
        entry["broker_error"] = f"{type(e).__name__}: {e}"
        fallback_log(entry)

    sys.exit(0)


if __name__ == "__main__":
    main()
