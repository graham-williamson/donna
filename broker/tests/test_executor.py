"""Tests for broker.executor.

Spec: security-v1.1 §8 (binding absolute), §13.4 (revalidation), §11
(durable start), §5 (executing → terminal), §9.2 (subprocess isolation).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from broker import executor
from broker import requests_db as db


# ---- fixtures -----------------------------------------------------------


@dataclass
class FakeCapability:
    name: str
    executor_type: str
    executor_target: str
    revalidate: dict[str, Any]
    creds: Any = None
    requires_session: bool = False


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(str(tmp_path / "requests.db"))
    yield c
    c.close()


def _insert_approved(
    conn, request_id: str, capability_name: str
) -> db.Request:
    r = db.Request(
        request_id=request_id,
        capability=capability_name,
        params_json='{"k":"v"}',
        params_hash="a" * 64,
        idempotency_key=f"ik-{request_id}",
        resolved_summary="test",
        context_reason=None,
        risk_level="medium",
        state="pending_approval",
        approval_code=f"C{request_id[-5:].upper()}",
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
    db.insert_request(conn, r)
    db.transition(
        conn, request_id, "pending_approval", "approved",
        execution_expires_at=5_000_000,
        approved_at=1_500_000,
        approval_hmac="c" * 64,
    )
    return db.get_request(conn, request_id)  # type: ignore[return-value]


def _write_exec_script(tmp_path: Path, body: str, name: str = "exec") -> str:
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


# ---- module surface ------------------------------------------------------


def test_module_importable():
    for name in (
        "execute",
        "ExecutionError",
        "ExecutionOutcome",
        "DEFAULT_EXECUTOR_TIMEOUT_SECONDS",
        "CredsConfig",
        "CredsBlockLike",
    ):
        assert hasattr(executor, name)


# ---- capability binding --------------------------------------------------


def test_capability_mismatch_raises(conn):
    r = _insert_approved(conn, "r1", "capA")
    cap = FakeCapability(name="capB", executor_type="subprocess",
                         executor_target="/bin/true", revalidate={})
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute(cap, r, {}, conn)
    assert exc.value.error_code == "capability_mismatch"


# ---- §13.4 revalidation --------------------------------------------------


def test_revalidation_stale_transitions_to_failed(conn, tmp_path):
    r = _insert_approved(conn, "r2", "cap")
    script = _write_exec_script(tmp_path, "print('{}')")
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={"handler": "stale_check", "arguments": []},
    )
    events: list[dict[str, Any]] = []
    handlers = {"stale_check": lambda name, p, a: (False, "class full")}
    outcome = executor.execute(
        cap, r, {}, conn, audit_writer=events.append,
        revalidate_handlers=handlers,
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "stale"
    fetched = db.get_request(conn, "r2")
    assert fetched is not None and fetched.state == "failed"


def test_revalidation_missing_handler_fails(conn):
    r = _insert_approved(conn, "r-mh", "cap")
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target="/bin/true",
        revalidate={"handler": "nonexistent", "arguments": []},
    )
    events: list[dict[str, Any]] = []
    outcome = executor.execute(
        cap, r, {}, conn, audit_writer=events.append,
        revalidate_handlers={},
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "revalidation_handler_missing"


def test_revalidation_not_applicable_skipped(conn, tmp_path):
    r = _insert_approved(conn, "r-na", "cap")
    script = _write_exec_script(tmp_path, 'print("{}")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={"not_applicable": "stateless_write"},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "succeeded"


def test_revalidation_no_handler_and_no_na_skipped(conn, tmp_path):
    """Low-risk capabilities may omit revalidate entirely."""
    r = _insert_approved(conn, "r-lr", "cap")
    script = _write_exec_script(tmp_path, 'print("{}")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "succeeded"


def test_revalidation_handler_success(conn, tmp_path):
    r = _insert_approved(conn, "r-rh", "cap")
    script = _write_exec_script(
        tmp_path, 'import json; print(json.dumps({"ok": True}))',
    )
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={"handler": "check", "arguments": ["class_id", "date"]},
    )
    seen: list[tuple[str, dict[str, Any], list[str]]] = []

    def check(name, p, args):
        seen.append((name, p, args))
        return True, "ok"

    outcome = executor.execute(
        cap, r, {"class_id": "hiit", "date": "2026-04-21"}, conn,
        revalidate_handlers={"check": check},
    )
    assert outcome.state == "succeeded"
    assert seen == [("cap", {"class_id": "hiit", "date": "2026-04-21"},
                     ["class_id", "date"])]


def test_revalidation_arguments_must_be_list(conn, tmp_path):
    r = _insert_approved(conn, "r-bad-args", "cap")
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target="/bin/true",
        revalidate={"handler": "x", "arguments": "not a list"},
    )
    outcome = executor.execute(
        cap, r, {}, conn,
        revalidate_handlers={"x": lambda *a, **kw: (True, "")},
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "revalidation_handler_bad_arguments"


# ---- §11 durable start ---------------------------------------------------


def test_durable_start_transitions_before_spawn(conn, tmp_path, monkeypatch):
    """Row must be in 'executing' state before the executor runs."""
    r = _insert_approved(conn, "r-ds", "cap")
    observed_state: list[str] = []

    # Wrap Popen so that AT SPAWN TIME we can read the DB state.
    orig_popen = subprocess.Popen

    def observing_popen(*args, **kwargs):
        row = db.get_request(conn, "r-ds")
        observed_state.append(row.state if row else "missing")
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observing_popen)

    script = _write_exec_script(tmp_path, 'print("{}")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    executor.execute(cap, r, {}, conn)
    assert observed_state == ["executing"]


def test_durable_start_emits_started_audit_event(conn, tmp_path):
    r = _insert_approved(conn, "r-aud", "cap")
    script = _write_exec_script(tmp_path, 'print("{}")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    events: list[dict[str, Any]] = []
    executor.execute(cap, r, {}, conn, audit_writer=events.append)
    started = [e for e in events if e["event"] == "request_execution_started"]
    assert len(started) == 1
    assert started[0]["request_id"] == "r-aud"
    assert started[0]["capability"] == "cap"


# ---- subprocess executor -------------------------------------------------


def test_subprocess_happy_path(conn, tmp_path):
    r = _insert_approved(conn, "r-ok", "cap")
    script = _write_exec_script(
        tmp_path,
        'import json, sys; '
        'data = json.load(sys.stdin); '
        'print(json.dumps({"confirmation": "PG-12345"}))',
    )
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    events: list[dict[str, Any]] = []
    outcome = executor.execute(
        cap, r, {"class_id": "hiit"}, conn, audit_writer=events.append,
    )
    assert outcome.state == "succeeded"
    assert outcome.result == {"confirmation": "PG-12345"}
    fetched = db.get_request(conn, "r-ok")
    assert fetched is not None
    assert fetched.state == "succeeded"
    assert fetched.result_json is not None
    assert json.loads(fetched.result_json) == {"confirmation": "PG-12345"}
    assert any(
        e["event"] == "request_execution_succeeded" for e in events
    )


def test_subprocess_non_zero_exit(conn, tmp_path):
    r = _insert_approved(conn, "r-xc", "cap")
    script = _write_exec_script(tmp_path, 'import sys; sys.exit(3)')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    events: list[dict[str, Any]] = []
    outcome = executor.execute(
        cap, r, {}, conn, audit_writer=events.append,
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_crashed"
    fetched = db.get_request(conn, "r-xc")
    assert fetched is not None
    assert fetched.state == "failed"
    assert fetched.error_code == "executor_crashed"
    failed_events = [e for e in events if e.get("reason") == "executor_crashed"]
    assert failed_events and failed_events[0]["exit_code"] == 3


def test_subprocess_timeout(conn, tmp_path):
    r = _insert_approved(conn, "r-to", "cap")
    script = _write_exec_script(tmp_path, "import time; time.sleep(5)")
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(
        cap, r, {}, conn, subprocess_timeout_seconds=0.3,
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_timeout"


def test_subprocess_missing_binary(conn, tmp_path):
    r = _insert_approved(conn, "r-mb", "cap")
    cap = FakeCapability(
        name="cap", executor_type="subprocess",
        executor_target=str(tmp_path / "nope"),
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_missing"


def test_subprocess_invalid_json_output(conn, tmp_path):
    r = _insert_approved(conn, "r-ij", "cap")
    script = _write_exec_script(tmp_path, 'print("not json at all")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_output_invalid_json"


def test_subprocess_non_object_json_output(conn, tmp_path):
    r = _insert_approved(conn, "r-nj", "cap")
    script = _write_exec_script(tmp_path, 'print("[1,2,3]")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_output_not_object"


def test_subprocess_empty_output_ok(conn, tmp_path):
    """Executor prints nothing → treated as empty-result success."""
    r = _insert_approved(conn, "r-empty", "cap")
    script = _write_exec_script(tmp_path, 'pass')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "succeeded"
    assert outcome.result == {}


def test_subprocess_output_too_large(conn, tmp_path):
    r = _insert_approved(conn, "r-big", "cap")
    script = _write_exec_script(
        tmp_path,
        'import sys; sys.stdout.write("x" * (300 * 1024))',
    )
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_output_too_large"


def test_subprocess_stderr_captured(conn, tmp_path):
    r = _insert_approved(conn, "r-se", "cap")
    script = _write_exec_script(
        tmp_path,
        'import sys; sys.stderr.write("diagnostic info\\n"); print("{}")',
    )
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    events: list[dict[str, Any]] = []
    executor.execute(cap, r, {}, conn, audit_writer=events.append)
    stderr_events = [e for e in events if e["event"] == "executor_stderr"]
    assert len(stderr_events) == 1
    ev = stderr_events[0]
    assert ev["stderr_bytes"] == len("diagnostic info\n")
    assert len(ev["stderr_sha256"]) == 64
    # Closed key-set — proves by construction that no body-carrying
    # field (regardless of name or encoding) can appear.
    assert set(ev.keys()) == {"event", "request_id", "stderr_bytes",
                              "stderr_sha256"}
    # Defence in depth: the literal stderr content must not survive
    # serialisation, even if a new field ever sneaks in.
    assert "diagnostic info" not in json.dumps(ev)


def test_subprocess_stderr_over_cap_recorded_by_length_and_hash(conn, tmp_path):
    """Oversized stderr is acknowledged via length + full-body sha256
    only. No head field, no verbatim body, no leak surface."""
    r = _insert_approved(conn, "r-st", "cap")
    script = _write_exec_script(
        tmp_path,
        'import sys; sys.stderr.write("X" * (20 * 1024)); print("{}")',
    )
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    events: list[dict[str, Any]] = []
    executor.execute(cap, r, {}, conn, audit_writer=events.append)
    stderr_event = next(e for e in events if e["event"] == "executor_stderr")
    assert stderr_event["stderr_bytes"] == 20 * 1024
    assert len(stderr_event["stderr_sha256"]) == 64
    # Closed key-set — proves by construction that no body-carrying
    # field (regardless of name or encoding) can appear.
    assert set(stderr_event.keys()) == {"event", "request_id",
                                        "stderr_bytes", "stderr_sha256"}
    # Defence in depth: a chunk of the oversized payload must not
    # survive serialisation.
    assert "XXXXXXXXXX" not in json.dumps(stderr_event)


# ---- §9.2 env sanitisation ----------------------------------------------


def test_subprocess_env_is_sanitised(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("HMAC_KEY", "super-secret")
    monkeypatch.setenv("BROKER_DB_PATH", "/var/private")
    r = _insert_approved(conn, "r-env", "cap")
    script = _write_exec_script(
        tmp_path,
        'import json, os; '
        'print(json.dumps({"env": sorted(os.environ.keys())}))',
    )
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "succeeded"
    assert outcome.result is not None
    env_keys = outcome.result["env"]
    assert "HMAC_KEY" not in env_keys
    assert "BROKER_DB_PATH" not in env_keys


# ---- mcp_tool dispatch ---------------------------------------------------


def test_mcp_tool_returns_metadata_without_terminating(conn):
    r = _insert_approved(conn, "r-mcp", "gmail.create_draft")
    cap = FakeCapability(
        name="gmail.create_draft",
        executor_type="mcp_tool",
        executor_target="mcp__claude_ai_Gmail__create_draft",
        revalidate={"not_applicable": "stateless_write"},
    )
    events: list[dict[str, Any]] = []
    outcome = executor.execute(
        cap, r, {"to": ["x@y"]}, conn, audit_writer=events.append,
    )
    # Row remains executing — the PostToolUse audit-result path will
    # close it out when the MCP tool actually runs.
    assert outcome.state == "executing"
    assert outcome.result is not None
    assert outcome.result["tool"] == "mcp__claude_ai_Gmail__create_draft"
    fetched = db.get_request(conn, "r-mcp")
    assert fetched is not None and fetched.state == "executing"
    assert any(
        e["event"] == "request_execution_mcp_tool_handoff" for e in events
    )


# ---- unknown executor_type ----------------------------------------------


def test_unknown_executor_type_fails_closed(conn):
    r = _insert_approved(conn, "r-ue", "cap")
    cap = FakeCapability(
        name="cap", executor_type="quantum", executor_target="???",
        revalidate={},
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "failed"
    assert outcome.error_code == "unknown_executor_type"


# ---- audit_writer resilience --------------------------------------------


def test_audit_writer_exception_non_blocking(conn, tmp_path):
    r = _insert_approved(conn, "r-ax", "cap")
    script = _write_exec_script(tmp_path, 'print("{}")')
    cap = FakeCapability(
        name="cap", executor_type="subprocess", executor_target=script,
        revalidate={},
    )

    def raising_writer(event):
        raise RuntimeError("audit is on fire")

    outcome = executor.execute(
        cap, r, {}, conn, audit_writer=raising_writer,
    )
    assert outcome.state == "succeeded"


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

    class _FakeCredsBlock:
        def __init__(self, delivery: str, entry: str) -> None:
            self.delivery = delivery
            self.entry = entry

    r = _insert_approved(conn, "r-cfgmissing", "capB")
    cap = FakeCapability(
        name="capB", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "foo"),
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


def test_creds_config_threads_to_execute_subprocess(conn, tmp_path, monkeypatch):
    """Forward-guard: when a creds-declaring capability is dispatched
    with a CredsConfig, the same config object must reach
    _execute_subprocess. Task 5 wires in the real use; this test keeps
    the threading honest in the interim."""

    class _FakeCredsBlock:
        def __init__(self, delivery: str, entry: str) -> None:
            self.delivery = delivery
            self.entry = entry

    captured: dict[str, Any] = {}

    def _spy(capability, request, params, state_conn, audit_writer,
             timeout_seconds, creds_config):
        captured["creds_config"] = creds_config
        return executor.ExecutionOutcome(state="succeeded", result={})

    monkeypatch.setattr(executor, "_execute_subprocess", _spy)

    r = _insert_approved(conn, "r-thread", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_FakeCredsBlock("fd3", "entry"),
    )
    cfg = executor.CredsConfig(
        creds_dir=tmp_path, identity_path=tmp_path / "identity.age",
    )
    executor.execute(cap, r, {}, conn, creds_config=cfg)
    assert captured["creds_config"] is cfg


# ---- §7 audit hardening -------------------------------------------------


def test_stderr_audit_carries_no_verbatim_body(conn, tmp_path):
    """A capability that prints token-like data to stderr must not
    leak it into the executor_stderr audit event. Only length + hash
    are recorded. The original spec draft included a redacted head,
    but the §15 sanitiser was designed for URL/hex/digit patterns,
    not for arbitrary text that might contain literal secrets — so
    any head-based approach carries exfil risk. Hash-only is the
    correct posture; if triage ever needs the body, that's a
    mode-0600 stash file, not an audit field."""
    r = _insert_approved(conn, "r-stderrbody", "capA")
    body = (
        'import sys; sys.stderr.write("secret-token-ABC123\\n"); sys.exit(0)'
    )
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
    serialised = json.dumps(ev)
    assert "secret-token-ABC123" not in serialised
    assert len(ev["stderr_sha256"]) == 64
    # Closed key-set — proves by construction that no body-carrying
    # field (under any name or encoding) can appear.
    assert set(ev.keys()) == {"event", "request_id", "stderr_bytes",
                              "stderr_sha256"}


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


# ---- §3 fd-3 creds injection --------------------------------------------


def _fake_creds_block(entry: str = "entry", delivery: str = "fd3",
                      model_key: bool = False):
    class _FakeCredsBlock:
        def __init__(self, d: str, e: str, mk: bool) -> None:
            self.delivery = d
            self.entry = e
            self.model_key = mk
    return _FakeCredsBlock(delivery, entry, model_key)


def _creds_config(tmp_path):
    return executor.CredsConfig(
        creds_dir=tmp_path, identity_path=tmp_path / "identity.age"
    )


def test_creds_happy_path_child_reads_bytes_from_fd3(conn, tmp_path, monkeypatch):
    r = _insert_approved(conn, "r-creds-ok", "capA")
    # Child script reads fd 3 to EOF and prints the SHA-256 of those bytes.
    body = (
        "import os, sys, json, hashlib\n"
        "fd = int(os.environ['DONNA_CREDS_FD'])\n"
        "data = b''\n"
        "while True:\n"
        "    chunk = os.read(fd, 65536)\n"
        "    if not chunk: break\n"
        "    data += chunk\n"
        "sys.stdout.write(json.dumps({'sha256': hashlib.sha256(data).hexdigest()}))\n"
    )
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="token_entry"),
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
    assert outcome.state == "succeeded", outcome.error_message
    assert outcome.result["sha256"] == hashlib.sha256(expected).hexdigest()


def test_requires_session_refused_without_marker(conn, tmp_path, monkeypatch):
    # A browser/session capability must NOT be spawned outside the launchd
    # session (would SIGTRAP). Refused fail-closed, no spawn, row stays approved
    # so the caller can retry through the trampoline.
    monkeypatch.delenv("DONNA_VIA_SESSION", raising=False)
    r = _insert_approved(conn, "r-sess-no", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, "import sys; sys.stdout.write('{}')\n"),
        revalidate={"not_applicable": "no_external_state"},
        requires_session=True,
    )
    popen_calls: list = []
    monkeypatch.setattr(executor.subprocess, "Popen",
                        lambda *a, **k: popen_calls.append(1))
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.error_code == "session_required"
    assert popen_calls == []                       # never spawned
    assert db.get_request(conn, "r-sess-no").state == "approved"  # retryable


def test_requires_session_runs_with_marker(conn, tmp_path, monkeypatch):
    # Inside the session (trampoline sets DONNA_VIA_SESSION=1) it runs normally.
    monkeypatch.setenv("DONNA_VIA_SESSION", "1")
    r = _insert_approved(conn, "r-sess-yes", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(
            tmp_path, "import sys,json; sys.stdout.write(json.dumps({'ran': True}))\n"),
        revalidate={"not_applicable": "no_external_state"},
        requires_session=True,
    )
    outcome = executor.execute(cap, r, {}, conn)
    assert outcome.state == "succeeded", outcome.error_message
    assert outcome.result["ran"] is True


def test_model_key_delivered_on_second_fd(conn, tmp_path, monkeypatch):
    # creds.model_key=True → executor receives a SECOND inherited fd
    # (DONNA_MODEL_KEY_FD) carrying the broker-level anthropic_api entry,
    # alongside the site credential on DONNA_CREDS_FD.
    r = _insert_approved(conn, "r-mk-ok", "capA")
    body = (
        "import os, sys, json\n"
        "def _read(fd):\n"
        "    data=b''\n"
        "    while True:\n"
        "        c=os.read(fd,65536)\n"
        "        if not c: break\n"
        "        data+=c\n"
        "    return data.decode()\n"
        "cred=_read(int(os.environ['DONNA_CREDS_FD']))\n"
        "mk=_read(int(os.environ['DONNA_MODEL_KEY_FD']))\n"
        "sys.stdout.write(json.dumps({'cred':cred,'mk':mk}))\n"
    )
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="site_entry", model_key=True),
    )

    def fake_unlock(entry, *a, **kw):
        # site credential vs the broker-level model key, keyed by entry name
        return b"SITE-CRED" if entry == "site_entry" else b"sk-ant-KEY"

    monkeypatch.setattr("broker.creds.unlock_creds", fake_unlock)
    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "succeeded", outcome.error_message
    assert outcome.result["cred"] == "SITE-CRED"
    assert outcome.result["mk"] == "sk-ant-KEY"


def test_no_model_key_means_no_second_fd(conn, tmp_path, monkeypatch):
    # Default (model_key=False): DONNA_MODEL_KEY_FD must NOT be in the child env.
    r = _insert_approved(conn, "r-mk-absent", "capA")
    body = (
        "import os, sys, json\n"
        "sys.stdout.write(json.dumps({'has_mk': 'DONNA_MODEL_KEY_FD' in os.environ}))\n"
    )
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="site_entry"),  # model_key defaults False
    )
    monkeypatch.setattr("broker.creds.unlock_creds", lambda *a, **kw: b"SITE")
    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "succeeded", outcome.error_message
    assert outcome.result["has_mk"] is False


def test_model_key_unlock_failure_blocks_spawn(conn, tmp_path, monkeypatch):
    # If the anthropic_api entry can't be unlocked, fail closed — no spawn,
    # and the already-open site-creds pipe is cleaned up.
    from broker import creds as creds_module
    r = _insert_approved(conn, "r-mk-fail", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="site_entry", model_key=True),
    )

    def fake_unlock(entry, *a, **kw):
        if entry == "site_entry":
            return b"SITE-CRED"
        raise creds_module.CredsError("creds_missing", "no anthropic_api entry")

    monkeypatch.setattr("broker.creds.unlock_creds", fake_unlock)

    popen_calls: list = []
    orig_popen = subprocess.Popen
    monkeypatch.setattr(executor.subprocess, "Popen",
                        lambda *a, **kw: popen_calls.append(1) or orig_popen(*a, **kw))

    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "creds_missing"
    assert popen_calls == []


def test_creds_unlock_failure_blocks_spawn(conn, tmp_path, monkeypatch):
    from broker import creds as creds_module
    r = _insert_approved(conn, "r-creds-fail", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="missing_entry"),
    )

    def raiser(*a, **kw):
        raise creds_module.CredsError("creds_missing", "no such entry")

    monkeypatch.setattr("broker.creds.unlock_creds", raiser)

    popen_calls: list = []
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        popen_calls.append((a, kw))
        return orig_popen(*a, **kw)

    monkeypatch.setattr(executor.subprocess, "Popen", spy)

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
        creds=_fake_creds_block(entry="big_entry"),
    )
    monkeypatch.setattr(
        "broker.creds.unlock_creds",
        lambda *a, **kw: b"X" * (16 * 1024 + 1),
    )

    popen_calls: list = []
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        popen_calls.append(None)
        return orig_popen(*a, **kw)

    monkeypatch.setattr(executor.subprocess, "Popen", spy)

    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "creds_too_large"
    assert popen_calls == []


def test_spawn_failure_cleans_up_pipe(conn, tmp_path, monkeypatch):
    """os.pipe() succeeds; Popen raises. Both fds must be closed."""
    import errno
    r = _insert_approved(conn, "r-creds-popenfail", "capA")
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target="/usr/bin/true",
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="entry"),
    )
    monkeypatch.setattr("broker.creds.unlock_creds",
                        lambda *a, **kw: b"short")

    opened_pipes: list[tuple[int, int]] = []
    orig_pipe = os.pipe

    def spy_pipe():
        r_fd, w_fd = orig_pipe()
        opened_pipes.append((r_fd, w_fd))
        return r_fd, w_fd

    monkeypatch.setattr(executor.os, "pipe", spy_pipe)

    def blowup(*a, **kw):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(executor.subprocess, "Popen", blowup)

    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "executor_spawn_error"
    assert len(opened_pipes) == 1

    # Both fds must be closed. os.fstat on a closed fd raises EBADF.
    for fd in opened_pipes[0]:
        with pytest.raises(OSError) as exc:
            os.fstat(fd)
        assert exc.value.errno == errno.EBADF


def test_child_exits_without_reading_fd3_still_handled(conn, tmp_path, monkeypatch):
    """Child exits 0 without consuming fd 3. Exit-status is
    authoritative — row goes to succeeded. Broker does not hang on
    the pipe write."""
    r = _insert_approved(conn, "r-creds-noread", "capA")
    body = 'import sys; sys.stdout.write("{}"); sys.exit(0)'
    cap = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="entry"),
    )
    monkeypatch.setattr("broker.creds.unlock_creds",
                        lambda *a, **kw: b"never-read")
    outcome = executor.execute(
        cap, r, {}, conn, creds_config=_creds_config(tmp_path),
    )
    assert outcome.state == "succeeded"


def test_fd_invariant_across_dispatches(conn, tmp_path, monkeypatch):
    """pass_fds is () for creds-less capabilities and a single-fd tuple
    for creds-declared. Never any other shape."""
    popen_kwargs: list[dict] = []
    orig_popen = subprocess.Popen

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

    # Creds dispatch. Child must read fd 3 to unblock the write — loop
    # until EOF.
    monkeypatch.setattr("broker.creds.unlock_creds", lambda *a, **kw: b"ok")
    r2 = _insert_approved(conn, "r-inv-2", "capA")
    body = (
        "import os, sys\n"
        "fd = int(os.environ['DONNA_CREDS_FD'])\n"
        "while os.read(fd, 65536):\n"
        "    pass\n"
        'sys.stdout.write("{}")\n'
    )
    cap2 = FakeCapability(
        name="capA", executor_type="subprocess",
        executor_target=_write_exec_script(tmp_path, body),
        revalidate={"not_applicable": "no_external_state"},
        creds=_fake_creds_block(entry="entry"),
    )
    executor.execute(cap2, r2, {}, conn, creds_config=_creds_config(tmp_path))

    pass_fds_seen = [kw.get("pass_fds", ()) for kw in popen_kwargs]
    assert pass_fds_seen[0] == ()
    assert len(pass_fds_seen[1]) == 1
    assert isinstance(pass_fds_seen[1][0], int)
