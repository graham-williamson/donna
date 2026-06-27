"""Tests for the Connected Sites broker modes (store-credential /
site-check) and the per-purchase-only checkout policy.

Contract: docs/connected-sites-broker-handoff.md (daru repo). The app
calls `store-credential` with {site, username, password} and expects
{"status": "stored", site, username} with the password never echoed,
logged, or audited; `site-check` probes a stored login and returns
{"status": "ok"|"login_failed", note}.

age / age-keygen are stubbed with shell scripts; the site probe is a
stub that reads the DONNA_CREDS_FD pipe like a real executor.
"""
from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from broker import main, policy


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def sites_env(tmp_path):
    """Minimal broker env for the manifest-free Connected Sites modes:
    DB, audit dir, a creds vault with identity, and stubbed age binaries."""
    home = tmp_path / "donna-broker"
    (home / "approval-queue").mkdir(parents=True)
    (home / "approval-responses").mkdir(parents=True)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    creds_dir = home / "creds"
    creds_dir.mkdir()
    identity = creds_dir / "identity.age"
    identity.write_bytes(b"AGE-SECRET-KEY-FAKE\n")
    identity.chmod(0o400)

    # age stub: `--decrypt ... <file>` cats the file; `-r ... -o OUT`
    # copies stdin to OUT. Identity behaviour, no real crypto — the
    # round-trip property is what the modes rely on.
    age_stub = tmp_path / "age"
    age_stub.write_text(
        "#!/bin/sh\n"
        'OUT=""; DEC=0; LAST=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --decrypt) DEC=1; shift ;;\n'
        '    -o) OUT="$2"; shift 2 ;;\n'
        '    -i|-r) shift 2 ;;\n'
        '    *) LAST="$1"; shift ;;\n'
        "  esac\n"
        "done\n"
        'if [ "$DEC" = "1" ]; then cat "$LAST"; else cat > "$OUT"; fi\n'
    )
    age_stub.chmod(age_stub.stat().st_mode | stat.S_IXUSR)

    keygen_stub = tmp_path / "age-keygen"
    keygen_stub.write_text("#!/bin/sh\necho age1faketestrecipient\n")
    keygen_stub.chmod(keygen_stub.stat().st_mode | stat.S_IXUSR)

    return {
        "DONNA_BROKER_HOME": str(home),
        "DONNA_BROKER_DB": str(home / "requests.db"),
        "DONNA_BROKER_AUDIT_DIR": str(audit_dir),
        "DONNA_BROKER_QUEUE_DIR": str(home / "approval-queue"),
        "DONNA_BROKER_RESPONSES_DIR": str(home / "approval-responses"),
        "DONNA_CREDS_DIR": str(creds_dir),
        "DONNA_IDENTITY_PATH": str(identity),
        "DONNA_AGE_BINARY": str(age_stub),
        "DONNA_AGE_KEYGEN_BINARY": str(keygen_stub),
    }


@pytest.fixture
def probe_stub(tmp_path):
    """Factory: a probe executable that reads the creds fd (like a real
    executor) and prints a fixed JSON result with a given exit code."""

    def _make(stdout_json: str, exit_code: int = 0) -> str:
        p = tmp_path / f"probe-{abs(hash(stdout_json + str(exit_code)))}.py"
        p.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "json.load(sys.stdin)\n"
            "fd = int(os.environ['DONNA_CREDS_FD'])\n"
            "data = b''\n"
            "while True:\n"
            "    chunk = os.read(fd, 65536)\n"
            "    if not chunk:\n"
            "        break\n"
            "    data += chunk\n"
            "os.close(fd)\n"
            "json.loads(data)  # creds must be the stored JSON blob\n"
            f"sys.stdout.write({stdout_json!r})\n"
            f"sys.exit({exit_code})\n"
        )
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        return str(p)

    return _make


def _run(mode: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    code = main.main(argv=[mode], stdin=stdin, stdout=stdout, env=env)
    out = stdout.getvalue().strip()
    return code, (json.loads(out) if out else {})


def _audit_text(env: dict[str, str]) -> str:
    parts = []
    for f in Path(env["DONNA_BROKER_AUDIT_DIR"]).glob("*"):
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ---- store-credential ----------------------------------------------------


def test_store_credential_writes_vault_entry(sites_env):
    code, resp = _run("store-credential", {
        "site": "everyone_active",
        "username": "graham@example.com",
        "password": "s3cretP@ss",
    }, sites_env)
    assert code == 0
    assert resp["status"] == "stored"
    assert resp["site"] == "everyone_active"
    assert resp["username"] == "graham@example.com"
    assert resp["replaced"] is False

    entry = Path(sites_env["DONNA_CREDS_DIR"]) / "everyone_active.age"
    assert entry.exists()
    assert stat.S_IMODE(entry.stat().st_mode) == 0o400
    # Stub-age "ciphertext" is the plaintext: confirm the executor blob
    # shape (username + email + password keys).
    blob = json.loads(entry.read_text(encoding="utf-8"))
    assert blob == {
        "username": "graham@example.com",
        "email": "graham@example.com",
        "password": "s3cretP@ss",
    }


def test_store_credential_never_echoes_or_audits_password(sites_env):
    code, resp = _run("store-credential", {
        "site": "everyone_active",
        "username": "graham@example.com",
        "password": "hunter2-NEVER-LOGGED",
    }, sites_env)
    assert code == 0
    assert "hunter2-NEVER-LOGGED" not in json.dumps(resp)
    assert "hunter2-NEVER-LOGGED" not in _audit_text(sites_env)
    # The audit DID record the store outcome (no secret material).
    assert "creds_store" in _audit_text(sites_env)


def test_store_credential_replace_is_flagged(sites_env):
    payload = {"site": "everyone_active", "username": "g@x.com", "password": "a"}
    _run("store-credential", payload, sites_env)
    code, resp = _run("store-credential", payload, sites_env)
    assert code == 0
    assert resp["replaced"] is True


@pytest.mark.parametrize("missing", ["site", "username", "password"])
def test_store_credential_requires_all_fields(sites_env, missing):
    payload = {"site": "x_site", "username": "u", "password": "p"}
    del payload[missing]
    code, resp = _run("store-credential", payload, sites_env)
    assert code == 1
    assert resp["error_code"] == "invalid_input"


@pytest.mark.parametrize("bad", ["../escape", "UPPER", "a/b", "", "-lead"])
def test_store_credential_rejects_bad_site_names(sites_env, bad):
    code, resp = _run("store-credential", {
        "site": bad, "username": "u", "password": "p",
    }, sites_env)
    assert code == 1
    assert resp["error_code"] in {"invalid_input", "creds_bad_capability_name"}
    # Nothing landed in the vault.
    vault_files = [
        f.name for f in Path(sites_env["DONNA_CREDS_DIR"]).iterdir()
        if f.name != "identity.age"
    ]
    assert vault_files == []


def test_store_credential_fails_closed_without_identity(sites_env):
    os.unlink(sites_env["DONNA_IDENTITY_PATH"])
    code, resp = _run("store-credential", {
        "site": "everyone_active", "username": "u", "password": "p",
    }, sites_env)
    assert code == 1
    assert resp["error_code"] == "creds_identity_missing"


# ---- site-check ----------------------------------------------------------


def test_site_check_unknown_site_is_no_checker(sites_env):
    code, resp = _run("site-check", {"site": "unknown_site"}, sites_env)
    assert code == 0
    assert resp["status"] == "no_checker"


def test_site_check_without_stored_creds_is_not_connected(
    sites_env, probe_stub, monkeypatch
):
    monkeypatch.setattr(
        main, "SITE_PROBES", {"everyone_active": probe_stub('{"status": "ok"}')}
    )
    code, resp = _run("site-check", {"site": "everyone_active"}, sites_env)
    assert code == 0
    assert resp["status"] == "not_connected"


def test_site_check_ok_roundtrip(sites_env, probe_stub, monkeypatch):
    _run("store-credential", {
        "site": "everyone_active", "username": "g@x.com", "password": "p",
    }, sites_env)
    monkeypatch.setattr(
        main, "SITE_PROBES",
        {"everyone_active": probe_stub('{"status": "ok", "note": "logged in"}')},
    )
    code, resp = _run("site-check", {"site": "everyone_active"}, sites_env)
    assert code == 0
    assert resp["status"] == "ok"
    assert resp["note"] == "logged in"


def test_site_check_login_failed_surfaces_note(sites_env, probe_stub, monkeypatch):
    _run("store-credential", {
        "site": "everyone_active", "username": "g@x.com", "password": "wrong",
    }, sites_env)
    monkeypatch.setattr(
        main, "SITE_PROBES",
        {"everyone_active": probe_stub(
            '{"status": "login_failed", "note": "bad password"}'
        )},
    )
    code, resp = _run("site-check", {"site": "everyone_active"}, sites_env)
    assert code == 0
    assert resp["status"] == "login_failed"
    assert resp["note"] == "bad password"


def test_site_check_probe_crash_is_structured_error(
    sites_env, probe_stub, monkeypatch
):
    _run("store-credential", {
        "site": "everyone_active", "username": "g@x.com", "password": "p",
    }, sites_env)
    monkeypatch.setattr(
        main, "SITE_PROBES",
        {"everyone_active": probe_stub(
            '{"error_code": "site_unavailable", "detail": "EA is down"}',
            exit_code=1,
        )},
    )
    code, resp = _run("site-check", {"site": "everyone_active"}, sites_env)
    assert code == 0
    assert resp["status"] == "error"
    assert resp["error_code"] == "site_unavailable"
    assert "EA is down" in resp["note"]


def test_site_check_requires_site(sites_env):
    code, resp = _run("site-check", {}, sites_env)
    assert code == 1
    assert resp["error_code"] == "invalid_input"


# ---- wrapper parity -------------------------------------------------------


def test_new_modes_are_in_MODES_and_wrapper():
    assert "store-credential" in main.MODES
    assert "site-check" in main.MODES
    wrapper = Path(__file__).resolve().parents[2] / "ops" / "donna-broker.sh"
    text = wrapper.read_text(encoding="utf-8")
    assert "store-credential" in text
    assert "site-check" in text


# ---- per-purchase-only checkout policy -------------------------------------


def test_checkout_is_per_action_only_in_policy():
    assert "everyone_active.checkout" in policy.NO_STANDING_GRANTS


def test_grant_constraints_refused_for_checkout():
    with pytest.raises(policy.GrantConstraintError):
        policy.validate_constraints(
            "everyone_active.checkout", {"centre": "chesham"}
        )


def test_standing_grants_never_match_checkout(tmp_path):
    """Even a grant row smuggled into the store must be inert for
    checkout: check_standing_grants short-circuits before reading it."""
    import sqlite3
    import time as _time
    from broker import grants_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    grants_db.ensure_grant_tables(conn)
    key = b"K" * 32
    now_ms = int(_time.time() * 1000)
    constraints = {"centre": "chesham"}
    grant = grants_db.StandingGrant(
        id="grant-smuggled",
        capability="everyone_active.checkout",
        constraints=json.dumps(constraints, sort_keys=True),
        constraints_mac=policy.compute_constraints_mac(
            key, "everyone_active.checkout", constraints
        ),
        purpose="should never match",
        max_per_period=100,
        period_seconds=86400,
        created_at=now_ms,
        expires_at=now_ms + 86400 * 1000,
        approved_via="XXXXXX",
        revoked_at=None,
    )
    grants_db.insert_grant(conn, grant)

    decision = policy.check_standing_grants(
        conn, "everyone_active.checkout",
        {"centre": "chesham", "activity_name": "Swim", "date": "2026-06-12",
         "max_price": 1000},
        now_ms, key,
    )
    assert decision is None


# ---- manifest: checkout capability ------------------------------------------


def test_real_manifest_has_capped_checkout():
    """The repo manifest carries everyone_active.checkout: high risk and a
    request-time max_price ceiling of 5000 pence (£50)."""
    from broker import validator

    manifest = Path(__file__).resolve().parents[1] / "manifests" / "capabilities.yaml"
    caps = validator.load_capabilities(str(manifest))
    cap = caps["everyone_active.checkout"]
    assert cap.risk_level == "high"
    assert cap.executor_type == "subprocess"
    assert cap.creds is not None and cap.creds.entry == "everyone_active"
    price = cap.param_schema["properties"]["max_price"]
    assert price["maximum"] == 5000
    assert "max_price" in cap.param_schema["required"]

    # And the schema actually rejects an over-cap request.
    with pytest.raises(validator.ParamValidationError):
        validator.validate_params(cap, {
            "activity_name": "Swimming Sessions", "centre": "chesham",
            "date": "2026-06-12", "max_price": 5001,
        })
    validator.validate_params(cap, {
        "activity_name": "Swimming Sessions", "centre": "chesham",
        "date": "2026-06-12", "max_price": 4999,
    })


# ---- manifest: promoter.install_pack capability (Plan B Task 6) -------------


def test_real_manifest_has_promoter_install_pack():
    """The repo manifest carries promoter.install_pack: high risk, subprocess
    executor, and it is un-grantable (policy.NO_STANDING_GRANTS)."""
    from broker import validator

    manifest = Path(__file__).resolve().parents[1] / "manifests" / "capabilities.yaml"
    caps = validator.load_capabilities(str(manifest))
    assert "promoter.install_pack" in caps
    cap = caps["promoter.install_pack"]
    assert cap.risk_level == "high"
    assert cap.executor_type == "subprocess"
    assert "promoter.install_pack" in policy.NO_STANDING_GRANTS
    # Schema binds the pack identity the approval is keyed on.
    assert set(cap.param_schema["required"]) == {"pack_id", "pack_hash"}

    # The schema rejects a bad pack_hash and accepts a well-formed pair.
    with pytest.raises(validator.ParamValidationError):
        validator.validate_params(cap, {"pack_id": "site", "pack_hash": "nope"})
    validator.validate_params(cap, {"pack_id": "site", "pack_hash": "a" * 64})
