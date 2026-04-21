"""Tests for broker.vault_health.

Spec: Piece C design doc §6 startup vault health checks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from broker import vault_health


@pytest.fixture
def good_vault(tmp_path, monkeypatch):
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o750)
    identity = creds_dir / "identity.age"
    identity.write_text("AGE-SECRET-KEY-FAKE", encoding="utf-8")
    identity.chmod(0o400)
    entry = creds_dir / "everyone_active.age"
    entry.write_text("age-ciphertext-fake", encoding="utf-8")
    entry.chmod(0o440)

    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda path, want_uid: True)
    return creds_dir, identity, [entry]


def test_all_checks_pass_emits_no_warnings(good_vault, monkeypatch):
    creds_dir, identity, _ = good_vault
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")

    captured: list[dict] = []
    result = vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=identity,
        age_binary="age",
        declared_entries=["everyone_active"],
        audit_writer=captured.append,
    )
    assert captured == []
    assert result == []


def test_vault_dir_missing(tmp_path, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
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
    creds_dir.mkdir(mode=0o777)
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
    identity.write_text("x", encoding="utf-8")
    identity.chmod(0o400)
    # Pretend every owner check fails.
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: False)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    # Ensure _get_donna_broker_uid returns an int so owner checks are actually run.
    monkeypatch.setattr(vault_health, "_get_donna_broker_uid",
                        lambda: 12345)
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=[], audit_writer=captured.append,
    )
    reasons = {w["reason"] for w in captured}
    assert "vault_dir_owner_wrong" in reasons
    assert "identity_owner_wrong" in reasons


def test_multiple_failures_all_reported(tmp_path, monkeypatch):
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


def test_entry_owner_wrong(good_vault, monkeypatch):
    creds_dir, identity, _ = good_vault
    # Owner check for entry files returns False (forces the per-entry owner warn).
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: p == creds_dir or p == identity)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    monkeypatch.setattr(vault_health, "_get_donna_broker_uid",
                        lambda: 12345)
    captured: list[dict] = []
    vault_health.sweep(
        creds_dir=creds_dir, identity_path=identity, age_binary="age",
        declared_entries=["everyone_active"], audit_writer=captured.append,
    )
    assert any(w["reason"] == "entry_owner_wrong" for w in captured)


def test_audit_writer_failure_does_not_raise(tmp_path, monkeypatch):
    """§10: audit failure never blocks startup."""
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o777)  # triggers vault_dir_mode_loose
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")

    def exploding_writer(evt: dict) -> None:
        raise RuntimeError("audit backend down")

    # Must not raise even though the writer explodes.
    result = vault_health.sweep(
        creds_dir=creds_dir,
        identity_path=creds_dir / "identity.age",
        age_binary="age",
        declared_entries=[],
        audit_writer=exploding_writer,
    )
    # Warnings are still returned even if audit write fails.
    assert any(w["reason"] == "vault_dir_mode_loose" for w in result)


def test_sweep_returns_warnings_list_not_none(tmp_path, monkeypatch):
    """sweep() always returns a list, never None."""
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")
    result = vault_health.sweep(
        creds_dir=tmp_path / "missing",
        identity_path=tmp_path / "missing" / "identity.age",
        age_binary="age",
        declared_entries=[],
        audit_writer=None,
    )
    assert isinstance(result, list)
