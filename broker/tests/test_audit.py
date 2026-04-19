"""Tests for broker.audit.

Spec: security-v1.1 §7.6, §15.

Ralph target scope (see ralph-prompts/audit.md):
  - Append-only: file opened O_APPEND; no seeks.
  - Chain: every entry's prev_hash = sha256(previous canonical entry).
  - Rotation at 100MB or 30 days; sealed segment ends with
    segment_seal entry; next segment prev_hash == segment_end_hash.
  - verify_chain() returns None on clean; returns {file, line, reason}
    on first break. Tests must include a mutate-one-line scenario.
  - Writer never emits §15 forbidden fields (approval codes plaintext,
    params_json plaintext, credentials, stack traces, etc.).
"""
from __future__ import annotations

import pytest

from broker import audit


def test_module_importable():
    assert hasattr(audit, "write_event")
    assert hasattr(audit, "verify_chain")
    assert hasattr(audit, "rotate_if_needed")


# TODO(phase-1 ralph): full coverage per spec-ref list above.
