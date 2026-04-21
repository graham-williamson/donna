"""Tests for broker.audit.

Spec: security-v1.1 §7.6, §15.

Coverage aims:
  - Append-only chain: first entry uses ZERO_HASH; subsequent entries
    link via sha256 of the prior canonical entry.
  - §15 forbidden-field rejection: every banned key name raises
    AuditViolation, at any depth.
  - Rotation: size-trigger and age-trigger both produce a sealed file
    named audit-YYYY-MM-DD-NNN.log.sealed with a segment_seal entry;
    the next segment chains from that seal's canonical hash.
  - verify_chain: None on clean, structured dict on first break;
    detects mutated bytes, swapped entries, cross-segment breaks.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from broker import audit


# ---- helpers -------------------------------------------------------------


def _read_entries(segment: Path) -> list[dict[str, Any]]:
    lines = segment.read_bytes().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _sha_of(entry: dict[str, Any]) -> str:
    return hashlib.sha256(audit._canonical(entry)).hexdigest()


# ---- module surface ------------------------------------------------------


def test_module_importable():
    assert hasattr(audit, "write_event")
    assert hasattr(audit, "verify_chain")
    assert hasattr(audit, "rotate_if_needed")
    assert hasattr(audit, "AuditViolation")
    assert hasattr(audit, "AuditIntegrityError")


# ---- basic append + chain ------------------------------------------------


def test_first_entry_uses_zero_prev_hash(broker_home):
    audit_dir = str(broker_home / "audit")
    returned = audit.write_event(audit_dir, {"event": "broker_service_started"})
    entries = _read_entries(Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME)
    assert len(entries) == 1
    assert entries[0]["prev_hash"] == audit.ZERO_HASH
    assert returned == _sha_of(entries[0])


def test_three_entry_chain_links_correctly(broker_home):
    audit_dir = str(broker_home / "audit")
    h1 = audit.write_event(audit_dir, {"event": "broker_service_started", "ts": 1_000})
    h2 = audit.write_event(audit_dir, {"event": "request_created", "ts": 2_000})
    h3 = audit.write_event(audit_dir, {"event": "request_approved", "ts": 3_000})
    entries = _read_entries(Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME)
    assert len(entries) == 3
    assert entries[0]["prev_hash"] == audit.ZERO_HASH
    assert entries[1]["prev_hash"] == h1
    assert entries[2]["prev_hash"] == h2
    assert _sha_of(entries[2]) == h3


def test_write_event_sets_ts_when_missing(broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "no_ts_provided"})
    entries = _read_entries(Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME)
    assert isinstance(entries[0]["ts"], int)
    assert entries[0]["ts"] > 0


def test_write_event_preserves_caller_supplied_ts(broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "test", "ts": 42})
    entries = _read_entries(Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME)
    assert entries[0]["ts"] == 42


def test_write_event_creates_file_mode_0600(broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "perm_check"})
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    mode = segment.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got 0o{mode:o}"


def test_write_event_non_dict_raises(broker_home):
    audit_dir = str(broker_home / "audit")
    with pytest.raises(audit.AuditViolation):
        audit.write_event(audit_dir, "not a dict")  # type: ignore[arg-type]


def test_prev_hash_always_writer_owned(broker_home):
    """Caller's attempt to supply prev_hash is silently overwritten."""
    audit_dir = str(broker_home / "audit")
    audit.write_event(
        audit_dir,
        {"event": "started", "prev_hash": "deadbeef" * 8},
    )
    entries = _read_entries(Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME)
    assert entries[0]["prev_hash"] == audit.ZERO_HASH


# ---- §15 forbidden-field rejection --------------------------------------


@pytest.mark.parametrize("forbidden", sorted(audit.FORBIDDEN_KEYS))
def test_forbidden_top_level_key_rejected(broker_home, forbidden):
    audit_dir = str(broker_home / "audit")
    with pytest.raises(audit.AuditViolation) as exc:
        audit.write_event(audit_dir, {"event": "x", forbidden: "any value"})
    assert forbidden in str(exc.value)


@pytest.mark.parametrize("forbidden", sorted(audit.FORBIDDEN_KEYS))
def test_forbidden_nested_key_rejected(broker_home, forbidden):
    audit_dir = str(broker_home / "audit")
    with pytest.raises(audit.AuditViolation):
        audit.write_event(
            audit_dir,
            {"event": "x", "outer": {"inner": {forbidden: "leak"}}},
        )


def test_forbidden_key_inside_list_rejected(broker_home):
    audit_dir = str(broker_home / "audit")
    with pytest.raises(audit.AuditViolation):
        audit.write_event(
            audit_dir,
            {"event": "x", "items": [{"ok": 1}, {"password": "leak"}]},
        )


def test_forbidden_rejection_happens_before_any_io(broker_home):
    audit_dir = str(broker_home / "audit")
    with pytest.raises(audit.AuditViolation):
        audit.write_event(audit_dir, {"password": "leak"})
    # No segment file should have been created.
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    assert not segment.exists(), "forbidden check must run before any I/O"


# ---- rotation ------------------------------------------------------------


def test_rotate_returns_none_when_no_segment(broker_home):
    audit_dir = str(broker_home / "audit")
    assert audit.rotate_if_needed(audit_dir) is None


def test_rotate_returns_none_when_under_thresholds(broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "small"})
    assert audit.rotate_if_needed(audit_dir) is None


def test_rotate_triggered_by_size(monkeypatch, broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "a", "ts": 1_000})
    audit.write_event(audit_dir, {"event": "b", "ts": 2_000})

    # Force tiny size threshold so anything rotates.
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    sealed = audit.rotate_if_needed(audit_dir)
    assert sealed is not None
    sealed_path = Path(sealed)
    assert sealed_path.name.startswith("audit-")
    assert sealed_path.name.endswith(".log.sealed")
    # Active segment is gone (renamed).
    assert not (Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME).exists()
    # Sealed file has the seal entry as the last line.
    entries = _read_entries(sealed_path)
    assert entries[-1]["event"] == audit.SEGMENT_SEAL_EVENT
    assert entries[-1]["segment_end_hash"] == _sha_of(entries[-2])


def test_rotate_triggered_by_age(monkeypatch, broker_home):
    audit_dir = str(broker_home / "audit")
    # Write an entry with an ancient ts.
    audit.write_event(audit_dir, {"event": "old", "ts": 0})
    # Force small age threshold — even 1 second of age rotates.
    monkeypatch.setattr(audit, "ROTATION_AGE_SECONDS", 1)
    sealed = audit.rotate_if_needed(audit_dir)
    assert sealed is not None


def test_rotation_chains_across_segments(monkeypatch, broker_home):
    audit_dir = str(broker_home / "audit")
    h1 = audit.write_event(audit_dir, {"event": "a", "ts": 1_000})
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    sealed = audit.rotate_if_needed(audit_dir)
    assert sealed is not None

    # Monkeypatch back so the next write doesn't try to rotate again.
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 100 * 1024 * 1024)

    # Next entry's prev_hash should be the seal entry's canonical hash.
    h2 = audit.write_event(audit_dir, {"event": "after_rotation", "ts": 5_000})
    sealed_entries = _read_entries(Path(sealed))
    seal_entry = sealed_entries[-1]
    seal_hash = _sha_of(seal_entry)

    new_entries = _read_entries(Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME)
    assert new_entries[0]["prev_hash"] == seal_hash
    # And the seal entry's prev_hash should be h1.
    assert seal_entry["prev_hash"] == h1


def test_rotation_nnn_increments_when_same_day(monkeypatch, broker_home):
    audit_dir = str(broker_home / "audit")

    # Two rotations back-to-back in the same UTC day.
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    audit.write_event(audit_dir, {"event": "x1", "ts": 1_000})
    sealed1 = audit.rotate_if_needed(audit_dir)
    audit.write_event(audit_dir, {"event": "x2", "ts": 2_000})
    sealed2 = audit.rotate_if_needed(audit_dir)

    assert sealed1 is not None and sealed2 is not None
    assert sealed1 != sealed2
    # NNN should increment: -001 then -002.
    assert "-001.log.sealed" in sealed1
    assert "-002.log.sealed" in sealed2


# ---- verify_chain --------------------------------------------------------


def test_verify_chain_clean(broker_home):
    audit_dir = str(broker_home / "audit")
    for i in range(20):
        audit.write_event(audit_dir, {"event": "evt", "n": i})
    assert audit.verify_chain(audit_dir) is None


def test_verify_chain_none_when_dir_missing(broker_home):
    assert audit.verify_chain(str(broker_home / "nonexistent")) is None


def test_verify_chain_none_when_empty(broker_home):
    audit_dir = broker_home / "audit"
    # broker_home fixture already creates the dir; don't write anything.
    assert audit.verify_chain(str(audit_dir)) is None


def test_verify_chain_detects_mutated_entry(broker_home):
    audit_dir = str(broker_home / "audit")
    for i in range(5):
        audit.write_event(audit_dir, {"event": "evt", "n": i})

    # Flip one byte in the middle of the file.
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    lines = segment.read_bytes().splitlines(keepends=True)
    # Mutate line 3 (0-indexed 2): change "evt" to "EVT".
    lines[2] = lines[2].replace(b'"evt"', b'"EVT"')
    segment.write_bytes(b"".join(lines))

    result = audit.verify_chain(audit_dir)
    assert result is not None
    assert "prev_hash mismatch" in result["reason"]
    # The break surfaces at line 4: mutating line 3's payload changes its
    # canonical hash, which is what line 4's prev_hash was computed from.
    # (Line 3 itself still has the correct prev_hash pointing at the
    # unmodified line 2.)
    assert result["line"] == 4


def test_verify_chain_detects_missing_prev_hash(broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "first"})
    # Inject a malformed entry missing prev_hash.
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    with open(segment, "ab") as f:
        f.write(b'{"event":"bogus","ts":9999}\n')
    result = audit.verify_chain(audit_dir)
    assert result is not None
    assert "prev_hash mismatch" in result["reason"]


def test_verify_chain_detects_junk_line(broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "first"})
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    with open(segment, "ab") as f:
        f.write(b"not valid json\n")
    result = audit.verify_chain(audit_dir)
    assert result is not None
    assert "json parse error" in result["reason"]


def test_verify_chain_across_two_sealed_segments(monkeypatch, broker_home):
    audit_dir = str(broker_home / "audit")

    # Three entries → rotate → three more entries → rotate → two more.
    audit.write_event(audit_dir, {"event": "a1", "ts": 1000})
    audit.write_event(audit_dir, {"event": "a2", "ts": 1100})
    audit.write_event(audit_dir, {"event": "a3", "ts": 1200})

    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    audit.rotate_if_needed(audit_dir)
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 100 * 1024 * 1024)

    audit.write_event(audit_dir, {"event": "b1", "ts": 2000})
    audit.write_event(audit_dir, {"event": "b2", "ts": 2100})
    audit.write_event(audit_dir, {"event": "b3", "ts": 2200})

    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    audit.rotate_if_needed(audit_dir)
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 100 * 1024 * 1024)

    audit.write_event(audit_dir, {"event": "c1", "ts": 3000})
    audit.write_event(audit_dir, {"event": "c2", "ts": 3100})

    assert audit.verify_chain(audit_dir) is None


def test_verify_chain_detects_break_at_seal_boundary(monkeypatch, broker_home):
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "a1", "ts": 1000})
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    sealed_str = audit.rotate_if_needed(audit_dir)
    assert sealed_str is not None
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 100 * 1024 * 1024)
    audit.write_event(audit_dir, {"event": "b1", "ts": 2000})

    # Tamper with the sealed file's seal entry: change segment_end_hash.
    sealed_path = Path(sealed_str)
    lines = sealed_path.read_bytes().splitlines(keepends=True)
    last = json.loads(lines[-1])
    last["segment_end_hash"] = "f" * 64
    lines[-1] = (json.dumps(last, sort_keys=True, separators=(",", ":"))
                 + "\n").encode("utf-8")
    sealed_path.write_bytes(b"".join(lines))

    result = audit.verify_chain(audit_dir)
    assert result is not None


def test_rotate_then_append_then_verify_roundtrip(monkeypatch, broker_home):
    """End-to-end: the most common real operational pattern."""
    audit_dir = str(broker_home / "audit")
    for i in range(5):
        audit.write_event(audit_dir, {"event": "pre", "n": i, "ts": 1000 + i})
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    audit.rotate_if_needed(audit_dir)
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 100 * 1024 * 1024)
    for i in range(5):
        audit.write_event(audit_dir, {"event": "post", "n": i, "ts": 2000 + i})
    assert audit.verify_chain(audit_dir) is None


# ---- tail corruption rejects append -------------------------------------


def test_write_on_corrupt_tail_raises_integrity(broker_home):
    audit_dir = str(broker_home / "audit")
    Path(audit_dir).mkdir(parents=True, exist_ok=True)
    # Create a segment with a bad last line.
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    segment.write_bytes(b"not json\n")
    with pytest.raises(audit.AuditIntegrityError):
        audit.write_event(audit_dir, {"event": "should_fail"})


# ---- O_APPEND behaviour (no seek allowed) -------------------------------


def test_write_does_not_overwrite_existing_content(broker_home):
    """Sanity: two calls produce two lines, not one overwritten."""
    audit_dir = str(broker_home / "audit")
    audit.write_event(audit_dir, {"event": "first"})
    audit.write_event(audit_dir, {"event": "second"})
    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    lines = [l for l in segment.read_bytes().splitlines() if l.strip()]
    assert len(lines) == 2


# ---- canonicalisation independence --------------------------------------


def test_canonical_is_deterministic_across_key_order(broker_home):
    """The audit canonical form sorts keys so the same logical entry
    hashes the same regardless of insertion order."""
    a = audit._canonical({"b": 2, "a": 1, "c": 3})
    b = audit._canonical({"c": 3, "b": 2, "a": 1})
    assert a == b


def test_canonical_uses_compact_separators():
    """No whitespace between tokens — audit serialiser §7.6."""
    result = audit._canonical({"a": 1, "b": [2, 3]})
    assert b" " not in result


# ---- internal edge-case helpers ------------------------------------------


def test_first_entry_ts_none_when_segment_missing(broker_home):
    missing = broker_home / "nowhere" / "audit.log"
    assert audit._first_entry_ts(missing) is None


def test_first_entry_ts_none_when_unparseable_first_line(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b"not json\n")
    assert audit._first_entry_ts(seg) is None


def test_first_entry_ts_none_when_ts_missing(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b'{"event":"no_ts"}\n')
    assert audit._first_entry_ts(seg) is None


def test_first_entry_ts_none_when_ts_not_int(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b'{"event":"bad","ts":"not-an-int"}\n')
    assert audit._first_entry_ts(seg) is None


def test_first_entry_ts_skips_blank_leading_lines(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b"\n\n" + b'{"event":"x","ts":1234}\n')
    assert audit._first_entry_ts(seg) == 1234


def test_last_entry_hash_rejects_non_dict_tail(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b'["not", "an", "object"]\n')
    with pytest.raises(audit.AuditIntegrityError):
        audit._last_entry_hash(seg)


def test_last_entry_hash_on_empty_but_existing_segment(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b"")  # empty
    assert audit._last_entry_hash(seg) == audit.ZERO_HASH


def test_last_entry_hash_on_whitespace_only_segment(broker_home):
    audit_dir = broker_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    seg = audit_dir / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b"   \n\n   \n")  # present but no usable content
    # Size > 0 so the first-short-circuit doesn't fire; we fall through
    # to last_line == b"" and return ZERO.
    assert audit._last_entry_hash(seg) == audit.ZERO_HASH


def test_verify_chain_rejects_non_dict_entry(broker_home):
    audit_dir = str(broker_home / "audit")
    # Hand-build a segment whose first line is a JSON array.
    Path(audit_dir).mkdir(parents=True, exist_ok=True)
    seg = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b'["not", "an", "object"]\n')
    result = audit.verify_chain(audit_dir)
    assert result is not None
    assert "not a JSON object" in result["reason"]


def test_verify_chain_accepts_seal_without_segment_end_hash_field(
    monkeypatch, broker_home
):
    """A seal entry that happens to omit segment_end_hash is still
    chain-valid if its prev_hash is correct. The self-consistency check
    only fires when segment_end_hash is present."""
    audit_dir = str(broker_home / "audit")
    # Write a single entry, then rotate.
    audit.write_event(audit_dir, {"event": "a", "ts": 1_000})
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    sealed_str = audit.rotate_if_needed(audit_dir)
    assert sealed_str is not None
    sealed = Path(sealed_str)

    # Rewrite seal entry without segment_end_hash.
    lines = sealed.read_bytes().splitlines(keepends=True)
    seal = json.loads(lines[-1])
    seal.pop("segment_end_hash")
    lines[-1] = (
        json.dumps(seal, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    sealed.write_bytes(b"".join(lines))

    assert audit.verify_chain(audit_dir) is None


def test_rotate_handles_segment_without_parseable_ts(monkeypatch, broker_home):
    """If the first entry lacks a valid ts, age_ms is 0 — rotation
    should still trigger on size alone."""
    audit_dir = str(broker_home / "audit")
    Path(audit_dir).mkdir(parents=True, exist_ok=True)
    seg = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    seg.write_bytes(b'{"event":"no_ts_here","prev_hash":"' + b"0" * 64 + b'"}\n')
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 10)
    assert audit.rotate_if_needed(audit_dir) is not None


# ---- concurrency --------------------------------------------------------


def test_parallel_writers_produce_intact_chain(broker_home):
    """Regression guard for the shared audit.lock flock.

    Eight threads each append 25 entries. Without the segment lock,
    two writers could read the same tail hash, each compute the same
    prev_hash, and append two rows chained from the same predecessor —
    breaking the chain. With the lock in place, every write is
    serialised through the `_acquire_segment_lock` fd and verify_chain
    stays clean at the end.

    Using threads (not multiprocessing) because the audit module opens
    a fresh fd per lock acquisition, and BSD flock on macOS is a
    per-open-file-description primitive — so distinct fds from the
    same process contend correctly.
    """
    audit_dir = str(broker_home / "audit")
    n_threads = 8
    writes_per_thread = 25
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(thread_index: int) -> None:
        try:
            for i in range(writes_per_thread):
                audit.write_event(
                    audit_dir,
                    {
                        "event": "parallel_write",
                        "thread": thread_index,
                        "seq": i,
                    },
                )
        except BaseException as e:  # noqa: BLE001 — we re-raise below
            with errors_lock:
                errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(tid,), daemon=True)
        for tid in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "writer thread did not finish in time"

    assert not errors, f"worker(s) raised: {errors!r}"

    segment = Path(audit_dir) / audit.ACTIVE_SEGMENT_NAME
    entries = _read_entries(segment)
    assert len(entries) == n_threads * writes_per_thread, (
        f"expected {n_threads * writes_per_thread} rows, got {len(entries)}"
    )

    # Chain integrity is the real contract. If any write raced, the
    # prev_hash chain would break here.
    assert audit.verify_chain(audit_dir) is None


def test_parallel_write_and_rotate_stay_consistent(monkeypatch, broker_home):
    """Mixed workload: writers keep appending while rotation fires.

    Four writer threads + one rotator thread. The rotator trips the
    size threshold every tick by monkeypatching ROTATION_SIZE_BYTES
    to a tiny value. Because write_event and rotate_if_needed both
    take the shared lock, we never see a seal entry written after a
    writer has already appended a newer tail, and verify_chain is
    clean across every sealed segment + the live one.
    """
    audit_dir = str(broker_home / "audit")

    # Force rotation to be triggered by size on almost every call.
    monkeypatch.setattr(audit, "ROTATION_SIZE_BYTES", 256)

    stop = threading.Event()
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def writer(thread_index: int) -> None:
        seq = 0
        try:
            while not stop.is_set():
                audit.write_event(
                    audit_dir,
                    {
                        "event": "mixed_write",
                        "thread": thread_index,
                        "seq": seq,
                    },
                )
                seq += 1
                if seq >= 30:
                    return
        except BaseException as e:  # noqa: BLE001
            with errors_lock:
                errors.append(e)

    def rotator() -> None:
        try:
            for _ in range(60):
                if stop.is_set():
                    return
                audit.rotate_if_needed(audit_dir)
        except BaseException as e:  # noqa: BLE001
            with errors_lock:
                errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(i,), daemon=True)
        for i in range(4)
    ]
    threads.append(threading.Thread(target=rotator, daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "thread stuck — possible deadlock"
    stop.set()

    assert not errors, f"worker(s) raised: {errors!r}"

    # Chain must be intact across every sealed segment and the live one.
    assert audit.verify_chain(audit_dir) is None
