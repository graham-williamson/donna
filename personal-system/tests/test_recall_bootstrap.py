import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(db_path):
    spec = importlib.util.spec_from_file_location("rb", ROOT / "tools" / "recall_bootstrap.py")
    rb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rb)
    rb.PMEM_DB = db_path
    return rb


def test_bootstrap_loads_only_personas_slice(tmp_path, monkeypatch):
    db = str(tmp_path / "memory.db")
    monkeypatch.setenv("PMEM_DB", db)
    spec = importlib.util.spec_from_file_location("pmem", ROOT / "tools" / "pmem.py")
    pmem = importlib.util.module_from_spec(spec)
    pmem.DB_PATH = db
    spec.loader.exec_module(pmem)
    pmem.DB_PATH = db
    pmem.add({"kind": "episodic", "owner": "esme", "shared": 0,
              "content": "fear of failing", "topics": ["worry"]})
    pmem.add({"kind": "episodic", "owner": "nike", "shared": 0,
              "content": "deadlift PB", "topics": ["training"]})
    rb = _load(db)
    bundle = rb.bootstrap("esme")
    assert "fear of failing" in bundle
    assert "deadlift PB" not in bundle
