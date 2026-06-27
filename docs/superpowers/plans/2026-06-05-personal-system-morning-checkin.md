# Personal System — Plan 5: Nike's Morning Check-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Replace the deleted dumb SPR ping with Nike's smart morning check-in — built as the system's first ICM **Layer-2 stage contract**.

**Architecture:** A Layer-2 stage doc at `personas/nike/skills/morning-checkin/SKILL.md` (INPUTS / PROCESS / OUTPUTS) plus `tools/checkin.py` providing the data plumbing: `gather_inputs()` (active Nike/green committed goals + recent energy observations from the memory floor) and `log_response()` (logs Graham's reply as `observation`s so nightly dreaming surfaces patterns). Nike *composes* the message at runtime in her voice; the calendar and SPR plan are fetched at runtime via the daemon's MCP/Notion (out of scope for this module). Live scheduling/firing is the final daemon-integration step (after Plan 6).

**Tech Stack:** Python 3 stdlib + Plan-1 `pmem` + Plan-4 `goals`, pytest.

**Spec:** §8 morning check-in, §2.5 (Layer-2 commitment).

## File structure
- Create: `personal-system/personas/nike/skills/morning-checkin/SKILL.md`
- Create: `personal-system/tools/checkin.py`
- Test: `personal-system/tests/test_checkin.py`

---

### Task 1: The Layer-2 stage contract

- [ ] **Step 1: Write failing test** (`test_checkin.py`)

```python
import importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/"tools"/f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_skill_contract_layers():
    t = (ROOT/"personas"/"nike"/"skills"/"morning-checkin"/"SKILL.md").read_text()
    assert "INPUTS" in t and "PROCESS" in t and "OUTPUTS" in t
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Write `SKILL.md`** (full content in build).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(nike): morning check-in Layer-2 stage contract`

---

### Task 2: `checkin.py` — gather inputs + log reply

- [ ] **Step 1: Write failing tests**

```python
def test_gather_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path/"memory.db"))
    gp = str(tmp_path/"g.json")
    goals = load("goals"); pmem = load("pmem")
    g = goals.add_goal("Run 10k", "green", goals_path=gp)
    goals.commit_goal(g["id"], goals_path=gp)
    pmem.add({"kind":"observation","owner":"nike","content":"slept 5h","topics":["energy"]})
    checkin = load("checkin")
    res = checkin.gather_inputs(goals_path=gp)
    assert len(res["active_goals"]) == 1
    assert any("slept 5h" in r["content"] for r in res["recent_energy"])

def test_log_response(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path/"memory.db"))
    checkin = load("checkin")
    checkin.log_response("legs are toast", energy="low")
    pmem = load("pmem")
    contents = [r["content"] for r in pmem.recall(topic="energy", persona="nike", limit=10)]
    assert "legs are toast" in contents and any("low" in c for c in contents)
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `checkin.py`.**
- [ ] **Step 4: Run full suite — expect PASS.**
- [ ] **Step 5: Commit** `feat(nike): checkin gather_inputs + log_response`

## Done-when
- The stage contract exists with INPUTS/PROCESS/OUTPUTS.
- `gather_inputs` returns Nike's committed goals + recent energy; `log_response` logs the reply as observations the dreamer can consolidate.

## Out of scope (later)
- Runtime Notion/calendar fetch + live scheduling/firing (final daemon integration).
- Nightly dreaming + attention gatekeeper (Plan 6).
