# Personal System — Plan 4: Daruma Goal Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** A coloured-Daruma goal board — `goals.json` as source of truth, a purpose-built local web dashboard with fillable eyes and a set/complete tracker.

**Architecture:** `tools/goals.py` owns `goals.json` (in `_shared/_state/`, gitignored, Time-Machine-backed). Colour = domain = owner (`green→nike`, `purple→esme`, `red→shared`, `black→donna`, `pink→esme`, `gold→donna`, `white→shared`, `blue→donna`). Eye mechanic: `commit_goal` fills the **left** eye (`daruma_state=left`, stamps `committed_at`); `achieve_goal` fills the **right** eye (`daruma_state=both`, stamps `achieved_at`) **and logs a win to Esme's evidence ledger** (Plan 3). `tools/dashboard.py` is a launch-on-demand local web server (stdlib `http.server`, NOT a daemon) that renders each goal as a coloured Daruma SVG with fillable eyes and click-to-commit/achieve links.

**Tech Stack:** Python 3 stdlib (`json`, `http.server`), Plan-1 `pmem` + Plan-3 `evidence`, pytest.

**Spec:** §6 Daruma board.

## File structure
- Create: `personal-system/tools/goals.py`, `personal-system/tools/dashboard.py`
- Test: `personal-system/tests/test_goals.py`, `personal-system/tests/test_dashboard.py`
- Data: `personal-system/_shared/_state/goals.json` (created at runtime, gitignored)

---

### Task 1: `goals.py` — the board engine

- [ ] **Step 1: Write failing tests** (`test_goals.py`)

```python
import importlib.util, pathlib, pytest
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/"tools"/f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_add_derives_owner_from_colour(tmp_path):
    goals = load("goals")
    g = goals.add_goal("Deadlift 140", "green", goals_path=str(tmp_path/"g.json"))
    assert g["owner"] == "nike" and g["daruma_state"] == "none" and g["committed_at"] is None

def test_commit_fills_left_eye(tmp_path):
    goals = load("goals"); gp = str(tmp_path/"g.json")
    g = goals.add_goal("x", "purple", goals_path=gp)
    c = goals.commit_goal(g["id"], goals_path=gp)
    assert c["daruma_state"] == "left" and c["committed_at"]

def test_achieve_fills_both_and_logs_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path/"memory.db"))
    goals = load("goals"); gp = str(tmp_path/"g.json")
    g = goals.add_goal("Run 10k", "green", goals_path=gp)
    goals.commit_goal(g["id"], goals_path=gp)
    a = goals.achieve_goal(g["id"], goals_path=gp)
    assert a["daruma_state"] == "both" and a["achieved_at"] and a["evidence"]
    ev = load("evidence")
    assert any("Run 10k" in r["content"] for r in ev.surface_evidence())

def test_invalid_colour(tmp_path):
    goals = load("goals")
    with pytest.raises(ValueError):
        goals.add_goal("x", "teal", goals_path=str(tmp_path/"g.json"))

def test_list_filters(tmp_path):
    goals = load("goals"); gp = str(tmp_path/"g.json")
    goals.add_goal("a", "green", goals_path=gp)
    goals.add_goal("b", "purple", goals_path=gp)
    assert len(goals.list_goals(colour="green", goals_path=gp)) == 1
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `goals.py`** (full code in build).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(goals): Daruma board engine — colour→owner, eye lifecycle, evidence on achieve`

---

### Task 2: `dashboard.py` — coloured-doll render + on-demand server

- [ ] **Step 1: Write failing tests** (`test_dashboard.py`)

```python
import importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load_dashboard():
    spec = importlib.util.spec_from_file_location("dashboard", ROOT/"tools"/"dashboard.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_render_shows_colour_and_title():
    dash = load_dashboard()
    html = dash.render_board([{"id":1,"title":"Deadlift 140kg","colour":"green","owner":"nike",
        "why_it_matters":"strength","daruma_state":"left","committed_at":"2026-06-05","achieved_at":None}])
    assert "Deadlift 140kg" in html and "#2e7d32" in html and 'data-state="left"' in html

def test_render_eye_fills():
    dash = load_dashboard()
    none = dash.render_board([{"id":1,"title":"x","colour":"red","owner":"shared","daruma_state":"none"}])
    both = dash.render_board([{"id":2,"title":"y","colour":"red","owner":"shared","daruma_state":"both"}])
    assert none.count('fill="#111"') == 0 and both.count('fill="#111"') == 2
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `dashboard.py`** (`render_board`, `_daruma_svg`, on-demand `serve`).
- [ ] **Step 4: Run full suite — expect PASS.**
- [ ] **Step 5: Commit** `feat(dashboard): coloured Daruma board with fillable eyes + tracker`

## Done-when
- `goals.py` manages the board with the colour→owner model and the two-eye lifecycle; achieving logs a win.
- `dashboard.py render_board` renders coloured dolls with correct eye states; `python3 tools/dashboard.py` serves an interactive board on `localhost:8765` (launch-on-demand).

## Out of scope (later)
- Add-goal web form (CLI/agents add goals for now).
- The morning check-in (Plan 5); nightly dreaming (Plan 6).
