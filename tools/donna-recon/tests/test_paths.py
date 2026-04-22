"""Tests for donna_recon.paths.

Concurrency-critical: the cross-process lock test uses a subprocess so we
exercise real ``fcntl.flock`` semantics rather than in-process aliasing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from donna_recon.paths import (
    Lock,
    cleanup_stale,
    clear_current,
    current_path,
    is_pid_alive,
    new_recording_id,
    read_current,
    write_current,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ROOT and the derived paths to a tmp dir for this test."""
    fake = tmp_path / ".donna-recon"
    fake.mkdir(mode=0o700)
    monkeypatch.setattr("donna_recon.paths.ROOT", fake)
    return fake


class TestIsPidAlive:
    def test_current_process(self) -> None:
        assert is_pid_alive(os.getpid())

    def test_pid_zero_is_dead(self) -> None:
        assert not is_pid_alive(0)

    def test_obviously_dead_pid(self) -> None:
        # PID 999999 is virtually guaranteed to be unused on a dev machine.
        assert not is_pid_alive(999_999)


class TestCurrentPointer:
    def test_missing_returns_none(self, fake_root: Path) -> None:
        assert read_current() is None

    def test_write_and_read(self, fake_root: Path) -> None:
        target = fake_root / "2026-01-01T00-00-00Z"
        write_current(target)
        assert read_current() == target

    def test_clear(self, fake_root: Path) -> None:
        write_current(fake_root / "x")
        clear_current()
        assert read_current() is None

    def test_clear_idempotent(self, fake_root: Path) -> None:
        clear_current()  # no-op on missing


class TestCleanupStale:
    def test_no_current_is_noop(self, fake_root: Path) -> None:
        cleanup_stale()
        assert read_current() is None

    def test_removes_stale_current(self, fake_root: Path) -> None:
        rec = fake_root / "stale-recording"
        rec.mkdir(mode=0o700)
        (rec / "recorder.pid").write_text("999999\n")  # dead PID
        write_current(rec)
        assert read_current() == rec
        cleanup_stale()
        assert read_current() is None

    def test_keeps_live_current(self, fake_root: Path) -> None:
        rec = fake_root / "live-recording"
        rec.mkdir(mode=0o700)
        (rec / "recorder.pid").write_text(f"{os.getpid()}\n")
        write_current(rec)
        cleanup_stale()
        # Pointer preserved because the pid is live (our pid).
        assert read_current() == rec

    def test_missing_pid_file_clears_current(self, fake_root: Path) -> None:
        rec = fake_root / "no-pid"
        rec.mkdir(mode=0o700)
        # No recorder.pid written.
        write_current(rec)
        cleanup_stale()
        assert read_current() is None

    def test_corrupt_pid_file_clears_current(self, fake_root: Path) -> None:
        rec = fake_root / "bad-pid"
        rec.mkdir(mode=0o700)
        (rec / "recorder.pid").write_text("not-a-number")
        write_current(rec)
        cleanup_stale()
        assert read_current() is None


class TestNewRecordingId:
    def test_shape(self) -> None:
        rid = new_recording_id()
        # YYYY-MM-DDTHH-MM-SSZ — 20 chars, ends with Z.
        assert len(rid) == 20
        assert rid.endswith("Z")
        # No colons (shell-friendly).
        assert ":" not in rid


class TestLock:
    def test_acquire_release_roundtrip(self, tmp_path: Path) -> None:
        lock_file = tmp_path / ".lock"
        lock = Lock(lock_file)
        lock.acquire()
        assert lock.held
        lock.release()
        assert not lock.held

    def test_cross_process_acquire_blocks(self, tmp_path: Path) -> None:
        """A second process must fail to acquire the same lock file."""
        lock_file = tmp_path / ".lock"
        holder = Lock(lock_file)
        holder.acquire()
        try:
            script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from donna_recon.paths import Lock
try:
    Lock(Path({str(lock_file)!r})).acquire()
    print('ACQUIRED')
except BlockingIOError:
    print('BLOCKED')
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert "BLOCKED" in result.stdout, result.stdout + result.stderr
        finally:
            holder.release()

    def test_release_without_acquire_is_safe(self, tmp_path: Path) -> None:
        lock = Lock(tmp_path / ".lock")
        lock.release()  # no-op
