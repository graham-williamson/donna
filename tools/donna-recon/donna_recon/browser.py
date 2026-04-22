"""Chromium launcher + CDP readiness poll.

Launches Playwright's bundled Chromium as a separate process bound to an
ephemeral loopback port. The attach side (``recorder.py``) then connects
over CDP to that port.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


EPHEMERAL_PROFILE_ROOT = Path("/tmp")


def allocate_ephemeral_port() -> int:
    """Bind a throwaway socket to 127.0.0.1:0 and return the assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def ephemeral_profile_path(recording_id: str) -> Path:
    """Build the sandboxed user-data-dir path for a recording."""
    return EPHEMERAL_PROFILE_ROOT / f"donna-recon-{recording_id}"


def _chromium_executable() -> str:
    """Return the path to Playwright's bundled Chromium."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return str(p.chromium.executable_path)


def launch_chromium(
    user_data_dir: Path,
    port: int,
    start_url: str,
) -> subprocess.Popen[bytes]:
    """Spawn Chromium with CDP on the given loopback port.

    Safety invariants enforced here:
      - Bind address pinned to 127.0.0.1 (never 0.0.0.0).
      - ``user_data_dir`` must live under /tmp — the caller is expected to
        have built the path via ``ephemeral_profile_path``; we verify.
    """
    if user_data_dir.resolve().parent != EPHEMERAL_PROFILE_ROOT.resolve():
        raise ValueError(
            f"user_data_dir must be a direct child of {EPHEMERAL_PROFILE_ROOT}, "
            f"got {user_data_dir}"
        )
    user_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    args = [
        _chromium_executable(),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        start_url,
    ]
    return subprocess.Popen(
        args,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_cdp(port: int, timeout: float = 10.0) -> None:
    """Block until http://127.0.0.1:<port>/json/version responds."""
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(0.1)
    raise TimeoutError(
        f"CDP not ready on 127.0.0.1:{port} after {timeout}s (last: {last_err})"
    )


def get_chrome_version(port: int) -> str:
    """Fetch the ``Browser`` string from /json/version after CDP is up."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown"
    browser = payload.get("Browser")
    return str(browser) if browser else "unknown"
