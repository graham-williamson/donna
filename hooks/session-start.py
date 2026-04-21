#!/usr/bin/env python3
"""session-start.py — SessionStart hook for Donna (security-v1.1 Piece A.5).

Runs once when Claude Code starts a session in this project. Pulls
two things into Donna's initial context:

1. The handoff summary from ~/.claude/channels/telegram/data/session-summary.md,
   which Donna herself writes at the end of each response. This is
   best-effort — if the previous session crashed mid-response, the
   file may be stale.

2. Authoritative pending-approval state from the broker via
   `donna-broker list-pending`. This half is machine-sourced so it
   cannot drift: if a request is approved but unexecuted, Donna will
   see it here and will not accidentally create a duplicate.

The merged markdown is injected via the SessionStart hook's
`hookSpecificOutput.additionalContext` channel so Claude Code shows
it to the model as part of the initial context.

Failure policy: best-effort. If the handoff file is missing or the
broker is unreachable, emit a clear "not available" stub rather than
blocking session start. A SessionStart hook that errors stops the
daemon from bringing Claude up; we'd rather ship Donna with a bit
less context than not ship her at all.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SUMMARY_PATH = (
    Path.home()
    / ".claude"
    / "channels"
    / "telegram"
    / "data"
    / "session-summary.md"
)
BROKER_BIN = "/usr/local/bin/donna-broker"
BROKER_TIMEOUT_SECONDS = 5.0


def read_summary() -> str:
    """Return the handoff file contents, or an explicit sentinel string
    if missing or unreadable. Never raises."""
    if not SUMMARY_PATH.exists():
        return (
            "_(no handoff file yet — first session, or the previous "
            "session did not update it)_"
        )
    try:
        text = SUMMARY_PATH.read_text(encoding="utf-8").strip()
    except Exception as e:
        return f"_(handoff file at {SUMMARY_PATH} unreadable: {e})_"
    return text or "_(handoff file empty)_"


def list_pending() -> list[dict]:
    """Ask the broker for currently-pending approvals. Fails open to
    an empty list — the handoff is useful without it, and we don't
    want a broker hiccup to block session start."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/sudo",
                "-u",
                "donna-broker",
                BROKER_BIN,
                "list-pending",
                "{}",
            ],
            capture_output=True,
            timeout=BROKER_TIMEOUT_SECONDS,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    try:
        resp = json.loads(result.stdout)
    except Exception:
        return []
    reqs = resp.get("requests") or []
    return reqs if isinstance(reqs, list) else []


def format_pending(rows: list[dict]) -> str:
    """Render pending approvals as a compact markdown list. `rows` is
    whatever the broker's list-pending returns; we tolerate missing
    fields rather than failing the whole handoff."""
    if not rows:
        return "_(no pending approvals)_"
    lines: list[str] = []
    for r in rows:
        cap = r.get("capability") or "?"
        code = r.get("approval_code") or "?"
        state = r.get("state") or "?"
        summary = r.get("resolved_summary") or ""
        lines.append(f"- `{code}` · {cap} · **{state}** — {summary}")
    return "\n".join(lines)


def build_context() -> str:
    summary = read_summary()
    pending_md = format_pending(list_pending())
    return (
        "## Session handoff from previous turn\n\n"
        f"{summary}\n\n"
        "## Authoritative pending approvals (broker, live)\n\n"
        f"{pending_md}\n"
    )


def emit(context: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(payload))


def main() -> int:
    # Drain stdin so Claude Code's hook envelope flows through; we
    # don't use any field from it but leaving it unread can in
    # principle deadlock the writer.
    try:
        sys.stdin.read()
    except Exception:
        pass
    emit(build_context())
    return 0


if __name__ == "__main__":
    sys.exit(main())
