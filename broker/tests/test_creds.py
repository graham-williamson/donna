"""Tests for broker.creds.

Spec: security-v1.1 §17 (Phase 2 age vault), §15 (audit redactions),
§10 (fail-closed).

Coverage aims:
  - Capability-name guard rejects slashes, dots, empty, uppercase.
  - Missing identity / missing ciphertext produce structured errors
    and emit `outcome: "failure"` audit events with stable `reason`
    tokens.
  - Age-binary stubs: happy path returns stdout bytes verbatim; non-
    zero exit raises creds_decrypt_failed; missing binary raises
    creds_binary_missing; a stubbed hang is cut off by timeout.
  - Audit events never carry any §15-forbidden key. Writer-raised
    exceptions are swallowed (audit never blocks the data path).
  - Real-age round trip (skipped when `age` is not installed) confirms
    identity + recipient semantics are wired correctly.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from broker import audit, creds


# ---- helpers / fixtures -------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Synthetic vault layout under tmp_path. Matches §12.1: creds live
    in creds/, identity sits alongside."""
    v = tmp_path / "donna-broker" / ".config" / "donna" / "creds"
    v.mkdir(parents=True)
    return v


@pytest.fixture
def identity(vault: Path) -> Path:
    """A placeholder identity file. Content is irrelevant for stubbed
    age runs — unlock_creds only cares that it exists."""
    p = vault / "identity.age"
    p.write_bytes(b"AGE-SECRET-KEY-FAKE\n")
    p.chmod(0o400)
    return p


@pytest.fixture
def ciphertext(vault: Path) -> Path:
    """A placeholder ciphertext at the capability path. Content is
    irrelevant when the age binary is stubbed."""
    p = vault / "synthetic.test.age"
    p.write_bytes(b"age-ciphertext-fake\n")
    p.chmod(0o400)
    return p


@pytest.fixture
def age_stub(tmp_path: Path) -> Callable[[str], str]:
    """Factory that writes an executable shell script and returns its
    path. Body is arbitrary sh; tests use it to simulate age behaviour
    without pulling in the real binary.
    """
    counter = {"n": 0}

    def _make(body: str) -> str:
        counter["n"] += 1
        p = tmp_path / f"age-stub-{counter['n']}.sh"
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(p)

    return _make


def _capture_events() -> tuple[list[dict[str, Any]], Callable[[dict[str, Any]], None]]:
    """Return (events_list, writer) so tests can assert on emitted
    audit events. Drop-in for the `audit_writer` kwarg."""
    events: list[dict[str, Any]] = []

    def writer(event: dict[str, Any]) -> None:
        events.append(event)

    return events, writer


# ---- module surface ------------------------------------------------------


def test_module_surface():
    assert hasattr(creds, "unlock_creds")
    assert hasattr(creds, "CredsError")
    assert hasattr(creds, "CAPABILITY_NAME_RE")


# ---- capability-name guard ----------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/etc/passwd",
        "../escape",
        "name with spaces",
        "UPPERCASE",
        "trailing-",
        "-leading",
        "a" * 65,
        ".",
        "a/b",
        "..",
    ],
)
def test_unlock_rejects_bad_capability_name(
    bad: str, vault: Path, identity: Path
) -> None:
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(bad, vault, identity)
    assert exc.value.error_code == "creds_bad_capability_name"


@pytest.mark.parametrize("good", ["a", "puregym.book_class", "x-y_z.1", "a1.b2"])
def test_unlock_accepts_well_formed_capability_name(
    good: str, vault: Path, identity: Path, age_stub: Callable[[str], str]
) -> None:
    # Ciphertext doesn't exist for these — we only check that the name
    # passes the shape guard and the code reaches the existence check.
    stub = age_stub("exit 0")
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(good, vault, identity, age_binary=stub)
    assert exc.value.error_code == "creds_missing"


# ---- path existence checks ----------------------------------------------


def test_unlock_raises_on_missing_identity(
    vault: Path, ciphertext: Path, age_stub: Callable[[str], str]
) -> None:
    stub = age_stub("exit 0")
    events, writer = _capture_events()
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(
            "synthetic.test",
            vault,
            vault / "no-such-identity.age",
            age_binary=stub,
            audit_writer=writer,
        )
    assert exc.value.error_code == "creds_identity_missing"
    assert events == [{
        "event": "creds_unlock",
        "capability": "synthetic.test",
        "outcome": "failure",
        "reason": "creds_identity_missing",
    }]


def test_unlock_raises_on_missing_ciphertext(
    vault: Path, identity: Path, age_stub: Callable[[str], str]
) -> None:
    stub = age_stub("exit 0")
    events, writer = _capture_events()
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(
            "synthetic.test",
            vault,
            identity,
            age_binary=stub,
            audit_writer=writer,
        )
    assert exc.value.error_code == "creds_missing"
    assert events[-1]["reason"] == "creds_missing"


# ---- age invocation ------------------------------------------------------


def test_unlock_returns_age_stdout_bytes(
    vault: Path,
    identity: Path,
    ciphertext: Path,
    age_stub: Callable[[str], str],
) -> None:
    # printf avoids the trailing newline echo would add, so the test
    # confirms that what age writes is exactly what the caller gets.
    stub = age_stub("printf 'SECRET-TOKEN-VALUE'")
    events, writer = _capture_events()
    out = creds.unlock_creds(
        "synthetic.test",
        vault,
        identity,
        age_binary=stub,
        audit_writer=writer,
    )
    assert out == b"SECRET-TOKEN-VALUE"
    assert events == [{
        "event": "creds_unlock",
        "capability": "synthetic.test",
        "outcome": "success",
    }]


def test_unlock_returns_exact_bytes_including_nul(
    vault: Path,
    identity: Path,
    ciphertext: Path,
    age_stub: Callable[[str], str],
) -> None:
    # Credentials may be arbitrary bytes. Confirm no mangling — stdout
    # is passed through unchanged to the caller. POSIX printf interprets
    # `\NNN` (octal) in the format string, so this emits three literal
    # NUL bytes interleaved with `a`, `b`, `c`, `d`.
    stub = age_stub(r"""printf 'a\000b\000c\000d'""")
    out = creds.unlock_creds(
        "synthetic.test", vault, identity, age_binary=stub,
    )
    assert out == b"a\x00b\x00c\x00d"


def test_unlock_raises_on_non_zero_exit(
    vault: Path,
    identity: Path,
    ciphertext: Path,
    age_stub: Callable[[str], str],
) -> None:
    stub = age_stub("echo 'no identity match' >&2\nexit 1")
    events, writer = _capture_events()
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(
            "synthetic.test",
            vault,
            identity,
            age_binary=stub,
            audit_writer=writer,
        )
    assert exc.value.error_code == "creds_decrypt_failed"
    assert events[-1]["reason"] == "creds_decrypt_failed"
    # Stderr content from age must not leak into the event.
    assert "no identity match" not in str(events[-1])


def test_unlock_raises_on_timeout(
    vault: Path,
    identity: Path,
    ciphertext: Path,
    age_stub: Callable[[str], str],
) -> None:
    stub = age_stub("sleep 5")
    events, writer = _capture_events()
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(
            "synthetic.test",
            vault,
            identity,
            age_binary=stub,
            timeout_seconds=0.3,
            audit_writer=writer,
        )
    assert exc.value.error_code == "creds_timeout"
    assert events[-1]["reason"] == "creds_timeout"


def test_unlock_raises_on_missing_binary(
    vault: Path, identity: Path, ciphertext: Path
) -> None:
    events, writer = _capture_events()
    with pytest.raises(creds.CredsError) as exc:
        creds.unlock_creds(
            "synthetic.test",
            vault,
            identity,
            age_binary="/no/such/age-binary",
            audit_writer=writer,
        )
    assert exc.value.error_code == "creds_binary_missing"
    assert events[-1]["reason"] == "creds_binary_missing"


# ---- audit hygiene -------------------------------------------------------


def test_audit_events_carry_no_forbidden_keys(
    vault: Path,
    identity: Path,
    ciphertext: Path,
    age_stub: Callable[[str], str],
) -> None:
    """Any event the module emits must survive audit.write_event's
    §15 recursive forbidden-key guard — otherwise a future caller that
    wires audit_writer directly to write_event would blow up."""
    stub = age_stub("printf 'ok'")
    events, writer = _capture_events()
    creds.unlock_creds(
        "synthetic.test",
        vault,
        identity,
        age_binary=stub,
        audit_writer=writer,
    )
    # Success event
    audit._check_forbidden_recursive(events[-1])

    # Failure event
    bad_stub = age_stub("exit 2")
    with pytest.raises(creds.CredsError):
        creds.unlock_creds(
            "synthetic.test",
            vault,
            identity,
            age_binary=bad_stub,
            audit_writer=writer,
        )
    audit._check_forbidden_recursive(events[-1])


def test_audit_writer_exception_is_swallowed(
    vault: Path,
    identity: Path,
    ciphertext: Path,
    age_stub: Callable[[str], str],
) -> None:
    """A broken audit_writer must never block a successful decrypt — §10
    universal rule. The caller still receives the plaintext bytes."""
    stub = age_stub("printf 'PAYLOAD'")

    def boom(_: dict[str, Any]) -> None:
        raise RuntimeError("audit subsystem is on fire")

    out = creds.unlock_creds(
        "synthetic.test",
        vault,
        identity,
        age_binary=stub,
        audit_writer=boom,
    )
    assert out == b"PAYLOAD"


def test_failure_event_reason_in_stable_allowlist(
    vault: Path, identity: Path, age_stub: Callable[[str], str]
) -> None:
    """Callers key on `reason` tokens. Lock down the vocabulary so a
    silent rename downstream shows up here first."""
    stub = age_stub("exit 0")
    events, writer = _capture_events()
    with pytest.raises(creds.CredsError):
        creds.unlock_creds(
            "synthetic.test",
            vault,
            vault / "no-such-identity.age",
            age_binary=stub,
            audit_writer=writer,
        )
    assert events[-1]["reason"] in {
        "creds_identity_missing",
        "creds_missing",
        "creds_decrypt_failed",
        "creds_timeout",
        "creds_binary_missing",
        "creds_spawn_error",
    }


# ---- real-age integration (opt-in) --------------------------------------


_AGE = shutil.which("age")
_AGE_KEYGEN = shutil.which("age-keygen")

real_age = pytest.mark.skipif(
    _AGE is None or _AGE_KEYGEN is None,
    reason="age / age-keygen not installed; run `brew install age`",
)


@real_age
def test_real_age_roundtrip(tmp_path: Path) -> None:
    """Generate an age identity, encrypt a known payload to its public
    recipient, then verify unlock_creds returns the original bytes.

    This is the only test that depends on the binary being present —
    everything else runs via shell stubs.
    """
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    identity = vault_dir / "identity.age"

    # age-keygen writes both the private key and a `# public key:`
    # comment line to the file (or stdout). Capture recipient from it.
    assert _AGE_KEYGEN is not None and _AGE is not None
    keygen = subprocess.run(
        [_AGE_KEYGEN, "-o", str(identity)],
        capture_output=True,
        check=True,
    )
    # age-keygen emits the public recipient on stderr. Parse it.
    recipient = None
    for line in keygen.stderr.decode().splitlines():
        if line.startswith("Public key:"):
            recipient = line.split(":", 1)[1].strip()
            break
    # Fallback: derive recipient from the identity file itself.
    if recipient is None:
        derive = subprocess.run(
            [_AGE_KEYGEN, "-y", str(identity)],
            capture_output=True,
            check=True,
        )
        recipient = derive.stdout.decode().strip()
    assert recipient and recipient.startswith("age1")
    identity.chmod(0o400)

    # Encrypt a known payload to that recipient.
    payload = b"real-secret-\x00-bytes\n"
    ciphertext = vault_dir / "synthetic.test.age"
    enc = subprocess.run(
        [_AGE, "-r", recipient, "-o", str(ciphertext)],
        input=payload,
        capture_output=True,
        check=True,
    )
    assert enc.returncode == 0
    ciphertext.chmod(0o400)

    # Decrypt via the module under test.
    out = creds.unlock_creds(
        "synthetic.test",
        vault_dir,
        identity,
        age_binary=_AGE,
    )
    assert out == payload
