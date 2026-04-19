"""Capability manifest + JSON-schema validation.

Spec: security-v1.1 §8 (manifest format), §8.1 (MCP risk tiers),
§13.4 (revalidate contract).

Loaded at broker startup. Refuses to start on any manifest error.

Per-capability checks (§8 "Validation at broker startup"):
  - `executor`, `param_schema`, `idempotency_date_from` present.
  - `risk_level` ∈ {low, medium, high}.
  - Every `medium`/`high` capability must declare either:
        revalidate: {handler: <name>, arguments: [<field>, ...]}
      or
        revalidate: {not_applicable: <reason>}
    with reason ∈ {stateless_write, idempotent_create, no_external_state}.
  - `param_schema` file resolves and parses as valid JSON Schema.
  - Every field in queue-file output is annotated with provenance
    (§12.5) — validator refuses manifests that produce unannotated
    fields.

Also owns parsing `mcp-tools.yaml` — the Phase 1 replacement for the
hardcoded MCP allowlist in `hooks/capability-guard.sh` (§14.1 TODO).

Phase 1 Ralph target — see `broker/ralph-prompts/validator.md`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Capability:
    name: str
    executor_type: str
    executor_target: str
    param_schema: dict[str, Any]
    params_exact_match_required: bool
    derived_fields_allowed: tuple[str, ...]
    risk_level: str
    revalidate: dict[str, Any]
    idempotency_date_from: str
    approval_window_minutes: int
    execution_window_minutes: int


class ManifestError(Exception):
    """Raised on any manifest-validation failure. Broker must refuse to
    start when this is raised."""


def load_capabilities(path: str) -> dict[str, Capability]:
    """Parse capabilities.yaml + referenced JSON Schemas. Raises
    ManifestError on any problem (§8 "Broker refuses to start")."""
    raise NotImplementedError("load_capabilities: Phase 1 Ralph target")


def load_mcp_tools(path: str) -> dict[str, str]:
    """Parse mcp-tools.yaml. Returns {tool_name: risk_level} per §8.1.
    Raises ManifestError on any unknown risk level or duplicate tool."""
    raise NotImplementedError("load_mcp_tools: Phase 1 Ralph target")


def validate_params(capability: Capability, params: Any) -> None:
    """Validate `params` against `capability.param_schema`. Raises
    ManifestError (subclass) with a structured reason on failure."""
    raise NotImplementedError("validate_params: Phase 1 Ralph target")
