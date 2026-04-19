"""Hash-chained JSONL audit writer.

Spec: security-v1.1 §7.6, §15 (event list), §5 (integrity scope).

Contract:
  - Append-only. File opened O_APPEND; never truncated or seeked.
  - Every entry carries prev_hash = sha256(previous canonical entry).
  - Rotation: 100MB or 30 days. Rotating writes `segment_seal` with the
    tail hash; next segment's first entry references that hash as its
    prev_hash.
  - `verify_chain` walks segments in order, returns the offset/file of
    the first break or None when clean.
  - Source of truth on conflict with SQLite: JSONL wins (§7.6).
  - Must never write: credential values, raw email/Notion bodies, raw
    MCP responses, plaintext approval codes, plaintext params_json,
    stack traces, screenshot bytes, HMAC key contents, bot token (§15).

The writer is the only code path allowed to touch
/Users/donna-broker/audit/*.log — the hook enforces that Donna's Claude
process never reads those files.

Audit canonicalisation is deliberately its own routine (json.dumps with
sort_keys + compact separators), NOT broker.canonicalize. Keeping them
independent means a bug in the RFC 8785 library can't silently mask an
audit-chain problem.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional


# §15 forbidden keys. Must never appear as a key at any depth inside an
# audit entry. The writer refuses the call; callers are expected to
# redact upstream rather than rely on this as a safety net.
FORBIDDEN_KEYS = frozenset({
    "params_json",
    "body",
    "email_body",
    "notion_body",
    "credentials",
    "password",
    "api_key",
    "token",
    "bot_token",
    "hmac_key",
    "screenshot",
    "screenshot_bytes",
    "stack_trace",
    "traceback",
    "approval_code",
    "raw_response",
})

ACTIVE_SEGMENT_NAME = "audit.log"
SEGMENT_SEAL_EVENT = "segment_seal"
ROTATION_SIZE_BYTES = 100 * 1024 * 1024
ROTATION_AGE_SECONDS = 30 * 24 * 60 * 60

# Chain anchor: the very first entry in the very first segment uses
# this sentinel as prev_hash (there is nothing prior to hash).
ZERO_HASH = "0" * 64


class AuditViolation(Exception):
    """Raised when an event carries a §15-forbidden field. Callers are
    expected to redact before write, not rely on this safety net."""


class AuditIntegrityError(Exception):
    """Raised on structural corruption: unparseable tail line, missing
    prev_hash key, mid-chain surprise that prevents safe append."""


def _canonical(entry: dict[str, Any]) -> bytes:
    """Deterministic UTF-8 bytes for hashing an audit entry.

    Explicitly NOT broker.canonicalize — audit chain integrity stays
    independent of the RFC 8785 library per §7.6.
    """
    return json.dumps(
        entry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _check_forbidden_recursive(value: Any, path: str = "") -> None:
    """Raise AuditViolation if any dict key at any depth is §15-forbidden."""
    if isinstance(value, dict):
        for key, v in value.items():
            if key in FORBIDDEN_KEYS:
                location = path if path else "<root>"
                raise AuditViolation(
                    f"forbidden field {key!r} at {location}"
                )
            _check_forbidden_recursive(v, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_forbidden_recursive(item, f"{path}[{i}]")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _segment_path(audit_dir: str) -> Path:
    return Path(audit_dir) / ACTIVE_SEGMENT_NAME


def _last_entry_hash(segment: Path) -> str:
    """Return sha256 hex of the last canonical entry in `segment`.

    Returns ZERO_HASH when the segment is absent or empty. Raises
    AuditIntegrityError when the tail line cannot be parsed — refusing
    to append on top of corruption is part of the contract.
    """
    if not segment.exists() or segment.stat().st_size == 0:
        return ZERO_HASH

    last_line: bytes = b""
    with open(segment, "rb") as f:
        for line in f:
            if line.strip():
                last_line = line
    if not last_line:
        return ZERO_HASH
    try:
        entry = json.loads(last_line)
    except json.JSONDecodeError as e:
        raise AuditIntegrityError(
            f"tail line of {segment} is not valid JSON: {e}"
        ) from e
    if not isinstance(entry, dict):
        raise AuditIntegrityError(
            f"tail line of {segment} is not a JSON object"
        )
    return _sha256_hex(_canonical(entry))


def _next_prev_hash(audit_dir: str) -> str:
    """What value the next write_event should stamp into prev_hash.

    Active segment wins; if it is absent or empty, chain from the most
    recent sealed segment; otherwise the chain is brand new.
    """
    active = _segment_path(audit_dir)
    if active.exists() and active.stat().st_size > 0:
        return _last_entry_hash(active)
    sealed = sorted(Path(audit_dir).glob("audit-*.log.sealed"))
    if sealed:
        return _last_entry_hash(sealed[-1])
    return ZERO_HASH


def _first_entry_ts(segment: Path) -> Optional[int]:
    """Epoch-ms of the first entry, or None if absent/unparseable."""
    if not segment.exists() or segment.stat().st_size == 0:
        return None
    with open(segment, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return None
            ts = entry.get("ts") if isinstance(entry, dict) else None
            return ts if isinstance(ts, int) else None
    return None


def write_event(audit_dir: str, event: dict[str, Any]) -> str:
    """Append `event` to the active segment. Returns the sha256 hex of
    the canonical entry — callers store this in `requests.prev_audit_hash`
    when they want a tamper-evident backlink.

    Raises AuditViolation for §15-forbidden fields, AuditIntegrityError
    for corruption at the append point.
    """
    if not isinstance(event, dict):
        raise AuditViolation("event must be a dict")
    _check_forbidden_recursive(event)

    Path(audit_dir).mkdir(parents=True, exist_ok=True)
    segment = _segment_path(audit_dir)
    prev_hash = _next_prev_hash(audit_dir)

    # Caller may pre-set `ts` (useful for deterministic tests); otherwise
    # we set it. `prev_hash` is always writer-owned.
    entry: dict[str, Any] = dict(event)
    entry.setdefault("ts", _now_ms())
    entry["prev_hash"] = prev_hash

    canonical = _canonical(entry)

    # O_APPEND is the atomicity primitive. Mode 0600 when creating.
    fd = os.open(
        str(segment),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        os.write(fd, canonical + b"\n")
    finally:
        os.close(fd)

    return _sha256_hex(canonical)


def rotate_if_needed(audit_dir: str) -> Optional[str]:
    """Seal and rotate the active segment if size > 100MB or age > 30
    days. Returns the sealed segment path or None."""
    segment = _segment_path(audit_dir)
    if not segment.exists():
        return None
    size = segment.stat().st_size
    if size == 0:
        return None

    first_ts = _first_entry_ts(segment)
    now_ms = _now_ms()
    age_ms = (now_ms - first_ts) if first_ts is not None else 0

    if size <= ROTATION_SIZE_BYTES and age_ms <= ROTATION_AGE_SECONDS * 1000:
        return None

    # Seal entry: chain-anchored same as any other entry, plus a
    # `segment_end_hash` field that mirrors prev_hash so the marker is
    # self-describing when humans eyeball the tail of a sealed segment.
    tail_hash = _last_entry_hash(segment)
    seal_entry: dict[str, Any] = {
        "event": SEGMENT_SEAL_EVENT,
        "ts": now_ms,
        "segment_end_hash": tail_hash,
        "prev_hash": tail_hash,
    }
    seal_canonical = _canonical(seal_entry)

    fd = os.open(str(segment), os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, seal_canonical + b"\n")
    finally:
        os.close(fd)

    # Rename to audit-YYYY-MM-DD-NNN.log.sealed, NNN monotonic per UTC day.
    date_str = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    nnn = 1
    while True:
        sealed_path = Path(audit_dir) / f"audit-{date_str}-{nnn:03d}.log.sealed"
        if not sealed_path.exists():
            break
        nnn += 1
    segment.rename(sealed_path)
    return str(sealed_path)


def verify_chain(audit_dir: str) -> Optional[dict[str, Any]]:
    """Walk all segments in order, verify the prev_hash chain end-to-end.

    Returns None on clean. Returns {file, line, reason} for the first
    break so the caller can surface a single specific diagnosis to
    Graham via the integrity alert path (§12.4).
    """
    audit_path = Path(audit_dir)
    if not audit_path.exists():
        return None

    # Sealed segments in lex order (chronological given our naming),
    # then the live segment last.
    segments: list[Path] = sorted(audit_path.glob("audit-*.log.sealed"))
    active = audit_path / ACTIVE_SEGMENT_NAME
    if active.exists():
        segments.append(active)

    if not segments:
        return None

    expected_prev = ZERO_HASH

    for seg in segments:
        with open(seg, "rb") as f:
            line_num = 0
            for raw in f:
                line_num += 1
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError as e:
                    return {
                        "file": str(seg),
                        "line": line_num,
                        "reason": f"json parse error: {e}",
                    }
                if not isinstance(entry, dict):
                    return {
                        "file": str(seg),
                        "line": line_num,
                        "reason": "entry is not a JSON object",
                    }
                actual_prev = entry.get("prev_hash")
                if actual_prev != expected_prev:
                    return {
                        "file": str(seg),
                        "line": line_num,
                        "reason": (
                            f"prev_hash mismatch: entry has {actual_prev!r}, "
                            f"expected {expected_prev!r}"
                        ),
                    }
                # Self-consistency on seal entries: segment_end_hash, if
                # present, must equal prev_hash.
                if entry.get("event") == SEGMENT_SEAL_EVENT:
                    seh = entry.get("segment_end_hash")
                    if seh is not None and seh != actual_prev:
                        return {
                            "file": str(seg),
                            "line": line_num,
                            "reason": (
                                f"seal segment_end_hash {seh!r} does not "
                                f"match prev_hash {actual_prev!r}"
                            ),
                        }
                expected_prev = _sha256_hex(_canonical(entry))

    return None
