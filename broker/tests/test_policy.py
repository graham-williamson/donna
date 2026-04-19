"""Tests for broker.policy.

Spec: security-v1.1 §7.2 (idempotency), §7.3 (HMAC), §7.4 (cooldown +
override), §7.7 (context_reason sanitisation), §13.2 (rate limits),
§9.1 (purity).
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3

import pytest

from broker import policy
from broker import requests_db as db


# ---- module surface ------------------------------------------------------


def test_module_importable():
    for name in (
        "idempotency_key",
        "generate_approval_code",
        "compute_creation_hmac",
        "compute_approval_hmac",
        "verify_hmac",
        "rate_limit_check",
        "rate_limit_increment",
        "cooldown_remaining_seconds",
        "sanitise_context_reason",
        "build_creation_message",
        "ContextReasonTooLong",
    ):
        assert hasattr(policy, name), name


def test_no_network_imports_at_module_level():
    """Sanity — authoritative check is the import-linter contract."""
    import broker.policy as p
    import sys
    banned = {"requests", "httpx", "aiohttp", "urllib3"}
    loaded = banned & set(sys.modules)
    # These may be imported by other modules. Check none are referenced
    # from broker.policy's own namespace.
    for name in banned:
        assert not hasattr(p, name.split(".")[0]), (
            f"broker.policy references banned module {name}"
        )


# ---- §7.2 idempotency key ------------------------------------------------


def _manual_idempotency(capability: str, canonical: bytes, date: str) -> str:
    return hashlib.sha256(
        capability.encode("utf-8")
        + b"\x1f"
        + canonical
        + b"\x1f"
        + date.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "capability,canonical,date",
    [
        ("gmail.create_draft", b"{}", "2026-04-21"),
        ("puregym.book_class", b'{"class_id":"hiit"}', "2026-04-21"),
        ("gcal.create_event", b'{"summary":"meet"}', "2026-05-01"),
        # Unicode inside canonical params.
        ("notion.create_pages", '{"title":"café"}'.encode("utf-8"), "2026-04-21"),
        # Empty canonical.
        ("cap", b"", "2026-04-21"),
    ],
)
def test_idempotency_key_matches_manual(capability, canonical, date):
    expected = _manual_idempotency(capability, canonical, date)
    assert policy.idempotency_key(capability, canonical, date) == expected


def test_idempotency_key_differs_on_any_field(capability_key="gmail.create_draft"):
    a = policy.idempotency_key("a", b"{}", "2026-04-21")
    b = policy.idempotency_key("b", b"{}", "2026-04-21")
    c = policy.idempotency_key("a", b"{ }", "2026-04-21")
    d = policy.idempotency_key("a", b"{}", "2026-04-22")
    assert len({a, b, c, d}) == 4


def test_idempotency_key_rejects_wrong_types():
    with pytest.raises(TypeError):
        policy.idempotency_key(42, b"{}", "2026-04-21")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.idempotency_key("cap", "not bytes", "2026-04-21")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.idempotency_key("cap", b"{}", 42)  # type: ignore[arg-type]


# ---- §7.3 approval code --------------------------------------------------


def test_approval_code_length():
    for _ in range(20):
        code = policy.generate_approval_code()
        assert len(code) == policy.APPROVAL_CODE_LENGTH


def test_approval_code_alphabet_constrained():
    """Over 10k samples every character stays inside the 32-symbol set."""
    alphabet = set(policy.APPROVAL_CODE_ALPHABET)
    seen_chars: set[str] = set()
    for _ in range(10_000):
        code = policy.generate_approval_code()
        for ch in code:
            assert ch in alphabet, f"char {ch!r} outside alphabet"
            seen_chars.add(ch)
    # Distribution sanity: over 10k samples, most of the 32 chars appear.
    assert len(seen_chars) >= 30


def test_approval_code_alphabet_excludes_iluou():
    for banned in "ILOU":
        assert banned not in policy.APPROVAL_CODE_ALPHABET


# ---- §7.3 HMAC serialisation ---------------------------------------------


def _manual_hmac(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


@pytest.fixture
def test_key() -> bytes:
    return b"A" * 32


def test_creation_message_exact_bytes(test_key):
    """Canonical serialisation is test-anchored — changing field order
    or encoding would be a silent compatibility break."""
    msg = policy.build_creation_message(
        request_id="req-001",
        capability="gmail.create_draft",
        params_hash="a" * 64,
        idempotency_key_="b" * 64,
        risk_level="medium",
        created_at=1_700_000_000_000,
        approval_expires_at=1_700_086_400_000,
    )
    assert msg == b"\x1f".join([
        b"req-001",
        b"gmail.create_draft",
        b"a" * 64,
        b"b" * 64,
        b"medium",
        b"1700000000000",
        b"1700086400000",
    ])


def test_compute_creation_hmac_matches_manual(test_key):
    msg = policy.build_creation_message(
        request_id="req-001",
        capability="gmail.create_draft",
        params_hash="a" * 64,
        idempotency_key_="b" * 64,
        risk_level="medium",
        created_at=1_700_000_000_000,
        approval_expires_at=1_700_086_400_000,
    )
    expected = _manual_hmac(test_key, msg)
    actual = policy.compute_creation_hmac(
        key=test_key,
        request_id="req-001",
        capability="gmail.create_draft",
        params_hash="a" * 64,
        idempotency_key_="b" * 64,
        risk_level="medium",
        created_at=1_700_000_000_000,
        approval_expires_at=1_700_086_400_000,
    )
    assert actual == expected


def test_compute_approval_hmac_extends_creation(test_key):
    creation = policy.build_creation_message(
        request_id="req-001",
        capability="gmail.create_draft",
        params_hash="a" * 64,
        idempotency_key_="b" * 64,
        risk_level="medium",
        created_at=1_700_000_000_000,
        approval_expires_at=1_700_086_400_000,
    )
    expected_msg = creation + b"\x1f" + b"1700043200000" + b"\x1f" + b"1700010000000"
    expected = _manual_hmac(test_key, expected_msg)
    actual = policy.compute_approval_hmac(
        key=test_key,
        creation_msg=creation,
        execution_expires_at=1_700_043_200_000,
        approved_at=1_700_010_000_000,
    )
    assert actual == expected


def test_negative_timestamps_encode_with_minus(test_key):
    msg = policy.build_creation_message(
        request_id="r",
        capability="c",
        params_hash="h",
        idempotency_key_="i",
        risk_level="low",
        created_at=-1,
        approval_expires_at=1,
    )
    assert b"\x1f-1\x1f" in msg


def test_all_covered_fields_appear_in_message():
    """Every §6 immutable-at-creation field must appear in the HMAC
    message exactly once, separated by \\x1f. Typo-catching assertion."""
    msg = policy.build_creation_message(
        request_id="REQID",
        capability="CAP",
        params_hash="PARAMSHASH",
        idempotency_key_="IDEMKEY",
        risk_level="medium",
        created_at=11,
        approval_expires_at=22,
    )
    parts = msg.split(b"\x1f")
    assert parts == [
        b"REQID", b"CAP", b"PARAMSHASH", b"IDEMKEY",
        b"medium", b"11", b"22",
    ]


# ---- verify_hmac constant-time --------------------------------------------


def test_verify_hmac_success(test_key):
    msg = b"hello"
    digest = hmac.new(test_key, msg, hashlib.sha256).hexdigest()
    assert policy.verify_hmac(test_key, msg, digest) is True


def test_verify_hmac_wrong_digest(test_key):
    msg = b"hello"
    assert policy.verify_hmac(test_key, msg, "0" * 64) is False


def test_verify_hmac_wrong_length_no_raise(test_key):
    """Attacker-controlled digest: truncated, overlong, wrong case.
    Must return False without raising."""
    msg = b"hello"
    assert policy.verify_hmac(test_key, msg, "short") is False
    assert policy.verify_hmac(test_key, msg, "z" * 128) is False


def test_verify_hmac_uses_compare_digest():
    """Source-level verification: the implementation must use
    hmac.compare_digest, not == comparison."""
    import inspect
    src = inspect.getsource(policy.verify_hmac)
    assert "compare_digest" in src


# ---- §13.2 rate limits ---------------------------------------------------


@pytest.fixture
def rl_conn(tmp_path):
    conn = db.open_db(str(tmp_path / "requests.db"))
    yield conn
    conn.close()


def test_rate_limit_check_under_cap(rl_conn):
    assert policy.rate_limit_check(rl_conn, "gmail.create_draft", 10, "2026-04-21") is True


def test_rate_limit_increment_counts(rl_conn):
    for _ in range(3):
        policy.rate_limit_increment(rl_conn, "cap", "2026-04-21")
    row = rl_conn.execute(
        "SELECT count FROM rate_limits WHERE capability = ? AND date_utc = ?",
        ("cap", "2026-04-21"),
    ).fetchone()
    assert row["count"] == 3


def test_rate_limit_check_at_cap(rl_conn):
    for _ in range(5):
        policy.rate_limit_increment(rl_conn, "cap", "2026-04-21")
    assert policy.rate_limit_check(rl_conn, "cap", 5, "2026-04-21") is False
    assert policy.rate_limit_check(rl_conn, "cap", 6, "2026-04-21") is True


def test_rate_limit_scoped_per_date(rl_conn):
    policy.rate_limit_increment(rl_conn, "cap", "2026-04-21")
    policy.rate_limit_increment(rl_conn, "cap", "2026-04-22")
    rows = rl_conn.execute(
        "SELECT date_utc, count FROM rate_limits WHERE capability = 'cap' "
        "ORDER BY date_utc"
    ).fetchall()
    assert [(r["date_utc"], r["count"]) for r in rows] == [
        ("2026-04-21", 1),
        ("2026-04-22", 1),
    ]


# ---- §7.4 cooldown -------------------------------------------------------


def _insert_denied(conn, idempotency_key: str, created_at_ms: int) -> None:
    r = db.Request(
        request_id=f"r-{idempotency_key}"[:50],
        capability="c",
        params_json="{}",
        params_hash="a" * 64,
        idempotency_key=idempotency_key,
        resolved_summary="s",
        context_reason=None,
        risk_level="medium",
        state="pending_approval",
        approval_code=None,
        approval_hmac=None,
        created_at=created_at_ms,
        approval_expires_at=created_at_ms + 10_000,
        execution_expires_at=None,
        approved_at=None,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    db.insert_request(conn, r)
    db.transition(conn, r.request_id, "pending_approval", "denied")


def test_cooldown_no_denied_row_returns_zero(rl_conn):
    assert policy.cooldown_remaining_seconds(rl_conn, "never-denied") == 0


def test_cooldown_within_window(rl_conn):
    now = 1_700_000_000_000
    _insert_denied(rl_conn, "k1", now - 5 * 60 * 1000)  # 5 min ago
    remaining = policy.cooldown_remaining_seconds(
        rl_conn, "k1", cooldown_minutes=30, now_ms=now
    )
    assert 24 * 60 <= remaining <= 25 * 60


def test_cooldown_expired(rl_conn):
    now = 1_700_000_000_000
    _insert_denied(rl_conn, "k1", now - 40 * 60 * 1000)  # 40 min ago
    assert policy.cooldown_remaining_seconds(
        rl_conn, "k1", cooldown_minutes=30, now_ms=now
    ) == 0


def test_cooldown_custom_minutes(rl_conn):
    now = 1_700_000_000_000
    _insert_denied(rl_conn, "k1", now - 10 * 60 * 1000)  # 10 min ago
    # 5-minute cooldown → already expired.
    assert policy.cooldown_remaining_seconds(
        rl_conn, "k1", cooldown_minutes=5, now_ms=now
    ) == 0
    # 60-minute cooldown → 50 min left.
    remaining = policy.cooldown_remaining_seconds(
        rl_conn, "k1", cooldown_minutes=60, now_ms=now
    )
    assert 49 * 60 <= remaining <= 51 * 60


# ---- §7.7 sanitise_context_reason ----------------------------------------


def test_sanitise_empty_string():
    assert policy.sanitise_context_reason("") == ("", [])


def test_sanitise_plain_ascii_untouched():
    s = "Chief asked for Tuesday evening HIIT class"
    assert policy.sanitise_context_reason(s) == (s, [])


def test_sanitise_too_long_raises():
    with pytest.raises(policy.ContextReasonTooLong):
        policy.sanitise_context_reason("x" * (policy.MAX_CONTEXT_REASON_LENGTH + 1))


def test_sanitise_strips_url():
    result, types = policy.sanitise_context_reason(
        "see https://example.com/secret for details"
    )
    assert "[redacted]" in result
    assert "https://" not in result
    assert "url" in types


def test_sanitise_strips_long_base64():
    payload = "A" * 30
    result, types = policy.sanitise_context_reason(f"token {payload} embedded")
    assert "[redacted]" in result
    assert payload not in result
    assert "base64" in types


def test_sanitise_strips_long_hex():
    result, types = policy.sanitise_context_reason(
        "sig deadbeefcafebabe1234567890abcdef end"
    )
    assert "[redacted]" in result
    assert "deadbeefcafebabe1234567890abcdef" not in result
    assert "hex" in types


def test_sanitise_strips_long_digits():
    result, types = policy.sanitise_context_reason("PIN 1234567890 today")
    assert "[redacted]" in result
    assert "1234567890" not in result
    assert "digits" in types


def test_sanitise_short_digits_kept():
    """5 or fewer consecutive digits are not redacted — the cutoff is 6."""
    result, types = policy.sanitise_context_reason("bus 42 to meeting at 3pm")
    assert "42" in result
    assert "3pm" in result
    assert "digits" not in types


def test_sanitise_keeps_printable_latin1():
    """Standard Latin-1 accented letters are kept."""
    s = "café déjà vu"
    result, types = policy.sanitise_context_reason(s)
    assert "café" in result
    assert "déjà" in result
    assert "non_ascii" not in types


def test_sanitise_strips_cjk():
    result, types = policy.sanitise_context_reason("meet 日本 at 3pm")
    assert "日本" not in result
    assert "non_ascii" in types


def test_sanitise_strips_rtl_override():
    """RTL override is a classic obfuscation trick — must strip."""
    bad = "file\u202eneve.txt"
    result, types = policy.sanitise_context_reason(bad)
    assert "\u202e" not in result
    assert "non_ascii" in types


def test_sanitise_strips_zero_width_joiner():
    bad = "hi\u200dthere"
    result, types = policy.sanitise_context_reason(bad)
    assert "\u200d" not in result
    assert "non_ascii" in types


def test_sanitise_multiple_redactions():
    result, types = policy.sanitise_context_reason(
        "see https://evil.com token aaaaaaaaaaaaaaaaaaaaaaaaaa and PIN 987654"
    )
    assert "url" in types
    # base64 OR hex may fire depending on content; at least one should:
    assert {"base64", "hex", "digits"} & set(types)


def test_sanitise_rejects_non_string():
    with pytest.raises(TypeError):
        policy.sanitise_context_reason(123)  # type: ignore[arg-type]
