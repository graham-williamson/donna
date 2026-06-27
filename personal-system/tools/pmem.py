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
# Donna's owners + Daru's council coaches (so dream.py — a separate process — can
# consolidate Daru-coach memories too; memory_bridge also adds these at runtime).
VALID_OWNERS = {"donna", "nike", "esme", "bodhi", "shared", "chief",
                "sage", "takumi", "reeve", "momo", "kuro", "daru"}
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
CREATE TABLE IF NOT EXISTS memory_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_a INTEGER NOT NULL,
    id_b INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE (id_a, id_b, kind)
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


# --- relevance scoring (2026-06-10): recall is ranked, not just newest-first.
# A 20×-recurred high-confidence pattern should outrank a one-off mention from
# yesterday. Computed on read; no schema change; additive.
_CONFIDENCE_W = {"high": 2.0, "medium": 1.0, "low": 0.5, "": 1.0}


def _score(row):
    import math
    conf = _CONFIDENCE_W.get((row.get("confidence") or "").lower(), 1.0)
    rec = math.log10((row.get("recurrence_count") or 1) + 1) + 1.0
    try:
        age = _age_days(row["last_verified"])
    except Exception:
        age = 0.0
    decay = row.get("decay_days") or DECAY_DEFAULTS.get(row.get("kind"), None)
    fresh = 1.0 if decay is None else max(0.5, 1.0 - 0.5 * min(1.0, age / max(decay, 1)))
    return conf * rec * fresh


def _rank(rows, limit):
    rows.sort(key=_score, reverse=True)
    return rows[:limit]


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
    # over-fetch then rank by relevance (confidence × recurrence × freshness)
    q += "ORDER BY m.created_at DESC LIMIT ?"
    params.append(max(limit * 4, limit))
    return _rank([dict(r) for r in conn.execute(q, params)], limit)


def verify(mid):
    conn = get_db()
    conn.execute("UPDATE memories SET status='active', last_verified=? WHERE id=?",
                 (now_iso(), mid))
    conn.commit()
    return {"status": "verified", "id": mid}


def archive(mid, reason=""):
    """Retire a memory: status='archived' so recall() never returns it again.
    NEVER deletes — the row is kept, with the reason + timestamp recorded in
    metadata_json, so it stays auditable and reversible. Idempotent."""
    conn = get_db()
    row = conn.execute("SELECT metadata_json FROM memories WHERE id=?", (mid,)).fetchone()
    if not row:
        return {"status": "missing", "id": mid}
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except Exception:
        meta = {}
    meta["archived_reason"] = reason
    meta["archived_at"] = now_iso()
    conn.execute("UPDATE memories SET status='archived', metadata_json=? WHERE id=?",
                 (json.dumps(meta), mid))
    conn.commit()
    return {"status": "archived", "id": mid}


def correct(mid, new_content, reason="", **overrides):
    """Supersede a wrong memory with a corrected one, non-destructively: ARCHIVE
    the old fact and ADD a new one that INHERITS the old fact's owner / kind /
    category / confidence / topics (each overridable via **overrides), tagged
    metadata.supersedes=mid. The old fact is retained (archived), not deleted."""
    conn = get_db()
    old = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
    if not old:
        return {"status": "missing", "id": mid}
    topics = [r["topic"] for r in conn.execute(
        "SELECT topic FROM memory_topics WHERE memory_id=?", (mid,)).fetchall()]
    archive(mid, reason or "superseded by correction")
    entry = {
        "kind": overrides.get("kind", old["kind"]),
        "owner": overrides.get("owner", old["owner"]),
        "shared": overrides.get("shared", old["shared"]),
        "category": overrides.get("category", old["category"]),
        "content": new_content,
        "confidence": overrides.get("confidence", old["confidence"]),
        "topics": overrides.get("topics", topics),
        "tags": overrides.get("tags", [t for t in (old["tags"] or "").split(",") if t]),
        "metadata": {"supersedes": mid, "correction_reason": reason},
    }
    res = add(entry)
    return {"status": "corrected", "archived": mid,
            "added": res.get("id"), "add_status": res.get("status")}


def recall_all(topic, limit=10):
    """Like recall() but ACROSS ALL OWNERS (no shared/owner scoping) — the whole
    brain. For Donna's in-app chat, which sees every coach's facts. Active only."""
    conn = get_db()
    q = ("SELECT m.* FROM memories m JOIN memory_topics t ON t.memory_id = m.id "
         "WHERE t.topic = ? AND m.status='active' ORDER BY m.created_at DESC LIMIT ?")
    return _rank([dict(r) for r in conn.execute(q, (topic, max(limit * 4, limit)))], limit)


# --- full-text search (2026-06-10): topics are rigid ("tired" never finds the
# "energy" facts). FTS5 over content, kept in sync by triggers + a lazy
# backfill; LIKE fallback if this SQLite lacks FTS5. Additive.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, mem_id UNINDEXED);
CREATE TRIGGER IF NOT EXISTS memories_fts_ins AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts (content, mem_id) VALUES (new.content, new.id);
END;
"""


def _ensure_fts(conn):
    conn.executescript(_FTS_SCHEMA)
    conn.execute(
        "INSERT INTO memory_fts (content, mem_id) "
        "SELECT m.content, m.id FROM memories m "
        "WHERE m.id NOT IN (SELECT mem_id FROM memory_fts)")
    conn.commit()


def search(text, persona=None, limit=10, include_stale=False):
    """Free-text search over memory CONTENT (not topics). Owner-scoped when a
    persona is given (their facts + shared). Best-effort: any FTS failure falls
    back to a LIKE scan; failures never raise."""
    text = (text or "").strip()
    if not text:
        return []
    conn = get_db()
    status_sql = "m.status='active'" if not include_stale else "m.status!='archived'"
    scope_sql, scope_params = ("AND (m.shared=1 OR m.owner=?)", [persona]) if persona else ("", [])
    try:
        _ensure_fts(conn)
        q = (f"SELECT m.* FROM memory_fts f JOIN memories m ON m.id=f.mem_id "
             f"WHERE memory_fts MATCH ? AND {status_sql} {scope_sql} LIMIT ?")
        rows = conn.execute(q, ['"%s"' % text.replace('"', " "), *scope_params,
                                max(limit * 4, limit)]).fetchall()
    except Exception:
        q = (f"SELECT m.* FROM memories m WHERE m.content LIKE ? AND {status_sql} "
             f"{scope_sql} LIMIT ?")
        rows = conn.execute(q, [f"%{text}%", *scope_params, max(limit * 4, limit)]).fetchall()
    return _rank([dict(r) for r in rows], limit)


def list_facts(owner=None, topic=None, kind=None, status="active", limit=100):
    """Browse the brain (memory-browser backend): filterable, ranked, with each
    fact's topics attached. status='any' includes everything but archived."""
    conn = get_db()
    q = "SELECT DISTINCT m.* FROM memories m "
    where, params = [], []
    if topic:
        q += "JOIN memory_topics t ON t.memory_id = m.id "
        where.append("t.topic=?"); params.append(topic)
    if status == "any":
        where.append("m.status!='archived'")
    elif status:
        where.append("m.status=?"); params.append(status)
    if owner:
        where.append("m.owner=?"); params.append(owner)
    if kind:
        where.append("m.kind=?"); params.append(kind)
    if where:
        q += "WHERE " + " AND ".join(where) + " "
    q += "ORDER BY m.created_at DESC LIMIT ?"
    params.append(max(limit * 2, limit))
    rows = _rank([dict(r) for r in conn.execute(q, params)], limit)
    for r in rows:
        r["topics"] = [x["topic"] for x in conn.execute(
            "SELECT topic FROM memory_topics WHERE memory_id=?", (r["id"],))]
    return rows


def topics_summary():
    """All active (owner, topic) pairs with fact counts — the brain's index."""
    conn = get_db()
    return [dict(r) for r in conn.execute(
        "SELECT t.topic, m.owner, COUNT(*) AS n FROM memory_topics t "
        "JOIN memories m ON m.id=t.memory_id WHERE m.status='active' "
        "GROUP BY t.topic, m.owner ORDER BY n DESC")]


# --- nightly audit (2026-06-10): proactively flag near-duplicate facts into
# memory_issues instead of waiting for a contradiction to surface in chat.
# Deterministic (token Jaccard); the LLM contradiction pass lives in dream.py.
def _tokens(text):
    import re as _re
    # crude plural-stem (nights→night) — this is a flagging heuristic, not NLP
    return {w.rstrip("s") for w in _re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) > 2}


def flag_issue(id_a, id_b, kind, detail=""):
    """Record one issue pair (idempotent via UNIQUE)."""
    conn = get_db()
    a, b = sorted((int(id_a), int(id_b)))
    conn.execute(
        "INSERT OR IGNORE INTO memory_issues (id_a, id_b, kind, detail, created_at) "
        "VALUES (?,?,?,?,?)", (a, b, kind, detail, now_iso()))
    conn.commit()


def issues(status="open", limit=100):
    conn = get_db()
    rows = conn.execute(
        "SELECT i.*, ma.content AS content_a, mb.content AS content_b "
        "FROM memory_issues i JOIN memories ma ON ma.id=i.id_a "
        "JOIN memories mb ON mb.id=i.id_b WHERE i.status=? "
        "ORDER BY i.created_at DESC LIMIT ?", (status, limit)).fetchall()
    return [dict(r) for r in rows]


def resolve_issue(issue_id, choice):
    """Human resolution of a flagged pair (Daru's Mind surface, 2026-06-11).
    choice: 'a' keeps fact A (archives B) · 'b' keeps B (archives A) ·
    'both' = both are true (flag dismissed, nothing archived).
    Archive-only, never deletes; idempotent on non-open issues."""
    conn = get_db()
    row = conn.execute("SELECT * FROM memory_issues WHERE id=?",
                       (int(issue_id),)).fetchone()
    if not row:
        return {"status": "missing", "id": issue_id}
    if row["status"] != "open":
        return {"status": row["status"], "id": issue_id}
    archived = None
    if choice in ("a", "b"):
        keep = row["id_a"] if choice == "a" else row["id_b"]
        lose = row["id_b"] if choice == "a" else row["id_a"]
        archive(lose, reason=f"memory issue #{row['id']} resolved: kept #{keep}")
        archived = lose
    new_status = "resolved" if choice in ("a", "b") else "dismissed"
    conn = get_db()
    conn.execute("UPDATE memory_issues SET status=?, detail=? WHERE id=?",
                 (new_status,
                  (row["detail"] or "") + f" | choice={choice}", row["id"]))
    conn.commit()
    return {"status": new_status, "id": row["id"],
            "choice": choice, "archived": archived}


def audit(jaccard_threshold=0.6):
    """Pairwise near-duplicate scan within each (owner, kind) of active facts.
    Flags pairs into memory_issues (idempotent). Returns counts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, owner, kind, content FROM memories WHERE status='active'").fetchall()
    by_group = {}
    for r in rows:
        by_group.setdefault((r["owner"], r["kind"]), []).append(r)
    near = 0
    for group in by_group.values():
        toks = {r["id"]: _tokens(r["content"]) for r in group}
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ta, tb = toks[a["id"]], toks[b["id"]]
                if not ta or not tb:
                    continue
                jac = len(ta & tb) / len(ta | tb)
                if jac >= jaccard_threshold:
                    flag_issue(a["id"], b["id"], "near_dup", f"jaccard={jac:.2f}")
                    near += 1
    return {"near_dups": near, "checked": len(rows)}


def all_active(limit=200, kinds=("semantic", "observation", "episodic")):
    """A bounded full-brain snapshot: all active facts (any owner) of the given
    kinds, newest first. Single-user scale — used to give Donna's chat broad
    context when no single topic dominates the message."""
    conn = get_db()
    ph = ",".join("?" for _ in kinds)
    q = (f"SELECT * FROM memories WHERE status='active' AND kind IN ({ph}) "
         "ORDER BY created_at DESC LIMIT ?")
    return [dict(r) for r in conn.execute(q, (*kinds, limit))]


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


def promote(topic, owner, threshold=PROMOTE_THRESHOLD, confidence="medium",
            summarizer=None):
    """Consolidate recurring observations into one semantic fact. `summarizer`
    (optional callable (topic, [contents]) -> str, e.g. an LLM pass from
    dream.py) turns the signals into ONE readable insight; any failure or None
    falls back to the deterministic concatenation. Additive."""
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
    summary = ""
    if summarizer is not None:
        try:
            summary = (summarizer(topic, [o["content"] for o in obs[:10]]) or "").strip()
        except Exception:
            summary = ""
    if not summary:
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
