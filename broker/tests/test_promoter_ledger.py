from __future__ import annotations

from pathlib import Path

from broker import promoter_ledger


def test_append_then_read_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    led = promoter_ledger.Ledger(str(p), now=lambda: 1000.0)
    led.record(pack_id="waitrose", pack_hash="abc", key_id="k1",
               approval_id="A1", outcome="installed", reason="")
    rows = promoter_ledger.read_all(str(p))
    assert rows[0]["pack_id"] == "waitrose"
    assert rows[0]["outcome"] == "installed"
    assert rows[0]["ts"] == 1000.0


def test_append_is_additive(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    led = promoter_ledger.Ledger(str(p), now=lambda: 1.0)
    led.record(pack_id="a", pack_hash="h", key_id="k", approval_id="A",
               outcome="refused", reason="bad sig")
    led.record(pack_id="b", pack_hash="h2", key_id="k", approval_id="B",
               outcome="installed", reason="")
    rows = promoter_ledger.read_all(str(p))
    assert [r["pack_id"] for r in rows] == ["a", "b"]


def test_ledger_never_writes_signature_field(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    led = promoter_ledger.Ledger(str(p), now=lambda: 1.0)
    led.record(pack_id="a", pack_hash="h", key_id="k", approval_id="A",
               outcome="installed", reason="")
    raw = p.read_text()
    assert "signature" not in raw and "private" not in raw


# ---- extra tests (required by the task) ---------------------------------


def test_read_all_missing_path_returns_empty(tmp_path: Path) -> None:
    """read_all on a non-existent path returns [] (no crash)."""
    p = tmp_path / "does-not-exist.jsonl"
    assert promoter_ledger.read_all(str(p)) == []


def test_read_all_skips_blank_lines(tmp_path: Path) -> None:
    """Blank / whitespace-only lines are ignored, not parsed."""
    p = tmp_path / "ledger.jsonl"
    led = promoter_ledger.Ledger(str(p), now=lambda: 1.0)
    led.record(pack_id="a", pack_hash="h", key_id="k", approval_id="A",
               outcome="installed", reason="")
    led.record(pack_id="b", pack_hash="h2", key_id="k", approval_id="B",
               outcome="refused", reason="x")
    # inject blank lines (leading, between, trailing) around the real rows
    body = p.read_text().splitlines()
    p.write_text("\n" + body[0] + "\n   \n" + body[1] + "\n\n")
    rows = promoter_ledger.read_all(str(p))
    assert [r["pack_id"] for r in rows] == ["a", "b"]


def test_written_row_has_exactly_the_allowed_fields(tmp_path: Path) -> None:
    """The JSON line contains exactly the seven allowed fields and nothing
    else — the precise 'no secret leakage' guarantee (§9.6)."""
    import json

    p = tmp_path / "ledger.jsonl"
    led = promoter_ledger.Ledger(str(p), now=lambda: 7.0)
    led.record(pack_id="a", pack_hash="h", key_id="k", approval_id="A",
               outcome="installed", reason="")
    parsed = json.loads(p.read_text().strip())
    assert set(parsed.keys()) == {
        "ts", "pack_id", "pack_hash", "key_id",
        "approval_id", "outcome", "reason",
    }
