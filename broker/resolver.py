"""Mode-aware resolver with subprocess isolation for enrichment.

Spec: security-v1.1 §9 (policy-check purity + resolver isolation),
§12.5 (field provenance), §12.6 (approval prompt content),
§7.7 (resolver-returned strings are attacker-tainted for display).

Two entry modes:
  - policy_check_mode: pure, local-only, deterministic, hot-path.
    Must NOT spawn subprocesses, touch the network, or read anything
    outside the capability metadata the caller already has in hand.
  - request_mode: may enrich. If the capability declares a resolver
    binary, spawn it as a subprocess with §9.2 isolation guarantees:
        * sanitised env (PATH only; HMAC_KEY, BROKER_DB_PATH, any
          *_TOKEN absent)
        * pass_fds=()  — resolver cannot inherit broker file descriptors
        * cwd: ephemeral /tmp/donna-resolver-<uuid>, removed after run
        * stdin: validated JSON {capability, params}
        * stdout: JSON object, schema-validated, ≤64KB
        * stderr: captured, truncated to 4KB with a marker; logged via
          audit_writer as `resolver_stderr`
        * timeout: capability-configurable, default 10s

Enrichment failure (timeout, non-zero exit, bad JSON, schema mismatch)
is non-blocking: the caller gets a degraded summary and an
`audit.enrichment_failed` event is logged. Approval proceeds.

Provenance tagging (§7.7, §12.5):
  - Strings from the resolver subprocess → `provenance: "donna"`
    (attacker-tainted if the resolver touched the network).
  - Capability name, param keys, boolean/integer/enum fields validated
    against a schema → `provenance: "broker"`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional


# §9.2 caps and defaults.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 4 * 1024
STDERR_TRUNCATION_MARKER = b"\n...[truncated]"

# §9.2 secret env var names that must NEVER be inherited by a resolver.
# If any of these exist in the spawning broker's env, the sanitised env
# omits them. This is an explicit allowlist (PATH only) in practice;
# the set exists to document intent and for audit assertions in tests.
SECRET_ENV_VARS = frozenset({
    "HMAC_KEY",
    "BROKER_DB_PATH",
    "TELEGRAM_BOT_TOKEN",
    "NOTION_TOKEN",
    "GMAIL_TOKEN",
    "GMAIL_REFRESH_TOKEN",
    "GCAL_TOKEN",
    "GCAL_REFRESH_TOKEN",
    "AGE_PRIVATE_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
})


AuditWriter = Callable[[dict[str, Any]], Any]


# ---- policy_check_mode (§9.1) -------------------------------------------


def policy_check_mode(
    capability_name: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Pure summary render for the hook path.

    No subprocess, no network, no filesystem beyond what the caller
    already has in memory. Deterministic given (capability_name, params).

    Returns {fields, resolved_summary} where fields is a list of
    provenance-tagged {label, value, provenance} dicts.
    """
    if not isinstance(capability_name, str):
        raise TypeError("capability_name must be str")
    if not isinstance(params, dict):
        raise TypeError("params must be dict")

    param_keys = sorted(params.keys())
    fields: list[dict[str, str]] = [
        {
            "label": "capability",
            "value": capability_name,
            "provenance": "broker",
        },
        {
            "label": "params",
            "value": ", ".join(param_keys) if param_keys else "<none>",
            "provenance": "broker",
        },
    ]
    summary = f"{capability_name} ({len(param_keys)} param(s))"
    return {"fields": fields, "resolved_summary": summary}


# ---- request_mode (§9.2) -------------------------------------------------


def _sanitised_env() -> dict[str, str]:
    """PATH only. No secret-bearing variables, no user environment."""
    return {"PATH": "/usr/bin:/bin"}


def _run_resolver_subprocess(
    resolver_binary: str,
    stdin_payload: bytes,
    timeout_seconds: float,
) -> tuple[int, bytes, bytes]:
    """Spawn with §9.2 isolation. Returns (exit_code, stdout, stderr)."""
    workdir = Path(tempfile.mkdtemp(
        prefix=f"donna-resolver-{uuid.uuid4().hex}-"
    ))
    try:
        proc = subprocess.Popen(
            [resolver_binary],
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


def _truncate_stderr(stderr: bytes) -> bytes:
    if len(stderr) <= MAX_STDERR_BYTES:
        return stderr
    keep = MAX_STDERR_BYTES - len(STDERR_TRUNCATION_MARKER)
    return stderr[:keep] + STDERR_TRUNCATION_MARKER


def _emit(
    audit_writer: Optional[AuditWriter], event: dict[str, Any]
) -> None:
    if audit_writer is None:
        return
    try:
        audit_writer(event)
    except Exception:
        # Enrichment failure is non-blocking (§10); that applies
        # transitively to audit of that failure.
        pass


def _parse_resolver_output(
    stdout: bytes, output_schema: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Parsed + schema-validated output, or None on any failure."""
    if len(stdout) > MAX_STDOUT_BYTES:
        return None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if output_schema is not None:
        try:
            # Lazy import so pure policy_check_mode paths never load
            # jsonschema — keeps the hot path lean.
            from jsonschema import Draft7Validator
            Draft7Validator(output_schema).validate(parsed)
        except Exception:
            return None
    return parsed


def request_mode(
    capability_name: str,
    params: dict[str, Any],
    audit_writer: Optional[AuditWriter] = None,
    resolver_binary: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_schema: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Full resolver. When `resolver_binary` is declared, spawns the
    subprocess with §9.2 isolation; otherwise returns the same shape as
    `policy_check_mode`.

    Always non-blocking on failure: returns a degraded summary and emits
    `audit.enrichment_failed` when the resolver can't run.
    """
    if not isinstance(capability_name, str):
        raise TypeError("capability_name must be str")
    if not isinstance(params, dict):
        raise TypeError("params must be dict")

    base = policy_check_mode(capability_name, params)

    if resolver_binary is None:
        return base

    if not Path(resolver_binary).exists():
        _emit(audit_writer, {
            "event": "audit.enrichment_failed",
            "capability": capability_name,
            "reason": "resolver_binary_missing",
            "resolver_binary": resolver_binary,
        })
        base["resolved_summary"] += " (enrichment: resolver missing)"
        return base

    stdin_payload = json.dumps(
        {"capability": capability_name, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        exit_code, stdout, stderr = _run_resolver_subprocess(
            resolver_binary, stdin_payload, timeout_seconds
        )
    except subprocess.TimeoutExpired:
        _emit(audit_writer, {
            "event": "audit.enrichment_failed",
            "capability": capability_name,
            "reason": "timeout",
            "timeout_seconds": timeout_seconds,
        })
        base["resolved_summary"] += " (enrichment: timeout)"
        return base
    except Exception as e:
        _emit(audit_writer, {
            "event": "audit.enrichment_failed",
            "capability": capability_name,
            "reason": "spawn_error",
            "error_type": type(e).__name__,
        })
        base["resolved_summary"] += " (enrichment: spawn error)"
        return base

    # Audit stderr regardless of exit code.
    if stderr:
        truncated = _truncate_stderr(stderr)
        _emit(audit_writer, {
            "event": "resolver_stderr",
            "capability": capability_name,
            "stderr_bytes": len(stderr),
            "stderr": truncated.decode("utf-8", errors="replace"),
        })

    if exit_code != 0:
        _emit(audit_writer, {
            "event": "audit.enrichment_failed",
            "capability": capability_name,
            "reason": "non_zero_exit",
            "exit_code": exit_code,
        })
        base["resolved_summary"] += " (enrichment: failed)"
        return base

    parsed = _parse_resolver_output(stdout, output_schema)
    if parsed is None:
        _emit(audit_writer, {
            "event": "audit.enrichment_failed",
            "capability": capability_name,
            "reason": "invalid_output",
        })
        base["resolved_summary"] += " (enrichment: invalid output)"
        return base

    # Merge parsed into base. Resolver-supplied string fields are
    # attacker-tainted (provenance=donna); structured types (bool/int)
    # that survived schema validation are trusted (provenance=broker).
    for key, value in parsed.items():
        if key == "resolved_summary" and isinstance(value, str):
            base["resolved_summary"] = value
            continue
        provenance = "broker" if isinstance(value, (bool, int)) else "donna"
        base["fields"].append({
            "label": str(key),
            "value": str(value),
            "provenance": provenance,
        })
    return base
