# Personal System — Plan 6: Nightly Dreaming + Attention Gatekeeper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** The nightly "dream" that consolidates memory without forgetting, and the attention gatekeeper that keeps the four voices from overwhelming Graham.

**Architecture:** `tools/dream.py` — the nightly consolidation pass: auto-discover every `(owner, topic)` with active observations, run `pmem.promote` on each (recurrence ≥ threshold → semantic, sources archived with provenance), then `pmem.sweep` (decay active → stale). Never deletes. `tools/gatekeeper.py` — the attention tiers: `propose(persona, text, tier)` sends now for `interrupt`/`nudge`, queues to a digest for `digest`, drops for `silent`; `drain_digest()` flushes the queue at a ritual. Single-gatekeeper discipline (only Donna sends proactively between rituals) is enforced by routing all proactive output through `propose`.

**Tech Stack:** Python 3 stdlib + Plan-1 `pmem`, pytest.

**Spec:** §3.3 dreaming, §5 attention model.

## File structure
- Create: `personal-system/tools/dream.py`, `personal-system/tools/gatekeeper.py`
- Test: `personal-system/tests/test_dream.py`, `personal-system/tests/test_gatekeeper.py`

---

### Task 1: `dream.py` — nightly consolidation

- [ ] **Step 1: Write failing tests** (`test_dream.py`)

```python
import importlib.util, pathlib
from datetime import datetime, timezone, timedelta
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/"tools"/f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_dream_promotes_and_preserves(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path/"memory.db"))
    pmem = load("pmem")
    for _ in range(5):
        pmem.add({"kind":"observation","owner":"nike","content":"low energy late nights","topics":["energy"]})
    before = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    res = load("dream").dream()
    after = pmem.get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert any(p["topic"] == "energy" for p in res["promoted"])
    assert after == before + 1 and res["deleted"] == 0

def test_dream_decays_old_to_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path/"memory.db"))
    pmem = load("pmem")
    r = pmem.add({"kind":"observation","owner":"esme","content":"x","topics":["worry"]})
    old = (datetime.now(timezone.utc)-timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = pmem.get_db(); c.execute("UPDATE memories SET last_verified=? WHERE id=?", (old, r["id"])); c.commit()
    res = load("dream").dream()
    assert r["id"] in res["staled"]
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `dream.py`.**
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(dream): nightly consolidation — auto-promote + decay, never delete`

---

### Task 2: `gatekeeper.py` — attention tiers

- [ ] **Step 1: Write failing tests** (`test_gatekeeper.py`)

```python
import importlib.util, pathlib, pytest
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/"tools"/f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_interrupt_sends(tmp_path):
    gk = load("gatekeeper")
    assert gk.propose("donna","urgent","interrupt", queue_path=str(tmp_path/"q.json"))["action"] == "send"

def test_digest_queues_and_drains(tmp_path):
    gk = load("gatekeeper"); q = str(tmp_path/"q.json")
    gk.propose("esme","gentle nudge","digest", queue_path=q)
    gk.propose("bodhi","a reflection","digest", queue_path=q)
    assert len(gk.drain_digest(queue_path=q)) == 2
    assert gk.drain_digest(queue_path=q) == []

def test_silent(tmp_path):
    gk = load("gatekeeper")
    assert gk.propose("nike","fyi","silent", queue_path=str(tmp_path/"q.json"))["action"] == "silent"

def test_bad_tier(tmp_path):
    gk = load("gatekeeper")
    with pytest.raises(ValueError):
        gk.propose("donna","x","shout", queue_path=str(tmp_path/"q.json"))
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `gatekeeper.py`.**
- [ ] **Step 4: Run full suite — expect PASS.**
- [ ] **Step 5: Commit** `feat(gatekeeper): attention tiers — interrupt/nudge/digest/silent`

## Done-when
- `dream.dream()` consolidates observations to semantics and decays stale items, with the row count only ever growing (never deleting).
- `gatekeeper.propose` routes by tier; the digest queue accumulates and drains.

## Go-live ops (final integration, documented here)
- **Nightly dream + DR backup** (launchd or cron, ~03:30): `python3 personal-system/tools/dream.py` then `sqlite3 personal-system/data/memory.db ".backup personal-system/data/memory.backup.db"` (the consistent snapshot Time Machine then captures off-box).
- **Live wiring:** route inbound Telegram through `dispatch.route` → `assemble_context` (persona overlay + `recall_bootstrap`) per turn; schedule the morning check-in (Nike) and a weekly review; rename the bot to **Daruma**.
