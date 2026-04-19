"""Mode-aware resolver with subprocess isolation for enrichment.

Spec: security-v1.1 §9 (policy-check purity + resolver isolation),
§12.5 (field provenance), §12.6 (approval prompt content).

Two entry modes:
  - `policy_check_mode`: pure, local-only, deterministic, ≤1s budget.
    Must NOT spawn subprocesses, touch the network, or read anything
    outside the broker state dir + manifest (§9.1).
  - `request_mode`: may enrich. Subprocess isolation is mandatory
    when the resolver touches the network (§9.2):
        * sanitised env (PATH only; no HMAC_KEY, BROKER_DB_PATH)
        * pass_fds=()
        * stdin: validated JSON; stdout: schema-validated JSON ≤64KB
        * stderr: capped at 4KB per invocation, logged as
          `resolver_stderr` audit event
        * cwd: ephemeral /tmp/donna-resolver-<uuid>
        * timeout: capability-configurable, default 10s
        * dependency isolation per capability
      Resolver output strings are tagged `provenance: "donna"` (§12.5);
      only strictly-typed schema-validated values are `provenance:
      "broker"`.

Enrichment failure is non-blocking: approval proceeds with degraded
`resolved_summary`, `audit.enrichment_failed` emitted (§9.2).

Phase 1 Ralph target — see `broker/ralph-prompts/resolver.md`.
"""
from __future__ import annotations

from typing import Any


def policy_check_mode(
    capability_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Pure summary render for the hook path. No subprocess, no network,
    no filesystem beyond the manifest. Returns a dict suitable for queue
    file rendering or direct hook response."""
    raise NotImplementedError("policy_check_mode: Phase 1 Ralph target")


def request_mode(
    capability_name: str,
    params: dict[str, Any],
    audit_writer: Any,
) -> dict[str, Any]:
    """Full resolver. Spawns the per-capability resolver subprocess
    when a network-touching enricher is declared. Returns a dict with
    provenance-tagged fields ready for the queue file."""
    raise NotImplementedError("request_mode: Phase 1 Ralph target")
