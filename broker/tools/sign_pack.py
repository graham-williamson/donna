# tools/sign_pack.py
"""Off-device pack signer (promoter design §5).

Run this on the AUTHORING device where the private key lives — NEVER on the
broker host. It signs ``pack_format.canonical_bytes(pack)`` (the exact bytes
the on-device promoter verifies) with an Ed25519 private key and writes a
detached ``<pack_dir>/pack.sig``.

``keygen`` creates a fresh Ed25519 keypair: it writes the private key hex to a
file (kept off-device) and returns the public key hex, which is installed on
the Mac as ``<key_id>.ed25519.pub`` in the promoter's trusted-keys dir.

No trust/verification logic lives here — that is ``broker.pack_keys`` on the
broker host. This tool only produces signatures and keys.
"""
from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from broker import pack_format


def sign(pack_dir: str, priv_hex_path: str) -> bytes:
    """Sign the pack at ``pack_dir`` with the private key hex in
    ``priv_hex_path`` and write the detached signature to
    ``<pack_dir>/pack.sig``. Returns the raw signature bytes.
    """
    priv_hex = Path(priv_hex_path).read_text(encoding="utf-8").strip()
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    pack = pack_format.load_pack(pack_dir)
    sig = priv.sign(pack_format.canonical_bytes(pack))
    (Path(pack_dir) / "pack.sig").write_bytes(sig)
    return sig


def keygen(priv_out: str) -> str:
    """Generate an Ed25519 keypair. Write the private key hex to ``priv_out``
    (keep this off-device) and return the public key hex (install on the Mac as
    ``<key_id>.ed25519.pub``).
    """
    priv = Ed25519PrivateKey.generate()
    Path(priv_out).write_text(priv.private_bytes_raw().hex(), encoding="utf-8")
    return priv.public_key().public_bytes_raw().hex()


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "keygen":
        pub = keygen(argv[1])
        print(f"public_key_hex: {pub}")
        print("install this as <key_id>.ed25519.pub in the Mac trusted-keys dir")
        return 0
    if len(argv) == 3 and argv[0] == "sign":
        sign(argv[1], argv[2])
        print(f"wrote {argv[1]}/pack.sig")
        return 0
    print(
        "usage: sign_pack.py keygen <priv_out> | sign <pack_dir> <priv_hex_file>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
