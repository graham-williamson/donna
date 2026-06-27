# Personal System — Plan 1: Foundation (Memory Substrate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local, never-forget memory substrate for the personal multi-agent system — the foundation every persona depends on.

**Architecture:** A single SQLite store (`personal-system/data/memory.db`) holding three strata in one `memories` table: an append-only **episodic floor**, **observations** that recur, and a **consolidated/semantic** layer that "dreaming" builds. Memory is **never deleted** — consolidation and decay only transition a row's `status` (`active → stale → archived`) and link provenance. Ported from two working engines: Graham's `donna/tools/donna-memory.py` (CLI shape, content-hash dedup, JSON I/O) and TradeAlly's Mnemosyne `atlas-cli` (the record/recall/promote/sweep/verify algorithms and the kind/status/confidence/topics/provenance schema — `/Users/tradeally/TradeAlly/TA_Operations/Agents/_infrastructure/atlas/cli/atlas-cli`). We port the *algorithms*, not TradeAlly's markdown-file storage — everything unifies on SQLite for one-file backup and transactional safety.

**Tech Stack:** Python 3 (stdlib only: `sqlite3`, `hashlib`, `json`, `argparse`, `datetime`), `pytest` for tests. No third-party deps.

**Spec:** `docs/superpowers/specs/2026-06-05-personal-multi-agent-system-design.md` (§3 The Brain, §2.4 Reuse).

**Environment note:** This session's capability-guard hook blocks most Bash; the commands below (`pytest`, `git`, `python3`) are written for the executor's normal shell (or for Graham to run via `!`). Files under `/Users/tradeally` are world-readable for reference but are NOT modified by this plan.

---

## Key design decisions (read before starting)

- **One table, three kinds.** `kind ∈ {episodic, observation, semantic}`. Episodic = the permanent floor. Observation = recurring signals. Semantic = consolidated wisdom built by dreaming.
- **Never delete.** No code path issues `DELETE`. Decay and consolidation only set `status` (`active`→`stale`→`archived`) and write provenance. This is the never-forget guarantee and is asserted by tests.
- **Owner / shared.** `owner ∈ {donna, nike, esme, bodhi, shared, chief}`. `shared = 1` means readable by every persona; `shared = 0` means owner-only.
- **Topics** drive per-persona recall, stored in a `memory_topics` junction table.
- **Dedup by kind:** episodic/semantic reject exact duplicates (per owner+kind, by content hash); observations instead **increment `recurrence_count`** and bump `last_verified` (this is what powers recurrence-based promotion).
- **Decay defaults (from Mnemosyne):** semantic 30 days, observation 7 days, episodic `NULL` (never). Promotion threshold 5; promotion looks at active observations on a topic and triggers when their summed `recurrence_count ≥ threshold`.

## File structure

- Create: `personal-system/tools/pmem.py` — the memory CLI/engine (one focused module).
- Create: `personal-system/data/` — holds `memory.db` (gitignored).
- Create: `personal-system/_shared/_policies/recall-topics.json` — per-persona topic map.
- Create: `personal-system/tools/recall_bootstrap.py` — emits a per-persona recall bundle at session start.
- Create: `personal-system/tests/test_pmem.py`, `personal-system/tests/test_recall_bootstrap.py`.
- Create: `personal-system/.gitignore`, `personal-system/README.md`.

---

### Task 1: Scaffold the tree + schema + DB connection

**Files:**
- Create: `personal-system/tools/pmem.py`
- Create: `personal-system/.gitignore`
- Create: `personal-system/README.md`
- Test: `personal-system/tests/test_pmem.py`

- [ ] **Step 1: Write the failing test**

```python
# personal-system/tests/test_pmem.py
import os, sqlite3, tempfile, importlib.util, pathlib

PMEM = pathlib.Path(__file__).resolve().parents[1] / "tools" / "pmem.py"

def load_pmem(db_path):
    spec = importlib.util.spec_from_file_location("pmem", PMEM)
    mod = importlib.util.module_from_spec(spec)
    mod.DB_PATH = str(db_path)
    spec.loader.exec_module(mod)
    mod.DB_PATH = str(db_path)
    return mod

def test_schema_creates_tables(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    conn = pmem.get_db()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memories" in names
    assert "memory_topics" in names
    # WAL mode is on
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest personal-system/tests/test_pmem.py::test_schema_creates_tables -v`
Expected: FAIL (no module `pmem` / file does not exist).

- [ ] **Step 3: Write minimal implementation**

```python
# personal-system/tools/pmem.py
#!/usr/bin/env python3
"""Personal-system memory engine. Local SQLite, never-forget, three strata."""
import os, sys, json, hashlib, sqlite3, argparse
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest personal-system/tests/test_pmem.py::test_schema_creates_tables -v`
Expected: PASS.

- [ ] **Step 5: Add scaffolding files**

```gitignore
# personal-system/.gitignore
data/
*.db
*.db-wal
*.db-shm
__pycache__/
```

```markdown
# personal-system/README.md
Personal multi-agent system. See docs/superpowers/specs/2026-06-05-personal-multi-agent-system-design.md.
`tools/pmem.py` is the local never-forget memory engine.
```

- [ ] **Step 6: Commit**

```bash
git add personal-system/tools/pmem.py personal-system/tests/test_pmem.py personal-system/.gitignore personal-system/README.md
git commit -m "feat(pmem): scaffold personal-system + memory schema"
```

---

### Task 2: `add` — record a memory (with kind-aware dedup)

**Files:**
- Modify: `personal-system/tools/pmem.py`
- Test: `personal-system/tests/test_pmem.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_add_episodic_then_duplicate_rejected(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r1 = pmem.add({"kind": "episodic", "owner": "esme",
                   "content": "Graham landed the demo", "topics": ["wins"]})
    assert r1["status"] == "added"
    r2 = pmem.add({"kind": "episodic", "owner": "esme",
                   "content": "Graham landed the demo", "topics": ["wins"]})
    assert r2["status"] == "duplicate" and r2["id"] == r1["id"]

def test_add_observation_increments_recurrence(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    a = pmem.add({"kind": "observation", "owner": "nike",
                  "content": "low energy after late night", "topics": ["energy"]})
    b = pmem.add({"kind": "observation", "owner": "nike",
                  "content": "low energy after late night", "topics": ["energy"]})
    assert a["id"] == b["id"]
    conn = pmem.get_db()
    row = conn.execute("SELECT recurrence_count FROM memories WHERE id=?",
                       (a["id"],)).fetchone()
    assert row["recurrence_count"] == 2

def test_add_writes_topics(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "episodic", "owner": "shared",
                  "content": "fact", "topics": ["a", "b"]})
    conn = pmem.get_db()
    topics = {x[0] for x in conn.execute(
        "SELECT topic FROM memory_topics WHERE memory_id=?", (r["id"],))}
    assert topics == {"a", "b"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest personal-system/tests/test_pmem.py -k add -v`
Expected: FAIL (`add` not defined).

- [ ] **Step 3: Implement `add`**

```python
# append to pmem.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest personal-system/tests/test_pmem.py -k add -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add personal-system/tools/pmem.py personal-system/tests/test_pmem.py
git commit -m "feat(pmem): add() with kind-aware dedup and topics"
```

---

### Task 3: `recall` — by topic, per-persona, excludes stale

**Files:**
- Modify: `personal-system/tools/pmem.py`
- Test: `personal-system/tests/test_pmem.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_recall_by_topic_and_persona_visibility(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    pmem.add({"kind": "episodic", "owner": "esme", "shared": 0,
              "content": "esme-only note", "topics": ["worry"]})
    pmem.add({"kind": "episodic", "owner": "shared", "shared": 1,
              "content": "shared note", "topics": ["worry"]})
    # bodhi sees shared but not esme-only
    got = [r["content"] for r in pmem.recall(topic="worry", persona="bodhi")]
    assert "shared note" in got
    assert "esme-only note" not in got
    # esme sees her own + shared
    got_esme = [r["content"] for r in pmem.recall(topic="worry", persona="esme")]
    assert "esme-only note" in got_esme and "shared note" in got_esme

def test_recall_excludes_stale_by_default(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "semantic", "owner": "shared",
                  "content": "old wisdom", "topics": ["t"], "confidence": "high"})
    pmem.get_db().execute("UPDATE memories SET status='stale' WHERE id=?", (r["id"],))
    pmem.get_db().commit()
    assert pmem.recall(topic="t", persona="donna") == []
    assert len(pmem.recall(topic="t", persona="donna", include_stale=True)) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest personal-system/tests/test_pmem.py -k recall -v`
Expected: FAIL (`recall` not defined).

- [ ] **Step 3: Implement `recall`**

```python
# append to pmem.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest personal-system/tests/test_pmem.py -k recall -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add personal-system/tools/pmem.py personal-system/tests/test_pmem.py
git commit -m "feat(pmem): recall() by topic with persona visibility + stale filter"
```

---

### Task 4: `verify` + `sweep` decay — mark stale, NEVER delete

**Files:**
- Modify: `personal-system/tools/pmem.py`
- Test: `personal-system/tests/test_pmem.py`

- [ ] **Step 1: Write the failing tests (the never-forget guarantee)**

```python
def _age_row(pmem, mid, days):
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = pmem.get_db()
    c.execute("UPDATE memories SET last_verified=? WHERE id=?", (old, mid)); c.commit()

def test_sweep_marks_stale_but_never_deletes(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "observation", "owner": "nike",
                  "content": "stale obs", "topics": ["t"]})
    _age_row(pmem, r["id"], 10)  # observation decay default = 7d
    before = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    res = pmem.sweep()
    after = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert after == before  # NOTHING deleted
    row = pmem.get_db().execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "stale"
    assert r["id"] in res["staled"]

def test_episodic_never_decays(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "episodic", "owner": "shared", "content": "x", "topics": ["t"]})
    _age_row(pmem, r["id"], 9999)
    pmem.sweep()
    row = pmem.get_db().execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "active"

def test_verify_reactivates(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    r = pmem.add({"kind": "semantic", "owner": "shared", "content": "w",
                  "topics": ["t"], "confidence": "high"})
    pmem.get_db().execute("UPDATE memories SET status='stale' WHERE id=?", (r["id"],))
    pmem.get_db().commit()
    pmem.verify(r["id"])
    row = pmem.get_db().execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest personal-system/tests/test_pmem.py -k "sweep or verify or decays" -v`
Expected: FAIL (`sweep`/`verify` not defined).

- [ ] **Step 3: Implement `verify` and `sweep` (decay phase)**

```python
# append to pmem.py
from datetime import datetime as _dt

def verify(mid):
    conn = get_db()
    conn.execute("UPDATE memories SET status='active', last_verified=? WHERE id=?",
                 (now_iso(), mid))
    conn.commit()
    return {"status": "verified", "id": mid}

def _age_days(last_verified):
    lv = _dt.strptime(last_verified, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest personal-system/tests/test_pmem.py -k "sweep or verify or decays" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add personal-system/tools/pmem.py personal-system/tests/test_pmem.py
git commit -m "feat(pmem): verify() + sweep() decay — stale not deleted (never-forget)"
```

---

### Task 5: `promote` — recurrence-based consolidation (non-lossy, with provenance)

**Files:**
- Modify: `personal-system/tools/pmem.py`
- Test: `personal-system/tests/test_pmem.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_promote_consolidates_and_archives_sources(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    # one observation recurring to threshold (5)
    for _ in range(5):
        r = pmem.add({"kind": "observation", "owner": "nike",
                      "content": "low energy after late night", "topics": ["energy"]})
    res = pmem.promote(topic="energy", owner="nike", threshold=5)
    assert res["promoted"] is True
    conn = pmem.get_db()
    # a new semantic exists on the topic, with provenance to the source id
    sem = conn.execute(
        "SELECT m.* FROM memories m JOIN memory_topics t ON t.memory_id=m.id "
        "WHERE t.topic='energy' AND m.kind='semantic'").fetchone()
    assert sem is not None
    assert str(r["id"]) in sem["promoted_from"]
    # source observation is archived, NOT deleted
    src = conn.execute("SELECT status FROM memories WHERE id=?", (r["id"],)).fetchone()
    assert src["status"] == "archived"
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id=?",
                        (r["id"],)).fetchone()[0] == 1

def test_promote_below_threshold_does_nothing(tmp_path):
    pmem = load_pmem(tmp_path / "memory.db")
    pmem.add({"kind": "observation", "owner": "nike",
              "content": "x", "topics": ["energy"]})
    res = pmem.promote(topic="energy", owner="nike", threshold=5)
    assert res["promoted"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest personal-system/tests/test_pmem.py -k promote -v`
Expected: FAIL (`promote` not defined).

- [ ] **Step 3: Implement `promote`**

```python
# append to pmem.py
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
    # archive sources — preserve provenance, never delete
    for sid in source_ids:
        conn.execute("UPDATE memories SET status='archived', last_verified=? WHERE id=?",
                     (ts, sid))
    conn.commit()
    return {"promoted": True, "semantic_id": sem_id, "sources": source_ids}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest personal-system/tests/test_pmem.py -k promote -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add personal-system/tools/pmem.py personal-system/tests/test_pmem.py
git commit -m "feat(pmem): promote() consolidation — archives sources with provenance"
```

---

### Task 6: CLI surface + migrate existing donna-memory.db

**Files:**
- Modify: `personal-system/tools/pmem.py` (argparse `main`)
- Create: `personal-system/tools/migrate_donna_memory.py`
- Test: `personal-system/tests/test_pmem.py`, `personal-system/tests/test_migrate.py`

- [ ] **Step 1: Write the failing tests**

```python
# in test_pmem.py
import subprocess, sys
def test_cli_add_and_recall(tmp_path):
    db = str(tmp_path / "memory.db")
    env = dict(os.environ, PMEM_DB=db)
    payload = tmp_path / "e.json"
    payload.write_text(json.dumps({"kind": "episodic", "owner": "shared",
                                   "content": "cli works", "topics": ["t"]}))
    out = subprocess.run([sys.executable, str(PMEM), "add", str(payload)],
                         capture_output=True, text=True, env=env)
    assert json.loads(out.stdout)["status"] == "added"
    out2 = subprocess.run([sys.executable, str(PMEM), "recall",
                           "--topic", "t", "--persona", "donna"],
                          capture_output=True, text=True, env=env)
    assert "cli works" in out2.stdout
```

```python
# personal-system/tests/test_migrate.py
import os, sqlite3, json, importlib.util, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]

def test_migrate_imports_existing_rows(tmp_path):
    # build a fake legacy donna-memory.db
    legacy = tmp_path / "donna-memory.db"
    c = sqlite3.connect(legacy)
    c.executescript("""CREATE TABLE entries (id INTEGER PRIMARY KEY, category TEXT,
        subcategory TEXT, date TEXT, content TEXT, content_hash TEXT, tags TEXT,
        metadata_json TEXT, created_at TEXT);""")
    c.execute("INSERT INTO entries (category, subcategory, date, content, content_hash, "
              "tags, metadata_json, created_at) VALUES "
              "('fitness','', '2026-05-01','did legs','h','','{}','2026-05-01T00:00:00Z')")
    c.commit(); c.close()
    target = tmp_path / "memory.db"
    spec = importlib.util.spec_from_file_location(
        "mig", ROOT / "tools" / "migrate_donna_memory.py")
    mig = importlib.util.module_from_spec(spec); spec.loader.exec_module(mig)
    n = mig.migrate(str(legacy), str(target))
    assert n == 1
    out = sqlite3.connect(target)
    row = out.execute("SELECT kind, owner, category, content FROM memories").fetchone()
    assert row == ("episodic", "shared", "fitness", "did legs")
    # topic carries the legacy category
    t = out.execute("SELECT topic FROM memory_topics").fetchone()[0]
    assert t == "fitness"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest personal-system/tests/ -k "cli or migrate" -v`
Expected: FAIL (no `main` CLI / no migrate module).

- [ ] **Step 3: Implement the CLI `main` and the migration**

```python
# append to pmem.py
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
    a = sub.add_parser("add"); a.add_argument("file"); a.set_defaults(fn=_cmd_add)
    r = sub.add_parser("recall")
    r.add_argument("--topic", required=True); r.add_argument("--persona", required=True)
    r.add_argument("--kind"); r.add_argument("--limit", type=int, default=10)
    r.add_argument("--include-stale", action="store_true"); r.set_defaults(fn=_cmd_recall)
    s = sub.add_parser("sweep"); s.set_defaults(fn=_cmd_sweep)
    pr = sub.add_parser("promote")
    pr.add_argument("--topic", required=True); pr.add_argument("--owner", required=True)
    pr.add_argument("--threshold", type=int, default=PROMOTE_THRESHOLD)
    pr.set_defaults(fn=_cmd_promote)
    v = sub.add_parser("verify"); v.add_argument("id", type=int); v.set_defaults(fn=_cmd_verify)
    args = p.parse_args(argv)
    args.fn(args)

if __name__ == "__main__":
    main()
```

```python
# personal-system/tools/migrate_donna_memory.py
#!/usr/bin/env python3
"""One-time import of legacy donna-memory.db rows into the new memory floor as episodic."""
import sqlite3, os, importlib.util, pathlib
PMEM = pathlib.Path(__file__).resolve().parent / "pmem.py"

def _load_pmem(db_path):
    spec = importlib.util.spec_from_file_location("pmem", PMEM)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.DB_PATH = db_path
    return mod

def migrate(legacy_path, target_path):
    pmem = _load_pmem(target_path)
    src = sqlite3.connect(legacy_path); src.row_factory = sqlite3.Row
    n = 0
    for row in src.execute("SELECT * FROM entries"):
        topics = [row["category"]] if row["category"] else []
        pmem.add({"kind": "episodic", "owner": "shared", "shared": 1,
                  "category": row["category"], "content": row["content"],
                  "topics": topics, "tags": (row["tags"] or "").split(",") if row["tags"] else [],
                  "date": row["date"]})
        n += 1
    return n

if __name__ == "__main__":
    import sys
    legacy = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.expanduser("~/donna/data/donna-memory.db")
    target = os.environ.get("PMEM_DB",
        str(pathlib.Path(__file__).resolve().parents[1] / "data" / "memory.db"))
    print("migrated", migrate(legacy, target), "rows")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest personal-system/tests/ -k "cli or migrate" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add personal-system/tools/pmem.py personal-system/tools/migrate_donna_memory.py personal-system/tests/
git commit -m "feat(pmem): CLI surface + legacy donna-memory migration"
```

---

### Task 7: Per-persona recall bootstrap

**Files:**
- Create: `personal-system/_shared/_policies/recall-topics.json`
- Create: `personal-system/tools/recall_bootstrap.py`
- Test: `personal-system/tests/test_recall_bootstrap.py`

- [ ] **Step 1: Create the topic config**

```json
{
  "schema_version": 1,
  "default": ["session-handoff"],
  "personas": {
    "donna": ["chief-preferences", "schedule", "commitments"],
    "nike": ["energy", "training", "fitness"],
    "esme": ["goals", "worry", "wins", "self-worth"],
    "bodhi": ["values", "reflections", "meaning"]
  }
}
```

- [ ] **Step 2: Write the failing test**

```python
# personal-system/tests/test_recall_bootstrap.py
import os, json, importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]

def _load(db_path):
    spec = importlib.util.spec_from_file_location("rb", ROOT / "tools" / "recall_bootstrap.py")
    rb = importlib.util.module_from_spec(spec); spec.loader.exec_module(rb)
    rb.PMEM_DB = db_path
    return rb

def test_bootstrap_loads_only_personas_slice(tmp_path, monkeypatch):
    db = str(tmp_path / "memory.db")
    monkeypatch.setenv("PMEM_DB", db)
    # seed memories on esme + nike topics
    import importlib.util as u
    ps = u.spec_from_file_location("pmem", ROOT / "tools" / "pmem.py")
    pmem = u.module_from_spec(ps); pmem.DB_PATH = db; ps.loader.exec_module(pmem); pmem.DB_PATH = db
    pmem.add({"kind": "episodic", "owner": "esme", "shared": 0,
              "content": "fear of failing", "topics": ["worry"]})
    pmem.add({"kind": "episodic", "owner": "nike", "shared": 0,
              "content": "deadlift PB", "topics": ["training"]})
    rb = _load(db)
    bundle = rb.bootstrap("esme")
    assert "fear of failing" in bundle
    assert "deadlift PB" not in bundle  # nike's private slice excluded
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest personal-system/tests/test_recall_bootstrap.py -v`
Expected: FAIL (no module).

- [ ] **Step 4: Implement the bootstrap**

```python
# personal-system/tools/recall_bootstrap.py
#!/usr/bin/env python3
"""Emit a per-persona recall bundle at session start. Loads only that persona's slice."""
import os, json, importlib.util, pathlib
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest personal-system/tests/test_recall_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite + commit**

Run: `pytest personal-system/tests/ -v`
Expected: PASS (all tasks' tests).

```bash
git add personal-system/_shared/_policies/recall-topics.json personal-system/tools/recall_bootstrap.py personal-system/tests/test_recall_bootstrap.py
git commit -m "feat(pmem): per-persona recall bootstrap"
```

---

## Done-when

- `pytest personal-system/tests/ -v` is green.
- `pmem.py` supports `add | recall | sweep | promote | verify` over a local SQLite floor.
- Never-forget is enforced and tested: no `DELETE`; decay → `stale`; consolidation → sources `archived` with provenance.
- Legacy `donna-memory.db` migrates in as episodic memory.
- A persona boots with only its own topic slice (shared + owner-private).

## Out of scope (later plans)
- Nightly scheduling of `sweep` (Plan 6 — dreaming + attention).
- Persona dispatch / addressing (Plan 2).
- The personas themselves, the Daruma board, the morning check-in (Plans 3–5).
