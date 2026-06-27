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

import os
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
DEFAULT_AGE_KEYGEN_BINARY = "age-keygen"
DEFAULT_TIMEOUT_SECONDS = 10.0

# Vault entry files land 0400 like ops/create-vault-entry.sh writes them.
VAULT_ENTRY_MODE = 0o400


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


def _store_audit_event(
    entry: str, outcome: str, reason: Optional[str] = None,
    replaced: Optional[bool] = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event": "creds_store",
        "capability": entry,
        "outcome": outcome,
    }
    if reason is not None:
        event["reason"] = reason
    if replaced is not None:
        event["replaced"] = replaced
    return event


def store_creds(
    entry: str,
    plaintext: bytes,
    creds_dir: PathLike,
    identity_path: PathLike,
    age_binary: str = DEFAULT_AGE_BINARY,
    age_keygen_binary: str = DEFAULT_AGE_KEYGEN_BINARY,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    audit_writer: Optional[AuditWriter] = None,
) -> dict[str, Any]:
    """Encrypt `plaintext` to the vault's own recipient and land it at
    `<creds_dir>/<entry>.age` (0400, atomic rename). The write-side
    counterpart of unlock_creds, used by the `store-credential` broker
    mode (Connected Sites).

    The recipient is derived from the broker identity via
    `age-keygen -y <identity>` — the same identity unlock_creds decrypts
    with, so a stored entry round-trips. Plaintext flows in as caller
    bytes and out only as `age` stdin; it is never written to disk
    unencrypted, never logged, and never part of the audit event or any
    raised error. Replacing an existing entry is allowed (re-connect
    flow) and audited as `replaced: true`.

    Returns {"entry": ..., "replaced": bool}. Raises CredsError on any
    failure (fail-closed, §10)."""
    if not isinstance(entry, str) or not CAPABILITY_NAME_RE.fullmatch(entry):
        # Same shape guard as unlock_creds: the name is interpolated
        # into a vault path, and may be attacker-influenced — fail
        # loudly without auditing the raw value.
        raise CredsError(
            "creds_bad_capability_name",
            f"vault entry name {entry!r} is not in the permitted shape",
        )
    if not isinstance(plaintext, bytes) or not plaintext:
        raise CredsError(
            "creds_store_empty", "refusing to store an empty credential",
        )

    creds_dir_p = Path(creds_dir)
    identity_p = Path(identity_path)
    target_p = creds_dir_p / f"{entry}.age"

    if not creds_dir_p.is_dir():
        _emit(
            audit_writer,
            _store_audit_event(entry, "failure", "creds_vault_dir_missing"),
        )
        raise CredsError(
            "creds_vault_dir_missing", f"vault dir not found at {creds_dir_p}",
        )
    if not identity_p.is_file():
        _emit(
            audit_writer,
            _store_audit_event(entry, "failure", "creds_identity_missing"),
        )
        raise CredsError(
            "creds_identity_missing",
            f"age identity not found at {identity_p}",
        )

    def _run(
        argv: list[str], stdin_bytes: Optional[bytes], step: str
    ) -> "subprocess.CompletedProcess[bytes]":
        try:
            return subprocess.run(
                argv,
                input=stdin_bytes,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            _emit(
                audit_writer,
                _store_audit_event(entry, "failure", "creds_timeout"),
            )
            raise CredsError(
                "creds_timeout", f"{step} timed out after {timeout_seconds}s",
            )
        except FileNotFoundError:
            _emit(
                audit_writer,
                _store_audit_event(entry, "failure", "creds_binary_missing"),
            )
            raise CredsError(
                "creds_binary_missing", f"binary not found for {step}",
            )
        except Exception as e:
            _emit(
                audit_writer,
                _store_audit_event(entry, "failure", "creds_spawn_error"),
            )
            raise CredsError(
                "creds_spawn_error",
                f"{type(e).__name__} while spawning {step}",
            )

    keygen = _run(
        [age_keygen_binary, "-y", str(identity_p)], None, "age-keygen -y",
    )
    recipient = keygen.stdout.decode("utf-8", "replace").strip()
    if keygen.returncode != 0 or not recipient.startswith("age1"):
        _emit(
            audit_writer,
            _store_audit_event(entry, "failure", "creds_recipient_failed"),
        )
        raise CredsError(
            "creds_recipient_failed",
            f"could not derive age recipient (exit {keygen.returncode})",
        )

    # Encrypt to a temp file in the vault dir itself so the final
    # os.replace is atomic on the same filesystem. Only ciphertext ever
    # touches disk.
    tmp_p = creds_dir_p / f".{entry}.age.tmp"
    replaced = target_p.exists()
    try:
        enc = _run(
            [age_binary, "-r", recipient, "-o", str(tmp_p)],
            plaintext, "age encrypt",
        )
        if enc.returncode != 0:
            _emit(
                audit_writer,
                _store_audit_event(entry, "failure", "creds_encrypt_failed"),
            )
            raise CredsError(
                "creds_encrypt_failed",
                f"age exited with status {enc.returncode}",
            )
        try:
            os.chmod(tmp_p, VAULT_ENTRY_MODE)
            os.replace(tmp_p, target_p)
        except OSError as oe:
            _emit(
                audit_writer,
                _store_audit_event(entry, "failure", "creds_write_failed"),
            )
            raise CredsError(
                "creds_write_failed",
                f"{type(oe).__name__} landing vault entry",
            )
    finally:
        try:
            tmp_p.unlink(missing_ok=True)
        except OSError:
            pass

    _emit(
        audit_writer,
        _store_audit_event(entry, "success", replaced=replaced),
    )
    return {"entry": entry, "replaced": replaced}
