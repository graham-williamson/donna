"""Synthetic real-age + real-pipe end-to-end.

The only test in the suite that exercises the full unlock + pipe
delivery + child-read path with a real age identity and real
ciphertext. Auto-skips when `age` isn't on PATH.

Spec: design §9.4.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from broker import executor, validator
from broker import requests_db as db


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_BINARY = REPO_ROOT / "tools" / "synthetic_echo_creds"
TEST_MANIFEST = REPO_ROOT / "broker" / "manifests" / "capabilities.test.yaml"


pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age / age-keygen not on PATH — skipping real-age end-to-end",
)


@pytest.fixture
def synthetic_vault(tmp_path):
    """Build a real age identity + encrypt a known plaintext as
    `synthetic.age` under a tmp creds_dir. Returns (vault_dir, plaintext)."""
    identity_path = tmp_path / "identity.age"
    subprocess.run(
        ["age-keygen", "-o", str(identity_path)],
        check=True, capture_output=True,
    )
    pubkey: str | None = None
    for line in identity_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key:"):
            pubkey = line.split(":", 1)[1].strip()
            break
    assert pubkey is not None, "could not extract public key from identity"

    plaintext = b"synthetic-payload-2026-04-21"
    ciphertext_path = tmp_path / "synthetic.age"
    subprocess.run(
        ["age", "--encrypt", "-r", pubkey, "-o", str(ciphertext_path)],
        input=plaintext,
        check=True, capture_output=True,
    )
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

    # The manifest uses a relative binary path. Rebind to absolute so
    # the test works regardless of Popen cwd.
    cap = replace(cap, executor_target=str(SYNTHETIC_BINARY))

    # Confirm the binary is executable; if not, skip loudly.
    if not SYNTHETIC_BINARY.exists():
        pytest.skip(f"{SYNTHETIC_BINARY} missing — build step deferred")
    import os as _os
    mode = SYNTHETIC_BINARY.stat().st_mode
    if not (mode & 0o111):
        pytest.skip(
            f"{SYNTHETIC_BINARY} is not executable (mode={mode:04o}) — "
            f"chmod 0755 required"
        )

    # Seed a pre-approved row directly — we exercise execute(), not approval.
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
    approved = db.get_request(conn, "rsyn1")
    assert approved is not None

    cfg = executor.CredsConfig(
        creds_dir=vault_dir,
        identity_path=vault_dir / "identity.age",
    )

    outcome = executor.execute(cap, approved, {}, conn, creds_config=cfg)
    assert outcome.state == "succeeded", outcome.error_message
    assert outcome.result is not None
    assert outcome.result["sha256"] == hashlib.sha256(plaintext).hexdigest()
