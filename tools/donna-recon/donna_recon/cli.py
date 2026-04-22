"""CLI entry points for donna-recon.

Foreground-first: ``start`` blocks until SIGINT/SIGTERM. Lifecycle owned by
this module — the recorder just runs the async event loop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from donna_recon import __version__, paths
from donna_recon.browser import (
    allocate_ephemeral_port,
    ephemeral_profile_path,
    get_chrome_version,
    launch_chromium,
    wait_for_cdp,
)
from donna_recon.recorder import Recorder
from donna_recon.writer import ensure_dir, write_meta


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _playwright_version() -> str:
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:
        return "unknown"


# ─────────────────────────── start ───────────────────────────


def cmd_start(args: argparse.Namespace) -> int:
    os.umask(0o077)

    lock = paths.Lock()
    ensure_dir(paths.root())
    try:
        lock.acquire()
    except BlockingIOError:
        holder = _read_holder_pid()
        sys.stderr.write(
            "donna-recon: another recording is already active"
            + (f" (pid {holder})" if holder else "")
            + "\n"
        )
        return 1

    try:
        paths.cleanup_stale()
        if paths.read_current() is not None:
            sys.stderr.write(
                "donna-recon: .current still points at a live recording — refusing.\n"
            )
            return 1

        recording_id = paths.new_recording_id()
        recording_dir = paths.root() / recording_id
        ensure_dir(recording_dir)
        ensure_dir(paths.snapshots_dir(recording_dir))

        # Write our pid + .current before touching Chromium so a crash mid-launch
        # leaves a traceable breadcrumb.
        paths.pid_path(recording_dir).write_text(f"{os.getpid()}\n")
        os.chmod(paths.pid_path(recording_dir), 0o600)
        paths.write_current(recording_dir)

        port = allocate_ephemeral_port()
        profile_dir = ephemeral_profile_path(recording_id)
        start_url = args.url or "about:blank"

        chrome_proc = launch_chromium(profile_dir, port, start_url)

        try:
            wait_for_cdp(port, timeout=10.0)
        except TimeoutError as e:
            _terminate(chrome_proc)
            sys.stderr.write(f"donna-recon: {e}\n")
            return 1

        chrome_version = get_chrome_version(port)

        meta: dict[str, Any] = {
            "started_at": _iso_now(),
            "stopped_at": None,
            "start_url": start_url,
            "chrome_version": chrome_version,
            "playwright_version": _playwright_version(),
            "tool_version": __version__,
            "cdp_port": port,
            "ephemeral_profile_wiped": False,
        }
        write_meta(paths.meta_path(recording_dir), meta)

        sys.stdout.write(f"{recording_dir}\n")
        sys.stdout.write(f"cdp: 127.0.0.1:{port}\n")
        sys.stdout.flush()

        recorder = Recorder(recording_dir, port)

        async def _run_with_signals() -> None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, recorder.request_stop)
            await recorder.run()

        asyncio.run(_run_with_signals())

        _terminate(chrome_proc)

        wiped = _wipe_profile_prompt(profile_dir)

        meta["stopped_at"] = _iso_now()
        meta["ephemeral_profile_wiped"] = wiped
        if not wiped:
            meta["ephemeral_profile_dir"] = str(profile_dir)
        write_meta(paths.meta_path(recording_dir), meta)

        # Clean up live-session pointer — recording dir stays.
        try:
            paths.pid_path(recording_dir).unlink()
        except FileNotFoundError:
            pass
        paths.clear_current()

        sys.stdout.write(f"donna-recon: stopped. recording: {recording_dir}\n")
        return 0
    finally:
        lock.release()


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        with _swallow():
            proc.wait(timeout=2)


def _wipe_profile_prompt(profile_dir: Path) -> bool:
    if not profile_dir.exists():
        return True
    if not sys.stdin.isatty():
        shutil.rmtree(profile_dir, ignore_errors=True)
        return True
    try:
        ans = input(
            f"donna-recon: wipe ephemeral profile {profile_dir}? [Y/n] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "y"
    if ans in ("", "y", "yes"):
        shutil.rmtree(profile_dir, ignore_errors=True)
        return True
    return False


def _read_holder_pid() -> int | None:
    cur = paths.read_current()
    if cur is None:
        return None
    try:
        return int(paths.pid_path(cur).read_text().strip())
    except (OSError, ValueError):
        return None


class _swallow:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: Any) -> bool:
        return True


# ─────────────────────────── stop ───────────────────────────


def cmd_stop(_args: argparse.Namespace) -> int:
    cur = paths.read_current()
    if cur is None:
        sys.stderr.write("donna-recon: no active recording\n")
        return 1
    pid_file = paths.pid_path(cur)
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        sys.stderr.write(
            "donna-recon: cannot read recorder.pid — "
            "recording may already be stopping or crashed.\n"
        )
        return 1
    if not paths.is_pid_alive(pid):
        sys.stderr.write(f"donna-recon: pid {pid} is not alive\n")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        sys.stderr.write(f"donna-recon: pid {pid} vanished before signal\n")
        return 1
    # Wait for the recorder to flush and release.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not paths.is_pid_alive(pid):
            sys.stdout.write("donna-recon: stopped\n")
            return 0
        time.sleep(0.25)
    sys.stderr.write(
        f"donna-recon: pid {pid} did not exit within 10s — may need manual cleanup\n"
    )
    return 1


# ─────────────────────────── mark ───────────────────────────


def cmd_mark(args: argparse.Namespace) -> int:
    cur = paths.read_current()
    if cur is None:
        sys.stderr.write("donna-recon: no active recording\n")
        return 1
    label = args.label.strip()
    if not label:
        sys.stderr.write("donna-recon: label must be non-empty\n")
        return 1
    req = paths.mark_req_path(cur)
    # O_EXCL would be safer but the watcher deletes the file, so a genuine
    # tight-loop mark should still land; prefer O_TRUNC for forgiving retry.
    fd = os.open(req, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, label.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    sys.stdout.write(f"donna-recon: mark queued — {label}\n")
    return 0


# ─────────────────────────── list ───────────────────────────


def cmd_list(_args: argparse.Namespace) -> int:
    root = paths.root()
    if not root.exists():
        sys.stdout.write("donna-recon: no recordings\n")
        return 0
    rows: list[tuple[str, str, str, int]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_file = paths.meta_path(d)
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        snaps = paths.snapshots_dir(d)
        count = 0
        if snaps.exists():
            count = sum(1 for _ in snaps.glob("*.html"))
        rows.append(
            (
                d.name,
                str(meta.get("start_url", "")),
                str(meta.get("stopped_at") or "(active)"),
                count,
            )
        )
    if not rows:
        sys.stdout.write("donna-recon: no recordings\n")
        return 0
    sys.stdout.write(f"{'id':<22} {'snaps':>5}  {'stopped':<20}  url\n")
    for rid, url, stopped, count in rows:
        sys.stdout.write(f"{rid:<22} {count:>5}  {stopped:<20}  {url}\n")
    return 0


# ─────────────────────────── show ───────────────────────────


def cmd_show(args: argparse.Namespace) -> int:
    rec = paths.root() / args.id
    if not rec.exists():
        sys.stderr.write(f"donna-recon: no such recording: {args.id}\n")
        return 1
    meta_file = paths.meta_path(rec)
    if meta_file.exists():
        sys.stdout.write("meta:\n")
        sys.stdout.write(meta_file.read_text())
        sys.stdout.write("\n\n")

    trace_file = paths.trace_path(rec)
    if trace_file.exists():
        counts: dict[str, int] = {}
        markers: list[str] = []
        for line in trace_file.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = str(ev.get("type", "?"))
            counts[t] = counts.get(t, 0) + 1
            if t == "marker":
                markers.append(f"  [{ev.get('seq')}] {ev.get('label')} — {ev.get('url')}")
        sys.stdout.write("trace summary:\n")
        for k in sorted(counts):
            sys.stdout.write(f"  {k}: {counts[k]}\n")
        if markers:
            sys.stdout.write("\nmarkers:\n")
            for m in markers:
                sys.stdout.write(m + "\n")
        sys.stdout.write("\n")

    snaps = paths.snapshots_dir(rec)
    if snaps.exists():
        files = sorted(f.name for f in snaps.glob("*.html"))
        sys.stdout.write(f"snapshots ({len(files)}):\n")
        for f in files:
            sys.stdout.write(f"  {f}\n")
    return 0


# ─────────────────────────── entry ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="donna-recon",
        description="Browser-recon recorder for spec'ing Donna capability executors.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="launch Chromium and begin recording")
    start.add_argument("--url", default=None, help="initial URL (default about:blank)")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop the active recording")
    stop.set_defaults(func=cmd_stop)

    mark = sub.add_parser("mark", help="queue a marker with a label (F9 fallback)")
    mark.add_argument("label", help="short description of the current page state")
    mark.set_defaults(func=cmd_mark)

    ls = sub.add_parser("list", help="list past recordings")
    ls.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="summarise a recording by id")
    show.add_argument("id", help="recording id (directory name under ~/.donna-recon/)")
    show.set_defaults(func=cmd_show)

    return p


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
