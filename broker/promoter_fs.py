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

publish_to_config (the second half of the pipeline) copies the merged manifest
artifacts from the manifests-only ``live_dir`` into the broker's *config* dir —
the dir the broker actually reads at ``<config>/capabilities.yaml`` (with schema
``$ref``s resolved relative to that file). That config dir ALSO holds the
requests DB and the age vault, so publish is a PER-FILE atomic copy of ONLY the
manifest artifacts (capabilities.yaml, mcp-tools.yaml, schemas/, profiles/) —
it NEVER does a whole-directory swap and NEVER touches ``requests.db``,
``hmac.key``, ``creds/``, ``approval-queue/``, or anything else in there. It
mirrors ``ops/deploy-manifests.sh``'s safe per-file ``install(1)`` pattern.
"""
from __future__ import annotations

import json
import os
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


def _atomic_copy_file(src: Path, dst: Path) -> None:
    """Copy ``src`` onto ``dst`` atomically: write the bytes to a temp file in
    the SAME directory as ``dst`` (so ``os.replace`` is a same-filesystem rename,
    never a cross-device copy) then ``os.replace`` it into place. Replacing only
    ``dst`` means no other entry in its directory is ever renamed or touched."""
    data = src.read_bytes()
    fd, tmp_name = tempfile.mkstemp(prefix=".promoter-pub-", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, dst)
    except BaseException:
        # Never leak the temp file if the replace failed.
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def publish_to_config(live_dir: str, config_dir: str) -> None:
    """Publish the merged manifest from the manifests-only ``live_dir`` into the
    broker's config dir (the dir the broker reads), copying ONLY the manifest
    artifacts via per-file atomic replace.

    Copies, each via ``os.replace`` onto the exact target file:
      * ``capabilities.yaml`` (required);
      * ``mcp-tools.yaml`` IF present in ``live_dir``;
      * every ``*.json`` under ``live_dir/schemas/`` -> ``config_dir/schemas/``
        (created if missing);
      * every file under ``live_dir/profiles/`` -> ``config_dir/profiles/``
        (created if missing).

    It touches NOTHING else in ``config_dir`` — never ``requests.db``,
    ``hmac.key``, ``creds/``, ``approval-queue/``, and it NEVER renames, rmtrees,
    or replaces ``config_dir`` itself or any of its other entries. That is the
    security guarantee: the live secrets that share this directory are inviolate.

    After copying, the published ``capabilities.yaml`` is re-parsed with
    ``validator.load_capabilities`` (resolving schema ``$ref``s against the
    config dir) to confirm the broker will accept it. Fails closed: any OSError
    or a non-parsing published manifest raises ``InstallError`` (the merge into
    ``live_dir`` already stands; only the publish failed)."""
    live = Path(live_dir)
    config = Path(config_dir)
    cap_yaml = live / "capabilities.yaml"
    if not cap_yaml.is_file():
        raise InstallError(
            f"live manifests missing capabilities.yaml: {live_dir}"
        )
    try:
        _atomic_copy_file(cap_yaml, config / "capabilities.yaml")

        mcp_tools = live / "mcp-tools.yaml"
        if mcp_tools.is_file():
            _atomic_copy_file(mcp_tools, config / "mcp-tools.yaml")

        for sub in ("schemas", "profiles"):
            src_dir = live / sub
            if not src_dir.is_dir():
                continue
            entries = [p for p in sorted(src_dir.iterdir()) if p.is_file()]
            if sub == "schemas":
                entries = [p for p in entries if p.suffix == ".json"]
            if not entries:
                continue
            dst_dir = config / sub
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src_file in entries:
                _atomic_copy_file(src_file, dst_dir / src_file.name)
    except OSError as e:
        raise InstallError(f"publish to broker config failed: {e}") from e

    # Confirm the published config actually parses the way the broker will read
    # it (schema $refs resolve against config_dir). Failure here is a publish
    # failure — the live merge already stands.
    try:
        validator.load_capabilities(str(config / "capabilities.yaml"))
    except validator.ManifestError as e:
        raise InstallError(
            f"published config did not validate: {e}"
        ) from e
