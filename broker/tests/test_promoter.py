"""Tests for the promoter install orchestrator (design §6.3, §9 invariants).

The orchestrator ties together pack-verify (Plan A), approval-verify (read from
the broker requests DB), the filesystem merge (promoter_fs), and the append-only
ledger. Privileged effects (restart, the DB) are injected so the orchestration
is unit-tested without a real daemon, launchctl, or live broker DB.

Security contract proven here (fail-closed, ledger EVERY outcome):
  - happy path -> "installed", ledger row, restart called once, mark_consumed;
  - unsigned/bad pack -> PromoterError + ledger "refused" + restart NOT called
    + live manifests byte-for-byte unchanged;
  - no approval record -> refused;
  - approval bound to a DIFFERENT pack_hash -> refused;
  - restart raises AFTER a successful merge -> "installed_restart_failed"
    ledgered + PromoterError (the merge stands).

Plus a requests_db.get_request round-trip (insert a request, read it back;
missing id -> None) and the real RequestsDbApprovalSource state->status map.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from broker import (
    pack_format,
    pack_keys,
    pack_token,
    promoter,
    promoter_ledger,
    requests_db,
)


# ---- fixtures: a live manifests dir + a signed pack on disk --------------


def _live(tmp_path: Path) -> Path:
    """A valid live manifests dir with one existing capability."""
    d = tmp_path / "manifests"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    (d / "capabilities.yaml").write_text(
        yaml.safe_dump(
            {
                "capabilities": [
                    {
                        "name": "gmail.send",
                        "executor": {"type": "mcp_tool", "tool": "mcp__x__y"},
                        "param_schema": {"$ref": "./schemas/gmail.json"},
                        "params_exact_match_required": True,
                        "derived_fields_allowed": [],
                        "risk_level": "high",
                        "revalidate": {"not_applicable": "stateless_write"},
                        "idempotency_date_from": "created_utc",
                        "approval_window_minutes": 60,
                        "execution_window_minutes": 60,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (d / "schemas" / "gmail.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    return d


def _write_pack(
    tmp_path: Path,
    *,
    priv: Ed25519PrivateKey,
    sign: bool = True,
    name: str = "site.read",
    pack_id: str = "site",
) -> Path:
    """A valid one-capability pack dir, signed with ``priv`` unless ``sign``."""
    d = tmp_path / "pack"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    (d / "meta.json").write_text(
        json.dumps(
            {
                "pack_id": pack_id,
                "version": 1,
                "created_utc": "2026-06-15T00:00:00Z",
                "description": "d",
                "capabilities": [name],
            }
        ),
        encoding="utf-8",
    )
    (d / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "capabilities": [
                    {
                        "name": name,
                        "executor": {"type": "mcp_tool", "tool": "mcp__a__b"},
                        "param_schema": {"$ref": "./schemas/site.json"},
                        "params_exact_match_required": True,
                        "derived_fields_allowed": [],
                        "risk_level": "low",
                        "revalidate": {"not_applicable": "stateless_write"},
                        "idempotency_date_from": "created_utc",
                        "approval_window_minutes": 60,
                        "execution_window_minutes": 60,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (d / "schemas" / "site.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    if sign:
        pack = pack_format.load_pack(str(d))
        (d / "pack.sig").write_bytes(priv.sign(pack_format.canonical_bytes(pack)))
    return d


def _keys_dir(tmp_path: Path, priv: Ed25519PrivateKey, key_id: str = "k1") -> Path:
    d = tmp_path / "keys"
    d.mkdir()
    (d / f"{key_id}.ed25519.pub").write_text(
        priv.public_key().public_bytes_raw().hex(), encoding="utf-8"
    )
    return d


# ---- fake injected effects ----------------------------------------------


class FakeApprovalSource:
    """An in-memory ApprovalSource keyed by PACK IDENTITY: hands back a
    pre-built record when (pack_id, pack_hash) match the record it holds, and
    records whether mark_consumed was called and with which id."""

    def __init__(self, record: pack_token.ApprovalRecord | None) -> None:
        self._record = record
        self.consumed: list[str] = []
        self.fetched: list[tuple[str, str]] = []

    def fetch(
        self, *, pack_id: str, pack_hash: str
    ) -> pack_token.ApprovalRecord | None:
        self.fetched.append((pack_id, pack_hash))
        if self._record is None:
            return None
        if (
            self._record.pack_id == pack_id
            and self._record.pack_hash == pack_hash
        ):
            return self._record
        return None

    def mark_consumed(self, request_id: str) -> None:
        self.consumed.append(request_id)


class FakeRestart:
    """A fake restart callable that counts calls and can be made to raise."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self._raises = raises

    def __call__(self) -> None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("launchctl kickstart failed")


def _approval(
    *,
    pack_id: str,
    pack_hash: str,
    status: str = "approved",
    approved_at_ts: float = 1000.0,
    consumed: bool = False,
) -> pack_token.ApprovalRecord:
    return pack_token.ApprovalRecord(
        pack_id=pack_id,
        pack_hash=pack_hash,
        approval_id="req-1",
        status=status,
        approved_at_ts=approved_at_ts,
        consumed=consumed,
    )


def _pack_hash_of(pack_dir: Path) -> str:
    return pack_format.pack_hash(pack_format.load_pack(str(pack_dir)))


def _ledger(tmp_path: Path) -> promoter_ledger.Ledger:
    return promoter_ledger.Ledger(str(tmp_path / "ledger.jsonl"), now=lambda: 1000.0)


# ---- happy path ----------------------------------------------------------


def test_install_happy_path(tmp_path: Path) -> None:
    priv = Ed25519PrivateKey.generate()
    live = _live(tmp_path)
    pack_dir = _write_pack(tmp_path, priv=priv)
    keys = _keys_dir(tmp_path, priv)
    approvals = FakeApprovalSource(
        _approval(pack_id="site", pack_hash=_pack_hash_of(pack_dir))
    )
    restart = FakeRestart()
    ledger = _ledger(tmp_path)

    result = promoter.install(
        pack_dir=str(pack_dir),
        trusted_keys_dir=str(keys),
        live_manifests_dir=str(live),
        approvals=approvals,
        restart=restart,
        ledger=ledger,
        now=lambda: 1500.0,
    )

    assert result["outcome"] == "installed"
    assert result["pack_id"] == "site"
    assert restart.calls == 1
    assert approvals.consumed == ["req-1"]

    rows = promoter_ledger.read_all(str(tmp_path / "ledger.jsonl"))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "installed"
    assert rows[0]["pack_id"] == "site"
    assert rows[0]["approval_id"] == "req-1"

    # The pack was actually merged into the live manifests.
    merged = yaml.safe_load((live / "capabilities.yaml").read_text())
    names = {c["name"] for c in merged["capabilities"]}
    assert names == {"gmail.send", "site.read"}


# ---- unsigned / bad pack -> refused, restart NOT called, live unchanged --


def test_install_unsigned_pack_refused(tmp_path: Path) -> None:
    priv = Ed25519PrivateKey.generate()
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack_dir = _write_pack(tmp_path, priv=priv, sign=False)  # no pack.sig
    keys = _keys_dir(tmp_path, priv)
    approvals = FakeApprovalSource(
        _approval(pack_id="site", pack_hash="deadbeef")
    )
    restart = FakeRestart()
    ledger = _ledger(tmp_path)

    with pytest.raises(promoter.PromoterError):
        promoter.install(
            pack_dir=str(pack_dir),
            trusted_keys_dir=str(keys),
            live_manifests_dir=str(live),
            approvals=approvals,
            restart=restart,
            ledger=ledger,
            now=lambda: 1500.0,
        )

    assert restart.calls == 0
    assert approvals.consumed == []
    rows = promoter_ledger.read_all(str(tmp_path / "ledger.jsonl"))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    # live manifests byte-for-byte unchanged.
    assert (live / "capabilities.yaml").read_text() == before


# ---- no approval record -> refused --------------------------------------


def test_install_no_approval_refused(tmp_path: Path) -> None:
    priv = Ed25519PrivateKey.generate()
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack_dir = _write_pack(tmp_path, priv=priv)
    keys = _keys_dir(tmp_path, priv)
    approvals = FakeApprovalSource(None)  # no matching install approval
    restart = FakeRestart()
    ledger = _ledger(tmp_path)

    with pytest.raises(promoter.PromoterError):
        promoter.install(
            pack_dir=str(pack_dir),
            trusted_keys_dir=str(keys),
            live_manifests_dir=str(live),
            approvals=approvals,
            restart=restart,
            ledger=ledger,
            now=lambda: 1500.0,
        )

    assert restart.calls == 0
    rows = promoter_ledger.read_all(str(tmp_path / "ledger.jsonl"))
    assert rows[0]["outcome"] == "refused"
    assert (live / "capabilities.yaml").read_text() == before


# ---- approval bound to a DIFFERENT pack_hash -> refused ------------------


def test_install_wrong_pack_hash_refused(tmp_path: Path) -> None:
    priv = Ed25519PrivateKey.generate()
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack_dir = _write_pack(tmp_path, priv=priv)
    keys = _keys_dir(tmp_path, priv)
    # The approval is for the right pack_id but a stale/wrong hash.
    approvals = FakeApprovalSource(
        _approval(pack_id="site", pack_hash="0" * 64)
    )
    restart = FakeRestart()
    ledger = _ledger(tmp_path)

    with pytest.raises(promoter.PromoterError):
        promoter.install(
            pack_dir=str(pack_dir),
            trusted_keys_dir=str(keys),
            live_manifests_dir=str(live),
            approvals=approvals,
            restart=restart,
            ledger=ledger,
            now=lambda: 1500.0,
        )

    assert restart.calls == 0
    rows = promoter_ledger.read_all(str(tmp_path / "ledger.jsonl"))
    assert rows[0]["outcome"] == "refused"
    assert (live / "capabilities.yaml").read_text() == before


# ---- restart raises AFTER merge -> installed_restart_failed --------------


def test_install_restart_failure_is_installed_restart_failed(tmp_path: Path) -> None:
    priv = Ed25519PrivateKey.generate()
    live = _live(tmp_path)
    pack_dir = _write_pack(tmp_path, priv=priv)
    keys = _keys_dir(tmp_path, priv)
    approvals = FakeApprovalSource(
        _approval(pack_id="site", pack_hash=_pack_hash_of(pack_dir))
    )
    restart = FakeRestart(raises=True)
    ledger = _ledger(tmp_path)

    with pytest.raises(promoter.PromoterError):
        promoter.install(
            pack_dir=str(pack_dir),
            trusted_keys_dir=str(keys),
            live_manifests_dir=str(live),
            approvals=approvals,
            restart=restart,
            ledger=ledger,
            now=lambda: 1500.0,
        )

    assert restart.calls == 1
    # The merge stands — the consume still happens so the approval can't be reused.
    assert approvals.consumed == ["req-1"]
    rows = promoter_ledger.read_all(str(tmp_path / "ledger.jsonl"))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "installed_restart_failed"
    # The merge is real — live now contains the new capability.
    merged = yaml.safe_load((live / "capabilities.yaml").read_text())
    names = {c["name"] for c in merged["capabilities"]}
    assert names == {"gmail.send", "site.read"}


# ---- requests_db.get_request round-trip ---------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    c = requests_db.open_db(str(tmp_path / "requests.db"))
    yield c
    c.close()


def _install_request(
    *, request_id: str, state: str, pack_hash: str, approved_at: int | None
) -> requests_db.Request:
    params = json.dumps({"pack_id": "site", "pack_hash": pack_hash})
    return requests_db.Request(
        request_id=request_id,
        capability="promoter.install_pack",
        params_json=params,
        params_hash="a" * 64,
        idempotency_key=f"idem-{request_id}",
        resolved_summary="install site pack",
        context_reason="Graham activated the site pack",
        risk_level="high",
        state=state,
        approval_code="ABC123",
        approval_hmac="f" * 64,
        created_at=1_000_000,
        approval_expires_at=2_000_000,
        execution_expires_at=None,
        approved_at=approved_at,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )


def test_get_request_round_trip(conn: sqlite3.Connection) -> None:
    req = _install_request(
        request_id="r1", state="approved", pack_hash="b" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    got = requests_db.get_request(conn, "r1")
    assert got is not None
    assert got.request_id == "r1"
    assert got.capability == "promoter.install_pack"
    assert got.state == "approved"
    assert got.approved_at == 1_500_000
    assert json.loads(got.params_json)["pack_hash"] == "b" * 64


def test_get_request_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert requests_db.get_request(conn, "nope") is None


# ---- requests_db.find_install_approval: lookup by PACK IDENTITY ----------


def test_find_install_approval_match_found(conn: sqlite3.Connection) -> None:
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    got = requests_db.find_install_approval(
        conn, pack_id="site", pack_hash="c" * 64
    )
    assert got is not None
    assert got.request_id == "r1"


def test_find_install_approval_match_in_executing_state(
    conn: sqlite3.Connection,
) -> None:
    """The broker moves the request approved -> executing before running the
    executor, so the approval must still resolve while it is executing."""
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    requests_db.transition(conn, "r1", "approved", "executing")
    got = requests_db.find_install_approval(
        conn, pack_id="site", pack_hash="c" * 64
    )
    assert got is not None
    assert got.request_id == "r1"
    assert got.state == "executing"


def test_find_install_approval_wrong_hash_returns_none(
    conn: sqlite3.Connection,
) -> None:
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    assert (
        requests_db.find_install_approval(conn, pack_id="site", pack_hash="d" * 64)
        is None
    )


def test_find_install_approval_wrong_state_returns_none(
    conn: sqlite3.Connection,
) -> None:
    """A denied request must NOT resolve as an approval."""
    req = _install_request(
        request_id="r1",
        state="pending_approval",
        pack_hash="c" * 64,
        approved_at=None,
    )
    requests_db.insert_request(conn, req)
    requests_db.transition(conn, "r1", "pending_approval", "denied")
    assert (
        requests_db.find_install_approval(conn, pack_id="site", pack_hash="c" * 64)
        is None
    )


def test_find_install_approval_wrong_capability_returns_none(
    conn: sqlite3.Connection,
) -> None:
    """A non-install request with matching params_json must not resolve."""
    req = requests_db.Request(
        request_id="r2",
        capability="gmail.send",  # not an install request
        params_json=json.dumps({"pack_id": "site", "pack_hash": "c" * 64}),
        params_hash="a" * 64,
        idempotency_key="idem-r2",
        resolved_summary="s",
        context_reason=None,
        risk_level="high",
        state="approved",
        approval_code="ZZZ999",
        approval_hmac="f" * 64,
        created_at=1_000_000,
        approval_expires_at=2_000_000,
        execution_expires_at=None,
        approved_at=1_500_000,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    requests_db.insert_request(conn, req)
    assert (
        requests_db.find_install_approval(conn, pack_id="site", pack_hash="c" * 64)
        is None
    )


def _install_request_raw_params(
    *, request_id: str, params_json: str
) -> requests_db.Request:
    """An approved install request with arbitrary (possibly malformed)
    params_json — to exercise find_install_approval's defensive branches."""
    return requests_db.Request(
        request_id=request_id,
        capability="promoter.install_pack",
        params_json=params_json,
        params_hash="a" * 64,
        idempotency_key=f"idem-{request_id}",
        resolved_summary="install",
        context_reason=None,
        risk_level="high",
        state="approved",
        approval_code=None,
        approval_hmac="f" * 64,
        created_at=1_000_000,
        approval_expires_at=2_000_000,
        execution_expires_at=None,
        approved_at=1_500_000,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )


def test_find_install_approval_skips_malformed_json_params(
    conn: sqlite3.Connection,
) -> None:
    """A row whose params_json is not valid JSON is skipped, not crashed on."""
    requests_db.insert_request(
        conn, _install_request_raw_params(request_id="bad", params_json="{not json")
    )
    requests_db.insert_request(
        conn,
        _install_request_raw_params(
            request_id="good",
            params_json=json.dumps({"pack_id": "site", "pack_hash": "c" * 64}),
        ),
    )
    got = requests_db.find_install_approval(
        conn, pack_id="site", pack_hash="c" * 64
    )
    assert got is not None and got.request_id == "good"


def test_find_install_approval_skips_non_dict_params(
    conn: sqlite3.Connection,
) -> None:
    """A row whose params_json is valid JSON but not an object is skipped."""
    requests_db.insert_request(
        conn, _install_request_raw_params(request_id="arr", params_json="[1, 2, 3]")
    )
    assert (
        requests_db.find_install_approval(conn, pack_id="site", pack_hash="c" * 64)
        is None
    )


# ---- RequestsDbApprovalSource: real DB-backed source (by pack identity) --


def test_db_approval_source_maps_approved(conn: sqlite3.Connection) -> None:
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    src = promoter.RequestsDbApprovalSource(conn)
    rec = src.fetch(pack_id="site", pack_hash="c" * 64)
    assert rec is not None
    assert rec.status == "approved"
    assert rec.pack_id == "site"
    assert rec.pack_hash == "c" * 64
    assert rec.approval_id == "r1"
    assert rec.consumed is False
    # approved_at is epoch-ms; the record carries epoch-seconds.
    assert rec.approved_at_ts == 1_500_000 / 1000.0


def test_db_approval_source_maps_executing_to_approved(conn: sqlite3.Connection) -> None:
    """By the time the daemon reads the request, the broker may have already
    moved it approved -> executing (it is running THIS install). Both mean a
    human approved it, so executing maps to status 'approved'."""
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    requests_db.transition(conn, "r1", "approved", "executing")
    src = promoter.RequestsDbApprovalSource(conn)
    rec = src.fetch(pack_id="site", pack_hash="c" * 64)
    assert rec is not None
    assert rec.status == "approved"


def test_db_approval_source_missing_returns_none(conn: sqlite3.Connection) -> None:
    src = promoter.RequestsDbApprovalSource(conn)
    assert src.fetch(pack_id="nope", pack_hash="0" * 64) is None


def test_db_approval_source_mark_consumed_is_best_effort(conn: sqlite3.Connection) -> None:
    """mark_consumed attempts approved->executing only when valid; a no-op (e.g.
    the row is already executing) must NOT raise — an install that succeeded must
    not be reported failed because a bookkeeping transition was a no-op."""
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)
    src = promoter.RequestsDbApprovalSource(conn)
    # First call: approved -> executing (a real transition).
    src.mark_consumed("r1")
    after_first = requests_db.get_request(conn, "r1")
    assert after_first is not None and after_first.state == "executing"
    # Second call: already executing -> no valid transition; must swallow.
    src.mark_consumed("r1")  # does not raise
    after_second = requests_db.get_request(conn, "r1")
    assert after_second is not None and after_second.state == "executing"


def test_db_approval_source_mark_consumed_unknown_id_swallows(
    conn: sqlite3.Connection,
) -> None:
    src = promoter.RequestsDbApprovalSource(conn)
    src.mark_consumed("does-not-exist")  # must not raise


def test_db_approval_source_mark_consumed_swallows_race(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the row reads as 'approved' at the guard but the transition then loses
    a race (InvalidTransition), mark_consumed must swallow it — a succeeded
    install must not be reported as failed over a bookkeeping no-op."""
    req = _install_request(
        request_id="r1", state="approved", pack_hash="c" * 64, approved_at=1_500_000
    )
    requests_db.insert_request(conn, req)

    def racing_transition(*args: Any, **kwargs: Any) -> None:
        raise requests_db.InvalidTransition("row r1 not in state 'approved'")

    monkeypatch.setattr(requests_db, "transition", racing_transition)
    src = promoter.RequestsDbApprovalSource(conn)
    src.mark_consumed("r1")  # must not raise
