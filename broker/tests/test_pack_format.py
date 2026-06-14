# tests/test_pack_format.py
"""Tests for broker.pack_format (promoter design §3).

The contract under test is the security property: load_pack reads ONLY
meta.json, manifest.yaml, pack.sig, and direct-child files of schemas/ and
profiles/ — nothing else, no path traversal — and its canonicalisation is
deterministic and excludes pack.sig.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from broker import pack_format


def _write_pack(
    tmp_path: Path,
    *,
    meta: dict[str, Any] | None = None,
    manifest_yaml: str | None = None,
    schemas: dict[str, Any] | None = None,
    profiles: dict[str, Any] | None = None,
) -> Path:
    d = tmp_path / "p"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    meta = meta or {
        "pack_id": "waitrose",
        "version": 1,
        "created_utc": "2026-06-15T00:00:00Z",
        "description": "Waitrose groceries",
        "capabilities": ["browser_goal.plan"],
    }
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "manifest.yaml").write_text(
        manifest_yaml
        if manifest_yaml is not None
        else "capabilities:\n  - name: browser_goal.plan\n"
    )
    for name, body in (schemas or {}).items():
        (d / "schemas" / name).write_text(json.dumps(body))
    for name, body in (profiles or {}).items():
        (d / "profiles" / name).write_text(json.dumps(body))
    return d


def test_load_pack_reads_meta_and_content(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    pack = pack_format.load_pack(str(d))
    assert pack.pack_id == "waitrose"
    assert pack.version == 1
    assert "browser_goal.plan" in pack.capability_names


def test_load_pack_reads_schemas_and_profiles(tmp_path: Path) -> None:
    d = _write_pack(
        tmp_path,
        schemas={"x.json": {"type": "object"}},
        profiles={"waitrose.json": {"site": "waitrose"}},
    )
    pack = pack_format.load_pack(str(d))
    assert pack.schemas == {"x.json": {"type": "object"}}
    assert pack.profiles == {"waitrose.json": {"site": "waitrose"}}


def test_canonical_bytes_is_deterministic_regardless_of_key_order(tmp_path: Path) -> None:
    d1 = _write_pack(
        tmp_path / "a",
        meta={
            "version": 1,
            "pack_id": "x",
            "created_utc": "t",
            "description": "d",
            "capabilities": ["c"],
        },
    )
    d2 = _write_pack(
        tmp_path / "b",
        meta={
            "pack_id": "x",
            "capabilities": ["c"],
            "version": 1,
            "created_utc": "t",
            "description": "d",
        },
    )
    assert pack_format.canonical_bytes(
        pack_format.load_pack(str(d1))
    ) == pack_format.canonical_bytes(pack_format.load_pack(str(d2)))


def test_canonical_bytes_excludes_pack_sig(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    before = pack_format.canonical_bytes(pack_format.load_pack(str(d)))
    (d / "pack.sig").write_bytes(b"\x01\x02\x03")
    after = pack_format.canonical_bytes(pack_format.load_pack(str(d)))
    assert before == after


def test_signature_is_read_when_present(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    assert pack_format.load_pack(str(d)).signature is None
    (d / "pack.sig").write_bytes(b"\x01\x02\x03")
    assert pack_format.load_pack(str(d)).signature == b"\x01\x02\x03"


def test_pack_hash_changes_when_content_changes(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    h1 = pack_format.pack_hash(pack_format.load_pack(str(d)))
    (d / "meta.json").write_text(
        json.dumps(
            {
                "pack_id": "waitrose",
                "version": 2,
                "created_utc": "2026-06-15T00:00:00Z",
                "description": "x",
                "capabilities": ["browser_goal.plan"],
            }
        )
    )
    h2 = pack_format.pack_hash(pack_format.load_pack(str(d)))
    assert h1 != h2


def test_pack_hash_is_sha256_hex(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    h = pack_format.pack_hash(pack_format.load_pack(str(d)))
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_load_pack_rejects_missing_meta(tmp_path: Path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    (d / "manifest.yaml").write_text("capabilities:\n  - name: a\n")
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_missing_manifest(tmp_path: Path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps(
            {
                "pack_id": "x",
                "version": 1,
                "created_utc": "t",
                "description": "d",
                "capabilities": [],
            }
        )
    )
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_malformed_meta_json(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    (d / "meta.json").write_text("{ not json")
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_malformed_manifest_yaml(tmp_path: Path) -> None:
    d = _write_pack(tmp_path, manifest_yaml="capabilities: [unterminated\n")
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_meta_not_object(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    (d / "meta.json").write_text(json.dumps([1, 2, 3]))
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_manifest_without_capabilities(tmp_path: Path) -> None:
    d = _write_pack(tmp_path, manifest_yaml="other: 1\n")
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_meta_missing_required_field(tmp_path: Path) -> None:
    d = _write_pack(
        tmp_path,
        meta={"pack_id": "x", "version": 1, "created_utc": "t", "description": "d"},
    )
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_non_integer_version(tmp_path: Path) -> None:
    d = _write_pack(
        tmp_path,
        meta={
            "pack_id": "x",
            "version": "not-an-int",
            "created_utc": "t",
            "description": "d",
            "capabilities": ["c"],
        },
    )
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_malformed_schema_file(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    (d / "schemas" / "broken.json").write_text("{ not json")
    with pytest.raises(pack_format.PackFormatError):
        pack_format.load_pack(str(d))


def test_load_pack_handles_missing_schemas_and_profiles_dirs(tmp_path: Path) -> None:
    # A pack with no schemas/ or profiles/ subdirs at all is valid.
    d = tmp_path / "p"
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps(
            {
                "pack_id": "x",
                "version": 1,
                "created_utc": "t",
                "description": "d",
                "capabilities": ["c"],
            }
        )
    )
    (d / "manifest.yaml").write_text("capabilities:\n  - name: c\n")
    pack = pack_format.load_pack(str(d))
    assert pack.schemas == {}
    assert pack.profiles == {}


def test_load_pack_ignores_files_outside_the_known_set(tmp_path: Path) -> None:
    # The security property: load_pack reads ONLY meta.json, manifest.yaml,
    # pack.sig, and direct children of schemas/ and profiles/. A stray file
    # dropped into the pack root (or a nested subdir of schemas/) must not be
    # read into the canonical content.
    d = _write_pack(tmp_path, schemas={"x.json": {"a": 1}})
    baseline = pack_format.canonical_bytes(pack_format.load_pack(str(d)))

    # Stray file in the pack root — not one of the four known names.
    (d / "README.txt").write_text("ignore me")
    # Stray file under a nested subdir of schemas/ — not a direct child.
    nested = d / "schemas" / "nested"
    nested.mkdir()
    (nested / "deep.json").write_text(json.dumps({"sneaky": True}))

    after = pack_format.canonical_bytes(pack_format.load_pack(str(d)))
    assert after == baseline


def test_load_pack_rejects_path_separator_in_schema_name(tmp_path: Path) -> None:
    # Directly exercise the name guard: a file whose intended name contains a
    # path separator or '..' must be rejected. Writing "schemas/../evil.json"
    # actually lands OUTSIDE schemas/ (one level up), so to assert the guard we
    # construct a child of schemas/ whose name contains the forbidden token by
    # using a real entry that the guard must reject.
    d = _write_pack(tmp_path)
    # Place a file literally named with a traversal token inside schemas/.
    # On the filesystem this is a single directory entry named "..evil.json"
    # which is a normal child; but the guard rejects any name containing '..'.
    (d / "schemas" / "..evil.json").write_text(json.dumps({"x": 1}))
    with pytest.raises(pack_format.PackFormatError, match="unsafe"):
        pack_format.load_pack(str(d))


def test_load_pack_rejects_traversal_token_in_profile_name(tmp_path: Path) -> None:
    d = _write_pack(tmp_path)
    (d / "profiles" / "..secret.json").write_text(json.dumps({"x": 1}))
    with pytest.raises(pack_format.PackFormatError, match="unsafe"):
        pack_format.load_pack(str(d))
