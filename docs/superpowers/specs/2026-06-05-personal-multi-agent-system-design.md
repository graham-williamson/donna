# Personal Multi-Agent System — Design Spec (v1)

**Date:** 2026-06-05
**Status:** DRAFT — awaiting Chief sign-off
**Author:** Donna (with Graham)

---

## 0. North Star

A personal AI system that **grows with Graham** and **never forgets**. Not a productivity gadget — a small crew of distinct voices that help him run his life, train his body, tend his mind, and walk a spiritual-not-religious path, all sharing one memory of who he is and who he's becoming.

Two non-negotiables drive every decision below:
1. **The system grows with him** — it gets to know him better over time, never resets to zero.
2. **It never forgets** — consolidation builds new layers on top of memory; it never destroys the floor.

**One-line architecture:** *one brain, one set of hands, several minds — on one daemon.*

---

## 1. The Cast

Four distinct voices, split by the **relationship** each holds. Each has a signature emoji that doubles as the user's summon-glyph.

| Glyph | Name | Relationship | Owns |
|---|---|---|---|
| 💁‍♀️ | **Donna** | PA, in the truest sense — runs the day, has his back, never on his case | Comms, scheduling, logistics, the front door, broker-mediated action |
| 💪 | **Nike** | Trainer — Greek goddess of victory; drive, the body, the win | SPR, training, recovery, fitness habits, the push |
| 🌱 | **Esme** | Coach + therapist — *"beloved"*; growth and the steady work of believing he's worthy | Goals, worries, self-worth, the evidence ledger, inner-critic work |
| 🗻 | **Bodhi** | Contemplative — *"awakening"*; spiritual-not-religious + philosophy | Stillness, awe, meaning, Stoic/Buddhist/Shinto threads |

**Naming provenance worth preserving:** "Bodhi" = awakening/enlightenment. The Daruma goal-board (§6) is built around the Daruma doll, and "Daruma" is the Japanese name for **Bodhidharma**, founder of Zen — so the contemplative voice carries the same name-root as the doll the whole goal system rests on. (Donna is the only *person* in the set; the others are elemental — fitting, she's the human front door.)

**Bodhi's hard rule:** approaches "connection to something larger" **only** through awe, nature, interbeing, and lineage — never religion. (Graham is spiritual, not religious, and wary of the "something larger" framing on religious grounds but open to exploring it this way.)

**Esme's hard boundary:** coach and therapist, **not a clinician**. See §9.

---

## 2. Architecture: one brain, one set of hands, several minds

### 2.1 The minds (personas)

Each persona is **not** a separate program. It is a bundle of:
- a `PERSONA.md` (identity, voice, hard rules, method-set),
- a memory-topic subscription (which slice of the brain it wakes with),
- a set of skills (`SKILL.md` contracts), and
- an authority profile (which broker capabilities it may reach).

A persona is *summoned* — adopted as the voice + memory scope for a turn — not booted as a daemon.

### 2.2 The runtime: ONE daemon, summonable minds

The system runs as the **single existing Donna daemon** (launchd `com.user.claude-telegram`, the hardened supervisor, one Telegram bot). **Not** TradeAlly's fleet of per-agent daemons — that model is too fragile for a personal system (see the 2026-06-05 outage: one daemon was hard enough to keep alive; four would be four silent-death risks).

- **Donna is the front door** on the one bot.
- Other voices are **summoned as sub-sessions** sharing the brain (persona overlay + memory bootstrap for that turn).
- **Scheduled rituals** (the smart morning check-in, a weekly review) boot the right persona directly.

### 2.3 The hands: the broker, shared

All four voices reach the outside world through the **existing `donna-broker`** — the hardened capability-guard, age-vault, approval flow. The broker is **agent-agnostic**: it gates by *capability*, not by *who's asking*.

- **Inner-loop work** (reading memory, conversing, reflecting, writing to the local memory floor) — **no broker.** No secrets, no external surface. This is ~90% of Esme's and Bodhi's work.
- **Outer-loop work** (Notion, Gmail, Calendar, the EA executor) — **broker-gated, unchanged.** New capabilities are registered as new external actions are needed (e.g. a `notion.update_page` for the Daruma board already exists).

This is the load-bearing simplification: the broker we already hardened is what makes a multi-voice personal system *safe to attempt* — no soft prompt-authority tables (TradeAlly's model), a hard gate instead.

### 2.4 Reuse vs deliberately-different (lessons from TradeAlly)

We **lift TradeAlly's proven, working code and patterns** where they fit, and **deliberately diverge** where its choices were a poor fit for a personal system.

**Reuse (port, don't rewrite):**
- The **Mnemosyne memory engine** — `atlas-cli` (~3000 lines of production-tested Python: `memory-record`/`recall`/`promote`/`verify`/`sweep`, recurrence-based consolidation, non-lossy archival-with-provenance, the decay sweep). We port the **memory verbs** and strip the TradeAlly-specific tiers (GDPR/PII intel, customer cohorts, Atlas orchestration). This is the single biggest reuse — we are **not** reinventing memory.
- The **memory-bootstrap** pattern (SessionStart hook → topic recall → inject).
- The **`_shared/_state` JSON + schema-validation** pattern.
- The **agent-mail** handoff pattern.
- **SKILL.md** capability contracts.

**Deliberately different (avoid the mistakes):**
- **One daemon, not a fleet.** TradeAlly runs a persistent supervisor per agent; the 2026-06-05 outage showed how fragile even a single daemon is to keep alive. Four daemons = four silent-death risks. We run ONE.
- **Hard broker, not prompt-authority.** TradeAlly trusts prompt-written authority tables; we use the injection-resistant capability-guard broker.
- **No bidirectional Notion sync.** TradeAlly pushes insights to Notion; we keep local memory as the single source of truth and feed Notion one-way only (§3.2).

### 2.5 ICM layer alignment (TradeAlly's Interpretable Context Methodology)

TradeAlly defines agents with a documented 5-layer ICM model (`_shared/_context/icm-layer-conventions.md`). We adopt its principles where the one-daemon model fits, and diverge where it doesn't:

| ICM Layer | Question | Personal system | Status |
|---|---|---|---|
| **0 — Identity** | who am I? | per-persona `personas/<id>/PERSONA.md` | ✅ adopted |
| **1 — Routing** | where do I go? | centralized `tools/dispatch.py` (routes *between* personas, not per-persona self-routing) | ⚠️ deliberately collapsed — one daemon, one shared dispatcher |
| **2 — Stage contract** | what do I do? | per-ritual `SKILL.md` with INPUTS / PROCESS / OUTPUTS | 🔜 **adopt from Plan 5 onward** |
| **3 — Reference** | what rules apply? | `_shared/_policies/` (e.g. `recall-topics.json`); persona methods embedded inline at this scale | ◑ partial |
| **4 — Working** | what am I working with? | `pmem` memory floor + `_shared/_state/` (`active_voice.json`, later `goals.json`) + agent-mail (later) | ✅ core present |

**Commitments:**
- Every ritual/workflow (morning check-in, weekly review, …) is built as a **Layer-2 stage contract** (INPUTS / PROCESS / OUTPUTS), per ICM. The morning check-in (Plan 5) is the first.
- Add a thin per-persona **capability manifest** when wiring the broker — declaring which broker capabilities each voice may reach (the Layer-1 capability-declaration role, minus the routing).
- We forgo ICM's selective-layer loading (~80% token saving) at v1 scale — four short overlays load whole. Revisit if overlays grow heavy.

---

## 3. The Brain — memory substrate (THE key system)

The memory is the heart. It is **local** (on the Mac Mini — confirmed), modelled on the **Mnemosyne** pattern Graham already built in TradeAlly (`atlas-cli`, ~3000 lines of working code + spec `2026-05-27-mnemosyne-memory-architecture-9b1d3a47.md`) and the **Anthropic Dreams** concept it was learned from (`platform.claude.com/docs/en/managed-agents/dreams`).

### 3.1 The never-forget guarantee — three strata

Memory is layered. **Consolidation builds new strata on top; it never deletes the floor.**

1. **Episodic floor** — append-only, permanent, *never deleted*. Every fact, exchange, win, worry, workout. This is the never-forget guarantee. **Reuses the existing `data/donna-memory.db` SQLite store** (migrated into the new `personal-system/_shared/_memory/` tree — see §10), extended with a `persona`/`owner` column and a `shared` flag so memories are namespaced per-voice or shared-across-voices.
2. **Consolidated layer** — what "dreaming" builds (§3.3): merged, de-duplicated, distilled wisdom. Each consolidated item carries a **provenance link down to its raw episodic sources**.
3. **Recall** — per-persona topic slices, loaded at session start via a bootstrap hook (Mnemosyne's best idea). Esme wakes with goals/worries/wins/self-worth; Nike with training history/energy; Bodhi with values/reflections; Donna with schedule/preferences/commitments.

**Why this satisfies "never forget":** both reference designs preserve the source.
- *Dreams:* "The input store is never modified" — a dream produces a **new** curated store you adopt or discard; the original is untouched.
- *Mnemosyne:* recurrence promotes observations to semantic memory, but the source is stamped `archived` with provenance, never deleted ("preserve provenance but stop loading").

Even if a consolidated layer drops a detail, the episodic floor still holds it — always re-derivable.

### 3.2 Reconciling Graham's existing stores

| Store | Role in the new substrate |
|---|---|
| `data/donna-memory.db` (SQLite) | **Becomes the episodic floor**, migrated into `personal-system/_shared/_memory/`. Add `persona`/`owner` + `shared`; keep content-hash dedup. |
| `~/.claude/.../memory/*.md` files | **Stay the always-on persona layer** — small, persona/preference facts loaded every session (per voice). |
| Notion "Donna's Desk" | **Complementary read surface** — long-form content Graham browses (Desk, exercise plans, written reflections). **Fed one-way (memory → Notion) only; never bidirectionally synced** — local memory is the source of truth, so the two can't drift. The Daruma board does NOT live in Notion (see §6). |
| `session-summary.md` handoff | **Stays the daemon-restart continuity note.** Orthogonal to long-term memory. |

### 3.3 Dreaming — local consolidation, non-lossy

A scheduled (or manually-triggered) **dream** pass, ported from Mnemosyne's `memory sweep`:
- **Input:** the episodic floor + recent session transcripts.
- **Process:** merge duplicates, replace stale/contradicted entries with the latest, surface new cross-session insights (e.g. "across six mornings, his energy is lowest after late TradeAlly nights").
- **Output:** updated/added items in the **consolidated layer**, each linked back to sources. Sources are stamped `archived`/`stale` and dropped from default recall — **never deleted**.
- **Trigger:** a **nightly local sweep** from day one (Chief's call 2026-06-05). v2 may optionally call the real **Dreams API** as a premium "dreamer" once it's out of research preview.

### 3.4 Inter-voice handoffs

A lightweight **agent-mail** pattern (ported from TradeAlly's `_shared/_mail/`): one voice can leave a note for another via the shared brain. *Donna → Esme: "He sounded flat about the startup this morning, worth a check-in."* These feed the attention model (§5); they don't ping Graham directly.

---

## 4. Addressing / routing

There is always an **active voice**, defaulting to Donna. Conversation is **sticky** to the active voice until Graham switches — so replies need no re-tagging; he just talks.

**Switching, three ways:**
1. **Lead with the glyph or name** — `🌱 I feel like a fraud today` → Esme; `Bodhi, …` → Bodhi. Cold-opening a chat: same rule.
2. **Telegram reply-to** — reply to a specific persona's message bubble → routes to that voice, even mid-thread. The precise scalpel.
3. **Say nothing** — defaults to Donna, the receptionist, who routes/hands off: *"This is an Esme conversation — bringing her in."*

Under the hood: the daemon reads the routing signal (reply-to → leading glyph/name → sticky active voice → else Donna), then adopts that persona's voice + memory bootstrap for the turn. No new bots, no new daemons.

---

## 5. Attention model — rhythm + one gatekeeper

The make-or-break constraint: **agents must not fight over Graham's time, but must not be passive either.** Resolution (Graham chose option A):

- **Rituals are the heartbeat.** Each voice has *guaranteed* scheduled slots — Nike at the morning check-in, Esme an evening or two a week, Bodhi a Sunday. This answers "not passive": each voice reliably shows up.
- **Between rituals, only Donna may reach him**, and only when something clears a real bar. Single gatekeeper → no slot-machine.
- **Decouple push from pull:** the only thing single-gatekept is *who initiates*. Where Graham *goes* to work (entering Esme's or Bodhi's space) is pull — those voices never buzz him.
- **Notification tiers** (ported from TradeAlly's `notification_policy`): `interrupt` (rare, time-critical) / `nudge` (single, gentle) / `digest` (batched into the next ritual) / `silent` (logged only). Most agent activity is digest/silent.

---

## 6. The Daruma goal board

**Colour = life domain = the voice that owns the push.** The board is both a dashboard and the **routing layer for goals**.

| Daruma | Domain | Owner |
|---|---|---|
| 🟢 Green | Body, training, energy | Nike |
| 🟣 Purple | Inner growth, self-worth, meaning | Esme + Bodhi (the self-improvement / enlightenment seam) |
| 🔴 Red | The one north-star goal | All three |
| ⚫ Black | TradeAlly / startup | Donna surfaces; strategy stays in TA_Operations |
| 💗 Pink | Sarah, Max, Annabelle, relationships | Esme holds, Donna actions |
| 🟡 Gold | Personal finances | Donna |
| ⚪ White | Balance, learning, the journey | Shared |
| 🔵 Blue | Craft, skills, career | Donna + Esme |

**Eye mechanic = goal lifecycle:** **left eye = committed** (the owning voice starts working it), **right eye = achieved** (moves to the won column, and the win is logged to Esme's evidence ledger).

- **Source of truth:** `goals.json` in the shared state — each goal carries `colour`, `owner`, `priority`, `why_it_matters`, `daruma_state` (none/left/both), `committed_at` (timestamp the left eye was filled — goal *set*), `achieved_at` (timestamp the right eye was filled — goal *complete*), and a link to evidence.
- **Interface:** a purpose-built lightweight **Daruma dashboard** — a small local web UI that reads and writes `goals.json` directly. Requirements (Chief, 2026-06-05):
  - Each goal is rendered as an **actual coloured Daruma doll** in its domain colour (🟢 green, 🟣 purple, 🔴 red, ⚫ black, 💗 pink, 🟡 gold, ⚪ white, 🔵 blue), with **eyes that fill in** — left when set, right when achieved.
  - Set goals interactively (fill the left eye) and mark them complete (fill the right eye).
  - A **tracker** showing, per goal, **when it was set** and **when it was completed** (from `committed_at` / `achieved_at`) — a history of commitments and wins.
  - One source of truth *and* the interaction surface, so there is no two-way sync to drift. (Chosen over a Notion render so the board is genuinely *interactive*.)

---

## 7. Esme's evidence ledger (the self-worth mechanism)

Graham's deepest ask — *"believe I am worthy of success, love who I am"* — is served by **mechanism, not sympathy**:
- An append-only **evidence ledger** in the episodic floor: wins, hard things done anyway, competence shown, good feedback. Esme actively *catches* him doing well and files it.
- When the inner critic flares, Esme answers with **logged evidence** ("here are fourteen times the record disagrees").
- Methods: CBT thought-records, ACT (defusion, values-based action), motivational interviewing, self-compassion (Neff), identity-based habits (Atomic Habits), values anchoring against the Stoic/Buddhist thread.

---

## 8. The wedge — smart morning check-in (first build)

Replaces the deleted dumb 8:49 "SPR check-in" ping (removed from `schedules.json` 2026-06-05).

- **Reads:** Daruma goals, commitments, calendar, energy/mood log, SPR plan.
- **Composed by Nike** (Chief's call 2026-06-05) — she owns the morning ritual, reading goals/calendar/energy/SPR data; Donna gatekeeps the channel and may add a day-level note.
- **Asks one real question**, adapts to him.
- **Logs his answer back to the brain** (mood, energy, intention) — so every morning the system gets smarter. This is the growth loop in miniature.

---

## 9. Esme's safety boundary

Esme is a **coach and structured-reflection companion, not a clinician.** Hard rules in her `PERSONA.md`:
- She does CBT/ACT/MI/self-compassion *techniques*, not diagnosis or treatment.
- A **crisis-escalation line**: on signals of genuine clinical distress or risk, she drops the coaching frame, says so plainly, and points to a human / helpline — she never plays doctor.
- This boundary is explicit and tested (§12).

---

## 10. v1 scope vs later

**Chief's call (2026-06-05): build the full system in v1 — all four voices at depth, not a thin slice.**

**v1 (this spec):**
- New **`personal-system/`** tree holding the whole system (separate from `donna/`; shares the broker + daemon).
- Local memory substrate: episodic floor (existing SQLite migrated in) + per-persona recall bootstrap + **nightly dream sweep**.
- **All four voices at full depth:**
  - **Donna** — refactored to pure PA (fitness role removed).
  - **Nike** — full training engine; **owns the smart morning check-in**.
  - **Esme** — evidence ledger, CBT/ACT/MI/self-compassion method-set, safety boundary.
  - **Bodhi** — contemplative-practice library (stillness, awe, Stoic/Buddhist/Shinto).
- Persona-dispatch + addressing (§4).
- Daruma board: `goals.json` + Notion render + eye mechanic + evidence ledger.
- Attention: the gatekeeper + ritual slots (§5).
- The smart morning check-in (Nike).

**Phase 2 / later:**
- A voice earning its own separate bot (likely Esme or Bodhi) if in-thread rooms prove insufficient.
- Optional Dreams API as the premium "dreamer."
- **DR / backup in case the Mac Mini dies** — the never-forget floor must survive hardware death. **Approach: Time Machine** (no bespoke system), with two requirements: (a) the backup target lives **off the Mac Mini** — external drive or NAS — or it dies with the box; (b) a small scheduled `sqlite3 .backup` dump of the episodic memory DB so there's always a guaranteed-consistent, restorable copy of the one irreplaceable file. *(Captured at Graham's request, 2026-06-05.)*

---

## 11. Error handling & failure modes

- **Daemon resilience:** inherits the hardened supervisor (one daemon, KeepAlive). Lessons from the 2026-06-05 outage already baked into the plist (PATH includes `~/.local/bin`, `DONNA_SKIP_PERMISSIONS=true`).
- **Memory never-forget:** the episodic floor is append-only; dreaming is non-destructive; sources archived with provenance. A bad dream output can be discarded — the floor is intact.
- **Graceful degradation:** if memory recall fails at session start, the voice wakes with an empty bundle and continues (Mnemosyne's "never block startup" rule) — never goes silent on Telegram.
- **Broker:** outer-loop failures surface honestly (no silent-fail), per existing broker rules.

---

## 12. Testing

- **Memory:** write→dream→recall round-trip preserves every source (assert no episodic deletion); provenance links resolve; per-persona recall returns only the right slice.
- **Routing:** each switch mechanism (glyph / name / reply-to / default) lands on the correct voice; stickiness holds across replies.
- **Attention:** between-ritual messages are gatekept by Donna; tiers route correctly (interrupt vs digest).
- **Esme safety:** crisis-signal inputs trigger the escalation line, not coaching.
- **Daruma:** left/right-eye transitions update `goals.json` and (on achieve) write to the evidence ledger.

---

## 13. Resolved decisions (2026-06-05)

1. **v1 voice depth** — **all four voices at full depth in v1** (not a basic slice).
2. **Morning check-in voice** — **Nike owns it.**
3. **Dream cadence** — **nightly sweep from the start.**
4. **Memory home** — **a new `personal-system/` tree** (separate from `donna/`).

---

## 14. Build order (for the plan)

1. Scaffold the `personal-system/` tree + memory substrate (episodic floor migrated in + recall bootstrap) — the foundation everything needs.
2. Persona-dispatch + addressing on the existing daemon.
3. The four full personas: Donna (pure PA), Esme (evidence ledger + safety), Nike (training engine), Bodhi (contemplative library).
4. Daruma board (`goals.json` + Notion render + eye mechanic + evidence ledger).
5. Nike's smart morning check-in.
6. Nightly dream sweep + attention gatekeeper.
