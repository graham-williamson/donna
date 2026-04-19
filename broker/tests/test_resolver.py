"""Tests for broker.resolver.

Spec: security-v1.1 §9 (purity + subprocess isolation), §12.5
(provenance), §7.7 (attacker-tainted output tagging).
"""
from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from broker import resolver


# ---- policy_check_mode: purity ------------------------------------------


def test_module_importable():
    for name in (
        "policy_check_mode",
        "request_mode",
        "SECRET_ENV_VARS",
        "DEFAULT_TIMEOUT_SECONDS",
    ):
        assert hasattr(resolver, name), name


def test_policy_check_mode_returns_expected_shape():
    out = resolver.policy_check_mode(
        "puregym.book_class", {"class_id": "hiit", "date": "2026-04-21"}
    )
    assert "fields" in out
    assert "resolved_summary" in out
    labels = [f["label"] for f in out["fields"]]
    assert "capability" in labels
    assert "params" in labels
    # Every field is provenance-tagged broker (pure path).
    assert all(f["provenance"] == "broker" for f in out["fields"])


def test_policy_check_mode_is_deterministic():
    a = resolver.policy_check_mode("cap", {"x": 1, "y": 2})
    b = resolver.policy_check_mode("cap", {"y": 2, "x": 1})
    assert a == b


def test_policy_check_mode_rejects_wrong_types():
    with pytest.raises(TypeError):
        resolver.policy_check_mode(42, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolver.policy_check_mode("cap", "not a dict")  # type: ignore[arg-type]


def test_policy_check_mode_does_not_spawn_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("policy_check_mode must not spawn subprocesses")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    resolver.policy_check_mode("cap", {"a": 1})


def test_policy_check_mode_empty_params():
    out = resolver.policy_check_mode("cap", {})
    params_field = next(f for f in out["fields"] if f["label"] == "params")
    assert params_field["value"] == "<none>"


# ---- request_mode: no resolver declared ---------------------------------


def test_request_mode_without_resolver_matches_policy_check(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("resolver-less path must not spawn")
    monkeypatch.setattr(subprocess, "Popen", boom)
    a = resolver.policy_check_mode("cap", {"x": 1})
    b = resolver.request_mode("cap", {"x": 1})
    assert a == b


# ---- request_mode: subprocess helpers ----------------------------------


def _write_resolver_script(
    tmp_path: Path, script_body: str, name: str = "resolver"
) -> str:
    path = tmp_path / name
    path.write_text(
        "#!/usr/bin/env python3\n" + script_body, encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def test_request_mode_happy_path(tmp_path):
    script = _write_resolver_script(tmp_path, """
import json, sys
data = json.load(sys.stdin)
print(json.dumps({
    "spaces_remaining": 4,
    "instructor": "Sam",
    "resolved_summary": "4 spaces left, Sam instructing"
}))
""")
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "puregym.book_class",
        {"class_id": "hiit"},
        audit_writer=events.append,
        resolver_binary=script,
    )
    assert out["resolved_summary"] == "4 spaces left, Sam instructing"
    by_label = {f["label"]: f for f in out["fields"]}
    # spaces_remaining is an int → broker provenance.
    assert by_label["spaces_remaining"]["provenance"] == "broker"
    assert by_label["spaces_remaining"]["value"] == "4"
    # instructor is a string → donna provenance (attacker-tainted).
    assert by_label["instructor"]["provenance"] == "donna"
    assert by_label["instructor"]["value"] == "Sam"
    # No enrichment_failed on a clean run.
    assert not any(
        e.get("event", "").endswith("enrichment_failed") for e in events
    )


def test_request_mode_missing_binary(tmp_path):
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap",
        {"x": 1},
        audit_writer=events.append,
        resolver_binary=str(tmp_path / "does-not-exist"),
    )
    assert "(enrichment:" in out["resolved_summary"]
    assert any(
        e["event"] == "audit.enrichment_failed"
        and e["reason"] == "resolver_binary_missing"
        for e in events
    )


def test_request_mode_non_zero_exit(tmp_path):
    script = _write_resolver_script(tmp_path, """
import sys
print("problem", file=sys.stderr)
sys.exit(1)
""")
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    assert "(enrichment:" in out["resolved_summary"]
    assert any(e.get("reason") == "non_zero_exit" for e in events)


def test_request_mode_invalid_json_output(tmp_path):
    script = _write_resolver_script(tmp_path, """
print("not valid json at all")
""")
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    assert "(enrichment:" in out["resolved_summary"]
    assert any(e.get("reason") == "invalid_output" for e in events)


def test_request_mode_non_dict_json(tmp_path):
    script = _write_resolver_script(tmp_path, """
print('[1, 2, 3]')
""")
    events: list[dict[str, Any]] = []
    resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    assert any(e.get("reason") == "invalid_output" for e in events)


def test_request_mode_timeout(tmp_path):
    script = _write_resolver_script(tmp_path, """
import time
time.sleep(5)
""")
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap",
        {},
        audit_writer=events.append,
        resolver_binary=script,
        timeout_seconds=0.3,
    )
    assert "(enrichment: timeout)" in out["resolved_summary"]
    assert any(e.get("reason") == "timeout" for e in events)


def test_request_mode_schema_validation_failure(tmp_path):
    script = _write_resolver_script(tmp_path, """
import json
print(json.dumps({"unexpected_field": "value"}))
""")
    schema = {
        "type": "object",
        "required": ["spaces_remaining"],
        "properties": {"spaces_remaining": {"type": "integer"}},
    }
    events: list[dict[str, Any]] = []
    resolver.request_mode(
        "cap", {}, audit_writer=events.append,
        resolver_binary=script, output_schema=schema,
    )
    assert any(e.get("reason") == "invalid_output" for e in events)


def test_request_mode_schema_validation_success(tmp_path):
    script = _write_resolver_script(tmp_path, """
import json
print(json.dumps({"spaces_remaining": 3}))
""")
    schema = {
        "type": "object",
        "required": ["spaces_remaining"],
        "properties": {"spaces_remaining": {"type": "integer"}},
    }
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap", {}, audit_writer=events.append,
        resolver_binary=script, output_schema=schema,
    )
    assert not any(
        e.get("event", "").endswith("enrichment_failed") for e in events
    )
    by_label = {f["label"]: f["value"] for f in out["fields"]}
    assert by_label["spaces_remaining"] == "3"


# ---- §9.2 isolation guarantees ------------------------------------------


def test_sanitised_env_only_contains_path():
    env = resolver._sanitised_env()
    assert list(env.keys()) == ["PATH"]
    assert env["PATH"] == "/usr/bin:/bin"


def test_sanitised_env_excludes_hmac_key(monkeypatch):
    monkeypatch.setenv("HMAC_KEY", "deadbeef")
    env = resolver._sanitised_env()
    assert "HMAC_KEY" not in env


def test_sanitised_env_excludes_broker_db_path(monkeypatch):
    monkeypatch.setenv("BROKER_DB_PATH", "/var/tmp/broker.db")
    env = resolver._sanitised_env()
    assert "BROKER_DB_PATH" not in env


@pytest.mark.parametrize("var", sorted(resolver.SECRET_ENV_VARS))
def test_sanitised_env_excludes_every_secret_var(monkeypatch, var):
    monkeypatch.setenv(var, "secret-value")
    env = resolver._sanitised_env()
    assert var not in env, f"secret var {var} leaked into resolver env"


def test_env_actually_sanitised_at_spawn(monkeypatch, tmp_path):
    """Spawn a resolver that echoes its env — secrets must not appear."""
    monkeypatch.setenv("HMAC_KEY", "super-secret-key-abcdef")
    monkeypatch.setenv("BROKER_DB_PATH", "/var/private/requests.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:tokenvalue")
    script = _write_resolver_script(tmp_path, """
import json, os
print(json.dumps({"env_keys": sorted(os.environ.keys())}))
""")
    out = resolver.request_mode("cap", {}, resolver_binary=script)
    by_label = {f["label"]: f["value"] for f in out["fields"]}
    env_keys_repr = by_label["env_keys"]
    assert "HMAC_KEY" not in env_keys_repr
    assert "BROKER_DB_PATH" not in env_keys_repr
    assert "TELEGRAM_BOT_TOKEN" not in env_keys_repr


def test_cwd_is_ephemeral_tmp(tmp_path):
    script = _write_resolver_script(tmp_path, """
import json, os
print(json.dumps({"cwd": os.getcwd()}))
""")
    out = resolver.request_mode("cap", {}, resolver_binary=script)
    cwd = next(f["value"] for f in out["fields"] if f["label"] == "cwd")
    assert "donna-resolver-" in cwd
    # The ephemeral dir is cleaned up on exit.
    assert not Path(cwd).exists()


def test_stderr_over_4kb_truncated(tmp_path):
    script = _write_resolver_script(tmp_path, """
import sys
sys.stderr.write("X" * (8 * 1024))
print('{}')
""")
    events: list[dict[str, Any]] = []
    resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    stderr_event = next(e for e in events if e.get("event") == "resolver_stderr")
    assert stderr_event["stderr_bytes"] == 8 * 1024
    assert len(stderr_event["stderr"]) <= resolver.MAX_STDERR_BYTES
    assert "[truncated]" in stderr_event["stderr"]


def test_stderr_under_4kb_not_truncated(tmp_path):
    script = _write_resolver_script(tmp_path, """
import sys
sys.stderr.write("small")
print('{}')
""")
    events: list[dict[str, Any]] = []
    resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    stderr_event = next(e for e in events if e.get("event") == "resolver_stderr")
    assert stderr_event["stderr"] == "small"


def test_stdout_over_64kb_treated_as_invalid(tmp_path):
    script = _write_resolver_script(tmp_path, """
import sys
sys.stdout.write("x" * (70 * 1024))
""")
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    assert any(e.get("reason") == "invalid_output" for e in events)
    assert "(enrichment:" in out["resolved_summary"]


# ---- audit_writer robustness --------------------------------------------


def test_request_mode_survives_audit_writer_exception(tmp_path):
    script = _write_resolver_script(tmp_path, """
print("not json")
""")
    def raising_writer(event):
        raise RuntimeError("audit is on fire")
    out = resolver.request_mode(
        "cap", {}, audit_writer=raising_writer, resolver_binary=script,
    )
    assert out is not None


def test_request_mode_rejects_wrong_types():
    with pytest.raises(TypeError):
        resolver.request_mode(42, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolver.request_mode("cap", "not a dict")  # type: ignore[arg-type]


def test_request_mode_spawn_error_is_non_blocking(tmp_path, monkeypatch):
    """subprocess.Popen raising anything other than TimeoutExpired must
    surface as audit.enrichment_failed + degraded summary, not a crash."""
    script = _write_resolver_script(tmp_path, "print('{}')\n")

    def bad_popen(*args, **kwargs):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(subprocess, "Popen", bad_popen)
    events: list[dict[str, Any]] = []
    out = resolver.request_mode(
        "cap", {}, audit_writer=events.append, resolver_binary=script,
    )
    assert "(enrichment: spawn error)" in out["resolved_summary"]
    assert any(
        e.get("reason") == "spawn_error" and e.get("error_type") == "OSError"
        for e in events
    )
