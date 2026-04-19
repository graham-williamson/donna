"""HMAC, idempotency, rate limits, cooldown, approval codes.

Spec: security-v1.1 §7.2 (idempotency key), §7.3 (HMAC), §7.4 (cooldown +
override), §13.2 (rate limits), §11 (replay semantics), §9.1 (policy-check
purity — IMPORT-LINTER enforces no-network here).

Invariant: **no network I/O anywhere in this module.** import-linter
bans `requests`, `httpx`, `urllib.*`, `http.client`, `aiohttp`.

Verification order at every state transition (§7.3):
  1. Recompute params_hash from canonicalize(params_json).
     Mismatch → audit.params_hash_mismatch → integrity_failed.
  2. Verify HMAC over the full current field set.
     Mismatch → audit.hmac_mismatch → integrity_failed.
  3. Only then apply the transition.

Phase 1 Ralph target — see `broker/ralph-prompts/policy.md`.
"""
from __future__ import annotations

from typing import Any, Optional


def idempotency_key(
    capability: str,
    canonical_params: bytes,
    date_component: str,
) -> str:
    """Per §7.2. sha256 of the three fields joined by \\x1f."""
    raise NotImplementedError("idempotency_key: Phase 1 Ralph target")


def generate_approval_code() -> str:
    """6 characters from RFC 4648 base32 minus I L O U. ~30 bits entropy.
    Uniqueness across pending_approval + approved enforced by SQL partial
    unique index (§7.3)."""
    raise NotImplementedError("generate_approval_code: Phase 1 Ralph target")


def compute_creation_hmac(
    key: bytes,
    request_id: str,
    capability: str,
    params_hash: str,
    idempotency_key_: str,
    risk_level: str,
    created_at: int,
    approval_expires_at: int,
) -> str:
    """Per §7.3. hmac_sha256 over the creation-time immutable fields,
    separated by \\x1f."""
    raise NotImplementedError("compute_creation_hmac: Phase 1 Ralph target")


def compute_approval_hmac(
    key: bytes,
    creation_msg: bytes,
    execution_expires_at: int,
    approved_at: int,
) -> str:
    """Per §7.3. Extends the creation message with the fields that become
    immutable at approval."""
    raise NotImplementedError("compute_approval_hmac: Phase 1 Ralph target")


def verify_hmac(key: bytes, message: bytes, expected_hex: str) -> bool:
    """Constant-time comparison of HMAC over `message` to `expected_hex`."""
    raise NotImplementedError("verify_hmac: Phase 1 Ralph target")


def rate_limit_check(
    conn: Any,
    capability: str,
    daily_cap: int,
    utc_date: str,
) -> bool:
    """Return True if under cap, False if exceeded. Caller increments on
    row creation, not on execution (§13.2). Denied/expired rows do NOT
    refund."""
    raise NotImplementedError("rate_limit_check: Phase 1 Ralph target")


def rate_limit_increment(conn: Any, capability: str, utc_date: str) -> None:
    raise NotImplementedError("rate_limit_increment: Phase 1 Ralph target")


def cooldown_remaining_seconds(
    conn: Any,
    idempotency_key_: str,
    cooldown_minutes: int,
) -> int:
    """For a denied row, return seconds remaining in the 30-minute
    cooldown window (default, capability-configurable). 0 when expired.
    Override path is the only way to resurrect during cooldown (§7.4)."""
    raise NotImplementedError("cooldown_remaining_seconds: Phase 1 Ralph target")


def sanitise_context_reason(raw: str) -> tuple[str, list[str]]:
    """Apply §7.7 ingest rules to Donna-provenance `context_reason`.
    Returns (sanitised, redaction_types)."""
    raise NotImplementedError("sanitise_context_reason: Phase 1 Ralph target")
