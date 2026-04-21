"""Startup vault health checks.

Spec: Piece C design doc §6 + security-v1.1 §10 (fail-closed: warnings
only, don't refuse boot), §15 (audit redaction posture).

Usage:
    from broker import vault_health
    vault_health.sweep(
        creds_dir=Path("/Users/donna-broker/.config/donna/creds"),
        identity_path=Path(".../identity.age"),
        age_binary="age",
        declared_entries=["gmail", "everyone_active"],
        audit_writer=audit_append,
    )

Each failed check emits a `creds_vault_warning` audit event with a
stable `reason` code. The broker does not refuse to start — a
misconfigured capability fails at request time; everything else keeps
working.

Ten stable reason codes (do not rename):
  vault_dir_missing, vault_dir_mode_loose, vault_dir_owner_wrong,
  identity_missing, identity_mode_loose, identity_owner_wrong,
  age_binary_missing, entry_missing, entry_mode_loose, entry_owner_wrong.
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path
from typing import Any, Callable, Iterable

AuditWriter = Callable[[dict[str, Any]], Any]

DONNA_BROKER_UID_NAME = "donna-broker"


def _check_owner_matches(path: Path, want_uid: int) -> bool:
    """Stat path; return True iff owner UID matches. Monkeypatchable
    in tests that don't run as donna-broker."""
    try:
        return path.stat().st_uid == want_uid
    except OSError:
        return False


def _resolve_age_binary(binary: str) -> str | None:
    """Return resolved absolute path or None if not found.
    Monkeypatchable in tests."""
    return shutil.which(binary)


def _get_donna_broker_uid() -> int | None:
    """Lookup uid by name. Returns None if the user doesn't exist on
    this host (test environments). Tests monkeypatch
    _check_owner_matches to bypass ownership assertions."""
    try:
        import pwd
        return pwd.getpwnam(DONNA_BROKER_UID_NAME).pw_uid
    except (KeyError, ImportError):
        return None


def _emit(audit_writer: AuditWriter | None, reason: str, **extra: Any) -> None:
    if audit_writer is None:
        return
    event: dict[str, Any] = {"event": "creds_vault_warning", "reason": reason}
    event.update(extra)
    try:
        audit_writer(event)
    except Exception:
        # §10 — audit failure never blocks startup.
        pass


def sweep(
    *,
    creds_dir: Path,
    identity_path: Path,
    age_binary: str,
    declared_entries: Iterable[str],
    audit_writer: AuditWriter | None,
) -> list[dict[str, Any]]:
    """Run the ten structural checks. Returns the list of warnings
    (also emitted via audit_writer). Never raises."""
    warnings: list[dict[str, Any]] = []

    def warn(reason: str, **extra: Any) -> None:
        w = {"reason": reason, **extra}
        warnings.append(w)
        _emit(audit_writer, reason, **extra)

    donna_uid = _get_donna_broker_uid()

    # ---- broker-wide (§6.1) ---------------------------------------------

    if not creds_dir.exists() or not creds_dir.is_dir():
        warn("vault_dir_missing", path=str(creds_dir))
    else:
        mode = stat.S_IMODE(creds_dir.stat().st_mode)
        if mode & 0o027:
            warn("vault_dir_mode_loose", path=str(creds_dir),
                 mode=f"{mode:04o}")
        if donna_uid is not None and not _check_owner_matches(creds_dir, donna_uid):
            warn("vault_dir_owner_wrong", path=str(creds_dir),
                 want_uid=donna_uid)

    if not identity_path.exists():
        warn("identity_missing", path=str(identity_path))
    else:
        mode = stat.S_IMODE(identity_path.stat().st_mode)
        if mode != 0o400:
            warn("identity_mode_loose", path=str(identity_path),
                 mode=f"{mode:04o}")
        if donna_uid is not None and not _check_owner_matches(identity_path, donna_uid):
            warn("identity_owner_wrong", path=str(identity_path),
                 want_uid=donna_uid)

    if _resolve_age_binary(age_binary) is None:
        warn("age_binary_missing", binary=age_binary)

    # ---- per-capability (§6.2) ------------------------------------------

    if creds_dir.exists() and creds_dir.is_dir():
        for entry in declared_entries:
            entry_path = creds_dir / f"{entry}.age"
            if not entry_path.exists():
                warn("entry_missing", entry=entry, path=str(entry_path))
                continue
            mode = stat.S_IMODE(entry_path.stat().st_mode)
            if mode & 0o037:
                warn("entry_mode_loose", entry=entry,
                     path=str(entry_path), mode=f"{mode:04o}")
            if donna_uid is not None and not _check_owner_matches(entry_path, donna_uid):
                warn("entry_owner_wrong", entry=entry,
                     path=str(entry_path), want_uid=donna_uid)

    return warnings
