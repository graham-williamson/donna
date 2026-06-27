# pack_keys.py
"""Trusted Ed25519 key store + signature verification (promoter design §4, §9.1).

Public keys live as `<key_id>.ed25519.pub` (hex-encoded raw 32-byte keys) in a
root-owned directory the promoter reads. A `revoked` file (one key_id per line)
removes a key from trust without deleting the file. A signature is trusted iff
it verifies against a present, non-revoked key — fail-closed: unknown, revoked,
malformed, tampered, or non-verifying ⇒ a typed exception. No bare excepts, no
silent return-None.

This module never holds a private key; signing happens off-device.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_SUFFIX = ".ed25519.pub"


class KeyStoreError(Exception):
    """A trusted-key file is malformed or unreadable. Fail-closed at load time —
    the whole store refuses to load rather than silently dropping a key."""


class SignatureError(Exception):
    """A signature does not verify against any trusted, non-revoked key (or the
    signature/message is malformed). Fail-closed — the pack is refused."""


@dataclass(frozen=True)
class TrustedKeys:
    """An immutable view of the trusted keys, with revoked ids already removed."""

    keys: dict[str, Ed25519PublicKey]  # key_id -> public key (revoked excluded)


def load_trusted_keys(path: str) -> TrustedKeys:
    """Load every `<key_id>.ed25519.pub` under `path`, dropping any id listed in
    a sibling `revoked` file. A missing directory yields an empty (fail-closed)
    store. Any malformed key file raises KeyStoreError — we never partially load
    a key store and pretend it is complete."""
    base = Path(path)
    revoked = _load_revoked(base)
    keys: dict[str, Ed25519PublicKey] = {}
    if base.is_dir():
        for child in sorted(base.glob(f"*{_SUFFIX}")):
            key_id = child.name[: -len(_SUFFIX)]
            if key_id in revoked:
                continue
            keys[key_id] = _load_one_key(child)
    return TrustedKeys(keys=keys)


def _load_revoked(base: Path) -> set[str]:
    revoked_file = base / "revoked"
    if not revoked_file.is_file():
        return set()
    try:
        text = revoked_file.read_text(encoding="utf-8")
    except OSError as e:
        raise KeyStoreError(f"cannot read revocation list: {e}") from e
    return {line.strip() for line in text.splitlines() if line.strip()}


def _load_one_key(child: Path) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(child.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as e:
        raise KeyStoreError(f"malformed trusted key {child.name}: {e}") from e
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as e:
        raise KeyStoreError(
            f"trusted key {child.name} is not a valid Ed25519 public key: {e}"
        ) from e


def verify(store: TrustedKeys, message: bytes, signature: bytes) -> str:
    """Return the key_id whose public key verifies `signature` over `message`,
    or raise SignatureError. Tries every trusted key (there are very few).

    Only `InvalidSignature` is caught per-key — that is the expected outcome for
    a key that simply did not sign this message (wrong key, tampered message, or
    a malformed/wrong-length signature). Iteration continues so another trusted
    key still gets its chance. If no key verifies, fail closed."""
    for key_id, pub in store.keys.items():
        try:
            pub.verify(signature, message)
        except InvalidSignature:
            continue
        return key_id
    raise SignatureError("no trusted key verifies this signature")
