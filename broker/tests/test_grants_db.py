"""Tests for broker.grants_db.

Spec: broker-standing-grants §4 (grant model), §6 (active filtering),
§7 (revoke). Extends security-v1.1.

Coverage aims:
  - ensure_grant_tables is idempotent and does not touch `requests`.
  - insert/get/list round-trips a grant.
  - active_grants filters out revoked + expired and is deterministic in now.
  - revoke_grant sets revoked_at, returns True once / False thereafter.
  - record_use + count_uses_within for the rate window.
"""
from __future__ import annotations

import json

import pytest

from broker import grants_db
from broker import requests_db as db


NOW = 1_700_000_000_000


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(str(tmp_path / "requests.db"))
    grants_db.ensure_grant_tables(c)
    yield c
    c.close()


def _grant(
    *,
    grant_id="g-001",
    capability="gmail.send",
    constraints=None,
    expires_at=NOW + 1_000_000,
    revoked_at=None,
    max_per_period=1,
    period_seconds=604_800,
    created_at=NOW,
) -> grants_db.StandingGrant:
    if constraints is None:
        constraints = {"to": "graham@example.com"}
    return grants_db.StandingGrant(
        id=grant_id,
        capability=capability,
        constraints=json.dumps(constraints, sort_keys=True),
        constraints_mac="deadbeef",
        purpose="School roundup",
        max_per_period=max_per_period,
        period_seconds=period_seconds,
        created_at=created_at,
        expires_at=expires_at,
        approved_via="AB12CD",
        revoked_at=revoked_at,
    )


def test_ensure_grant_tables_idempotent(conn):
    grants_db.ensure_grant_tables(conn)
    grants_db.ensure_grant_tables(conn)  # twice — must not raise
    # Tables exist.
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "standing_grants" in names
    assert "grant_uses" in names
    # Did not disturb the requests table.
    assert "requests" in names


def test_insert_and_get_roundtrip(conn):
    g = _grant()
    grants_db.insert_grant(conn, g)
    got = grants_db.get_grant(conn, "g-001")
    assert got == g


def test_get_missing_returns_none(conn):
    assert grants_db.get_grant(conn, "nope") is None


def test_list_grants_newest_first(conn):
    grants_db.insert_grant(conn, _grant(grant_id="g-old", created_at=NOW))
    grants_db.insert_grant(conn, _grant(grant_id="g-new", created_at=NOW + 1000))
    listed = grants_db.list_grants(conn)
    assert [g.id for g in listed] == ["g-new", "g-old"]


def test_list_grants_includes_expired_and_revoked(conn):
    grants_db.insert_grant(conn, _grant(grant_id="g-exp", expires_at=NOW - 1))
    grants_db.insert_grant(conn, _grant(grant_id="g-rev", revoked_at=NOW - 1))
    ids = {g.id for g in grants_db.list_grants(conn)}
    assert ids == {"g-exp", "g-rev"}


def test_active_grants_excludes_expired(conn):
    grants_db.insert_grant(conn, _grant(grant_id="g-live", expires_at=NOW + 1))
    grants_db.insert_grant(conn, _grant(grant_id="g-exp", expires_at=NOW - 1))
    active = grants_db.active_grants(conn, "gmail.send", NOW)
    assert [g.id for g in active] == ["g-live"]


def test_active_grants_excludes_revoked(conn):
    grants_db.insert_grant(conn, _grant(grant_id="g-live"))
    grants_db.insert_grant(conn, _grant(grant_id="g-rev", revoked_at=NOW - 1))
    active = grants_db.active_grants(conn, "gmail.send", NOW)
    assert [g.id for g in active] == ["g-live"]


def test_active_grants_scoped_by_capability(conn):
    grants_db.insert_grant(conn, _grant(grant_id="g-send", capability="gmail.send"))
    grants_db.insert_grant(conn, _grant(grant_id="g-cal", capability="gcal.create_event"))
    active = grants_db.active_grants(conn, "gmail.send", NOW)
    assert [g.id for g in active] == ["g-send"]


def test_revoke_grant_sets_revoked_at(conn):
    grants_db.insert_grant(conn, _grant())
    assert grants_db.revoke_grant(conn, "g-001", NOW) is True
    got = grants_db.get_grant(conn, "g-001")
    assert got is not None and got.revoked_at == NOW


def test_revoke_grant_already_revoked_returns_false(conn):
    grants_db.insert_grant(conn, _grant(revoked_at=NOW - 5))
    assert grants_db.revoke_grant(conn, "g-001", NOW) is False


def test_revoke_grant_missing_returns_false(conn):
    assert grants_db.revoke_grant(conn, "nope", NOW) is False


def test_record_use_and_count_window(conn):
    grants_db.insert_grant(conn, _grant())
    grants_db.record_use(conn, "g-001", NOW)
    grants_db.record_use(conn, "g-001", NOW + 1000)
    assert grants_db.count_uses_within(conn, "g-001", 0) == 2
    # Window excludes earlier uses.
    assert grants_db.count_uses_within(conn, "g-001", NOW + 500) == 1
