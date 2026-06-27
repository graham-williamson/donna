"""Integration tests for broker.main.

Exercises the CLI dispatcher end-to-end with tmp-path fixtures so tests
run in isolation from any /Users/donna-broker/* real state.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import time
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
  - name: gmail.create_draft
    executor:
      type: mcp_tool
      tool: mcp__claude_ai_Gmail__create_draft
    param_schema:
      type: object
      required: [to, subject, body]
      additionalProperties: false
      properties:
        to:
          type: array
          items: {{type: string}}
          minItems: 1
        subject: {{type: string}}
        body: {{type: string}}
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: medium
    revalidate:
      not_applicable: stateless_write
    idempotency_date_from: created_utc
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
        {"tool_name": "mcp__claude_ai_Gmail__create_draft", "params": {"to": ["x@y"]}},
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


# ---- in-app approval (inapp-approval-broker-handoff, Option B) ----------


def _request_pending(
    broker_env: dict[str, str], date: str = "2026-04-21"
) -> dict[str, Any]:
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": date},
        },
        broker_env,
    )
    return req_resp


def test_app_approve_records_proof_then_execute_runs(broker_env):
    """The full app flow: execute → approval_required → app-approve →
    execute → succeeded. The in-app tap stands in for the Telegram tap."""
    req = _request_pending(broker_env)

    # execute before any proof-of-human → approval_required.
    _, pre = _run("execute", {"approval_code": req["code"]}, broker_env)
    assert pre["status"] == "approval_required"

    # In-app human tap records the proof-of-human.
    code, resp = _run("app-approve", {"approval_code": req["code"]}, broker_env)
    assert code == 0, resp
    assert resp["status"] == "approved"
    assert resp["state"] == "approved"

    # Receipt is written with channel="app".
    receipt = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req['request_id']}.json"
    assert receipt.exists()
    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert body["decision"] == "approve"
    assert body["channel"] == "app"

    # Audit trail carries the distinct in-app event.
    audit_text = "".join(
        p.read_text(encoding="utf-8")
        for p in Path(broker_env["DONNA_BROKER_AUDIT_DIR"]).glob("*")
        if p.is_file()
    )
    assert "request_approved_in_app" in audit_text

    # Now execute runs the capability for real.
    code, resp = _run("execute", {"approval_code": req["code"]}, broker_env)
    assert code == 0, resp
    assert resp["status"] == "succeeded"
    assert resp["result"] == {"confirmation": "PG-12345"}


def test_app_approve_requires_code(broker_env):
    code, resp = _run("app-approve", {}, broker_env)
    assert code == 1
    assert resp["error_code"] == "invalid_input"


def test_app_approve_unknown_code(broker_env):
    code, resp = _run("app-approve", {"approval_code": "NOSUCH"}, broker_env)
    assert code == 1
    assert resp["error_code"] == "not_found"


def test_app_approve_is_idempotent_on_already_approved(broker_env):
    req = _request_pending(broker_env)
    _run("app-approve", {"approval_code": req["code"]}, broker_env)
    # Second tap, now post-execute the row would be terminal, so approve
    # again while still pre-execute: state stays approvable and returns ok.
    code, resp = _run("app-approve", {"approval_code": req["code"]}, broker_env)
    assert code == 0, resp
    assert resp["status"] == "approved"


def test_app_approve_does_not_overwrite_existing_deny(broker_env):
    """An app tap must not flip a decision already recorded out-of-band."""
    req = _request_pending(broker_env)
    receipt = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req['request_id']}.json"
    receipt.write_text(
        json.dumps({"request_id": req["request_id"], "decision": "deny"}),
        encoding="utf-8",
    )
    code, resp = _run("app-approve", {"approval_code": req["code"]}, broker_env)
    assert code == 0, resp
    assert resp["status"] == "already_recorded"
    assert resp["decision"] == "deny"
    # Receipt is unchanged — still a deny.
    assert json.loads(receipt.read_text(encoding="utf-8"))["decision"] == "deny"


def test_app_approve_refuses_expired_window(broker_env):
    """A pending row past its approval window is refused. approval_expires_at
    is immutable once written, so we insert a fresh already-expired pending
    row and call the handler directly — that path bypasses main()'s lazy
    reconcile, isolating the handler's own belt-and-braces window check."""
    from broker import requests_db as db

    config = main._config_from_env(broker_env)
    ctx = main._build_ctx(config, need_manifests=False)
    now_ms = int(time.time() * 1000)
    row = db.Request(
        request_id="req-expired0001",
        capability="puregym.book_class",
        params_json="{}",
        params_hash="x" * 64,
        idempotency_key="idem-expired",
        resolved_summary="expired test",
        context_reason=None,
        risk_level="medium",
        state="pending_approval",
        approval_code="EXPIRE",
        approval_hmac="y" * 64,
        created_at=now_ms - 10_000,
        approval_expires_at=now_ms - 1_000,
        execution_expires_at=None,
        approved_at=None,
        executed_at=None,
        result_json=None,
        error_code=None,
        error_message=None,
        prev_audit_hash=None,
    )
    db.insert_request(ctx["conn"], row)

    with pytest.raises(main.BrokerError) as exc:
        main._handle_app_approve({"approval_code": "EXPIRE"}, ctx)
    assert exc.value.error_code == "expired"
    # No receipt written for a refused approval.
    receipt = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / "req-expired0001.json"
    assert not receipt.exists()


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


def _make_executing_gmail(env: dict[str, str]) -> dict[str, Any]:
    """Create a gmail.create_draft request and force it into executing."""
    _, req = _run(
        "request",
        {
            "capability": "gmail.create_draft",
            "params": {
                "to": ["test@example.com"],
                "subject": "hello",
                "body": "world",
            },
        },
        env,
    )
    future = int(time.time() * 1000) + 60 * 60 * 1000
    _force_state(
        env["DONNA_BROKER_DB"], req["request_id"],
        "executing",
        approved_at=int(time.time() * 1000),
        execution_expires_at=future,
    )
    return req


def test_audit_result_closes_executing_row_via_tool_name_fallback(broker_env):
    """When request_id is absent, audit-result resolves the row by tool_name."""
    req = _make_executing_gmail(broker_env)
    code, resp = _run(
        "audit-result",
        {
            "tool_name": "mcp__claude_ai_Gmail__create_draft",
            "outcome": "succeeded",
            # no request_id — mirrors real PostToolUse hook behaviour
        },
        broker_env,
    )
    assert code == 0
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "succeeded"


def test_audit_result_fails_executing_row_via_tool_name_fallback(broker_env):
    """Failure outcome via tool_name fallback transitions row to failed."""
    req = _make_executing_gmail(broker_env)
    code, _ = _run(
        "audit-result",
        {
            "tool_name": "mcp__claude_ai_Gmail__create_draft",
            "outcome": "failed",
        },
        broker_env,
    )
    assert code == 0
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "failed"
    assert st["error_code"] == "mcp_tool_reported_failure"


def test_audit_result_explicit_request_id_still_works(broker_env):
    """Explicit request_id path still closes the row correctly."""
    req = _make_executing_gmail(broker_env)
    code, _ = _run(
        "audit-result",
        {
            "tool_name": "mcp__claude_ai_Gmail__create_draft",
            "outcome": "succeeded",
            "request_id": req["request_id"],
        },
        broker_env,
    )
    assert code == 0
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "succeeded"


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
    assert resp["capabilities_count"] == 2
    assert resp["capabilities"] == ["gmail.create_draft", "puregym.book_class"]
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


# ---- lazy reconcile + in-flight visibility + idempotent execute --------
#
# These cover the four compounding broker bugs observed in prod 2026-04-22:
#   - No sweep → rows stranded in non-terminal states forever.
#   - get_by_approval_code + list-pending excluded 'executing' →
#     stranded rows invisible.
#   - cancel refused 'executing' → no manual rescue.
# Fix is six changes: lazy sweep, broaden two queries, idempotent execute
# from executing, allow cancel from executing (adds one transition pair).


def _force_state(
    db_path: str, request_id: str, state: str, **fields: Any
) -> None:
    """Bypass triggers to force a row into an arbitrary state+field combo.
    Test-only helper — production code goes through db.transition()."""
    conn = sqlite3.connect(db_path)
    for f in ("approval_expires_at", "execution_expires_at", "approved_at"):
        conn.execute(f"DROP TRIGGER IF EXISTS trg_immutable_{f}")
        conn.execute(f"DROP TRIGGER IF EXISTS trg_set_once_{f}")
    assignments = ["state = ?"] + [f"{k} = ?" for k in fields]
    params = [state] + list(fields.values()) + [request_id]
    conn.execute(
        f"UPDATE requests SET {', '.join(assignments)} WHERE request_id = ?",
        params,
    )
    conn.commit()
    conn.close()


def _make_pending(env: dict[str, str], date: str = "2026-04-21") -> dict[str, Any]:
    _, resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": date},
        },
        env,
    )
    return resp


def test_lazy_reconcile_expires_pending_approval(broker_env):
    req = _make_pending(broker_env)
    past = int(time.time() * 1000) - 60_000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "pending_approval", approval_expires_at=past,
    )
    # Any CLI call triggers the sweep.
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "expired"


def test_lazy_reconcile_expires_approved_past_exec_window(broker_env):
    req = _make_pending(broker_env)
    past = int(time.time() * 1000) - 60_000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "approved",
        approved_at=past,
        execution_expires_at=past,
    )
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "expired"


def test_lazy_reconcile_fails_executing_past_exec_window(broker_env):
    req = _make_pending(broker_env)
    past = int(time.time() * 1000) - 60_000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "executing",
        approved_at=past,
        execution_expires_at=past,
    )
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "failed"
    assert st["error_code"] == "exec_window_expired"


def test_lazy_reconcile_emits_audit_events(broker_env):
    req = _make_pending(broker_env)
    past = int(time.time() * 1000) - 60_000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "pending_approval", approval_expires_at=past,
    )
    _run("status", {"request_id": req["request_id"]}, broker_env)
    log = Path(broker_env["DONNA_BROKER_AUDIT_DIR"]) / "audit.log"
    assert "request_expired" in log.read_text(encoding="utf-8")


def test_lazy_reconcile_leaves_unexpired_rows_alone(broker_env):
    req = _make_pending(broker_env)
    # Default approval window is 60min; row is well within window.
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "pending_approval"


def test_lazy_reconcile_deletes_queue_file_on_expiry(broker_env):
    """Queue file must be deleted when pending_approval expires, so a daemon
    restart doesn't replay the prompt for a dead request."""
    req = _make_pending(broker_env)
    queue_file = Path(req["queue_file"])
    assert queue_file.exists()
    past = int(time.time() * 1000) - 60_000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "pending_approval", approval_expires_at=past,
    )
    _run("status", {"request_id": req["request_id"]}, broker_env)
    assert not queue_file.exists()


def test_status_by_code_finds_executing_row(broker_env):
    req = _make_pending(broker_env)
    future = int(time.time() * 1000) + 60 * 60 * 1000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "executing",
        approved_at=int(time.time() * 1000),
        execution_expires_at=future,
    )
    code, resp = _run(
        "status-by-code", {"approval_code": req["code"]}, broker_env,
    )
    assert code == 0
    assert resp["state"] == "executing"


def test_list_pending_includes_executing_rows(broker_env):
    req = _make_pending(broker_env)
    future = int(time.time() * 1000) + 60 * 60 * 1000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "executing",
        approved_at=int(time.time() * 1000),
        execution_expires_at=future,
    )
    code, resp = _run("list-pending", {}, broker_env)
    assert code == 0
    states = [r["state"] for r in resp["requests"]]
    assert "executing" in states


def test_cancel_from_executing(broker_env):
    req = _make_pending(broker_env)
    future = int(time.time() * 1000) + 60 * 60 * 1000
    _force_state(
        broker_env["DONNA_BROKER_DB"], req["request_id"],
        "executing",
        approved_at=int(time.time() * 1000),
        execution_expires_at=future,
    )
    code, resp = _run(
        "cancel", {"request_id": req["request_id"]}, broker_env,
    )
    assert code == 0
    assert resp["status"] == "cancelled"
    _, st = _run("status", {"request_id": req["request_id"]}, broker_env)
    assert st["state"] == "cancelled"


def test_cancel_deletes_queue_file(broker_env):
    """Cancel must delete the queue file to prevent prompt replay on daemon restart."""
    req = _make_pending(broker_env)
    queue_file = Path(req["queue_file"])
    assert queue_file.exists()
    code, resp = _run("cancel", {"request_id": req["request_id"]}, broker_env)
    assert code == 0
    assert not queue_file.exists()


# ---- idempotent execute from executing (mcp_tool path) ------------------


def test_execute_deletes_queue_file_on_approve(broker_env):
    """Queue file must be removed after execute reads an approve response.
    Guards against daemon-restart re-sending the approval prompt."""
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    queue_file = Path(req_resp["queue_file"])
    assert queue_file.exists()

    response_file = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req_resp['request_id']}.json"
    response_file.write_text(
        json.dumps({"request_id": req_resp["request_id"], "decision": "approve"}),
        encoding="utf-8",
    )
    _run("execute", {"approval_code": req_resp["code"]}, broker_env)
    assert not queue_file.exists()


def test_execute_deletes_queue_file_on_deny(broker_env):
    """Queue file must also be removed when execute reads a deny response."""
    _, req_resp = _run(
        "request",
        {
            "capability": "puregym.book_class",
            "params": {"class_id": "hiit", "date": "2026-04-21"},
        },
        broker_env,
    )
    queue_file = Path(req_resp["queue_file"])
    assert queue_file.exists()

    response_file = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / f"{req_resp['request_id']}.json"
    response_file.write_text(
        json.dumps({"request_id": req_resp["request_id"], "decision": "deny"}),
        encoding="utf-8",
    )
    _run("execute", {"approval_code": req_resp["code"]}, broker_env)
    assert not queue_file.exists()


def test_execute_reentry_from_executing_returns_handoff(broker_env):
    """Once a row is in `executing` (mcp_tool handoff awaiting Donna's
    re-attempt), calling execute again with the same approval_code must
    succeed and re-emit the handoff metadata — not state-bounce."""
    params = {"to": ["x@y"], "subject": "hi", "body": "test"}
    _, req = _run(
        "request",
        {"capability": "gmail.create_draft", "params": params},
        broker_env,
    )
    rf = Path(broker_env["DONNA_BROKER_RESPONSES_DIR"]) / (
        f"{req['request_id']}.json"
    )
    rf.write_text(
        json.dumps({"request_id": req["request_id"], "decision": "approve"}),
        encoding="utf-8",
    )
    # First execute: pending → approved → executing.
    code1, resp1 = _run(
        "execute", {"approval_code": req["code"]}, broker_env,
    )
    assert code1 == 0, resp1
    assert resp1["status"] == "executing"
    assert resp1["result"]["tool"] == "mcp__claude_ai_Gmail__create_draft"

    # Second execute from executing: idempotent handoff, not an error.
    code2, resp2 = _run(
        "execute", {"approval_code": req["code"]}, broker_env,
    )
    assert code2 == 0, resp2
    assert resp2["status"] == "executing"
    assert resp2["result"]["tool"] == "mcp__claude_ai_Gmail__create_draft"
    assert resp2["result"]["params"] == params

    # Row is still executing — not bounced through another transition.
    _, st = _run(
        "status", {"request_id": req["request_id"]}, broker_env,
    )
    assert st["state"] == "executing"


# ---- standing grants (broker-standing-grants §7) ------------------------


@pytest.fixture
def grants_env(tmp_path, broker_env):
    """Extend broker_env with a high-risk gmail.send capability so grant
    flows have a real capability to grant + auto-execute against."""
    home = Path(broker_env["DONNA_BROKER_HOME"])

    gmail_send_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["to", "subject", "body"],
        "additionalProperties": False,
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "thread_id": {"type": "string"},
        },
    }
    (home / "gmail_send.json").write_text(
        json.dumps(gmail_send_schema), encoding="utf-8"
    )

    capabilities_yaml = home / "capabilities.yaml"
    capabilities_yaml.write_text(f"""
capabilities:
  - name: gmail.create_draft
    executor:
      type: mcp_tool
      tool: mcp__claude_ai_Gmail__create_draft
    param_schema:
      type: object
      required: [to, subject, body]
      additionalProperties: false
      properties:
        to: {{type: array, items: {{type: string}}, minItems: 1}}
        subject: {{type: string}}
        body: {{type: string}}
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: medium
    revalidate:
      not_applicable: stateless_write
    idempotency_date_from: created_utc
    approval_window_minutes: 60
    execution_window_minutes: 30
  - name: gmail.send
    executor:
      type: mcp_tool
      tool: mcp__claude_ai_Gmail__send_message
    param_schema:
      $ref: ./gmail_send.json
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: high
    revalidate:
      not_applicable: stateless_write
    idempotency_date_from: created_utc
    approval_window_minutes: 1440
    execution_window_minutes: 720
""", encoding="utf-8")

    mcp_yaml = home / "mcp-tools.yaml"
    mcp_yaml.write_text("""
tools:
  mcp__claude_ai_Gmail__gmail_search_messages: low
  mcp__claude_ai_Gmail__create_draft: medium
  mcp__claude_ai_Gmail__send_message: high
  mcp__plugin_playwright_playwright__browser_navigate: blocked
""", encoding="utf-8")

    return broker_env


def _approve(env: dict[str, str], request_id: str, decision: str = "approve") -> None:
    rf = Path(env["DONNA_BROKER_RESPONSES_DIR"]) / f"{request_id}.json"
    rf.write_text(
        json.dumps({"request_id": request_id, "decision": decision}),
        encoding="utf-8",
    )


def _create_grant(
    env: dict[str, str],
    *,
    to: str = "graham@example.com",
    max_per_period: int = 1,
    period_seconds: int = 604_800,
    expires_in_days: int = 90,
) -> str:
    """grant-create → approve → execute; returns the grant_id."""
    _, resp = _run(
        "grant-create",
        {
            "capability": "gmail.send",
            "constraints": {"to": to},
            "purpose": "School roundup",
            "max_per_period": max_per_period,
            "period_seconds": period_seconds,
            "expires_in_days": expires_in_days,
        },
        env,
    )
    assert resp["status"] == "approval_required", resp
    _approve(env, resp["request_id"])
    _, fin = _run("execute", {"approval_code": resp["code"]}, env)
    assert fin["status"] == "succeeded", fin
    return fin["grant_id"]


def test_grant_create_returns_approval_required_not_persisted(grants_env):
    """grant-create raises approval_required and does NOT persist a grant."""
    code, resp = _run(
        "grant-create",
        {
            "capability": "gmail.send",
            "constraints": {"to": "graham@example.com"},
            "purpose": "School roundup",
            "max_per_period": 1,
            "period_seconds": 604_800,
            "expires_in_days": 90,
        },
        grants_env,
    )
    assert code == 0, resp
    assert resp["status"] == "approval_required"
    assert len(resp["code"]) == 6
    assert resp["risk_level"] == "high"
    assert resp["is_grant"] is True
    # Full scope spelled out in the human summary.
    assert "graham@example.com" in resp["summary"]
    assert "90d" in resp["summary"]
    assert "School roundup" in resp["summary"]
    # No grant persisted yet.
    _, listing = _run("grant-list", {}, grants_env)
    assert listing["grants"] == []


def test_grant_create_persists_only_on_execute(grants_env):
    grant_id = _create_grant(grants_env)
    _, listing = _run("grant-list", {}, grants_env)
    assert len(listing["grants"]) == 1
    g = listing["grants"][0]
    assert g["grant_id"] == grant_id
    assert g["capability"] == "gmail.send"
    assert g["status"] == "active"
    assert g["constraints"] == {"to": "graham@example.com"}


def test_grant_create_gmail_send_requires_to(grants_env):
    """§5: a gmail.send grant whose constraints omit `to` is rejected."""
    code, resp = _run(
        "grant-create",
        {
            "capability": "gmail.send",
            "constraints": {"subject": {"prefix": "School"}},
            "purpose": "x",
            "max_per_period": 1,
            "period_seconds": 604_800,
        },
        grants_env,
    )
    assert code == 1
    assert resp["error_code"] == "invalid_constraints"


def test_grant_create_rejects_overlong_expiry(grants_env):
    code, resp = _run(
        "grant-create",
        {
            "capability": "gmail.send",
            "constraints": {"to": "g@x.com"},
            "purpose": "x",
            "max_per_period": 1,
            "period_seconds": 604_800,
            "expires_in_days": 400,
        },
        grants_env,
    )
    assert code == 1
    assert resp["error_code"] == "invalid_input"


def test_grant_create_unknown_capability(grants_env):
    code, resp = _run(
        "grant-create",
        {
            "capability": "not.real",
            "constraints": {"to": "g@x.com"},
            "purpose": "x",
            "max_per_period": 1,
            "period_seconds": 604_800,
        },
        grants_env,
    )
    assert code == 1
    assert resp["error_code"] == "unknown_capability"


def test_gmail_send_matching_grant_auto_executes(grants_env):
    """A gmail.send request matching an active grant auto-executes (no
    approval) — returns executing + the mcp_tool handoff + via grant."""
    grant_id = _create_grant(grants_env)
    code, resp = _run(
        "request",
        {
            "capability": "gmail.send",
            "params": {
                "to": "graham@example.com",
                "subject": "School roundup — week 3",
                "body": "the news",
            },
        },
        grants_env,
    )
    assert code == 0, resp
    assert resp["status"] == "executing"
    assert resp["via"] == "standing_grant"
    assert resp["grant_id"] == grant_id
    assert resp["result"]["tool"] == "mcp__claude_ai_Gmail__send_message"


def test_gmail_send_non_matching_to_falls_through_to_approval(grants_env):
    """NON-NEGOTIABLE: a gmail.send whose `to` doesn't match the grant
    falls through to approval_required — never auto-sent."""
    _create_grant(grants_env, to="graham@example.com")
    code, resp = _run(
        "request",
        {
            "capability": "gmail.send",
            "params": {
                "to": "stranger@elsewhere.com",
                "subject": "hi",
                "body": "x",
            },
        },
        grants_env,
    )
    assert code == 0, resp
    assert resp["status"] == "approval_required"
    assert resp.get("via") != "standing_grant"


def test_gmail_send_rate_limit_second_within_window_needs_approval(grants_env):
    """Rate limit: with max_per_period=1, the first send auto-executes,
    the second within the window falls through to approval."""
    _create_grant(grants_env, max_per_period=1)
    p1 = {"to": "graham@example.com", "subject": "roundup 1", "body": "a"}
    code1, r1 = _run("request", {"capability": "gmail.send", "params": p1}, grants_env)
    assert r1["status"] == "executing", r1
    # Different body so idempotency key differs (fresh request).
    p2 = {"to": "graham@example.com", "subject": "roundup 2", "body": "b"}
    code2, r2 = _run("request", {"capability": "gmail.send", "params": p2}, grants_env)
    assert r2["status"] == "approval_required", r2


def test_grant_revoke_always_allowed_and_audited(grants_env):
    grant_id = _create_grant(grants_env)
    code, resp = _run("grant-revoke", {"grant_id": grant_id}, grants_env)
    assert code == 0, resp
    assert resp["status"] == "revoked"
    assert resp["grant_id"] == grant_id
    # Audit recorded.
    log = Path(grants_env["DONNA_BROKER_AUDIT_DIR"]) / "audit.log"
    assert "grant.revoked" in log.read_text(encoding="utf-8")
    # grant-list shows it revoked.
    _, listing = _run("grant-list", {}, grants_env)
    assert listing["grants"][0]["status"] == "revoked"


def test_grant_revoke_no_manifest_needed(grants_env, tmp_path):
    """Revoke must always work — even if the capabilities manifest is
    broken (revocation only reduces privilege, §3.6)."""
    grant_id = _create_grant(grants_env)
    env = dict(grants_env)
    env["DONNA_BROKER_CAPABILITIES"] = str(tmp_path / "nope.yaml")
    code, resp = _run("grant-revoke", {"grant_id": grant_id}, env)
    assert code == 0, resp
    assert resp["status"] == "revoked"


def test_revoked_grant_no_longer_auto_executes(grants_env):
    grant_id = _create_grant(grants_env)
    _run("grant-revoke", {"grant_id": grant_id}, grants_env)
    code, resp = _run(
        "request",
        {
            "capability": "gmail.send",
            "params": {"to": "graham@example.com", "subject": "x", "body": "y"},
        },
        grants_env,
    )
    assert resp["status"] == "approval_required"


def test_grant_revoke_unknown_grant(grants_env):
    code, resp = _run("grant-revoke", {"grant_id": "nope"}, grants_env)
    assert code == 1
    assert resp["error_code"] == "not_found"


def test_grant_create_never_authorised_by_a_grant(grants_env):
    """NON-NEGOTIABLE §3.1: grant.create is never matched by any standing
    grant. There is no manifest capability `grant.create`, and the policy
    layer short-circuits it — so a grant-create always requires the human
    approval code, never an auto-execute."""
    # Even after a grant exists, grant-create still raises approval_required.
    _create_grant(grants_env)
    code, resp = _run(
        "grant-create",
        {
            "capability": "gmail.send",
            "constraints": {"to": "other@example.com"},
            "purpose": "another",
            "max_per_period": 1,
            "period_seconds": 604_800,
        },
        grants_env,
    )
    assert resp["status"] == "approval_required"
    assert resp["is_grant"] is True


def test_grant_create_audits_proposed_and_created(grants_env):
    _create_grant(grants_env)
    log = (Path(grants_env["DONNA_BROKER_AUDIT_DIR"]) / "audit.log").read_text()
    assert "grant.create.proposed" in log
    assert "grant.created" in log


def test_auto_exec_audits_policy_allow_standing_grant(grants_env):
    _create_grant(grants_env)
    _run(
        "request",
        {
            "capability": "gmail.send",
            "params": {"to": "graham@example.com", "subject": "r", "body": "b"},
        },
        grants_env,
    )
    log = (Path(grants_env["DONNA_BROKER_AUDIT_DIR"]) / "audit.log").read_text()
    assert "policy.allow.standing_grant" in log


def test_grant_modes_in_frozen_sets():
    for m in ("grant-create", "grant-list", "grant-revoke"):
        assert m in main.MODES
        assert m in main.MODE_HANDLERS
    assert "grant-create" in main.MODES_NEEDING_MANIFESTS


def test_grant_list_empty(grants_env):
    code, resp = _run("grant-list", {}, grants_env)
    assert code == 0
    assert resp["status"] == "ok"
    assert resp["grants"] == []


def test_grant_create_subject_prefix_in_summary(grants_env):
    code, resp = _run(
        "grant-create",
        {
            "capability": "gmail.send",
            "constraints": {
                "to": "graham@example.com",
                "subject": {"prefix": "School roundup"},
            },
            "purpose": "weekly",
            "max_per_period": 1,
            "period_seconds": 604_800,
        },
        grants_env,
    )
    assert code == 0
    assert "School roundup" in resp["summary"]


# ---- list-packs (read-only pack summary) --------------------------------


def _write_signed_pack(
    pack_dir: Path, pack_id: str, capabilities: list[str], *, sign: bool
) -> None:
    """Write a minimal valid pack (meta.json + manifest.yaml + a schema) and,
    if ``sign`` is set, a detached pack.sig from a freshly generated key."""
    from broker import pack_format
    from broker.tools import sign_pack

    pack_dir.mkdir(parents=True)
    meta = {
        "pack_id": pack_id,
        "version": 1,
        "created_utc": "2026-06-15T00:00:00Z",
        "description": f"Plain English summary for {pack_id}",
        "capabilities": capabilities,
    }
    (pack_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    manifest_lines = ["capabilities:"]
    for name in capabilities:
        manifest_lines.append(f"  - name: {name}")
    (pack_dir / "manifest.yaml").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )
    schemas = pack_dir / "schemas"
    schemas.mkdir()
    (schemas / "params.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )

    if sign:
        priv_path = pack_dir.parent / f"{pack_id}.priv.hex"
        sign_pack.keygen(str(priv_path))
        sign_pack.sign(str(pack_dir), str(priv_path))
        # Confirm pack_format now sees the signature.
        assert pack_format.load_pack(str(pack_dir)).signature is not None


def _list_packs_ctx(capabilities: list[str]) -> dict[str, Any]:
    """Minimal ctx for _handle_list_packs: it only reads ctx['capabilities']
    membership, so name->sentinel is enough (no requests DB)."""
    return {"capabilities": {name: object() for name in capabilities}}


def test_list_packs_registered_everywhere():
    assert "list-packs" in main.MODES
    assert "list-packs" in main.MODE_HANDLERS
    assert "list-packs" in main.MODES_NEEDING_MANIFESTS
    assert "list-packs" in main.MODES_WITH_STARTUP_SWEEP


def test_list_packs_summary_shape_and_hash(tmp_path):
    from broker import pack_format

    packs_dir = tmp_path / "available"
    packs_dir.mkdir()
    pack_dir = packs_dir / "demo-pack"
    _write_signed_pack(pack_dir, "demo-pack", ["demo.thing"], sign=True)

    ctx = _list_packs_ctx([])  # cap absent -> not installed
    resp = main._handle_list_packs({"packs_dir": str(packs_dir)}, ctx)

    assert resp["status"] == "ok"
    assert len(resp["packs"]) == 1
    entry = resp["packs"][0]
    assert entry["pack_id"] == "demo-pack"
    assert entry["description"] == "Plain English summary for demo-pack"
    assert entry["capabilities"] == ["demo.thing"]
    assert entry["signed"] is True
    assert entry["installed"] is False
    assert "error" not in entry

    expected_hash = pack_format.pack_hash(pack_format.load_pack(str(pack_dir)))
    assert entry["pack_hash"] == expected_hash


def test_list_packs_installed_flips_when_cap_present(tmp_path):
    packs_dir = tmp_path / "available"
    packs_dir.mkdir()
    _write_signed_pack(packs_dir / "demo-pack", "demo-pack", ["demo.thing"],
                       sign=True)

    # Cap present in the live manifest -> installed True.
    ctx = _list_packs_ctx(["demo.thing"])
    resp = main._handle_list_packs({"packs_dir": str(packs_dir)}, ctx)
    assert resp["packs"][0]["installed"] is True


def test_list_packs_unsigned_pack(tmp_path):
    packs_dir = tmp_path / "available"
    packs_dir.mkdir()
    _write_signed_pack(packs_dir / "bare", "bare", ["x.y"], sign=False)

    resp = main._handle_list_packs(
        {"packs_dir": str(packs_dir)}, _list_packs_ctx([])
    )
    assert resp["packs"][0]["signed"] is False


def test_list_packs_malformed_pack_does_not_crash(tmp_path):
    packs_dir = tmp_path / "available"
    packs_dir.mkdir()
    _write_signed_pack(packs_dir / "good", "good", ["a.b"], sign=True)
    # Malformed: a dir with no meta.json.
    (packs_dir / "broken").mkdir()
    (packs_dir / "broken" / "manifest.yaml").write_text(
        "capabilities: []\n", encoding="utf-8"
    )

    resp = main._handle_list_packs(
        {"packs_dir": str(packs_dir)}, _list_packs_ctx([])
    )
    by_id = {p["pack_id"]: p for p in resp["packs"]}
    assert by_id["good"]["signed"] is True
    assert "error" in by_id["broken"]
    assert by_id["broken"]["installed"] is False
    assert "error" not in by_id["good"]
    # Sorted by pack_id for determinism.
    assert [p["pack_id"] for p in resp["packs"]] == ["broken", "good"]


def test_list_packs_missing_dir_returns_empty(tmp_path):
    resp = main._handle_list_packs(
        {"packs_dir": str(tmp_path / "nope")}, _list_packs_ctx([])
    )
    assert resp == {"status": "ok", "packs": []}
