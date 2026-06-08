# Broker Standing Grants — scoped approve-once autonomy

**Date:** 2026-06-08 · **Status:** spec → build · **Extends:** security-v1.1 (the broker
policy/approval core). This is a *spec'd extension*, not a bolt-on — the broker's
guarantees (purity, param-binding, audit, no-self-escalation) must continue to hold.

## 1. Problem

The broker today decides by **risk tier** (`policy_check_mode`): `low` → auto-allow,
`medium`/`high` → interactive approval **every time**, `blocked` → deny. That's too
coarse for recurring trusted actions. Concretely: the weekly school-email roundup can
only ever land as a **draft**, so drafts pile up unsent all week and Graham has to
send each one by hand. He wants to **approve once** and let Donna **send** that one
specific thing on schedule — **without** granting "Donna can send any email."

## 2. Goals / non-goals

**Goals**
- A **scoped, revocable, expiring standing grant**, held + enforced **by the broker**,
  that lets a *specific* (capability + pinned params) action auto-execute (skip the
  per-run approval) up to a rate limit.
- Creating/widening a grant is a **high-trust, human-approved** act (meta-privilege).
- Narrow by construction: a grant to "send the roundup to *you*" can **never** be used
  to send other mail to other people.
- A new `gmail.send` capability, usable only under a grant or per-action approval.

**Non-goals**
- No blanket / wildcard "send any email" grants.
- The **agent can never create or widen a grant** (only *request* + *revoke*).
- No change to how `low`/`blocked` tiers behave; no network in the policy hot-path.

## 3. Threat model & invariants (must all hold)

1. **No self-escalation.** `grant.create` is itself a capability that is **high-risk,
   always interactive-approval, and can NEVER be covered by a standing grant** (grants
   cannot grant grants). The LLM/agent path can call `request` but the *human* must
   approve the grant via the normal approval code flow.
2. **Param-binding.** A grant pins the sensitive params (`to`, and a `subject` prefix
   for sends). On each request the candidate params are canonicalised and checked
   against the grant's **HMAC-bound** constraint set (reuse security-v1.1
   canonicalize.py + the existing param-binding). Any unpinned-but-sensitive deviation
   → no match → falls through to approval.
3. **Bounded blast radius.** Every grant has: a **rate limit** (e.g. ≤1/week), an
   **expiry** (default 90d, max 365d), and is **revocable instantly** from the app.
4. **Auditable.** Every grant-matched auto-execution is written to the audit log with
   the matching `grant_id`; grants list/created/revoked are audited too.
5. **Purity preserved.** `policy_check_mode` stays pure/deterministic/local — it reads
   the grant store from a local SQLite file, no network, no clock-dependent logic
   except a passed-in `now` (for expiry/rate, supplied by the caller, testable).
6. **Asymmetry.** Granting = high friction (human approval). **Revoking = always
   allowed**, even by the agent (revocation only ever *reduces* privilege).

## 4. The grant model

Stored in a broker-owned table `standing_grants` (in the broker's requests DB, perms
root:wheel / donna-broker only — never writable by the app user directly):

```
standing_grant {
  id            TEXT  PRIMARY KEY        -- uuid
  capability    TEXT  NOT NULL           -- e.g. "gmail.send"
  constraints   TEXT  NOT NULL           -- canonical JSON of pinned params (see §5)
  constraints_mac TEXT NOT NULL          -- HMAC of (capability + constraints), broker key
  purpose       TEXT  NOT NULL           -- human label, shown at approval + in app
  max_per_period INTEGER NOT NULL        -- rate limit count
  period_seconds INTEGER NOT NULL        -- rate limit window (e.g. 604800 = 1 week)
  created_at    TEXT  NOT NULL
  expires_at    TEXT  NOT NULL
  approved_via  TEXT  NOT NULL           -- the approval_code that authorised this grant
  revoked_at    TEXT                      -- NULL = active
}
```
Plus `grant_uses(grant_id, used_at)` for rate accounting.

## 5. Constraint semantics (how "pinned" works)

`constraints` is a canonical JSON object describing required param values:
- **exact pins** for sensitive fields — e.g. `{"to": "graham@…"}`. A request matches
  only if its `to` canonicalises identically.
- **prefix pins** for `subject` — `{"subject": {"prefix": "School roundup"}}`.
- Fields **not** listed are free to vary (e.g. `body`).
- For a `gmail.send` grant, `to` is **mandatory** in constraints (no unpinned
  recipient is ever auto-sendable). The validator rejects a `gmail.send` grant whose
  constraints omit `to`.

`constraints_mac = HMAC(broker_key, capability ‖ canonical(constraints))` so the stored
constraints can't be tampered with out-of-band.

## 6. policy_check_mode integration

New step, before the risk-tier fallthrough, in `policy_check_mode(capability, params, now)`:
```
for g in active_grants(capability, now):            # active = not revoked, not expired
    if constraints_match(g.constraints, params)     # exact/prefix per §5
       and verify_mac(g)                            # tamper check
       and within_rate(g, now):                     # §3.3
        record_use(g, now)
        return {decision: "allow", via: "standing_grant", grant_id: g.id, risk_level: <tier>}
# else: existing behaviour (low→allow, medium/high→approval, blocked→deny)
```
Determinism: `active_grants`/`within_rate` take `now` as an argument; no wall-clock in
the pure function. (The caller passes `now`; tests pin it.)

## 7. Lifecycle & new CLI modes (add to `MODES` in main.py)

- **`grant-create`** — the meta-approval. Payload `{capability, constraints, purpose,
  max_per_period, period_seconds, expires_in_days}`. It does **not** create the grant
  immediately; it raises a normal **approval_required** (a code) describing the *full
  scope* in plain English ("Allow: send email to graham@… , ≤1/week, expires 90d, for
  'School roundup'"). Only when the human approves that code (existing `execute` path,
  specialised) is the grant persisted. **`grant.create` is hard-coded high-risk and is
  excluded from §6 grant-matching** (no grant can authorise creating grants).
- **`grant-list`** — returns active + expired grants (for the app).
- **`grant-revoke`** `{grant_id}` — sets `revoked_at`; **always allowed**, audited.

The agent (via `broker_bridge`) may call `request` (normal) and `grant-revoke`. It may
**propose** a grant (surface a `grant-create` request for the human), but the human
must approve it — same as any approval, just labelled as a permission grant.

## 8. New capability: `gmail.send`

- `manifests/capabilities.yaml`: `name: gmail.send`, MCP tool
  `mcp__claude_ai_Gmail__send_message` (or send via draft+send), risk **high**,
  `$ref: ./schemas/gmail_send.json` (params: `to`, `subject`, `body`, optional
  `thread_id`). High-risk ⇒ always approval **unless** a matching standing grant.
- Executor mirrors `gmail.create_draft`'s executor pattern.

## 9. Audit & observability

- `audit.py`: log `grant.create.proposed`, `grant.created`(with scope), `grant.revoked`,
  and on each auto-exec `policy.allow.standing_grant`(grant_id, capability, canonical
  params hash). The app's Activity feed shows auto-sent items ("Sent: School roundup —
  via standing grant").

## 10. Rollout / tests (TDD — extend the broker's existing test suite)

- `test_policy.py`: constraints_match (exact + prefix + free fields); MAC verify
  pass/fail; rate-limit window (allow N, deny N+1, allow after window); expiry; revoked
  ⇒ no match; **grant.create never matched by a grant**.
- `test_main.py`: grant-create → approval_required (not persisted yet) → execute(code)
  persists; grant-list; grant-revoke; a `gmail.send` request that matches a grant →
  allow/auto; non-matching `to` → approval_required.
- Determinism: pass fixed `now`; assert pure.
- Migrations: create tables if absent; no change to existing rows/flows.

## 11. App-side (built on top, separate from broker core)

- `broker_bridge`: add `grant_create(...)` (→ proposes, returns approval code),
  `grant_list()`, `grant_revoke(id)`. (`grant_create`/revoke reachable from the human
  approve path / app; the LLM path may only *propose* a grant, never approve it.)
- Donna backend: `/api/donna/grants` (list/revoke), grant approval reuses the approvals
  surface (a grant shows as a distinct "permission" approval). Scheduler/runner + the
  school-roundup job sit on top, unchanged by this spec.

## 12. Why the broker, not Donna (the core principle)
The grant is a **policy object the broker owns and enforces**. Donna can ask for one
and use it, but cannot create, widen, or forge one. Approve-once autonomy with a hard,
auditable, revocable ceiling — and zero path for the agent to escalate itself.
