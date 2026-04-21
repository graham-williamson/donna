"""Capability-bound executor dispatch.

Spec: security-v1.1 §8 (execution binding absolute), §13.4 (revalidation),
§11 (replay semantics), §5 (executing → terminal states).

Contract:
  - Dispatch by capability name only. No fuzzy matching, no substitution.
    The approved row's `capability` field is re-checked against the
    Capability object passed in — mismatch raises immediately.
  - Revalidate before execute when the capability declares
    `revalidate.handler` (§13.4). Handler lookup is caller-supplied
    (revalidate_handlers map) — this keeps executor decoupled from
    capability-specific imports.
  - Durable start (§11 rule 2): transition row → executing, audit
    `request_execution_started`, commit. Only then spawn the executor.
  - Subprocess executor: sanitised env, closed fds, ephemeral cwd,
    capability-configurable timeout.
  - MCP-tool executor: returns metadata describing the tool to re-run;
    the actual MCP invocation happens on Donna's next turn and the
    PostToolUse audit-result hook transitions executing → succeeded.
  - Every failure path transitions to a terminal state and emits a
    structured audit event.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


# §9.2-style sandbox reused here for subprocess executors.
DEFAULT_EXECUTOR_TIMEOUT_SECONDS = 120.0
MAX_EXECUTOR_STDOUT_BYTES = 256 * 1024
MAX_EXECUTOR_STDERR_BYTES = 16 * 1024
STDERR_TRUNCATION_MARKER = b"\n...[truncated]"


# Type protocols so executor doesn't depend on concrete validator /
# requests_db classes. Read-only @property form so frozen dataclasses
# (like requests_db.Request) satisfy the protocol.
class CredsBlockLike(Protocol):
    @property
    def delivery(self) -> str: ...
    @property
    def entry(self) -> str: ...


class CapabilityLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def executor_type(self) -> str: ...
    @property
    def executor_target(self) -> str: ...
    @property
    def revalidate(self) -> dict[str, Any]: ...
    @property
    def creds(self) -> CredsBlockLike | None: ...


class RequestLike(Protocol):
    @property
    def request_id(self) -> str: ...
    @property
    def capability(self) -> str: ...
    @property
    def state(self) -> str: ...


AuditWriter = Callable[[dict[str, Any]], Any]
# A revalidator is a callable that takes (capability_name, params,
# arguments_from_manifest) and returns (ok: bool, detail: str).
Revalidator = Callable[[str, dict[str, Any], list[str]], tuple[bool, str]]


class ExecutionError(Exception):
    """Raised to signal structured failure before the state machine
    could be advanced. Callers map this to `failed` with error_code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ExecutionOutcome:
    state: str  # 'succeeded' | 'failed' | 'executing' (MCP-tool metadata path)
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CredsConfig:
    """§5 creds injection runtime config. Constructed by main.py from
    broker constants; passed to execute() for every dispatch that may
    touch a creds-declared capability. Never wired as module-global
    state."""
    creds_dir: Path
    identity_path: Path
    age_binary: str = "age"
    timeout_seconds: float = 10.0


# ---- helpers ------------------------------------------------------------


def _sanitised_env() -> dict[str, str]:
    """PATH only. Capability-specific credentials are injected separately
    in Phase 2+ via the age vault — not covered here."""
    return {"PATH": "/usr/bin:/bin"}


def _truncate(buf: bytes, cap: int) -> bytes:
    if len(buf) <= cap:
        return buf
    keep = cap - len(STDERR_TRUNCATION_MARKER)
    return buf[:keep] + STDERR_TRUNCATION_MARKER


def _emit(audit_writer: AuditWriter | None, event: dict[str, Any]) -> None:
    if audit_writer is None:
        return
    try:
        audit_writer(event)
    except Exception:
        # Never let audit failure block execution. Spec §10 applies.
        pass


def _run_executor_subprocess(
    binary: str,
    stdin_payload: bytes,
    timeout_seconds: float,
) -> tuple[int, bytes, bytes]:
    workdir = Path(tempfile.mkdtemp(
        prefix=f"donna-exec-{uuid.uuid4().hex}-"
    ))
    try:
        proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_sanitised_env(),
            cwd=str(workdir),
            pass_fds=(),
            close_fds=True,
        )
        try:
            stdout, stderr = proc.communicate(
                input=stdin_payload, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except Exception:
                stdout, stderr = b"", b""
            raise
        return proc.returncode, stdout, stderr
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---- revalidation (§13.4) ------------------------------------------------


def _run_revalidation(
    capability: CapabilityLike,
    params: dict[str, Any],
    revalidate_handlers: dict[str, Revalidator],
) -> None:
    """Per §13.4. Raises ExecutionError(error_code='stale') on failure.
    No-op when the capability's revalidate declares not_applicable.
    """
    reval = capability.revalidate or {}
    if "not_applicable" in reval:
        return
    handler_name = reval.get("handler")
    if handler_name is None:
        return  # low-risk capabilities may omit revalidate entirely
    handler = revalidate_handlers.get(handler_name)
    if handler is None:
        raise ExecutionError(
            "revalidation_handler_missing",
            f"no handler registered for {handler_name!r}",
        )
    arguments = reval.get("arguments", []) or []
    if not isinstance(arguments, list):
        raise ExecutionError(
            "revalidation_handler_bad_arguments",
            f"{handler_name!r} arguments must be a list, got {type(arguments).__name__}",
        )
    ok, detail = handler(capability.name, params, list(arguments))
    if not ok:
        raise ExecutionError("stale", detail)


# ---- execute (§5, §8, §11) ----------------------------------------------


def execute(
    capability: CapabilityLike,
    request: RequestLike,
    params: dict[str, Any],
    state_conn: Any,
    audit_writer: AuditWriter | None = None,
    revalidate_handlers: Optional[dict[str, Revalidator]] = None,
    subprocess_timeout_seconds: float = DEFAULT_EXECUTOR_TIMEOUT_SECONDS,
    creds_config: CredsConfig | None = None,
) -> ExecutionOutcome:
    """Run a capability against an approved row.

    Order of operations (§11 rule 2 "durable start" + §13.4):
      1. Capability-binding check: approved row's capability must match
         the Capability object passed in. No aliasing.
      2. Durable start: transition approved → executing, audit
         `request_execution_started`, commit. No spawn yet.
      3. Revalidation (§13.4): if declared, run it. Stale or handler
         missing → row → failed{stale}. Revalidation runs AFTER durable
         start because §5 has no direct approved→failed transition;
         this makes the "failed{reason: stale}" outcome § spec-faithful.
      4. Dispatch: subprocess → spawn; mcp_tool → metadata handoff (row
         stays executing for the PostToolUse hook to close out).
      5. On exit: executing → succeeded/failed with structured audit.

    Parameters are passed separately rather than re-parsed from the
    row so the caller owns canonicalisation concerns (§7.1).
    """
    # 1. Capability binding. No aliasing, no substitution.
    if request.capability != capability.name:
        raise ExecutionError(
            "capability_mismatch",
            f"row capability {request.capability!r} does not match "
            f"{capability.name!r}",
        )

    if revalidate_handlers is None:
        revalidate_handlers = {}

    # 2. Durable start. Flip approved → executing + audit before any
    # further work. PID is capability-specific (subprocess only) and is
    # recorded at spawn, per §11 rule 2.
    _transition_approved_to_executing(state_conn, request)
    _emit(audit_writer, {
        "event": "request_execution_started",
        "request_id": request.request_id,
        "capability": capability.name,
        "executor_type": capability.executor_type,
    })

    # 3. Revalidation. Failure → failed{stale}.
    try:
        _run_revalidation(capability, params, revalidate_handlers)
    except ExecutionError as e:
        return _finalise_failure(
            state_conn, request, audit_writer, e.error_code, e.message,
        )

    # §5.2 fail-closed guard: capability declares creds but no config
    # was threaded into execute(). No unlock attempt, no spawn.
    if capability.creds is not None and creds_config is None:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "creds_config_missing",
            f"capability {capability.name!r} declares creds but "
            f"execute() was called without creds_config",
        )

    # 4. Dispatch by executor type. No fuzzy matching.
    if capability.executor_type == "subprocess":
        return _execute_subprocess(
            capability, request, params, state_conn, audit_writer,
            subprocess_timeout_seconds, creds_config,
        )
    if capability.executor_type == "mcp_tool":
        return _execute_mcp_tool(
            capability, request, params, audit_writer,
        )
    # Unreachable for manifests that passed validator, but defence-in-
    # depth: fail closed.
    return _finalise_failure(
        state_conn, request, audit_writer,
        "unknown_executor_type",
        f"executor_type {capability.executor_type!r}",
    )


def _execute_subprocess(
    capability: CapabilityLike,
    request: RequestLike,
    params: dict[str, Any],
    state_conn: Any,
    audit_writer: AuditWriter | None,
    timeout_seconds: float,
    creds_config: CredsConfig | None,
) -> ExecutionOutcome:
    stdin_payload = json.dumps(
        {"capability": capability.name, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        exit_code, stdout, stderr = _run_executor_subprocess(
            capability.executor_target, stdin_payload, timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_timeout",
            f"timed out after {timeout_seconds}s",
            extra={"timeout_seconds": timeout_seconds},
        )
    except FileNotFoundError:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_missing",
            f"binary {capability.executor_target!r} not found",
        )
    except Exception as e:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_spawn_error",
            f"{type(e).__name__}: {e}",
        )

    if stderr:
        truncated = _truncate(stderr, MAX_EXECUTOR_STDERR_BYTES)
        _emit(audit_writer, {
            "event": "executor_stderr",
            "request_id": request.request_id,
            "stderr_bytes": len(stderr),
            "stderr": truncated.decode("utf-8", errors="replace"),
        })

    if exit_code != 0:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_crashed",
            f"exit code {exit_code}",
            extra={"exit_code": exit_code},
        )

    # Parse stdout as JSON result; truncate oversized payloads.
    if len(stdout) > MAX_EXECUTOR_STDOUT_BYTES:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_output_too_large",
            f"stdout {len(stdout)} bytes exceeds {MAX_EXECUTOR_STDOUT_BYTES}",
        )
    try:
        # json.loads returns Any at runtime; don't narrow the type
        # annotation so the isinstance check below isn't marked
        # unreachable by mypy.
        parsed: Any = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as e:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_output_invalid_json",
            str(e),
        )
    if not isinstance(parsed, dict):
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_output_not_object",
            f"executor returned {type(parsed).__name__}",
        )
    result: dict[str, Any] = parsed

    # Success path.
    _transition_executing_to_succeeded(
        state_conn, request, result_json=json.dumps(result),
        executed_at=int(time.time() * 1000),
    )
    _emit(audit_writer, {
        "event": "request_execution_succeeded",
        "request_id": request.request_id,
        "capability": capability.name,
    })
    return ExecutionOutcome(state="succeeded", result=result)


def _execute_mcp_tool(
    capability: CapabilityLike,
    request: RequestLike,
    params: dict[str, Any],
    audit_writer: AuditWriter | None,
) -> ExecutionOutcome:
    """MCP-tool dispatch is a metadata handoff. Donna re-runs the
    MCP tool on her next turn; the PostToolUse audit-result hook
    transitions executing → succeeded on completion. The row stays
    `executing` here; this function does NOT write a terminal state."""
    _emit(audit_writer, {
        "event": "request_execution_mcp_tool_handoff",
        "request_id": request.request_id,
        "capability": capability.name,
        "tool": capability.executor_target,
    })
    return ExecutionOutcome(
        state="executing",
        result={
            "executor_type": "mcp_tool",
            "tool": capability.executor_target,
            "params": params,
        },
    )


# ---- state transition helpers -------------------------------------------
#
# These wrap broker.requests_db's transition() calls. Kept local so the
# executor unit-tests can use a fake state_conn without pulling the
# full requests_db integration.


def _transition_approved_to_executing(
    state_conn: Any, request: RequestLike
) -> None:
    # Late import keeps import graph shallow for tests that monkey-patch.
    from broker.requests_db import transition
    transition(state_conn, request.request_id, "approved", "executing")


def _transition_executing_to_succeeded(
    state_conn: Any,
    request: RequestLike,
    result_json: str,
    executed_at: int,
) -> None:
    from broker.requests_db import transition
    transition(
        state_conn, request.request_id, "executing", "succeeded",
        result_json=result_json,
        executed_at=executed_at,
    )


def _transition_executing_to_failed(
    state_conn: Any,
    request: RequestLike,
    error_code: str,
    error_message: str,
) -> None:
    from broker.requests_db import transition
    transition(
        state_conn, request.request_id, "executing", "failed",
        error_code=error_code,
        error_message=error_message,
        executed_at=int(time.time() * 1000),
    )


def _finalise_failure(
    state_conn: Any,
    request: RequestLike,
    audit_writer: AuditWriter | None,
    error_code: str,
    error_message: str,
    extra: dict[str, Any] | None = None,
) -> ExecutionOutcome:
    """Audit + transition executing → failed + return outcome."""
    event: dict[str, Any] = {
        "event": "request_execution_failed",
        "request_id": request.request_id,
        "reason": error_code,
        "detail": error_message,
    }
    if extra:
        event.update(extra)
    _emit(audit_writer, event)
    _transition_executing_to_failed(state_conn, request, error_code, error_message)
    return ExecutionOutcome(
        state="failed", error_code=error_code, error_message=error_message,
    )
