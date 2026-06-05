import importlib.util
import pathlib
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_dream_promotes_and_preserves(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    pmem = load("pmem")
    for _ in range(5):
        pmem.add({"kind": "observation", "owner": "nike",
                  "content": "low energy late nights", "topics": ["energy"]})
    before = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    res = load("dream").dream()
    after = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert any(p["topic"] == "energy" for p in res["promoted"])
    assert after == before + 1 and res["deleted"] == 0


def test_dream_decays_old_to_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    pmem = load("pmem")
    r = pmem.add({"kind": "observation", "owner": "esme", "content": "x", "topics": ["worry"]})
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = pmem.get_db()
    c.execute("UPDATE memories SET last_verified=? WHERE id=?", (old, r["id"]))
    c.commit()
    res = load("dream").dream()
    assert r["id"] in res["staled"]
