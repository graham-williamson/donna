"""SQLite persistence for standing grants (scoped approve-once autonomy).

Spec: broker-standing-grants §4 (grant model), §5 (constraint semantics),
§6 (policy integration), §7 (lifecycle). Extends security-v1.1.

A *standing grant* is a broker-owned policy object: a specific
(capability + pinned params) action that may auto-execute (skip the
per-run approval) up to a rate limit, until it expires or is revoked.

Responsibilities (mirrors requests_db.py conventions):
  - Create the `standing_grants` + `grant_uses` tables if absent. Never
    alters existing tables/rows — migrations are create-if-absent only.
  - Narrow API: insert a grant, look it up, list active/all, revoke,
    record a use, count uses within a rolling window.

Does NOT:
  - Verify HMAC / compute the constraints MAC — that is policy.py's job
    (purity boundary; the MAC is computed/verified at the call site with
    the broker key).
  - Make network calls — local-only, ever (import-linter enforces this).

The grant tables live in the SAME SQLite file as `requests` so a single
connection serves both. `ensure_grant_tables()` is idempotent and is
invoked lazily by callers that need the grant store.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from typing import Optional


# §4 schema. Create-if-absent only — never ALTER an existing table.
GRANT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS standing_grants (
  id              TEXT PRIMARY KEY,
  capability      TEXT NOT NULL,
  constraints     TEXT NOT NULL,
  constraints_mac TEXT NOT NULL,
  purpose         TEXT NOT NULL,
  max_per_period  INTEGER NOT NULL,
  period_seconds  INTEGER NOT NULL,
  created_at      INTEGER NOT NULL,
  expires_at      INTEGER NOT NULL,
  approved_via    TEXT NOT NULL,
  revoked_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_grant_capability
  ON standing_grants(capability);

CREATE TABLE IF NOT EXISTS grant_uses (
  grant_id TEXT NOT NULL,
  used_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grant_uses_grant
  ON grant_uses(grant_id, used_at);
"""


@dataclass(frozen=True)
class StandingGrant:
    """In-memory projection of a standing_grants row. Frozen — a grant's
    scope is immutable once persisted; only `revoked_at` changes (and
    that goes through revoke_grant, not field mutation)."""
    id: str
    capability: str
    constraints: str          # canonical JSON of pinned params (§5)
    constraints_mac: str      # HMAC(broker_key, capability ‖ canonical) (§5)
    purpose: str
    max_per_period: int
    period_seconds: int
    created_at: int           # epoch-ms
    expires_at: int           # epoch-ms
    approved_via: str         # the approval_code that authorised this grant
    revoked_at: Optional[int]  # NULL = active


_GRANT_FIELDS = tuple(f.name for f in fields(StandingGrant))


def ensure_grant_tables(conn: sqlite3.Connection) -> None:
    """Create the grant tables if absent. Idempotent; safe to call on a
    DB that already has them. Never alters `requests` or any existing
    rows."""
    with conn:
        conn.executescript(GRANT_SCHEMA_SQL)


def _row_to_grant(row: sqlite3.Row) -> StandingGrant:
    return StandingGrant(**{name: row[name] for name in _GRANT_FIELDS})


def insert_grant(conn: sqlite3.Connection, grant: StandingGrant) -> None:
    """Insert a fully-formed grant row. Caller owns id generation, the
    constraints MAC, and timestamps (so the function stays deterministic
    and testable)."""
    placeholders = ",".join(["?"] * len(_GRANT_FIELDS))
    columns = ",".join(_GRANT_FIELDS)
    values = tuple(getattr(grant, name) for name in _GRANT_FIELDS)
    with conn:
        conn.execute(
            f"INSERT INTO standing_grants ({columns}) VALUES ({placeholders})",
            values,
        )


def get_grant(conn: sqlite3.Connection, grant_id: str) -> Optional[StandingGrant]:
    row = conn.execute(
        "SELECT * FROM standing_grants WHERE id = ?", (grant_id,)
    ).fetchone()
    return _row_to_grant(row) if row else None


def list_grants(conn: sqlite3.Connection) -> list[StandingGrant]:
    """All grants (active, expired, revoked) newest-first. The §7
    `grant-list` mode surfaces these to the app."""
    rows = conn.execute(
        "SELECT * FROM standing_grants ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_grant(r) for r in rows]


def active_grants(
    conn: sqlite3.Connection, capability: str, now_ms: int
) -> list[StandingGrant]:
    """Grants for `capability` that are neither revoked nor expired at
    `now_ms`. Ordered by created_at ASC for deterministic matching.

    `now_ms` is supplied by the caller (no wall-clock here) so the
    policy hot-path stays pure/deterministic (§6)."""
    rows = conn.execute(
        "SELECT * FROM standing_grants "
        "WHERE capability = ? "
        "AND revoked_at IS NULL "
        "AND expires_at > ? "
        "ORDER BY created_at ASC",
        (capability, now_ms),
    ).fetchall()
    return [_row_to_grant(r) for r in rows]


def revoke_grant(
    conn: sqlite3.Connection, grant_id: str, now_ms: int
) -> bool:
    """Set revoked_at on an active grant. Returns True if a row was
    revoked, False if the grant doesn't exist or was already revoked.
    Revocation only ever reduces privilege (§3.6) — never fails for a
    policy reason."""
    with conn:
        cur = conn.execute(
            "UPDATE standing_grants SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (now_ms, grant_id),
        )
    return cur.rowcount > 0


def record_use(conn: sqlite3.Connection, grant_id: str, now_ms: int) -> None:
    """Append a usage row for rate accounting (§3.3)."""
    with conn:
        conn.execute(
            "INSERT INTO grant_uses (grant_id, used_at) VALUES (?, ?)",
            (grant_id, now_ms),
        )


def count_uses_within(
    conn: sqlite3.Connection, grant_id: str, window_start_ms: int
) -> int:
    """Count uses of `grant_id` at-or-after `window_start_ms`. The caller
    computes the window from `now - period_seconds` so the rolling-window
    rate limit (§3.3) stays deterministic given a fixed `now`."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM grant_uses "
        "WHERE grant_id = ? AND used_at >= ?",
        (grant_id, window_start_ms),
    ).fetchone()
    return int(row["n"])
