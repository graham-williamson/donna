"""SQLite persistence for the request state machine.

Spec: security-v1.1 §6 (schema), §7.5 (durability), §5 (state machine).

Responsibilities:
  - Create the DB with WAL mode and the §6 schema on first use.
  - Enforce immutability at the DB layer via triggers on every field
    classified "immutable at creation" and "set once on approval" (§6).
  - Provide the narrow API the rest of the broker uses: insert a request,
    look up by id / approval_code / idempotency_key, transition state
    (guarded by allowed-transition table), count pending, archive.
  - Own the daily `.backup` snapshot (§7.5).

Does NOT:
  - Verify HMAC — that's policy.py (HMAC-verify-before-transition order
    is enforced at the call site).
  - Touch audit JSONL — audit.py is authoritative for the immutable
    record.
  - Make network calls — this module is local-only, ever.

Phase 1 Ralph target — see `broker/ralph-prompts/requests_db.md`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# §6 schema is authoritative. Keep the SQL text in sync with the spec.
SCHEMA_SQL = """
-- See security-v1.1 §6 for the canonical schema.
-- Ralph target: transcribe the full schema including both partial unique
-- indexes (idx_approval_code_active, idx_idempotency_active) and the
-- idx_state index.
"""


@dataclass(frozen=True)
class Request:
    """In-memory projection of a row. Frozen because immutable fields
    must not be mutated in-process any more than on disk.
    """
    request_id: str
    capability: str
    params_json: str
    params_hash: str
    idempotency_key: str
    resolved_summary: str
    context_reason: Optional[str]
    risk_level: str
    state: str
    approval_code: Optional[str]
    approval_hmac: Optional[str]
    created_at: int
    approval_expires_at: int
    execution_expires_at: Optional[int]
    approved_at: Optional[int]
    executed_at: Optional[int]
    result_json: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    prev_audit_hash: Optional[str]


def open_db(path: str) -> Any:
    """Open or initialise the SQLite DB at `path`. WAL mode, triggers,
    indexes. Idempotent. Returns a connection."""
    raise NotImplementedError("open_db: Phase 1 Ralph target")


def insert_request(conn: Any, request: Request) -> None:
    raise NotImplementedError("insert_request: Phase 1 Ralph target")


def get_request(conn: Any, request_id: str) -> Optional[Request]:
    raise NotImplementedError("get_request: Phase 1 Ralph target")


def get_by_approval_code(conn: Any, code: str) -> Optional[Request]:
    raise NotImplementedError("get_by_approval_code: Phase 1 Ralph target")


def get_by_idempotency_key(conn: Any, key: str) -> Optional[Request]:
    raise NotImplementedError("get_by_idempotency_key: Phase 1 Ralph target")


def transition(
    conn: Any,
    request_id: str,
    from_state: str,
    to_state: str,
    **mutable_fields: Any,
) -> None:
    """Apply `from_state`→`to_state` atomically. Raises on invalid transition
    per §5. Caller must have already verified HMAC (§7.3).
    """
    raise NotImplementedError("transition: Phase 1 Ralph target")


def count_pending(conn: Any) -> int:
    """Cheap count of state='approved' rows. Used by §13.6 pending_summary."""
    raise NotImplementedError("count_pending: Phase 1 Ralph target")


def daily_backup(conn: Any, backup_dir: str) -> Optional[str]:
    """Opportunistic per-UTC-day .backup snapshot. Returns path on write,
    None when already snapshotted today. §7.5."""
    raise NotImplementedError("daily_backup: Phase 1 Ralph target")
