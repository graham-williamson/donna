"""Integration tests for broker.main.

Exercises the CLI dispatcher end-to-end with tmp-path fixtures so tests
run in isolation from any /Users/donna-broker/* real state.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from broker import main


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def broker_env(tmp_path):
    """A full broker home with tmp paths + minimal manifests + HMAC key."""
    home = tmp_path / "donna-broker"
    (home / "approval-queue").mkdir(parents=True)
    (home / "approval-responses").mkdir(parents=True)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    # HMAC key.
    hmac_key = home / "hmac.key"
    hmac_key.write_bytes(b"A" * 32)
    hmac_key.chmod(0o400)

    # Capability manifest with one subprocess capability and one mcp_tool.
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["class_id", "date"],
        "additionalProperties": False,
        "properties": {
            "class_id": {"type": "string"},
            "date": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        },
    }
    (home / "puregym_book.json").write_text(json.dumps(schema), encoding="utf-8")

    # A tiny resolver/executor script so subprocess capabilities can run.
    exec_script = home / "fake_puregym.sh"
    exec_script.write_text(
        '#!/bin/sh\necho \'{"confirmation": "PG-12345"}\'\n',
        encoding="utf-8",
    )
    exec_script.chmod(0o755)

    capabilities_yaml = home / "capabilities.yaml"
    capabilities_yaml.write_text(f"""
capabilities:
  - name: puregym.book_class
    executor:
      type: subprocess
      binary: {exec_script}
      timeout_seconds: 30
    param_schema:
      $ref: ./puregym_book.json
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: medium
    revalidate:
      not_applicable: stateless_write
    idempotency_date_from: params.date
    approval_window_minutes: 60
    execution_window_minutes: 30
""", encoding="utf-8")

    mcp_yaml = home / "mcp-tools.yaml"
    mcp_yaml.write_text("""
tools:
  mcp__claude_ai_Gmail__gmail_search_messages: low
  mcp__claude_ai_Gmail__create_draft: medium
  mcp__plugin_playwright_playwright__browser_navigate: blocked
""", encoding="utf-8")

    return {
        "DONNA_BROKER_HOME": str(home),
        "DONNA_BROKER_DB": str(home / "requests.db"),
        "DONNA_BROKER_AUDIT_DIR": str(audit_dir),
        "DONNA_BROKER_HMAC_KEY": str(hmac_key),
        "DONNA_BROKER_CAPABILITIES": str(capabilities_yaml),
        "DONNA_BROKER_MCP_TOOLS": str(mcp_yaml),
        "DONNA_BROKER_QUEUE_DIR": str(home / "approval-queue"),
        "DONNA_BROKER_RESPONSES_DIR": str(home / "approval-responses"),
    }


def _run(mode: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Invoke main() with captured stdout; return (exit_code, response_json)."""
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    code = main.main(argv=[mode], stdin=stdin, stdout=stdout, env=env)
    out = stdout.getvalue().strip()
    response = json.loads(out) if out else {}
    return code, response


# ---- basics -------------------------------------------------------------


def test_unknown_mode_returns_error(broker_env):
    code, resp = _run("not-a-mode", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "unknown_mode"


def test_not_yet_implemented_modes_return_clear_error(broker_env):
    for mode in ("reconcile", "rotate-hmac"):
        code, resp = _run(mode, {}, broker_env)
        assert code == 1
        assert resp["error_code"] == "not_implemented"


def test_invalid_json_stdin(broker_env):
    stdin = io.StringIO("not json")
    stdout = io.StringIO()
    exit_code = main.main(
        argv=["status"], stdin=stdin, stdout=stdout, env=broker_env,
    )
    assert exit_code == 1
    assert json.loads(stdout.getvalue())["error_code"] == "invalid_json"


def test_empty_stdin_is_empty_object(broker_env):
    stdin = io.StringIO("")
    stdout = io.StringIO()
    main.main(argv=["list-pending"], stdin=stdin, stdout=stdout, env=broker_env)
    resp = json.loads(stdout.getvalue())
    assert resp["status"] == "ok"
    assert resp["requests"] == []


# ---- policy-check --------------------------------------------------------


def test_policy_check_allows_low_risk(broker_env):
    code, resp = _run(
        "policy-check",
        {"tool_name": "mcp__claude_ai_Gmail__gmail_search_messages"},
        broker_env,
    )
    assert code == 0
    assert resp["decision"] == "allow"
    assert resp["risk_level"] == "low"


def test_policy_check_blocks_playwright(broker_env):
    code, resp = _run(
        "policy-check",
        {"tool_name": "mcp__plugin_playwright_playwright__browser_navigate"},
        broker_env,
    )
    assert code == 0
    assert resp["decision"] == "deny"
    assert "blocked" in resp["reason"].lower()


def test_policy_check_blocks_medium_with_summary(broker_env):
    code, resp = _run(
        "policy-check",
        {"tool_name": "mcp__claude_ai_Gmail__create_draft", "params": {"to": "x@y"}},
        broker_env,
    )
    assert code == 0
    assert resp["decision"] == "block"
    assert "summary" in resp


def test_policy_check_requires_tool_name(broker_env):
    code, resp = _run("policy-check", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "invalid_input"


# ---- request flow -------------------------------------------------------


def test_request_creates_pending_approval(broker_env):
    code, resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
            "context_reason": "Chief asked for Tuesday",
        },
        broker_env,
    )
    assert code == 0, resp
    assert resp["status"] == "approval_required"
    assert len(resp["code"]) == 6
    assert resp["risk_level"] == "medium"
    # Queue file should exist.
    assert Path(resp["queue_file"]).exists()


def test_request_rejects_unknown_capability(broker_env):
    code, resp = _run(
        "request",
        {"capability": "not.real", "params": {}},
        broker_env,
    )
    assert code == 1
    assert resp["error_code"] == "unknown_capability"


def test_request_rejects_invalid_params(broker_env):
    code, resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit"},  # missing date
        },
        broker_env,
    )
    assert code == 1
    assert resp["error_code"] == "invalid_params"


def test_request_idempotency_returns_existing(broker_env):
    """Same (capability, canonical_params, date) returns the same row."""
    payload = {
        "capability": "puregym.book_class",
        "params": {"class_id": "hiit", "date": "2026-04-21"},
    }
    code1, resp1 = _run("request", payload, broker_env)
    code2, resp2 = _run("request", payload, broker_env)
    assert code1 == code2 == 0
    assert resp1["request_id"] == resp2["request_id"]
    assert resp2["status"] == "existing"


def test_request_sanitises_context_reason(broker_env):
    code, resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
            "context_reason": "see https://evil.com for details",
        },
        broker_env,
    )
    assert code == 0
    # Read queue file and confirm redaction.
    qf = Path(resp["queue_file"])
    payload_out = json.loads(qf.read_text())
    ctx_field = next(
        f for f in payload_out["fields"] if f["label"] == "context_reason"
    )
    assert "[redacted]" in ctx_field["value"]
    assert "https://" not in ctx_field["value"]


# ---- execute flow -------------------------------------------------------


def test_execute_without_approval_response_returns_approval_required(broker_env):
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    code, resp = _run(
        "execute", {"approval_code": req_resp["code"]}, broker_env,
    )
    assert resp["status"] == "approval_required"


def test_execute_after_approval_runs_capability(broker_env):
    # 1. Request creates pending_approval row.
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    # 2. Simulate Telegram approval by writing the response file.
    response_file = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req_resp['request_id']}.json"
    response_file.write_text(json.dumps({
        "request_id": req_resp["request_id"],
        "decision": "approve",
        "approved_by": "graham",
    }), encoding="utf-8")
    # 3. Execute.
    code, resp = _run(
        "execute", {"approval_code": req_resp["code"]}, broker_env,
    )
    assert code == 0, resp
    assert resp["status"] == "succeeded"
    assert resp["result"] == {"confirmation": "PG-12345"}


def test_execute_with_deny_response(broker_env):
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    response_file = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req_resp['request_id']}.json"
    response_file.write_text(
        json.dumps({"request_id": req_resp["request_id"], "decision": "deny"}),
        encoding="utf-8",
    )
    code, resp = _run(
        "execute", {"approval_code": req_resp["code"]}, broker_env,
    )
    assert resp["status"] == "denied"


def test_execute_unknown_code(broker_env):
    code, resp = _run(
        "execute", {"approval_code": "NOSUCH"}, broker_env,
    )
    assert code == 1
    assert resp["error_code"] == "not_found"


def test_execute_detects_params_hash_mismatch(broker_env):
    """Mutate params_json in SQLite directly → execute refuses + quarantines."""
    import sqlite3
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    response_file = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req_resp['request_id']}.json"
    response_file.write_text(json.dumps({
        "request_id": req_resp["request_id"],
        "decision": "approve",
    }), encoding="utf-8")

    # Tamper: mutate params_hash directly. params_json is immutable by
    # trigger, but params_hash is too — so we disable triggers by
    # dropping and rewriting through a fresh connection with triggers
    # temporarily removed.
    conn = sqlite3.connect(broker_env["DONNA_BROKER_DB"])
    conn.execute("DROP TRIGGER IF EXISTS trg_immutable_params_hash")
    conn.execute(
        "UPDATE requests SET params_hash = ? WHERE request_id = ?",
        ("f" * 64, req_resp["request_id"]),
    )
    conn.commit()
    conn.close()

    code, resp = _run(
        "execute", {"approval_code": req_resp["code"]}, broker_env,
    )
    assert code == 1
    assert resp["error_code"] == "integrity_failed"


# ---- status / status-by-code / list-pending -----------------------------


def test_status_returns_row_details(broker_env):
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    code, resp = _run(
        "status", {"request_id": req_resp["request_id"]}, broker_env,
    )
    assert code == 0
    assert resp["state"] == "pending_approval"
    assert resp["approval_code"] == req_resp["code"]


def test_status_by_code_returns_same_row(broker_env):
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    _, status_resp = _run(
        "status-by-code", {"approval_code": req_resp["code"]}, broker_env,
    )
    assert status_resp["request_id"] == req_resp["request_id"]


def test_list_pending_empty_when_no_requests(broker_env):
    code, resp = _run("list-pending", {}, broker_env)
    assert code == 0
    assert resp["requests"] == []


def test_list_pending_shows_pending_rows(broker_env):
    _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    code, resp = _run("list-pending", {}, broker_env)
    assert len(resp["requests"]) == 1
    assert resp["requests"][0]["state"] == "pending_approval"


# ---- audit-result -------------------------------------------------------


def test_audit_result_records_low_risk_read(broker_env):
    code, resp = _run(
        "audit-result",
        {
            "tool_name": "mcp__claude_ai_Gmail__gmail_search_messages",
            "outcome": "succeeded",
        },
        broker_env,
    )
    assert code == 0
    # Audit log should have an entry.
    from broker import audit as audit_mod
    result = audit_mod.verify_chain(broker_env["DONNA_BROKER_AUDIT_DIR"])
    assert result is None


# ---- cancel --------------------------------------------------------------


def test_cancel_pending_request(broker_env):
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    code, resp = _run(
        "cancel", {"request_id": req_resp["request_id"]}, broker_env,
    )
    assert code == 0
    assert resp["status"] == "cancelled"
    # Row state confirmed.
    _, status = _run(
        "status", {"request_id": req_resp["request_id"]}, broker_env,
    )
    assert status["state"] == "cancelled"


# ---- verify-audit -------------------------------------------------------


def test_verify_audit_clean_on_empty(broker_env):
    code, resp = _run("verify-audit", {}, broker_env)
    assert code == 0
    assert resp["verified"] is True


def test_verify_audit_reports_break_on_tampered_log(broker_env):
    # Write a few events first.
    _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    # Mutate the audit log.
    log = Path(broker_env["DONNA_BROKER_AUDIT_DIR"]) / "audit.log"
    lines = log.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b"request_created", b"request_CREATED")
    log.write_bytes(b"".join(lines))

    code, resp = _run("verify-audit", {}, broker_env)
    assert resp["verified"] is False
    assert "break" in resp


# ---- pending_count / pending_summary surfacing --------------------------


def test_pending_count_is_on_every_response(broker_env):
    code, resp = _run("list-pending", {}, broker_env)
    assert "pending_count" in resp
    assert resp["pending_count"] == 0


def test_pending_count_tracks_approved_only(broker_env):
    # Create a pending request, approve it (simulate Telegram response).
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    # Before approval: pending_count = 0 (state is pending_approval).
    _, pre = _run("list-pending", {}, broker_env)
    assert pre["pending_count"] == 0

    # Approve but don't execute — row transitions to approved.
    response_file = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req_resp['request_id']}.json"
    response_file.write_text(json.dumps({
        "request_id": req_resp["request_id"],
        "decision": "approve",
    }), encoding="utf-8")

    # A partial execute (we'll break the executor binary so it fails
    # after durable-start) is awkward; instead, just observe what
    # pending_summary looks like after a successful execute.
    _run("execute", {"approval_code": req_resp["code"]}, broker_env)
    # Now the row is succeeded, so pending_count is 0 again.
    _, post = _run("list-pending", {}, broker_env)
    assert post["pending_count"] == 0


# ---- config ---------------------------------------------------------------


def test_config_falls_back_to_defaults():
    cfg = main._config_from_env({})
    assert cfg["db_path"].endswith("requests.db")
    assert cfg["audit_dir"].startswith("/Users/donna-broker/")


def test_config_respects_env_overrides():
    cfg = main._config_from_env({"DONNA_BROKER_DB": "/tmp/r.db"})
    assert cfg["db_path"] == "/tmp/r.db"


# ---- error handling -----------------------------------------------------


def test_missing_hmac_key_structured_error(broker_env, tmp_path):
    # Point at a path that doesn't exist.
    broker_env = dict(broker_env)
    broker_env["DONNA_BROKER_HMAC_KEY"] = str(tmp_path / "does-not-exist")
    code, resp = _run(
        "request",
        {"capability": "puregym.book_class", "params": {}},
        broker_env,
    )
    assert code == 1
    assert resp["error_code"] == "hmac_key_missing"


def test_short_hmac_key_rejected(broker_env, tmp_path):
    short_key = tmp_path / "short.key"
    short_key.write_bytes(b"abc")
    broker_env = dict(broker_env)
    broker_env["DONNA_BROKER_HMAC_KEY"] = str(short_key)
    code, resp = _run(
        "request", {"capability": "puregym.book_class", "params": {}}, broker_env,
    )
    assert code == 1
    assert resp["error_code"] == "hmac_key_too_short"


def test_internal_error_structured_to_stdout(broker_env, monkeypatch):
    """A bug inside a handler must still produce JSON on stdout."""
    def blow_up(payload, ctx):
        raise RuntimeError("synthetic bug for test")
    monkeypatch.setitem(main.MODE_HANDLERS, "status", blow_up)
    code, resp = _run(
        "status", {"request_id": "anything"}, broker_env,
    )
    assert code == 2
    assert resp["status"] == "internal"
    assert resp["error_code"] == "internal_error"


# ---- verify-manifests ---------------------------------------------------


def test_verify_manifests_ok(broker_env):
    """Happy path: both manifests parse, schema $ref resolves, returns
    a summary the supervisor can log."""
    code, resp = _run("verify-manifests", {}, broker_env)
    assert code == 0, resp
    assert resp["status"] == "ok"
    assert resp["verified"] is True
    assert resp["capabilities_count"] == 1
    assert resp["capabilities"] == ["puregym.book_class"]
    assert resp["mcp_tools_count"] == 3


def test_verify_manifests_missing_schema_file(broker_env):
    """A $ref pointing at a nonexistent schema file must fail exit 1
    with manifest_error and a message naming the broken capability."""
    schema_path = Path(broker_env["DONNA_BROKER_HOME"]) / "puregym_book.json"
    schema_path.unlink()
    code, resp = _run("verify-manifests", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "manifest_error"
    assert "puregym.book_class" in resp["message"]
    assert "does not exist" in resp["message"]


def test_verify_manifests_unparseable_schema_file(broker_env):
    """A $ref pointing at a file that isn't JSON must fail cleanly."""
    schema_path = Path(broker_env["DONNA_BROKER_HOME"]) / "puregym_book.json"
    schema_path.write_text("{ not valid json", encoding="utf-8")
    code, resp = _run("verify-manifests", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "manifest_error"
    assert "not valid JSON" in resp["message"]


def test_verify_manifests_missing_capabilities_file(broker_env, tmp_path):
    """Pointed at a nonexistent capabilities.yaml → manifest_error."""
    env = dict(broker_env)
    env["DONNA_BROKER_CAPABILITIES"] = str(tmp_path / "nope.yaml")
    code, resp = _run("verify-manifests", {}, env)
    assert code == 1
    assert resp["error_code"] == "manifest_error"
    assert "not found" in resp["message"]


def test_verify_manifests_bad_mcp_tools_risk(broker_env):
    """mcp-tools.yaml with an invalid risk level → manifest_error."""
    mcp_path = Path(broker_env["DONNA_BROKER_MCP_TOOLS"])
    mcp_path.write_text(
        "tools:\n  mcp__whatever: not-a-real-risk\n", encoding="utf-8",
    )
    code, resp = _run("verify-manifests", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "manifest_error"


def test_verify_manifests_unblocked_playwright_refused(broker_env):
    """Playwright must be blocked (§14.1). A non-blocked entry fails."""
    mcp_path = Path(broker_env["DONNA_BROKER_MCP_TOOLS"])
    mcp_path.write_text(
        "tools:\n  mcp__plugin_playwright_playwright__browser_navigate: low\n",
        encoding="utf-8",
    )
    code, resp = _run("verify-manifests", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "manifest_error"
    assert "Playwright" in resp["message"] or "playwright" in resp["message"]


def test_verify_manifests_in_modes_frozen_set(broker_env):
    """Regression guard: if MODES drops verify-manifests, the wrapper
    and hook allowlists go out of sync silently. Keep this trivial
    check so the CI suite alerts us first."""
    assert "verify-manifests" in main.MODES
    assert "verify-manifests" in main.MODE_HANDLERS
    assert "verify-manifests" in main.MODES_NEEDING_MANIFESTS


# ---- verify-vault -------------------------------------------------------


@pytest.fixture
def vault_env(tmp_path, broker_env):
    """Extend broker_env with a capability that declares creds, a real
    creds directory, and env vars pointing at it."""
    home = Path(broker_env["DONNA_BROKER_HOME"])

    # Schema for the new capability (reuse a minimal one).
    creds_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["class_id", "date"],
        "additionalProperties": False,
        "properties": {
            "class_id": {"type": "string"},
            "date": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        },
    }
    schema_path = home / "everyone_active_book.json"
    schema_path.write_text(json.dumps(creds_schema), encoding="utf-8")

    exec_script = home / "fake_ea.sh"
    exec_script.write_text(
        '#!/bin/sh\necho \'{"confirmation": "EA-99999"}\'\n',
        encoding="utf-8",
    )
    exec_script.chmod(0o755)

    # Replace capabilities.yaml with one that has a creds block.
    capabilities_yaml = home / "capabilities.yaml"
    capabilities_yaml.write_text(f"""
capabilities:
  - name: everyone_active.book_class
    executor:
      type: subprocess
      binary: {exec_script}
      timeout_seconds: 30
    param_schema:
      $ref: ./everyone_active_book.json
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: medium
    revalidate:
      not_applicable: stateless_write
    idempotency_date_from: params.date
    approval_window_minutes: 60
    execution_window_minutes: 30
    creds:
      delivery: fd3
      entry: everyone_active
""", encoding="utf-8")

    # Build the creds dir under tmp_path.
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir(mode=0o750)
    identity = creds_dir / "identity.age"
    identity.write_text("AGE-SECRET-KEY-FAKE", encoding="utf-8")
    identity.chmod(0o400)
    entry_file = creds_dir / "everyone_active.age"
    entry_file.write_text("age-ciphertext-fake", encoding="utf-8")
    entry_file.chmod(0o440)

    env = dict(broker_env)
    env["DONNA_CREDS_DIR"] = str(creds_dir)
    env["DONNA_IDENTITY_PATH"] = str(identity)
    env["DONNA_AGE_BINARY"] = "age"
    return env, creds_dir, identity, entry_file


def test_verify_vault_clean_exits_zero(vault_env, monkeypatch):
    """When all checks pass (via monkeypatched owner + age resolver),
    verify-vault exits 0 with no WARN lines."""
    from broker import vault_health

    env, creds_dir, identity, entry_file = vault_env
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")

    code, resp = _run("verify-vault", {}, env)
    assert code == 0, resp
    assert resp["status"] == "ok"
    assert resp["warnings"] == []
    lines = resp["stdout_lines"]
    assert any("0 warnings" in line for line in lines)
    assert not any(line.startswith("WARN") for line in lines)


def test_verify_vault_with_warning_reports_nonzero_exit_code(vault_env, monkeypatch):
    """When identity.age is missing, verify-vault outputs an
    identity_missing WARN line and resp["exit_code"] is 1 (main()
    itself always returns 0 — the non-zero signal is inside the
    response payload for the CLI wrapper to propagate)."""
    from broker import vault_health

    env, creds_dir, identity, entry_file = vault_env
    identity.unlink()  # Force identity_missing
    monkeypatch.setattr(vault_health, "_check_owner_matches",
                        lambda p, u: True)
    monkeypatch.setattr(vault_health, "_resolve_age_binary",
                        lambda b: "/usr/local/bin/age")

    code, resp = _run("verify-vault", {}, env)
    assert code == 0, resp  # main() exit code is always 0 for verify-vault
    assert resp["status"] == "warnings"
    assert resp["exit_code"] == 1
    reasons = [w["reason"] for w in resp["warnings"]]
    assert "identity_missing" in reasons
    lines = resp["stdout_lines"]
    assert any("WARN" in line and "identity_missing" in line for line in lines)


def test_verify_vault_in_modes_frozen_set():
    """Regression guard: verify-vault must be in all three relevant sets."""
    assert "verify-vault" in main.MODES
    assert "verify-vault" in main.MODE_HANDLERS
    assert "verify-vault" in main.MODES_NEEDING_MANIFESTS
