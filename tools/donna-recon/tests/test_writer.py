"""Tests for donna_recon.writer."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from donna_recon.writer import (
    append_jsonl,
    ensure_dir,
    write_meta,
    write_snapshot_html,
    write_snapshot_png,
)


@pytest.fixture(autouse=True)
def strict_umask():
    """Match the CLI's umask so tests exercise the real perm path."""
    prev = os.umask(0o077)
    yield
    os.umask(prev)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestEnsureDir:
    def test_creates_with_0700(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b"
        ensure_dir(target)
        assert target.is_dir()
        assert _mode(target) == 0o700

    def test_idempotent(self, tmp_path: Path) -> None:
        target = tmp_path / "a"
        ensure_dir(target)
        ensure_dir(target)
        assert target.is_dir()


class TestAppendJsonl:
    def test_appends_two_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        append_jsonl(path, {"k": 1})
        append_jsonl(path, {"k": 2})
        lines = path.read_text().splitlines()
        assert [json.loads(l) for l in lines] == [{"k": 1}, {"k": 2}]

    def test_file_perms_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        append_jsonl(path, {"k": 1})
        assert _mode(path) == 0o600


class TestWriteSnapshotHtml:
    def test_sanitises_before_write(self, tmp_path: Path) -> None:
        path = tmp_path / "0001.html"
        write_snapshot_html(
            path, '<input type="password" value="hunter2">'
        )
        content = path.read_text()
        assert "hunter2" not in content
        assert 'data-donna-redacted="password"' in content

    def test_perms_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "0001.html"
        write_snapshot_html(path, "<html><body>ok</body></html>")
        assert _mode(path) == 0o600


class TestWriteSnapshotPng:
    def test_bytes_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "0001.png"
        blob = b"\x89PNG\r\n\x1a\nopaque"
        write_snapshot_png(path, blob)
        assert path.read_bytes() == blob

    def test_perms_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "0001.png"
        write_snapshot_png(path, b"x")
        assert _mode(path) == 0o600


class TestWriteMeta:
    def test_atomic_tmp_cleaned(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        write_meta(path, {"foo": "bar"})
        assert path.exists()
        assert not (tmp_path / "meta.json.tmp").exists()

    def test_content(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        write_meta(path, {"a": 1, "b": "x"})
        assert json.loads(path.read_text()) == {"a": 1, "b": "x"}

    def test_perms_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        write_meta(path, {})
        assert _mode(path) == 0o600

    def test_overwrites(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        write_meta(path, {"v": 1})
        write_meta(path, {"v": 2})
        assert json.loads(path.read_text())["v"] == 2
