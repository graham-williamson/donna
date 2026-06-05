#!/usr/bin/env python3
"""Personal-system memory engine. Local SQLite, never-forget, three strata.

kind: episodic (permanent floor) | observation (recurring) | semantic (consolidated).
Nothing is ever deleted — decay/consolidation only transition `status`
(active -> stale -> archived) and write provenance.
"""
import os
import sys
import json
import hashlib
import sqlite3
import argparse
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "PMEM_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "memory.db"),
)

VALID_KINDS = {"episodic", "observation", "semantic"}
VALID_OWNERS = {"donna", "nike", "esme", "bodhi", "shared", "chief"}
DECAY_DEFAULTS = {"semantic": 30, "observation": 7, "episodic": None}
PROMOTE_THRESHOLD = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'episodic',
    owner TEXT NOT NULL DEFAULT 'shared',
    shared INTEGER NOT NULL DEFAULT 1,
    category TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    confidence TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    decay_days INTEGER,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    promoted_from TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_verified TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_topics (
    memory_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    PRIMARY KEY (memory_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_owner ON memories(owner);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_topic ON memory_topics(topic);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(text):
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def get_db():
    path = os.path.abspath(DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def add(entry):
    kind = entry.get("kind", "episodic")
    owner = entry.get("owner", "shared")
    if kind not in VALID_KINDS:
        raise ValueError(f"bad kind: {kind}")
    if owner not in VALID_OWNERS:
        raise ValueError(f"bad owner: {owner}")
    content = entry["content"]
    h = content_hash(content)
    ts = now_iso()
    topics = entry.get("topics", []) or []
    conn = get_db()
    existing = conn.execute(
        "SELECT id, kind FROM memories WHERE content_hash=? AND owner=? AND kind=?",
        (h, owner, kind)).fetchone()
    if existing:
        if kind == "observation":
            conn.execute(
                "UPDATE memories SET recurrence_count = recurrence_count + 1, "
                "last_verified=?, status='active' WHERE id=?", (ts, existing["id"]))
            conn.commit()
            return {"status": "recurred", "id": existing["id"]}
        return {"status": "duplicate", "id": existing["id"]}
    decay = entry.get("decay_days", DECAY_DEFAULTS[kind])
    cur = conn.execute(
        "INSERT INTO memories (kind, owner, shared, category, content, content_hash, "
        "confidence, status, decay_days, recurrence_count, promoted_from, tags, "
        "metadata_json, date, created_at, last_verified) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, owner, int(entry.get("shared", 1)), entry.get("category", ""),
         content, h, entry.get("confidence", ""), "active", decay, 1, "",
         ",".join(entry.get("tags", [])), json.dumps(entry.get("metadata", {})),
         entry.get("date", ts[:10]), ts, ts))
    mid = cur.lastrowid
    for t in topics:
        conn.execute("INSERT OR IGNORE INTO memory_topics (memory_id, topic) VALUES (?,?)",
                     (mid, t))
    conn.commit()
    return {"status": "added", "id": mid}


def recall(topic, persona, kind=None, limit=10, include_stale=False):
    conn = get_db()
    q = ("SELECT m.* FROM memories m JOIN memory_topics t ON t.memory_id = m.id "
         "WHERE t.topic = ? AND (m.shared = 1 OR m.owner = ?) ")
    params = [topic, persona]
    if not include_stale:
        q += "AND m.status = 'active' "
    else:
        q += "AND m.status != 'archived' "
    if kind:
        q += "AND m.kind = ? "
        params.append(kind)
    q += "ORDER BY m.created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params)]


def verify(mid):
    conn = get_db()
    conn.execute("UPDATE memories SET status='active', last_verified=? WHERE id=?",
                 (now_iso(), mid))
    conn.commit()
    return {"status": "verified", "id": mid}


def _age_days(last_verified):
    lv = datetime.strptime(last_verified, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - lv).total_seconds() / 86400.0


def sweep(promote_threshold=PROMOTE_THRESHOLD):
    conn = get_db()
    staled = []
    for row in conn.execute("SELECT id, kind, decay_days, last_verified "
                            "FROM memories WHERE status='active'").fetchall():
        decay = row["decay_days"]
        if decay is None:
            decay = DECAY_DEFAULTS.get(row["kind"])
        if decay is None:  # episodic / never-decay
            continue
        if _age_days(row["last_verified"]) > decay:
            conn.execute("UPDATE memories SET status='stale' WHERE id=?", (row["id"],))
            staled.append(row["id"])
    conn.commit()
    return {"staled": staled, "deleted": 0}


def promote(topic, owner, threshold=PROMOTE_THRESHOLD, confidence="medium"):
    conn = get_db()
    obs = conn.execute(
        "SELECT m.id, m.content, m.recurrence_count FROM memories m "
        "JOIN memory_topics t ON t.memory_id=m.id "
        "WHERE t.topic=? AND m.owner=? AND m.kind='observation' AND m.status='active'",
        (topic, owner)).fetchall()
    weight = sum(o["recurrence_count"] for o in obs)
    if weight < threshold:
        return {"promoted": False, "weight": weight}
    source_ids = [o["id"] for o in obs]
    summary = "Consolidated pattern on '%s' (%d signals): %s" % (
        topic, weight, "; ".join(o["content"] for o in obs[:10]))
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO memories (kind, owner, shared, content, content_hash, confidence, "
        "status, decay_days, recurrence_count, promoted_from, date, created_at, last_verified) "
        "VALUES ('semantic',?,1,?,?,?,'active',?,1,?,?,?,?)",
        (owner, summary, content_hash(summary), confidence,
         DECAY_DEFAULTS["semantic"], json.dumps(source_ids), ts[:10], ts, ts))
    sem_id = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO memory_topics (memory_id, topic) VALUES (?,?)",
                 (sem_id, topic))
    for sid in source_ids:
        conn.execute("UPDATE memories SET status='archived', last_verified=? WHERE id=?",
                     (ts, sid))
    conn.commit()
    return {"promoted": True, "semantic_id": sem_id, "sources": source_ids}


def _cmd_add(args):
    print(json.dumps(add(json.load(open(args.file)))))


def _cmd_recall(args):
    rows = recall(args.topic, args.persona, kind=args.kind, limit=args.limit,
                  include_stale=args.include_stale)
    print(json.dumps(rows, indent=2))


def _cmd_sweep(args):
    print(json.dumps(sweep()))


def _cmd_promote(args):
    print(json.dumps(promote(args.topic, args.owner, threshold=args.threshold)))


def _cmd_verify(args):
    print(json.dumps(verify(args.id)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="pmem")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("file")
    a.set_defaults(fn=_cmd_add)
    r = sub.add_parser("recall")
    r.add_argument("--topic", required=True)
    r.add_argument("--persona", required=True)
    r.add_argument("--kind")
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--include-stale", action="store_true")
    r.set_defaults(fn=_cmd_recall)
    s = sub.add_parser("sweep")
    s.set_defaults(fn=_cmd_sweep)
    pr = sub.add_parser("promote")
    pr.add_argument("--topic", required=True)
    pr.add_argument("--owner", required=True)
    pr.add_argument("--threshold", type=int, default=PROMOTE_THRESHOLD)
    pr.set_defaults(fn=_cmd_promote)
    v = sub.add_parser("verify")
    v.add_argument("id", type=int)
    v.set_defaults(fn=_cmd_verify)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
