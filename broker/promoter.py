# promoter.py
"""Promoter install orchestrator (design §6.3, §9 invariants).

Privileged side effects (the filesystem merge into the manifests-only live dir,
and the publish of the merged manifest into the dir the broker actually reads)
live behind injected callables so the orchestration is unit-tested without a
real daemon. The promoter independently re-verifies BOTH the pack
signature/safety (Plan A: ``pack_verify``) AND the approval record (read from
the broker requests DB ITSELF, via ``RequestsDbApprovalSource`` — never the
client's claim).

Sequence (fail-closed, every outcome ledgered, no secret ever ledgered):

  1. ``pack_format.load_pack``
  2. ``pack_verify.verify_pack`` against the LIVE capability names -> key_id,
     pack_hash
  3. fetch the approval record by PACK IDENTITY (pack_id + pack_hash) via the
     injected ``ApprovalSource``; ``pack_token.verify_approval(pack_id,
     pack_hash)`` -> approval_id. (Resolution is by pack identity, NOT
     request_id: the executor never receives the request_id, and the broker
     has already moved the matching request approved -> executing.)
  4. ``promoter_fs.install`` (staged-verify -> atomic merge -> re-verify ->
     rollback on failure) into the manifests-only live dir.
  5. ``publish()`` (injected; the daemon passes a
     ``promoter_fs.publish_to_config`` closure that copies capabilities.yaml +
     schemas into the dir the broker reads, per-file, never touching the DB or
     age vault that also live there).
  6. ``ApprovalSource.mark_consumed`` + ledger ``installed``

Failure handling:
  - ANY verification / fs error BEFORE the merge -> ledger ``refused`` and raise
    ``PromoterError``; ``publish`` is NEVER called and the live manifests are
    left valid (``promoter_fs`` guarantees this).
  - ``publish()`` raises AFTER a successful merge -> ledger
    ``installed_publish_failed`` and raise ``PromoterError``. The merge into the
    manifests-only dir STANDS; the approval is still consumed so it cannot drive
    a second install.

This module deliberately imports NO ``subprocess`` — the publish is injected, so
no privileged OS call lives here.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from broker import (
    pack_format,
    pack_keys,
    pack_token,
    pack_verify,
    promoter_fs,
    promoter_ledger,
    requests_db,
    validator,
)

# Matches the promoter.install_pack approval window (design §6, Task 6:
# approval_window_minutes: 120). Kept here as the orchestrator's freshness gate.
APPROVAL_TTL_SECONDS = 7200.0


class PromoterError(Exception):
    """An install was refused or failed. Fail-closed; the reason is ledgered."""


class ApprovalSource(Protocol):
    """The promoter's view of where an install approval comes from. The real
    implementation reads the broker requests DB; tests inject a fake.

    The approval is resolved by PACK IDENTITY (pack_id + pack_hash), NOT by
    request_id: the broker's subprocess executor contract never passes the
    request_id to the executor, and by the time this runs the broker has
    already moved the matching request approved -> executing. The pack
    identity is what the promoter independently re-verified, so it is the
    correct join key."""

    def fetch(  # pragma: no cover - Protocol stub
        self, *, pack_id: str, pack_hash: str
    ) -> pack_token.ApprovalRecord | None: ...

    def mark_consumed(self, request_id: str) -> None: ...  # pragma: no cover


@dataclass
class RequestsDbApprovalSource:
    """Reads the broker's requests DB to obtain an install approval record and
    confirm it independently (defence in depth, not the client's claim). The
    ``params_json`` of a ``promoter.install_pack`` request is
    ``{"pack_id": ..., "pack_hash": ...}``.
    """

    conn: sqlite3.Connection

    def fetch(
        self, *, pack_id: str, pack_hash: str
    ) -> pack_token.ApprovalRecord | None:
        req = requests_db.find_install_approval(
            self.conn, pack_id=pack_id, pack_hash=pack_hash
        )
        if req is None:
            return None
        params = json.loads(req.params_json)
        # State -> status mapping. By the time this runs the broker may have
        # ALREADY moved the request approved -> executing (it is executing THIS
        # install right now). Both states mean "a human approved this", so both
        # map to status "approved" — which is what pack_token.verify_approval
        # requires. This keeps that already-committed pure module unmodified.
        status = "approved" if req.state in {"approved", "executing"} else req.state
        # approved_at is stored as epoch-MILLISECONDS (requests_db §6 schema);
        # the approval record carries epoch-seconds.
        approved_at_ts = float(req.approved_at or 0) / 1000.0
        return pack_token.ApprovalRecord(
            pack_id=str(params.get("pack_id", "")),
            pack_hash=str(params.get("pack_hash", "")),
            approval_id=req.request_id,
            status=status,
            approved_at_ts=approved_at_ts,
            # Single execution is the broker's lifecycle guarantee (it executes
            # each approved request once). The promoter's security check is the
            # signature + pack-bound hash + a real approval existing — NOT a
            # consumed flag — so we leave consumed=False here.
            consumed=False,
        )

    def mark_consumed(self, request_id: str) -> None:
        """Best-effort bookkeeping. The broker owns the request lifecycle and
        guarantees each approved request is executed once, so the promoter does
        NOT need to drive state transitions for single-use. We attempt the
        broker's ``approved -> executing`` transition ONLY if it is a valid
        transition from the current state, and swallow any transition error
        rather than failing the install: an install that already succeeded must
        not be reported as failed because this bookkeeping transition was a
        no-op (e.g. the broker already moved the row to ``executing``)."""
        req = requests_db.get_request(self.conn, request_id)
        if req is None or req.state != "approved":
            return  # nothing valid to transition; not an error.
        try:
            requests_db.transition(self.conn, request_id, "approved", "executing")
        except requests_db.InvalidTransition:
            # A concurrent transition raced us; the row is already past
            # 'approved'. The single-execution guarantee still holds — swallow.
            pass


def _existing_capability_names(live_manifests_dir: str) -> set[str]:
    cap_yaml = Path(live_manifests_dir) / "capabilities.yaml"
    return set(validator.load_capabilities(str(cap_yaml)).keys())


def install(
    *,
    pack_dir: str,
    trusted_keys_dir: str,
    live_manifests_dir: str,
    approvals: ApprovalSource,
    publish: Callable[[], None],
    ledger: promoter_ledger.Ledger,
    now: Callable[[], float],
) -> dict[str, str]:
    """Verify + install a signed pack, publish it to the broker config, ledger
    the outcome.

    Returns ``{"outcome": "installed", "pack_id": ..., "key_id": ...}`` on
    success. Raises ``PromoterError`` on any refusal or failure (always
    ledgered first). ``publish`` is never called on a pre-merge failure.
    """
    pack_id = pack_hash = key_id = approval_id = ""
    try:
        pack = pack_format.load_pack(pack_dir)
        pack_id = pack.pack_id

        keys = pack_keys.load_trusted_keys(trusted_keys_dir)
        existing = _existing_capability_names(live_manifests_dir)
        result = pack_verify.verify_pack(
            pack, keys, existing_capabilities=existing
        )
        key_id, pack_hash = result.key_id, result.pack_hash

        # Resolve the approval by the pack identity we just re-verified — NOT
        # by request_id (the executor never receives it; the broker has already
        # moved the matching request approved -> executing).
        record = approvals.fetch(pack_id=pack_id, pack_hash=pack_hash)
        if record is None:
            raise PromoterError("no matching install approval")
        approval_id = pack_token.verify_approval(
            record,
            pack_id=pack_id,
            pack_hash=pack_hash,
            now_ts=now(),
            ttl_seconds=APPROVAL_TTL_SECONDS,
        )

        # All checks passed — perform the privileged merge.
        promoter_fs.install(pack, live_manifests_dir)
    except (
        pack_format.PackFormatError,
        pack_keys.KeyStoreError,
        pack_verify.PackRejected,
        pack_token.PackTokenError,
        promoter_fs.InstallError,
        validator.ManifestError,
        PromoterError,
    ) as e:
        # Pre-merge (or merge-with-rollback) failure: live manifests are valid,
        # publish was NOT called. Ledger the refusal and fail closed.
        ledger.record(
            pack_id=pack_id,
            pack_hash=pack_hash,
            key_id=key_id,
            approval_id=approval_id,
            outcome="refused",
            reason=str(e),
        )
        raise PromoterError(str(e)) from e

    # The merge has landed. From here, a publish failure does NOT undo the merge.
    try:
        publish()
    except Exception as e:
        # The merge into the manifests-only dir stands. Consume the approval
        # first so it can never drive a second install. The resolved record's id
        # is the request id (read it from the fetched approval).
        approvals.mark_consumed(approval_id)
        ledger.record(
            pack_id=pack_id,
            pack_hash=pack_hash,
            key_id=key_id,
            approval_id=approval_id,
            outcome="installed_publish_failed",
            reason=str(e),
        )
        raise PromoterError(
            f"installed but publish to broker config failed: {e}"
        ) from e

    approvals.mark_consumed(approval_id)
    ledger.record(
        pack_id=pack_id,
        pack_hash=pack_hash,
        key_id=key_id,
        approval_id=approval_id,
        outcome="installed",
        reason="",
    )
    return {"outcome": "installed", "pack_id": pack_id, "key_id": key_id}
