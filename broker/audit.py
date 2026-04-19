"""Hash-chained JSONL audit writer.

Spec: security-v1.1 §7.6, §15 (event list), §5 (integrity scope).

Contract:
  - Append-only. File opened O_APPEND; never truncated or seeked.
  - Every entry carries prev_hash = sha256(previous canonical entry).
  - Rotation: 100MB or 30 days. Rotating writes `segment_seal` with the
    tail hash; next segment's first entry references that hash as its
    prev_hash.
  - `verify_chain` walks segments in order, returns the offset/file of
    the first break or None when clean.
  - Source of truth on conflict with SQLite: JSONL wins (§7.6).
  - Must never write: credential values, raw email/Notion bodies, raw
    MCP responses, plaintext approval codes, plaintext params_json,
    stack traces, screenshot bytes, HMAC key contents, bot token (§15).

The writer is the only code path allowed to touch
/Users/donna-broker/audit/*.log — the hook enforces that Donna's Claude
process never reads those files.

Phase 1 Ralph target — see `broker/ralph-prompts/audit.md`.
"""
from __future__ import annotations

from typing import Any, Optional


def write_event(audit_dir: str, event: dict[str, Any]) -> str:
    """Append `event` to the current segment. Returns the sha256 hex of
    the canonical entry (for caller to store in requests.prev_audit_hash)."""
    raise NotImplementedError("write_event: Phase 1 Ralph target")


def rotate_if_needed(audit_dir: str) -> Optional[str]:
    """Seal the current segment and roll to a new one if size or age
    thresholds exceeded. Returns the sealed segment path or None."""
    raise NotImplementedError("rotate_if_needed: Phase 1 Ralph target")


def verify_chain(audit_dir: str) -> Optional[dict[str, Any]]:
    """Walk all segments, verify prev_hash chain end-to-end. Returns None
    on clean, or a dict describing the first break (file, line, reason)."""
    raise NotImplementedError("verify_chain: Phase 1 Ralph target")
