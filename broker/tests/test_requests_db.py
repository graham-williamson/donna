"""Tests for broker.requests_db.

Spec: security-v1.1 §6 (schema), §7.5 (durability), §5 (state machine),
§11 (replay semantics).

Coverage aims:
  - Schema creation is idempotent and WAL-mode active.
  - Immutable-at-creation triggers raise on any UPDATE.
  - Set-once-on-approval triggers allow NULL → value once, then raise.
  - Partial unique indexes enforce §6 exclusivity.
  - Every §5 valid transition applies; every invalid one raises.
  - Optimistic lock: transition fails when on-disk state ≠ from_state.
  - quarantine() forces integrity_failed from any non-terminal.
  - count_pending() is accurate.
  - daily_backup() is once-per-UTC-day; prunes past 14 days.
  - SQL injection safety: every public API uses parameterised queries.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from broker import requests_db as db


# ---- helpers -------------------------------------------------------------


def _make_request(
    *,
    request_id: str = "req-001",
    capability: str = "puregym.book_class",
    params_json: str = '{"class_id":"hiit"}',
    params_hash: str = "a" * 64,
    idempotency_key: str = "idem-001",
    resolved_summary: str = "Tuesday 6:30pm HIIT",
    context_reason: str | None = "Chief asked",
    risk_level: str = "medium",
    state: str = "pending_approval",
    approval_code: str | None = "A2B7KQ",
    approval_hmac: str | None = "f" * 64,
    created_at: int = 1_000_000,
    approval_expires_at: int = 2_000_000,
    execution_expires_at: int | None = None,
    approved_at: int | None = None,
    executed_at: int | None = None,
    result_json: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    prev_audit_hash: str | None = None,
) -> db.Request:
    return db.Request(
        request_id=request_id,
        capability=capability,
        params_json=params_json,
        params_hash=params_hash,
        idempotency_key=idempotency_key,
        resolved_summary=resolved_summary,
        context_reason=context_reason,
        risk_level=risk_level,
        state=state,
        approval_code=approval_code,
        approval_hmac=approval_hmac,
        created_at=created_at,
        approval_expires_at=approval_expires_at,
        execution_expires_at=execution_expires_at,
        approved_at=approved_at,
        executed_at=executed_at,
        result_json=result_json,
        error_code=error_code,
        error_message=error_message,
        prev_audit_hash=prev_audit_hash,
    )


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(str(tmp_path / "requests.db"))
    yield c
    c.close()


# ---- module surface + schema --------------------------------------------


def test_module_importable():
    assert hasattr(db, "open_db")
    assert hasattr(db, "insert_request")
    assert hasattr(db, "transition")
    assert hasattr(db, "quarantine")
    assert hasattr(db, "Request")


def test_open_db_creates_wal_mode(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_open_db_is_idempotent(tmp_path):
    path = str(tmp_path / "requests.db")
    c1 = db.open_db(path)
    c1.close()
    c2 = db.open_db(path)
    c2.close()  # second open must not raise


def test_schema_has_expected_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"requests", "rate_limits", "broker_state"}.issubset(tables)


def test_schema_has_expected_indexes(conn):
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_approval_code_active" in indexes
    assert "idx_idempotency_active" in indexes
    assert "idx_state" in indexes


def test_schema_has_all_triggers(conn):
    triggers = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    for field in db.IMMUTABLE_AT_CREATION:
        assert f"trg_immutable_{field}" in triggers
    for field in db.SET_ONCE_ON_APPROVAL:
        assert f"trg_set_once_{field}" in triggers


# ---- insert + fetch roundtrip -------------------------------------------


def test_insert_and_fetch_roundtrips_every_field(conn):
    r = _make_request()
    db.insert_request(conn, r)
    fetched = db.get_request(conn, r.request_id)
    assert fetched == r


def test_get_returns_none_for_missing(conn):
    assert db.get_request(conn, "does-not-exist") is None


def test_get_by_approval_code_honours_active_states(conn):
    r = _make_request(request_id="r1", idempotency_key="i1", state="pending_approval")
    db.insert_request(conn, r)
    assert db.get_by_approval_code(conn, "A2B7KQ") is not None

    # Move the row to 'denied' — should no longer be visible by code.
    db.transition(conn, "r1", "pending_approval", "denied")
    assert db.get_by_approval_code(conn, "A2B7KQ") is None


def test_get_by_idempotency_key_honours_active_states(conn):
    r = _make_request(request_id="r1", idempotency_key="key-x", state="pending_approval")
    db.insert_request(conn, r)
    assert db.get_by_idempotency_key(conn, "key-x") is not None

    db.transition(conn, "r1", "pending_approval", "cancelled")
    assert db.get_by_idempotency_key(conn, "key-x") is None


# ---- partial unique index enforcement -----------------------------------


def test_duplicate_approval_code_on_active_states_rejected(conn):
    r1 = _make_request(request_id="r1", idempotency_key="i1")
    r2 = _make_request(request_id="r2", idempotency_key="i2")
    db.insert_request(conn, r1)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_request(conn, r2)


def test_duplicate_idempotency_key_on_active_states_rejected(conn):
    r1 = _make_request(request_id="r1", approval_code="CODE01")
    r2 = _make_request(request_id="r2", approval_code="CODE02")
    db.insert_request(conn, r1)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_request(conn, r2)


def test_duplicate_idempotency_key_allowed_after_terminal(conn):
    """After the first row goes denied/expired/etc., the key is reusable."""
    r1 = _make_request(
        request_id="r1", idempotency_key="same", approval_code="C1"
    )
    db.insert_request(conn, r1)
    db.transition(conn, "r1", "pending_approval", "denied")
    # Now a second row with the same idempotency_key is allowed.
    r2 = _make_request(
        request_id="r2", idempotency_key="same", approval_code="C2"
    )
    db.insert_request(conn, r2)
    assert db.get_request(conn, "r2") is not None


# ---- immutable-at-creation triggers -------------------------------------


@pytest.mark.parametrize("field", list(db.IMMUTABLE_AT_CREATION))
def test_immutable_field_update_raises(conn, field):
    r = _make_request(request_id="r-imm", idempotency_key="i-imm", approval_code="C-imm")
    db.insert_request(conn, r)
    new_value = 99 if isinstance(getattr(r, field), int) else "different"
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            f"UPDATE requests SET {field} = ? WHERE request_id = ?",
            (new_value, r.request_id),
        )
    assert "immutable field" in str(exc.value)


def test_immutable_trigger_allows_no_op_update(conn):
    """Setting a field to its existing value must not trip the trigger."""
    r = _make_request(request_id="r-noop", idempotency_key="i-noop", approval_code="C-noop")
    db.insert_request(conn, r)
    conn.execute(
        "UPDATE requests SET capability = ? WHERE request_id = ?",
        (r.capability, r.request_id),
    )


# ---- set-once-on-approval triggers --------------------------------------


@pytest.mark.parametrize("field", list(db.SET_ONCE_ON_APPROVAL))
def test_set_once_field_first_write_allowed(conn, field):
    r = _make_request(
        request_id="r-so", idempotency_key="i-so", approval_code="C-so",
        execution_expires_at=None,
        approved_at=None,
        approval_hmac=None,
    )
    db.insert_request(conn, r)
    value = 12345 if field in {"execution_expires_at", "approved_at"} else "aa" * 32
    conn.execute(
        f"UPDATE requests SET {field} = ? WHERE request_id = ?",
        (value, r.request_id),
    )
    conn.commit()


@pytest.mark.parametrize("field", list(db.SET_ONCE_ON_APPROVAL))
def test_set_once_field_second_write_raises(conn, field):
    r = _make_request(
        request_id="r-so2", idempotency_key="i-so2", approval_code="C-so2",
        execution_expires_at=None,
        approved_at=None,
        approval_hmac=None,
    )
    db.insert_request(conn, r)
    first = 111 if field in {"execution_expires_at", "approved_at"} else "aa" * 32
    second = 222 if field in {"execution_expires_at", "approved_at"} else "bb" * 32
    conn.execute(
        f"UPDATE requests SET {field} = ? WHERE request_id = ?",
        (first, r.request_id),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            f"UPDATE requests SET {field} = ? WHERE request_id = ?",
            (second, r.request_id),
        )
    assert "set-once" in str(exc.value)


# ---- §5 transition table -------------------------------------------------


def test_transition_valid_pair_updates_state(conn):
    r = _make_request(request_id="r-t1", idempotency_key="i-t1", approval_code="C-t1")
    db.insert_request(conn, r)
    db.transition(conn, "r-t1", "pending_approval", "denied")
    fetched = db.get_request(conn, "r-t1")
    assert fetched is not None
    assert fetched.state == "denied"


def test_transition_invalid_pair_raises(conn):
    r = _make_request(request_id="r-t2", idempotency_key="i-t2", approval_code="C-t2")
    db.insert_request(conn, r)
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, "r-t2", "pending_approval", "succeeded")


def test_transition_wrong_from_state_raises(conn):
    """Optimistic lock: the row's actual state must match from_state."""
    r = _make_request(request_id="r-t3", idempotency_key="i-t3", approval_code="C-t3")
    db.insert_request(conn, r)
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, "r-t3", "approved", "executing")


def test_transition_missing_row_raises(conn):
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, "ghost", "pending_approval", "denied")


def test_transition_approval_unlocks_set_once_fields(conn):
    r = _make_request(
        request_id="r-t4", idempotency_key="i-t4", approval_code="C-t4",
        execution_expires_at=None,
        approved_at=None,
        approval_hmac=None,
    )
    db.insert_request(conn, r)
    db.transition(
        conn,
        "r-t4",
        "pending_approval",
        "approved",
        execution_expires_at=5_000_000,
        approved_at=4_500_000,
        approval_hmac="c" * 64,
    )
    fetched = db.get_request(conn, "r-t4")
    assert fetched is not None
    assert fetched.state == "approved"
    assert fetched.execution_expires_at == 5_000_000
    assert fetched.approved_at == 4_500_000
    assert fetched.approval_hmac == "c" * 64


def test_transition_rejects_set_once_on_non_approval_transition(conn):
    r = _make_request(request_id="r-t5", idempotency_key="i-t5", approval_code="C-t5")
    db.insert_request(conn, r)
    with pytest.raises(db.ImmutableFieldViolation):
        db.transition(
            conn,
            "r-t5",
            "pending_approval",
            "denied",
            execution_expires_at=999,
        )


def test_transition_rejects_immutable_field_passthrough(conn):
    r = _make_request(request_id="r-t6", idempotency_key="i-t6", approval_code="C-t6")
    db.insert_request(conn, r)
    with pytest.raises(db.ImmutableFieldViolation):
        db.transition(
            conn,
            "r-t6",
            "pending_approval",
            "denied",
            params_json="{}",
        )


def test_transition_state_field_conflict_raises(conn):
    r = _make_request(request_id="r-t7", idempotency_key="i-t7", approval_code="C-t7")
    db.insert_request(conn, r)
    with pytest.raises(db.ImmutableFieldViolation):
        db.transition(
            conn, "r-t7", "pending_approval", "denied", state="other"
        )


def test_transition_state_field_passthrough_matches_ok(conn):
    """If caller redundantly passes state=to_state, that's fine."""
    r = _make_request(request_id="r-t8", idempotency_key="i-t8", approval_code="C-t8")
    db.insert_request(conn, r)
    db.transition(
        conn, "r-t8", "pending_approval", "denied", state="denied"
    )
    fetched = db.get_request(conn, "r-t8")
    assert fetched is not None
    assert fetched.state == "denied"


def test_transition_every_valid_pair_in_table(conn):
    """Every pair in VALID_TRANSITIONS is applied against a fresh row.
    No exhaustive reachability traversal — just a typo check."""
    for from_state, to_state in db.VALID_TRANSITIONS:
        rid = f"t-{from_state}-{to_state}"[:60]
        r = _make_request(
            request_id=rid,
            idempotency_key=f"ik-{from_state}-{to_state}"[:60],
            approval_code=None,
            state=from_state,
        )
        db.insert_request(conn, r)
        db.transition(conn, rid, from_state, to_state)
        fetched = db.get_request(conn, rid)
        assert fetched is not None
        assert fetched.state == to_state


def test_transition_updates_mutable_fields(conn):
    r = _make_request(request_id="r-mt", idempotency_key="i-mt", approval_code="C-mt")
    db.insert_request(conn, r)
    db.transition(
        conn, "r-mt", "pending_approval", "denied",
        error_code="user_denied",
        error_message="Graham said no",
    )
    fetched = db.get_request(conn, "r-mt")
    assert fetched is not None
    assert fetched.error_code == "user_denied"
    assert fetched.error_message == "Graham said no"


# ---- quarantine ----------------------------------------------------------


def test_quarantine_from_pending(conn):
    r = _make_request(request_id="q1", idempotency_key="q-i1", approval_code="Q-C1")
    db.insert_request(conn, r)
    db.quarantine(conn, "q1", "hmac_mismatch", "detail")
    fetched = db.get_request(conn, "q1")
    assert fetched is not None
    assert fetched.state == "integrity_failed"
    assert fetched.error_code == "hmac_mismatch"


def test_quarantine_noop_on_terminal(conn):
    r = _make_request(
        request_id="q2", idempotency_key="q-i2", approval_code="Q-C2"
    )
    db.insert_request(conn, r)
    db.transition(conn, "q2", "pending_approval", "cancelled")
    db.quarantine(conn, "q2", "late", "ignored")
    fetched = db.get_request(conn, "q2")
    assert fetched is not None
    assert fetched.state == "cancelled"


def test_quarantine_on_executing_row(conn):
    """Integrity failure during execute: row is yanked to integrity_failed."""
    r = _make_request(
        request_id="q3", idempotency_key="q-i3", approval_code="Q-C3",
        execution_expires_at=None, approved_at=None, approval_hmac=None,
    )
    db.insert_request(conn, r)
    db.transition(
        conn, "q3", "pending_approval", "approved",
        execution_expires_at=5_000_000,
        approved_at=4_500_000,
        approval_hmac="c" * 64,
    )
    db.transition(conn, "q3", "approved", "executing")
    db.quarantine(conn, "q3", "params_hash_mismatch", "tampered")
    fetched = db.get_request(conn, "q3")
    assert fetched is not None
    assert fetched.state == "integrity_failed"


# ---- count_pending -------------------------------------------------------


def test_count_pending_zero_on_empty(conn):
    assert db.count_pending(conn) == 0


def test_count_pending_counts_only_approved(conn):
    r1 = _make_request(
        request_id="c1", idempotency_key="c-i1", approval_code="C-C1",
        execution_expires_at=None, approved_at=None, approval_hmac=None,
    )
    r2 = _make_request(
        request_id="c2", idempotency_key="c-i2", approval_code="C-C2",
    )
    db.insert_request(conn, r1)
    db.insert_request(conn, r2)
    db.transition(
        conn, "c1", "pending_approval", "approved",
        execution_expires_at=5_000_000,
        approved_at=4_500_000,
        approval_hmac="c" * 64,
    )
    assert db.count_pending(conn) == 1


# ---- daily_backup --------------------------------------------------------


def test_daily_backup_writes_snapshot(conn, tmp_path):
    backup_dir = tmp_path / "backups"
    out = db.daily_backup(conn, str(backup_dir))
    assert out is not None
    assert Path(out).exists()
    assert Path(out).name.startswith("requests-")
    assert Path(out).name.endswith(".db")


def test_daily_backup_second_same_day_returns_none(conn, tmp_path):
    backup_dir = tmp_path / "backups"
    first = db.daily_backup(conn, str(backup_dir))
    second = db.daily_backup(conn, str(backup_dir))
    assert first is not None
    assert second is None


def test_daily_backup_prunes_old(conn, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(20):
        (backup_dir / f"requests-2025-{i % 12 + 1:02d}-{i % 28 + 1:02d}.db").touch()
    db.daily_backup(conn, str(backup_dir))
    remaining = sorted(backup_dir.glob("requests-*.db"))
    assert len(remaining) == 14


# ---- sql injection safety -----------------------------------------------


def test_get_request_resists_sql_injection(conn):
    r = _make_request(request_id="safe", idempotency_key="safe-i", approval_code="SAFE01")
    db.insert_request(conn, r)
    malicious = "safe' OR 1=1 --"
    result = db.get_request(conn, malicious)
    assert result is None
    # Sanity: the legitimate lookup still works.
    assert db.get_request(conn, "safe") is not None


def test_transition_resists_sql_injection_on_request_id(conn):
    r = _make_request(request_id="inj", idempotency_key="inj-i", approval_code="INJ01")
    db.insert_request(conn, r)
    with pytest.raises(db.InvalidTransition):
        db.transition(
            conn, "inj'; DROP TABLE requests; --", "pending_approval", "denied"
        )
    # Table still there.
    assert db.get_request(conn, "inj") is not None
