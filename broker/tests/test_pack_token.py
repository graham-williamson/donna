# tests/test_pack_token.py
import pytest
from broker import pack_token


def _rec(
    *,
    pack_id: str = "waitrose",
    pack_hash: str = "abc",
    approval_id: str = "A1",
    status: str = "approved",
    approved_at_ts: float = 100.0,
    consumed: bool = False,
) -> pack_token.ApprovalRecord:
    return pack_token.ApprovalRecord(
        pack_id=pack_id,
        pack_hash=pack_hash,
        approval_id=approval_id,
        status=status,
        approved_at_ts=approved_at_ts,
        consumed=consumed,
    )


def test_valid_record_authorises_matching_pack():
    aid = pack_token.verify_approval(_rec(), pack_id="waitrose", pack_hash="abc",
                                     now_ts=200.0, ttl_seconds=300.0)
    assert aid == "A1"


def test_unapproved_record_rejected():
    with pytest.raises(pack_token.PackTokenError, match="not approved"):
        pack_token.verify_approval(_rec(status="pending"), pack_id="waitrose",
                                   pack_hash="abc", now_ts=200.0, ttl_seconds=300.0)


def test_already_consumed_record_rejected():
    with pytest.raises(pack_token.PackTokenError, match="consumed"):
        pack_token.verify_approval(_rec(consumed=True), pack_id="waitrose",
                                   pack_hash="abc", now_ts=200.0, ttl_seconds=300.0)


def test_record_bound_to_pack_id():
    with pytest.raises(pack_token.PackTokenError):
        pack_token.verify_approval(_rec(), pack_id="tesco", pack_hash="abc",
                                   now_ts=200.0, ttl_seconds=300.0)


def test_record_bound_to_pack_hash():
    with pytest.raises(pack_token.PackTokenError):
        pack_token.verify_approval(_rec(), pack_id="waitrose", pack_hash="DIFFERENT",
                                   now_ts=200.0, ttl_seconds=300.0)


def test_expired_record_rejected():
    with pytest.raises(pack_token.PackTokenError, match="expired"):
        pack_token.verify_approval(_rec(approved_at_ts=100.0), pack_id="waitrose",
                                   pack_hash="abc", now_ts=500.0, ttl_seconds=300.0)


# --- extra tests (TTL boundary; multi-failure does not crash) ---

def test_record_exactly_at_ttl_edge_is_valid():
    # now_ts - approved_at_ts == ttl_seconds → NOT > ttl → still valid.
    aid = pack_token.verify_approval(_rec(approved_at_ts=100.0), pack_id="waitrose",
                                     pack_hash="abc", now_ts=400.0, ttl_seconds=300.0)
    assert aid == "A1"


def test_record_one_second_past_ttl_edge_is_rejected():
    with pytest.raises(pack_token.PackTokenError, match="expired"):
        pack_token.verify_approval(_rec(approved_at_ts=100.0), pack_id="waitrose",
                                   pack_hash="abc", now_ts=401.0, ttl_seconds=300.0)


def test_record_failing_multiple_checks_still_raises_packtokenerror():
    # Unapproved AND consumed AND wrong pack AND expired — must raise cleanly,
    # not crash, regardless of which check fires first.
    bad = _rec(status="pending", consumed=True, pack_id="tesco",
               pack_hash="DIFFERENT", approved_at_ts=0.0)
    with pytest.raises(pack_token.PackTokenError):
        pack_token.verify_approval(bad, pack_id="waitrose", pack_hash="abc",
                                   now_ts=9999.0, ttl_seconds=300.0)
