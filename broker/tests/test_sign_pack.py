# tests/test_sign_pack.py
"""Tests for the off-device pack signer (plan Task 7).

All work happens in tmp_path; no real private keys touch disk outside the test
sandbox. Signatures are verified through the broker's own trust path
(broker.pack_keys) over pack_format.canonical_bytes — the exact bytes the
promoter checks on-device — so these tests prove the signer is interoperable
with the verifier, not merely self-consistent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from broker import pack_format, pack_keys
from broker.tools import sign_pack


def _make_pack(tmp_path: Path) -> Path:
    """Write a minimal, loadable pack under tmp_path and return its dir."""
    d = tmp_path / "pack"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    (d / "meta.json").write_text(
        json.dumps(
            {
                "pack_id": "site",
                "version": 1,
                "created_utc": "2026-06-15T00:00:00Z",
                "description": "a test pack",
                "capabilities": ["site.read"],
            }
        ),
        encoding="utf-8",
    )
    (d / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "capabilities": [
                    {
                        "name": "site.read",
                        "executor": {"type": "mcp_tool", "tool": "mcp__a__b"},
                        "param_schema": {"$ref": "./schemas/site.json"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (d / "schemas" / "site.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    return d


def _install_pub(tmp_path: Path, key_id: str, pub_hex: str) -> pack_keys.TrustedKeys:
    """Install a public key hex as <key_id>.ed25519.pub and load the store."""
    keys_dir = tmp_path / "trusted_keys"
    keys_dir.mkdir(exist_ok=True)
    (keys_dir / f"{key_id}.ed25519.pub").write_text(pub_hex, encoding="utf-8")
    return pack_keys.load_trusted_keys(str(keys_dir))


def test_keygen_writes_private_hex_and_returns_usable_public(tmp_path: Path) -> None:
    priv_out = tmp_path / "key.priv"
    pub_hex = sign_pack.keygen(str(priv_out))

    # private file is hex of the raw 32-byte Ed25519 private key
    priv_hex = priv_out.read_text().strip()
    assert len(bytes.fromhex(priv_hex)) == 32
    # public hex is the raw 32-byte public key
    assert len(bytes.fromhex(pub_hex)) == 32

    # The returned public hex verifies a signature made with the written
    # private key (round-trip through pack_keys, the on-device trust path).
    pack_dir = _make_pack(tmp_path)
    sig = sign_pack.sign(str(pack_dir), str(priv_out))
    store = _install_pub(tmp_path, "k1", pub_hex)
    pack = pack_format.load_pack(str(pack_dir))
    assert pack_keys.verify(store, pack_format.canonical_bytes(pack), sig) == "k1"


def test_sign_writes_pack_sig_that_verifies(tmp_path: Path) -> None:
    priv_out = tmp_path / "key.priv"
    pub_hex = sign_pack.keygen(str(priv_out))
    pack_dir = _make_pack(tmp_path)

    returned = sign_pack.sign(str(pack_dir), str(priv_out))

    sig_path = pack_dir / "pack.sig"
    assert sig_path.is_file()
    assert sig_path.read_bytes() == returned

    store = _install_pub(tmp_path, "k1", pub_hex)
    pack = pack_format.load_pack(str(pack_dir))
    assert (
        pack_keys.verify(store, pack_format.canonical_bytes(pack), returned) == "k1"
    )


def test_signing_twice_both_verify(tmp_path: Path) -> None:
    priv_out = tmp_path / "key.priv"
    pub_hex = sign_pack.keygen(str(priv_out))
    pack_dir = _make_pack(tmp_path)
    store = _install_pub(tmp_path, "k1", pub_hex)

    sig1 = sign_pack.sign(str(pack_dir), str(priv_out))
    sig2 = sign_pack.sign(str(pack_dir), str(priv_out))

    pack = pack_format.load_pack(str(pack_dir))
    msg = pack_format.canonical_bytes(pack)
    # Ed25519 is deterministic, but assert verification, not byte-equality.
    assert pack_keys.verify(store, msg, sig1) == "k1"
    assert pack_keys.verify(store, msg, sig2) == "k1"


def test_sig_from_wrong_key_does_not_verify(tmp_path: Path) -> None:
    priv_out = tmp_path / "key.priv"
    sign_pack.keygen(str(priv_out))  # the signing key
    other_pub = sign_pack.keygen(str(tmp_path / "other.priv"))  # a different key
    pack_dir = _make_pack(tmp_path)

    sig = sign_pack.sign(str(pack_dir), str(priv_out))
    store = _install_pub(tmp_path, "other", other_pub)
    pack = pack_format.load_pack(str(pack_dir))
    with pytest.raises(pack_keys.SignatureError):
        pack_keys.verify(store, pack_format.canonical_bytes(pack), sig)


def test_main_keygen_returns_zero(tmp_path: Path) -> None:
    priv_out = tmp_path / "key.priv"
    assert sign_pack.main(["keygen", str(priv_out)]) == 0
    assert priv_out.is_file()


def test_main_sign_returns_zero(tmp_path: Path) -> None:
    priv_out = tmp_path / "key.priv"
    sign_pack.keygen(str(priv_out))
    pack_dir = _make_pack(tmp_path)
    assert sign_pack.main(["sign", str(pack_dir), str(priv_out)]) == 0
    assert (pack_dir / "pack.sig").is_file()


def test_main_bad_usage_returns_two() -> None:
    assert sign_pack.main([]) == 2
    assert sign_pack.main(["bogus"]) == 2
    assert sign_pack.main(["keygen"]) == 2  # missing priv_out arg
