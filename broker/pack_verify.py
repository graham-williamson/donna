# pack_verify.py
"""Pack safety verifier (promoter design §6c, §9 invariants 1, 3, 4).

The single most security-critical decision in the promoter: given a loaded
``Pack``, the trusted keys, and the current live capability set, decide whether
the pack is safe to install. Pure function — no I/O beyond what callers pass in.

A pack is INSTALLED only if EVERY one of these holds (else ``PackRejected``):

  1. Signature present and verifies against a trusted, non-revoked key, over
     EXACTLY ``pack_format.canonical_bytes(pack)`` (so any tampering after
     signing breaks verification).
  2. Data-only — every capability's ``executor`` is either ``mcp_tool`` with a
     tool name, OR ``subprocess`` whose ``binary`` is EXACTLY one of
     ``VETTED_EXECUTORS``. No other executor type, no unknown binary path, no
     near-miss path (matching is byte-exact — no normalisation). A pack can
     never introduce new code across the boundary.
  3. No reserved-name redefinition — no pack capability name is in
     ``RESERVED_CAPABILITIES`` (an explicit security-critical set unioned with
     the live ``policy.NO_STANDING_GRANTS`` so the two cannot drift apart).
  4. No collision with an existing live capability — installing is additive;
     updates go through the reviewed path.
  5. Policy immutability — the manifest contains ONLY a ``capabilities`` key.
     Any other top-level key (``policy``, ``no_standing_grants``,
     ``mcp_tools`` …) is refused: a pack can never touch policy.
  6. Declared == defined — ``meta.capabilities`` exactly equals the set of
     ``name``s defined in ``manifest.capabilities`` (no hidden capabilities,
     no phantom declarations).
  7. The pack defines SOMETHING — at least one capability OR at least one
     browser-goal SiteProfile. A truly-empty pack (no capabilities AND no
     profiles) is meaningless and is refused explicitly (two empty
     capability sets would otherwise satisfy #6).
  8. Every browser-goal SiteProfile in the pack is well-formed — each profile
     in ``pack.profiles`` loads cleanly via ``browser_profile.load`` (a
     malformed/garbage profile can never be installed).

A DATA-ONLY SITE-PROFILE pack legitimately has ZERO capabilities and one (or
more) profiles: ``browser_goal.plan``/``browser_goal.commit`` are generic
capabilities that load ``manifests/profiles/<site>.json``, so enabling a new
site is adding a profile, not a new capability. Such a pack passes with
``capability_names == ()``.

Every failure raises ``PackRejected`` with a precise reason. Fail-closed: no
bare excepts, no silent return-None — there is no path out of this function
that approves a pack without satisfying all eight checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from broker import browser_profile, pack_format, pack_keys, policy

# Already-vetted executor binaries a pack may reference (design §3). A pack may
# NOT introduce any other subprocess binary. Matching is byte-exact — no path
# normalisation — so a trailing slash, a `/../` segment, a case variation, or
# stray whitespace is a different (rejected) string, not the vetted binary.
VETTED_EXECUTORS: tuple[str, ...] = (
    "/Users/donna-broker/broker/executors/browser_goal",
    "/Users/donna-broker/broker/executors/everyone_active_checkout",
    "/Users/donna-broker/broker/executors/everyone_active_book",
)

# Security-critical capabilities a pack may never (re)define. The union of an
# explicit list with the live NO_STANDING_GRANTS set means that if a capability
# is ever added to the per-action-only set in policy.py, it automatically
# becomes un-redefinable by a pack too — the two sets cannot drift.
_EXPLICIT_RESERVED: frozenset[str] = frozenset(
    {
        "browser_goal.commit",
        "everyone_active.checkout",
        "gmail.send",
    }
)
RESERVED_CAPABILITIES: frozenset[str] = _EXPLICIT_RESERVED | policy.NO_STANDING_GRANTS

# The ONLY top-level key a pack manifest may contain.
_ALLOWED_MANIFEST_KEYS: frozenset[str] = frozenset({"capabilities"})


class PackRejected(Exception):
    """The pack is unsafe to install. Fail-closed — the install is refused."""


@dataclass(frozen=True)
class VerifyResult:
    """The result of a successful verification — never returned otherwise."""

    key_id: str
    pack_id: str
    pack_hash: str
    capability_names: tuple[str, ...]


def verify_pack(
    pack: pack_format.Pack,
    trusted_keys: pack_keys.TrustedKeys,
    *,
    existing_capabilities: Iterable[str],
) -> VerifyResult:
    """Decide whether ``pack`` is safe to install. Returns a ``VerifyResult``
    (carrying the signing key_id and the pack's content hash) iff every safety
    check passes; otherwise raises ``PackRejected`` with a precise reason."""
    existing = frozenset(existing_capabilities)

    # 1. Signature over the EXACT canonical bytes (tamper-evident).
    if pack.signature is None:
        raise PackRejected("pack is unsigned")
    try:
        key_id = pack_keys.verify(
            trusted_keys, pack_format.canonical_bytes(pack), pack.signature
        )
    except pack_keys.SignatureError as exc:
        raise PackRejected(f"signature does not verify: {exc}") from exc

    # 5. Policy immutability: only `capabilities` is allowed at the top level.
    extra_keys = set(pack.manifest.keys()) - _ALLOWED_MANIFEST_KEYS
    if extra_keys:
        raise PackRejected(
            "pack may not touch policy or other config; "
            f"forbidden top-level manifest keys: {sorted(extra_keys)}"
        )

    caps = pack.manifest["capabilities"]
    if not isinstance(caps, list):
        raise PackRejected("manifest `capabilities` must be a list")

    # 7. A pack must define SOMETHING — at least one capability OR at least one
    #    site profile. A truly-empty pack is meaningless (and two empty sets
    #    would otherwise satisfy the declared==defined check below). A pack with
    #    zero capabilities but ≥1 profile is a legitimate data-only site-profile
    #    pack (enabling a new browser_goal site = adding a profile, not a cap).
    if not caps and not pack.profiles:
        raise PackRejected(
            "pack defines nothing — no capabilities and no profiles"
        )

    # 8. Every site profile must be well-formed (a malformed/garbage profile can
    #    never be installed). Validated whether or not the pack has capabilities.
    for filename, profile in pack.profiles.items():
        if not isinstance(profile, dict):
            raise PackRejected(
                f"invalid site profile {filename}: profile must be a JSON object"
            )
        try:
            browser_profile.load(profile)
        except browser_profile.ProfileError as exc:
            raise PackRejected(
                f"invalid site profile {filename}: {exc}"
            ) from exc

    defined_names: list[str] = []
    for entry in caps:
        if not isinstance(entry, dict) or "name" not in entry:
            raise PackRejected("each capability entry must be a mapping with a `name`")
        name = str(entry["name"])
        defined_names.append(name)
        # 3. Reserved-name redefinition (checked before collision so the clearer
        #    "reserved" reason wins for a name that is both reserved and live).
        if name in RESERVED_CAPABILITIES:
            raise PackRejected(
                f"pack may not define reserved capability {name!r}"
            )
        # 4. Collision with an existing live capability.
        if name in existing:
            raise PackRejected(
                f"pack may not redefine existing capability {name!r}"
            )
        # 2. Data-only executor.
        _check_executor(entry.get("executor"), name)

    # 6. Declared == defined.
    if set(defined_names) != set(pack.capability_names):
        raise PackRejected(
            "meta.capabilities must equal the manifest's declared capability "
            f"names (meta={sorted(pack.capability_names)}, "
            f"manifest={sorted(defined_names)})"
        )

    return VerifyResult(
        key_id=key_id,
        pack_id=pack.pack_id,
        pack_hash=pack_format.pack_hash(pack),
        capability_names=tuple(defined_names),
    )


def _check_executor(executor: Any, capability_name: str) -> None:
    """Enforce the data-only invariant for one capability's executor. Raises
    ``PackRejected`` for any executor that is not a recognised data-only form."""
    if not isinstance(executor, dict) or "type" not in executor:
        raise PackRejected(
            f"capability {capability_name!r} has a missing/invalid executor"
        )
    etype = executor["type"]
    if etype == "mcp_tool":
        if not executor.get("tool"):
            raise PackRejected(
                f"capability {capability_name!r} mcp_tool executor needs a tool name"
            )
        return
    if etype == "subprocess":
        binary = executor.get("binary")
        if binary not in VETTED_EXECUTORS:
            raise PackRejected(
                f"capability {capability_name!r} subprocess executor binary "
                f"{binary!r} is not a vetted executor (data-only)"
            )
        return
    raise PackRejected(
        f"capability {capability_name!r} has unsupported executor type {etype!r}"
    )
