from __future__ import annotations

import json

from broker import browser_ledger as bl


def test_records_append_in_order(tmp_path):
    path = tmp_path / "run.ledger.jsonl"
    led = bl.Ledger(path, run_id="R1", now=lambda: 5.0)
    led.record(step=1, snapshot_hash="h1", action={"kind": "read"},
               gate_decision="allow", outcome="ok")
    led.record(step=2, snapshot_hash="h2", action={"kind": "click", "ref": "r5"},
               gate_decision="needs_approval", outcome="paused")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    assert r0["run_id"] == "R1" and r0["step"] == 1 and r0["gate_decision"] == "allow"
    assert r0["ts"] == 5.0


def test_secrets_are_never_written(tmp_path):
    path = tmp_path / "run.ledger.jsonl"
    led = bl.Ledger(path, run_id="R1", now=lambda: 1.0)
    led.record(step=1, snapshot_hash="h", action={"kind": "type", "ref": "r1",
               "text": "{{cred:password}}"}, gate_decision="allow", outcome="ok")
    body = path.read_text()
    assert "{{cred:" not in body and "password" not in body
    assert "<redacted-credential>" in body


def test_nested_placeholder_scrubbed(tmp_path):
    path = tmp_path / "run.ledger.jsonl"
    led = bl.Ledger(path, run_id="R1", now=lambda: 1.0)
    led.record(step=1, snapshot_hash="h",
               action={"kind": "type", "fields": [{"text": "{{cred:username}}"}]},
               gate_decision="allow", outcome="ok")
    body = path.read_text()
    assert "{{cred:" not in body
    assert "<redacted-credential>" in body


def test_run_header_written_once(tmp_path):
    path = tmp_path / "run.ledger.jsonl"
    led = bl.Ledger(path, run_id="R1", now=lambda: 1.0)
    led.run_header(site="everyone_active", goal="book a court", caps={"max_actions": 40})
    head = json.loads(path.read_text().strip().splitlines()[0])
    assert head["type"] == "run" and head["site"] == "everyone_active"


def test_optional_fields_present(tmp_path):
    path = tmp_path / "run.ledger.jsonl"
    led = bl.Ledger(path, run_id="R1", now=lambda: 1.0)
    led.record(step=1, snapshot_hash="h", action={"kind": "click"},
               gate_decision="allow", outcome="ok",
               approval_id="A1", commit_token="tok123", network_events=[{"method": "POST"}])
    r = json.loads(path.read_text().strip().splitlines()[0])
    assert r["approval_id"] == "A1" and r["commit_token"] == "tok123"
    assert r["network_events"] == [{"method": "POST"}]
