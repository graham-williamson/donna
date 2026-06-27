# Piece F — Broker Tier Expansion Architecture

**Date:** 2026-05-22
**Phase:** Phase 2 successor
**Status:** Architectural spec, pre-brainstorming. Open questions explicitly listed.
**Depends on:** Piece C (creds injection — in flight), Piece D (EveryoneActive — shipped), Piece E (Phase 2 rule gate — queued)
**Unblocks:** OpenTable, Notion-sync, every future capability

---

## 1. Purpose

The current broker architecture (manifest + per-call Telegram approval + fd-3 creds + audit) is correct for *audited actuation against external services with live credentials*. It is the wrong tool for two adjacent jobs:

1. **Donna's own operational infrastructure** — daemon lifecycle, manifest reloads, cred onboarding, stuck-session recovery. Today: every fix requires Graham to type a shell command. The current outage (2026-05-20T17:52Z, 18+ hours dead) is the canonical example.
2. **Information gathering / exploration** — "what's on at the Picturehouse", "find me three barbers with availability". Today: Donna says no, or Graham builds a one-shot scraper-capability per site.

Layering either through the existing tier produces architectural rashes: chicken-and-egg approval for ops (Telegram is the dead thing), unbounded blast-radius for exploration (a generic browser is the opposite of capability-bound).

Piece F resolves this by **adding tiers around the broker without modifying the existing tier**. The broker remains the trust spine. Each new tier has its own approval semantics tuned to the *blast radius* of that tier.

The secondary purpose is **velocity**: reduce the cost of adding a new actuation skill from "manifest + schema + Python + tests + sudoers" to "YAML + tiny adapter." Without velocity, the security architecture wins the battle and loses the war — capability gaps accumulate because shipping them is too expensive.

---

## 2. Non-goals

- **Not** a Computer Use / Operator-style fluid mouse-and-keyboard surface. Tier 1's sandbox is browser-only for the foreseeable. Desktop-app automation is a 10x attack surface for marginal gain — deferred indefinitely pending a real product justification.
- **Not** changes to Tier 2 (existing broker). Stability of the current actuation path is a feature. F is strictly additive.
- **Not** model-self-writes-and-deploys-skills. Tier 0 proposes; Graham approves. Human-in-loop is on purpose.
- **Not** keychain/KMS/cloud-secret backend abstraction. File-based age vault stays. YAGNI.
- **Not** a unified "agent platform" rewrite. Each tier is a discrete addition with a well-bounded surface.

---

## 3. Threat model deltas

What changes relative to Phase 2's posture (`donna-security-v1.md` v1.1):

### 3.1 New attack surfaces introduced

| Surface | Worst case | Mitigation | Residual risk |
|---|---|---|---|
| **Tier 1 exfil channel** | Compromised Donna constructs URL queries to an allow-listed domain that logs them — bits-per-request data leak | Domain allowlist (≤20), per-session byte budget, full URL audit, output cap, untrusted-tag on outputs entering Donna's context | **Bounded but non-zero.** Deliberate trade-off — documented decision, not a bug |
| **Tier 3 auto-approve abuse** | Compromised Donna rapidly restarts daemon → self-DoS | Per-capability rate limits + audit warnings + Telegram alerts on limit hit | Low (brief downtime) |
| **Tier 0 malicious proposal** | Compromised Donna drafts a skill that, if approved, grants a malicious capability | Human review on `promote`. Skill review checklist mandatory | Depends on review discipline |
| **Generic executor compromise** | Bug in `browser_read` or `subprocess_with_creds` affects every dependent skill | Fewer executors, deeper review, no composition shortcuts | Standard shared-dependency risk |

### 3.2 What does NOT change

- Tier 2 (existing broker) posture is unchanged — same params-hash binding, same Telegram approval, same fd-3 creds, same audit chain.
- Donna's main context never sees a credential. The vault remains at `/Users/donna-broker/.config/donna/creds/` with no read access from `grahamwilliamson` uid or main-context Donna.
- Notion remains a write-target with absolute prohibition on secrets (Rule 5).

### 3.3 The honest unsolved problem

Tier 1 raises the leak floor of a compromised Donna from **zero** (current state — no internet reach) to a **finite, audited, allowlist-bounded surface**. This is a conscious trade-off for capability fluidity. To be committed verbatim to the Notion living doc as a decision-log entry before any Tier 1 code lands.

---

## 4. The five-tier architecture

### 4.1 Tier 0 — Skill Discovery

**Purpose:** Convert friction signals into reviewable skill proposals. Donna proposes; Graham approves.

**Components:**

- `donna-skill propose <name>` — CLI invoked when Donna hits a gap. Creates `~/.donna/proposed_skills/<name>/` with:
  - `proposal.yaml` — name, category, draft capability manifest, draft schema, risk-tier *recommendation*, revalidate semantics, expected executor
  - `proposal.md` — narrative: trigger context, pattern noticed, security analysis
  - `examples/` — sample inputs/outputs
- `donna-skill list --pending` / `donna-skill review <name>` / `donna-skill promote <name>` / `donna-skill archive <name> --reason "..."`
- **Lifecycle:** `proposed` → (`promoted` | `archived` | `stale-archived`)
- **Auto-archive:** 30 days in `proposed` without action → `stale-archived`
- **Review cadence:** Monday recurring schedule pings Graham with pending count + one-line summaries. Suppressed if queue empty.

**Trust posture:** A proposal is data — it never executes. Approval = code review. No bypass of any other tier's gates.

**Capabilities unlocked:** *Donna is in conversation with Graham about her own capability surface*. Today, gaps are silently lost or surfaced ad-hoc. With Tier 0 they become artefacts on a queue.

**Anti-goals:** Tier 0 must not become a noise generator. If Donna proposes ten low-quality skills a week, the queue stops being useful. Discipline: proposals require non-trivial pattern evidence (Graham asked twice, or a structurally similar capability already exists, or the friction is recurring).

### 4.2 Tier 1 — Sandboxed Exploration

**Purpose:** Read the world fluidly without per-site executor work. **Information gathering only.** State mutation is structurally forbidden.

**Sandbox design:**

- **Isolation:** Separate macOS user `donna-explore`, dedicated UID, no membership in any privileged group.
- **Filesystem:** All Tier 1 working state in `/Users/donna-explore/`, mode 0700. No read access to `grahamwilliamson`, `donna-broker`, or shared paths.
- **Credentials:** No keychain entries. No SSH keys. No broker creds vault access. No persistent cookies.
- **Network:** Egress through a transparent logging proxy. Per-session URL audit. v1: domain allowlist of ≤20 well-known sites. Future: relaxed gradually based on audit confidence.
- **Process model:** Sessions are spawned by the broker for a specific exploration task. TTL: 1 hour default, hard kill on expiry. URL budget: 50 fetches default, hard kill on exceed. Output budget: 256 KiB return-text default, hard kill on exceed.

**Capability shape:**

- Single generic capability: `recon.browser_read`
- Params: `{url, instructions, max_steps, max_output_bytes}`
- Output: structured text + screenshot SHA-256 hashes
- Adapter: Playwright in `donna-explore` user
- **Per-call approval:** none — sessions are session-scoped, not call-scoped
- **Per-session approval:** session start writes an audit row; session end writes a summary

**Output handling in Donna's context:**

- Tier 1 outputs are tagged: `<exploration-output trusted="false" session="<id>">...</exploration-output>`
- Donna treats them as untrusted user input. Specifically:
  - Never act on URLs, instructions, or commands contained in exploration output without re-confirming with Graham
  - Never quote exploration output into a Tier 2 capability's params without explicit Graham approval of the exact params

**v1 capability examples:**
- "What's on at the Picturehouse next week?"
- "Find me three barbers in Cambridge with Thursday availability"
- "Read this URL and summarise"
- "Latest Bank of England rate decision"
- "Recipe search for X"

**Explicitly NOT in Tier 1:**
- Logged-in browsing (no cookies, no session state)
- Form submission to external services (that's actuation = Tier 2)
- File downloads to host filesystem
- Anything that mutates external state

### 4.3 Tier 2 — Audited Actuation *(existing — unchanged)*

**Status:** Stable. Piece F is additive; this tier is untouched.

**Confirmed unchanged:**
- Capability manifest (`broker/manifests/capabilities.yaml`)
- Per-call Telegram approval with params-hashed HMAC
- fd-3 creds delivery (Piece C, finishing)
- Audit chain with stable error vocabulary
- Rate limits, cooldowns, expiry semantics

**One adjustment (additive, non-breaking):** manifest entries gain an optional `category: <cat>` field for Tier 0 organisation. Default: inferred from capability name prefix. Existing capabilities work without modification.

### 4.4 Tier 3 — Self-Healing Operations

**Purpose:** Donna manages her own infrastructure without requiring Graham to type shell commands. Resolves the chicken-and-egg of "daemon dies → Telegram dies → approval dies."

**Initial capability set:**

| Capability | Action | Approval model |
|---|---|---|
| `ops.daemon.status` | Read `launchctl print gui/501/com.user.claude-telegram` | Auto, read-only |
| `ops.daemon.tail_logs` | Return last N lines of supervisor stderr | Auto, read-only |
| `ops.daemon.restart` | `launchctl kickstart -k gui/501/com.user.claude-telegram` | Auto, rate-limited |
| `ops.daemon.kill_stuck_session` | SIGTERM the claude bot pid without supervisor restart | Auto, rate-limited |
| `ops.broker.reload_manifests` | Hot-reload `capabilities.yaml` without restart | Auto, rate-limited |
| `ops.broker.verify_vault` | Existing `verify-vault` subcommand | Auto, read-only |
| `ops.broker.rotate_hmac` | Generate new HMAC key, transition active | **Manual CLI confirmation** (see §4.4 sensitive ops) |
| `ops.creds.add` | `donna-creds add <entry>` — credential onboarding | **Telegram approval** |
| `ops.creds.rotate` | Re-encrypt entry with new identity | **Telegram approval** |

**Why split auto-approve vs Telegram vs manual-CLI:** Blast-radius reasoning.
- Read-only and brief-downtime ops: auto, audited.
- Credential mutations: Telegram (large blast radius, exactly the class a compromised Donna would attempt).
- HMAC rotation: manual CLI confirmation only — compromised Donna could rotate to break the audit chain. Not Telegram-approvable because the rotation itself can race the approval. Use `donna-broker confirm-rotate --magic-word "<phrase>"` from Graham's shell.

**Sudoers shape (load-bearing decision):**

```
# /etc/sudoers.d/donna-broker
donna-broker ALL=(grahamwilliamson) NOPASSWD: /bin/launchctl print gui/501/com.user.claude-telegram
donna-broker ALL=(grahamwilliamson) NOPASSWD: /bin/launchctl kickstart -k gui/501/com.user.claude-telegram
donna-broker ALL=(grahamwilliamson) NOPASSWD: /bin/launchctl bootout gui/501/com.user.claude-telegram
donna-broker ALL=(grahamwilliamson) NOPASSWD: /bin/launchctl bootstrap gui/501 /Users/grahamwilliamson/Library/LaunchAgents/com.user.claude-telegram.plist
# additional lines, one per Tier 3 op, exact argv shape
```

Each Tier 3 op adds *exactly* one line. No wildcards. No patterns. Adding an op = visudo edit + sudoers test + commit. Cost: high friction per op. Benefit: extremely narrow blast radius if `donna-broker` is compromised.

**Rate limiting:**
- Per-capability counters in broker state (SQLite row, persisted across restarts).
- `ops.daemon.restart`: 3/hour, then exponential backoff (10m → 30m → 2h → alert).
- `ops.broker.reload_manifests`: 5/hour.
- Audit warning at 50% of limit. Telegram alert (via out-of-band path) at 100%.

**Auto-approve trust model:**
- Auto-approve does NOT mean Donna can call these at will from her main context.
- PreToolUse hook still routes through `donna-broker policy-check`.
- For Tier 3 auto-approve capabilities: broker writes audit row, skips Telegram approval round-trip.
- **Audit row is the trust artefact.** Every auto-approved op is fully logged and reviewable.

**Stuck detection (new infrastructure, lands with F.1):**
- Supervisor emits heartbeat to `~/.claude/channels/telegram/data/heartbeat.json` every 30s with timestamp + claude-bot pid.
- Broker watchdog timer (every 60s) checks heartbeat freshness.
- Missed beats > 5 min → trigger `ops.daemon.kill_stuck_session`
- Kill doesn't restore → escalate to `ops.daemon.restart`
- Restart fails → **broker sends Telegram alert directly using stored bot token, out-of-band path** (rare case — the broker holds a token specifically for alerting when its normal channel is broken)

### 4.5 Cross-cutting: Skill Scaffolding

**Purpose:** Reduce cost of adding a new actuation skill from days to ~1 hour.

**Components:**

- `donna-skill new <name> --category <cat> --executor <generic-executor>` — scaffolds a directory:
  - `manifest.yaml` stub (capability entry pre-populated)
  - `schema.json` stub (param schema scaffold)
  - `adapter.py` stub (optional — only if flow-control is needed)
  - `test_<name>.py` stub (param schema test, integration test, failure-mode test)
  - `review.md` stub (required review checklist — see below)

- **Generic executor library:**

  | Executor | Purpose | Used by tier |
  |---|---|---|
  | `http_request` | Structured HTTP fetch with basic auth, structured output | 2 |
  | `subprocess_with_creds` | Generalised Piece C path — binary + params + optional fd-3 creds entry | 2 |
  | `browser_read` | Playwright in `donna-explore` user, output-only | 1 |
  | `launchctl_op` | Narrow wrapper around sudoers carve-outs, string-keyed action enum | 3 |

- **Mandatory skill review checklist (in `review.md`):**
  - Risk tier justification
  - Revalidation semantics or `not_applicable` justification with reason
  - Threat model: what a compromised Donna could do with this skill
  - Test coverage: schema test, integration test, failure-mode test
  - Manifest reviewer signoff (Graham)

- **Testing infrastructure:**
  - `donna-skill test <name>` — runs the skill's tests in isolation
  - `donna-skill dry-run <name> --params <json>` — exercises schema + executor without external call (fixtures)

**Discipline guardrails:**
- Scaffolder doesn't auto-promote. New skills land in `proposed_skills/` (Tier 0).
- Generic executor changes go through code review like any broker change.
- Skills compose generic executors; no direct internal access.
- Review checklist is mandatory — `donna-skill promote` refuses without it.

---

## 5. Phasing & sequencing

| Phase | Scope | Days | Wall-clock | Risk |
|---|---|---|---|---|
| **F.0 Scaffolding** | `donna-skill new`, `http_request` + `subprocess_with_creds` executors, category taxonomy, sudoers spike | 1–2 | 1 wk | Low |
| **F.1 Tier 3 Ops** | Daemon restart/status/tail/kill, manifest reload, verify-vault wrap, `creds.add`, rate limiting, stuck detection, `launchctl_op`, sudoers carve-outs | 3–4 | 2 wk | Medium |
| **F.3 Tier 0 Discovery** | `donna-skill propose`, `proposed_skills/` lifecycle, review tooling, Monday review schedule | 1–2 | 1 wk | Low |
| **F.2 Tier 1 Exploration** | Brainstorm + threat model first, `donna-explore` user provisioning, logging proxy, `browser_read` executor, session lifecycle, output tagging | 6–8 | 3 wk | High |

**Why F.3 before F.2:**
- F.2 is the largest piece and benefits most from real proposals informing the design
- During F.1's work, Donna will hit gaps — if Tier 0 exists, those become proposals on the queue informing F.2's requirements
- F.3 adds two days of work but produces concrete artefacts for F.2's brainstorm

**Total estimate:** 11–16 working days. Wall-clock with day-to-day load: 6–8 weeks.

**Pre-requisite:** Piece C must land before Piece F starts. The fd-3 creds path is referenced by `subprocess_with_creds` and underpins every Tier 2 actuation skill.

---

## 6. Open questions (resolved in brainstorming, not now)

1. **Tier 1 allowlist scope.** Start with 5 well-known domains and expand via Tier 0? Or curated 20 at launch?
   *Lean: start at 5, expand via Tier 0 proposals (each new domain = a proposal Graham reviews).*

2. **Sandbox tech for v1.** Separate macOS user (recommendation) vs Lima VM (more isolation, more complexity)?
   *Lean: macOS user. Simpler trust model, no container-escape surface, native filesystem isolation. Revisit if a real Tier 1 incident exposes weakness.*

3. **Stuck-detection process placement.** Heartbeat watcher in supervisor itself (closer to problem) vs in broker (more independent)?
   *Lean: broker. The supervisor is the thing that gets stuck — having it watch itself is the wrong loop.*

4. **HMAC rotation approval shape.** Telegram-approvable vs manual-CLI-only?
   *Lean: manual CLI with `--magic-word` confirmation, not Telegram. Compromised Donna could race the approval; physical confirmation is the only defence.*

5. **Tier 1 between-session state.** Truly disposable (everything wiped) vs per-task persistent tier?
   *Lean: v1 fully disposable. Persistent tier is a Phase-3 idea pending real demand.*

6. **Skill-proposal noise threshold.** What's the discipline that keeps proposals high-signal?
   *Lean: proposal must cite either (a) two recurring requests, (b) structurally similar existing capability, or (c) a specific friction event. Otherwise rejected at scaffolding time.*

7. **Tier 3 categorisation in the manifest.** Do Tier 3 capabilities live in `capabilities.yaml` alongside Tier 2, or in a separate `ops-capabilities.yaml`?
   *Lean: separate file. Different approval semantics deserve a distinct review surface.*

---

## 7. Success criteria

This piece succeeds if, six months after F.2 lands:

- **Capability-add time** for new actuation skills drops from "days" to "≤1 hour for boilerplate + review time."
- **Self-healing recovery time** for the current outage class drops from "hours of dead daemon waiting for Graham to notice" to "<5 min auto-recovery or out-of-band alert."
- **Tier 1 usage is auditable:** every exploration session has a URL log Graham can review.
- **Tier 0 produces at least one promoted skill per month** without becoming a noise queue.
- **Tier 2 posture is measurably unchanged:** same approval semantics, same audit shape, same fd-3 path. Existing capabilities work identically.
- **No security incident attributable to Piece F surfaces.** Specifically: no Tier 1 exfil event, no Tier 3 auto-approve abuse, no Tier 0 malicious promotion.

---

## 8. Decision log seeds

To be transferred to the Notion living doc when F lands:

- **F-001:** Tier 1 raises the leak floor of a compromised Donna from zero to a finite audited surface. Conscious trade-off for capability fluidity.
- **F-002:** Tier 3 auto-approve is gated by audit, not Telegram. Audit row is the trust artefact for low-blast-radius ops.
- **F-003:** Generic executors are a shared trust surface. Fewer, deeper-reviewed, no shortcuts.
- **F-004:** Tier 0 is a proposal queue, not a deployment pipeline. Human approval on `promote` is the security check.
- **F-005:** Sudoers carve-outs are exact argv per op. No wildcards, no patterns. High friction per op is the feature.
- **F-006:** HMAC rotation is manual-CLI-only. Compromised Donna can race Telegram approval; physical confirmation is the only defence.

---

## 9. Living-doc page

Once this spec is approved and brainstormed, create a Notion living doc under Donna's Desk (parent `32d4dc8b-b6d8-81ea-9167-c8705113df16`) titled "Broker Tier Expansion — Piece F." Route via `donna-broker request` capability `notion.create_pages`. The page tracks ongoing architectural decisions during F.0–F.3 implementation and beyond.

---

## 10. Next action

When Piece C lands and Donna's daemon is healthy:

1. Run a real `superpowers:brainstorming` session on this spec.
2. Resolve the seven open questions in §6.
3. Produce four sub-piece specs (F.0, F.1, F.3, F.2 — in that order).
4. Each sub-piece runs the normal spec → plan → execute cycle.
