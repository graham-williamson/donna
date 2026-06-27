# Personal System — Plan 2: Persona Dispatch + Addressing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans / subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Decide which persona handles each turn and keep a sticky "active voice", honouring glyph/name/reply-to switches.

**Architecture:** A pure-logic `dispatch.py` module. `route(text, reply_to, state)` resolves the persona by precedence — **reply-to → leading glyph/name → sticky active voice → default (Donna)** — strips any address prefix, and persists the new active voice to `_shared/_state/active_voice.json`. `assemble_context(persona_id, recall)` returns the persona's system overlay (its `PERSONA.md` when present — Plan 3 — else a registry fallback) optionally combined with a recall bundle (Plan 1). No daemon wiring yet; that's where the daemon calls `route` then `assemble_context`.

**Tech Stack:** Python 3 stdlib (`re`, `json`, `pathlib`), pytest.

**Spec:** §4 Addressing/routing.

## File structure
- Create: `personal-system/tools/dispatch.py`
- Test: `personal-system/tests/test_dispatch.py`
- Modify: `personal-system/.gitignore` (ignore runtime `_shared/_state/`)

## Key design
- **Personas registry:** `donna 💁‍♀️ / nike 💪 / esme 🌱 / bodhi 🗻`. The glyph doubles as the summon-token.
- **Precedence:** reply-to (explicit) > address prefix (glyph or `Name,`/`Name:`/`Name `) > sticky active voice > Donna.
- **Stickiness:** the active voice persists across turns until switched.
- **Purity:** `route` and `assemble_context` are pure/injectable (state path + recall passed in) so they test without a daemon or DB.

---

### Task 1: Registry + `parse_address`

**Files:** Create `personal-system/tools/dispatch.py`; Test `personal-system/tests/test_dispatch.py`

- [ ] **Step 1: Write failing tests**

```python
import importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
def load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch", ROOT / "tools" / "dispatch.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d); return d

def test_parse_address_glyph():
    d = load_dispatch()
    assert d.parse_address("🌱 I feel like a fraud") == ("esme", "I feel like a fraud")
    assert d.parse_address("💪 leg day?") == ("nike", "leg day?")
    assert d.parse_address("🗻 what is enough") == ("bodhi", "what is enough")

def test_parse_address_name():
    d = load_dispatch()
    assert d.parse_address("Esme, I'm anxious") == ("esme", "I'm anxious")
    assert d.parse_address("bodhi: meaning?") == ("bodhi", "meaning?")
    assert d.parse_address("Donna do the thing") == ("donna", "do the thing")

def test_parse_address_none():
    d = load_dispatch()
    assert d.parse_address("just a normal message") == (None, "just a normal message")
    assert d.parse_address("Hello there") == (None, "Hello there")
```

- [ ] **Step 2: Run — expect FAIL** (`pytest personal-system/tests/test_dispatch.py -k parse -v`)

- [ ] **Step 3: Implement registry + parse_address** (see `dispatch.py` Task-1 block in the build).

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** `feat(dispatch): persona registry + address parsing`

---

### Task 2: State + `route` (precedence, sticky, default)

**Files:** Modify `dispatch.py`; Test `test_dispatch.py`; Modify `.gitignore`

- [ ] **Step 1: Write failing tests**

```python
def test_route_default_is_donna(tmp_path):
    d = load_dispatch()
    assert d.route("hi", state_path=tmp_path/"s.json")["persona"] == "donna"

def test_route_sticky(tmp_path):
    d = load_dispatch(); sp = tmp_path/"s.json"
    d.route("🌱 hello", state_path=sp)
    res = d.route("still talking", state_path=sp)
    assert res["persona"] == "esme" and res["switched"] is False

def test_route_reply_to_overrides(tmp_path):
    d = load_dispatch(); sp = tmp_path/"s.json"
    d.route("🌱 hello", state_path=sp)
    res = d.route("about that workout", reply_to="nike", state_path=sp)
    assert res["persona"] == "nike" and res["switched"] is True

def test_route_strips_address(tmp_path):
    d = load_dispatch(); sp = tmp_path/"s.json"
    res = d.route("Esme, I'm worried", state_path=sp)
    assert res["persona"] == "esme" and res["text"] == "I'm worried"

def test_state_persists(tmp_path):
    d = load_dispatch(); sp = tmp_path/"s.json"
    d.save_state("bodhi", sp)
    assert d.load_state(sp) == "bodhi"
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `load_state`/`save_state`/`route`; add `_shared/_state/` to `.gitignore`.**
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(dispatch): route() precedence + sticky active voice`

---

### Task 3: `assemble_context` + CLI

**Files:** Modify `dispatch.py`; Test `test_dispatch.py`

- [ ] **Step 1: Write failing tests**

```python
def test_assemble_context_fallback_header():
    d = load_dispatch()
    ctx = d.assemble_context("esme")
    assert "Esme" in ctx and "🌱" in ctx

def test_assemble_context_includes_recall():
    d = load_dispatch()
    ctx = d.assemble_context("nike", recall="- [training] deadlift PB")
    assert "Nike" in ctx and "deadlift PB" in ctx
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `assemble_context` (PERSONA.md if present else fallback) + argparse `main` with `route`/`context` subcommands; `context` wires recall_bootstrap best-effort.**
- [ ] **Step 4: Run full suite — expect PASS.**
- [ ] **Step 5: Commit** `feat(dispatch): assemble_context + CLI`

## Done-when
- `pytest personal-system/tests/test_dispatch.py -v` green.
- `route` resolves persona by the documented precedence and persists the sticky active voice.
- `assemble_context` returns a persona overlay (fallback now, PERSONA.md in Plan 3) + optional recall.

## Out of scope (later)
- Wiring `route`/`assemble_context` into the live Telegram daemon (after Plan 3 personas exist).
- The persona `PERSONA.md` content (Plan 3).
