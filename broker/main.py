"""CLI dispatcher — `donna-broker <mode>` with JSON on stdin/stdout.

Spec: security-v1.1 §13.1 (modes + pause scope), §13.5 (hook contracts),
§13.6 (pending-summary surfacing), §10 (failure semantics matrix),
§7.2 (idempotency rules), §7.7 (context_reason sanitisation),
§12.1 (queue file layout).

Invocation:
    echo '{"capability":"...","params":{...}}' | donna-broker request

Error envelope: {status, error_code, message}. Never stack traces.
Every response carries `pending_count`; callers that haven't ack'd
recently also receive `pending_summary` (§13.6).

Config is read from env; paths default to /Users/donna-broker/... but
every knob is overridable for testing. The broker never falls back to
undefined behaviour: missing files or manifests become structured
errors, not crashes.
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from broker import audit as audit_mod
from broker import canonicalize
from broker import creds as creds_mod
from broker import executor
from broker import grants_db
from broker import pack_format
from broker import policy
from broker import requests_db as db
from broker import resolver
from broker import validator
from broker import vault_health


MODES = frozenset({
    "request", "policy-check", "execute", "cancel", "reconcile",
    "status", "status-by-code", "list-pending", "list-recent",
    "audit-result", "rotate-hmac", "verify-audit", "verify-manifests",
    "verify-vault", "list-packs",
    # broker-standing-grants §7: scoped approve-once autonomy.
    "grant-create", "grant-list", "grant-revoke",
    # Connected Sites (docs/connected-sites-broker-handoff.md): the app
    # hands a site login straight to the vault / probes a stored login.
    "store-credential", "site-check",
    # In-app approval (docs/inapp-approval-broker-handoff.md, master-design
    # Option B): the app's human-tap records a proof-of-human approval.
    "app-approve",
})

# site-check probe binaries, keyed by site slug (= vault entry name).
# A fixed allowlist — site input never becomes a path. Adding a site
# means adding its probe executor here and deploying it.
SITE_PROBES: dict[str, str] = {
    "everyone_active": (
        "/Users/donna-broker/broker/executors/everyone_active_site_check"
    ),
}
SITE_PROBE_TIMEOUT_SECONDS = 90.0

# broker-standing-grants §3.3 grant scope bounds.
GRANT_DEFAULT_EXPIRES_DAYS = 90
GRANT_MAX_EXPIRES_DAYS = 365


# ---- config -------------------------------------------------------------


def _config_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Read config paths. Env wins; defaults target the donna-broker home."""
    e = env if env is not None else os.environ
    home = e.get("DONNA_BROKER_HOME", "/Users/donna-broker/.config/donna")
    return {
        "db_path": e.get("DONNA_BROKER_DB", f"{home}/requests.db"),
        "audit_dir": e.get(
            "DONNA_BROKER_AUDIT_DIR", "/Users/donna-broker/audit"
        ),
        "hmac_key_path": e.get("DONNA_BROKER_HMAC_KEY", f"{home}/hmac.key"),
        "capabilities_path": e.get(
            "DONNA_BROKER_CAPABILITIES", f"{home}/capabilities.yaml"
        ),
        "mcp_tools_path": e.get(
            "DONNA_BROKER_MCP_TOOLS", f"{home}/mcp-tools.yaml"
        ),
        "queue_dir": e.get(
            "DONNA_BROKER_QUEUE_DIR", f"{home}/approval-queue"
        ),
        "responses_dir": e.get(
            "DONNA_BROKER_RESPONSES_DIR", f"{home}/approval-responses"
        ),
        "creds_dir": e.get(
            "DONNA_CREDS_DIR",
            "/Users/donna-broker/.config/donna/creds",
        ),
        "identity_path": e.get(
            "DONNA_IDENTITY_PATH",
            "/Users/donna-broker/.config/donna/creds/identity.age",
        ),
        "age_binary": e.get("DONNA_AGE_BINARY", "age"),
        "age_keygen_binary": e.get("DONNA_AGE_KEYGEN_BINARY", "age-keygen"),
    }


# ---- error handling -----------------------------------------------------


class BrokerError(Exception):
    """Structured error surfaced to the caller as JSON."""

    def __init__(self, error_code: str, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message


def _error_response(err: BrokerError) -> dict[str, Any]:
    return {
        "status": err.status,
        "error_code": err.error_code,
        "message": err.message,
    }


# ---- helpers: env-level bootstrapping -----------------------------------


def _load_hmac_key(path: str) -> bytes:
    p = Path(path)
    if not p.exists():
        raise BrokerError(
            "hmac_key_missing", f"HMAC key not found at {path}",
        )
    key = p.read_bytes()
    if len(key) < 16:
        raise BrokerError(
            "hmac_key_too_short",
            f"HMAC key at {path} is {len(key)} bytes; spec requires 32",
        )
    return key


def _open_or_raise(path: str) -> sqlite3.Connection:
    try:
        return db.open_db(path)
    except Exception as e:
        raise BrokerError("db_open_failed", str(e)) from e


def _load_stdin_json(stdin: Any) -> dict[str, Any]:
    """Read stdin to EOF and parse. Empty stdin is an empty object."""
    raw = stdin.read()
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BrokerError("invalid_json", f"stdin not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise BrokerError(
            "invalid_payload", "stdin must be a JSON object"
        )
    return data


def _append_pending_summary(
    conn: sqlite3.Connection, response: dict[str, Any]
) -> dict[str, Any]:
    """Always add pending_count; add pending_summary when there are any
    approved-not-executed rows. §13.6."""
    count = db.count_pending(conn)
    response["pending_count"] = count
    if count > 0:
        rows = conn.execute(
            "SELECT approval_code, capability, resolved_summary, "
            "execution_expires_at FROM requests WHERE state = 'approved' "
            "ORDER BY approved_at ASC LIMIT 20"
        ).fetchall()
        now_ms = int(time.time() * 1000)
        response["pending_summary"] = [
            {
                "code": r["approval_code"],
                "capability": r["capability"],
                "resolved_summary": r["resolved_summary"],
                "expires_in_seconds": max(
                    0, (int(r["execution_expires_at"]) - now_ms) // 1000,
                ),
            }
            for r in rows
        ]
    return response


# ---- capability / manifest loading --------------------------------------


def _require_capability(caps: dict[str, validator.Capability], name: str) -> validator.Capability:
    if name not in caps:
        raise BrokerError(
            "unknown_capability", f"capability {name!r} not in manifest",
        )
    return caps[name]


def _derive_date_component(capability: validator.Capability, params: dict[str, Any], now_ms: int) -> str:
    """Per §7.2. `idempotency_date_from` is either `created_utc` or
    `params.<field>` referencing a date-shaped param."""
    src = capability.idempotency_date_from
    if src == "created_utc":
        return time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    if src.startswith("params."):
        field = src[len("params."):]
        val = params.get(field)
        if not isinstance(val, str):
            raise BrokerError(
                "invalid_input",
                f"idempotency_date_from=params.{field} but params.{field} "
                f"is {type(val).__name__}",
            )
        return val
    raise BrokerError(
        "manifest_error",
        f"unsupported idempotency_date_from: {src!r}",
    )


# ---- mode handlers ------------------------------------------------------


def _handle_policy_check(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Fast hook path (§9.1). No network, no subprocess, no broker state
    changes. For MCP tools, consults mcp-tools.yaml risk tier; for
    capabilities, returns a summary the hook can show on a block.

    Executing-row bypass (§8 binding): if a matching row is already in
    `executing` state for a capability whose `executor.type == mcp_tool`
    and `executor.tool == tool_name`, and the canonical params hash
    matches, the hook is allowed. This is the only path by which an
    approved medium/high-risk MCP tool actually fires — without it the
    broker accepts the `execute` call and transitions state but the
    subsequent MCP tool call is still blocked at the PreToolUse hook,
    stranding the draft.
    """
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        raise BrokerError("invalid_input", "tool_name required")

    mcp_tools: dict[str, str] = ctx["mcp_tools"]
    if tool_name in mcp_tools:
        risk = mcp_tools[tool_name]
        if risk == "low":
            return {
                "status": "ok",
                "decision": "allow",
                "tool_name": tool_name,
                "risk_level": "low",
            }
        if risk == "blocked":
            return {
                "status": "ok",
                "decision": "deny",
                "tool_name": tool_name,
                "reason": f"{tool_name} blocked by §8.1 / §14.1",
            }
        # medium / high fall through to the executing-row bypass first,
        # then the block-with-summary pattern below.

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise BrokerError("invalid_input", "params must be an object")

    # Executing-row bypass. Find the capability whose mcp_tool executor
    # targets this tool_name. Since capability names are unique and the
    # executor mapping is one-to-one for mcp_tool type, a single loop
    # over capabilities is adequate and runs sub-ms at manifest sizes
    # we care about (≪ 100 capabilities). Errors are swallowed to
    # `None` so a malformed capability never fails the hot path —
    # worst case we fall through to the normal block behaviour.
    capabilities: dict[str, Any] = ctx.get("capabilities") or {}
    matching_capability: Optional[str] = None
    for cap_name, cap in capabilities.items():
        if getattr(cap, "executor_type", None) != "mcp_tool":
            continue
        if getattr(cap, "executor_target", None) != tool_name:
            continue
        matching_capability = cap_name
        break

    if matching_capability is not None:
        cap_schema = getattr(
            capabilities[matching_capability], "param_schema", None
        )
        normalised = _normalise_hook_params(params, cap_schema)
        try:
            params_hash_val = canonicalize.params_hash(normalised)
        except Exception:
            params_hash_val = None
        if params_hash_val is not None:
            row = ctx["conn"].execute(
                "SELECT request_id FROM requests "
                "WHERE state = 'executing' "
                "AND capability = ? "
                "AND params_hash = ? "
                "LIMIT 1",
                (matching_capability, params_hash_val),
            ).fetchone()
            if row is not None:
                return {
                    "status": "ok",
                    "decision": "allow",
                    "tool_name": tool_name,
                    "risk_level": mcp_tools.get(tool_name, "medium"),
                    "reason": (
                        f"executing row {row[0]} authorises this call"
                    ),
                    "request_id": row[0],
                }

    summary = resolver.policy_check_mode(tool_name, params)
    response: dict[str, Any] = {
        "status": "ok",
        "decision": "block",
        "tool_name": tool_name,
        "reason": "medium/high-risk tool requires approval via broker request",
        "summary": summary,
    }

    # Diagnostic: if an executing row for this capability exists but
    # the params_hash didn't match, return the diff so Donna sees
    # exactly which keys/values differ from the approved row. Without
    # this the hook just says "requires approval" and the mismatch is
    # invisible — she re-requests, re-approves, still blocks, loop.
    if matching_capability is not None:
        try:
            diag_row = ctx["conn"].execute(
                "SELECT request_id, params_json FROM requests "
                "WHERE state = 'executing' AND capability = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (matching_capability,),
            ).fetchone()
        except Exception:
            diag_row = None
        if diag_row is not None:
            try:
                approved_params = json.loads(diag_row[1])
            except Exception:
                approved_params = None
            if isinstance(approved_params, dict):
                cap_schema = getattr(
                    capabilities[matching_capability], "param_schema", None
                )
                normalised_recv = _normalise_hook_params(params, cap_schema)
                diff = _params_diff(approved_params, normalised_recv)
                if diff:
                    response["params_mismatch"] = {
                        "request_id": diag_row[0],
                        "diff": diff,
                    }
    return response


def _normalise_hook_params(
    params: dict[str, Any], cap_schema: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Un-stringify fields that the capability's JSON Schema declares as
    `array` or `object` but which arrived as JSON-encoded strings.

    Observed behaviour (Claude Code MCP hook envelope, 2.1.81): when a
    tool-use argument is a structured value like `{"to": ["x@y"]}`, the
    hook's `tool_input.to` is sometimes delivered as the *string*
    `'["x@y"]'` rather than a native list. Because `params_hash` is an
    exact canonical-JSON comparison, a stringified array will never
    match an approved native-array row and the executing-row bypass
    always misses.

    The normalisation is narrow and schema-gated: only fields the
    capability explicitly declares as `array` or `object` are
    considered, and the parsed value must match that declared type.
    Anything else is passed through untouched so we never silently
    coerce an attacker-controlled string into something the capability
    did not expect.
    """
    if not isinstance(cap_schema, dict):
        return params
    props = cap_schema.get("properties")
    if not isinstance(props, dict):
        return params
    out: dict[str, Any] = {}
    for key, value in params.items():
        prop = props.get(key)
        expected_type = (
            prop.get("type") if isinstance(prop, dict) else None
        )
        if expected_type in {"array", "object"} and isinstance(value, str):
            stripped = value.strip()
            if (expected_type == "array" and stripped.startswith("[")) or (
                expected_type == "object" and stripped.startswith("{")
            ):
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    parsed = None
                if expected_type == "array" and isinstance(parsed, list):
                    out[key] = parsed
                    continue
                if expected_type == "object" and isinstance(parsed, dict):
                    out[key] = parsed
                    continue
        out[key] = value
    return out


def _params_diff(
    approved: dict[str, Any], received: dict[str, Any]
) -> dict[str, Any]:
    """Shallow diff of the two param objects. Reports added, removed,
    and changed keys without dumping full values of large strings."""
    def summarise(v: Any) -> Any:
        if isinstance(v, str) and len(v) > 80:
            return f"{v[:77]!r}…(len={len(v)})"
        if isinstance(v, list):
            return f"list(len={len(v)}): {v[:3]}{'…' if len(v) > 3 else ''}"
        return v

    diff: dict[str, Any] = {}
    for key in sorted(set(approved) | set(received)):
        if key not in received:
            diff[key] = {"removed": summarise(approved[key])}
        elif key not in approved:
            diff[key] = {"added": summarise(received[key])}
        elif approved[key] != received[key]:
            diff[key] = {
                "approved": summarise(approved[key]),
                "received": summarise(received[key]),
            }
    return diff


def _handle_status(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rid = payload.get("request_id")
    if not isinstance(rid, str) or not rid:
        raise BrokerError("invalid_input", "request_id required")
    r = db.get_request(ctx["conn"], rid)
    if r is None:
        raise BrokerError(
            "not_found", f"no request with id {rid!r}", status="not_found",
        )
    return {
        "status": "ok",
        "request_id": r.request_id,
        "capability": r.capability,
        "state": r.state,
        "approval_code": r.approval_code,
        "resolved_summary": r.resolved_summary,
        "created_at": r.created_at,
        "approval_expires_at": r.approval_expires_at,
        "execution_expires_at": r.execution_expires_at,
        "approved_at": r.approved_at,
        "executed_at": r.executed_at,
        "error_code": r.error_code,
    }


def _handle_status_by_code(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("approval_code")
    if not isinstance(code, str) or not code:
        raise BrokerError("invalid_input", "approval_code required")
    r = db.get_by_approval_code(ctx["conn"], code)
    if r is None:
        raise BrokerError(
            "not_found", f"no active request with code {code!r}",
            status="not_found",
        )
    payload_out = _handle_status({"request_id": r.request_id}, ctx)
    return payload_out


def _handle_list_pending(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    conn = ctx["conn"]
    rows = conn.execute(
        "SELECT request_id, capability, approval_code, state, resolved_summary, "
        "approval_expires_at, execution_expires_at, approved_at "
        "FROM requests "
        "WHERE state IN ('pending_approval','approved','executing') "
        "ORDER BY created_at ASC"
    ).fetchall()
    return {
        "status": "ok",
        "requests": [dict(r) for r in rows],
    }


def _handle_list_recent(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    n = int(payload.get("limit", 20))
    n = max(1, min(200, n))
    rows = ctx["conn"].execute(
        "SELECT request_id, capability, approval_code, state, "
        "resolved_summary, error_code, created_at, executed_at "
        "FROM requests "
        "WHERE state IN ('succeeded','failed','denied','expired',"
        "'cancelled','integrity_failed') "
        "ORDER BY created_at DESC LIMIT ?",
        (n,),
    ).fetchall()
    return {
        "status": "ok",
        "requests": [dict(r) for r in rows],
    }


def _handle_request(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Create a new capability request. Heart of the broker flow."""
    capability_name = payload.get("capability")
    params = payload.get("params") or {}
    raw_context_reason = payload.get("context_reason") or ""

    if not isinstance(capability_name, str):
        raise BrokerError("invalid_input", "capability required")
    if not isinstance(params, dict):
        raise BrokerError("invalid_input", "params must be an object")
    if not isinstance(raw_context_reason, str):
        raise BrokerError("invalid_input", "context_reason must be a string")

    cap = _require_capability(ctx["capabilities"], capability_name)

    try:
        validator.validate_params(cap, params)
    except validator.ParamValidationError as e:
        raise BrokerError("invalid_params", str(e)) from e

    # §7.7 sanitise context_reason.
    try:
        context_reason, redactions = policy.sanitise_context_reason(raw_context_reason)
    except policy.ContextReasonTooLong as e:
        raise BrokerError("invalid_input", str(e)) from e

    canonical = canonicalize.canonicalize(params)
    params_hash = canonicalize.params_hash(params)
    now_ms = int(time.time() * 1000)
    date_component = _derive_date_component(cap, params, now_ms)
    idem_key = policy.idempotency_key(capability_name, canonical, date_component)

    conn = ctx["conn"]

    # §7.2 idempotency rules — return the existing row if one exists.
    existing = db.get_by_idempotency_key(conn, idem_key)
    if existing is not None:
        # Non-terminal or succeeded → return existing state.
        return {
            "status": "existing",
            "request_id": existing.request_id,
            "approval_code": existing.approval_code,
            "state": existing.state,
            "resolved_summary": existing.resolved_summary,
        }

    # §7.4 cooldown after denial.
    cooldown = policy.cooldown_remaining_seconds(conn, idem_key, now_ms=now_ms)
    if cooldown > 0:
        return {
            "status": "cooldown",
            "retry_after_seconds": cooldown,
            "reason": (
                f"Denied {policy.DEFAULT_COOLDOWN_MINUTES * 60 - cooldown} "
                f"sec ago. Re-requestable in {cooldown}s, or Graham can "
                f"/override."
            ),
        }

    # broker-standing-grants §6: consult active standing grants BEFORE
    # the risk-tier fallthrough. A matching, in-rate, MAC-verified grant
    # auto-authorises the request (skips the per-run approval). The check
    # is pure (now_ms passed in, grant store read locally). grant.create
    # is never matched here (no self-escalation) — enforced inside
    # check_standing_grants.
    grants_db.ensure_grant_tables(conn)
    grant_decision = policy.check_standing_grants(
        conn, capability_name, params, now_ms, ctx["hmac_key"],
    )
    if grant_decision is not None:
        return _auto_execute_via_grant(
            ctx, cap, capability_name, params, params_hash, idem_key,
            context_reason, now_ms, grant_decision,
        )

    # Build the row.
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    risk_level = cap.risk_level
    approval_expires_at = now_ms + cap.approval_window_minutes * 60 * 1000
    approval_code = None
    state = "pending_approval"
    if risk_level == "low":
        # Low-risk capabilities historically went auto-approved; per §5
        # auto_approved is reserved for future use, so low-risk
        # capabilities via `request` flow still create a pending row.
        # (The MCP read hot path uses policy-check directly, no row.)
        pass
    approval_code = policy.generate_approval_code()
    hmac_value = policy.compute_creation_hmac(
        key=ctx["hmac_key"],
        request_id=request_id,
        capability=capability_name,
        params_hash=params_hash,
        idempotency_key_=idem_key,
        risk_level=risk_level,
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
    )

    resolved = resolver.policy_check_mode(capability_name, params)
    resolved_summary = resolved.get("resolved_summary", capability_name)

    row = db.Request(
        request_id=request_id,
        capability=capability_name,
        params_json=json.dumps(params, sort_keys=True),
        params_hash=params_hash,
        idempotency_key=idem_key,
        resolved_summary=resolved_summary,
        context_reason=context_reason,
        risk_level=risk_level,
        state=state,
        approval_code=approval_code,
        approval_hmac=hmac_value,
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
        execution_expires_at=None,
        approved_at=None,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    try:
        db.insert_request(conn, row)
    except sqlite3.IntegrityError as e:
        # Partial-unique-index race — another call inserted first; return
        # what's now there rather than raising.
        existing = db.get_by_idempotency_key(conn, idem_key)
        if existing is not None:
            return {
                "status": "existing",
                "request_id": existing.request_id,
                "approval_code": existing.approval_code,
                "state": existing.state,
                "resolved_summary": existing.resolved_summary,
            }
        raise BrokerError("db_insert_failed", str(e)) from e

    # Audit the creation + pending transition.
    audit_dir = ctx["audit_dir"]
    audit_mod.write_event(audit_dir, {
        "event": "request_created",
        "request_id": request_id,
        "capability": capability_name,
        "params_hash": params_hash,
        "risk_level": risk_level,
    })
    audit_mod.write_event(audit_dir, {
        "event": "request_pending",
        "request_id": request_id,
        "resolved_summary": resolved_summary,
    })
    if redactions:
        audit_mod.write_event(audit_dir, {
            "event": "audit.context_reason_redacted",
            "request_id": request_id,
            "original_length": len(raw_context_reason),
            "redaction_types": redactions,
            "context_reason_original": raw_context_reason,
        })

    # Write queue file for Telegram server (§12.1).
    queue_file_path = _write_queue_file(
        ctx["queue_dir"], request_id, approval_code, approval_expires_at,
        capability_name, risk_level, resolved_summary, context_reason,
    )

    return {
        "status": "approval_required",
        "request_id": request_id,
        "code": approval_code,
        "summary": resolved_summary,
        "risk_level": risk_level,
        "approval_expires_at": approval_expires_at,
        "queue_file": queue_file_path,
    }


def _auto_execute_via_grant(
    ctx: dict[str, Any],
    cap: validator.Capability,
    capability_name: str,
    params: dict[str, Any],
    params_hash: str,
    idem_key: str,
    context_reason: Optional[str],
    now_ms: int,
    grant_decision: dict[str, Any],
) -> dict[str, Any]:
    """broker-standing-grants §6: a standing grant matched this request.

    Skip the per-run approval entirely. We still create a tracked
    `requests` row (full audit + state machine), drive it
    pending_approval → approved → executing through the normal HMAC-bound
    path, then dispatch the executor. For mcp_tool capabilities this
    returns the executor handoff (status `executing`); the PostToolUse
    hook closes the row exactly as for a human-approved send.

    The auto-allow is audited as `policy.allow.standing_grant` with the
    matching grant_id (§9).
    """
    conn = ctx["conn"]
    audit_dir = ctx["audit_dir"]
    grant_id = grant_decision["grant_id"]

    request_id = f"req-{uuid.uuid4().hex[:12]}"
    risk_level = cap.risk_level
    approval_expires_at = now_ms + cap.approval_window_minutes * 60 * 1000
    exec_expires = now_ms + cap.execution_window_minutes * 60 * 1000
    approval_code = policy.generate_approval_code()

    creation_hmac = policy.compute_creation_hmac(
        key=ctx["hmac_key"],
        request_id=request_id,
        capability=capability_name,
        params_hash=params_hash,
        idempotency_key_=idem_key,
        risk_level=risk_level,
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
    )
    resolved = resolver.policy_check_mode(capability_name, params)
    resolved_summary = resolved.get("resolved_summary", capability_name)

    row = db.Request(
        request_id=request_id,
        capability=capability_name,
        params_json=json.dumps(params, sort_keys=True),
        params_hash=params_hash,
        idempotency_key=idem_key,
        resolved_summary=resolved_summary,
        context_reason=context_reason,
        risk_level=risk_level,
        state="pending_approval",
        approval_code=approval_code,
        approval_hmac=creation_hmac,
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
        execution_expires_at=None,
        approved_at=None,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    try:
        db.insert_request(conn, row)
    except sqlite3.IntegrityError as e:
        existing = db.get_by_idempotency_key(conn, idem_key)
        if existing is not None:
            return {
                "status": "existing",
                "request_id": existing.request_id,
                "approval_code": existing.approval_code,
                "state": existing.state,
                "resolved_summary": existing.resolved_summary,
            }
        raise BrokerError("db_insert_failed", str(e)) from e

    audit_mod.write_event(audit_dir, {
        "event": "request_created",
        "request_id": request_id,
        "capability": capability_name,
        "params_hash": params_hash,
        "risk_level": risk_level,
    })
    # §9 auto-exec audit: the grant that authorised this, plus a hash of
    # the canonical params (never the params themselves — §15 forbidden).
    audit_mod.write_event(audit_dir, {
        "event": "policy.allow.standing_grant",
        "request_id": request_id,
        "capability": capability_name,
        "grant_id": grant_id,
        "params_hash": params_hash,
    })

    # Promote pending_approval → approved with the extended approval HMAC.
    creation_msg = policy.build_creation_message(
        request_id=request_id,
        capability=capability_name,
        params_hash=params_hash,
        idempotency_key_=idem_key,
        risk_level=risk_level,
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
    )
    approval_hmac = policy.compute_approval_hmac(
        key=ctx["hmac_key"],
        creation_msg=creation_msg,
        execution_expires_at=exec_expires,
        approved_at=now_ms,
    )
    db.transition(
        conn, request_id, "pending_approval", "approved",
        execution_expires_at=exec_expires,
        approved_at=now_ms,
        approval_hmac=approval_hmac,
    )

    approved_row = db.get_request(conn, request_id)
    assert approved_row is not None

    # Browser/session executors cannot be inline-executed here: they SIGTRAP
    # outside the donna-broker launchd session, and the broker cannot trampoline
    # itself. The grant has done its job (skipped the human approval); leave the
    # row `approved` and hand back the code so the caller runs `execute` through
    # donna-broker-via-session. (mcp_tool and other inline-safe caps fall through
    # to direct execution below, unchanged.)
    if cap.requires_session:
        return {
            "status": "approved",
            "request_id": request_id,
            "approval_code": approval_code,
            "via": "standing_grant",
            "grant_id": grant_id,
            "next": "execute_via_session_trampoline",
            "resolved_summary": resolved_summary,
        }

    def audit_writer(evt: dict[str, Any]) -> None:
        audit_mod.write_event(audit_dir, evt)

    creds_config = executor.CredsConfig(
        creds_dir=Path(ctx["config"]["creds_dir"]),
        identity_path=Path(ctx["config"]["identity_path"]),
        age_binary=ctx["config"]["age_binary"],
    ) if cap.creds is not None else None

    outcome = executor.execute(
        cap, approved_row, params, conn, audit_writer=audit_writer,
        creds_config=creds_config,
    )
    return {
        "status": outcome.state,
        "request_id": request_id,
        "via": "standing_grant",
        "grant_id": grant_id,
        "result": outcome.result,
        "error_code": outcome.error_code,
        "error_message": outcome.error_message,
    }


def _write_queue_file(
    queue_dir: str, request_id: str, code: str, expires_at: int,
    capability: str, risk_level: str, summary: str, context_reason: str,
) -> str:
    """Atomic write: .tmp → fsync → rename. §12.2."""
    Path(queue_dir).mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": request_id,
        "code": code,
        "expires_at": expires_at,
        "risk_level": risk_level,
        "capability": capability,
        "fields": [
            {"label": "capability", "value": capability, "provenance": "broker"},
            {"label": "resolved_summary", "value": summary, "provenance": "broker"},
            {"label": "context_reason", "value": context_reason, "provenance": "donna"},
        ],
    }
    tmp = Path(queue_dir) / f"{request_id}.json.tmp"
    final = Path(queue_dir) / f"{request_id}.json"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, final)
    return str(final)


def _delete_queue_file(queue_dir: str, request_id: str) -> None:
    """Remove the approval-queue file once the approval response is confirmed.
    Prevents prompt replay on daemon restart (seenRequestIds is volatile).
    Idempotent — silently ignores missing files."""
    try:
        (Path(queue_dir) / f"{request_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _handle_execute(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Run an approved capability. Checks approval response file for a
    Telegram-approved code, verifies HMAC, dispatches to executor."""
    code = payload.get("approval_code")
    if not isinstance(code, str) or not code:
        raise BrokerError("invalid_input", "approval_code required")

    conn = ctx["conn"]
    row = db.get_by_approval_code(conn, code)
    if row is None:
        raise BrokerError(
            "not_found", f"no active request with code {code!r}",
            status="not_found",
        )

    # Must have a matching approval-response file before execute.
    response_path = Path(ctx["responses_dir"]) / f"{row.request_id}.json"
    if not response_path.exists():
        return {
            "status": "approval_required",
            "request_id": row.request_id,
            "code": code,
            "reason": "no approval response seen yet",
        }

    try:
        response_payload = json.loads(response_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise BrokerError(
            "approval_response_malformed",
            f"could not parse {response_path}: {e}",
        ) from e

    # Queue file has served its purpose — delete it now so a daemon restart
    # doesn't replay the approval prompt via the bridge's volatile seenRequestIds.
    _delete_queue_file(ctx["queue_dir"], row.request_id)

    decision = response_payload.get("decision")
    if decision != "approve":
        return {
            "status": "denied" if decision == "deny" else "cancelled",
            "request_id": row.request_id,
        }

    # Verify creation-time HMAC before any state change.
    params = json.loads(row.params_json)
    canonical = canonicalize.canonicalize(params)
    recomputed_hash = canonicalize.params_hash(params)
    if recomputed_hash != row.params_hash:
        db.quarantine(conn, row.request_id, "params_hash_mismatch",
                      f"{recomputed_hash} != {row.params_hash}")
        audit_mod.write_event(ctx["audit_dir"], {
            "event": "audit.params_hash_mismatch",
            "request_id": row.request_id,
        })
        raise BrokerError(
            "integrity_failed",
            f"params_hash mismatch on {row.request_id}",
        )

    creation_msg = policy.build_creation_message(
        request_id=row.request_id,
        capability=row.capability,
        params_hash=row.params_hash,
        idempotency_key_=row.idempotency_key,
        risk_level=row.risk_level,
        created_at=row.created_at,
        approval_expires_at=row.approval_expires_at,
    )
    # §7.3: the stored HMAC is the creation HMAC until the pending →
    # approved transition, after which it's the extended approval-time
    # HMAC covering execution_expires_at + approved_at. Pick the
    # expected digest by state so re-entry from 'executing' (and a
    # fresh execute on an already-approved row) verifies correctly.
    if row.state == "pending_approval":
        expected_hmac = policy.compute_creation_hmac(
            key=ctx["hmac_key"],
            request_id=row.request_id,
            capability=row.capability,
            params_hash=row.params_hash,
            idempotency_key_=row.idempotency_key,
            risk_level=row.risk_level,
            created_at=row.created_at,
            approval_expires_at=row.approval_expires_at,
        )
    else:
        if row.execution_expires_at is None or row.approved_at is None:
            db.quarantine(conn, row.request_id, "hmac_mismatch",
                          "approval fields missing on post-pending row")
            audit_mod.write_event(ctx["audit_dir"], {
                "event": "audit.hmac_mismatch",
                "request_id": row.request_id,
            })
            raise BrokerError("integrity_failed", "HMAC verification failed")
        expected_hmac = policy.compute_approval_hmac(
            key=ctx["hmac_key"],
            creation_msg=creation_msg,
            execution_expires_at=row.execution_expires_at,
            approved_at=row.approved_at,
        )
    if row.approval_hmac is None or not hmac.compare_digest(
        expected_hmac, row.approval_hmac,
    ):
        db.quarantine(conn, row.request_id, "hmac_mismatch", "creation HMAC")
        audit_mod.write_event(ctx["audit_dir"], {
            "event": "audit.hmac_mismatch",
            "request_id": row.request_id,
        })
        raise BrokerError("integrity_failed", "HMAC verification failed")

    # broker-standing-grants §7: specialise execute to finalise a pending
    # grant. A grant.create row is NOT a manifest capability — it has no
    # executor. The approve decision + params_hash + HMAC have all been
    # verified above (same guards as any approval), so persisting the
    # grant now is safe. We promote pending → approved, then
    # _finalise_grant drives approved → executing → succeeded and inserts
    # the standing grant.
    if row.capability == policy.GRANT_CREATE_CAPABILITY:
        if row.state == "pending_approval":
            now_ms = int(time.time() * 1000)
            grant_exec_window_minutes = 720
            exec_expires = now_ms + grant_exec_window_minutes * 60 * 1000
            approval_hmac = policy.compute_approval_hmac(
                key=ctx["hmac_key"],
                creation_msg=creation_msg,
                execution_expires_at=exec_expires,
                approved_at=now_ms,
            )
            db.transition(
                conn, row.request_id, "pending_approval", "approved",
                execution_expires_at=exec_expires,
                approved_at=now_ms,
                approval_hmac=approval_hmac,
            )
            audit_mod.write_event(ctx["audit_dir"], {
                "event": "request_approved",
                "request_id": row.request_id,
            })
            refreshed = db.get_request(conn, row.request_id)
            assert refreshed is not None
            row = refreshed
        return _finalise_grant(ctx, row, params)

    # Idempotent re-entry from `executing`. Happens when Donna's first
    # execute call handed off mcp_tool metadata but the subsequent MCP
    # re-attempt tripped params_mismatch at the hook (or Donna lost
    # context and is re-calling execute to recover the handoff). The
    # row is already executing; we must NOT re-run the approved →
    # executing transition (which would fail). HMAC + params_hash +
    # approval-response file are re-verified above, so re-emitting the
    # handoff is safe — every guard that gated the original fires again.
    # MVP: only mcp_tool capabilities are safe to re-dispatch this way.
    # Subprocess capabilities would require orphan-detection of the
    # previous Popen, which is out of scope for this fix.
    if row.state == "executing":
        cap = ctx["capabilities"][row.capability]
        if cap.executor_type != "mcp_tool":
            raise BrokerError(
                "invalid_state",
                "re-entry from 'executing' is only supported for "
                "mcp_tool capabilities; subprocess capabilities stranded "
                "in executing must be cancelled manually",
            )
        audit_mod.write_event(ctx["audit_dir"], {
            "event": "request_execution_mcp_tool_reentry",
            "request_id": row.request_id,
            "capability": row.capability,
            "tool": cap.executor_target,
        })
        return {
            "status": "executing",
            "request_id": row.request_id,
            "result": {
                "executor_type": "mcp_tool",
                "tool": cap.executor_target,
                "params": params,
            },
            "error_code": None,
            "error_message": None,
        }

    # If row is still pending_approval, promote it to approved first.
    if row.state == "pending_approval":
        now_ms = int(time.time() * 1000)
        exec_expires = now_ms + int(
            ctx["capabilities"][row.capability].execution_window_minutes
        ) * 60 * 1000
        # Extended HMAC at approval time (§7.3).
        approval_hmac = policy.compute_approval_hmac(
            key=ctx["hmac_key"],
            creation_msg=creation_msg,
            execution_expires_at=exec_expires,
            approved_at=now_ms,
        )
        db.transition(
            conn, row.request_id, "pending_approval", "approved",
            execution_expires_at=exec_expires,
            approved_at=now_ms,
            approval_hmac=approval_hmac,
        )
        audit_mod.write_event(ctx["audit_dir"], {
            "event": "request_approved",
            "request_id": row.request_id,
        })
        row = db.get_request(conn, row.request_id)
        assert row is not None

    cap = ctx["capabilities"][row.capability]

    def audit_writer(evt: dict[str, Any]) -> None:
        audit_mod.write_event(ctx["audit_dir"], evt)

    creds_config = executor.CredsConfig(
        creds_dir=Path(ctx["config"]["creds_dir"]),
        identity_path=Path(ctx["config"]["identity_path"]),
        age_binary=ctx["config"]["age_binary"],
    ) if cap.creds is not None else None

    outcome = executor.execute(
        cap, row, params, conn, audit_writer=audit_writer,
        creds_config=creds_config,
    )
    return {
        "status": outcome.state,
        "request_id": row.request_id,
        "result": outcome.result,
        "error_code": outcome.error_code,
        "error_message": outcome.error_message,
    }


def _find_executing_by_tool(
    conn: sqlite3.Connection, tool_name: str, capabilities: dict[str, Any]
) -> Optional[str]:
    """Return the request_id of the oldest executing mcp_tool row
    whose capability's executor_target matches tool_name.

    Oldest-first (ASC) so that when multiple rows are executing for the
    same capability, the PostToolUse hook closes the one that was
    dispatched first — matching the order in which MCP calls complete.
    DESC caused the wrong row to be closed when two requests were
    in-flight simultaneously (newer row was closed instead of older).

    Used when the PostToolUse hook omits request_id — which happens
    because including _donna_request_id in an MCP call's tool_input
    would break the params_hash check at the PreToolUse hook.
    """
    for cap_name, cap in capabilities.items():
        if getattr(cap, "executor_type", None) != "mcp_tool":
            continue
        if getattr(cap, "executor_target", None) != tool_name:
            continue
        row = conn.execute(
            "SELECT request_id FROM requests "
            "WHERE state = 'executing' AND capability = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (cap_name,),
        ).fetchone()
        if row:
            return str(row[0])
    return None


def _close_executing_row(
    conn: sqlite3.Connection,
    audit_dir: str,
    request_id: str,
    tool_outcome: str,
) -> None:
    """Transition executing → succeeded/failed and emit the audit event.
    No-op when the row is not in executing state (race guard)."""
    row = db.get_request(conn, request_id)
    if row is None or row.state != "executing":
        return
    if tool_outcome == "succeeded":
        db.transition(conn, request_id, "executing", "succeeded",
                      executed_at=int(time.time() * 1000))
        audit_mod.write_event(audit_dir, {
            "event": "request_execution_succeeded",
            "request_id": request_id,
        })
    else:
        db.transition(conn, request_id, "executing", "failed",
                      error_code="mcp_tool_reported_failure",
                      executed_at=int(time.time() * 1000))
        audit_mod.write_event(audit_dir, {
            "event": "request_execution_failed",
            "request_id": request_id,
        })


def _handle_audit_result(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """PostToolUse hook. For low-risk reads: emit mcp_tool_allowed. For
    medium+ successful runs on tracked rows: transition executing →
    succeeded."""
    tool_name = payload.get("tool_name") or ""
    tool_outcome = payload.get("outcome") or "succeeded"
    request_id: Optional[str] = payload.get("request_id") or None

    # Fallback: when the PostToolUse hook omits request_id (no
    # _donna_request_id in tool_input — adding it would change the
    # params_hash and break the PreToolUse executing-row bypass),
    # resolve the executing row by capability lookup on tool_name.
    if not request_id and tool_name:
        request_id = _find_executing_by_tool(
            ctx["conn"], tool_name, ctx.get("capabilities", {})
        )

    mcp_tools: dict[str, str] = ctx["mcp_tools"]
    risk = mcp_tools.get(tool_name, "unknown")
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "mcp_tool_allowed" if tool_outcome == "succeeded" else "mcp_tool_blocked",
        "tool": tool_name,
        "risk": risk,
        "outcome": tool_outcome,
        "request_id": request_id,
    })
    if request_id:
        _close_executing_row(ctx["conn"], ctx["audit_dir"], request_id, tool_outcome)
    return {"status": "ok"}


def _handle_cancel(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rid = payload.get("request_id") or (
        db.get_by_approval_code(ctx["conn"], payload.get("approval_code", "")).request_id  # type: ignore[union-attr]
        if isinstance(payload.get("approval_code"), str)
        and db.get_by_approval_code(ctx["conn"], payload["approval_code"]) is not None
        else None
    )
    if not isinstance(rid, str):
        raise BrokerError(
            "invalid_input", "request_id or approval_code required",
        )
    r = db.get_request(ctx["conn"], rid)
    if r is None:
        raise BrokerError("not_found", f"no request {rid!r}", status="not_found")
    if r.state not in {"pending_approval", "approved", "executing"}:
        raise BrokerError(
            "invalid_state",
            f"cannot cancel from state {r.state!r}",
        )
    db.transition(ctx["conn"], rid, r.state, "cancelled")
    _delete_queue_file(ctx["queue_dir"], rid)
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "request_cancelled",
        "request_id": rid,
    })
    return {"status": "cancelled", "request_id": rid}


# ---- Connected Sites modes (connected-sites-broker-handoff) -------------


def _handle_store_credential(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Seal a site login into the age vault at <creds_dir>/<site>.age.

    The password arrives on stdin, is wrapped into the executor creds
    JSON shape ({username, email, password} — the EA executors read
    `email`), encrypted to the broker's own recipient, and dropped.
    It is never echoed back, never audited, never written to disk in
    plaintext. Replacing an existing entry is allowed (re-connect)."""
    site = payload.get("site")
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(site, str) or not site:
        raise BrokerError("invalid_input", "site required")
    if not isinstance(username, str) or not username.strip():
        raise BrokerError("invalid_input", "username required")
    if not isinstance(password, str) or not password:
        raise BrokerError("invalid_input", "password required")
    username = username.strip()

    plaintext = json.dumps(
        {"username": username, "email": username, "password": password},
        ensure_ascii=False,
    ).encode("utf-8")
    if len(plaintext) > executor.CREDS_MAX_BYTES:
        raise BrokerError(
            "invalid_input",
            f"credential exceeds {executor.CREDS_MAX_BYTES} bytes",
        )

    def audit_writer(evt: dict[str, Any]) -> None:
        audit_mod.write_event(ctx["audit_dir"], evt)

    try:
        stored = creds_mod.store_creds(
            site,
            plaintext,
            creds_dir=ctx["config"]["creds_dir"],
            identity_path=ctx["config"]["identity_path"],
            age_binary=ctx["config"]["age_binary"],
            age_keygen_binary=ctx["config"]["age_keygen_binary"],
            audit_writer=audit_writer,
        )
    except creds_mod.CredsError as ce:
        # ce.message never carries the credential (store_creds contract).
        raise BrokerError(ce.error_code, ce.message) from ce

    return {
        "status": "stored",
        "site": site,
        "username": username,
        "replaced": bool(stored.get("replaced")),
    }


def _handle_site_check(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Probe whether a stored site login still works: unlock the vault
    entry and hand it to the site's check executor (headless login, no
    writes). Human-tap read — no approval row. Responses are honest
    structured statuses, not exceptions, so the app can mirror them."""
    site = payload.get("site")
    if not isinstance(site, str) or not site:
        raise BrokerError("invalid_input", "site required")

    probe_binary = SITE_PROBES.get(site)
    if probe_binary is None:
        return {
            "status": "no_checker",
            "site": site,
            "note": f"no site-check executor installed for {site!r}",
        }

    def audit_writer(evt: dict[str, Any]) -> None:
        audit_mod.write_event(ctx["audit_dir"], evt)

    try:
        cred_bytes = creds_mod.unlock_creds(
            site,
            creds_dir=ctx["config"]["creds_dir"],
            identity_path=ctx["config"]["identity_path"],
            age_binary=ctx["config"]["age_binary"],
            audit_writer=audit_writer,
        )
    except creds_mod.CredsError as ce:
        if ce.error_code == "creds_missing":
            return {
                "status": "not_connected",
                "site": site,
                "note": "no stored login for this site",
            }
        raise BrokerError(ce.error_code, ce.message) from ce

    result = executor.run_probe(
        probe_binary,
        {"capability": f"{site}.site_check", "params": {"site": site}},
        cred_bytes,
        timeout_seconds=SITE_PROBE_TIMEOUT_SECONDS,
        audit_writer=audit_writer,
    )
    del cred_bytes

    audit_mod.write_event(ctx["audit_dir"], {
        "event": "site_check",
        "site": site,
        "outcome": result.get("status", "error"),
    })

    if result.get("status") == "error":
        return {
            "status": "error",
            "site": site,
            "error_code": result.get("error_code"),
            "note": str(result.get("detail") or "site check failed"),
        }
    return {
        "status": str(result.get("status") or "error"),
        "site": site,
        "note": str(result.get("note") or ""),
    }


# ---- in-app approval mode (inapp-approval-broker-handoff, Option B) ------


def _write_approval_response(
    responses_dir: str, request_id: str, channel: str, approved_by: str
) -> str:
    """Record a proof-of-human approval receipt — byte-for-byte the same
    receipt the Telegram daemon writes (claude-telegram-hardened
    donna_broker.ts `writeApprovalResponse`), so the existing `execute`
    path consumes it unchanged. The extra `channel` field marks the
    proof-of-human source ("app" vs "telegram") for the audit trail;
    `execute` only reads `decision`, so the superset is harmless.

    Atomic .tmp → rename so `execute` never reads a half-written file."""
    Path(responses_dir).mkdir(parents=True, exist_ok=True)
    receipt = {
        "request_id": request_id,
        "decision": "approve",
        "approved_by": approved_by,
        "channel": channel,
        "responded_at": int(time.time() * 1000),
    }
    tmp = Path(responses_dir) / f"{request_id}.json.tmp"
    final = Path(responses_dir) / f"{request_id}.json"
    tmp.write_text(json.dumps(receipt), encoding="utf-8")
    os.replace(tmp, final)
    return str(final)


def _handle_app_approve(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Record an IN-APP proof-of-human approval (master-design Option B,
    docs/inapp-approval-broker-handoff.md).

    The Daru app's human-tap endpoint calls this when `execute` reports
    `approval_required`. It records the SAME proof-of-human receipt the
    Telegram daemon would write — just sourced from the app — and the app
    then retries `execute`, which owns the HMAC-bound
    pending_approval → approved → executing transition. We deliberately do
    NOT duplicate that transition (or its HMAC) here: `execute` stays the
    single source of truth for state changes, exactly as on the Telegram
    path. This handler's only effect is the receipt + a distinct audit
    line, so it needs neither the manifest nor the HMAC key.

    SECURITY — this is the proof-of-human gate (see CLAUDE.md §Security):
      - The broker CANNOT distinguish the app caller from Donna (the LLM):
        both reach the donna-broker CLI as the same OS user. What keeps the
        LLM from self-approving is that `app-approve` is DELIBERATELY absent
        from the PreToolUse hook's BROKER_MODES allowlist
        (hooks/capability-guard-phase1.py) — the same defence-by-omission
        that protects store-credential / site-check / grant-*. If this mode
        is ever added to that allowlist the proof-of-human gate collapses.
        Do not add it.
      - Only active rows within the approval window are addressable:
        terminal/expired codes already return None from
        get_by_approval_code (and lazy reconcile sweeps expired pending
        rows before dispatch); we re-check the window here belt-and-braces.
      - An existing receipt is never overwritten — an app tap cannot flip a
        decision already recorded (e.g. a Telegram deny).
      - Each in-app approval is audited as `request_approved_in_app` with
        channel="app", distinct from the Telegram trail.
    """
    code = payload.get("approval_code")
    if not isinstance(code, str) or not code:
        raise BrokerError("invalid_input", "approval_code required")

    conn = ctx["conn"]
    row = db.get_by_approval_code(conn, code)
    if row is None:
        # Unknown, expired, or terminal codes are not addressable by code.
        raise BrokerError(
            "not_found", f"no active request with code {code!r}",
            status="not_found",
        )

    # The proof-of-human is already recorded once the row has moved past
    # pending_approval (a prior tap, or a Telegram approval). Report the
    # current state idempotently rather than re-writing the receipt.
    if row.state in {"approved", "executing"}:
        return {"status": "approved", "state": row.state}

    # Honour the approval window explicitly (defence in depth — lazy
    # reconcile already expires stale pending rows before dispatch).
    now_ms = int(time.time() * 1000)
    if row.approval_expires_at < now_ms:
        raise BrokerError(
            "expired",
            f"approval window for code {code!r} has closed",
            status="expired",
        )

    # Never overwrite an existing receipt: an app tap must not be able to
    # flip a decision already recorded out-of-band (e.g. a Telegram deny).
    existing = Path(ctx["responses_dir"]) / f"{row.request_id}.json"
    if existing.exists():
        try:
            prior_decision = json.loads(
                existing.read_text(encoding="utf-8")
            ).get("decision")
        except Exception:
            prior_decision = None
        if prior_decision == "approve":
            return {"status": "approved", "state": row.state}
        return {
            "status": "already_recorded",
            "state": row.state,
            "decision": prior_decision,
        }

    _write_approval_response(
        ctx["responses_dir"], row.request_id,
        channel="app", approved_by="app",
    )
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "request_approved_in_app",
        "request_id": row.request_id,
        "capability": row.capability,
        "channel": "app",
    })
    return {"status": "approved", "state": "approved"}


# ---- standing-grant modes (broker-standing-grants §7) -------------------


def _format_grant_scope(
    capability: str,
    constraints: dict[str, Any],
    purpose: str,
    max_per_period: int,
    period_seconds: int,
    expires_in_days: int,
) -> str:
    """Plain-English full-scope summary shown at the grant approval (§7).

    e.g. "Allow: send email to graham@… , ≤1/week, expires 90d, purpose
    'School roundup'". The whole scope must be legible to the human
    approving it — this is the meta-privilege consent string."""
    if capability == "gmail.send":
        target = constraints.get("to", "?")
        action = f"send email to {target}"
        subj = constraints.get("subject")
        if isinstance(subj, dict) and "prefix" in subj:
            action += f" with subject starting '{subj['prefix']}'"
        elif isinstance(subj, str):
            action += f" with subject '{subj}'"
    else:
        pins = ", ".join(f"{k}={v}" for k, v in sorted(constraints.items()))
        action = f"{capability} ({pins})" if pins else capability

    period_days = period_seconds / 86400
    if abs(period_days - 7) < 1e-9:
        rate = f"≤{max_per_period}/week"
    elif abs(period_days - 1) < 1e-9:
        rate = f"≤{max_per_period}/day"
    else:
        rate = f"≤{max_per_period} per {int(period_seconds)}s"

    return (
        f"Allow: {action} , {rate}, expires {expires_in_days}d, "
        f"purpose '{purpose}'"
    )


def _handle_grant_create(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """broker-standing-grants §7. The meta-approval. Does NOT persist the
    grant — it raises a normal approval_required whose summary spells out
    the FULL scope. The grant is persisted only when that code is approved
    via the existing execute path (see _finalise_grant).

    `grant.create` is hard-coded high-risk and (by construction) excluded
    from §6 grant-matching — no grant can authorise creating grants.
    """
    capability = payload.get("capability")
    constraints = payload.get("constraints")
    purpose = payload.get("purpose")
    max_per_period = payload.get("max_per_period")
    period_seconds = payload.get("period_seconds")
    expires_in_days = payload.get("expires_in_days", GRANT_DEFAULT_EXPIRES_DAYS)
    raw_context_reason = payload.get("context_reason") or ""

    if not isinstance(capability, str) or not capability:
        raise BrokerError("invalid_input", "capability required")
    if not isinstance(constraints, dict):
        raise BrokerError("invalid_input", "constraints must be an object")
    if not isinstance(purpose, str) or not purpose:
        raise BrokerError("invalid_input", "purpose required")
    if not isinstance(max_per_period, int) or isinstance(max_per_period, bool) \
            or max_per_period <= 0:
        raise BrokerError("invalid_input", "max_per_period must be a positive int")
    if not isinstance(period_seconds, int) or isinstance(period_seconds, bool) \
            or period_seconds <= 0:
        raise BrokerError("invalid_input", "period_seconds must be a positive int")
    if not isinstance(expires_in_days, int) or isinstance(expires_in_days, bool) \
            or expires_in_days <= 0:
        raise BrokerError("invalid_input", "expires_in_days must be a positive int")
    if expires_in_days > GRANT_MAX_EXPIRES_DAYS:
        raise BrokerError(
            "invalid_input",
            f"expires_in_days {expires_in_days} exceeds max "
            f"{GRANT_MAX_EXPIRES_DAYS}",
        )

    # The grant must target a known capability (you can't grant standing
    # autonomy for something the broker can't execute).
    _require_capability(ctx["capabilities"], capability)

    # §5 structural validation (e.g. gmail.send must pin `to`).
    try:
        policy.validate_constraints(capability, constraints)
    except policy.GrantConstraintError as e:
        raise BrokerError("invalid_constraints", str(e)) from e

    try:
        context_reason, redactions = policy.sanitise_context_reason(
            raw_context_reason
        )
    except policy.ContextReasonTooLong as e:
        raise BrokerError("invalid_input", str(e)) from e

    conn = ctx["conn"]
    grants_db.ensure_grant_tables(conn)
    now_ms = int(time.time() * 1000)

    # The grant proposal is carried in the request row's params_json so
    # the existing execute path can finalise it after human approval.
    proposal = {
        "capability": capability,
        "constraints": constraints,
        "purpose": purpose,
        "max_per_period": max_per_period,
        "period_seconds": period_seconds,
        "expires_in_days": expires_in_days,
    }
    params_hash = canonicalize.params_hash(proposal)
    canonical = canonicalize.canonicalize(proposal)
    date_component = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    idem_key = policy.idempotency_key(
        policy.GRANT_CREATE_CAPABILITY, canonical, date_component,
    )

    existing = db.get_by_idempotency_key(conn, idem_key)
    if existing is not None:
        return {
            "status": "existing",
            "request_id": existing.request_id,
            "approval_code": existing.approval_code,
            "state": existing.state,
            "resolved_summary": existing.resolved_summary,
        }

    request_id = f"req-{uuid.uuid4().hex[:12]}"
    # grant.create is HARD-CODED high-risk (§3.1). Reuse gmail.send's
    # windows as a sensible default for the meta-approval; the exact
    # window isn't grant-scope, just how long the approval code lives.
    approval_window_minutes = 1440
    approval_expires_at = now_ms + approval_window_minutes * 60 * 1000
    approval_code = policy.generate_approval_code()
    summary = _format_grant_scope(
        capability, constraints, purpose, max_per_period,
        period_seconds, expires_in_days,
    )
    creation_hmac = policy.compute_creation_hmac(
        key=ctx["hmac_key"],
        request_id=request_id,
        capability=policy.GRANT_CREATE_CAPABILITY,
        params_hash=params_hash,
        idempotency_key_=idem_key,
        risk_level="high",
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
    )

    row = db.Request(
        request_id=request_id,
        capability=policy.GRANT_CREATE_CAPABILITY,
        params_json=json.dumps(proposal, sort_keys=True),
        params_hash=params_hash,
        idempotency_key=idem_key,
        resolved_summary=summary,
        context_reason=context_reason,
        risk_level="high",
        state="pending_approval",
        approval_code=approval_code,
        approval_hmac=creation_hmac,
        created_at=now_ms,
        approval_expires_at=approval_expires_at,
        execution_expires_at=None,
        approved_at=None,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    try:
        db.insert_request(conn, row)
    except sqlite3.IntegrityError as e:
        existing = db.get_by_idempotency_key(conn, idem_key)
        if existing is not None:
            return {
                "status": "existing",
                "request_id": existing.request_id,
                "approval_code": existing.approval_code,
                "state": existing.state,
                "resolved_summary": existing.resolved_summary,
            }
        raise BrokerError("db_insert_failed", str(e)) from e

    audit_dir = ctx["audit_dir"]
    audit_mod.write_event(audit_dir, {
        "event": "grant.create.proposed",
        "request_id": request_id,
        "grant_capability": capability,
        "params_hash": params_hash,
    })
    if redactions:
        audit_mod.write_event(audit_dir, {
            "event": "audit.context_reason_redacted",
            "request_id": request_id,
            "original_length": len(raw_context_reason),
            "redaction_types": redactions,
            "context_reason_original": raw_context_reason,
        })

    queue_file_path = _write_queue_file(
        ctx["queue_dir"], request_id, approval_code, approval_expires_at,
        policy.GRANT_CREATE_CAPABILITY, "high", summary,
        context_reason or "",
    )

    return {
        "status": "approval_required",
        "request_id": request_id,
        "code": approval_code,
        "summary": summary,
        "risk_level": "high",
        "is_grant": True,
        "approval_expires_at": approval_expires_at,
        "queue_file": queue_file_path,
    }


def _finalise_grant(
    ctx: dict[str, Any], row: db.Request, proposal: dict[str, Any]
) -> dict[str, Any]:
    """Persist a grant from an approved grant.create row. Called from the
    execute path AFTER the approve decision + HMAC verify. The grant's
    constraints MAC is computed here with the broker key; the requests
    row is transitioned approved → succeeded."""
    conn = ctx["conn"]
    grants_db.ensure_grant_tables(conn)
    now_ms = int(time.time() * 1000)

    capability = proposal["capability"]
    constraints = proposal["constraints"]
    expires_in_days = int(proposal["expires_in_days"])
    expires_at = now_ms + expires_in_days * 86400 * 1000

    grant_id = f"grant-{uuid.uuid4().hex[:12]}"
    constraints_mac = policy.compute_constraints_mac(
        ctx["hmac_key"], capability, constraints,
    )
    grant = grants_db.StandingGrant(
        id=grant_id,
        capability=capability,
        constraints=json.dumps(constraints, sort_keys=True),
        constraints_mac=constraints_mac,
        purpose=proposal["purpose"],
        max_per_period=int(proposal["max_per_period"]),
        period_seconds=int(proposal["period_seconds"]),
        created_at=now_ms,
        expires_at=expires_at,
        approved_via=row.approval_code or "",
        revoked_at=None,
    )
    grants_db.insert_grant(conn, grant)

    db.transition(
        conn, row.request_id, "approved", "executing",
    )
    db.transition(
        conn, row.request_id, "executing", "succeeded",
        result_json=json.dumps({"grant_id": grant_id}),
        executed_at=now_ms,
    )
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "grant.created",
        "request_id": row.request_id,
        "grant_id": grant_id,
        "grant_capability": capability,
        "max_per_period": grant.max_per_period,
        "period_seconds": grant.period_seconds,
        "expires_at": expires_at,
    })
    return {
        "status": "succeeded",
        "request_id": row.request_id,
        "grant_id": grant_id,
        "result": {"grant_id": grant_id},
        "error_code": None,
        "error_message": None,
    }


def _handle_grant_list(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """broker-standing-grants §7. Returns active + expired (and revoked)
    grants for the app. now_ms is computed here only to label active vs
    expired in the response — the stored rows are unchanged."""
    conn = ctx["conn"]
    grants_db.ensure_grant_tables(conn)
    now_ms = int(time.time() * 1000)
    grants = grants_db.list_grants(conn)
    out = []
    for g in grants:
        if g.revoked_at is not None:
            status = "revoked"
        elif g.expires_at <= now_ms:
            status = "expired"
        else:
            status = "active"
        out.append({
            "grant_id": g.id,
            "capability": g.capability,
            "constraints": json.loads(g.constraints),
            "purpose": g.purpose,
            "max_per_period": g.max_per_period,
            "period_seconds": g.period_seconds,
            "created_at": g.created_at,
            "expires_at": g.expires_at,
            "revoked_at": g.revoked_at,
            "status": status,
        })
    return {"status": "ok", "grants": out}


def _handle_grant_revoke(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """broker-standing-grants §7. Sets revoked_at. ALWAYS allowed (revoke
    only ever reduces privilege, §3.6) — no approval, no rate limit.
    Audited."""
    grant_id = payload.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise BrokerError("invalid_input", "grant_id required")

    conn = ctx["conn"]
    grants_db.ensure_grant_tables(conn)
    now_ms = int(time.time() * 1000)
    revoked = grants_db.revoke_grant(conn, grant_id, now_ms)
    if not revoked:
        # Either missing or already revoked. Idempotent + always-allowed:
        # report the state without erroring (revoke never fails for a
        # policy reason).
        existing = grants_db.get_grant(conn, grant_id)
        if existing is None:
            raise BrokerError(
                "not_found", f"no grant with id {grant_id!r}",
                status="not_found",
            )
        return {
            "status": "already_revoked",
            "grant_id": grant_id,
            "revoked_at": existing.revoked_at,
        }
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "grant.revoked",
        "grant_id": grant_id,
        "revoked_at": now_ms,
    })
    return {"status": "revoked", "grant_id": grant_id, "revoked_at": now_ms}


def _handle_verify_audit(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    result = audit_mod.verify_chain(ctx["audit_dir"])
    if result is None:
        return {"status": "ok", "verified": True}
    return {"status": "integrity_break", "verified": False, "break": result}


def _handle_verify_manifests(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Preflight-check capabilities.yaml + referenced JSON schemas +
    mcp-tools.yaml. Intended for supervisor startup: runs before we
    spawn Claude, so a missing schema fails loudly *before* Donna comes
    up silently broken.

    Spec: security-v1.1 §8 (manifest format), §8.1 (mcp-tools risk
    tiers), §13.1 (broker modes).

    Success returns a deterministic summary of what loaded, so the
    supervisor (and ops tooling) can log exactly which capabilities
    were picked up by this deploy. Failure is already surfaced by
    _build_ctx raising BrokerError("manifest_error", ...) with the
    precise line that broke — exit code 1.
    """
    capabilities: dict[str, validator.Capability] = ctx["capabilities"]
    mcp_tools: dict[str, str] = ctx["mcp_tools"]
    return {
        "status": "ok",
        "verified": True,
        "capabilities_count": len(capabilities),
        "capabilities": sorted(capabilities.keys()),
        "mcp_tools_count": len(mcp_tools),
    }


def _handle_verify_vault(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """§6.3 verify-vault subcommand. Runs the same structural checks
    as the startup sweep. One line per check, OK or WARN prefix, plus
    a summary. Exit code 1 if any WARN fired.

        OK   vault_dir_exists       /path/to/creds
        WARN identity_mode_loose    /path/to/identity.age mode=0644
        --
        N warnings, M checks passed.
    """
    caps = ctx["capabilities"]
    declared = [c.creds.entry for c in caps.values() if c.creds is not None]

    creds_dir = Path(ctx["config"]["creds_dir"])
    identity_path = Path(ctx["config"]["identity_path"])
    age_binary = ctx["config"]["age_binary"]

    warnings = vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=identity_path,
        age_binary=age_binary,
        declared_entries=declared,
        audit_writer=None,   # CLI path does not audit
    )

    # The full set of checks that sweep() performs, in order. Each
    # tuple: (check_label, artefact, set of reason codes that would
    # fire for this check). Used to emit OK lines for passes.
    expected: list[tuple[str, str, set[str]]] = [
        ("vault_dir_exists", str(creds_dir), {"vault_dir_missing"}),
        ("vault_dir_mode", str(creds_dir), {"vault_dir_mode_loose"}),
        ("vault_dir_owner", str(creds_dir), {"vault_dir_owner_wrong"}),
        ("identity_exists", str(identity_path), {"identity_missing"}),
        ("identity_mode", str(identity_path), {"identity_mode_loose"}),
        ("identity_owner", str(identity_path), {"identity_owner_wrong"}),
        ("age_binary", age_binary, {"age_binary_missing"}),
    ]
    for entry in declared:
        entry_artefact = str(creds_dir / f"{entry}.age")
        expected.append((f"entry_exists[{entry}]", entry_artefact, {"entry_missing"}))
        expected.append((f"entry_mode[{entry}]", entry_artefact, {"entry_mode_loose"}))
        expected.append((f"entry_owner[{entry}]", entry_artefact, {"entry_owner_wrong"}))

    # Key each warning by (reason, primary_artefact) for O(1) lookup.
    warning_keys: dict[tuple[str, str], dict[str, Any]] = {}
    for w in warnings:
        artefact = w.get("path") or w.get("binary") or ""
        warning_keys[(w["reason"], artefact)] = w

    lines: list[str] = []
    warn_count = 0
    ok_count = 0
    for check_label, artefact, reasons in expected:
        fired = [(r, warning_keys[(r, artefact)])
                 for r in reasons if (r, artefact) in warning_keys]
        if fired:
            for reason, w in fired:
                detail_parts = [
                    f"{k}={v}" for k, v in w.items()
                    if k not in {"reason", "path", "entry", "binary"}
                ]
                detail = " ".join(detail_parts)
                lines.append(
                    f"WARN {reason:22s} {artefact} {detail}".rstrip()
                )
                warn_count += 1
        else:
            lines.append(f"OK   {check_label:22s} {artefact}")
            ok_count += 1

    lines.append("--")
    lines.append(f"{warn_count} warnings, {ok_count} checks passed.")

    return {
        "status": "ok" if warn_count == 0 else "warnings",
        "warnings": warnings,
        "stdout_lines": lines,
        "exit_code": 0 if warn_count == 0 else 1,
    }


def _handle_list_packs(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Read-only summary of capability packs on disk (promoter Plan C
    Task 1). For each child DIR under the configured packs dir, the broker
    (the trusted side) computes a plain summary the app can surface:
    ``{pack_id, description, capabilities, pack_hash, signed, installed}``.

    ``packs_dir`` comes from ``payload["packs_dir"]`` or ``DONNA_PACKS_DIR``,
    default ``/Users/donna-broker/broker/packs/available``. ``signed`` only
    reports whether ``pack.sig`` exists — NO signature verification here
    (that is install-time, ``pack_keys``/``pack_verify``). ``installed`` is
    true iff every capability the pack declares is already present in the
    live manifest (``ctx["capabilities"]``). ``pack_hash`` is the trusted
    content identity (``pack_format.pack_hash``).

    A malformed pack does NOT crash the list: its entry carries an ``error``
    key instead. Packs are sorted by ``pack_id`` for determinism. Read-only,
    no mutation, no requests DB.
    """
    packs_dir = Path(
        payload.get("packs_dir")
        or os.environ.get("DONNA_PACKS_DIR")
        or "/Users/donna-broker/broker/packs/available"
    )
    capabilities: dict[str, Any] = ctx["capabilities"]

    packs: list[dict[str, Any]] = []
    if packs_dir.is_dir():
        for child in sorted(packs_dir.iterdir()):
            if not child.is_dir():
                continue
            try:
                pack = pack_format.load_pack(str(child))
            except pack_format.PackFormatError as exc:
                packs.append({
                    "pack_id": child.name,
                    "error": str(exc),
                    "installed": False,
                })
                continue
            installed = all(
                name in capabilities for name in pack.capability_names
            )
            packs.append({
                "pack_id": pack.pack_id,
                "description": pack.description,
                "capabilities": list(pack.capability_names),
                "pack_hash": pack_format.pack_hash(pack),
                "signed": pack.signature is not None,
                "installed": installed,
            })

    packs.sort(key=lambda p: str(p["pack_id"]))
    return {"status": "ok", "packs": packs}


MODE_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "policy-check": _handle_policy_check,
    "request": _handle_request,
    "execute": _handle_execute,
    "status": _handle_status,
    "status-by-code": _handle_status_by_code,
    "list-pending": _handle_list_pending,
    "list-recent": _handle_list_recent,
    "audit-result": _handle_audit_result,
    "cancel": _handle_cancel,
    "verify-audit": _handle_verify_audit,
    "verify-manifests": _handle_verify_manifests,
    "verify-vault": _handle_verify_vault,
    "list-packs": _handle_list_packs,
    "grant-create": _handle_grant_create,
    "grant-list": _handle_grant_list,
    "grant-revoke": _handle_grant_revoke,
    "store-credential": _handle_store_credential,
    "site-check": _handle_site_check,
    "app-approve": _handle_app_approve,
}

# Modes left as explicit not-implemented so the CLI doesn't silently
# accept garbage when these are invoked before their implementations land.
NOT_YET_IMPLEMENTED = frozenset({"reconcile", "rotate-hmac"})

# Startup vault health sweep (§6) runs only on operator-initiated modes.
# Hook-driven modes (policy-check, audit-result) fire many times per
# session; running the sweep in those paths would produce duplicate
# audit noise and unnecessary filesystem I/O on the hot path.
MODES_WITH_STARTUP_SWEEP = frozenset({
    "execute", "request", "verify-manifests", "verify-vault", "list-packs",
})


# ---- main() -------------------------------------------------------------


def _build_ctx(config: dict[str, str], need_manifests: bool) -> dict[str, Any]:
    conn = _open_or_raise(config["db_path"])
    ctx: dict[str, Any] = {
        "conn": conn,
        "audit_dir": config["audit_dir"],
        "queue_dir": config["queue_dir"],
        "responses_dir": config["responses_dir"],
        "config": config,
    }
    if need_manifests:
        try:
            ctx["capabilities"] = validator.load_capabilities(
                config["capabilities_path"]
            )
        except validator.ManifestError as e:
            raise BrokerError("manifest_error", str(e)) from e
        try:
            ctx["mcp_tools"] = validator.load_mcp_tools(config["mcp_tools_path"])
        except validator.ManifestError as e:
            raise BrokerError("manifest_error", str(e)) from e
        ctx["hmac_key"] = _load_hmac_key(config["hmac_key_path"])
    else:
        ctx["capabilities"] = {}
        ctx["mcp_tools"] = {}
        ctx["hmac_key"] = b""
    return ctx


MODES_NEEDING_MANIFESTS = frozenset({
    "request", "execute", "policy-check", "audit-result",
    "verify-manifests", "verify-vault", "list-packs",
    # grant-create validates the target capability against the manifest
    # and needs the broker key to bind the constraints MAC. grant-list
    # and grant-revoke are deliberately manifest-free (revoke must always
    # work, even if a manifest is broken — §3.6).
    "grant-create",
})


def _lazy_reconcile(
    conn: sqlite3.Connection, audit_dir: str, queue_dir: str
) -> None:
    """Sweep expired rows on every CLI invocation.

    Replaces the (unimplemented) `reconcile` mode with in-band
    self-healing — no background process needed. Runs before handler
    dispatch so every CLI call is an opportunity to clear drift.

    Three expiry cases (§5 state machine, §7.6):
      - pending_approval past approval_expires_at → expired
      - approved past execution_expires_at → expired
      - executing past execution_expires_at → failed (exec_window_expired)

    Transitions go through db.transition() so the §6 triggers and
    state-machine guards apply. Audit events are emitted for each
    transition. A transition that races with another writer (e.g. the
    row already moved) is swallowed silently — whoever moved it already
    audited the transition."""
    now_ms = int(time.time() * 1000)

    def _sweep_and_clean(
        select_sql: str,
        from_state: str,
        to_state: str,
        audit_event: str,
        **extra_fields: Any,
    ) -> None:
        stranded = conn.execute(select_sql, (now_ms,)).fetchall()
        for r in stranded:
            rid = r["request_id"]
            try:
                db.transition(conn, rid, from_state, to_state, **extra_fields)
            except db.InvalidTransition:
                continue
            _delete_queue_file(queue_dir, rid)
            audit_mod.write_event(audit_dir, {
                "event": audit_event,
                "request_id": rid,
                "from_state": from_state,
                "reason": extra_fields.get("error_code", "window_expired"),
            })

    _sweep_and_clean(
        "SELECT request_id FROM requests "
        "WHERE state = 'pending_approval' AND approval_expires_at < ?",
        "pending_approval", "expired", "request_expired",
    )
    _sweep_and_clean(
        "SELECT request_id FROM requests "
        "WHERE state = 'approved' AND execution_expires_at < ?",
        "approved", "expired", "request_expired",
    )
    _sweep_and_clean(
        "SELECT request_id FROM requests "
        "WHERE state = 'executing' AND execution_expires_at < ?",
        "executing", "failed", "request_execution_failed",
        error_code="exec_window_expired",
        error_message="execution window expired before completion",
        executed_at=now_ms,
    )


def main(
    argv: list[str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    env: dict[str, str] | None = None,
) -> int:
    """Entry point. Returns 0 on success, 1 on structured error, 2 on
    internal bug (traceback to stderr, structured `{status: "internal"}`
    to stdout so the caller always gets JSON)."""
    argv = argv if argv is not None else sys.argv[1:]
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    if len(argv) < 1 or argv[0] not in MODES:
        err = BrokerError(
            "unknown_mode",
            f"usage: donna-broker <mode>; modes: {sorted(MODES)}",
        )
        stdout.write(json.dumps(_error_response(err)) + "\n")
        return 1
    mode = argv[0]

    if mode in NOT_YET_IMPLEMENTED:
        err = BrokerError(
            "not_implemented", f"mode {mode!r} is not yet implemented",
        )
        stdout.write(json.dumps(_error_response(err)) + "\n")
        return 1

    config = _config_from_env(env)

    try:
        payload = _load_stdin_json(stdin)
    except BrokerError as e:
        stdout.write(json.dumps(_error_response(e)) + "\n")
        return 1

    try:
        ctx = _build_ctx(config, need_manifests=mode in MODES_NEEDING_MANIFESTS)
    except BrokerError as e:
        stdout.write(json.dumps(_error_response(e)) + "\n")
        return 1

    # Lazy reconcile: sweep expired rows before dispatch. Replaces the
    # unimplemented `reconcile` mode with in-band self-healing.
    _lazy_reconcile(ctx["conn"], ctx["audit_dir"], ctx["queue_dir"])

    # §6 startup health sweep: operator-initiated modes only (see
    # MODES_WITH_STARTUP_SWEEP), and only when any capability declares
    # creds. Warnings are emitted via audit; they never block startup (§10).
    if mode in MODES_WITH_STARTUP_SWEEP and ctx.get("capabilities"):
        declared = [
            c.creds.entry for c in ctx["capabilities"].values()
            if c.creds is not None
        ]
        if declared:
            def _audit_writer(evt: dict[str, Any]) -> None:
                audit_mod.write_event(ctx["audit_dir"], evt)

            vault_health.sweep(
                creds_dir=Path(config["creds_dir"]),
                identity_path=Path(config["identity_path"]),
                age_binary=config["age_binary"],
                declared_entries=declared,
                audit_writer=_audit_writer,
            )

    handler = MODE_HANDLERS[mode]
    try:
        response = handler(payload, ctx)
    except BrokerError as e:
        response = _error_response(e)
        response = _append_pending_summary(ctx["conn"], response)
        stdout.write(json.dumps(response) + "\n")
        return 1
    except Exception as e:
        # Internal bug. Structured error to stdout (callers always get
        # JSON); traceback to stderr for operator debugging. §10 spec
        # rule: never stack traces to stdout.
        traceback.print_exc(file=sys.stderr)
        response = {
            "status": "internal",
            "error_code": "internal_error",
            "message": f"{type(e).__name__}: {e}",
        }
        stdout.write(json.dumps(response) + "\n")
        return 2

    response = _append_pending_summary(ctx["conn"], response)
    stdout.write(json.dumps(response) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
