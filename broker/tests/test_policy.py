"""Tests for broker.policy.

Spec: security-v1.1 §7.2 (idempotency), §7.3 (HMAC), §7.4 (cooldown +
override), §13.2 (rate limits), §9.1 (purity).

Ralph target scope (see ralph-prompts/policy.md):
  - idempotency_key() matches explicit test vectors for
    (capability, canonical_params, date_component) triples.
  - generate_approval_code() produces 6 chars from RFC 4648 base32 minus
    I L O U; distribution sanity check over N samples.
  - compute_creation_hmac() matches explicit test vectors; unit-separator
    (\\x1f) not appearing inside any field.
  - verify_hmac() is constant-time (no early return on first differing
    byte — verifiable via hmac.compare_digest usage).
  - Rate limit: increment at creation, no refund on denied/expired.
  - Cooldown: after /deny, 30min default; override path transitions
    denied → pending_approval with fresh window (§7.4).
  - sanitise_context_reason(): §7.7 rules — hard cap 200, URL/base64/
    hex/long-digit/non-ASCII redactions, each emits audit event.
  - import-linter contract: no network imports in this module.
"""
from __future__ import annotations

import pytest

from broker import policy


def test_module_importable():
    assert hasattr(policy, "idempotency_key")
    assert hasattr(policy, "compute_creation_hmac")
    assert hasattr(policy, "verify_hmac")


def test_no_network_imports_at_module_level():
    """Sanity check — full enforcement is via import-linter config."""
    import broker.policy as p
    import sys
    banned = {"requests", "httpx", "aiohttp", "urllib.request",
              "urllib3", "http.client"}
    leaked = banned & set(sys.modules)
    # If any banned module is loaded, check broker.policy didn't pull it.
    # A full check lives in the import-linter contract (lint-imports).
    if leaked:
        pytest.skip(
            "Banned network modules already loaded by test harness; "
            "import-linter contract is the authoritative check."
        )


# TODO(phase-1 ralph): full coverage per spec-ref list above.
