"""Adversarial tests for the pack safety verifier (promoter design §6c, §9).

These tests prove the security CONTRACT of ``pack_verify.verify_pack``: a pack
is installed only if ALL of these hold, and every failing path fails closed
with a typed ``PackRejected``:

  1. signature present and verifies against a trusted, non-revoked key;
  2. data-only — every executor is ``mcp_tool`` with a tool name, OR
     ``subprocess`` whose ``binary`` is in ``VETTED_EXECUTORS`` (no other
     type, no unknown binary);
  3. no capability name in ``RESERVED_CAPABILITIES``;
  4. no collision with an existing live capability;
  5. the manifest has NO top-level key other than ``capabilities``;
  6. ``meta.capabilities`` set == manifest defined-names set;
  7. the pack defines SOMETHING — at least one capability OR one site profile
     (a truly-empty pack — no capabilities AND no profiles — is meaningless);
  8. every site profile in the pack is well-formed (loads via
     ``browser_profile.load``).

A DATA-ONLY SITE-PROFILE pack has zero capabilities and ≥1 profile; it is
legitimate (enabling a new browser_goal site = adding a profile, not a cap)
and passes with ``capability_names == ()``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from broker import pack_format, pack_keys, pack_verify

EXISTING = frozenset({"gmail.send", "browser_goal.commit"})


def _write_pack(
    base: Path,
    *,
    manifest: dict[str, Any],
    meta_caps: list[str],
    profiles: dict[str, Any] | None = None,
    priv: Ed25519PrivateKey | None = None,
    sign: bool = True,
) -> Path:
    """Write a pack tree at ``base`` and (optionally) sign it with ``priv``."""
    d = base / "p"
    (d / "schemas").mkdir(parents=True)
    (d / "profiles").mkdir()
    for fname, body in (profiles or {}).items():
        (d / "profiles" / fname).write_text(json.dumps(body), encoding="utf-8")
    meta = {
        "pack_id": "site",
        "version": 1,
        "created_utc": "2026-06-15T00:00:00Z",
        "description": "d",
        "capabilities": meta_caps,
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    if sign:
        assert priv is not None
        pack = pack_format.load_pack(str(d))
        (d / "pack.sig").write_bytes(priv.sign(pack_format.canonical_bytes(pack)))
    return d


def _signed_pack(
    tmp_path: Path,
    *,
    manifest: dict[str, Any],
    meta_caps: list[str],
    profiles: dict[str, Any] | None = None,
    sign: bool = True,
    key_id: str = "k1",
) -> tuple[pack_format.Pack, pack_keys.TrustedKeys]:
    priv = Ed25519PrivateKey.generate()
    (tmp_path / f"{key_id}.ed25519.pub").write_text(
        priv.public_key().public_bytes_raw().hex(), encoding="utf-8"
    )
    d = _write_pack(
        tmp_path,
        manifest=manifest,
        meta_caps=meta_caps,
        profiles=profiles,
        priv=priv,
        sign=sign,
    )
    pack = pack_format.load_pack(str(d))
    store = pack_keys.load_trusted_keys(str(tmp_path))
    return pack, store


def _ok_profile() -> dict[str, Any]:
    """A valid everyone_active-style SiteProfile dict (loads cleanly)."""
    return {
        "site": "tesco",
        "login_url": "https://secure.tesco.com/account/login",
        "allowlist": ["tesco.com", "secure.tesco.com"],
        "success_indicators": [
            {"type": "url_pattern", "value": "/groceries/"}
        ],
        "mfa_rule": "pause_and_ask",
        "network_strictness": "monitor",
    }


def _ok_manifest() -> dict[str, Any]:
    return {
        "capabilities": [
            {
                "name": "site.browse_plan",
                "executor": {
                    "type": "subprocess",
                    "binary": pack_verify.VETTED_EXECUTORS[0],
                },
                "param_schema": {"$ref": "./schemas/x.json"},
                "risk_level": "medium",
            }
        ]
    }


def _verify(
    pack: pack_format.Pack, store: pack_keys.TrustedKeys
) -> pack_verify.VerifyResult:
    return pack_verify.verify_pack(pack, store, existing_capabilities=EXISTING)


# --- happy paths ---------------------------------------------------------


def test_valid_data_only_pack_passes(tmp_path: Path) -> None:
    pack, store = _signed_pack(
        tmp_path, manifest=_ok_manifest(), meta_caps=["site.browse_plan"]
    )
    result = _verify(pack, store)
    assert result.key_id == "k1"
    assert result.pack_id == "site"
    assert result.capability_names == ("site.browse_plan",)
    assert result.pack_hash == pack_format.pack_hash(pack)


def test_mcp_tool_executor_allowed(tmp_path: Path) -> None:
    m = {
        "capabilities": [
            {
                "name": "site.read",
                "executor": {"type": "mcp_tool", "tool": "mcp__x__y"},
                "param_schema": {"$ref": "./schemas/x.json"},
                "risk_level": "low",
            }
        ]
    }
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.read"])
    assert _verify(pack, store).key_id == "k1"


# --- signature ------------------------------------------------------------


def test_unsigned_pack_rejected(tmp_path: Path) -> None:
    pack, store = _signed_pack(
        tmp_path, manifest=_ok_manifest(), meta_caps=["site.browse_plan"], sign=False
    )
    with pytest.raises(pack_verify.PackRejected, match="unsigned"):
        _verify(pack, store)


def test_signature_from_untrusted_key_rejected(tmp_path: Path) -> None:
    # Sign with a key whose public half is NOT in the store.
    rogue = Ed25519PrivateKey.generate()
    d = _write_pack(
        tmp_path,
        manifest=_ok_manifest(),
        meta_caps=["site.browse_plan"],
        priv=rogue,
        sign=True,
    )
    pack = pack_format.load_pack(str(d))
    store = pack_keys.load_trusted_keys(str(tmp_path))  # empty: no .pub written
    with pytest.raises(pack_verify.PackRejected, match="signature"):
        _verify(pack, store)


def test_revoked_key_signature_rejected(tmp_path: Path) -> None:
    priv = Ed25519PrivateKey.generate()
    (tmp_path / "k1.ed25519.pub").write_text(
        priv.public_key().public_bytes_raw().hex(), encoding="utf-8"
    )
    (tmp_path / "revoked").write_text("k1\n", encoding="utf-8")
    d = _write_pack(
        tmp_path,
        manifest=_ok_manifest(),
        meta_caps=["site.browse_plan"],
        priv=priv,
        sign=True,
    )
    pack = pack_format.load_pack(str(d))
    store = pack_keys.load_trusted_keys(str(tmp_path))
    with pytest.raises(pack_verify.PackRejected, match="signature"):
        _verify(pack, store)


def test_tampered_after_signing_rejected(tmp_path: Path) -> None:
    """ADVERSARIAL (a): a signature valid over the ORIGINAL canonical bytes is
    rejected once the meta is mutated after signing — tampering breaks verify."""
    priv = Ed25519PrivateKey.generate()
    (tmp_path / "k1.ed25519.pub").write_text(
        priv.public_key().public_bytes_raw().hex(), encoding="utf-8"
    )
    d = _write_pack(
        tmp_path,
        manifest=_ok_manifest(),
        meta_caps=["site.browse_plan"],
        priv=priv,
        sign=True,
    )
    # Mutate meta.json AFTER the signature was produced. pack.sig still carries
    # the old signature, but canonical_bytes now reflects the new content.
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta["description"] = "MUTATED after signing"
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    pack = pack_format.load_pack(str(d))
    store = pack_keys.load_trusted_keys(str(tmp_path))
    with pytest.raises(pack_verify.PackRejected, match="signature"):
        _verify(pack, store)


# --- data-only executor ---------------------------------------------------


def test_pack_adding_new_binary_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = {
        "type": "subprocess",
        "binary": "/Users/donna-broker/broker/executors/EVIL",
    }
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)


def test_unknown_executor_type_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = {"type": "shell", "command": "rm -rf /"}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)


def test_executor_missing_type_rejected(tmp_path: Path) -> None:
    """ADVERSARIAL (b): an executor dict with no `type` is rejected."""
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = {"binary": pack_verify.VETTED_EXECUTORS[0]}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)


def test_executor_not_a_dict_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = "subprocess"
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)


def test_executor_missing_entirely_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    del m["capabilities"][0]["executor"]
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)


def test_mcp_tool_without_tool_name_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = {"type": "mcp_tool"}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="tool"):
        _verify(pack, store)


@pytest.mark.parametrize(
    "near_miss",
    [
        pack_verify.VETTED_EXECUTORS[0] + "/",  # trailing slash
        pack_verify.VETTED_EXECUTORS[0] + "/../EVIL",  # traversal off a vetted path
        "/Users/donna-broker/broker/executors/../executors/browser_goal",
        pack_verify.VETTED_EXECUTORS[0].upper(),  # case variation
        " " + pack_verify.VETTED_EXECUTORS[0],  # leading space
    ],
)
def test_near_miss_vetted_binary_rejected(tmp_path: Path, near_miss: str) -> None:
    """ADVERSARIAL (c): a subprocess binary that is a near-miss of a vetted path
    (trailing slash, /../, case, whitespace) is NOT accepted — exact match only."""
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = {"type": "subprocess", "binary": near_miss}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)


# --- reserved names -------------------------------------------------------


def test_pack_redefining_reserved_cap_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["capabilities"][0]["name"] = "browser_goal.commit"
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["browser_goal.commit"])
    with pytest.raises(pack_verify.PackRejected, match="reserved"):
        _verify(pack, store)


def test_pack_redefining_explicit_reserved_cap_rejected(tmp_path: Path) -> None:
    # gmail.send is in the explicit reserved set (and also in EXISTING) — the
    # reserved check fires first, so the message names "reserved".
    m = _ok_manifest()
    m["capabilities"][0]["name"] = "gmail.send"
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["gmail.send"])
    with pytest.raises(pack_verify.PackRejected, match="reserved"):
        _verify(pack, store)


def test_reserved_set_unions_no_standing_grants() -> None:
    from broker import policy

    assert policy.NO_STANDING_GRANTS <= pack_verify.RESERVED_CAPABILITIES
    assert "gmail.send" in pack_verify.RESERVED_CAPABILITIES
    assert "everyone_active.checkout" in pack_verify.RESERVED_CAPABILITIES


# --- collision with existing ---------------------------------------------


def test_pack_colliding_with_existing_cap_rejected(tmp_path: Path) -> None:
    # An existing-but-not-reserved cap: collision check is what must fire.
    m = _ok_manifest()
    m["capabilities"][0]["name"] = "site.already_live"
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.already_live"])
    with pytest.raises(pack_verify.PackRejected, match="existing"):
        pack_verify.verify_pack(
            pack,
            store,
            existing_capabilities=frozenset({"site.already_live"}),
        )


# --- policy immutability --------------------------------------------------


def test_pack_with_extra_top_level_key_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["no_standing_grants"] = []  # attempt to touch policy
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="policy"):
        _verify(pack, store)


def test_pack_with_policy_key_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["policy"] = {"rate_limit": 9999}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="policy"):
        _verify(pack, store)


# --- declared == defined --------------------------------------------------


def test_pack_with_hidden_undeclared_cap_rejected(tmp_path: Path) -> None:
    m = _ok_manifest()
    m["capabilities"].append(
        {
            "name": "site.secret",
            "executor": {"type": "mcp_tool", "tool": "x"},
            "param_schema": {"$ref": "./schemas/x.json"},
            "risk_level": "low",
        }
    )
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="declared"):
        _verify(pack, store)


def test_meta_declaring_extra_cap_rejected(tmp_path: Path) -> None:
    """ADVERSARIAL (d): meta.capabilities lists a name not in the manifest."""
    pack, store = _signed_pack(
        tmp_path,
        manifest=_ok_manifest(),
        meta_caps=["site.browse_plan", "site.phantom"],
    )
    with pytest.raises(pack_verify.PackRejected, match="declared"):
        _verify(pack, store)


# --- empty capabilities ---------------------------------------------------


def test_empty_capabilities_pack_rejected(tmp_path: Path) -> None:
    """ADVERSARIAL (e): a pack that defines NOTHING — no capabilities AND no
    profiles — is rejected. Installing an empty pack is meaningless, and two
    empty sets would otherwise pass the declared==defined check, so the contract
    rejects it explicitly."""
    pack, store = _signed_pack(tmp_path, manifest={"capabilities": []}, meta_caps=[])
    with pytest.raises(pack_verify.PackRejected, match="defines nothing"):
        _verify(pack, store)


# --- malformed manifest shapes -------------------------------------------


def test_capabilities_not_a_list_rejected(tmp_path: Path) -> None:
    pack, store = _signed_pack(
        tmp_path,
        manifest={"capabilities": {"name": "site.browse_plan"}},
        meta_caps=["site.browse_plan"],
    )
    with pytest.raises(pack_verify.PackRejected, match="list"):
        _verify(pack, store)


def test_capability_entry_without_name_rejected(tmp_path: Path) -> None:
    m = {"capabilities": [{"executor": {"type": "mcp_tool", "tool": "x"}}]}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="name"):
        _verify(pack, store)


def test_capability_entry_not_a_dict_rejected(tmp_path: Path) -> None:
    m = {"capabilities": ["site.browse_plan"]}
    pack, store = _signed_pack(tmp_path, manifest=m, meta_caps=["site.browse_plan"])
    with pytest.raises(pack_verify.PackRejected, match="name"):
        _verify(pack, store)


# --- data-only site-profile packs ----------------------------------------


def test_profile_only_pack_passes(tmp_path: Path) -> None:
    """A signed pack with ZERO capabilities but one valid site profile is a
    legitimate data-only site-profile pack: it verifies, and the result carries
    an EMPTY capability_names tuple."""
    pack, store = _signed_pack(
        tmp_path,
        manifest={"capabilities": []},
        meta_caps=[],
        profiles={"tesco.json": _ok_profile()},
    )
    result = _verify(pack, store)
    assert result.key_id == "k1"
    assert result.pack_id == "site"
    assert result.capability_names == ()
    assert result.pack_hash == pack_format.pack_hash(pack)


def test_pack_with_neither_caps_nor_profiles_rejected(tmp_path: Path) -> None:
    """A pack that defines NOTHING — no capabilities AND no profiles — is
    refused with a clear 'defines nothing' message."""
    pack, store = _signed_pack(
        tmp_path, manifest={"capabilities": []}, meta_caps=[], profiles={}
    )
    with pytest.raises(pack_verify.PackRejected, match="defines nothing"):
        _verify(pack, store)


def test_profile_only_pack_with_malformed_profile_rejected(tmp_path: Path) -> None:
    """A profile-only pack whose profile is missing a required field (allowlist)
    is refused — a malformed profile can never be installed."""
    bad = _ok_profile()
    del bad["allowlist"]
    pack, store = _signed_pack(
        tmp_path,
        manifest={"capabilities": []},
        meta_caps=[],
        profiles={"tesco.json": bad},
    )
    with pytest.raises(pack_verify.PackRejected, match="invalid site profile"):
        _verify(pack, store)


def test_profile_only_pack_with_bad_login_url_rejected(tmp_path: Path) -> None:
    """A profile with a non-https login_url is refused (ProfileError surfaced)."""
    bad = _ok_profile()
    bad["login_url"] = "http://insecure.tesco.com/login"
    pack, store = _signed_pack(
        tmp_path,
        manifest={"capabilities": []},
        meta_caps=[],
        profiles={"tesco.json": bad},
    )
    with pytest.raises(pack_verify.PackRejected, match="invalid site profile"):
        _verify(pack, store)


def test_profile_not_a_json_object_rejected(tmp_path: Path) -> None:
    """A profile file whose top-level JSON is not an object is refused before it
    ever reaches browser_profile.load."""
    pack, store = _signed_pack(
        tmp_path,
        manifest={"capabilities": []},
        meta_caps=[],
        profiles={"tesco.json": ["not", "an", "object"]},
    )
    with pytest.raises(pack_verify.PackRejected, match="invalid site profile"):
        _verify(pack, store)


def test_pack_with_both_capability_and_profile_validates_both(
    tmp_path: Path,
) -> None:
    """A pack carrying BOTH a capability and a profile validates both: a valid
    cap + valid profile passes."""
    pack, store = _signed_pack(
        tmp_path,
        manifest=_ok_manifest(),
        meta_caps=["site.browse_plan"],
        profiles={"tesco.json": _ok_profile()},
    )
    result = _verify(pack, store)
    assert result.capability_names == ("site.browse_plan",)


def test_pack_with_good_cap_but_bad_profile_rejected(tmp_path: Path) -> None:
    """A pack with a valid capability but a malformed profile is still refused —
    profile validation applies even when capabilities are present."""
    bad = _ok_profile()
    del bad["success_indicators"]
    pack, store = _signed_pack(
        tmp_path,
        manifest=_ok_manifest(),
        meta_caps=["site.browse_plan"],
        profiles={"tesco.json": bad},
    )
    with pytest.raises(pack_verify.PackRejected, match="invalid site profile"):
        _verify(pack, store)


def test_pack_with_bad_cap_and_good_profile_rejected(tmp_path: Path) -> None:
    """A pack with a bad executor is still rejected even if it carries a valid
    profile — the data-only check on capabilities still applies."""
    m = _ok_manifest()
    m["capabilities"][0]["executor"] = {"type": "shell", "command": "rm -rf /"}
    pack, store = _signed_pack(
        tmp_path,
        manifest=m,
        meta_caps=["site.browse_plan"],
        profiles={"tesco.json": _ok_profile()},
    )
    with pytest.raises(pack_verify.PackRejected, match="executor"):
        _verify(pack, store)
