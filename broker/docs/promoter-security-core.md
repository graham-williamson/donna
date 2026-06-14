# Promoter Security Core

Pure, side-effect-free enforcement layer for the signed-capability promoter
(spec: ~/daru/docs/superpowers/specs/2026-06-15-signed-capability-promoter-design.md).
This is **Plan A**: the four modules that decide whether a capability pack is
safe to install. They take fixtures in and return a verdict — nothing here
mutates the system.

## Modules
- pack_format — reads a pack tree (meta.json, manifest.yaml, pack.sig, and the
  direct children of schemas/ and profiles/ ONLY); deterministic canonical
  bytes (signature-excluded) + a SHA-256 pack hash. Rejects anything malformed
  or any name carrying a path-traversal token.
- pack_keys — the trusted Ed25519 key store. Loads `*.ed25519.pub`, honours a
  `revoked` list, and verifies a signature against the live trust set,
  returning the signing key id.
- pack_verify — the safety verifier. Given a loaded pack + the key store +
  the live capability set, runs every install gate and returns a VerifyResult
  (key_id, pack_id, capability_names, pack_hash) or raises PackRejected.
- pack_token — single-use approval binding. Checks a broker ApprovalRecord is
  approved, unconsumed, in-TTL, and bound to THIS pack_id + pack_hash before
  yielding the approval id.

## Security invariants (must hold; see spec §6c, §9)
1. **Signature-or-reject.** A pack installs only if its signature verifies
   against a trusted, non-revoked key. Unsigned, untrusted, revoked, or
   tampered-after-signing packs fail closed with PackRejected (pack_verify,
   on top of pack_keys' fail-closed verify).
2. **Data-only.** Every executor must be `mcp_tool` (with a tool name) or
   `subprocess` whose binary is an EXACT member of VETTED_EXECUTORS. No other
   type, no new binary, no near-miss path. Packs never introduce code.
3. **Reserved-name / policy-immutability.** No pack may redefine a reserved
   security-critical capability (RESERVED_CAPABILITIES ⊇ policy.NO_STANDING_GRANTS),
   collide with a live capability, or carry any manifest top-level key other
   than `capabilities`. Policy and gate are untouchable.
4. **Declared == defined.** meta.capabilities must equal the manifest's
   defined-name set, and a pack must define at least one capability — no hidden
   or phantom capabilities, no meaningless empty pack.
5. **Pack-bound single-use approval.** An approval authorises exactly one pack:
   the record must be approved, not yet consumed, within TTL, and match the
   pack's id and content hash (pack_token).

## NO privileged side effects
This layer has **no** privileged side effects. Nothing here starts a daemon,
opens or binds a socket, mutates the filesystem outside reading a pack the
caller points it at, writes to the broker DB or ledger, marks an approval
consumed, or calls launchctl. The modules are pure: pack/keys/record in,
verdict out. Actually staging the pack, restarting the broker, consuming the
approval token, and everything socket/daemon/launchd is **Plan B** (the
`donna-promoter` privileged daemon + `install-promoter.sh` bootstrap; spec
§4, §6).
