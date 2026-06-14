# pack_token.py
"""Pack-bound install-approval verifier (promoter design §7, §9 invariant 2).

The promoter confirms a fresh Telegram approval authorised installing THIS
exact pack version by reading the broker's requests DB ITSELF and passing the
record here. Pure decision: approved + bound to {pack_id, pack_hash} + fresh +
not-yet-consumed ⇒ returns approval_id; anything else ⇒ PackTokenError.
Fail-closed. The daemon (Plan B) marks the record consumed only after a
successful install, so a crash mid-install cannot silently burn an approval.
"""
from __future__ import annotations

from dataclasses import dataclass


class PackTokenError(ValueError):
    """The approval record does not authorise installing this pack version.
    Fail-closed — the install is refused."""


@dataclass(frozen=True)
class ApprovalRecord:
    pack_id: str
    pack_hash: str
    approval_id: str
    status: str
    approved_at_ts: float
    consumed: bool


def verify_approval(
    record: ApprovalRecord,
    *,
    pack_id: str,
    pack_hash: str,
    now_ts: float,
    ttl_seconds: float,
) -> str:
    if record.status != "approved":
        raise PackTokenError(f"install request not approved (status={record.status!r})")
    if record.consumed:
        raise PackTokenError("install approval already consumed")
    if record.pack_id != pack_id or record.pack_hash != pack_hash:
        raise PackTokenError("approval does not match this pack version")
    if now_ts - record.approved_at_ts > ttl_seconds:
        raise PackTokenError("install approval expired")
    return record.approval_id
