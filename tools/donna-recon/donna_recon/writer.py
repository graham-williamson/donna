"""File writes with security-relevant perms (0700 dirs, 0600 files) + fsync.

Callers must have set ``umask(0o077)`` first — ``cli.py`` does this once at
subcommand entry. Explicit mode args are passed to ``os.open`` / ``mkdir``
for defence-in-depth against a mis-set umask.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from donna_recon.dom_redact import sanitise_html


def ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with mode 0700. Idempotent."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append *obj* as one JSON line, fsync before returning."""
    data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_snapshot_html(path: Path, html: str) -> None:
    """Sanitise *html* then write to *path* with 0600 perms."""
    sanitised = sanitise_html(html).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, sanitised)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_snapshot_png(path: Path, png_bytes: bytes) -> None:
    """Write screenshot *png_bytes* to *path* with 0600 perms."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, png_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_meta(path: Path, meta: dict[str, Any]) -> None:
    """Atomically write *meta* to *path* (tmp + fsync + rename)."""
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(meta, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
