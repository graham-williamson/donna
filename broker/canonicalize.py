"""Canonicalize JSON per RFC 8785 (JCS).

Spec: security-v1.1 §7.1

The broker canonicalises every `params_json` before hashing so identical
payloads produce identical `params_hash` regardless of whitespace, key
order, or trivial number formatting. The canonical form is the exclusive
input to `params_hash` and feeds every HMAC check.

Test contract lives in `broker/tests/canonicalize_vectors.json` (~20
input→canonical pairs). Any new number-precision or Unicode edge case
must be added to the vectors file before changing this module.

Phase 1 Ralph target — see `broker/ralph-prompts/canonicalize.md`.
"""
from __future__ import annotations

from typing import Any


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of `value`.

    Must:
      - sort object keys by UTF-16 code unit sequence (JCS §3.2.3)
      - emit numbers per ECMAScript Number-to-String (JCS §3.2.2.3)
      - emit strings with the minimal JSON escape set (JCS §3.2.2.2)
      - strip all insignificant whitespace
      - encode output as UTF-8 without BOM

    Returns raw bytes, not str — downstream consumers hash these bytes.
    """
    raise NotImplementedError("canonicalize: Phase 1 Ralph target")


def params_hash(params: Any) -> str:
    """Return the hex sha256 of `canonicalize(params)`.

    Used as `requests.params_hash` and as the HMAC covers this hash,
    not the raw params_json (§7.3).
    """
    raise NotImplementedError("params_hash: Phase 1 Ralph target")
