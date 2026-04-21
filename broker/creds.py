"""Age-encrypted credential vault loader.

Spec: security-v1.1 §17 (Phase 2 age vault), §12.1 (broker home layout),
§15 (audit redactions), §10 (universal fail-closed).

Contract:
  - `unlock_creds(capability, ...)` spawns `age --decrypt -i <identity>
    <ciphertext>` and streams stdout bytes back to the caller. The
    caller — and only the caller — holds the plaintext, and is expected
    to pipe it straight into the capability's executor subprocess via
    stdin. No caching inside this module.
  - Never logs, audits, or returns stderr content that could hint at a
    plaintext byte. The audit event carries the capability name and an
    outcome token only.
  - Pure function. Tests can stub the `age` binary via a shell script
    that echoes fixed bytes; the identity + ciphertext paths are caller-
    supplied so nothing is hard-coded to /Users/donna-broker/.
  - §15-safe audit event shape: `{event, capability, outcome, reason}`
    where no key is in `audit.FORBIDDEN_KEYS`.
  - Fail-closed (§10): every error path raises CredsError with a
    structured error_code. The caller maps to a terminal executor
    state; we never silently return empty bytes.

Boundary note:
  This module is the second legitimate subprocess boundary in the broker
  alongside resolver.py / executor.py. Only executor.py's subprocess
  path may call into it, and only at the exact moment of child spawn.
  No other module imports creds — tests monkey-patch via caller
  injection rather than reaching in here.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, Union


PathLike = Union[str, Path]
AuditWriter = Callable[[dict[str, Any]], Any]


# Capability names are interpolated into a filename under the creds dir
# (`<capability>.age`) so the permissive spec-level allowance is not
# enough — we reject anything that could break out of that path or
# alias an unintended file. Matches the capability-name shape used by
# manifests (lowercase, dotted, short).
CAPABILITY_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

DEFAULT_AGE_BINARY = "age"
DEFAULT_TIMEOUT_SECONDS = 10.0


class CredsError(Exception):
    """Structured vault-unlock failure.

    `error_code` is the stable machine-readable token; `message` is a
    human-readable tail. Callers map `error_code` onto executor terminal
    states and the audit trail. Never carries plaintext.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _emit(writer: Optional[AuditWriter], event: dict[str, Any]) -> None:
    """Fire-and-forget audit emission. Mirrors executor._emit: audit
    failures must never block the data path. §10 applies."""
    if writer is None:
        return
    try:
        writer(event)
    except Exception:
        pass


def _audit_event(
    capability: str, outcome: str, reason: Optional[str] = None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event": "creds_unlock",
        "capability": capability,
        "outcome": outcome,
    }
    if reason is not None:
        event["reason"] = reason
    return event


def unlock_creds(
    capability: str,
    creds_dir: PathLike,
    identity_path: PathLike,
    age_binary: str = DEFAULT_AGE_BINARY,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    audit_writer: Optional[AuditWriter] = None,
) -> bytes:
    """Decrypt `<creds_dir>/<capability>.age` via `age` and return the
    plaintext bytes.

    Raises CredsError on any failure. Emits exactly one `creds_unlock`
    audit event on the writer (success or failure); plaintext is never
    part of that event.

    The caller must:
      - Immediately hand the returned bytes to the child subprocess's
        stdin and drop its own reference.
      - Not log or store the bytes. There is no way to zero Python
        `bytes` after the fact; minimise the residency window.
    """
    if not isinstance(capability, str) or not CAPABILITY_NAME_RE.fullmatch(
        capability
    ):
        # Pre-validation: do not emit an audit event for a malformed
        # capability name — the name itself might not be safe to log if
        # it was somehow attacker-influenced. Fail loudly instead.
        raise CredsError(
            "creds_bad_capability_name",
            f"capability name {capability!r} is not in the permitted shape",
        )

    creds_dir_p = Path(creds_dir)
    identity_p = Path(identity_path)
    ciphertext_p = creds_dir_p / f"{capability}.age"

    if not identity_p.is_file():
        _emit(
            audit_writer,
            _audit_event(capability, "failure", "creds_identity_missing"),
        )
        raise CredsError(
            "creds_identity_missing",
            f"age identity not found at {identity_p}",
        )
    if not ciphertext_p.is_file():
        _emit(
            audit_writer,
            _audit_event(capability, "failure", "creds_missing"),
        )
        raise CredsError(
            "creds_missing",
            f"no ciphertext for capability {capability!r} at {ciphertext_p}",
        )

    try:
        proc = subprocess.run(
            [
                age_binary,
                "--decrypt",
                "-i", str(identity_p),
                str(ciphertext_p),
            ],
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _emit(
            audit_writer,
            _audit_event(capability, "failure", "creds_timeout"),
        )
        raise CredsError(
            "creds_timeout",
            f"age --decrypt timed out after {timeout_seconds}s",
        )
    except FileNotFoundError:
        _emit(
            audit_writer,
            _audit_event(capability, "failure", "creds_binary_missing"),
        )
        raise CredsError(
            "creds_binary_missing",
            f"age binary not found at {age_binary!r}",
        )
    except Exception as e:
        _emit(
            audit_writer,
            _audit_event(capability, "failure", "creds_spawn_error"),
        )
        raise CredsError(
            "creds_spawn_error",
            f"{type(e).__name__} while spawning age",
        )

    if proc.returncode != 0:
        _emit(
            audit_writer,
            _audit_event(capability, "failure", "creds_decrypt_failed"),
        )
        raise CredsError(
            "creds_decrypt_failed",
            f"age exited with status {proc.returncode}",
        )

    _emit(
        audit_writer,
        _audit_event(capability, "success"),
    )
    return proc.stdout
