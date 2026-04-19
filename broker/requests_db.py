"""SQLite persistence for the request state machine.

Spec: security-v1.1 §6 (schema), §7.5 (durability), §5 (state machine),
§11 (replay semantics).

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

Every public SQL call uses parameterised queries (no string-formatted
user data). The only dynamic SQL is trigger-body interpolation and
UPDATE column lists in `transition()`, where the identifiers are
validated against an allowlist before interpolation.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Optional


# §6 schema. Kept verbatim with the spec — change spec first.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY,
  capability TEXT NOT NULL,
  params_json TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  resolved_summary TEXT NOT NULL,
  context_reason TEXT,
  risk_level TEXT NOT NULL,
  state TEXT NOT NULL,
  approval_code TEXT,
  approval_hmac TEXT,
  created_at INTEGER NOT NULL,
  approval_expires_at INTEGER NOT NULL,
  execution_expires_at INTEGER,
  approved_at INTEGER,
  executed_at INTEGER,
  result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  prev_audit_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_code_active
  ON requests(approval_code)
  WHERE state IN ('pending_approval','approved');
CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency_active
  ON requests(idempotency_key)
  WHERE state NOT IN ('denied','expired','failed','cancelled','integrity_failed');
CREATE INDEX IF NOT EXISTS idx_state ON requests(state);

CREATE TABLE IF NOT EXISTS rate_limits (
  capability TEXT NOT NULL,
  date_utc TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (capability, date_utc)
);

CREATE TABLE IF NOT EXISTS broker_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


# §6 field classifications.
IMMUTABLE_AT_CREATION = (
    "request_id",
    "capability",
    "params_json",
    "params_hash",
    "idempotency_key",
    "risk_level",
    "created_at",
    "approval_expires_at",
)

# These start NULL at creation and may be set exactly once, on the
# pending_approval → approved transition. Subsequent changes are rejected.
#
# Note: approval_hmac is NOT in this tuple. Per §7.3, the HMAC is
# computed at row creation (covering creation fields) and recomputed at
# approval (extending coverage to execution_expires_at + approved_at).
# Exactly one rewrite is expected — the HMAC verification itself is
# what protects the field at rest.
SET_ONCE_ON_APPROVAL = (
    "execution_expires_at",
    "approved_at",
)

# Fields that may change freely during the row's lifetime.
MUTABLE_FIELDS = frozenset({
    "state",
    "executed_at",
    "result_json",
    "error_code",
    "error_message",
    "prev_audit_hash",
    # approval_hmac is rewritten exactly once (at approval, §7.3). Kept
    # mutable here so transition() allows the update; the HMAC verify
    # path is what enforces integrity.
    "approval_hmac",
    # resolved_summary and context_reason are derived/display — not HMAC
    # covered (§6) and may be regenerated. Kept mutable to allow resolver
    # refresh without tripping triggers.
    "resolved_summary",
    "context_reason",
})


# §5 state-machine transition table. (from_state, to_state) pairs.
# `integrity_failed` is NOT listed — it's reachable from any non-terminal
# tracked state via `quarantine()`, not via `transition()`.
VALID_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("created", "auto_approved"),
    ("created", "pending_approval"),
    ("auto_approved", "executing"),
    ("auto_approved", "cancelled"),
    ("pending_approval", "approved"),
    ("pending_approval", "denied"),
    ("pending_approval", "expired"),
    ("pending_approval", "cancelled"),
    ("approved", "executing"),
    ("approved", "expired"),
    ("approved", "cancelled"),
    # Override path (§7.4): only from denied/expired back to pending.
    ("denied", "pending_approval"),
    ("expired", "pending_approval"),
    # Execution outcomes.
    ("executing", "succeeded"),
    ("executing", "failed"),
    ("executing", "reconciliation_needed"),
    # Manual reconcile (§11 rule 9).
    ("reconciliation_needed", "succeeded"),
    ("reconciliation_needed", "failed"),
})

# States that cannot be transitioned out of (except to integrity_failed
# via quarantine, which is a separate path).
TERMINAL_STATES = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "integrity_failed",
})


class InvalidTransition(Exception):
    """State transition not permitted by §5."""


class ImmutableFieldViolation(Exception):
    """Attempted write to a §6 immutable or set-once field."""


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


_REQUEST_FIELDS = tuple(f.name for f in fields(Request))


def open_db(path: str) -> sqlite3.Connection:
    """Open or initialise the SQLite DB at `path`. WAL mode, triggers,
    partial unique indexes, idx_state. Idempotent across calls."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # autocommit=None keeps the default Python behaviour (BEGIN on
    # modification, COMMIT on transaction end). `with conn:` wraps
    # multi-statement atomic work.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL mode must be set before the first DDL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _install_triggers(conn)
    conn.commit()
    return conn


def _install_triggers(conn: sqlite3.Connection) -> None:
    """Install BEFORE UPDATE triggers that enforce §6 immutability."""
    # Immutable-at-creation: any change raises.
    for field in IMMUTABLE_AT_CREATION:
        # Identifier is static; no user input. Safe to interpolate.
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_immutable_{field}
            BEFORE UPDATE OF {field} ON requests
            BEGIN
                SELECT RAISE(ABORT, 'immutable field: {field}')
                WHERE OLD.{field} IS NOT NEW.{field};
            END
            """
        )
    # Set-once: only NULL → value is allowed. value → different value raises.
    for field in SET_ONCE_ON_APPROVAL:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_set_once_{field}
            BEFORE UPDATE OF {field} ON requests
            BEGIN
                SELECT RAISE(ABORT, 'set-once field already written: {field}')
                WHERE OLD.{field} IS NOT NULL
                  AND OLD.{field} IS NOT NEW.{field};
            END
            """
        )


def _row_to_request(row: sqlite3.Row) -> Request:
    return Request(**{name: row[name] for name in _REQUEST_FIELDS})


def insert_request(conn: sqlite3.Connection, request: Request) -> None:
    """Insert a new row. The partial unique indexes (§6) enforce the
    idempotency-key and approval-code exclusivity — callers handle
    IntegrityError as §7.2 "return existing row" semantics.
    """
    placeholders = ",".join(["?"] * len(_REQUEST_FIELDS))
    columns = ",".join(_REQUEST_FIELDS)
    values = tuple(getattr(request, name) for name in _REQUEST_FIELDS)
    with conn:
        conn.execute(
            f"INSERT INTO requests ({columns}) VALUES ({placeholders})",
            values,
        )


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[Request]:
    row = conn.execute(
        "SELECT * FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return _row_to_request(row) if row else None


def get_by_approval_code(
    conn: sqlite3.Connection, code: str
) -> Optional[Request]:
    """Lookup a row by its 6-character code, restricted to active states
    (§6 partial unique index). Codes from terminal rows are not
    addressable by code (§15 note on audit/operational split)."""
    row = conn.execute(
        "SELECT * FROM requests WHERE approval_code = ? "
        "AND state IN ('pending_approval','approved')",
        (code,),
    ).fetchone()
    return _row_to_request(row) if row else None


def get_by_idempotency_key(
    conn: sqlite3.Connection, key: str
) -> Optional[Request]:
    """Lookup by idempotency key, restricted to the active set used by
    the §6 partial unique index. Terminal failures (denied, expired,
    failed, cancelled, integrity_failed) do not block re-requests."""
    row = conn.execute(
        "SELECT * FROM requests WHERE idempotency_key = ? "
        "AND state NOT IN ("
        "'denied','expired','failed','cancelled','integrity_failed'"
        ")",
        (key,),
    ).fetchone()
    return _row_to_request(row) if row else None


def _allowed_mutable_for_transition(
    from_state: str, to_state: str
) -> frozenset[str]:
    """The set of field names this transition may update.

    Base: always-mutable fields. Special cases:
      - pending_approval → approved unlocks the set-once fields.
      - any → reconciliation_needed / terminal states: base mutable only.
    """
    allowed = set(MUTABLE_FIELDS)
    if from_state == "pending_approval" and to_state == "approved":
        allowed.update(SET_ONCE_ON_APPROVAL)
    return frozenset(allowed)


def transition(
    conn: sqlite3.Connection,
    request_id: str,
    from_state: str,
    to_state: str,
    **mutable_fields: Any,
) -> None:
    """Apply `from_state` → `to_state` atomically. Raises InvalidTransition
    if the pair is not in §5's table or if the row's current state does
    not match `from_state` (optimistic lock). Raises ImmutableFieldViolation
    if `mutable_fields` includes a field not updatable on this transition.

    Caller must have verified HMAC before this call (§7.3).
    """
    if (from_state, to_state) not in VALID_TRANSITIONS:
        raise InvalidTransition(
            f"transition not permitted: {from_state!r} -> {to_state!r}"
        )

    allowed = _allowed_mutable_for_transition(from_state, to_state)
    # `state` is always in allowed — we always set it. Strip it if caller
    # passed it (redundant) before checking the rest.
    extra_state = mutable_fields.pop("state", None)
    if extra_state is not None and extra_state != to_state:
        raise ImmutableFieldViolation(
            f"mutable_fields['state']={extra_state!r} conflicts with "
            f"to_state={to_state!r}"
        )

    bad = set(mutable_fields) - allowed
    if bad:
        raise ImmutableFieldViolation(
            f"fields not updatable on {from_state}->{to_state}: {sorted(bad)}"
        )

    set_clauses = ["state = ?"]
    params: list[Any] = [to_state]
    for col in sorted(mutable_fields):  # sorted for deterministic SQL
        # col is validated against `allowed` (whitelist); safe to interpolate.
        set_clauses.append(f"{col} = ?")
        params.append(mutable_fields[col])
    params.extend([request_id, from_state])

    with conn:
        cursor = conn.execute(
            f"UPDATE requests SET {', '.join(set_clauses)} "
            "WHERE request_id = ? AND state = ?",
            params,
        )
    if cursor.rowcount == 0:
        # Row missing, or its current state ≠ from_state (race or bug).
        raise InvalidTransition(
            f"row {request_id!r} not in state {from_state!r} "
            f"(or does not exist)"
        )


def quarantine(
    conn: sqlite3.Connection,
    request_id: str,
    error_code: str,
    error_message: str,
) -> None:
    """Force a row into `integrity_failed`. Callable from any non-terminal
    state — used by the HMAC/params_hash mismatch handler (§7.3, §12.4).
    No-op when the row is already terminal."""
    with conn:
        conn.execute(
            "UPDATE requests SET state='integrity_failed', "
            "error_code = ?, error_message = ? "
            "WHERE request_id = ? AND state NOT IN ("
            "'succeeded','failed','cancelled','integrity_failed'"
            ")",
            (error_code, error_message, request_id),
        )


def count_pending(conn: sqlite3.Connection) -> int:
    """Count of rows in state='approved'. Used by §13.6 pending_summary."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE state = 'approved'"
    ).fetchone()
    return int(row["n"])


def daily_backup(
    conn: sqlite3.Connection, backup_dir: str
) -> Optional[str]:
    """Opportunistic per-UTC-day .backup snapshot at
    `backup_dir/requests-YYYY-MM-DD.db`. Returns the path on write,
    or None when today's snapshot already exists. Prunes backups older
    than 14 days (§7.5)."""
    date_str = time.strftime("%Y-%m-%d", time.gmtime())
    backup_path = Path(backup_dir) / f"requests-{date_str}.db"
    if backup_path.exists():
        return None
    Path(backup_dir).mkdir(parents=True, exist_ok=True)

    # sqlite3.Connection.backup writes a consistent snapshot using the
    # online backup API — safe even while readers are active.
    dest = sqlite3.connect(str(backup_path))
    try:
        conn.backup(dest)
    finally:
        dest.close()

    _prune_backups(backup_dir, keep=14)
    return str(backup_path)


def _prune_backups(backup_dir: str, keep: int) -> None:
    files = sorted(Path(backup_dir).glob("requests-*.db"))
    # Keep the most recent `keep` files; delete the rest.
    for f in files[:-keep] if len(files) > keep else []:
        f.unlink()
