#!/usr/bin/env python3
"""Nightly "dream" — consolidate memory without forgetting.

Auto-discovers every (owner, topic) with active observations, promotes those that
recur past threshold into semantic memory (sources archived with provenance, never
deleted), then decays stale items (active -> stale, never deleted). Run nightly.
"""
import json
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pmem():
    spec = importlib.util.spec_from_file_location("pmem", ROOT / "tools" / "pmem.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def dream(promote_threshold=None):
    pmem = _pmem()
    conn = pmem.get_db()
    pairs = conn.execute(
        "SELECT DISTINCT m.owner, t.topic FROM memories m "
        "JOIN memory_topics t ON t.memory_id = m.id "
        "WHERE m.kind = 'observation' AND m.status = 'active'").fetchall()
    promoted = []
    for r in pairs:
        kw = {} if promote_threshold is None else {"threshold": promote_threshold}
        res = pmem.promote(r["topic"], r["owner"], **kw)
        if res.get("promoted"):
            promoted.append({"owner": r["owner"], "topic": r["topic"],
                             "semantic_id": res["semantic_id"]})
    swept = pmem.sweep()
    return {"promoted": promoted, "staled": swept["staled"], "deleted": 0}


if __name__ == "__main__":
    print(json.dumps(dream(), indent=2))
