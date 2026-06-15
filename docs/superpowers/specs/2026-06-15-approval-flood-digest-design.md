# Approval Flood Fix + Digest UI — Design

**Date:** 2026-06-15
**Status:** Approved (design); ready for implementation plan
**Repos touched:** `donna` (bridge — Layers 1 & 2), `daru` (routine engine — Layer 3)

## Problem

On 2026-06-14 Graham received 100+ approval prompts on Telegram, mostly for
exercise calendar events, many of them duplicates.

The broker database is authoritative and shows the truth: only **8 real
approval requests** existed that day, all `gcal.create_event`, created in two
bursts (3 at 21:48:33, 5 at 22:29:23–22:29:46) and all left to expire
unactioned. So 8 genuine requests were rendered as 100+ Telegram messages.

### Root causes (evidence-backed)

1. **Replay on restart (the multiplier — `donna/claude-telegram-hardened/donna_broker.ts`).**
   The "already sent this" guard is an in-memory `Set` (`seenRequestIds`,
   `donna_broker.ts:360`). Queue files are deleted only when a response is
   recorded — i.e. on approve/deny (`deleteQueueFile`, `donna_broker.ts:311`) —
   never when an approval is ignored or expires. The code comment at
   `donna_broker.ts:305` states the consequence outright: *"any queue file that
   survives a restart gets re-sent."* The system restarts frequently
   (supervisor, context watchdog, auto-updates — see `donna/CLAUDE.md`), so each
   restart re-blasted every still-in-window approval. 8 × ~13 restarts ≈ 100+.
   Confirmed corollary: 18 orphaned queue files dating to 23 April still sit
   uncleaned in `/Users/donna-broker/.config/donna/approval-queue/`.

2. **Routine over-proposing (`daru/api/daru/scheduler.py`).** The
   "Plan the week's exercise" routine (`routine:260`) fired ~twice within 40
   minutes, emitting 8 separate `gcal.create_event` proposals instead of one
   batch. Each request keys idempotency on *creation date*
   (`idempotency_date_from: created_utc`), so re-runs on the same day produce
   fresh, non-deduped requests.

3. **No batching in the UI.** Every approval is its own Telegram message with
   its own buttons. Even without the bugs, a legitimate 8-session week would be
   8 separate pings.

### What it is NOT

The flood did not pass through the Claude Telegram daemon (`messages.db` shows
no flood on any day). It came through the **separate donna-broker bot**, which
sends via its own token and is not logged locally. The broker's
approval-execution gate is sound — this is a delivery/dedup and UX problem, not
a security-gate problem.

## Constraints

- **The approval execution gate stays in Telegram.** The app proposes and
  mirrors; it never executes (per project memory `no-telegram`). The digest
  therefore lives in Telegram.
- **Minimal change to the security-critical broker.** The broker's HMAC,
  idempotency, and state machine are unchanged. Batching is a presentation
  concern handled by the bridge; the broker still approves/executes one code at
  a time.

## Design

Three layers. Layer 1 is the bug fix and is sufficient on its own to stop the
flood; Layers 2 and 3 deliver the better UX Graham asked for.

### Layer 1 — Stop the replay (`donna_broker.ts`)

- **Persistent sent-marker.** Replace the volatile `seenRequestIds` Set with a
  durable marker that survives restart. On successful send, move the queue file
  into a `sent/` subdirectory (atomic `rename`). On poll, files under `sent/`
  are not re-rendered. `deleteQueueFile` is updated to resolve a request's file
  in either the root or `sent/`.
- **Expiry sweep.** On each poll, delete any queue file (root or `sent/`) whose
  `expires_at < now`. Run once at startup to clear the existing April orphans.
- **Outcome:** a bridge restart no longer re-sends in-window approvals; 8
  requests yield 8 sends, not 100+.

### Layer 2 — Digest UI (`donna_broker.ts` + queue-file schema)

- **Schema additions (optional fields):** `batch_id: string` and
  `batch_label: string` on the queue file. Absent → today's single-message
  behaviour is preserved exactly.
- **Grouping at send time.** On poll, unsent queue files sharing a `batch_id`
  are rendered as **one** digest message:

  ```
  🔐 Daru wants to add 5 sessions to your calendar (weekly exercise plan):
    • Mon 07:00  Run
    • Tue 18:00  Gym
    • Wed 07:00  Run
    • Fri 18:00  Gym
    • Sun 09:00  Long run

  [✓ Approve all] [Review each] [✕ Dismiss all]
  ```

- **Callbacks (reuse the existing per-code path):**
  - **Approve all** → loop the batch's codes through the current single-code
    approve flow, writing one response per code. **Offered only when every item
    in the batch is the same capability and that capability is low/medium-risk.**
    Mixed-capability or high-risk batches (e.g. `everyone_active.checkout`)
    render with **Review each** only — no one-tap path.
  - **Dismiss all** → deny/cancel each code in the batch.
  - **Review each** → expand the batch into the current per-item messages
    (one message + buttons per code) and do not re-collapse.
- **The broker is untouched.** Each underlying approve/deny is still one code,
  one HMAC verification, one state transition.

### Layer 3 — Reduce noise at the source (`daru` routine engine)

- The weekly-exercise routine proposes its whole week as **one batch**: every
  proposed `gcal.create_event` for that run shares a `batch_id` and a human
  `batch_label` ("weekly exercise plan"), threaded through `broker_bridge.request`.
- **Idempotent per (routine, ISO-week).** A re-run within the same ISO week
  updates/no-ops the existing batch rather than emitting a second one. This
  closes the 21:48 + 22:29 double-fire.

## Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `donna_broker.ts` queue lifecycle | sent-marker, expiry sweep | filesystem queue dir |
| `donna_broker.ts` digest renderer | group by `batch_id`, render digest vs single | queue files, capability risk metadata |
| `donna_broker.ts` callback handler | fan out Approve-all / Dismiss-all over batch codes | existing single-code approve/deny path |
| `daru` routine engine | emit one labelled batch per week, idempotently | `broker_bridge.request` (batch fields) |

## Error handling

- **Partial fan-out failure on Approve all.** If approving code _k_ of _n_
  fails, continue the rest and report a per-item result summary back to the
  chat; never silently drop one.
- **Expired item inside a live batch.** The expiry sweep may remove an item
  between render and tap. Approve-all skips codes no longer pending and notes
  the skip in the summary.
- **Capability risk lookup unavailable.** Fail safe: if risk level can't be
  resolved, treat the batch as not eligible for Approve-all (Review each only).

## Testing

- **Layer 1:** simulate a restart (clear in-memory state, re-poll) with
  in-window queue files present → assert zero re-sends. Expiry sweep removes
  past-`expires_at` files and leaves live ones.
- **Layer 2:** grouping renders one digest for shared `batch_id`; single
  request unchanged; Approve-all fans out to N response files; mixed/high-risk
  batch suppresses Approve-all; Review-each expands without re-collapsing.
- **Layer 3:** one routine run emits one batch with shared `batch_id`; a second
  run in the same ISO week emits no new batch.

## Out of scope

- Moving the approval gate out of Telegram.
- Changing the broker's idempotency/HMAC/state machine.
- App-side attention-feed mirroring of approvals (separate, reserved work).
