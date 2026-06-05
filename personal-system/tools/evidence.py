#!/usr/bin/env python3
"""Esme's evidence ledger — the self-worth mechanism.

log_win() files a win to the memory floor; surface_evidence() pulls the logged
proof back out when the inner critic needs answering. Wins are shared so the
whole crew can celebrate them; the source of truth is the Plan-1 memory engine.
"""
import os
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pmem():
    db = os.environ.get("PMEM_DB", str(ROOT / "data" / "memory.db"))
    spec = importlib.util.spec_from_file_location("pmem", ROOT / "tools" / "pmem.py")
    mod = importlib.util.module_from_spec(spec)
    mod.DB_PATH = db
    spec.loader.exec_module(mod)
    mod.DB_PATH = db
    return mod


def log_win(content, tags=None):
    return _pmem().add({
        "kind": "episodic", "owner": "esme", "shared": 1,
        "content": content, "topics": ["wins", "self-worth"],
        "tags": tags or [],
    })


def surface_evidence(limit=10):
    p = _pmem()
    rows = p.recall(topic="wins", persona="esme", limit=limit)
    seen = {r["id"] for r in rows}
    for r in p.recall(topic="self-worth", persona="esme", limit=limit):
        if r["id"] not in seen:
            rows.append(r)
            seen.add(r["id"])
    return rows


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 2 and sys.argv[1] == "log":
        print(json.dumps(log_win(sys.argv[2])))
    else:
        print(json.dumps([r["content"] for r in surface_evidence()], indent=2))
