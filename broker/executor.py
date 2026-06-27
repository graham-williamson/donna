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

Fd invariant (Piece C design §3):
  Dispatch may pass an inherited pipe fd to creds-declared capabilities
  via pass_fds. The child discovers that fd number via the
  DONNA_CREDS_FD env var and reads decrypted credential bytes from it.
  Non-creds-declared capabilities never see pass_fds != (). Any other
  passed fd is a broker bug — see
  test_executor.py::test_fd_invariant_across_dispatches.
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

# §3.4 creds payload hard cap — fits within macOS default pipe buffer
# so one os.write() never blocks. Larger creds indicate either a
# format change or a bug.
CREDS_MAX_BYTES = 16 * 1024
# Env var name carrying the inherited pipe fd number for creds-declared
# capabilities. Amended 2026-04-21: the original design pinned this to
# fd 3 via preexec_fn dup2, but Python subprocess's internal error-pipe
# and stdio-setup interactions with preexec_fn + pass_fds produced
# timing-sensitive failures on macOS (child's fd 3 was occasionally
# not our pipe read end). Passing the fd number via env keeps the
# "binary-clean pipe, ps-invisible bytes" security posture while
# avoiding the preexec_fn race. The env var carries a small integer,
# never creds.
CREDS_FD_ENV_VAR = "DONNA_CREDS_FD"

# Second inherited pipe fd, carrying the broker-level model API key for
# capabilities whose executor runs a reasoning agent (creds.model_key: true).
# Delivered exactly like the site credential — vault -> pipe -> child, never
# env. The env var carries only the fd number (a small integer), never the key.
MODEL_KEY_FD_ENV_VAR = "DONNA_MODEL_KEY_FD"
# The broker-level vault entry holding the Anthropic API key. Distinct from the
# per-site credential entry (capability.creds.entry).
MODEL_KEY_ENTRY = "anthropic_api"


# Type protocols so executor doesn't depend on concrete validator /
# requests_db classes. Read-only @property form so frozen dataclasses
# (like requests_db.Request) satisfy the protocol.
class CredsBlockLike(Protocol):
    @property
    def delivery(self) -> str: ...
    @property
    def entry(self) -> str: ...
    @property
    def model_key(self) -> bool: ...


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


def _close_fds(*fds: int | None) -> None:
    """Best-effort close of each non-None fd; OSError is swallowed. Used on the
    creds/model-key pipe cleanup paths where a failed close must not mask the
    structured failure being returned."""
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _sanitised_env(extras: dict[str, str] | None = None) -> dict[str, str]:
    """PATH-only baseline plus optional capability-bound extras.

    Extras are narrowly scoped: today only `DONNA_CREDS_FD` when a
    creds-declared capability is being dispatched (§3). The fd number
    is not sensitive — the pipe contents are only accessible to the fd
    holder, and the fd itself dies when the subprocess exits. Credentials
    never enter env.

    HOME and TMPDIR are passed through because browser-automation
    executors (Playwright channel="chrome") need HOME to locate Chrome's
    profile directory. Values are taken from the broker's own env at
    call time — both are set by launchd for the donna-broker service
    account and contain no secrets.
    """
    env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    for key in ("HOME", "TMPDIR"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    if extras:
        env.update(extras)
    return env


def _emit(audit_writer: AuditWriter | None, event: dict[str, Any]) -> None:
    if audit_writer is None:
        return
    try:
        audit_writer(event)
    except Exception:
        # Never let audit failure block execution. Spec §10 applies.
        pass


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

    # §3 creds wiring — only when capability opts in.
    cred_bytes: bytes | None = None
    r_fd: int | None = None
    w_fd: int | None = None
    pass_fds: tuple[int, ...] = ()
    env_extras: dict[str, str] | None = None
    # Second fd: broker-level model API key, for agent-running executors only.
    mk_bytes: bytes | None = None
    mk_r_fd: int | None = None
    mk_w_fd: int | None = None

    if capability.creds is not None:
        # execute() guards creds_config presence, but defence-in-depth:
        assert creds_config is not None
        from broker import creds as _creds
        try:
            cred_bytes = _creds.unlock_creds(
                capability.creds.entry,
                creds_dir=str(creds_config.creds_dir),
                identity_path=str(creds_config.identity_path),
                age_binary=creds_config.age_binary,
                timeout_seconds=creds_config.timeout_seconds,
                audit_writer=audit_writer,
            )
        except _creds.CredsError as ce:
            return _finalise_failure(
                state_conn, request, audit_writer,
                ce.error_code, ce.message,
            )

        if len(cred_bytes) > CREDS_MAX_BYTES:
            del cred_bytes
            return _finalise_failure(
                state_conn, request, audit_writer,
                "creds_too_large",
                f"creds exceed {CREDS_MAX_BYTES} bytes",
            )

        try:
            r_fd, w_fd = os.pipe()
            os.set_inheritable(r_fd, True)
            os.set_inheritable(w_fd, False)
        except OSError as oe:
            if r_fd is not None:
                os.close(r_fd)
            if w_fd is not None:
                os.close(w_fd)
            del cred_bytes
            return _finalise_failure(
                state_conn, request, audit_writer,
                "creds_pipe_error",
                f"os.pipe failed: {type(oe).__name__}",
            )

        pass_fds = (r_fd,)
        env_extras = {CREDS_FD_ENV_VAR: str(r_fd)}

        # Second inherited pipe: the broker-level model API key, delivered the
        # same way (vault -> pipe -> child, never env). Only when the capability
        # declares creds.model_key. If anything here fails, the already-open
        # creds pipe is closed before bailing.
        if capability.creds.model_key:
            from broker import creds as _creds2
            try:
                mk_bytes = _creds2.unlock_creds(
                    MODEL_KEY_ENTRY,
                    creds_dir=str(creds_config.creds_dir),
                    identity_path=str(creds_config.identity_path),
                    age_binary=creds_config.age_binary,
                    timeout_seconds=creds_config.timeout_seconds,
                    audit_writer=audit_writer,
                )
            except _creds2.CredsError as ce:
                _close_fds(r_fd, w_fd)
                del cred_bytes
                return _finalise_failure(
                    state_conn, request, audit_writer, ce.error_code, ce.message,
                )
            if len(mk_bytes) > CREDS_MAX_BYTES:
                del mk_bytes
                _close_fds(r_fd, w_fd)
                del cred_bytes
                return _finalise_failure(
                    state_conn, request, audit_writer,
                    "creds_too_large",
                    f"model key exceeds {CREDS_MAX_BYTES} bytes",
                )
            try:
                mk_r_fd, mk_w_fd = os.pipe()
                os.set_inheritable(mk_r_fd, True)
                os.set_inheritable(mk_w_fd, False)
            except OSError as oe:
                _close_fds(r_fd, w_fd, mk_r_fd, mk_w_fd)
                del cred_bytes
                del mk_bytes
                return _finalise_failure(
                    state_conn, request, audit_writer,
                    "creds_pipe_error",
                    f"os.pipe failed: {type(oe).__name__}",
                )
            pass_fds = (r_fd, mk_r_fd)
            env_extras = {
                CREDS_FD_ENV_VAR: str(r_fd),
                MODEL_KEY_FD_ENV_VAR: str(mk_r_fd),
            }

    # Spawn + communicate block. Ephemeral workdir per §9.2.
    workdir = Path(tempfile.mkdtemp(prefix=f"donna-exec-{uuid.uuid4().hex}-"))
    proc: subprocess.Popen | None = None  # type: ignore[type-arg]
    try:
        try:
            proc = subprocess.Popen(
                [capability.executor_target],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_sanitised_env(env_extras),
                cwd=str(workdir),
                pass_fds=pass_fds,
                close_fds=True,
            )
        except FileNotFoundError:
            # Clean up pipe fds before returning.
            _close_fds(r_fd, w_fd, mk_r_fd, mk_w_fd)
            if cred_bytes is not None:
                del cred_bytes
            if mk_bytes is not None:
                del mk_bytes
            return _finalise_failure(
                state_conn, request, audit_writer,
                "executor_missing",
                f"binary {capability.executor_target!r} not found",
            )
        except Exception as e:
            _close_fds(r_fd, w_fd, mk_r_fd, mk_w_fd)
            if cred_bytes is not None:
                del cred_bytes
            if mk_bytes is not None:
                del mk_bytes
            # §7.2 — type-only detail.
            return _finalise_failure(
                state_conn, request, audit_writer,
                "executor_spawn_error",
                type(e).__name__,
                extra={"exception_type": type(e).__name__},
            )

        # Popen succeeded. Parent no longer needs the read ends.
        if r_fd is not None:
            os.close(r_fd)
            r_fd = None
        if mk_r_fd is not None:
            os.close(mk_r_fd)
            mk_r_fd = None

        # Write creds and close write end, so child sees EOF.
        # §3.3 — broker guarantees delivery attempt, not consumption.
        # If the child exited before reading (EPIPE), that's their
        # exit status to answer for; we don't treat the BrokenPipe as
        # a broker failure here.
        if cred_bytes is not None and w_fd is not None:
            try:
                try:
                    os.write(w_fd, cred_bytes)
                except BrokenPipeError:
                    pass
            finally:
                os.close(w_fd)
                w_fd = None
                del cred_bytes
                cred_bytes = None

        # Same delivery for the model key on its own pipe.
        if mk_bytes is not None and mk_w_fd is not None:
            try:
                try:
                    os.write(mk_w_fd, mk_bytes)
                except BrokenPipeError:
                    pass
            finally:
                os.close(mk_w_fd)
                mk_w_fd = None
                del mk_bytes
                mk_bytes = None

        try:
            stdout, stderr = proc.communicate(
                input=stdin_payload, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1.0)
            except Exception:
                pass
            return _finalise_failure(
                state_conn, request, audit_writer,
                "executor_timeout",
                f"timed out after {timeout_seconds}s",
                extra={"timeout_seconds": timeout_seconds},
            )
        exit_code = proc.returncode
    finally:
        # Defensive: if we reach here with any fds still open (shouldn't
        # happen in the happy path but belt-and-braces for the control
        # flow), close them.
        _close_fds(r_fd, w_fd, mk_r_fd, mk_w_fd)
        shutil.rmtree(workdir, ignore_errors=True)

    # ---- From here down: existing logic (stderr audit, exit check,
    # stdout parsing, terminal transition) unchanged from Task 4.

    if stderr:
        # §7.1 — stderr body is NEVER included in the audit event.
        # Any capability subprocess that accidentally prints a
        # credential or other secret to stderr must not leak it into
        # the hash-chained audit log. Length + SHA-256 only. Matching
        # failures can be correlated by hash; actual stderr content
        # is out of reach of Donna and deliberately so.
        _emit(audit_writer, {
            "event": "executor_stderr",
            "request_id": request.request_id,
            "stderr_bytes": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        })

    if exit_code != 0:
        # Try to recover a structured error from stdout before discarding it.
        # The executor's fail() writes {error_code, detail} to stdout then exits 1.
        error_code = "executor_crashed"
        error_detail = f"exit code {exit_code}"
        if stdout:
            try:
                err_json: Any = json.loads(stdout)
                if isinstance(err_json, dict):
                    error_code = str(err_json.get("error_code") or error_code)
                    error_detail = str(err_json.get("detail") or error_detail)
            except (json.JSONDecodeError, Exception):
                pass
        return _finalise_failure(
            state_conn, request, audit_writer,
            error_code,
            error_detail,
            extra={"exit_code": exit_code},
        )

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


def run_probe(
    binary: str,
    payload: dict[str, Any],
    cred_bytes: bytes,
    timeout_seconds: float = DEFAULT_EXECUTOR_TIMEOUT_SECONDS,
    audit_writer: AuditWriter | None = None,
) -> dict[str, Any]:
    """Run a read-only probe subprocess (e.g. a site-check login test)
    with credentials delivered over the same DONNA_CREDS_FD pipe that
    capability executors use. No request row, no state machine — probes
    are human-tap reads, not broker actions.

    `payload` is written to the child's stdin as
    {"capability": <probe name>, "params": {...}} so probe binaries
    share the capability-executor stdin contract. Returns the parsed
    stdout object on exit 0; on failure returns a structured
    {"status": "error", "error_code", "detail"} — it never raises for
    child-side problems (fail-closed but reportable). The cred bytes
    are written to the pipe and dropped; they never appear in the
    return value or audit."""
    if len(cred_bytes) > CREDS_MAX_BYTES:
        return {
            "status": "error",
            "error_code": "creds_too_large",
            "detail": f"creds exceed {CREDS_MAX_BYTES} bytes",
        }

    stdin_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    r_fd: int | None = None
    w_fd: int | None = None
    try:
        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        os.set_inheritable(w_fd, False)
    except OSError as oe:
        if r_fd is not None:
            os.close(r_fd)
        if w_fd is not None:
            os.close(w_fd)
        return {
            "status": "error",
            "error_code": "creds_pipe_error",
            "detail": f"os.pipe failed: {type(oe).__name__}",
        }

    workdir = Path(tempfile.mkdtemp(prefix=f"donna-probe-{uuid.uuid4().hex}-"))
    try:
        try:
            proc = subprocess.Popen(
                [binary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_sanitised_env({CREDS_FD_ENV_VAR: str(r_fd)}),
                cwd=str(workdir),
                pass_fds=(r_fd,),
                close_fds=True,
            )
        except FileNotFoundError:
            return {
                "status": "error",
                "error_code": "probe_missing",
                "detail": f"binary {binary!r} not found",
            }
        except Exception as e:
            return {
                "status": "error",
                "error_code": "probe_spawn_error",
                "detail": type(e).__name__,
            }

        os.close(r_fd)
        r_fd = None
        try:
            try:
                os.write(w_fd, cred_bytes)
            except BrokenPipeError:
                pass
        finally:
            os.close(w_fd)
            w_fd = None
            del cred_bytes

        try:
            stdout, stderr = proc.communicate(
                input=stdin_payload, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1.0)
            except Exception:
                pass
            return {
                "status": "error",
                "error_code": "probe_timeout",
                "detail": f"timed out after {timeout_seconds}s",
            }
    finally:
        if r_fd is not None:
            try:
                os.close(r_fd)
            except OSError:
                pass
        if w_fd is not None:
            try:
                os.close(w_fd)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)

    if stderr:
        # Same §7.1 posture as capability executors: stderr content may
        # carry secrets, so the audit gets length + hash only.
        _emit(audit_writer, {
            "event": "probe_stderr",
            "probe": payload.get("capability", ""),
            "stderr_bytes": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        })

    parsed: Any = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None

    if proc.returncode != 0:
        error_code = "probe_failed"
        detail = f"exit code {proc.returncode}"
        if isinstance(parsed, dict):
            error_code = str(parsed.get("error_code") or error_code)
            detail = str(parsed.get("detail") or detail)
        return {"status": "error", "error_code": error_code, "detail": detail}

    if not isinstance(parsed, dict):
        return {
            "status": "error",
            "error_code": "probe_output_invalid",
            "detail": "probe stdout was not a JSON object",
        }
    return parsed


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
