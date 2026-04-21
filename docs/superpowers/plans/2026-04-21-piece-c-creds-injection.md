# Piece C — Creds Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `broker/creds.py::unlock_creds()` into `broker/executor.py::_execute_subprocess` so a capability can opt in via its manifest to having decrypted credentials delivered through an inherited fd 3 pipe, with audit hardening, startup vault health checks, and a synthetic end-to-end test proving the real-age path works.

**Architecture:** Two-field `creds:` block on a capability entry (`delivery: fd3`, `entry: <filename_stem>`). `CredsConfig` dataclass threaded through `executor.execute()`. Inside `_execute_subprocess`, when a capability declares creds, the broker unlocks bytes via `unlock_creds`, opens an anonymous pipe, remaps the read end to fd 3 in the child via `preexec_fn dup2`, writes the bytes then closes. Capability subprocess reads fd 3 to EOF for its creds. Sibling audit hardening strips verbatim stderr and exception strings from audit events. Startup vault health sweep + `verify-vault` CLI subcommand give operators a structural health check.

**Tech Stack:** Python 3.11+ (broker runs on donna-broker uid), `dataclasses` frozen dataclasses, `subprocess` with `preexec_fn`, `os.pipe`, `os.dup2`. Test framework: `pytest`. Type gate: `mypy --strict`. Architecture gate: `import-linter` (`lint-imports`). Coverage gate: ≥95% per module (`pyproject.toml` has `fail_under = 90` suite-wide).

**Spec reference:** `docs/superpowers/specs/2026-04-21-piece-c-creds-injection-design.md`. All section refs in this plan (§3, §4, etc.) point at that spec unless otherwise noted. Broker spec refs (§2.3, §8, §9.2, §15, §17) point at `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` v1.1.

**Environment note:** The live repo has a PreToolUse hook (`hooks/capability-guard-phase1.py`) that blocks most Bash tool calls. `pytest`, `lint-imports`, `mypy`, `git add`, `git commit` are all blocked inside Claude's Bash invocations. If you are executing this plan as an agent inside that environment, surface those commands to Graham for execution (he runs them with `!<command>` in his Claude Code prompt). If you are executing in a worktree / container without the hook, run them yourself.

---

## File Structure

**Created:**
- `broker/vault_health.py` — health-check module (broker-wide + per-capability structural checks, no decrypt).
- `broker/tests/test_vault_health.py` — unit tests for vault_health.
- `broker/manifests/capabilities.test.yaml` — **test-only** manifest containing the synthetic capability. Never loaded by the live broker.
- `broker/manifests/schemas/synthetic_echo_creds.json` — JSON schema for synthetic capability params.
- `tools/synthetic_echo_creds` — tiny Python script (mode 0755) that reads fd 3 to EOF and prints sha256 JSON.
- `broker/tests/test_synthetic_e2e.py` — the real-age end-to-end test (auto-skips when age absent).

**Modified:**
- `broker/validator.py` — new `CredsBlock` dataclass, new field on `Capability`, new validator `_validate_creds`.
- `broker/tests/test_validator.py` — seven new cases covering creds schema.
- `broker/executor.py` — new `CredsConfig` dataclass, signature extension on `execute()`, full fd-3 integration in `_execute_subprocess`, audit hardening in stderr + spawn-error paths, `_dup_rfd_to_fd3` preexec helper.
- `broker/tests/test_executor.py` — seven new cases (no-creds, happy, unlock-fail, oversize, spawn-fail cleanup, child-exits-early, fd-invariant).
- `broker/main.py` — `CredsConfig` wiring into every `execute()` dispatch, new `verify-vault` subcommand, startup health sweep call.
- `broker/tests/test_main.py` — cases covering `verify-vault` subcommand outputs + exit codes.
- `broker/CLAUDE.md` — subprocess-boundary list extended to include `creds.py`.
- `broker/.importlinter` (or wherever the contracts live) — new `forbidden` contract restricting `subprocess` imports.
- `CLAUDE.md` (root) — one-line addition under rule 4, and living-doc URL reference (added last, after Notion page lands).

---

## Task 1: Manifest schema — `CredsBlock` + validator

**Files:**
- Modify: `broker/validator.py`
- Modify: `broker/tests/test_validator.py`

### Steps

- [ ] **Step 1: Write the failing tests**

Append to `broker/tests/test_validator.py`. Tests use the existing `manifest_dir` fixture (a tmp directory with fake capability YAML + schemas; check the existing file for the fixture's signature and how other tests write `capabilities.yaml` into it).

```python
# ---- §4.2 creds block validation ---------------------------------------

def _write_caps(manifest_dir: Path, capability_yaml: str) -> str:
    """Helper: write a single-capability manifest and return its path."""
    manifest = manifest_dir / "capabilities.yaml"
    manifest.write_text(capability_yaml, encoding="utf-8")
    return str(manifest)


def test_capability_without_creds_block_parses_with_none(manifest_dir):
    # Baseline: absence of creds: leaves the field as None.
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml())
    caps = validator.load_capabilities(caps_path)
    cap = next(iter(caps.values()))
    assert cap.creds is None


def test_capability_with_valid_creds_block_parses(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra="\n    creds:\n      delivery: fd3\n      entry: everyone_active"
    ))
    caps = validator.load_capabilities(caps_path)
    cap = next(iter(caps.values()))
    assert cap.creds is not None
    assert cap.creds.delivery == "fd3"
    assert cap.creds.entry == "everyone_active"


def test_creds_missing_entry_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra="\n    creds:\n      delivery: fd3"
    ))
    with pytest.raises(validator.ManifestError, match="entry"):
        validator.load_capabilities(caps_path)


def test_creds_invalid_delivery_enum_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra="\n    creds:\n      delivery: smoke_signals\n      entry: foo"
    ))
    with pytest.raises(validator.ManifestError, match="delivery"):
        validator.load_capabilities(caps_path)


def test_creds_invalid_entry_pattern_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra='\n    creds:\n      delivery: fd3\n      entry: "Has Spaces"'
    ))
    with pytest.raises(validator.ManifestError, match="entry"):
        validator.load_capabilities(caps_path)


def test_creds_string_instead_of_dict_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra='\n    creds: "yes"'
    ))
    with pytest.raises(validator.ManifestError, match="creds"):
        validator.load_capabilities(caps_path)


def test_creds_null_or_list_raises(manifest_dir):
    for bad in ("    creds: null", "    creds: []"):
        caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
            extra=f"\n{bad}"
        ))
        with pytest.raises(validator.ManifestError, match="creds"):
            validator.load_capabilities(caps_path)
```

Where `_good_subprocess_capability_yaml(extra: str = "")` is a helper returning a minimal valid capability YAML with `extra` appended at capability level. If such a helper doesn't already exist in `test_validator.py`, define it at the top of the new test section:

```python
def _good_subprocess_capability_yaml(extra: str = "") -> str:
    return f"""capabilities:
  - name: gmail.create_draft
    executor:
      type: subprocess
      binary: /usr/local/bin/donna-exec-gmail
      timeout_seconds: 30
    param_schema:
      $ref: ./schemas/gmail_create_draft.json
    risk_level: medium
    idempotency_date_from: created_at
    approval_window_minutes: 15
    execution_window_minutes: 5
    revalidate:
      not_applicable: stateless_write{extra}
"""
```

Note: the `$ref` target must exist in `manifest_dir`. Check the existing fixture to see what schema files it already provides; if `gmail_create_draft.json` isn't seeded, either seed it in the helper or use an inline `param_schema: {type: object}` in the test YAML.

- [ ] **Step 2: Run the failing tests**

Run: `cd broker && pytest tests/test_validator.py -v -k creds`
Expected: 7 failures (tests reference `cap.creds`, which doesn't exist yet).

- [ ] **Step 3: Implement `CredsBlock` and validator logic**

Edit `broker/validator.py`. Near the top-of-file constants (around the `VALID_RISK_LEVELS` block), add:

```python
VALID_CREDS_DELIVERY = frozenset({"fd3"})
CREDS_ENTRY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
```

If `re` isn't imported at the top, add `import re`.

Below the existing `Capability` dataclass, add:

```python
@dataclass(frozen=True)
class CredsBlock:
    """§4 creds-injection opt-in. Presence of a CredsBlock on a
    Capability is the declaration that the capability requires
    credentials at spawn time. See spec §3 for delivery semantics."""
    delivery: str
    entry: str
```

Modify the `Capability` dataclass — add a new field after `execution_window_minutes`:

```python
    creds: CredsBlock | None = None
```

Add a validator function near `_validate_revalidate`:

```python
def _validate_creds(raw: Any, capability_name: str) -> CredsBlock | None:
    """§4.2 creds-block validation. Returns None if absent; raises
    ManifestError on any structural issue."""
    if raw is None or raw == {} and isinstance(raw, dict):
        return None
    if "creds" not in raw:
        return None
    creds = raw["creds"]
    if not isinstance(creds, dict):
        raise ManifestError(
            f"capability {capability_name!r}: creds must be a mapping, "
            f"got {type(creds).__name__}"
        )
    if "delivery" not in creds:
        raise ManifestError(
            f"capability {capability_name!r}: creds.delivery is required"
        )
    if "entry" not in creds:
        raise ManifestError(
            f"capability {capability_name!r}: creds.entry is required"
        )
    delivery = creds["delivery"]
    if delivery not in VALID_CREDS_DELIVERY:
        raise ManifestError(
            f"capability {capability_name!r}: creds.delivery must be one "
            f"of {sorted(VALID_CREDS_DELIVERY)}, got {delivery!r}"
        )
    entry = creds["entry"]
    if not isinstance(entry, str) or not CREDS_ENTRY_RE.match(entry):
        raise ManifestError(
            f"capability {capability_name!r}: creds.entry must match "
            f"{CREDS_ENTRY_RE.pattern!r}, got {entry!r}"
        )
    # Strict: no unknown keys.
    unknown = set(creds.keys()) - {"delivery", "entry"}
    if unknown:
        raise ManifestError(
            f"capability {capability_name!r}: unknown creds keys: "
            f"{sorted(unknown)}"
        )
    return CredsBlock(delivery=delivery, entry=entry)
```

Fix the function signature — the above takes `raw` (the whole capability dict) rather than just `raw["creds"]`; adjust the call site accordingly. Alternative cleaner shape:

```python
def _validate_creds(creds_raw: Any, capability_name: str) -> CredsBlock | None:
    if creds_raw is None:
        return None
    if not isinstance(creds_raw, dict):
        raise ManifestError(
            f"capability {capability_name!r}: creds must be a mapping, "
            f"got {type(creds_raw).__name__}"
        )
    # ... rest identical from delivery/entry checks down
```

Then in `_parse_one_capability`, near where `revalidate` is resolved, add:

```python
    creds_raw = raw.get("creds")   # None if absent
    creds_block = _validate_creds(creds_raw, name)
```

And pass `creds=creds_block` to the `Capability(...)` constructor.

**Edge case for test 7 (`creds: null` vs `creds: []`):** `raw.get("creds")` returns `None` in both YAML cases — wait, YAML `null` → Python `None`, so `raw.get("creds")` gives `None` and `_validate_creds` returns `None` silently, which is wrong. We want `creds: null` to raise. Handle this by checking `"creds" in raw` explicitly:

```python
    if "creds" in raw:
        creds_raw = raw["creds"]
        if creds_raw is None:
            raise ManifestError(
                f"capability {name!r}: creds key present but value is null"
            )
        creds_block = _validate_creds(creds_raw, name)
    else:
        creds_block = None
```

And for `creds: []` (a list), the existing `isinstance(creds_raw, dict)` check in `_validate_creds` catches it.

- [ ] **Step 4: Run the tests, verify pass**

Run: `cd broker && pytest tests/test_validator.py -v -k creds`
Expected: 7 passes.

Also run the full validator test file to catch regressions:

Run: `cd broker && pytest tests/test_validator.py -v`
Expected: all existing + new tests pass.

- [ ] **Step 5: Run mypy + coverage**

Run: `cd broker && mypy broker tests`
Expected: clean.

Run: `cd broker && pytest tests/test_validator.py --cov=broker.validator --cov-report=term-missing`
Expected: `broker/validator.py` coverage ≥95%.

- [ ] **Step 6: Commit**

```bash
git add broker/validator.py broker/tests/test_validator.py
git commit -m "broker: validator parses creds:{delivery,entry} block"
```

---

## Task 2: `CredsConfig` dataclass + `execute()` signature

**Files:**
- Modify: `broker/executor.py`
- Modify: `broker/tests/test_executor.py`

### Steps

- [ ] **Step 1: Write the failing tests**

Append to `broker/tests/test_executor.py`. Assumes the existing `FakeCapability` gets extended to include a `creds` attribute (see Step 3 below).

```python
# ---- §5 CredsConfig threading -------------------------------------------


def test_execute_no_creds_config_accepted_for_capability_without_creds(conn, tmp_path):
    """Baseline: a capability without creds runs fine without creds_config."""
    r = _insert_approved(conn, "r-nocreds", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, 'import sys; sys.stdout.write("{}")'),
        revalidate={"not_applicable": "no_external_state"},
        creds=None,
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "succeeded"


def test_execute_missing_creds_config_fails_closed(conn, tmp_path):
    """Capability declares creds:, but execute() was called without
    creds_config. Row transitions to failed with creds_config_missing."""
    r = _insert_approved(conn, "r-cfgmissing", "capB")
    cap = FakeCapability(
        name="capB", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=executor.CredsBlockLike(delivery="fd3", entry="foo"),
    )
    outcome = executor.execute(cap, r, {}, conn, creds_config=None)
    assert outcome.state == "failed"
    assert outcome.error_code == "creds_config_missing"


def test_credsconfig_is_frozen_dataclass(tmp_path):
    cfg = executor.CredsConfig(
        creds_dir=tmp_path,
        identity_path=tmp_path / "identity.age",
    )
    assert cfg.age_binary == "age"
    assert cfg.timeout_seconds == 10.0
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        cfg.age_binary = "something_else"  # type: ignore[misc]
```

Note: `executor.CredsBlockLike` in the second test is a Protocol equivalent defined alongside `CapabilityLike` in executor.py — see Step 3. If the executor doesn't use a Protocol for creds, use `validator.CredsBlock` directly. Preference: a new Protocol to keep executor decoupled from validator at the type level.

- [ ] **Step 2: Run the failing tests**

Run: `cd broker && pytest tests/test_executor.py -v -k creds_config`
Expected: 3 failures (CredsConfig / CredsBlockLike / creds parameter don't exist yet).

- [ ] **Step 3: Implement `CredsConfig` + Protocol + signature**

Edit `broker/executor.py`.

Add near the top-of-file imports:

```python
import os
import hashlib
```

(`os` may already be imported; if not, add it. `hashlib` needed for Task 4.)

Extend the `CapabilityLike` Protocol (around line 47) with a creds accessor:

```python
class CredsBlockLike(Protocol):
    @property
    def delivery(self) -> str: ...
    @property
    def entry(self) -> str: ...


class CapabilityLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def executor_type(self) -> str: ...
    @property
    def executor_target(self) -> str: ...
    @property
    def revalidate(self) -> dict[str, Any]: ...
    @property
    def creds(self) -> CredsBlockLike | None: ...   # NEW
```

Add the `CredsConfig` frozen dataclass near the other module-level dataclasses:

```python
@dataclass(frozen=True)
class CredsConfig:
    """§5 creds injection runtime config. Constructed by main.py from
    broker constants; passed to execute() for every dispatch that may
    touch a creds-declared capability. Never wired as module-global
    state."""
    creds_dir: Path
    identity_path: Path
    age_binary: str = "age"
    timeout_seconds: float = 10.0
```

Extend `execute()` signature:

```python
def execute(
    capability: CapabilityLike,
    request: RequestLike,
    params: dict[str, Any],
    state_conn: Any,
    audit_writer: AuditWriter | None = None,
    revalidate_handlers: Optional[dict[str, Revalidator]] = None,
    subprocess_timeout_seconds: float = DEFAULT_EXECUTOR_TIMEOUT_SECONDS,
    creds_config: CredsConfig | None = None,
) -> ExecutionOutcome:
```

Inside `execute()`, after durable start + revalidation, **before** dispatch:

```python
    # §5.2 fail-closed guard: capability declares creds but no config
    # was threaded into execute(). No unlock attempt, no spawn.
    if capability.creds is not None and creds_config is None:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "creds_config_missing",
            f"capability {capability.name!r} declares creds but "
            f"execute() was called without creds_config",
        )
```

Thread `creds_config` into `_execute_subprocess` call:

```python
    if capability.executor_type == "subprocess":
        return _execute_subprocess(
            capability, request, params, state_conn, audit_writer,
            subprocess_timeout_seconds, creds_config,
        )
```

Update `_execute_subprocess` signature:

```python
def _execute_subprocess(
    capability: CapabilityLike,
    request: RequestLike,
    params: dict[str, Any],
    state_conn: Any,
    audit_writer: AuditWriter | None,
    timeout_seconds: float,
    creds_config: CredsConfig | None,
) -> ExecutionOutcome:
```

Full fd-3 integration happens in Task 5 — for now, `_execute_subprocess` ignores `creds_config`. That keeps this task narrow and keeps tests green.

Extend `FakeCapability` in `test_executor.py` to include `creds`. Find the dataclass definition near the top (~line 24) and modify:

```python
@dataclass
class FakeCapability:
    name: str
    executor_type: str
    executor_target: str
    revalidate: dict[str, Any]
    creds: Any = None   # NEW — accepts None or a CredsBlock-like
```

- [ ] **Step 4: Run the tests, verify pass**

Run: `cd broker && pytest tests/test_executor.py -v -k creds_config`
Expected: 3 passes.

Run: `cd broker && pytest tests/test_executor.py -v`
Expected: all existing + new tests pass. Existing tests don't pass `creds_config`, which defaults to `None`, and their `FakeCapability` instances don't set `creds`, which defaults to `None`. Both paths compatible.

- [ ] **Step 5: Run mypy**

Run: `cd broker && mypy broker tests`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add broker/executor.py broker/tests/test_executor.py
git commit -m "broker: thread CredsConfig through execute(); fail-closed when missing"
```

---

## Task 3: Startup vault health checks + `verify-vault` subcommand

**Files:**
- Create: `broker/vault_health.py`
- Create: `broker/tests/test_vault_health.py`
- Modify: `broker/main.py`
- Modify: `broker/tests/test_main.py`

### Steps

- [ ] **Step 1: Write the failing tests for `vault_health` module**

Create `broker/tests/test_vault_health.py`:

```python
"""Tests for broker.vault_health.

Spec ref: design §6 startup vault health checks.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from broker import vault_health


@pytest.fixture
def good_vault(tmp_path, monkeypatch):
    """Build a structurally valid vault dir + identity + one entry.
    Ownership checks are bypassed via monkeypatch because tests do not
    run as donna-broker."""
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o750)
    identity = creds_dir / "identity.age"
    identity.write_text("AGE-SECRET-KEY-FAKE", encoding="utf-8")
    identity.chmod(0o400)
    entry = creds_dir / "everyone_active.age"
    entry.write_text("age-ciphertext-fake", encoding="utf-8")
    entry.chmod(0o440)

    # Bypass ownership checks — tests don't run as donna-broker.
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda path, want_uid: True)
    return creds_dir, identity, [entry]


def test_all_checks_pass_emits_no_warnings(good_vault, monkeypatch):
    creds_dir, identity, _entries = good_vault
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")

    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=identity,
        age_binary="age",
        declared_entries=["everyone_active"],
        audit_writer=captured.append,
    )
    assert captured == []


def test_vault_dir_missing(tmp_path, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(vault_health, "_resolve_age_binary", lambda b: "/usr/local/bin/age")
    vault_health.sweep(
        creds_dir=tmp_path / "does-not-exist",
        identity_path=tmp_path / "does-not-exist" / "identity.age",
        age_binary="age",
        declared_entries=[],
        audit_writer=captured.append,
    )
    assert any(w["reason"] == "vault_dir_missing" for w in captured)


def test_vault_dir_mode_loose(tmp_path, monkeypatch):
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o777)   # too permissive
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda path, want_uid: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=creds_dir / "identity.age",
        age_binary="age",
        declared_entries=[],
        audit_writer=captured.append,
    )
    assert any(w["reason"] == "vault_dir_mode_loose" for w in captured)


def test_identity_missing(tmp_path, monkeypatch):
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o750)
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=creds_dir / "identity.age",
        age_binary="age",
        declared_entries=[],
        audit_writer=captured.append,
    )
    assert any(w["reason"] == "identity_missing" for w in captured)


def test_identity_mode_loose(good_vault, monkeypatch):
    creds_dir, identity, _ = good_vault
    identity.chmod(0o644)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=[], audit_writer=captured.append,
    )
    assert any(w["reason"] == "identity_mode_loose" for w in captured)


def test_age_binary_missing(good_vault, monkeypatch):
    creds_dir, identity, _ = good_vault
    monkeypatch.setattr(vault_health, "_resolve_age_binary", lambda b: None)
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=[], audit_writer=captured.append,
    )
    assert any(w["reason"] == "age_binary_missing" for w in captured)


def test_entry_missing(good_vault, monkeypatch):
    creds_dir, identity, _ = good_vault
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=["everyone_active", "never_written"],
        audit_writer=captured.append,
    )
    # everyone_active exists; never_written does not.
    warnings = [w for w in captured if w["reason"] == "entry_missing"]
    assert len(warnings) == 1
    assert warnings[0]["entry"] == "never_written"


def test_entry_mode_loose(good_vault, monkeypatch):
    creds_dir, identity, entries = good_vault
    entries[0].chmod(0o644)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=["everyone_active"], audit_writer=captured.append,
    )
    assert any(w["reason"] == "entry_mode_loose" for w in captured)


def test_owner_checks_emit_warnings(tmp_path, monkeypatch):
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o750)
    identity = creds_dir / "identity.age"
    identity.write_text("x", encoding="utf-8"); identity.chmod(0o400)
    # Pretend every owner check fails.
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: False)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=[], audit_writer=captured.append,
    )
    reasons = {w["reason"] for w in captured}
    assert "vault_dir_owner_wrong" in reasons
    assert "identity_owner_wrong" in reasons


def test_multiple_failures_all_reported(tmp_path, monkeypatch):
    # No vault dir, no identity, no age binary — three warnings minimum.
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary", lambda b: None)
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=tmp_path / "missing",
        identity_path=tmp_path / "missing" / "identity.age",
        age_binary="age",
        declared_entries=[],
        audit_writer=captured.append,
    )
    reasons = [w["reason"] for w in captured]
    assert "vault_dir_missing" in reasons
    assert "age_binary_missing" in reasons
```

- [ ] **Step 2: Run the failing tests**

Run: `cd broker && pytest tests/test_vault_health.py -v`
Expected: `ModuleNotFoundError: broker.vault_health`.

- [ ] **Step 3: Implement `broker/vault_health.py`**

Create `broker/vault_health.py`:

```python
"""Startup vault health checks.

Spec: design §6 + security-v1.1 §10 (fail-closed — warnings only, don't
refuse boot), §15 (audit redaction posture).

Usage:
    from broker import vault_health
    vault_health.sweep(
        creds_dir=Path("/Users/donna-broker/.config/donna/creds"),
        identity_path=Path(".../identity.age"),
        age_binary="age",
        declared_entries=["gmail", "everyone_active"],
        audit_writer=audit_append,
    )

Each failed check emits a `creds_vault_warning` audit event with a
stable `reason` code. The broker does not refuse to start — a
misconfigured capability fails at request time; everything else keeps
working.

The ten reason codes are the contract. See spec §6.1-§6.2.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any, Callable, Iterable

AuditWriter = Callable[[dict[str, Any]], Any]

DONNA_BROKER_UID_NAME = "donna-broker"


def _check_owner_matches(path: Path, want_uid: int) -> bool:
    """Stat path; return True iff owner UID matches. Monkeypatchable
    in tests that don't run as donna-broker."""
    try:
        return path.stat().st_uid == want_uid
    except OSError:
        return False


def _resolve_age_binary(binary: str) -> str | None:
    """Return resolved absolute path or None if not found.
    Monkeypatchable in tests."""
    return shutil.which(binary)


def _get_donna_broker_uid() -> int | None:
    """Lookup uid by name. Returns None if the user doesn't exist on
    this host, which is only possible in a test environment — tests
    monkeypatch _check_owner_matches to bypass ownership assertions."""
    try:
        import pwd
        return pwd.getpwnam(DONNA_BROKER_UID_NAME).pw_uid
    except (KeyError, ImportError):
        return None


def _emit(audit_writer: AuditWriter | None, reason: str, **extra: Any) -> None:
    if audit_writer is None:
        return
    event: dict[str, Any] = {"event": "creds_vault_warning", "reason": reason}
    event.update(extra)
    try:
        audit_writer(event)
    except Exception:
        # §10 — audit failure never blocks startup.
        pass


def sweep(
    *,
    creds_dir: Path,
    identity_path: Path,
    age_binary: str,
    declared_entries: Iterable[str],
    audit_writer: AuditWriter | None,
) -> list[dict[str, Any]]:
    """Run the ten structural checks. Returns the list of warnings
    (also emitted via audit_writer). Never raises."""
    warnings: list[dict[str, Any]] = []

    def warn(reason: str, **extra: Any) -> None:
        w = {"reason": reason, **extra}
        warnings.append(w)
        _emit(audit_writer, reason, **extra)

    donna_uid = _get_donna_broker_uid()

    # ---- broker-wide (§6.1) ---------------------------------------------

    if not creds_dir.exists() or not creds_dir.is_dir():
        warn("vault_dir_missing", path=str(creds_dir))
        # No point running further dir-mode/owner checks.
    else:
        mode = stat.S_IMODE(creds_dir.stat().st_mode)
        if mode & 0o027:   # world or group-write/x beyond 0750
            warn("vault_dir_mode_loose", path=str(creds_dir),
                 mode=f"{mode:04o}")
        if donna_uid is not None and not _check_owner_matches(creds_dir, donna_uid):
            warn("vault_dir_owner_wrong", path=str(creds_dir),
                 want_uid=donna_uid)

    if not identity_path.exists():
        warn("identity_missing", path=str(identity_path))
    else:
        mode = stat.S_IMODE(identity_path.stat().st_mode)
        if mode != 0o400:
            warn("identity_mode_loose", path=str(identity_path),
                 mode=f"{mode:04o}")
        if donna_uid is not None and not _check_owner_matches(identity_path, donna_uid):
            warn("identity_owner_wrong", path=str(identity_path),
                 want_uid=donna_uid)

    if _resolve_age_binary(age_binary) is None:
        warn("age_binary_missing", binary=age_binary)

    # ---- per-capability (§6.2) ------------------------------------------

    if creds_dir.exists() and creds_dir.is_dir():
        for entry in declared_entries:
            entry_path = creds_dir / f"{entry}.age"
            if not entry_path.exists():
                warn("entry_missing", entry=entry, path=str(entry_path))
                continue
            mode = stat.S_IMODE(entry_path.stat().st_mode)
            if mode & 0o037:   # tighter than 0440
                warn("entry_mode_loose", entry=entry,
                     path=str(entry_path), mode=f"{mode:04o}")
            if donna_uid is not None and not _check_owner_matches(entry_path, donna_uid):
                warn("entry_owner_wrong", entry=entry,
                     path=str(entry_path), want_uid=donna_uid)

    return warnings
```

- [ ] **Step 4: Run `test_vault_health.py`, verify pass**

Run: `cd broker && pytest tests/test_vault_health.py -v`
Expected: 10 passes.

- [ ] **Step 5: Add `verify-vault` subcommand + test**

Edit `broker/main.py`. Find the modes tuple near line 44:

```python
# Existing (around line 44):
    "audit-result", "rotate-hmac", "verify-audit", "verify-manifests",
```

Add `"verify-vault"`:

```python
    "audit-result", "rotate-hmac", "verify-audit", "verify-manifests",
    "verify-vault",
```

Also add to the second modes tuple at line ~945 (the one guarded by `need_manifests`). Read the surrounding context to understand which of the two modes lists this belongs in — `verify-vault` needs to load manifests (to know `declared_entries`) so it goes in the manifest-requiring list.

Add a handler near `_handle_verify_manifests` (around line 866):

```python
def _handle_verify_vault(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """§6.3 verify-vault subcommand. Runs the same checks as the
    startup sweep, prints one line per check, exits non-zero if any
    warning fires."""
    from broker import vault_health

    caps = ctx["capabilities"]
    declared = [c.creds.entry for c in caps.values() if c.creds is not None]

    creds_dir = Path(ctx["config"]["creds_dir"])
    identity_path = Path(ctx["config"]["identity_path"])
    age_binary = ctx["config"].get("age_binary", "age")

    warnings = vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=identity_path,
        age_binary=age_binary,
        declared_entries=declared,
        audit_writer=None,   # CLI path doesn't audit
    )

    # Format per spec §6.3.
    lines: list[str] = []
    # Print OK lines for the broker-wide checks that passed (derived
    # by elimination from warnings).
    broker_wide = [
        ("vault_dir_permissions", str(creds_dir)),
        ("identity_mode", str(identity_path)),
        ("age_binary", age_binary),
    ]
    warned_reasons = {w["reason"] for w in warnings}
    # Simplified OK listing — detailed output format is a design choice
    # you may expand. Minimum requirement per spec: OK/WARN prefix,
    # reason code, artefact, summary.
    for w in warnings:
        detail = " ".join(f"{k}={v}" for k, v in w.items()
                          if k not in {"reason"})
        lines.append(f"WARN {w['reason']:22s} {detail}")
    ok_count = 10 - len(warnings)   # rough; see spec §6.3
    lines.append("--")
    lines.append(f"{len(warnings)} warnings, {ok_count} checks passed.")

    return {
        "status": "ok" if not warnings else "warnings",
        "warnings": warnings,
        "stdout_lines": lines,
        "exit_code": 0 if not warnings else 1,
    }
```

Register the handler in the dispatch dict (search for the line like `"verify-manifests": _handle_verify_manifests,` and add `"verify-vault": _handle_verify_vault,`).

Wire into the CLI main function so `donna-broker verify-vault` emits the lines to stdout and exits with the computed code. Check how `_handle_verify_manifests`'s stdout/exit-code handling is plumbed through `main()` and mirror it.

- [ ] **Step 6: Test `verify-vault` subcommand**

Add to `broker/tests/test_main.py`:

```python
def test_verify_vault_clean_exits_zero(tmp_path, monkeypatch):
    # Build a minimal clean vault + patch owner/age checks.
    from broker import main, vault_health
    creds_dir = tmp_path / "creds"; creds_dir.mkdir(mode=0o750)
    (creds_dir / "identity.age").write_text("x"); (creds_dir / "identity.age").chmod(0o400)
    monkeypatch.setattr(vault_health, "_check_owner_matches", lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary", lambda b: "/usr/local/bin/age")
    # Drive main() with argv = ["verify-vault"] — details depend on how
    # main.py's entrypoint is organised. See test_main.py's existing
    # verify-manifests test for the harness pattern.
    # ... (exact harness: copy from nearest test_verify_manifests_* test)


def test_verify_vault_with_warning_exits_nonzero(tmp_path, monkeypatch):
    from broker import main, vault_health
    # Missing identity triggers identity_missing warning.
    creds_dir = tmp_path / "creds"; creds_dir.mkdir(mode=0o750)
    monkeypatch.setattr(vault_health, "_check_owner_matches", lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary", lambda b: "/usr/local/bin/age")
    # ... drive main(), assert non-zero exit and "identity_missing" in output.
```

The scaffolding here is deliberately sketched, not complete — `test_main.py` already has an established harness for verify-manifests; the implementer should grep for `verify_manifests` in `test_main.py`, pick the nearest test, and mirror its shape.

- [ ] **Step 7: Wire startup sweep into `main.py`**

In the broker startup path (`main()` function or `_build_ctx`, wherever manifests get loaded), add a call that runs only when at least one capability declares creds:

```python
from broker import vault_health

# After capabilities are loaded:
declared = [c.creds.entry for c in caps.values() if c.creds is not None]
if declared:
    vault_health.sweep(
        creds_dir=Path(config["creds_dir"]),
        identity_path=Path(config["identity_path"]),
        age_binary=config.get("age_binary", "age"),
        declared_entries=declared,
        audit_writer=ctx.get("audit_writer"),
    )
```

Read `_build_ctx` to see where this fits cleanly.

Also add `creds_dir`, `identity_path` as new config keys in `_config_from_env` with sensible defaults:

```python
cfg = {
    # ... existing keys
    "creds_dir": env.get("DONNA_CREDS_DIR",
                         "/Users/donna-broker/.config/donna/creds"),
    "identity_path": env.get("DONNA_IDENTITY_PATH",
                             "/Users/donna-broker/.config/donna/creds/identity.age"),
    "age_binary": env.get("DONNA_AGE_BINARY", "age"),
}
```

- [ ] **Step 8: Run all tests + mypy**

Run: `cd broker && pytest -v`
Expected: all pass (existing + new).

Run: `cd broker && mypy broker tests`
Expected: clean.

Run: `cd broker && lint-imports`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add broker/vault_health.py broker/tests/test_vault_health.py \
        broker/main.py broker/tests/test_main.py
git commit -m "broker: add vault_health + verify-vault subcommand"
```

---

## Task 4: Audit hardening — stderr redact + spawn-error type-only

**Files:**
- Modify: `broker/executor.py` (lines around 295–309 — the `_execute_subprocess` stderr/spawn-error paths)
- Modify: `broker/tests/test_executor.py`

### Steps

- [ ] **Step 1: Write the failing tests**

Append to `broker/tests/test_executor.py`:

```python
# ---- §7 audit hardening -------------------------------------------------


def test_stderr_audit_carries_no_verbatim_body(conn, tmp_path):
    r = _insert_approved(conn, "r-stderrbody", "capA")
    # Executor prints a fake token-looking payload to stderr.
    body = 'import sys; sys.stderr.write("secret-token-ABC123\\n"); sys.exit(0)'
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=None,
    )
    events: list[dict] = []
    executor.execute(cap, r, {}, conn, audit_writer=events.append)

    stderr_events = [e for e in events if e.get("event") == "executor_stderr"]
    assert len(stderr_events) == 1
    ev = stderr_events[0]
    # Must not contain the raw bytes anywhere.
    serialised = json.dumps(ev)
    assert "secret-token-ABC123" not in serialised
    # Must contain required shape.
    assert "stderr_bytes" in ev
    assert "stderr_sha256" in ev
    # SPEC AMENDMENT: stderr_head_redacted was dropped during Task 4
    # review (sanitise_context_reason doesn't strip arbitrary secrets
    # and caps inputs at 200 chars). Use the assertion below instead.
    assert "stderr_head_redacted" not in ev
    assert set(ev.keys()) == {"event", "request_id", "stderr_bytes", "stderr_sha256"}
    assert len(ev["stderr_sha256"]) == 64


def test_spawn_error_audit_carries_no_exception_message(conn, tmp_path, monkeypatch):
    r = _insert_approved(conn, "r-spawnerr", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=None,
    )

    class ExplodingError(Exception):
        pass

    def boom(*a, **kw):
        raise ExplodingError("sensitive-looking-message-XYZ")

    monkeypatch.setattr(executor.subprocess, "Popen", boom)

    events: list[dict] = []
    outcome = executor.execute(cap, r, {}, conn, audit_writer=events.append)
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_spawn_error"

    failed = [e for e in events if e.get("event") == "request_execution_failed"]
    assert len(failed) == 1
    serialised = json.dumps(failed[0])
    assert "sensitive-looking-message-XYZ" not in serialised
    assert failed[0].get("detail") == "ExplodingError"
    assert failed[0].get("exception_type") == "ExplodingError"
```

- [ ] **Step 2: Run failing tests**

Run: `cd broker && pytest tests/test_executor.py -v -k "audit or stderr or spawn_error"`
Expected: 2 failures. (Note: there's likely an existing `test_subprocess_stderr_captured` test that will *also* fail because its assertions reference the old shape — update it in Step 3.)

- [ ] **Step 3: Implement stderr + spawn-error hardening**

Edit `broker/executor.py`.

Replace the stderr block (currently lines ~302–309) with:

```python
    if stderr:
        # §7.1 AMENDED: hash + length only, no head field.
        _emit(audit_writer, {
            "event": "executor_stderr",
            "request_id": request.request_id,
            "stderr_bytes": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        })
```

**Spec amendment note:** the original Task 4 plan included a `stderr_head_redacted` field using `sanitise_context_reason`. The review cycle revealed that helper raises `ContextReasonTooLong` on > 200-char input, and it doesn't strip arbitrary literal secrets (its patterns are URLs / long hex / digits / non-Latin). Hash-only is the safer posture. See spec §7.1 for the amended text.

Remove `STDERR_TRUNCATION_MARKER` and `_truncate` if no longer used elsewhere — check by grepping `broker/executor.py`. If `_truncate` is used by the stdout-size path, leave it alone; only the stderr call-site changes.

Replace the spawn-error block (currently lines ~295–300):

```python
    except subprocess.TimeoutExpired:
        # Unchanged — timeout path keeps its current shape.
        ...
    except FileNotFoundError:
        # Unchanged.
        ...
    except Exception as e:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_spawn_error",
            type(e).__name__,
            extra={"exception_type": type(e).__name__},
        )
```

Also update any existing `test_subprocess_stderr_captured` / `test_subprocess_stderr_over_cap_truncated` tests in `test_executor.py` that assert on `ev["stderr"]` or `STDERR_TRUNCATION_MARKER` — replace their assertions with the new shape (`stderr_sha256`, closed key-set check) or delete them if superseded by the new tests.

- [ ] **Step 4: Run the tests, verify pass**

Run: `cd broker && pytest tests/test_executor.py -v`
Expected: all pass.

- [ ] **Step 5: Run mypy + coverage**

Run: `cd broker && mypy broker tests`
Expected: clean.

Run: `cd broker && pytest tests/test_executor.py --cov=broker.executor --cov-report=term-missing`
Expected: ≥95%.

- [ ] **Step 6: Commit**

```bash
git add broker/executor.py broker/tests/test_executor.py
git commit -m "broker: harden executor_stderr + executor_spawn_error audit shapes"
```

---

## Task 5: fd-3 integration in `_execute_subprocess`

**Files:**
- Modify: `broker/executor.py`
- Modify: `broker/tests/test_executor.py`

### Steps

- [ ] **Step 1: Write the failing tests**

Append to `broker/tests/test_executor.py`:

```python
# ---- §3 fd-3 creds injection --------------------------------------------


class _FakeCredsBlock:
    def __init__(self, delivery: str, entry: str) -> None:
        self.delivery = delivery
        self.entry = entry


def _creds_config(tmp_path):
    return executor.CredsConfig(
        creds_dir=tmp_path, identity_path=tmp_path / "identity.age"
    )


def test_creds_happy_path_child_reads_bytes_from_fd3(conn, tmp_path, monkeypatch):
    r = _insert_approved(conn, "r-creds-ok", "capA")
    body = (
        "import os, sys, json, hashlib\n"
        "data = os.read(3, 65536)\n"
        "sys.stdout.write(json.dumps({'sha256': hashlib.sha256(data).hexdigest()}))\n"
    )
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "token_entry"),
    )
    expected = b"token-payload-xyz"
    monkeypatch.setattr(
        "broker.creds.unlock_creds",
        lambda *a, **kw: expected,
    )
    import hashlib
    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "succeeded"
    assert outcome.result["sha256"] == hashlib.sha256(expected).hexdigest()


def test_creds_unlock_failure_blocks_spawn(conn, tmp_path, monkeypatch):
    from broker import creds as creds_module
    r = _insert_approved(conn, "r-creds-fail", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "missing_entry"),
    )

    def raiser(*a, **kw):
        raise creds_module.CredsError("creds_missing", "no such entry")

    monkeypatch.setattr("broker.creds.unlock_creds", raiser)

    # Spy on Popen to prove it never ran.
    popen_calls: list = []
    orig_popen = executor.subprocess.Popen
    monkeypatch.setattr(executor.subprocess, "Popen",
                        lambda *a, **kw: popen_calls.append((a, kw)) or orig_popen(*a, **kw))

    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "creds_missing"
    assert popen_calls == []


def test_creds_oversize_fails_with_creds_too_large(conn, tmp_path, monkeypatch):
    r = _insert_approved(conn, "r-creds-big", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "big_entry"),
    )
    monkeypatch.setattr(
        "broker.creds.unlock_creds",
        lambda *a, **kw: b"X" * (16 * 1024 + 1),
    )

    popen_calls: list = []
    orig = executor.subprocess.Popen
    monkeypatch.setattr(executor.subprocess, "Popen",
                        lambda *a, **kw: popen_calls.append(None) or orig(*a, **kw))

    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "creds_too_large"
    assert popen_calls == []


def test_spawn_failure_cleans_up_pipe(conn, tmp_path, monkeypatch):
    """os.pipe() succeeds; Popen raises. Both fds must be closed."""
    r = _insert_approved(conn, "r-creds-popenfail", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "entry"),
    )
    monkeypatch.setattr("broker.creds.unlock_creds",
                        lambda *a, **kw: b"short")

    opened_pipes: list[tuple[int, int]] = []
    orig_pipe = os.pipe

    def spy_pipe():
        r_fd, w_fd = orig_pipe()
        opened_pipes.append((r_fd, w_fd))
        return r_fd, w_fd

    monkeypatch.setattr("broker.executor.os.pipe", spy_pipe)

    def blowup(*a, **kw):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(executor.subprocess, "Popen", blowup)

    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_spawn_error"

    # Both fds should now be closed — proven by a fresh os.pipe() not
    # handing out the same numbers until the OS recycles them. Easier:
    # try os.fstat — closed fd raises OSError(EBADF).
    import errno
    for fd in opened_pipes[0]:
        try:
            os.fstat(fd)
            raise AssertionError(f"fd {fd} still open after spawn failure")
        except OSError as exc:
            assert exc.errno == errno.EBADF


def test_child_exits_without_reading_fd3_still_handled(conn, tmp_path, monkeypatch):
    """Child exits zero without consuming fd 3. Exit status is
    authoritative — row goes to succeeded."""
    r = _insert_approved(conn, "r-creds-noread", "capA")
    # Child exits without ever reading fd 3.
    body = 'import sys; sys.stdout.write("{}"); sys.exit(0)'
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "entry"),
    )
    monkeypatch.setattr("broker.creds.unlock_creds",
                        lambda *a, **kw: b"never-read")
    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "succeeded"


def test_fd_invariant_across_dispatches(conn, tmp_path, monkeypatch):
    """pass_fds is () for creds-less capabilities and exactly one fd
    for creds-declared. No other shapes."""
    popen_kwargs: list[dict] = []
    orig_popen = executor.subprocess.Popen

    def capture(*a, **kw):
        popen_kwargs.append(kw)
        return orig_popen(*a, **kw)

    monkeypatch.setattr(executor.subprocess, "Popen", capture)

    # No-creds dispatch.
    r1 = _insert_approved(conn, "r-inv-1", "capA")
    cap1 = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, 'import sys; sys.stdout.write("{}")'),
        revalidate={"not_applicable": "no_external_state"}, creds=None,
    )
    executor.execute(cap1, r1, {}, conn)

    # Creds dispatch.
    monkeypatch.setattr("broker.creds.unlock_creds", lambda *a, **kw: b"ok")
    r2 = _insert_approved(conn, "r-inv-2", "capA")
    cap2 = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, 'import os, sys; os.read(3, 1024); sys.stdout.write("{}")'),
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "entry"),
    )
    executor.execute(cap2, r2, {}, conn, creds_config=_creds_config(tmp_path))

    pass_fds_seen = [kw.get("pass_fds", ()) for kw in popen_kwargs]
    assert pass_fds_seen[0] == ()
    assert len(pass_fds_seen[1]) == 1
    # The single fd must be a valid int handed to us by os.pipe.
    assert isinstance(pass_fds_seen[1][0], int)
```

- [ ] **Step 2: Run failing tests**

Run: `cd broker && pytest tests/test_executor.py -v -k creds`
Expected: 6 failures.

- [ ] **Step 3: Implement `_dup_rfd_to_fd3` helper + fd-3 branch**

Edit `broker/executor.py`.

Add near the other module helpers (below `_sanitised_env`):

```python
# Module-level; preexec_fn must not close over per-call state.
_CREDS_FD_TARGET = 3
_CREDS_MAX_BYTES = 16 * 1024


def _make_dup_rfd_to_fd3(r_fd: int) -> Callable[[], None]:
    """Build a preexec_fn that remaps r_fd to fd 3 in the child before
    exec. Kept tiny: no imports, no branching beyond the minimum, no
    logging. §3.2."""
    def _dup() -> None:
        os.dup2(r_fd, _CREDS_FD_TARGET)
        if r_fd != _CREDS_FD_TARGET:
            os.close(r_fd)
    return _dup
```

Modify `_run_executor_subprocess` to accept optional fd-3 wiring. Cleanest: split the creds-bearing path into its own helper, keep the non-creds path untouched. Option A is to add fd-wiring kwargs to `_run_executor_subprocess`; Option B is to inline the fd handling in `_execute_subprocess` around the call. Option B is easier to follow because the lifecycle of r_fd/w_fd lives alongside the unlock call.

Option B implementation — modify `_execute_subprocess`:

```python
def _execute_subprocess(
    capability, request, params, state_conn, audit_writer,
    timeout_seconds, creds_config,
):
    stdin_payload = json.dumps(
        {"capability": capability.name, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")

    # §3 creds wiring — happens only when the capability opts in.
    cred_bytes: bytes | None = None
    r_fd: int | None = None
    w_fd: int | None = None
    pass_fds: tuple[int, ...] = ()
    preexec: Callable[[], None] | None = None

    if capability.creds is not None:
        # creds_config presence is already guaranteed by execute()'s
        # fail-closed guard. Defence in depth here too.
        assert creds_config is not None
        from broker import creds as _creds
        try:
            cred_bytes = _creds.unlock_creds(
                capability.creds.entry,
                creds_dir=str(creds_config.creds_dir),
                identity_path=str(creds_config.identity_path),
                age_binary=creds_config.age_binary,
                timeout_seconds=creds_config.timeout_seconds,
                audit_writer=audit_writer,
            )
        except _creds.CredsError as ce:
            return _finalise_failure(
                state_conn, request, audit_writer,
                ce.error_code, ce.message,
            )

        if len(cred_bytes) > _CREDS_MAX_BYTES:
            # Drop the plaintext reference immediately — we're not
            # going to use it.
            del cred_bytes
            return _finalise_failure(
                state_conn, request, audit_writer,
                "creds_too_large",
                f"creds exceed {_CREDS_MAX_BYTES} bytes",
            )

        try:
            r_fd, w_fd = os.pipe()
            os.set_inheritable(r_fd, True)
            os.set_inheritable(w_fd, False)
        except OSError as oe:
            if r_fd is not None:
                os.close(r_fd)
            if w_fd is not None:
                os.close(w_fd)
            del cred_bytes
            return _finalise_failure(
                state_conn, request, audit_writer,
                "creds_pipe_error",
                f"os.pipe failed: {type(oe).__name__}",
            )

        pass_fds = (r_fd,)
        preexec = _make_dup_rfd_to_fd3(r_fd)

    # Spawn + communicate block.
    try:
        exit_code, stdout, stderr = _run_executor_subprocess_with_fds(
            capability.executor_target,
            stdin_payload,
            timeout_seconds,
            pass_fds=pass_fds,
            preexec=preexec,
            cred_bytes=cred_bytes,
            w_fd=w_fd,
            r_fd=r_fd,
        )
    except subprocess.TimeoutExpired:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_timeout",
            f"timed out after {timeout_seconds}s",
            extra={"timeout_seconds": timeout_seconds},
        )
    except FileNotFoundError:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_missing",
            f"binary {capability.executor_target!r} not found",
        )
    except Exception as e:
        return _finalise_failure(
            state_conn, request, audit_writer,
            "executor_spawn_error",
            type(e).__name__,
            extra={"exception_type": type(e).__name__},
        )
    finally:
        # Plaintext reference is dropped inside _run_executor_subprocess_with_fds
        # after the write. Belt-and-braces: if we never reached that
        # path, the local cred_bytes is still live; nuke it here too.
        if cred_bytes is not None:
            del cred_bytes

    # ... existing stderr-audit + exit-code + stdout handling unchanged
```

And a new helper that handles the fd cleanup across the spawn path:

```python
def _run_executor_subprocess_with_fds(
    binary: str,
    stdin_payload: bytes,
    timeout_seconds: float,
    *,
    pass_fds: tuple[int, ...],
    preexec: Callable[[], None] | None,
    cred_bytes: bytes | None,
    w_fd: int | None,
    r_fd: int | None,
) -> tuple[int, bytes, bytes]:
    """Like _run_executor_subprocess but handles the creds fd lifecycle."""
    workdir = Path(tempfile.mkdtemp(prefix=f"donna-exec-{uuid.uuid4().hex}-"))
    try:
        try:
            proc = subprocess.Popen(
                [binary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_sanitised_env(),
                cwd=str(workdir),
                pass_fds=pass_fds,
                preexec_fn=preexec,
                close_fds=True,
            )
        finally:
            # Parent no longer needs the read end regardless of outcome.
            if r_fd is not None:
                os.close(r_fd)

        # Write creds if present, then close write end.
        if cred_bytes is not None and w_fd is not None:
            try:
                os.write(w_fd, cred_bytes)
            finally:
                os.close(w_fd)
                # Drop the reference; Python can't zero the bytes.
                del cred_bytes
        elif w_fd is not None:
            os.close(w_fd)   # Shouldn't happen but keeps fd hygiene honest.

        try:
            stdout, stderr = proc.communicate(
                input=stdin_payload, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except Exception:
                stdout, stderr = b"", b""
            raise
        return proc.returncode, stdout, stderr
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
```

**Critical failure-path detail:** if `subprocess.Popen` itself raises (before returning `proc`), we must close *both* `r_fd` and `w_fd` in the outer caller. The `try/finally` around Popen in the helper closes `r_fd`; but `w_fd` is still live. The outer `_execute_subprocess` Exception handler needs to close `w_fd` too. Add to the `except Exception as e:` block:

```python
    except Exception as e:
        # If Popen raised before we could close w_fd, do it now.
        if w_fd is not None:
            try:
                os.close(w_fd)
            except OSError:
                pass   # already closed by helper
        return _finalise_failure(...)
```

Walk through the fd lifecycle carefully when reading this — it's the highest-risk bit. The test in Step 1 (`test_spawn_failure_cleans_up_pipe`) asserts both fds are closed.

- [ ] **Step 4: Run the tests, verify pass**

Run: `cd broker && pytest tests/test_executor.py -v -k creds`
Expected: 6 passes.

Run: `cd broker && pytest tests/test_executor.py -v`
Expected: all pass (existing + new).

- [ ] **Step 5: Run mypy + lint-imports + coverage**

Run: `cd broker && mypy broker tests`
Expected: clean.

Run: `cd broker && lint-imports`
Expected: clean (contract update is in Task 6 — if lint fails here, defer to that task).

Run: `cd broker && pytest tests/test_executor.py --cov=broker.executor --cov-report=term-missing`
Expected: ≥95%.

- [ ] **Step 6: Commit**

```bash
git add broker/executor.py broker/tests/test_executor.py
git commit -m "broker: inject creds via fd 3 pipe in _execute_subprocess"
```

---

## Task 6: Architectural annotations — `.importlinter`, `broker/CLAUDE.md`, module docstrings

**Files:**
- Modify: `.importlinter` (location is broker root — grep to confirm)
- Modify: `broker/CLAUDE.md`
- Modify: `broker/executor.py` (module docstring)
- Modify: `CLAUDE.md` (root, one-line addition)

### Steps

- [ ] **Step 1: Find and read existing contracts**

Run: `grep -l '^\[importlinter\]\|^\[contract\]\|name.*contract' -r .`
Expected: finds `broker/.importlinter` or similar. Read it.

- [ ] **Step 2: Add `forbidden` contract for `subprocess` imports**

Append to the `.importlinter` config:

```ini
[importlinter:contract:subprocess-boundary]
name = Only resolver/executor/creds may import subprocess
type = forbidden
source_modules =
    broker
forbidden_modules =
    subprocess
ignore_imports =
    broker.resolver -> subprocess
    broker.executor -> subprocess
    broker.creds -> subprocess
```

(Exact syntax varies by import-linter version — check the repo's existing contracts for the TOML-vs-INI convention and the exact field names. The intent is: `subprocess` is forbidden broker-wide *except* for the three named modules.)

- [ ] **Step 3: Update `broker/CLAUDE.md` import-rules section**

Read `broker/CLAUDE.md` § Import rules (around line 23 per prior inspection). Replace the existing block with:

```markdown
## Import rules

`import-linter` enforces these. See `.importlinter`.

- `policy.py` must not import any network-client module. Policy is pure
  local logic (§9.1).
- `canonicalize.py`, `requests_db.py`, `audit.py` same rule — local,
  deterministic, no outside I/O beyond the DB / audit file.
- `resolver.py` spawns subprocesses per §9.2.
- `executor.py` spawns subprocesses for capability-bound executors (§8).
- `creds.py` spawns a single subprocess (`age --decrypt`) per §17 (Phase 2 vault).
  Pure function boundary: inputs → plaintext bytes → audit event.
  No other module imports subprocess; the `subprocess-boundary` contract
  in `.importlinter` enforces this.
```

- [ ] **Step 4: Update `broker/executor.py` module docstring**

At the top of `broker/executor.py`, append to the existing module docstring (before the `"""` closer) a new paragraph:

```
Fd-3 invariant (§3 of design doc):
  Dispatch may receive inherited fd 3 for creds-declared capabilities
  (§17). All other `pass_fds` entries are a bug — see
  `test_executor.py::test_fd_invariant_across_dispatches`.
```

- [ ] **Step 5: Update root `CLAUDE.md` — Security & Broker § rule 4**

Read `CLAUDE.md` (root) — find the numbered list under "### Active now (Phase 1 live)", specifically rule 4.

Append one sentence at the end of rule 4:

```
Credentials reach capability subprocesses only via fd 3, a one-shot pipe opened by the broker. Donna doesn't open it, read it, or know which capabilities use it. The broker handles it inside `execute`.
```

- [ ] **Step 6: Run tests + lint-imports**

Run: `cd broker && lint-imports`
Expected: clean. If failures surface, they identify bad imports to fix — they should be zero on a clean codebase.

Run: `cd broker && pytest -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add .importlinter broker/CLAUDE.md broker/executor.py CLAUDE.md
git commit -m "broker: enforce subprocess-boundary contract + annotate fd-3 invariant"
```

---

## Task 7: Synthetic test-only manifest + binary + end-to-end test

**Files:**
- Create: `broker/manifests/capabilities.test.yaml`
- Create: `broker/manifests/schemas/synthetic_echo_creds.json`
- Create: `tools/synthetic_echo_creds` (mode 0755)
- Create: `broker/tests/test_synthetic_e2e.py`

### Steps

- [ ] **Step 1: Create the test-only manifest**

`broker/manifests/capabilities.test.yaml`:

```yaml
capabilities:
  - name: synthetic.echo_creds
    executor:
      type: subprocess
      binary: tools/synthetic_echo_creds   # relative to repo root in tests
      timeout_seconds: 10
    param_schema:
      $ref: ./schemas/synthetic_echo_creds.json
    risk_level: medium
    revalidate:
      not_applicable: no_external_state
    idempotency_date_from: created_at
    approval_window_minutes: 5
    execution_window_minutes: 5
    creds:
      delivery: fd3
      entry: synthetic
```

- [ ] **Step 2: Create the param schema**

`broker/manifests/schemas/synthetic_echo_creds.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "additionalProperties": false,
  "properties": {}
}
```

Parameterless capability — the schema just enforces "empty object."

- [ ] **Step 3: Create the executor binary**

`tools/synthetic_echo_creds` (mode 0755):

```python
#!/usr/bin/env python3
"""Synthetic creds-consuming executor.

Reads fd 3 to EOF, prints {"sha256": "<hex>"} to stdout, exits 0.
Used by broker/tests/test_synthetic_e2e.py to prove the full
real-age + real-pipe path works end-to-end. Never invoked in
production — lives under a test-only manifest.
"""
from __future__ import annotations
import hashlib
import json
import os
import sys


def main() -> int:
    buf = b""
    while True:
        chunk = os.read(3, 65536)
        if not chunk:
            break
        buf += chunk
    sys.stdout.write(json.dumps({"sha256": hashlib.sha256(buf).hexdigest()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Make it executable:

```bash
chmod 0755 tools/synthetic_echo_creds
```

- [ ] **Step 4: Write the end-to-end test**

`broker/tests/test_synthetic_e2e.py`:

```python
"""Synthetic real-age + real-pipe end-to-end.

The only test in the suite that exercises the full unlock + fd-3 +
dup2 path with a real age identity and real ciphertext. Auto-skips
when `age` isn't on PATH.

Spec: design §9.4.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from broker import executor, validator
from broker import requests_db as db


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_BINARY = REPO_ROOT / "tools" / "synthetic_echo_creds"
TEST_MANIFEST = REPO_ROOT / "broker" / "manifests" / "capabilities.test.yaml"


pytestmark = pytest.mark.skipif(
    shutil.which("age") is None,
    reason="age binary not on PATH — skipping real-age end-to-end",
)


@pytest.fixture
def synthetic_vault(tmp_path):
    """Build a real age identity + encrypt a known plaintext as
    `synthetic.age` under a tmp creds_dir."""
    # 1. Generate identity.
    identity_path = tmp_path / "identity.age"
    subprocess.run(
        ["age-keygen", "-o", str(identity_path)],
        check=True, capture_output=True,
    )
    # age-keygen writes the public key to stderr in a comment; extract it.
    pubkey: str | None = None
    for line in identity_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key:"):
            pubkey = line.split(":", 1)[1].strip()
            break
    assert pubkey is not None, "could not extract public key from identity"

    # 2. Encrypt the known plaintext.
    plaintext = b"synthetic-payload-2026-04-21"
    ciphertext_path = tmp_path / "synthetic.age"
    subprocess.run(
        ["age", "--encrypt", "-r", pubkey, "-o", str(ciphertext_path)],
        input=plaintext,
        check=True, capture_output=True,
    )
    # 3. Lock down permissions.
    identity_path.chmod(0o400)
    ciphertext_path.chmod(0o440)
    return tmp_path, plaintext


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(str(tmp_path / "requests.db"))
    yield c
    c.close()


def test_synthetic_e2e_real_age_real_pipe(synthetic_vault, conn):
    vault_dir, plaintext = synthetic_vault
    caps = validator.load_capabilities(str(TEST_MANIFEST))
    cap = caps["synthetic.echo_creds"]

    # Patch the binary path to the absolute repo-root one; the manifest
    # declares it relative.
    from dataclasses import replace
    cap = replace(cap, executor_target=str(SYNTHETIC_BINARY))

    # Seed a pre-approved row directly — we're testing execute, not approve.
    request = db.Request(
        request_id="rsyn1",
        capability="synthetic.echo_creds",
        params_json="{}",
        params_hash="a" * 64,
        idempotency_key="ik-rsyn1",
        resolved_summary="synthetic",
        context_reason=None,
        risk_level="medium",
        state="pending_approval",
        approval_code="CSYN01",
        approval_hmac=None,
        created_at=1_000_000,
        approval_expires_at=2_000_000,
        execution_expires_at=None,
        approved_at=None,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    db.insert_request(conn, request)
    db.transition(
        conn, "rsyn1", "pending_approval", "approved",
        execution_expires_at=5_000_000,
        approved_at=1_500_000,
        approval_hmac="c" * 64,
    )
    request = db.get_request(conn, "rsyn1")

    cfg = executor.CredsConfig(
        creds_dir=vault_dir,
        identity_path=vault_dir / "identity.age",
    )

    outcome = executor.execute(cap, request, {}, conn, creds_config=cfg)
    assert outcome.state == "succeeded", outcome.error_message
    assert outcome.result is not None
    assert outcome.result["sha256"] == hashlib.sha256(plaintext).hexdigest()
```

- [ ] **Step 5: Run the test**

Run: `cd broker && pytest tests/test_synthetic_e2e.py -v`
Expected on a machine with age: 1 pass. Expected without age: 1 skip.

- [ ] **Step 6: Run the full suite**

Run: `cd broker && pytest -v`
Expected: all pass (with the one skip if age is missing).

Run: `cd broker && mypy broker tests`
Expected: clean.

Run: `cd broker && lint-imports`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add broker/manifests/capabilities.test.yaml \
        broker/manifests/schemas/synthetic_echo_creds.json \
        tools/synthetic_echo_creds \
        broker/tests/test_synthetic_e2e.py
git commit -m "broker: add synthetic fd-3 end-to-end test (auto-skips without age)"
```

---

## Task 8: Living doc in Notion (broker-approved)

**Files:**
- Modify: `CLAUDE.md` (root, add living-doc URL)

This task uses the broker request flow; no code changes except a URL added to CLAUDE.md at the end.

### Steps

- [ ] **Step 1: Prepare the living-doc content**

Content follows the shape Donna described during the design session (see the Notion parent page `Donna's Desk`, ID `32d4dc8b-b6d8-81ea-9167-c8705113df16`). Structure:

- Purpose + "when to update" (one paragraph).
- Current status table (Phase 1 live / Piece A B shipped / Piece C landed on commit of this plan / D E queued).
- Key invariants (6 items — cred-never-in-context, PATH-only env, pass_fds=() default, broker-refuses-to-start-on-manifest-error, every-failure-is-audited, Playwright-blocked).
- Architectural components (one-line per module).
- Decision log: Piece C fd-3 delivery + sibling audit hardening + 10-check startup sweep. Each entry has decision, why, alternatives considered.
- Open questions (future pieces).

Draft the markdown locally and keep it under ~800 lines.

- [ ] **Step 2: Request Notion write via broker**

The `notion-create-pages` MCP tool is medium-risk → hook blocks direct use → route via broker. Broker request shape:

```json
{
  "capability": "notion.create_pages",
  "params": {
    "parent": {"type": "page_id", "page_id": "32d4dc8bb6d881ea9167c8705113df16"},
    "pages": [{
      "properties": {"title": "Donna Security — Living Doc"},
      "icon": "🔐",
      "content": "<the markdown from Step 1>"
    }]
  },
  "context_reason": "Creating Piece C living doc under Donna's Desk for ongoing security decision log"
}
```

Run:

```bash
sudo -u donna-broker /usr/local/bin/donna-broker request '<json>'
```

Expected: `{"status": "approval_required", "code": "<6char>", ...}`. Graham receives Telegram prompt.

- [ ] **Step 3: Execute on approval**

After Graham approves:

```bash
sudo -u donna-broker /usr/local/bin/donna-broker execute '{"approval_code": "<code>"}'
```

Then re-attempt the `notion-create-pages` MCP call (broker row is in `executing`, hook permits).

- [ ] **Step 4: Capture the page URL**

Extract the URL from the MCP response.

- [ ] **Step 5: Update root `CLAUDE.md`**

Under the "Security & Broker" section, add:

```markdown
**Living doc.** Ongoing security architectural decisions are tracked at [Donna Security — Living Doc](<URL>). Update it on any load-bearing decision worth remembering next session.
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: link Donna Security Living Doc in CLAUDE.md"
```

---

## Self-review (executed by plan author at write time)

1. **Spec coverage:** Every spec section is represented.
   - §3 delivery mechanism → Task 5.
   - §4 manifest schema → Task 1.
   - §5 config plumbing → Task 2.
   - §6 vault health → Task 3.
   - §7 audit hardening → Task 4.
   - §8 error taxonomy → surfaces across Tasks 2, 4, 5.
   - §9 tests → embedded in every code task + Task 7.
   - §10 annotations → Task 6.
   - §11 out of scope → respected (Piece D/E not in plan).
   - §12 sequencing → matches task order.

2. **Placeholder scan:** No "TBD"/"TODO" in steps. A handful of places reference existing patterns by grep (e.g., "the existing `manifest_dir` fixture", "see how `_handle_verify_manifests`'s stdout is plumbed") — those are concrete pointers, not placeholders.

3. **Type consistency:**
   - `CredsBlock` → defined in `broker.validator` (Task 1), used via Protocol `CredsBlockLike` in `broker.executor` (Task 2+).
   - `CredsConfig` → defined in `broker.executor` (Task 2), used in `broker.main` (Task 3) and tests.
   - `unlock_creds` first arg is called `capability` in the existing `creds.py` signature. Task 5's call site passes `capability.creds.entry` as that arg. The name mismatch is documented as a future cleanup candidate in the spec; no rename is in scope for Piece C.
   - `sanitise_context_reason` (tuple return) is referenced accurately in Task 4.

4. **Scope:** Piece C is a single-unit implementation plan. No decomposition needed.
