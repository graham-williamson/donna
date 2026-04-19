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

import json
import os
import sqlite3
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from broker import audit as audit_mod
from broker import canonicalize
from broker import executor
from broker import policy
from broker import requests_db as db
from broker import resolver
from broker import validator


MODES = frozenset({
    "request", "policy-check", "execute", "cancel", "reconcile",
    "status", "status-by-code", "list-pending", "list-recent",
    "audit-result", "rotate-hmac", "verify-audit",
})


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
    capabilities, returns a summary the hook can show on a block."""
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
        # medium / high fall through to block-with-summary pattern below.

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise BrokerError("invalid_input", "params must be an object")

    summary = resolver.policy_check_mode(tool_name, params)
    return {
        "status": "ok",
        "decision": "block",
        "tool_name": tool_name,
        "reason": "medium/high-risk tool requires approval via broker request",
        "summary": summary,
    }


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
        "WHERE state IN ('pending_approval','approved') "
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
    if row.approval_hmac is None or not policy.verify_hmac(
        ctx["hmac_key"], creation_msg, row.approval_hmac,
    ):
        db.quarantine(conn, row.request_id, "hmac_mismatch", "creation HMAC")
        audit_mod.write_event(ctx["audit_dir"], {
            "event": "audit.hmac_mismatch",
            "request_id": row.request_id,
        })
        raise BrokerError("integrity_failed", "HMAC verification failed")

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

    outcome = executor.execute(
        cap, row, params, conn, audit_writer=audit_writer,
    )
    return {
        "status": outcome.state,
        "request_id": row.request_id,
        "result": outcome.result,
        "error_code": outcome.error_code,
        "error_message": outcome.error_message,
    }


def _handle_audit_result(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """PostToolUse hook. For low-risk reads: emit mcp_tool_allowed. For
    medium+ successful runs on tracked rows: transition executing →
    succeeded."""
    tool_name = payload.get("tool_name") or ""
    tool_outcome = payload.get("outcome") or "succeeded"
    request_id = payload.get("request_id")  # optional, for tracked rows

    mcp_tools: dict[str, str] = ctx["mcp_tools"]
    risk = mcp_tools.get(tool_name, "unknown")
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "mcp_tool_allowed" if tool_outcome == "succeeded" else "mcp_tool_blocked",
        "tool": tool_name,
        "risk": risk,
        "outcome": tool_outcome,
        "request_id": request_id,
    })
    if request_id and isinstance(request_id, str):
        row = db.get_request(ctx["conn"], request_id)
        if row is not None and row.state == "executing":
            if tool_outcome == "succeeded":
                db.transition(
                    ctx["conn"], request_id, "executing", "succeeded",
                    executed_at=int(time.time() * 1000),
                )
                audit_mod.write_event(ctx["audit_dir"], {
                    "event": "request_execution_succeeded",
                    "request_id": request_id,
                })
            else:
                db.transition(
                    ctx["conn"], request_id, "executing", "failed",
                    error_code="mcp_tool_reported_failure",
                    executed_at=int(time.time() * 1000),
                )
                audit_mod.write_event(ctx["audit_dir"], {
                    "event": "request_execution_failed",
                    "request_id": request_id,
                })
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
    if r.state not in {"pending_approval", "approved"}:
        raise BrokerError(
            "invalid_state",
            f"cannot cancel from state {r.state!r}",
        )
    db.transition(ctx["conn"], rid, r.state, "cancelled")
    audit_mod.write_event(ctx["audit_dir"], {
        "event": "request_cancelled",
        "request_id": rid,
    })
    return {"status": "cancelled", "request_id": rid}


def _handle_verify_audit(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    result = audit_mod.verify_chain(ctx["audit_dir"])
    if result is None:
        return {"status": "ok", "verified": True}
    return {"status": "integrity_break", "verified": False, "break": result}


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
}

# Modes left as explicit not-implemented so the CLI doesn't silently
# accept garbage when these are invoked before their implementations land.
NOT_YET_IMPLEMENTED = frozenset({"reconcile", "rotate-hmac"})


# ---- main() -------------------------------------------------------------


def _build_ctx(config: dict[str, str], need_manifests: bool) -> dict[str, Any]:
    conn = _open_or_raise(config["db_path"])
    ctx: dict[str, Any] = {
        "conn": conn,
        "audit_dir": config["audit_dir"],
        "queue_dir": config["queue_dir"],
        "responses_dir": config["responses_dir"],
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
})


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
