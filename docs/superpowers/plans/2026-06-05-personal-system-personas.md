# Personal System — Plan 3: The Four Personas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Give each voice — 💁‍♀️ Donna, 💪 Nike, 🌱 Esme, 🗻 Bodhi — its real identity overlay, and build Esme's evidence-ledger mechanism.

**Architecture:** A `PERSONA.md` per voice under `personal-system/personas/<id>/`, loaded by `dispatch.assemble_context` (Plan 2 already prefers a real `PERSONA.md` over the fallback). Esme's self-worth mechanism is `tools/evidence.py` — `log_win()` / `surface_evidence()` over the Plan-1 memory floor (`owner=esme`, topics `wins`/`self-worth`). Persona content is markdown, so tests assert structural invariants (the overlay loads, Esme carries her safety boundary, Bodhi carries the no-religion rule) plus a code round-trip for the ledger.

**Tech Stack:** Markdown (overlays), Python 3 stdlib + Plan-1 `pmem` (evidence), pytest.

**Spec:** §1 cast, §7 evidence ledger, §9 Esme safety boundary.

## File structure
- Create: `personal-system/personas/{donna,nike,esme,bodhi}/PERSONA.md`
- Create: `personal-system/tools/evidence.py`
- Test: `personal-system/tests/test_personas.py`, `personal-system/tests/test_evidence.py`

---

### Task 1: The four PERSONA.md overlays

- [ ] **Step 1: Write failing tests** (`personal-system/tests/test_personas.py`)

```python
import importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch", ROOT/"tools"/"dispatch.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d); return d

def test_each_persona_overlay_loads():
    d = load_dispatch()
    for pid, name in [("donna","Donna"),("nike","Nike"),("esme","Esme"),("bodhi","Bodhi")]:
        ctx = d.assemble_context(pid)
        assert name in ctx and len(ctx) > 200  # real overlay, not the short fallback

def test_esme_has_safety_boundary():
    t = (ROOT/"personas"/"esme"/"PERSONA.md").read_text().lower()
    assert "not a clinician" in t and "crisis" in t

def test_bodhi_never_religion():
    t = (ROOT/"personas"/"bodhi"/"PERSONA.md").read_text().lower()
    assert "never" in t and "religion" in t
```

- [ ] **Step 2: Run — expect FAIL** (overlays missing).
- [ ] **Step 3: Write the four `PERSONA.md` files** (full content in the build).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(personas): Donna, Nike, Esme, Bodhi identity overlays`

---

### Task 2: Esme's evidence ledger

- [ ] **Step 1: Write failing tests** (`personal-system/tests/test_evidence.py`)

```python
import importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load_evidence():
    spec = importlib.util.spec_from_file_location("evidence", ROOT/"tools"/"evidence.py")
    e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e); return e

def test_evidence_log_and_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path/"memory.db"))
    ev = load_evidence()
    ev.log_win("shipped the memory engine")
    out = [r["content"] for r in ev.surface_evidence()]
    assert "shipped the memory engine" in out
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `tools/evidence.py`** (`log_win`, `surface_evidence`).
- [ ] **Step 4: Run full suite — expect PASS.**
- [ ] **Step 5: Commit** `feat(esme): evidence ledger — log_win + surface_evidence`

## Done-when
- All four overlays load through `assemble_context`.
- Esme's overlay carries the not-a-clinician / crisis boundary; Bodhi's carries the never-religion rule.
- The evidence ledger round-trips a win through the memory floor.

## Out of scope (later)
- Wiring overlays + recall into the live daemon (after Plan 4–5).
- The Daruma board (Plan 4); the morning check-in (Plan 5).
