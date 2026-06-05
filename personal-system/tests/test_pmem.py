import os
import sys
import json
import sqlite3
import subprocess
import importlib.util
import pathlib
from datetime import datetime, timezone, timedelta

PMEM = pathlib.Path(__file__).resolve().parents[1] / "tools" / "pmem.py"


def load_pmem(db_path):
    spec = importlib.util.spec_from_file_location("pmem", PMEM)
    mod = importlib.util.module_from_spec(spec)
    mod.DB_PATH = str(db_path)
    spec.loader.exec_module(mod)
    mod.DB_PATH = str(db_path)
    return mod


def _age_row(pmem, mid, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = pmem.get_db()
    c.execute("UPDATE memories SET last_verified=? WHERE id=?", (old, mid))
    c.commit()


def test_schema_creates_tables(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    conn = pmem.get_db()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memories" in names
    assert "memory_topics" in names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_add_episodic_then_duplicate_rejected(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r1 = pmem.add({"kind": "episodic", "owner": "esme",
                   "content": "Graham landed the demo", "topics": ["wins"]})
    assert r1["status"] == "added"
    r2 = pmem.add({"kind": "episodic", "owner": "esme",
                   "content": "Graham landed the demo", "topics": ["wins"]})
    assert r2["status"] == "duplicate" and r2["id"] == r1["id"]


def test_add_observation_increments_recurrence(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    a = pmem.add({"kind": "observation", "owner": "nike",
                  "content": "low energy after late night", "topics": ["energy"]})
    b = pmem.add({"kind": "observation", "owner": "nike",
                  "content": "low energy after late night", "topics": ["energy"]})
    assert a["id"] == b["id"]
    conn = pmem.get_db()
    row = conn.execute("SELECT recurrence_count FROM memories WHERE id=?",
                       (a["id"],)).fetchone()
    assert row["recurrence_count"] == 2


def test_add_writes_topics(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "episodic", "owner": "shared",
                  "content": "fact", "topics": ["a", "b"]})
    conn = pmem.get_db()
    topics = {x[0] for x in conn.execute(
        "SELECT topic FROM memory_topics WHERE memory_id=?", (r["id"],))}
    assert topics == {"a", "b"}


def test_recall_by_topic_and_persona_visibility(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    pmem.add({"kind": "episodic", "owner": "esme", "shared": 0,
              "content": "esme-only note", "topics": ["worry"]})
    pmem.add({"kind": "episodic", "owner": "shared", "shared": 1,
              "content": "shared note", "topics": ["worry"]})
    got = [r["content"] for r in pmem.recall(topic="worry", persona="bodhi")]
    assert "shared note" in got
    assert "esme-only note" not in got
    got_esme = [r["content"] for r in pmem.recall(topic="worry", persona="esme")]
    assert "esme-only note" in got_esme and "shared note" in got_esme


def test_recall_excludes_stale_by_default(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "semantic", "owner": "shared",
                  "content": "old wisdom", "topics": ["t"], "confidence": "high"})
    conn = pmem.get_db()
    conn.execute("UPDATE memories SET status='stale' WHERE id=?", (r["id"],))
    conn.commit()
    assert pmem.recall(topic="t", persona="donna") == []
    assert len(pmem.recall(topic="t", persona="donna", include_stale=True)) == 1


def test_sweep_marks_stale_but_never_deletes(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "observation", "owner": "nike",
                  "content": "stale obs", "topics": ["t"]})
    _age_row(pmem, r["id"], 10)
    before = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    res = pmem.sweep()
    after = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert after == before
    row = pmem.get_db().execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "stale"
    assert r["id"] in res["staled"]


def test_episodic_never_decays(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "episodic", "owner": "shared", "content": "x", "topics": ["t"]})
    _age_row(pmem, r["id"], 9999)
    pmem.sweep()
    row = pmem.get_db().execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "active"


def test_verify_reactivates(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "semantic", "owner": "shared", "content": "w",
                  "topics": ["t"], "confidence": "high"})
    conn = pmem.get_db()
    conn.execute("UPDATE memories SET status='stale' WHERE id=?", (r["id"],))
    conn.commit()
    pmem.verify(r["id"])
    row = pmem.get_db().execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "active"


def test_promote_consolidates_and_archives_sources(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = None
    for _ in range(5):
        r = pmem.add({"kind": "observation", "owner": "nike",
                      "content": "low energy after late night", "topics": ["energy"]})
    res = pmem.promote(topic="energy", owner="nike", threshold=5)
    assert res["promoted"] is True
    conn = pmem.get_db()
    sem = conn.execute(
        "SELECT m.* FROM memories m JOIN memory_topics t ON t.memory_id=m.id "
        "WHERE t.topic='energy' AND m.kind='semantic'").fetchone()
    assert sem is not None
    assert str(r["id"]) in sem["promoted_from"]
    src = conn.execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert src["status"] == "archived"
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id=?",
                        (r["id"],)).fetchone()[0] == 1


def test_promote_below_threshold_does_nothing(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    pmem.add({"kind": "observation", "owner": "nike",
              "content": "x", "topics": ["energy"]})
    res = pmem.promote(topic="energy", owner="nike", threshold=5)
    assert res["promoted"] is False


def test_cli_add_and_recall(tmp_path):
    db = str(tmp_path / "memory.db")
    env = dict(os.environ, PMEM_DB=db)
    payload = tmp_path / "e.json"
    payload.write_text(json.dumps({"kind": "episodic", "owner": "shared",
                                   "content": "cli works", "topics": ["t"]}))
    out = subprocess.run([sys.executable, str(PMEM), "add", str(payload)],
                         capture_output=True, text=True, env=env)
    assert json.loads(out.stdout)["status"] == "added"
    out2 = subprocess.run([sys.executable, str(PMEM), "recall",
                           "--topic", "t", "--persona", "donna"],
                          capture_output=True, text=True, env=env)
    assert "cli works" in out2.stdout
