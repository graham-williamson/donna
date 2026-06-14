# tests/test_pack_keys.py
"""Adversarial tests for the trusted Ed25519 key store + fail-closed verify
(promoter design §4, §9.1). Fail-closed is the contract: unknown, revoked,
malformed, tampered, empty store, or wrong-length signature ⇒ raise, never
silently pass.
"""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from broker import pack_keys


def _make_key(d: Path, key_id: str, *, revoked: bool = False) -> Ed25519PrivateKey:
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes_raw()
    (d / f"{key_id}.ed25519.pub").write_text(raw.hex())
    if revoked:
        existing = (d / "revoked").read_text() if (d / "revoked").exists() else ""
        (d / "revoked").write_text(existing + key_id + "\n")
    return priv


def test_verify_accepts_signature_from_trusted_key(tmp_path: Path) -> None:
    priv = _make_key(tmp_path, "k1")
    store = pack_keys.load_trusted_keys(str(tmp_path))
    msg = b"hello pack"
    sig = priv.sign(msg)
    assert pack_keys.verify(store, msg, sig) == "k1"


def test_verify_rejects_signature_from_unknown_key(tmp_path: Path) -> None:
    _make_key(tmp_path, "k1")
    other = Ed25519PrivateKey.generate()
    store = pack_keys.load_trusted_keys(str(tmp_path))
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"m", other.sign(b"m"))


def test_verify_rejects_tampered_message(tmp_path: Path) -> None:
    priv = _make_key(tmp_path, "k1")
    store = pack_keys.load_trusted_keys(str(tmp_path))
    sig = priv.sign(b"original")
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"tampered", sig)


def test_verify_rejects_revoked_key(tmp_path: Path) -> None:
    priv = _make_key(tmp_path, "k1", revoked=True)
    store = pack_keys.load_trusted_keys(str(tmp_path))
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"m", priv.sign(b"m"))


def test_load_trusted_keys_empty_dir_is_no_keys(tmp_path: Path) -> None:
    store = pack_keys.load_trusted_keys(str(tmp_path))
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"m", b"\x00" * 64)


def test_load_trusted_keys_rejects_malformed_key_file(tmp_path: Path) -> None:
    (tmp_path / "bad.ed25519.pub").write_text("not-hex-zzzz")
    with pytest.raises(pack_keys.KeyStoreError):
        pack_keys.load_trusted_keys(str(tmp_path))


# --- extra adversarial tests (beyond the plan's list) ---


def test_verify_rejects_wrong_length_signature(tmp_path: Path) -> None:
    """A signature of the wrong length (10 bytes) must be rejected with
    SignatureError — cryptography raises InvalidSignature on a malformed
    signature, which verify() must convert to a typed, fail-closed error
    rather than letting an uncaught exception escape."""
    priv = _make_key(tmp_path, "k1")
    store = pack_keys.load_trusted_keys(str(tmp_path))
    # message itself is valid; the signature is simply too short
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"m", b"\x00" * 10)


def test_verify_returns_second_key_when_only_it_matches(tmp_path: Path) -> None:
    """With two trusted keys present and only the second matching, verify
    returns the second key's id — the first key's InvalidSignature must be
    swallowed and iteration must continue."""
    _make_key(tmp_path, "k1")
    priv2 = _make_key(tmp_path, "k2")
    store = pack_keys.load_trusted_keys(str(tmp_path))
    msg = b"signed only by k2"
    assert pack_keys.verify(store, msg, priv2.sign(msg)) == "k2"


def test_load_trusted_keys_missing_dir_is_empty_store(tmp_path: Path) -> None:
    """A non-existent key directory yields an empty store (fail-closed): no
    keys, and any verify against it raises SignatureError."""
    missing = tmp_path / "does-not-exist"
    store = pack_keys.load_trusted_keys(str(missing))
    assert store.keys == {}
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"m", b"\x00" * 64)


def test_load_trusted_keys_skips_revoked_but_keeps_others(tmp_path: Path) -> None:
    """When a revoked key and a live key both have .pub files present, the
    revoked one is skipped at load time and the live one is kept — exercising
    the continue path inside the load loop."""
    _make_key(tmp_path, "revoked_key", revoked=True)
    priv_ok = _make_key(tmp_path, "good_key")
    store = pack_keys.load_trusted_keys(str(tmp_path))
    assert set(store.keys) == {"good_key"}
    msg = b"signed by good_key"
    assert pack_keys.verify(store, msg, priv_ok.sign(msg)) == "good_key"


def test_load_trusted_keys_rejects_valid_hex_wrong_length(tmp_path: Path) -> None:
    """A key file that is valid hex but the wrong byte length is not a valid
    Ed25519 public key — KeyStoreError, fail-closed at load time."""
    (tmp_path / "shortkey.ed25519.pub").write_text("aabbccdd")  # 4 bytes, not 32
    with pytest.raises(pack_keys.KeyStoreError):
        pack_keys.load_trusted_keys(str(tmp_path))


def test_load_trusted_keys_rejects_unreadable_revoked_file(tmp_path: Path) -> None:
    """An unreadable `revoked` file fails closed (KeyStoreError) rather than
    silently treating the store as having no revocations."""
    _make_key(tmp_path, "k1")
    revoked_file = tmp_path / "revoked"
    revoked_file.write_text("k1\n")
    revoked_file.chmod(0o000)
    try:
        with pytest.raises(pack_keys.KeyStoreError):
            pack_keys.load_trusted_keys(str(tmp_path))
    finally:
        revoked_file.chmod(0o600)


def test_verify_rejects_revoked_key_even_though_pub_file_present(tmp_path: Path) -> None:
    """A revoked key whose signature would otherwise verify is rejected even
    though the .pub file is still on disk — revocation removes it from trust
    at load time."""
    priv = _make_key(tmp_path, "k1", revoked=True)
    # the .pub file is still present (revocation file lists it; not deleted)
    assert (tmp_path / "k1.ed25519.pub").exists()
    store = pack_keys.load_trusted_keys(str(tmp_path))
    assert "k1" not in store.keys
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, b"m", priv.sign(b"m"))
