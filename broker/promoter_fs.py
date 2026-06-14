# promoter_fs.py
"""Filesystem install pipeline with rollback (design §6e–g).

Stage the merged manifest, verify it (validator.load_capabilities — the same
gate the broker uses at startup), atomically swap it into place, re-verify,
and restore the backup on any failure. No subprocess, no network. The broker
restart is the orchestrator's job (promoter.py), kept out of here.

Two rollback contracts (each has a test):
  1. staged-verify fails  -> live is NEVER touched (byte-for-byte unchanged);
  2. post-merge re-verify  -> live is RESTORED byte-for-byte from the moved-aside
     copy.
And a property: no ``promoter-*`` temp dir leaks on success OR failure.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import yaml

from broker import pack_format, validator


class InstallError(Exception):
    """Staging, verification, merge, or rollback failed. Fail-closed — the
    orchestrator records a refusal; the live manifests are left valid."""


def _append_capabilities(cap_yaml: Path, pack: pack_format.Pack) -> None:
    data = yaml.safe_load(cap_yaml.read_text(encoding="utf-8"))
    data["capabilities"].extend(pack.manifest["capabilities"])
    cap_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_pack_files(staged: Path, pack: pack_format.Pack) -> None:
    for sub, blobs in (("schemas", pack.schemas), ("profiles", pack.profiles)):
        target = staged / sub
        target.mkdir(exist_ok=True)
        for name, body in blobs.items():
            (target / name).write_text(json.dumps(body), encoding="utf-8")


def _fresh_tmp_dir(parent: Path, prefix: str) -> Path:
    """An empty, named temp directory under ``parent``. ``copytree`` needs the
    destination to NOT exist, so we mkdtemp (reserve the unique name) then
    remove it, leaving the path free for a copytree/rename into it."""
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    shutil.rmtree(d)
    return d


def install(pack: pack_format.Pack, live_dir: str) -> None:
    """Merge ``pack`` into the live manifests at ``live_dir`` with full
    rollback safety. Raises InstallError on any failure, leaving the live
    manifests valid (untouched if staged-verify failed, restored otherwise)."""
    live = Path(live_dir)
    cap_yaml = live / "capabilities.yaml"
    if not cap_yaml.is_file():
        raise InstallError(f"live manifests missing capabilities.yaml: {live_dir}")
    parent = live.parent

    backup = _fresh_tmp_dir(parent, "promoter-backup-")
    staged = _fresh_tmp_dir(parent, "promoter-staged-")
    old: Path | None = None
    try:
        # backup + stage are independent copies of the current live set.
        shutil.copytree(live, backup)
        shutil.copytree(live, staged)

        # Build the merged set in staging (live untouched so far).
        _append_capabilities(staged / "capabilities.yaml", pack)
        _write_pack_files(staged, pack)

        # Verify staged BEFORE touching live. Failure here = contract (1):
        # live is never touched.
        try:
            validator.load_capabilities(str(staged / "capabilities.yaml"))
        except validator.ManifestError as e:
            raise InstallError(f"staged manifest invalid: {e}") from e

        # Atomic-ish swap: move live aside, move staged into place, re-verify.
        old = _fresh_tmp_dir(parent, "promoter-old-")
        live.rename(old)
        try:
            staged.rename(live)
            validator.load_capabilities(str(cap_yaml))   # re-verify live
        except (OSError, validator.ManifestError) as e:
            # Contract (2): remove the partial/invalid live and restore the
            # byte-for-byte copy we moved aside. If even this restore fails,
            # the outer handler's independent-backup restore is the safety net.
            if live.exists():
                shutil.rmtree(live, ignore_errors=True)
            try:
                old.rename(live)
                old = None  # restored into place; nothing left to clean
            except OSError:
                pass  # leave live missing; outer block restores from backup
            raise InstallError(f"post-merge verify failed, rolled back: {e}") from e
    except InstallError:
        # Defence in depth: if any path above left live missing, restore from
        # the independent backup before propagating.
        if not cap_yaml.is_file() and backup.is_dir():
            if live.exists():
                shutil.rmtree(live, ignore_errors=True)
            shutil.copytree(backup, live)
        raise
    finally:
        # No temp dir may leak on success OR failure.
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(staged, ignore_errors=True)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
