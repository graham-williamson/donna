#!/usr/bin/env python3
"""Emit a per-persona recall bundle at session start. Loads only that persona's slice."""
import os
import json
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "_shared" / "_policies" / "recall-topics.json"
PMEM_DB = os.environ.get("PMEM_DB", str(ROOT / "data" / "memory.db"))
MAX_PER_TOPIC = 4
MAX_BYTES = 2048


def _pmem():
    spec = importlib.util.spec_from_file_location("pmem", ROOT / "tools" / "pmem.py")
    mod = importlib.util.module_from_spec(spec)
    mod.DB_PATH = PMEM_DB
    spec.loader.exec_module(mod)
    mod.DB_PATH = PMEM_DB
    return mod


def bootstrap(persona):
    cfg = json.load(open(CONFIG))
    topics = list(cfg.get("default", [])) + list(cfg.get("personas", {}).get(persona, []))
    pmem = _pmem()
    lines = [f"## Memory recall for {persona}"]
    for t in topics:
        rows = pmem.recall(topic=t, persona=persona, limit=MAX_PER_TOPIC)
        for r in rows:
            lines.append(f"- [{t}] {r['content'][:180]}")
    text = "\n".join(lines)
    enc = text.encode("utf-8")
    if len(enc) > MAX_BYTES:
        text = enc[:MAX_BYTES].decode("utf-8", "ignore").rsplit("\n", 1)[0]
        text += "\n_(recall truncated to budget)_"
    return text


if __name__ == "__main__":
    import sys
    print(bootstrap(sys.argv[1] if len(sys.argv) > 1 else "donna"))
