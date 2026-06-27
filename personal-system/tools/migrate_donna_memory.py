#!/usr/bin/env python3
"""One-time import of legacy donna-memory.db rows into the new memory floor as episodic."""
import os
import sqlite3
import importlib.util
import pathlib

PMEM = pathlib.Path(__file__).resolve().parent / "pmem.py"


def _load_pmem(db_path):
    spec = importlib.util.spec_from_file_location("pmem", PMEM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = db_path
    return mod


def migrate(legacy_path, target_path):
    pmem = _load_pmem(target_path)
    src = sqlite3.connect(legacy_path)
    src.row_factory = sqlite3.Row
    n = 0
    for row in src.execute("SELECT * FROM entries"):
        topics = [row["category"]] if row["category"] else []
        tags = (row["tags"] or "").split(",") if row["tags"] else []
        pmem.add({"kind": "episodic", "owner": "shared", "shared": 1,
                  "category": row["category"], "content": row["content"],
                  "topics": topics, "tags": tags, "date": row["date"]})
        n += 1
    return n


if __name__ == "__main__":
    import sys
    legacy = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.expanduser("~/donna/data/donna-memory.db")
    target = os.environ.get(
        "PMEM_DB",
        str(pathlib.Path(__file__).resolve().parents[1] / "data" / "memory.db"))
    print("migrated", migrate(legacy, target), "rows")
