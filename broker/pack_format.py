# pack_format.py
"""Declarative capability-pack model + canonicalisation (promoter design §3).

A pack is DATA on disk: meta + a manifest fragment + referenced JSON schemas
+ optional browser_goal SiteProfiles. The signed bytes are a deterministic
canonicalisation of that content (RFC 8785 via broker.canonicalize),
EXCLUDING pack.sig. Identical content -> identical bytes -> identical hash,
regardless of file key order or whitespace.

Security contract (fail-closed): load_pack reads ONLY the following files and
nothing else — no path traversal, no stray files:

  <dir>/meta.json              required, JSON object
  <dir>/manifest.yaml          required, must have a top-level `capabilities:`
  <dir>/pack.sig               optional, raw detached signature (NOT signed)
  <dir>/schemas/<name>.json    direct children only
  <dir>/profiles/<name>.json   direct children only

Any direct child of schemas/ or profiles/ whose name contains a path
separator or `..` is rejected; nested subdirs and any other file in the pack
root are ignored (never read into the canonical content).

No signature logic here (see pack_keys.py) and no safety policy (see
pack_verify.py). This module only models and canonicalises.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from broker import canonicalize


class PackFormatError(Exception):
    """A pack directory is missing required files, is malformed, or contains an
    unsafe filename (path traversal). Fail-closed — the pack does not load."""


@dataclass(frozen=True)
class Pack:
    pack_id: str
    version: int
    created_utc: str
    description: str
    capability_names: tuple[str, ...]
    meta: dict[str, Any]
    manifest: dict[str, Any]
    schemas: dict[str, Any]  # filename -> parsed JSON (direct children of schemas/)
    profiles: dict[str, Any]  # filename -> parsed JSON (direct children of profiles/)
    signature: bytes | None  # raw pack.sig bytes if present, else None


def _is_safe_name(name: str) -> bool:
    """A pack-relative filename is safe iff it is a plain file name with no
    path separator and no `..` traversal token."""
    return name != "" and "/" not in name and "\\" not in name and ".." not in name


def _read_dir_json(directory: Path) -> dict[str, Any]:
    """Parse every DIRECT-CHILD ``*.json`` file of ``directory``.

    Nested subdirectories are ignored (only direct children are read). Any
    direct child whose name is unsafe (path separator or ``..``) is rejected
    — the loader refuses the whole pack rather than read it.
    """
    out: dict[str, Any] = {}
    if not directory.is_dir():
        return out
    for child in sorted(directory.iterdir()):
        if not child.is_file():
            continue  # nested dirs and other non-files are not read
        if not _is_safe_name(child.name):
            raise PackFormatError(
                f"unsafe filename in {directory.name}/: {child.name!r}"
            )
        try:
            out[child.name] = json.loads(child.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PackFormatError(
                f"cannot parse {directory.name}/{child.name}: {exc}"
            ) from exc
    return out


def load_pack(path: str) -> Pack:
    """Load and validate the pack rooted at ``path``.

    Reads ONLY meta.json, manifest.yaml, pack.sig, and the direct-child JSON
    files of schemas/ and profiles/. Raises PackFormatError if a required file
    is missing, any file is malformed, or any schemas/profiles child has an
    unsafe name.
    """
    base = Path(path)
    meta_path = base / "meta.json"
    manifest_path = base / "manifest.yaml"
    if not meta_path.is_file():
        raise PackFormatError(f"pack missing meta.json: {path}")
    if not manifest_path.is_file():
        raise PackFormatError(f"pack missing manifest.yaml: {path}")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PackFormatError(f"cannot parse meta.json: {exc}") from exc
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise PackFormatError(f"cannot parse manifest.yaml: {exc}") from exc

    if not isinstance(meta, dict):
        raise PackFormatError("meta.json must be a JSON object")
    if not isinstance(manifest, dict) or "capabilities" not in manifest:
        raise PackFormatError(
            "manifest.yaml must have a top-level `capabilities:` list"
        )

    try:
        pack_id = str(meta["pack_id"])
        version = int(meta["version"])
        created_utc = str(meta["created_utc"])
        description = str(meta["description"])
        capability_names = tuple(str(name) for name in meta["capabilities"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PackFormatError(
            f"meta.json missing/invalid required field: {exc}"
        ) from exc

    schemas = _read_dir_json(base / "schemas")
    profiles = _read_dir_json(base / "profiles")

    sig_path = base / "pack.sig"
    signature = sig_path.read_bytes() if sig_path.is_file() else None

    return Pack(
        pack_id=pack_id,
        version=version,
        created_utc=created_utc,
        description=description,
        capability_names=capability_names,
        meta=meta,
        manifest=manifest,
        schemas=schemas,
        profiles=profiles,
        signature=signature,
    )


def _content(pack: Pack) -> dict[str, Any]:
    """The signed view of a pack: every content field EXCEPT the signature."""
    return {
        "meta": pack.meta,
        "manifest": pack.manifest,
        "schemas": pack.schemas,
        "profiles": pack.profiles,
    }


def canonical_bytes(pack: Pack) -> bytes:
    """RFC 8785 canonical bytes of the pack content (excludes pack.sig).

    These are the exact bytes that are signed and verified.
    """
    return canonicalize.canonicalize(_content(pack))


def pack_hash(pack: Pack) -> str:
    """Hex SHA-256 of the pack's canonical bytes — a stable content identity."""
    return hashlib.sha256(canonical_bytes(pack)).hexdigest()
