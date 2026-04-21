# Piece C — Creds injection into executor

**Date:** 2026-04-21
**Phase:** 2
**Status:** Design approved, pending implementation plan
**Spec refs:** `donna-security-v1.md` v1.1 §2.3 (credentialed actions), §8 (capability manifest), §9.2 (subprocess isolation), §15 (audit events), §17 (Phase 2 — browser executors + age vault)
**Depends on:** Piece A (atomic manifest deploy, shipped), Piece B (`broker/creds.py` + vault at `/Users/donna-broker/.config/donna/creds/`, shipped)
**Unblocks:** Piece D (Everyone Active subprocess executor), Piece E (Phase 2 rule gate)

---

## 1. Purpose

Wire `broker/creds.py::unlock_creds()` into `broker/executor.py::_execute_subprocess` so a capability can opt in — via manifest — to having its decrypted credentials delivered to its subprocess at spawn time. Credentials never enter Donna's main context; they flow broker → executor subprocess → die.

## 2. Non-goals

- Shipping any real capability that consumes creds. That's Piece D.
- Flipping the Phase 1 → Phase 2 rule set. That's Piece E.
- Supporting delivery mechanisms other than `fd3`. `env` and `stdin_field` are enum-ready future values but not implemented.
- Browser automation / Playwright plumbing. Out of scope indefinitely pending §14.1 review.
- Keychain / KMS / cloud-secret-manager backends. `unlock_creds` stays file-based.

## 3. Delivery mechanism: fd 3 pipe

### 3.1 Why fd 3, not env or stdin JSON

| Channel | Attack surface | Encoding cost | Notes |
|---|---|---|---|
| **fd 3 pipe (chosen)** | Pipe dies with `communicate()`; not introspectable via `ps`; not inherited by grandchildren. | None — raw bytes. | Invariant shift: `pass_fds=()` becomes `pass_fds=(r_fd,)` for creds-declared capabilities only. |
| `env` (`DONNA_CREDS=<b64>`) | `ps -E` same-UID readable on macOS; inherited by default; auto-captured by error-reporting libraries. | base64 (env is NUL-terminated). | Breaks the `_sanitised_env()` PATH-only invariant. Rejected. |
| `stdin_field` | Mixed channel with params JSON; slightly larger log surface than fd 3. | base64 (JSON can't carry raw bytes). | Second-best. Retained as a possible future enum value if a third-party binary forces it. |

Full reviewer analysis of these trade-offs is captured in the living doc's decision log.

### 3.2 Runtime mechanics

```
1. cred_bytes = creds.unlock_creds(entry, creds_dir, identity_path, ...)
   — on CredsError: _finalise_failure(error_code=e.error_code, ...)

2. assert len(cred_bytes) <= 16384
   — else _finalise_failure("creds_too_large", ...)

3. r_fd, w_fd = os.pipe()
   os.set_inheritable(r_fd, True)
   os.set_inheritable(w_fd, False)

4. try:
       proc = subprocess.Popen(
           [binary],
           stdin=PIPE, stdout=PIPE, stderr=PIPE,
           env=_sanitised_env(),        # unchanged: PATH-only
           cwd=<ephemeral workdir>,     # unchanged
           pass_fds=(r_fd,),            # invariant shift, creds-only
           preexec_fn=_dup_rfd_to_fd3,  # remaps r_fd -> 3 in child
           close_fds=True,
       )
   finally:
       os.close(r_fd)   # parent no longer needs the read end

5. try:
       os.write(w_fd, cred_bytes)
   finally:
       os.close(w_fd)
       del cred_bytes   # intent signal; Python can't zero

6. proc.communicate(...)   # existing path
```

`_dup_rfd_to_fd3` is a two-call preexec helper: `os.dup2(r_fd, 3)` then `os.close(r_fd)` if `r_fd != 3`. No imports, no branching beyond that, no logging. Runs in the forked child before `exec`.

### 3.3 Capability subprocess contract

A subprocess invoked under a `creds:`-declared capability:

- **Must** read fd 3 to EOF, then close it.
- **Must** treat missing or unreadable fd 3 as a fatal error (exit non-zero).
- Receives raw decrypted bytes: no framing, no JSON envelope, no newline. Interpretation is the executor's responsibility.
- Is guaranteed that no other fd beyond stdin/stdout/stderr/3 is inherited.

The broker guarantees *delivery attempt* to fd 3. It does not guarantee the child consumed the bytes — exit status is authoritative. A child that exits 0 without reading fd 3 is a capability-author bug, not a broker bug.

### 3.4 Size cap

16 KiB hard ceiling on `cred_bytes`. Reason: fits within macOS's default pipe buffer, so a single `os.write()` never blocks. Anything larger indicates either a format change or a bug. New stable error code: `creds_too_large`.

## 4. Manifest schema

### 4.1 Shape

The `creds:` block on a capability entry is optional. When absent, the capability runs exactly as today (no pipe, `pass_fds=()`).

```yaml
capabilities:
  - name: everyone_active.book_class
    executor:
      type: subprocess
      binary: /Users/donna-broker/tools/everyone_active_book
      timeout_seconds: 30
    creds:
      delivery: fd3
      entry: everyone_active
    # ... rest of existing fields unchanged
```

Two fields, both required when `creds:` is present:

- `delivery`: enum. Only legal value today is `fd3`. Keeping it as an enum allows future extension without a schema migration.
- `entry`: lowercase filename stem. Resolves to `<creds_dir>/<entry>.age`. Pattern: `^[a-z][a-z0-9_]*$`. Multiple capabilities may share one `entry` (same account, different actions).

**Explicitly rejected alternatives:**

- `required: true` field — presence of the block is the declaration. No third state (configured-but-optional) exists or will exist.
- Default `entry` derived from capability name — security-critical bindings should not rely on silent transforms. Sharing entries across capabilities is the common case and needs explicit declaration anyway.

### 4.2 Validator additions (`broker/validator.py`)

- New frozen dataclass `CredsBlock(delivery: str, entry: str)`.
- New field on `Capability`: `creds: CredsBlock | None`, default `None`.
- If `creds:` is present:
  - Must be a mapping. `creds: null`, `creds: "yes"`, `creds: []` → `ManifestError`.
  - `delivery` must be in `VALID_CREDS_DELIVERY = frozenset({"fd3"})`.
  - `entry` must match `^[a-z][a-z0-9_]*$`.
  - No unknown keys accepted (strict).

## 5. Config plumbing

### 5.1 `CredsConfig` dataclass

```python
@dataclass(frozen=True)
class CredsConfig:
    creds_dir: Path
    identity_path: Path
    age_binary: str = "age"
    timeout_seconds: float = 10.0
```

### 5.2 Signature extension

`executor.execute()` gains `creds_config: CredsConfig | None = None`. If a capability declares `creds:` but `creds_config is None`, `_finalise_failure("creds_config_missing", ...)` — fail closed.

Explicitly rejected alternatives:

- **Global module state** (`creds.configure(...)` + module-level imports in executor.py) — hidden coupling, test reset pain, clashes with `mypy --strict`.
- **Provider callable** (`creds_provider: Callable[[str], bytes]`) — abstraction for a backend we don't have. Loses stable `CredsError.error_code` vocabulary unless we define an exception contract. YAGNI.

### 5.3 `main.py` wiring

At CLI dispatch, `main.py` constructs a `CredsConfig` and passes it to every `execute()` call. Paths live as module-level constants in `main.py` for now (`/Users/donna-broker/.config/donna/creds/` and `/Users/donna-broker/.config/donna/creds/identity.age`); migration to a broker config file is a future concern, not Piece C. The module-level defaults inside `creds.py` stay for direct `unlock_creds` unit tests but are no longer the runtime source of truth.

## 6. Startup vault health checks

Trigger: `main.py` detects at least one capability in `capabilities.yaml` declares `creds:`. Runs a one-pass health sweep. Every failure emits a structured audit warning (`creds_vault_warning`) with a `reason` field. The broker does **not** refuse to boot on any of these — a misconfigured capability fails at request time; unrelated capabilities keep working.

### 6.1 Broker-wide checks

| # | Reason code | Check |
|---|---|---|
| 1 | `vault_dir_missing` | `creds_dir` exists and is a directory |
| 2 | `vault_dir_mode_loose` | `creds_dir` mode is `0750` or tighter |
| 3 | `vault_dir_owner_wrong` | `creds_dir` owner UID is `donna-broker` |
| 4 | `identity_missing` | `identity.age` exists |
| 5 | `identity_mode_loose` | `identity.age` mode is `0400` |
| 6 | `identity_owner_wrong` | `identity.age` owner UID is `donna-broker` |
| 7 | `age_binary_missing` | `shutil.which(age_binary)` resolves |

### 6.2 Per-capability checks

Run once per capability that declares `creds:`:

| # | Reason code | Check |
|---|---|---|
| 8 | `entry_missing` | `<entry>.age` exists in `creds_dir` |
| 9 | `entry_mode_loose` | `<entry>.age` mode is `0440` or tighter |
| 10 | `entry_owner_wrong` | `<entry>.age` owner UID is `donna-broker` |

### 6.3 `donna-broker verify-vault` subcommand

Standalone CLI that runs the same ten checks. Mirrors the `verify-manifests` pattern from Piece A. No sudo escalation beyond what the broker normally uses; reads metadata only.

**Output format (deliberately boring, scriptable):**

```
OK   vault_dir_permissions  /Users/donna-broker/.config/donna/creds  0750
OK   identity_mode          identity.age                             0400
WARN entry_mode_loose       gmail.age                                0644
WARN entry_owner_wrong      linear.age                               uid=501 (want donna-broker)
--
2 warnings, 8 checks passed.
```

- One line per check, `OK` / `WARN` prefix at fixed column, reason code, artefact, detail.
- Summary line at end prefixed `--`.
- Exit 0 when all checks pass, exit 1 if any `WARN` fired.

### 6.4 Cross-reference with runtime

When `unlock_creds` eventually fails with `creds_missing` / `creds_identity_missing` / `creds_binary_missing` at request time, the runtime audit event uses overlapping reason vocabulary where applicable. An operator reading the audit log can see at a glance whether a failure was warned-about-at-boot or regressed mid-session.

## 7. Audit hardening (lands in the same PR)

Two fixes flagged during design review. They're not creds-delivery, but they're leak paths whose weight grows the moment any cred is in flight.

### 7.1 `executor_stderr` — no more verbatim bodies

Today: up to 16 KiB of verbatim stderr is decoded and written into the hash-chained audit log. A buggy capability that prints a credential to stderr exfils it.

New shape:

```python
{
    "event": "executor_stderr",
    "request_id": request.request_id,
    "stderr_bytes": len(stderr),
    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    "stderr_head_redacted": _redact_audit_text(stderr[:256].decode("utf-8", errors="replace")),
}
```

- `stderr_bytes`: length. Keeps the noise signal.
- `stderr_sha256`: content-addressable. Two failures with identical stderr match up; attacker-chosen tokens do not.
- `stderr_head_redacted`: first 256 bytes, run through the existing §15 sanitiser (strips URLs, long hex, long digits, non-Latin). Enough to eyeball failure class without carrying a payload.
- **No raw stderr body anywhere in the audit event.**

Not in this PR: a mode-0600 stash file for post-hoc debugging. Deferred. If a real incident needs it, we'll add it then with retention policy considered.

### 7.2 `executor_spawn_error` — type-only, no `str(e)`

Today:

```python
return _finalise_failure(..., "executor_spawn_error", f"{type(e).__name__}: {e}")
```

New:

```python
return _finalise_failure(..., "executor_spawn_error", type(e).__name__,
                         extra={"exception_type": type(e).__name__})
```

Defence in depth. Costs a small amount of debugging signal; `request_id` + stderr hash + audit timeline carries enough context.

### 7.3 Audit vocabulary freeze

Once these events change shape, the schema is frozen. Any further change is a deliberate decision log entry in the living doc.

## 8. Error code taxonomy

Stable strings. Added here for a single reference point.

**From `creds.py` (already shipped in Piece B):** `creds_bad_capability_name`, `creds_identity_missing`, `creds_missing`, `creds_decrypt_failed`, `creds_timeout`, `creds_binary_missing`, `creds_spawn_error`.

**New in Piece C:** `creds_too_large`, `creds_pipe_error`, `creds_config_missing`.

**Startup vault warnings:** `vault_dir_missing`, `vault_dir_mode_loose`, `vault_dir_owner_wrong`, `identity_missing`, `identity_mode_loose`, `identity_owner_wrong`, `age_binary_missing`, `entry_missing`, `entry_mode_loose`, `entry_owner_wrong`.

## 9. Tests

Coverage gate stays ≥95% per the Ralph completion bar, but the real success bar is the invariants and failure paths below. Coverage is a thermometer, not the medicine.

### 9.1 `test_validator.py`

1. No `creds:` block → `capability.creds is None`.
2. `creds: { delivery: fd3, entry: everyone_active }` → parses, dataclass populated.
3. `creds: { delivery: fd3 }` (missing `entry`) → `ManifestError`.
4. `creds: { delivery: smoke_signals, entry: foo }` → `ManifestError`.
5. `creds: { delivery: fd3, entry: "Has Spaces" }` → `ManifestError`.
6. `creds: "yes"` (string where dict expected) → `ManifestError`.
7. `creds: null` / `creds: []` (wrong shape) → `ManifestError`.

### 9.2 `test_executor.py`

All with monkeypatched `unlock_creds` unless noted:

1. **No creds declared.** `pass_fds=()`, no pipe, no `unlock_creds` call.
2. **Happy path.** Monkeypatched `unlock_creds` returns `b"token-xyz"`. Child sees `b"token-xyz"` then EOF on fd 3. Row → succeeded. Contract-level assertion only; plumbing detail lives in case 5.
3. **Unlock fails.** Monkeypatch raises `CredsError("creds_missing", ...)`. Subprocess never spawned. Row → failed with `error_code="creds_missing"`.
4. **Oversize bytes.** Monkeypatch returns 20 KiB. Row → failed with `creds_too_large`. Subprocess never spawned. Pipe fds cleaned.
5. **Invariant.** Spy on `subprocess.Popen` kwargs across creds-less and creds-declared dispatches. `pass_fds ∈ {(), (r_fd,)}` — no other values, `r_fd` always matches what we just opened.
6. **Spawn failure cleanup.** `os.pipe()` succeeds, `Popen` raises. Assert: both fds closed, no leak, `executor_spawn_error` audit emitted (type-only per §7.2), row → failed.
7. **Child exits before reading.** Child exits (code 0 and non-zero variants) without consuming fd 3. Broker cleans up write end regardless. Exit-status-authoritative rule holds: exit 0 → succeeded, non-zero → failed.

### 9.3 `test_creds_config.py` (new)

- One test per startup reason code (10 cases).
- All-ten-pass → no warnings emitted.
- Multiple failures → all reported; one bad file doesn't short-circuit the sweep.
- `verify-vault` CLI: stdout format matches §6.3, exit 0 clean, exit 1 on any WARN.

### 9.4 Synthetic end-to-end

The only test in the suite that exercises the full real-age + real-pipe + real-dup2 path. Everything else monkey-patches something.

- New file: `broker/manifests/capabilities.test.yaml` (**test-only**, kept separate from production `capabilities.yaml`).
- New capability: `synthetic.echo_creds`, risk medium (creds imply medium/high per §2.3), `creds: { delivery: fd3, entry: synthetic }`. A matching `revalidate: { not_applicable: no_external_state }` satisfies §8's revalidation contract for medium-risk capabilities.
- New binary: `tools/synthetic_echo_creds` (tiny Python script, ~15 lines, mode 0755): reads fd 3 to EOF, prints `{"sha256": "<hex>"}` to stdout.
- Fixture: creates a real age identity and encrypts a known plaintext to a tmp vault's `synthetic.age`, points `CredsConfig` at the tmp dir. Seeds the requests_db with a pre-approved row directly rather than driving Telegram — the test exercises the execute path, not the approval path.
- Test: drives execute → assert stdout sha256 matches the known plaintext.
- Auto-skips when `age` isn't on PATH (same pattern as existing `test_creds.py::test_real_age_roundtrip`).

## 10. Architectural annotations

### 10.1 `broker/CLAUDE.md`

Add `creds.py` to the subprocess-boundary list alongside `resolver.py` and `executor.py`:

```
- resolver.py spawns subprocesses per §9.2.
- executor.py spawns subprocesses for capability-bound executors (§8).
- creds.py spawns a single subprocess (`age --decrypt`) per §17 (Phase 2 vault).
  Pure function boundary: inputs → plaintext bytes → audit event.
  No other module calls subprocess on behalf of creds.
```

### 10.2 `.importlinter`

Switch from prose invariant to an enforced contract. Add a `forbidden` contract: only `broker.resolver`, `broker.executor`, and `broker.creds` may import `subprocess`. Every other broker module importing `subprocess` is a CI failure.

### 10.3 `executor.py` module docstring

Add one line to the top-of-file docstring: "Dispatch may receive inherited fd 3 for creds-declared capabilities (§17). All other `pass_fds` entries are a bug — see `test_executor.py::test_fd_invariant`."

### 10.4 Root `CLAUDE.md` — Security & Broker section

One-line addition under rule 4:

> Credentials reach capability subprocesses only via fd 3, a one-shot pipe opened by the broker. Donna doesn't open it, read it, or know which capabilities use it. The broker handles it inside `execute`.

### 10.5 Living doc reference

After the Piece C Notion living doc lands, reference it from root `CLAUDE.md`'s Security & Broker section:

> Living doc for ongoing security decisions: [Notion URL]. Update it on any architectural decision worth remembering next session.

## 11. Out of scope for Piece C

- Real executor implementations (Everyone Active, any other real capability). → Piece D.
- Phase 2 rule gate in `CLAUDE.md`. → Piece E.
- Delivery mechanisms other than `fd3` (env, stdin_field). Enum-ready but not implemented.
- Stderr stash file for post-hoc debugging. Deferred until a real incident needs it.
- Keychain / KMS backend abstraction. YAGNI.
- Test-decrypt of each vault entry at startup. Health checks are structural only.

## 12. Sequencing inside Piece C

Rough order for the implementation plan (detailed sequencing is writing-plans' job):

1. Manifest schema + validator additions + validator tests.
2. `CredsConfig` dataclass + `execute()` signature extension + no-op tests (creds_config threaded but not yet used).
3. Startup vault health checks + `verify-vault` subcommand + tests.
4. Audit hardening (§7.1, §7.2) + tests. Lands before any cred actually flows.
5. `_execute_subprocess` integration: pipe + dup2 + write + cleanup. Unit tests with monkeypatched `unlock_creds`.
6. Architectural annotations (CLAUDE.md, .importlinter, docstrings).
7. Synthetic end-to-end + test-only manifest + `synthetic_echo_creds` binary.
8. Living doc entry in Notion (via broker approval flow).
