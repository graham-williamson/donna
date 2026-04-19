"""Tests for broker.resolver.

Spec: security-v1.1 §9 (policy-check purity + subprocess isolation),
§12.5 (provenance), §12.6 (prompt content).

Ralph target scope (see ralph-prompts/resolver.md):
  - policy_check_mode() is pure: monkey-patched subprocess.run raises if
    called; network sockets raise if opened.
  - request_mode() for a capability with a network-touching resolver
    spawns a subprocess with:
      env has no HMAC_KEY / BROKER_DB_PATH / any *_TOKEN;
      pass_fds=();
      cwd under /tmp and ephemeral;
      stdout/stderr size caps enforced.
  - Resolver stderr > 4KB truncated to 4KB with explicit marker.
  - Resolver output schema-validated; unexpected fields raise
    enrichment_failed; caller proceeds with degraded summary.
  - String fields from resolver tagged provenance="donna"; integers /
    enums / bools that pass schema tagged "broker".
"""
from __future__ import annotations

import pytest

from broker import resolver


def test_module_importable():
    assert hasattr(resolver, "policy_check_mode")
    assert hasattr(resolver, "request_mode")


# TODO(phase-1 ralph): full coverage per spec-ref list above.
