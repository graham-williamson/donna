# tests/test_promoter_fs.py
"""Tests for promoter_fs — staged verify, atomic merge, rollback (design §6e–g).

The two rollback contracts are non-negotiable and each has a test:
  (1) a malformed pack fails at STAGED verification -> the live
      capabilities.yaml is byte-for-byte UNCHANGED (live never touched);
  (2) a post-merge re-verify failure -> the live dir is RESTORED
      byte-for-byte from backup.
Plus a property: NO temp dirs (backup/staged/old) leak on success OR failure.

publish_to_config copies ONLY the manifest artifacts (capabilities.yaml,
mcp-tools.yaml, schemas/, profiles/) from the manifests-only live_dir into the
broker config dir — the dir the broker actually reads, which ALSO holds the
requests DB and the age vault. The security guarantee proven here: a publish
NEVER touches requests.db / creds/ (or anything else) in config_dir.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from broker import pack_format, promoter_fs, validator


def _live(tmp_path: Path) -> Path:
    d = tmp_path / "manifests"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    (d / "capabilities.yaml").write_text(yaml.safe_dump({"capabilities": [
        {"name": "gmail.send",
         "executor": {"type": "mcp_tool", "tool": "mcp__x__y"},
         "param_schema": {"$ref": "./schemas/gmail.json"},
         "params_exact_match_required": True, "derived_fields_allowed": [],
         "risk_level": "high", "revalidate": {"not_applicable": "stateless_write"},
         "idempotency_date_from": "created_utc",
         "approval_window_minutes": 60, "execution_window_minutes": 60}]}))
    (d / "schemas" / "gmail.json").write_text(json.dumps({"type": "object"}))
    return d


def _pack(tmp_path: Path, name: str = "site.read",
          schema: str = "site.json") -> pack_format.Pack:
    d = tmp_path / "pack"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    (d / "meta.json").write_text(json.dumps({"pack_id": "site", "version": 1,
        "created_utc": "t", "description": "d", "capabilities": [name]}))
    (d / "manifest.yaml").write_text(yaml.safe_dump({"capabilities": [
        {"name": name, "executor": {"type": "mcp_tool", "tool": "mcp__a__b"},
         "param_schema": {"$ref": f"./schemas/{schema}"},
         "params_exact_match_required": True, "derived_fields_allowed": [],
         "risk_level": "low", "revalidate": {"not_applicable": "stateless_write"},
         "idempotency_date_from": "created_utc",
         "approval_window_minutes": 60, "execution_window_minutes": 60}]}))
    (d / "schemas" / schema).write_text(json.dumps({"type": "object"}))
    return pack_format.load_pack(str(d))


def _leftover_temp_dirs(parent: Path) -> list[str]:
    return sorted(c.name for c in parent.iterdir()
                  if c.is_dir() and c.name.startswith("promoter-"))


def test_install_merges_pack_into_live(tmp_path: Path) -> None:
    live = _live(tmp_path)
    pack = _pack(tmp_path)
    promoter_fs.install(pack, str(live))
    merged = yaml.safe_load((live / "capabilities.yaml").read_text())
    names = {c["name"] for c in merged["capabilities"]}
    assert names == {"gmail.send", "site.read"}
    assert (live / "schemas" / "site.json").exists()


def test_install_leaves_no_temp_dirs_on_success(tmp_path: Path) -> None:
    live = _live(tmp_path)
    pack = _pack(tmp_path)
    promoter_fs.install(pack, str(live))
    assert _leftover_temp_dirs(tmp_path) == []


def test_malformed_pack_fails_at_staging_no_live_change(tmp_path: Path) -> None:
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack = _pack(tmp_path)
    # break it: schema $ref points at a missing file
    object.__setattr__(pack, "manifest", {"capabilities": [
        {"name": "bad", "executor": {"type": "mcp_tool", "tool": "t"},
         "param_schema": {"$ref": "./schemas/MISSING.json"},
         "params_exact_match_required": True, "derived_fields_allowed": [],
         "risk_level": "low", "revalidate": {"not_applicable": "stateless_write"},
         "idempotency_date_from": "created_utc",
         "approval_window_minutes": 1, "execution_window_minutes": 1}]})
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.install(pack, str(live))
    assert (live / "capabilities.yaml").read_text() == before   # untouched
    assert _leftover_temp_dirs(tmp_path) == []                   # no leak


def test_rollback_restores_on_post_merge_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack = _pack(tmp_path)
    # force the POST-merge verify to fail
    calls = {"n": 0}
    real = validator.load_capabilities

    def flaky(path: str) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:   # staged verify ok, live re-verify fails
            raise validator.ManifestError("boom")
        return real(path)

    monkeypatch.setattr(validator, "load_capabilities", flaky)
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.install(pack, str(live))
    assert (live / "capabilities.yaml").read_text() == before   # restored
    assert (live / "schemas" / "gmail.json").exists()           # full set restored
    assert _leftover_temp_dirs(tmp_path) == []                  # no leak


def test_missing_live_capabilities_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    pack = _pack(tmp_path)
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.install(pack, str(empty))
    assert _leftover_temp_dirs(tmp_path) == []


def test_rollback_when_swap_leaves_live_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the staged->live move fails AFTER live was moved aside, the inner
    handler restores from the moved-aside copy (live.exists() is False so the
    rmtree is skipped). Covers the False side of the post-swap branch."""
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack = _pack(tmp_path)
    orig_rename = Path.rename

    def boom_rename(self: Path, target: Any) -> Any:
        # The staged->live rename is the one whose destination is `live`.
        if Path(target) == live and self.name.startswith("promoter-staged-"):
            raise OSError("simulated cross-device swap failure")
        return orig_rename(self, target)

    monkeypatch.setattr(Path, "rename", boom_rename)
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.install(pack, str(live))
    assert (live / "capabilities.yaml").read_text() == before   # restored
    assert _leftover_temp_dirs(tmp_path) == []


def test_defence_in_depth_restores_from_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders: if an InstallError propagates with live missing
    (e.g. the inner rollback itself failed to put live back), the outer handler
    restores the independent byte-for-byte backup."""
    live = _live(tmp_path)
    before = (live / "capabilities.yaml").read_text()
    pack = _pack(tmp_path)

    calls = {"n": 0}
    real = validator.load_capabilities
    orig_rename = Path.rename

    def flaky(path: str) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:   # staged ok, live re-verify fails -> rollback
            raise validator.ManifestError("boom")
        return real(path)

    def half_broken_rename(self: Path, target: Any) -> Any:
        # Let live move aside, but make the rollback restore (old -> live) fail
        # so live is left MISSING when the InstallError reaches the outer block.
        if Path(target) == live and self.name.startswith("promoter-old-"):
            raise OSError("simulated rollback-restore failure")
        return orig_rename(self, target)

    monkeypatch.setattr(validator, "load_capabilities", flaky)
    monkeypatch.setattr(Path, "rename", half_broken_rename)
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.install(pack, str(live))
    # Outer defence-in-depth used the independent backup to restore live.
    assert (live / "capabilities.yaml").read_text() == before
    assert (live / "schemas" / "gmail.json").exists()
    assert _leftover_temp_dirs(tmp_path) == []


# ---- publish_to_config: publish merged manifest into the broker config dir,
#      NEVER touching the requests DB or age vault that also live there. ------


def _config_dir_with_secrets(tmp_path: Path) -> Path:
    """A broker config dir holding the live secrets the publisher must NEVER
    touch: a sentinel requests.db, an hmac.key, an age vault under creds/, and
    an approval-queue/ entry."""
    cfg = tmp_path / "config"
    (cfg / "creds").mkdir(parents=True)
    (cfg / "approval-queue").mkdir()
    (cfg / "requests.db").write_bytes(b"SENTINEL-DB-BYTES-do-not-touch")
    (cfg / "hmac.key").write_bytes(b"SENTINEL-HMAC-KEY")
    (cfg / "creds" / "identity.age").write_bytes(b"SENTINEL-AGE-IDENTITY")
    (cfg / "approval-queue" / "q1.json").write_text("{}", encoding="utf-8")
    return cfg


def _stat_snapshot(p: Path) -> tuple[bytes, int, int]:
    """(bytes, inode, mtime_ns) — a strong fingerprint for 'untouched'."""
    st = p.stat()
    return (p.read_bytes(), st.st_ino, st.st_mtime_ns)


def test_publish_copies_capabilities_and_schemas(tmp_path: Path) -> None:
    live = _live(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    promoter_fs.publish_to_config(str(live), str(cfg))
    assert (cfg / "capabilities.yaml").read_text() == (
        live / "capabilities.yaml"
    ).read_text()
    assert (cfg / "schemas" / "gmail.json").read_text() == (
        live / "schemas" / "gmail.json"
    ).read_text()


def test_publish_never_touches_db_or_creds(tmp_path: Path) -> None:
    """THE security guarantee: publishing into a config dir that also holds the
    requests DB and the age vault leaves those sentinel files byte-for-byte,
    inode-for-inode, and mtime-for-mtime UNTOUCHED."""
    live = _live(tmp_path)
    cfg = _config_dir_with_secrets(tmp_path)

    db = cfg / "requests.db"
    hmac_key = cfg / "hmac.key"
    age = cfg / "creds" / "identity.age"
    queue = cfg / "approval-queue" / "q1.json"
    before = {p: _stat_snapshot(p) for p in (db, hmac_key, age, queue)}

    promoter_fs.publish_to_config(str(live), str(cfg))

    # The manifest landed...
    assert (cfg / "capabilities.yaml").is_file()
    assert (cfg / "schemas" / "gmail.json").is_file()
    # ...and EVERY secret is identical: same bytes, same inode, same mtime.
    for p, snap in before.items():
        assert _stat_snapshot(p) == snap, f"publish modified {p.name}"
    # The creds/ and approval-queue/ dirs were never renamed/recreated.
    assert (cfg / "creds").is_dir()
    assert (cfg / "approval-queue").is_dir()
    # No stray publish temp file leaked into the secrets-bearing config dir.
    assert [p.name for p in cfg.iterdir() if p.name.startswith(".promoter-pub-")] == []
    # config_dir gained ONLY the manifest artifacts — nothing else appeared.
    assert {p.name for p in cfg.iterdir()} == {
        "requests.db", "hmac.key", "creds", "approval-queue",
        "capabilities.yaml", "schemas",
    }


def test_publish_creates_schemas_dir_if_absent(tmp_path: Path) -> None:
    live = _live(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    assert not (cfg / "schemas").exists()
    promoter_fs.publish_to_config(str(live), str(cfg))
    assert (cfg / "schemas" / "gmail.json").is_file()


def test_publish_copies_mcp_tools_only_if_present(tmp_path: Path) -> None:
    live = _live(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    # No mcp-tools.yaml in live -> none in config.
    promoter_fs.publish_to_config(str(live), str(cfg))
    assert not (cfg / "mcp-tools.yaml").exists()

    # Now add one and republish -> it lands.
    (live / "mcp-tools.yaml").write_text("tools: []\n", encoding="utf-8")
    promoter_fs.publish_to_config(str(live), str(cfg))
    assert (cfg / "mcp-tools.yaml").read_text() == "tools: []\n"


def test_publish_copies_profiles_if_present(tmp_path: Path) -> None:
    live = _live(tmp_path)
    (live / "profiles" / "p1.json").write_text('{"a": 1}', encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir()
    promoter_fs.publish_to_config(str(live), str(cfg))
    assert (cfg / "profiles" / "p1.json").read_text() == '{"a": 1}'


def test_publish_invalid_manifest_raises(tmp_path: Path) -> None:
    """If the merged manifest does not parse once published, publish raises
    InstallError (the merge into live already stands; publish failed)."""
    live = _live(tmp_path)
    # Corrupt the live capabilities.yaml so the post-publish validate fails.
    (live / "capabilities.yaml").write_text("capabilities: not-a-list\n", encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir()
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.publish_to_config(str(live), str(cfg))


def test_publish_missing_capabilities_raises(tmp_path: Path) -> None:
    """publish of a live dir with no capabilities.yaml fails closed."""
    live = tmp_path / "empty-live"
    live.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.publish_to_config(str(live), str(cfg))


def test_publish_oserror_raises_install_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any OSError during the copy fails closed as InstallError."""
    live = _live(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()

    def boom_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("os.replace", boom_replace)
    with pytest.raises(promoter_fs.InstallError):
        promoter_fs.publish_to_config(str(live), str(cfg))
