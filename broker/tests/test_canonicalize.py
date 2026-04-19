"""Test-vector-driven canonicalize tests.

Spec: security-v1.1 §7.1. Vectors live in canonicalize_vectors.json and
are the authoritative encoding of expected behaviour. Add a vector
before changing module behaviour.

Ralph target: make every vector pass (no skips). Coverage ≥95% for
broker/canonicalize.py.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from broker import canonicalize


VECTORS_PATH = Path(__file__).parent / "canonicalize_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_canonicalize_matches_expected(vector):
    try:
        result = canonicalize.canonicalize(vector["input"])
    except NotImplementedError:
        pytest.skip("Phase 1 Ralph target — canonicalize() not yet implemented")
    expected = vector["canonical"].encode("utf-8")
    assert result == expected, vector.get("description", vector["name"])


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_params_hash_is_sha256_of_canonical(vector):
    try:
        canonical = canonicalize.canonicalize(vector["input"])
        hash_ = canonicalize.params_hash(vector["input"])
    except NotImplementedError:
        pytest.skip("Phase 1 Ralph target — canonicalize()/params_hash() not yet implemented")
    assert hash_ == hashlib.sha256(canonical).hexdigest()


def test_canonicalize_idempotent():
    """Canonicalizing a value that's already been parsed from its own
    canonical form must produce the same bytes. Guards against ordering
    drift under repeated round-trips."""
    try:
        once = canonicalize.canonicalize({"b": 2, "a": 1})
        twice = canonicalize.canonicalize(json.loads(once.decode("utf-8")))
    except NotImplementedError:
        pytest.skip("Phase 1 Ralph target")
    assert once == twice
