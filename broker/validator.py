"""Capability manifest + JSON-schema validation.

Spec: security-v1.1 §8 (manifest format), §8.1 (MCP risk tiers),
§13.4 (revalidate contract), §14.1 (Playwright is always blocked).

Loaded at broker startup. Refuses to start on any manifest error
(§8: "Broker refuses to start" on missing / invalid fields).

Per-capability checks (§8):
  - `executor`, `param_schema`, `idempotency_date_from` present.
  - `risk_level` ∈ {low, medium, high}.
  - Every `medium`/`high` capability must declare `revalidate` as one of:
      revalidate: {handler: <name>, arguments: [<field>, ...]}
      revalidate: {not_applicable: <reason>}
    with reason ∈ {stateless_write, idempotent_create, no_external_state}.
  - `param_schema: {$ref: ...}` resolves to a local file; parses as
    valid JSON Schema Draft-07.

mcp-tools.yaml (§8.1):
  - Returns {tool_name: risk_level}. Valid risks: low / medium / high / blocked.
  - Duplicate names raise.
  - `mcp__plugin_playwright_*` patterns must either be absent or `blocked`.
    Non-blocked Playwright is a §14.1 violation — refuse to start.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError


VALID_RISK_LEVELS = frozenset({"low", "medium", "high"})
VALID_MCP_RISKS = frozenset({"low", "medium", "high", "blocked"})
VALID_EXECUTOR_TYPES = frozenset({"subprocess", "mcp_tool"})
VALID_NOT_APPLICABLE_REASONS = frozenset({
    "stateless_write",
    "idempotent_create",
    "no_external_state",
})
VALID_CREDS_DELIVERY = frozenset({"fd3"})
CREDS_ENTRY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Required top-level capability fields (§8 format).
REQUIRED_CAPABILITY_FIELDS = (
    "name",
    "executor",
    "param_schema",
    "risk_level",
    "idempotency_date_from",
    "approval_window_minutes",
    "execution_window_minutes",
)


class ManifestError(Exception):
    """Raised on any manifest-validation failure. Broker must refuse to
    start when this is raised."""


class ParamValidationError(ManifestError):
    """Raised when params do not satisfy a capability's JSON Schema.
    Separate subclass so request-time failures are distinguishable from
    startup-time manifest bugs."""


@dataclass(frozen=True)
class CredsBlock:
    """§4 creds-injection opt-in. Presence of a CredsBlock on a
    Capability is the declaration that the capability requires
    credentials at spawn time. See security-v1.1 §17 Phase 2 age vault
    and Piece C design doc §3 for delivery semantics."""
    delivery: str
    entry: str


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
    creds: CredsBlock | None = None


# ---- capabilities.yaml --------------------------------------------------


def _require(mapping: dict[str, Any], key: str, capability_name: str) -> Any:
    if key not in mapping:
        raise ManifestError(
            f"capability {capability_name!r}: missing required field {key!r}"
        )
    return mapping[key]


def _validate_creds(creds_raw: Any, capability_name: str) -> CredsBlock | None:
    """§4.2 creds-block validation. Returns None if absent; raises
    ManifestError on any structural issue."""
    if creds_raw is None:
        return None
    if not isinstance(creds_raw, dict):
        raise ManifestError(
            f"capability {capability_name!r}: creds must be a mapping, "
            f"got {type(creds_raw).__name__}"
        )
    if "delivery" not in creds_raw:
        raise ManifestError(
            f"capability {capability_name!r}: creds.delivery is required"
        )
    if "entry" not in creds_raw:
        raise ManifestError(
            f"capability {capability_name!r}: creds.entry is required"
        )
    delivery = creds_raw["delivery"]
    if not isinstance(delivery, str) or delivery not in VALID_CREDS_DELIVERY:
        raise ManifestError(
            f"capability {capability_name!r}: creds.delivery must be one "
            f"of {sorted(VALID_CREDS_DELIVERY)}, got {delivery!r}"
        )
    entry = creds_raw["entry"]
    if not isinstance(entry, str) or not CREDS_ENTRY_RE.fullmatch(entry):
        raise ManifestError(
            f"capability {capability_name!r}: creds.entry must match "
            f"{CREDS_ENTRY_RE.pattern!r}, got {entry!r}"
        )
    unknown = set(creds_raw.keys()) - {"delivery", "entry"}
    if unknown:
        raise ManifestError(
            f"capability {capability_name!r}: unknown creds keys: "
            f"{sorted(unknown)}"
        )
    return CredsBlock(delivery=delivery, entry=entry)


def _validate_revalidate(reval: Any, capability_name: str, risk_level: str) -> None:
    """§8 + §13.4 — medium/high capabilities must declare revalidate."""
    if risk_level not in {"medium", "high"}:
        return
    if not isinstance(reval, dict):
        raise ManifestError(
            f"capability {capability_name!r}: medium/high requires "
            f"`revalidate` (dict); got {type(reval).__name__}"
        )
    has_handler = "handler" in reval
    has_na = "not_applicable" in reval
    if has_handler == has_na:  # XOR: exactly one must be set
        raise ManifestError(
            f"capability {capability_name!r}: revalidate must declare "
            f"exactly one of `handler` or `not_applicable`"
        )
    if has_handler:
        handler = reval["handler"]
        if not isinstance(handler, str) or not handler:
            raise ManifestError(
                f"capability {capability_name!r}: revalidate.handler "
                f"must be a non-empty string"
            )
        arguments = reval.get("arguments", [])
        if not isinstance(arguments, list):
            raise ManifestError(
                f"capability {capability_name!r}: revalidate.arguments "
                f"must be a list"
            )
    else:
        reason = reval["not_applicable"]
        if reason not in VALID_NOT_APPLICABLE_REASONS:
            raise ManifestError(
                f"capability {capability_name!r}: revalidate.not_applicable "
                f"must be one of {sorted(VALID_NOT_APPLICABLE_REASONS)}, "
                f"got {reason!r}"
            )


def _validate_executor(executor: Any, capability_name: str) -> tuple[str, str]:
    """Return (executor_type, executor_target)."""
    if not isinstance(executor, dict):
        raise ManifestError(
            f"capability {capability_name!r}: executor must be a mapping"
        )
    etype = executor.get("type")
    if etype not in VALID_EXECUTOR_TYPES:
        raise ManifestError(
            f"capability {capability_name!r}: executor.type must be one of "
            f"{sorted(VALID_EXECUTOR_TYPES)}, got {etype!r}"
        )
    if etype == "subprocess":
        binary = executor.get("binary")
        if not isinstance(binary, str) or not binary:
            raise ManifestError(
                f"capability {capability_name!r}: executor.binary must be "
                f"a non-empty string for subprocess executors"
            )
        # timeout_seconds is required for subprocess executors.
        timeout = executor.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise ManifestError(
                f"capability {capability_name!r}: executor.timeout_seconds "
                f"must be a positive integer"
            )
        return "subprocess", binary
    tool = executor.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ManifestError(
            f"capability {capability_name!r}: executor.tool must be "
            f"a non-empty string for mcp_tool executors"
        )
    return "mcp_tool", tool


def _resolve_param_schema(
    schema_field: Any, manifest_dir: Path, capability_name: str
) -> dict[str, Any]:
    """Resolve `param_schema` which per §8 may be `{$ref: "./path.json"}`
    or an inline JSON Schema dict."""
    if not isinstance(schema_field, dict):
        raise ManifestError(
            f"capability {capability_name!r}: param_schema must be a mapping"
        )
    if "$ref" in schema_field:
        ref = schema_field["$ref"]
        if not isinstance(ref, str):
            raise ManifestError(
                f"capability {capability_name!r}: param_schema.$ref must be a string"
            )
        ref_path = (manifest_dir / ref).resolve()
        if not ref_path.exists():
            raise ManifestError(
                f"capability {capability_name!r}: param_schema.$ref "
                f"{ref!r} resolves to {ref_path} which does not exist"
            )
        try:
            import json
            schema: dict[str, Any] = json.loads(
                ref_path.read_text(encoding="utf-8")
            )
        except Exception as e:
            raise ManifestError(
                f"capability {capability_name!r}: param_schema.$ref "
                f"{ref!r} is not valid JSON: {e}"
            ) from e
    else:
        schema = schema_field
    # JSON Schema Draft-07 meta-validate.
    try:
        Draft7Validator.check_schema(schema)
    except Exception as e:
        raise ManifestError(
            f"capability {capability_name!r}: param_schema is not a valid "
            f"JSON Schema Draft-07: {e}"
        ) from e
    return schema


def _parse_one_capability(
    raw: Any, manifest_dir: Path
) -> Capability:
    if not isinstance(raw, dict):
        raise ManifestError(
            f"capability entry must be a mapping, got {type(raw).__name__}"
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(
            "capability entry missing `name` (non-empty string required)"
        )

    for field in REQUIRED_CAPABILITY_FIELDS:
        _require(raw, field, name)

    risk_level = raw["risk_level"]
    if risk_level not in VALID_RISK_LEVELS:
        raise ManifestError(
            f"capability {name!r}: risk_level must be one of "
            f"{sorted(VALID_RISK_LEVELS)}, got {risk_level!r}"
        )

    exec_type, exec_target = _validate_executor(raw["executor"], name)
    param_schema = _resolve_param_schema(raw["param_schema"], manifest_dir, name)

    revalidate = raw.get("revalidate", {})
    _validate_revalidate(revalidate, name, risk_level)

    idempotency_date_from = raw["idempotency_date_from"]
    if not isinstance(idempotency_date_from, str) or not idempotency_date_from:
        raise ManifestError(
            f"capability {name!r}: idempotency_date_from must be a "
            f"non-empty string"
        )

    for window_field in ("approval_window_minutes", "execution_window_minutes"):
        val = raw[window_field]
        if not isinstance(val, int) or val <= 0:
            raise ManifestError(
                f"capability {name!r}: {window_field} must be a positive integer"
            )

    params_exact_match = raw.get("params_exact_match_required", True)
    if not isinstance(params_exact_match, bool):
        raise ManifestError(
            f"capability {name!r}: params_exact_match_required must be a bool"
        )

    derived_fields_allowed_raw = raw.get("derived_fields_allowed", [])
    if not isinstance(derived_fields_allowed_raw, list):
        raise ManifestError(
            f"capability {name!r}: derived_fields_allowed must be a list"
        )
    if not all(isinstance(f, str) for f in derived_fields_allowed_raw):
        raise ManifestError(
            f"capability {name!r}: derived_fields_allowed entries must be strings"
        )

    if "creds" in raw:
        creds_raw = raw["creds"]
        if creds_raw is None:
            raise ManifestError(
                f"capability {name!r}: creds key present but value is null"
            )
        creds_block = _validate_creds(creds_raw, name)
    else:
        creds_block = None

    return Capability(
        name=name,
        executor_type=exec_type,
        executor_target=exec_target,
        param_schema=param_schema,
        params_exact_match_required=params_exact_match,
        derived_fields_allowed=tuple(derived_fields_allowed_raw),
        risk_level=risk_level,
        revalidate=dict(revalidate) if isinstance(revalidate, dict) else {},
        idempotency_date_from=idempotency_date_from,
        approval_window_minutes=raw["approval_window_minutes"],
        execution_window_minutes=raw["execution_window_minutes"],
        creds=creds_block,
    )


def load_capabilities(path: str) -> dict[str, Capability]:
    """Parse capabilities.yaml + referenced JSON Schemas. Raises
    ManifestError on any problem (§8 "Broker refuses to start")."""
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ManifestError(f"capabilities manifest not found: {path}")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ManifestError(f"capabilities manifest YAML parse error: {e}") from e

    if not isinstance(data, dict) or "capabilities" not in data:
        raise ManifestError(
            "capabilities manifest must have a top-level `capabilities:` list"
        )
    entries = data["capabilities"]
    if not isinstance(entries, list):
        raise ManifestError(
            "capabilities manifest: `capabilities` must be a list"
        )

    result: dict[str, Capability] = {}
    manifest_dir = manifest_path.parent
    for raw in entries:
        cap = _parse_one_capability(raw, manifest_dir)
        if cap.name in result:
            raise ManifestError(f"duplicate capability name: {cap.name!r}")
        result[cap.name] = cap
    return result


# ---- mcp-tools.yaml -----------------------------------------------------


def load_mcp_tools(path: str) -> dict[str, str]:
    """Parse mcp-tools.yaml. Returns {tool_name: risk_level} per §8.1.
    Raises ManifestError on any unknown risk level, duplicate tool, or a
    non-blocked `mcp__plugin_playwright_*` entry (§14.1)."""
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ManifestError(f"mcp-tools manifest not found: {path}")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ManifestError(f"mcp-tools manifest YAML parse error: {e}") from e

    if not isinstance(data, dict) or "tools" not in data:
        raise ManifestError(
            "mcp-tools manifest must have a top-level `tools:` mapping"
        )
    tools = data["tools"]
    if not isinstance(tools, dict):
        raise ManifestError("mcp-tools manifest: `tools` must be a mapping")

    result: dict[str, str] = {}
    for name, risk in tools.items():
        if not isinstance(name, str) or not name:
            raise ManifestError(
                f"mcp-tools: tool name must be a non-empty string, got {name!r}"
            )
        if not isinstance(risk, str) or risk not in VALID_MCP_RISKS:
            raise ManifestError(
                f"mcp-tools: tool {name!r} has invalid risk {risk!r}; "
                f"must be one of {sorted(VALID_MCP_RISKS)}"
            )
        # §14.1: Playwright is permanently blocked. If the manifest
        # accidentally lets it through, refuse to start.
        if name.startswith("mcp__plugin_playwright_") and risk != "blocked":
            raise ManifestError(
                f"mcp-tools: Playwright tool {name!r} must be `blocked` "
                f"per §14.1; got {risk!r}"
            )
        if name in result:
            raise ManifestError(f"mcp-tools: duplicate tool name {name!r}")
        result[name] = risk
    return result


# ---- params validation --------------------------------------------------


def validate_params(capability: Capability, params: Any) -> None:
    """Validate `params` against `capability.param_schema`. Raises
    ParamValidationError with a structured path-aware reason on failure."""
    validator = Draft7Validator(capability.param_schema)
    errors = sorted(validator.iter_errors(params), key=lambda e: e.path)
    if errors:
        first: ValidationError = errors[0]
        path_parts = [str(p) for p in first.absolute_path]
        path_str = "/".join(path_parts) if path_parts else "<root>"
        raise ParamValidationError(
            f"capability {capability.name!r}: params invalid at {path_str}: "
            f"{first.message}"
        )
