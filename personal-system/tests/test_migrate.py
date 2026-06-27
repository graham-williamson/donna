import sqlite3
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_migrate_imports_existing_rows(tmp_path):
    legacy = tmp_path / "donna-memory.db"
    c = sqlite3.connect(legacy)
    c.executescript("""CREATE TABLE entries (id INTEGER PRIMARY KEY, category TEXT,
        subcategory TEXT, date TEXT, content TEXT, content_hash TEXT, tags TEXT,
        metadata_json TEXT, created_at TEXT);""")
    c.execute("INSERT INTO entries (category, subcategory, date, content, content_hash, "
              "tags, metadata_json, created_at) VALUES "
              "('fitness','', '2026-05-01','did legs','h','','{}','2026-05-01T00:00:00Z')")
    c.commit()
    c.close()
    target = tmp_path / "memory.db"
    spec = importlib.util.spec_from_file_location(
        "mig", ROOT / "tools" / "migrate_donna_memory.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    n = mig.migrate(str(legacy), str(target))
    assert n == 1
    out = sqlite3.connect(target)
    row = out.execute("SELECT kind, owner, category, content FROM memories").fetchone()
    assert row == ("episodic", "shared", "fitness", "did legs")
    t = out.execute("SELECT topic FROM memory_topics").fetchone()[0]
    assert t == "fitness"
