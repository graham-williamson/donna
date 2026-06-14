from __future__ import annotations

from broker import browser_token as bt


def _store() -> bt.TokenStore:
    return bt.TokenStore(now=lambda: 1000.0, ttl_seconds=120.0)


def test_mint_then_consume_adjacent_action():
    s = _store()
    tok = s.mint(summary="Book 7pm court £8", snapshot_hash="h1",
                 target_ref="r5", expected_text="Confirm booking", approval_id="A1")
    s.consume(tok, snapshot_hash="h1", target_ref="r5", expected_text="Confirm booking")


def test_token_is_single_use():
    s = _store()
    tok = s.mint(summary="x", snapshot_hash="h1", target_ref="r5",
                 expected_text="Confirm", approval_id="A1")
    s.consume(tok, snapshot_hash="h1", target_ref="r5", expected_text="Confirm")
    try:
        s.consume(tok, snapshot_hash="h1", target_ref="r5", expected_text="Confirm")
        assert False, "a consumed token must not be reusable"
    except bt.TokenError:
        pass


def test_token_bound_to_target_and_text():
    s = _store()
    tok = s.mint(summary="x", snapshot_hash="h1", target_ref="r5",
                 expected_text="Confirm", approval_id="A1")
    try:
        s.consume(tok, snapshot_hash="h1", target_ref="r9", expected_text="Confirm")
        assert False, "wrong target_ref must be refused"
    except bt.TokenError:
        pass


def test_token_expires():
    s = bt.TokenStore(now=lambda: 1000.0, ttl_seconds=10.0)
    tok = s.mint(summary="x", snapshot_hash="h1", target_ref="r5",
                 expected_text="Confirm", approval_id="A1")
    s._now = lambda: 1011.0
    try:
        s.consume(tok, snapshot_hash="h1", target_ref="r5", expected_text="Confirm")
        assert False, "expired token must be refused"
    except bt.TokenError:
        pass


def test_unknown_token_refused():
    s = _store()
    try:
        s.consume("not-a-real-token", snapshot_hash="h1", target_ref="r5", expected_text="Confirm")
        assert False
    except bt.TokenError:
        pass


def test_only_one_live_token_at_a_time():
    s = _store()
    s.mint(summary="a", snapshot_hash="h1", target_ref="r1", expected_text="A", approval_id="A1")
    tok2 = s.mint(summary="b", snapshot_hash="h2", target_ref="r2", expected_text="B", approval_id="A2")
    assert s.live_token_id == tok2


def test_snapshot_mismatch_refused():
    s = _store()
    tok = s.mint(summary="x", snapshot_hash="h1", target_ref="r5",
                 expected_text="Confirm", approval_id="A1")
    try:
        s.consume(tok, snapshot_hash="DIFFERENT", target_ref="r5", expected_text="Confirm")
        assert False, "a changed page (snapshot) must void the token"
    except bt.TokenError:
        pass


def test_superseded_token_cannot_be_consumed():
    s = _store()
    tok1 = s.mint(summary="a", snapshot_hash="h1", target_ref="r1", expected_text="A", approval_id="A1")
    s.mint(summary="b", snapshot_hash="h2", target_ref="r2", expected_text="B", approval_id="A2")
    try:
        s.consume(tok1, snapshot_hash="h1", target_ref="r1", expected_text="A")
        assert False, "a superseded token must not be consumable"
    except bt.TokenError:
        pass
