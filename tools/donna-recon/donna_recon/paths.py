"""Layout of ``~/.donna-recon/`` + single-instance lock + stale cleanup.

The lock is a ``fcntl.flock`` on ``<root>/.lock``. The kernel drops it on
process exit — including crash — so there is never a stale lock to clear.
The ``.current`` pointer file can be stale after a crash, though; that's
what ``cleanup_stale`` handles.
"""
from __future__ import annotations

import errno
import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path.home() / ".donna-recon"


def root() -> Path:
    return ROOT


def lock_path() -> Path:
    return ROOT / ".lock"


def current_path() -> Path:
    return ROOT / ".current"


def pid_path(recording_dir: Path) -> Path:
    return recording_dir / "recorder.pid"


def meta_path(recording_dir: Path) -> Path:
    return recording_dir / "meta.json"


def trace_path(recording_dir: Path) -> Path:
    return recording_dir / "trace.jsonl"


def network_path(recording_dir: Path) -> Path:
    return recording_dir / "network.jsonl"


def snapshots_dir(recording_dir: Path) -> Path:
    return recording_dir / "snapshots"


def mark_req_path(recording_dir: Path) -> Path:
    return recording_dir / "mark.req"


def new_recording_id(now: datetime | None = None) -> str:
    """Return an ISO-8601 UTC identifier usable as a directory name."""
    now = now or datetime.now(timezone.utc)
    # Colons are fine on APFS but awkward in shells — use hyphens.
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def is_pid_alive(pid: int) -> bool:
    """Heuristic liveness check via ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM means the process exists but we don't own it — still alive.
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        raise
    return True


def read_current() -> Path | None:
    """Return the recording dir named by .current, or None if missing."""
    p = current_path()
    if not p.exists():
        return None
    try:
        contents = p.read_text().strip()
    except OSError:
        return None
    if not contents:
        return None
    return Path(contents)


def write_current(recording_dir: Path) -> None:
    """Atomically point .current at *recording_dir*."""
    tmp = current_path().with_name(".current.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, str(recording_dir).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, current_path())


def clear_current() -> None:
    try:
        current_path().unlink()
    except FileNotFoundError:
        pass


def cleanup_stale() -> None:
    """If .current points at a recording whose recorder.pid is dead, clear it.

    Called under the lock, so it races with nothing.
    """
    rec = read_current()
    if rec is None:
        return
    pid_file = pid_path(rec)
    try:
        pid_text = pid_file.read_text().strip()
    except (OSError, ValueError):
        clear_current()
        return
    try:
        pid = int(pid_text)
    except ValueError:
        clear_current()
        return
    if not is_pid_alive(pid):
        clear_current()


class Lock:
    """Non-blocking exclusive flock on ``<root>/.lock``.

    Usage::

        lock = Lock()
        lock.acquire()           # raises BlockingIOError if held
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or lock_path()
        self._fd: int = -1

    def acquire(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd != -1:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = -1

    @property
    def held(self) -> bool:
        return self._fd != -1
