"""Tests for broker.requests_db.

Spec: security-v1.1 §6 (schema), §7.5 (durability), §5 (state machine).

Ralph target scope (see ralph-prompts/requests_db.md):
  - Schema creation idempotent; WAL mode confirmed via pragma.
  - Immutable triggers: attempt to UPDATE any immutable field raises;
    state=integrity_failed is allowed because that's a broker-owned
    transition, not an attacker edit — confirm via the specific path.
  - Unique indexes: idx_approval_code_active and idx_idempotency_active
    enforce the partial-uniqueness semantics from §6.
  - Insert + fetch round-trip for every Request field.
  - transition() honours the §5 table; invalid transitions raise.
  - count_pending() returns exact count of state='approved' rows.
  - daily_backup() is once-per-UTC-day (subsequent same-day calls no-op).
"""
from __future__ import annotations

import pytest

from broker import requests_db


def test_module_importable():
    assert hasattr(requests_db, "open_db")
    assert hasattr(requests_db, "insert_request")
    assert hasattr(requests_db, "transition")


# TODO(phase-1 ralph): full coverage per spec-ref list above.
