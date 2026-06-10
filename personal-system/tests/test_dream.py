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
    res = load("dream").dream(use_llm=False)   # hermetic: no claude CLI in tests
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
    res = load("dream").dream(use_llm=False)   # hermetic: no claude CLI in tests
    assert r["id"] in res["staled"]


# --- 2026-06-10: LLM summaries + nightly issue audit ---
def test_dream_uses_summarizer_for_promotion(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    pmem = load("pmem")
    for _ in range(5):
        pmem.add({"kind": "observation", "owner": "nike",
                  "content": "low energy late nights", "topics": ["energy"]})
    res = load("dream").dream(
        summarizer=lambda topic, contents: "Late nights drain Graham.",
        use_llm=False)
    sem_id = res["promoted"][0]["semantic_id"]
    row = pmem.get_db().execute(
        "SELECT content FROM memories WHERE id=?", (sem_id,)).fetchone()
    assert row["content"] == "Late nights drain Graham."


def test_dream_audits_near_dups_and_contradictions(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    pmem = load("pmem")
    pmem.add({"kind": "semantic", "owner": "nike",
              "content": "Graham trains in the morning", "topics": ["training"]})
    pmem.add({"kind": "semantic", "owner": "nike",
              "content": "Graham never trains in the morning", "topics": ["training"]})
    res = load("dream").dream(
        contradiction_checker=lambda a, b: True, use_llm=False)
    assert res["issues"]["contradictions"] >= 1
    kinds = {i["kind"] for i in pmem.issues()}
    assert "contradiction" in kinds


def test_dream_without_llm_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    pmem = load("pmem")
    for _ in range(5):
        pmem.add({"kind": "observation", "owner": "nike",
                  "content": "tired on mondays", "topics": ["energy"]})
    res = load("dream").dream(use_llm=False)
    assert res["promoted"] and "issues" in res
