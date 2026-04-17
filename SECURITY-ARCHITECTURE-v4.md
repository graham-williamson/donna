# Donna — Security Architecture v4

Supersedes v3 (`~/.claude/plans/mutable-crafting-quill.md`). v4 folds in the review findings and reframes the Telegram approval path around the existing `claude-telegram-hardened` MCP server, which already provides every primitive the approval bot needs. v4 is a spec — precise enough to build from without re-opening design questions mid-implementation.

---

## 0. What changed from v3

**Approval bot is not a new process.** v3 described a standalone long-running Telegram bot. v4 extends the existing `/Users/grahamwilliamson/donna/claude-telegram-hardened/` MCP server — which already has sender-ID-verified access control, outbound `reply`/`ask_user` tools with inline buttons, SQLite+WAL, signal-file IPC, and a launchd-supervised supervisor with crash recovery. Building a second bot is duplication; v4 uses what's there.

**Spec-level gaps closed.** Params canonicalisation, HMAC key management, approval-code collision handling, approval bot liveness, mandatory revalidation for medium+ risk, session-startup behaviour, denial cooldown — all now pinned down, not "figure it out during build".

**Design decisions resolved.**
- `gmail_create_draft`: medium risk (unchanged)
- Broker v1 = per-invocation CLI (defer daemon until post-benchmark)
- Notion writes: approval must show char count + 200-char body excerpt
- Post-approval: telegram channel posts "ready to execute" nudge back to Donna
- Session start: Donna calls `list-pending` every fresh conversation

**Realistic Phase 0 estimate:** 3–4 hours, not 1.

Everything else in v3 (trust model, two-step commit, SQLite state, idempotency on `(capability, params, date)`, params-hash matching, "approval never executes" as a firm rule, Playwright removed, age-encrypted credentials, hash-chained audit) is unchanged and load-bearing. This document restates what's necessary to execute, not what's already decided.

---

## 1. Trust model (brief)

- **Donna's Claude process is untrusted.** She is subject to prompt injection from email bodies, Notion pages, calendar invites, and Telegram messages. Any capability that Donna can invoke directly, an attacker can invoke.
- **The broker is the trust boundary.** It runs as a separate OS user (`donna-broker`), owns all credentials, validates inputs against a capability manifest, and is the only path to risky actions.
- **Graham (via his Telegram sender_id) is the approver.** No other identity can approve.
- **Silent failure is the enemy.** Every denial, expiry, stale check, or channel outage must produce a visible error Donna or Graham sees — never a no-op.

---

## 2. Approval model: two-step commit

Because Claude Code's `--dangerously` Channels mode cannot reliably carry a long-blocking subprocess or resume a suspended MCP tool call, approval is **asynchronous and state-based**, not a synchronous handshake.

**Propose → Approve → Execute.** Three interactions, three distinct state transitions, all persisted in SQLite. The broker never blocks waiting for Graham; every broker invocation returns in under 1 second.

**Approval never executes.** A Telegram `/approve` or inline-button tap changes the request state to `approved` and nothing else. Execution requires a deliberate second action — either Donna re-invoking the MCP tool (for MCP capabilities) or Donna running `execute` (for browser capabilities). This separation is the core safety property. Do not add auto-execute-on-approval at any risk level, in any future phase.

---

## 3. Request state model

States: `created` → (`auto_approved` | `pending_approval`) → (`approved` | `denied` | `expired`) → `executing` → (`succeeded` | `failed`).

Stored in SQLite at `/Users/donna-broker/.config/donna/requests.db` in WAL mode.

**Schema** (SQL):

```sql
CREATE TABLE requests (
  request_id TEXT PRIMARY KEY,
  capability TEXT NOT NULL,
  params_json TEXT NOT NULL,          -- canonicalised (see §4.1)
  params_hash TEXT NOT NULL,          -- sha256 of canonical params_json
  idempotency_key TEXT NOT NULL,      -- see §4.2
  resolved_summary TEXT NOT NULL,     -- human-readable, surfaced in Telegram
  context_reason TEXT,                -- why Donna submitted it
  risk_level TEXT NOT NULL,           -- low | medium | high
  state TEXT NOT NULL,
  approval_code TEXT,                 -- base32, unique among non-terminal rows
  approval_hmac TEXT,                 -- hmac(request_id || params_hash || expires_at)
  created_at INTEGER NOT NULL,        -- epoch ms
  approval_expires_at INTEGER NOT NULL,
  execution_expires_at INTEGER,       -- set on approval
  approved_at INTEGER,
  executed_at INTEGER,
  result_json TEXT,                   -- redacted
  error_code TEXT,
  error_message TEXT,                 -- redacted, no stack traces
  prev_audit_hash TEXT                -- links to audit chain
);
CREATE UNIQUE INDEX idx_approval_code_active
  ON requests(approval_code)
  WHERE state IN ('pending_approval','approved');
CREATE INDEX idx_idempotency_active
  ON requests(idempotency_key)
  WHERE state NOT IN ('denied','expired','failed');
CREATE INDEX idx_state ON requests(state);

CREATE TABLE rate_limits (
  capability TEXT NOT NULL,
  date_utc TEXT NOT NULL,             -- YYYY-MM-DD
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (capability, date_utc)
);

CREATE TABLE broker_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- e.g. ('paused', 'global') or ('paused', 'puregym.*')
```

The unique partial index on `approval_code` enforces collision-free code assignment among non-terminal rows — broker regenerates on constraint violation.

**Expiry windows** (unchanged from v3): gym 2h/1h, email 24h/12h, calendar 4h/2h, notion 8h/4h, default 2h/1h. Checked lazily on every access and eagerly by a 5-minute cleanup in the broker (triggered opportunistically on any broker invocation, not a separate cron).

---

## 4. Cryptographic and storage specification

### 4.1 Params canonicalisation

Use **RFC 8785 (JSON Canonicalization Scheme / JCS)**. Python implementation: `pip install rfc8785` (Trail of Bits maintained). Broker module `canonicalize.py` wraps it; all `params_hash` computations go through this single function.

Canonical form: sorted object keys, no insignificant whitespace, numbers normalised per ECMAScript `ToString`, UTF-8 encoding. Any other library's "canonical JSON" is not acceptable — pin RFC 8785 exactly.

### 4.2 Idempotency key

```
idempotency_key = sha256(
  capability || "\x1f" ||
  canonical_params_json || "\x1f" ||
  date_component
)
```

`date_component` is determined by the capability manifest:

```yaml
capabilities:
  - name: puregym.book_class
    idempotency_date_from: params.date       # class date
  - name: gmail.create_draft
    idempotency_date_from: created_utc        # today (UTC)
  - name: gcal.create_event
    idempotency_date_from: params.start.date  # event date
```

If `idempotency_date_from` points into params and the field is missing, manifest validation fails. Timezone is always UTC; date format `YYYY-MM-DD`.

Same idempotency key + non-terminal state → return existing request, don't duplicate. Same key + `succeeded` → return cached result. Same key + terminal-not-success → allow a fresh request (see §4.3 for denial cooldown).

### 4.3 Approval code + HMAC

**Code format:** 6 characters from RFC 4648 base32 alphabet excluding I, L, O, U (i.e., `A-H J K M N P-T V-Z 2-7`). ~30 bits of entropy, no visual confusions, easy to type on a phone. Example: `A2B7KQ`.

**Uniqueness:** enforced by SQL unique partial index (§3). On `INSERT OR ABORT` collision, broker regenerates up to 5 times; if still colliding (implausible in practice), fails with internal error.

**HMAC:**

```
approval_hmac = hmac_sha256(
  key = contents of /Users/donna-broker/.config/donna/hmac.key,
  msg = request_id || "\x1f" || params_hash || "\x1f" || str(approval_expires_at)
)
```

HMAC is not sent to Telegram. It's stored alongside the record and verified on every state transition (approve, deny, execute). If the stored record has been tampered with (params, expiry, or id changed in SQLite), HMAC verification fails and the request is rejected.

**HMAC key file:** 32 bytes from `openssl rand -base64 32`, written to `/Users/donna-broker/.config/donna/hmac.key`, mode `0400`, owner `donna-broker`. Never logged, never copied, never backed up to Notion or cloud. Rotation (`donna-broker rotate-hmac`) invalidates all non-terminal requests and is a manual-only operation.

### 4.4 Denial cooldown

After `/deny`, the idempotency key is blocked for **30 minutes** (capability-configurable). During cooldown:

```json
{"status":"cooldown","retry_after_seconds":1623,
 "reason":"This action was denied 7 minutes ago. It can be re-requested in 27 minutes, or Graham can tell Donna to override explicitly."}
```

This prevents Donna from re-proposing the same denied action immediately, which would feel like nagging. Override: Graham can Telegram `/override <idempotency_prefix>` to clear the cooldown.

### 4.5 SQLite durability

- `PRAGMA journal_mode=WAL` at database creation.
- Daily snapshot via `sqlite3 .backup` run from broker (triggered opportunistically once per UTC day on any invocation), written to `/Users/donna-broker/.config/donna/backups/requests-YYYY-MM-DD.db`. Retain 14 days.
- Retention: rows in terminal states (`succeeded`/`failed`/`denied`/`expired`) are migrated to `requests_archive` table after 30 days, hard-deleted after 180 days.

### 4.6 Audit log rotation

- Format: JSONL at `/Users/donna-broker/audit/audit.log`, mode `0600`, owner `donna-broker`.
- Each entry includes `prev_hash = sha256(previous_entry_canonical_json)`. Entry ordering via monotonic counter + epoch ms.
- Rotate on 100MB or 30 days (whichever first). Current file renamed to `audit-YYYY-MM-DD-NNN.log.sealed`; final entry in sealed file is `{"event":"segment_seal","segment_end_hash":"<sha>"}`. New file's first entry uses `prev_hash = segment_end_hash`.
- `donna-broker verify-audit` walks sealed archives + current file as a single chain and reports the line/entry of the first break.

---

## 5. Telegram integration — extending `claude-telegram-hardened`

v4 piggy-backs on the existing MCP server at `/Users/grahamwilliamson/donna/claude-telegram-hardened/`. No new long-running process. All additions live inside `server.ts` and a new skill directory.

### 5.1 Why this works

`claude-telegram-hardened/server.ts` already provides:

- **Sender authentication** via `access.json` (§647–768 of server.ts). Only Graham's `allowFrom` sender_id reaches `handleInbound`.
- **Outbound messaging** via the `reply` MCP tool (§1274–1359) with MarkdownV2 auto-escape and chunking.
- **Inline-button prompts** via the `ask_user` MCP tool (§1395–1436) with callback handling and 120-second default timeout.
- **Signal-file IPC** pattern — already used for `restart.signal` and `approved/` directory.
- **SQLite WAL storage** — proven pattern, reusable schema idiom.
- **Supervision** via `supervisor.ts` with launchd, crash recovery with exponential backoff, and context watchdog.

Building a parallel bot would duplicate every one of these. v4 extends server.ts instead.

### 5.2 Directory contract between broker and server

Two directories under the broker's home, both owned `donna-broker:staff`, mode `0770` (broker writes, server reads and writes responses):

```
/Users/donna-broker/.config/donna/approval-queue/       # broker writes, server reads
  req_abc123.json                                       # approval request payload

/Users/donna-broker/.config/donna/approval-responses/   # server writes, broker reads
  req_abc123.json                                       # {"decision":"approve"|"deny","actor_id":"...","ts":...}

/Users/donna-broker/.config/donna/telegram-heartbeat    # server writes every 30s
```

The server joins group `staff` for this shared access, or alternatively the broker creates a dedicated `donna-bridge` group. Exact permission mechanics settled in Phase 1 setup — the contract above is the interface.

### 5.3 Minimal additions to server.ts

One new watcher, one new regex handler, one heartbeat writer, one internal helper. Estimated <250 LoC.

**Approval queue watcher:** `fs.watch` on `approval-queue/`. On new file:
1. Read `{request_id, summary, code, expires_at, chat_id, risk_level, resolver_extras}`.
2. Format Telegram message (see §5.4 for content rules).
3. Call internal `ask_user` equivalent to post with `Approve` / `Deny` inline buttons bearing payload `approve:<code>` / `deny:<code>`.
4. Also accept text fallback — `/approve <code>` and `/deny <code>` (see handler below).
5. On tap or text match: write `approval-responses/<request_id>.json` atomically (`.tmp` + rename).
6. Acknowledge to Graham: `✓ A2B7KQ approved. Donna will execute it on her next turn.`
7. Post a short message to Donna's chat (same Telegram thread): `✓ req A2B7KQ approved — ready to execute`. This is the **post-approval nudge**; it arrives as a channel notification on Donna's next turn and prompts her to call `execute` or re-attempt the MCP tool.

**Text-message command regex:** extend the existing text handler with:

```regex
^/(approve|deny|override)\s+([A-HJKMNP-TV-Z2-7]{6})\s*$
```

Handler flow:
1. Verify `ctx.from.id` is Graham's canonical sender_id from `access.json` `allowFrom`. Reject silently otherwise — do not leak code existence to unauthorised senders.
2. Find the request in SQLite by `approval_code` via a read-only broker call (`donna-broker status-by-code <code>` — new sub-mode; no state mutation).
3. If found and state is `pending_approval`: write `approval-responses/<request_id>.json`, reply to Graham with confirmation.
4. If not found, expired, or wrong state: reply with the specific reason. Never fall through silently.

**Heartbeat:** `setInterval(() => writeFileSync(heartbeatPath, Date.now().toString()), 30_000)` on server startup.

**Broker liveness check:** on every `request` or `policy-check`, broker reads `telegram-heartbeat` mtime. If > 90 seconds stale and the request needs approval, broker returns:

```json
{"status":"channel_unavailable",
 "reason":"Telegram approval channel is not running. Start it with /telegram:daemon start before attempting this action."}
```

Low-risk requests (auto-approved) are not affected by channel liveness — they don't need Graham.

### 5.4 Approval message content rules

Every approval prompt must contain:

1. The 6-character code (bold).
2. The resolved human-readable summary (not raw params, not opaque IDs).
3. `context_reason` verbatim from the request (Donna's stated reason).
4. The capability name in `code font` (so Graham can tell `gmail.create_draft` from `gmail.send`).
5. Expiry remaining, rounded to minutes.

Capability-specific additions (enforced by the resolver in `resolver.py`; manifest validation fails without them):

- **Notion writes (`notion-create-pages`, `notion-update-page`):** character count of body + first 200 characters of body. If the body is < 200 chars, show all. Mention explicitly that Notion writes are an exfiltration channel concern.
- **Email drafts:** to-count, cc-count, bcc-count, subject verbatim, first 200 chars of body.
- **Calendar events:** start/end in local time, invitee list (emails), location.
- **Gym bookings:** class name, instructor, gym, date/time, spaces remaining at resolve-time.

### 5.5 New skill: `/telegram:approval`

At `/Users/grahamwilliamson/donna/claude-telegram-hardened/skills/approval/SKILL.md`. Commands:

- `/telegram:approval list` — wraps broker `list-pending`, prints table.
- `/telegram:approval approve <code>` — terminal-side approval for when Telegram is unreachable (e.g., Graham's phone is dead). Writes `approval-responses/` the same way the bot does; actor_id field set to `terminal:<os_user>`.
- `/telegram:approval deny <code>` — symmetric.
- `/telegram:approval status <code>` — wraps broker `status-by-code`.

Skill follows the existing `/telegram:access` pattern. Not invocable from Telegram messages (same injection-resistance reasoning).

### 5.6 What Telegram is NOT (unchanged)

- Not an execution trigger. Approval changes state; it never causes the action to run.
- Not a way to send commands that bypass the broker.
- Not a resume mechanism. There are no suspended tool calls.

No `/approve-and-execute` shortcut in v4, v5, or any near-future version. This is the line.

---

## 6. Control flows

### 6.1 Browser capability — `puregym.book_class`

1. **Propose.** Donna runs `donna-broker` in `request` mode with capability + params + reason. Broker validates, resolves params to a human summary (calls internal `puregym.list_classes` to enrich), generates code, writes row (`pending_approval`), writes `approval-queue/<request_id>.json`, returns to Donna in <1s with `{status:"approval_required", code, summary, expires}`.
2. **Approve.** Server picks up queue file, sends inline-button prompt to Graham. Graham taps Approve. Server writes `approval-responses/<request_id>.json`, acknowledges to Graham, nudges Donna's chat.
3. **Execute.** Donna (on her next turn, prompted by the nudge) runs `donna-broker execute request_id=...`. Broker reads response file, confirms state is `approved` and within execution window, runs revalidation (`puregym.check_session` + "is this class still bookable"), then spawns the executor subprocess with decrypted credentials via age. Executor returns result; broker redacts, records, returns to Donna.

### 6.2 MCP capability — `gmail.create_draft`

1. **First attempt blocks.** Donna calls the MCP tool. PreToolUse hook invokes `policy-check`. Broker: no matching approved request, creates pending, returns `{decision:"block", code, summary}`. Hook blocks with structured reason.
2. **Graham approves** via Telegram (inline button or `/approve CODE`).
3. **Re-attempt.** Donna calls the exact same MCP tool with exact same params. Hook invokes `policy-check`. Broker matches by `(tool, params_hash)` against approved non-expired rows, returns `{decision:"approve"}`. Hook allows. Tool executes. PostToolUse hook calls `audit-result`; broker transitions to `succeeded`.

**Mutated params:** different `params_hash` → no match → new pending request. Approval of the old one does not cover the new one.

**Never re-attempted:** approved request hits execution window expiry, moves to `expired`. No action, no harm.

### 6.3 Denied, expired, stale, cooldown

- **Denied:** terminal. Same idempotency key enters 30-minute cooldown. Re-request during cooldown returns `cooldown` error.
- **Expired (approval):** terminal. Fresh request allowed immediately.
- **Expired (execution):** terminal. Fresh request allowed immediately.
- **Stale on execute:** revalidation fails (class full, slot taken, inbox state changed). Broker returns `stale`; request moves to `failed` with reason. Fresh request allowed.

---

## 7. Broker interface

### 7.1 Modes

| Mode | Gated by pause? | Purpose |
|---|---|---|
| `request` | yes | Donna creates a new capability request |
| `policy-check` | yes (returns block w/ reason=paused) | PreToolUse hook asks permission for an MCP call |
| `execute` | yes | Donna runs an approved request |
| `status` | no | Check a single request's state |
| `status-by-code` | no | Lookup by 6-char approval code (used by telegram server) |
| `list-pending` | no | Donna sees approved-but-not-executed + pending_approval |
| `list-recent` | no | Last N completed/denied/expired (replaces system.recent_actions) |
| `cancel` | no (cancelling is always safe) | Cancel a `pending_approval` request |
| `audit-result` | no | PostToolUse hook logs execution outcome |
| `rotate-hmac` | — | Manual, invalidates all non-terminal |
| `verify-audit` | no | Chain integrity check |

All modes accept JSON on stdin, emit JSON on stdout. Errors are structured: `{status, error_code, message}`. Never stack traces.

### 7.2 Rate limits

Per-capability daily cap lives in the `rate_limits` table (§3). Increment happens at `request` creation time, not execution. A denied or expired request **does not refund** the counter — this prevents Donna from burning through capacity by proposing bad actions. Counter resets at UTC midnight.

### 7.3 Pause scope

- `/stop` globally: `request`, `policy-check` (returns block), `execute` all rejected.
- `/stop <namespace>` (e.g., `/stop puregym`): only capabilities matching `<namespace>.*` are gated; others unaffected.
- Read modes (`status`, `list-pending`, `list-recent`, `audit-result`, `verify-audit`) are never gated. Graham must be able to see state during pause.

### 7.4 Revalidation

Mandatory for any capability with `risk_level: medium | high`. Manifest must declare:

```yaml
revalidate:
  handler: puregym.check_class_bookable
  arguments: [class_id, date]
```

Manifest validation (`donna-broker validate-manifest`) refuses to load a manifest where any medium/high capability lacks `revalidate`. This is enforced at broker startup; no way to disable without editing broker source.

### 7.5 Hook contracts

- PreToolUse hook invokes broker with a 5-second timeout. Broker unreachable → hook blocks with `{"decision":"block","reason":"broker unavailable, cannot verify policy"}`. Never allow on broker failure.
- PostToolUse hook invokes broker with a 2-second timeout. Broker unreachable → hook logs to a local fallback file `/Users/grahamwilliamson/donna/.claude/audit-fallback.log` and returns (never blocks).

---

## 8. Audit events

Full list unchanged from v3 §7: `request_created`, `request_auto_approved`, `request_approved`, `request_denied`, `request_expired`, `request_cancelled`, `request_execution_started`, `request_execution_succeeded`, `request_execution_failed`, `mcp_tool_allowed`, `mcp_tool_blocked`, `broker_paused`, `broker_resumed`, `debug_session_start`, `rate_limit_hit`.

**Explicitly not logged:** credential values, full email body, full Notion page body, raw MCP responses, approval codes in plaintext (stored as HMAC), params in plaintext (hash only; resolved summary is logged), stack traces, screenshot bytes, HMAC key contents, Telegram bot token.

**Resolved summaries are logged** (they're the auditable record of what was actually proposed).

---

## 9. Hook model

Identical to v3 §6, with two refinements:

**Unconditional blocks (PreToolUse):**
- Any `mcp__plugin_playwright_*` tool.
- Any `Bash` command not matching the allowlist:
  - `sudo -u donna-broker /usr/local/bin/donna-broker` (broker calls)
  - Read-only bash primitives: `ls`, `cat <path>` on non-sensitive paths, `git status`, `git log`, `git diff`.
- Any MCP tool not present in `mcp-tools.yaml` (default-deny on unknown).

**Risk tier for MCP tools** (in `mcp-tools.yaml`):

| Tool | Risk | Notes |
|---|---|---|
| `gmail_search_messages`, `gmail_read_message`, `gmail_read_thread`, `gmail_list_drafts`, `gmail_get_profile`, `gmail_list_labels` | low | reads |
| `gmail_create_draft` | medium | doesn't send, but content exfil concern; keep at medium for now |
| `gcal_list_*`, `gcal_get_*`, `gcal_suggest_time` | low | reads |
| `gcal_create_event`, `gcal_update_event`, `gcal_delete_event`, `gcal_respond_to_event` | medium | |
| `notion-fetch`, `notion-search`, `notion-get-*` | low | reads |
| `notion-create-*`, `notion-update-*`, `notion-move-*`, `notion-duplicate-*` | medium | resolver must include char count + 200-char body excerpt |
| `telegram reply` to allow-listed chats | low | covered by existing access gate |
| `telegram react`, `edit_message`, `download_attachment` | low | |
| `Sentry__search_*`, `Sentry__find_*`, `Sentry__get_*`, `Sentry__whoami`, `Sentry__analyze_issue_with_seer` | low | reads (pentest context: keep read-only) |
| `Sentry__create_*`, `Sentry__update_*` | medium | |
| `Gamma__generate`, `Gamma__read_*`, `Gamma__get_*` | low for reads, medium for `generate` | |
| All `playwright__*` | BLOCKED | not low/medium/high — unconditionally blocked |
| Any `*_send_*` or explicitly destructive | high | (none currently wired; reserved) |

Updates to the risk table require editing `mcp-tools.yaml` and restarting the broker. Not editable by Donna at runtime.

---

## 10. Donna's behaviour rules (CLAUDE.md additions)

The persona in `CLAUDE.md` stays. Add a new "Security & Broker" section with these rules, enforced by the persona (Donna will follow them because her character does):

1. **Session-start check.** On the first turn of every conversation, run `donna-broker list-pending` and surface to Graham anything in `approved` state that hasn't been executed. Wording: *"Chief, you approved X earlier — want me to go ahead?"*
2. **Never silent-fail an approval.** If a broker call returns `approval_required`, `channel_unavailable`, `cooldown`, `expired`, or `stale`, Donna says so in plain English with the next step. Never just "I can't do that."
3. **Never claim an action is done without the `succeeded` return.** If `execute` returns `success` with a confirmation, say so with the confirmation number. Otherwise, say it's pending.
4. **Credentials never enter Donna's context.** Never ask Graham for a password. Never read `/Users/donna-broker/` or any `.key`, `.age`, `.env` file. If Donna finds herself about to, she stops and tells Graham that a credential boundary was nearly crossed.
5. **Notion is read for reference, not written without approval.** Never write secrets, credentials, or API keys to Notion, ever, even with approval.
6. **Playwright is not available.** If Donna thinks she needs a browser, she asks Graham to add an executor workflow. She never tries to enable Playwright.

---

## 11. Implementation plan

### Phase 0 — Immediate risk reduction (3–4 hours)

Goals: remove the most dangerous capabilities, establish the hook framework, set expectations.

Deliverables:
1. **Delete** `~/.claude/plugins/cache/claude-plugins-official/playwright/` (not just disable the plugin).
2. **Create** `/Users/grahamwilliamson/donna/hooks/` with:
   - `capability-guard.sh` — PreToolUse. Blocks all Playwright tools unconditionally. Blocks Bash except the allowlist in §9. Allows all other MCP tools (broker doesn't exist yet, so no policy check). Allows Read/Write/Edit/Glob/Grep/Agent.
   - `audit-post.sh` — PostToolUse stub. Appends a JSONL line to `/Users/grahamwilliamson/donna/.claude/audit-fallback.log`. No blocking.
3. **Update** `/Users/grahamwilliamson/donna/CLAUDE.md` with the "Security & Broker" section from §10. Mark rules 1 and 2 as "activates when broker is installed in Phase 1" — they're no-ops until then but document intent.
4. **Audit** `/Users/grahamwilliamson/donna/.claude/settings.local.json`:
   - Remove `Bash(security find-generic-password:*)` if present.
   - Remove one-off permissions that accumulated.
   - Register the two hooks under `hooks.PreToolUse` and `hooks.PostToolUse`.
   - Leave the existing MCP-read permissions (they still work with the hook).
5. **Verify** via the Phase 0 gate tests in §12.

Phase 0 ships no new runtime dependencies, no OS user, no SQLite. It's a safety net before construction.

### Phase 1 — Broker + telegram-hardened integration (weeks 1–2)

Goals: broker with two-step commit; Telegram approval path via existing MCP server.

Deliverables:
1. **OS setup.** Create `donna-broker` OS user (home `/Users/donna-broker`). Shared group `donna-bridge` between `donna-broker` and the user running `claude-telegram-hardened`.
2. **Broker Python package** under `/Users/donna-broker/broker/`:
   - `main.py` — CLI dispatcher.
   - `validator.py` — JSON schema per capability.
   - `canonicalize.py` — RFC 8785 wrapper.
   - `policy.py` — rate limits, risk classification, idempotency, expiry, cooldown.
   - `resolver.py` — param → human summary with capability-specific enforcement (Notion char count, email preview, etc.).
   - `requests_db.py` — SQLite WAL, schema migrations.
   - `audit.py` — hash-chained JSONL writer with rotation.
   - `executor.py` — subprocess runner for browser capabilities (Phase 2 hook).
3. **Hardened wrapper** `/usr/local/bin/donna-broker` — shell script that `sudo`s to `donna-broker` user, cleans env, execs broker entry point.
4. **sudoers entry** allowing `grahamwilliamson` to `sudo -u donna-broker /usr/local/bin/donna-broker` with `NOPASSWD`, `CLOSE_FROM=3`, `NOEXEC`.
5. **Manifests:**
   - `capabilities.yaml` — MCP-wrapped capabilities for Phase 1 (no browser yet). Risk levels, revalidation handlers, idempotency date source, resolver fields.
   - `mcp-tools.yaml` — risk classification from §9.
6. **HMAC key** generation + perms.
7. **SQLite** init with WAL, schema from §3.
8. **Extend `server.ts`** (claude-telegram-hardened):
   - Approval queue watcher (§5.3).
   - `/approve`, `/deny`, `/override` regex handler (§5.3).
   - Heartbeat writer (§5.3).
   - No new long-running process.
9. **New skill** `skills/approval/SKILL.md` (§5.5).
10. **Hooks rewired** to call broker `policy-check` and `audit-result`.
11. **CLAUDE.md** — activate the rules deferred from Phase 0.
12. **Verification** via §12.

Estimated 40–60 engineering hours over two calendar weeks.

### Phase 2 — Browser executors + age vault (weeks 3–4)

Unchanged from v3. Adds age encryption for credentials, the first browser capability (`puregym.*`), and the `execute` mode exercising revalidation + decrypted credentials + Playwright-under-broker (not Playwright-as-MCP).

### Phase 3 — Docker isolation (weeks 5–6)

Unchanged from v3. Per-trust-class containers, network egress controls, scoped age keys.

### Phase 4+ — Finance, remote execution, broker-owned OAuth

Unchanged from v3. Finance deferred until Docker is operational.

### What stays out permanently

- General-web browser executor.
- Direct audit log access for Donna.
- setuid privilege model.
- Playwright MCP plugin in this project.
- Auto-execute-on-approval at any risk level.
- `/approve-and-execute` Telegram shortcut.

---

## 12. Verification

### Phase 0 gate

1. Attempt `mcp__plugin_playwright_playwright__browser_navigate` → blocked by hook with explicit message.
2. Attempt `Bash("curl https://example.com")` → blocked.
3. `Bash("ls /Users/grahamwilliamson/donna")` → allowed.
4. `Read(/Users/grahamwilliamson/donna/CLAUDE.md)` → allowed.
5. `mcp__claude_ai_Gmail__gmail_search_messages` → allowed (Phase 0 doesn't restrict MCP yet); PostToolUse writes to `audit-fallback.log`.
6. `ls ~/.claude/plugins/cache/claude-plugins-official/playwright/` → no such directory.

### Phase 1 end-to-end

1. **Low-risk path.** `gmail_search_messages` → hook → broker `policy-check` → `{decision:"approve"}` → tool runs → PostToolUse → audit event `mcp_tool_allowed` with sanitised params.
2. **Medium-risk first attempt.** `gmail_create_draft` → hook → broker creates `pending_approval`, writes `approval-queue/` → server watcher picks up, posts inline-button message to Graham → hook returns block to Donna with code + summary.
3. **Approval via inline button.** Graham taps Approve → server writes `approval-responses/` → server posts ack to Graham → server posts nudge to Donna's chat.
4. **Re-attempt succeeds.** Donna calls `gmail_create_draft` with identical params → hook → broker finds matching approved row by `(tool, params_hash)` → allows → tool runs → PostToolUse transitions row to `succeeded`.
5. **Mutated re-attempt blocked.** Donna calls `gmail_create_draft` with one character changed in body → new `params_hash` → new pending request; old approval unused.
6. **Text-command approval.** New request → Graham sends `/approve A2B7KQ` → server verifies sender_id, writes response, acks.
7. **Denial + cooldown.** New request → `/deny` → row denied → re-request within 30min → `{status:"cooldown", retry_after_seconds:...}`.
8. **Expiry.** Approved request not executed within 1 hour → broker's lazy check on next `execute` returns `expired` → row terminal.
9. **Channel unavailable.** Kill `claude-telegram-hardened` → heartbeat goes stale → next `request` for a medium-risk capability returns `channel_unavailable`. Low-risk continues to work.
10. **Audit integrity.** Run `donna-broker verify-audit` after 9 scenarios → chain verifies. Manually mutate one JSONL line → verify reports the first broken entry's index.
11. **HMAC tamper.** Manually edit `params_json` in SQLite → next operation on that row fails HMAC verification → broker refuses to proceed, logs `audit.hmac_mismatch`.
12. **List-pending on fresh session.** Approve a request, end conversation, start new conversation → Donna's first turn calls `list-pending`, sees approved-not-executed row, tells Graham.

Pass all 12 → Phase 1 done.

---

## 13. Risks and open items

**Top three residual risks (unchanged from v3):**

1. **Broker as single privileged component.** v1 has no Docker isolation. A supply-chain compromise of a broker dependency or a broker bug exposes all trust classes. Mitigation: small codebase (<1000 LoC core), pinned dependencies, no network exposure, process terminates per invocation. Fix: Phase 3 Docker.
2. **MCP output leakage.** Donna sees unredacted MCP responses (inbox content, calendar details, Notion pages). A prompt injection could exfiltrate via creative low-risk channels (notion-create-pages, gcal_create_event with sensitive body). Mitigation: PostToolUse audit + Notion write excerpt-in-approval + strict resolver enforcement. Fix: Phase 4 broker-owned OAuth.
3. **Approval fatigue.** If Graham rubber-stamps, the system is theatre. Mitigation: small approval surface, resolved human summaries, capability-specific enforcement (Notion body excerpt, email preview). No architectural fix — this is human discipline.

**Known rough edges acknowledged:**

- Double-invocation for MCP writes (first attempt blocks, re-attempt after approval runs). Friction for safety.
- Three interactions for browser actions (Donna proposes, Graham approves, Donna executes). Price of the "approval never executes" rule.
- Cross-session continuity depends on Donna calling `list-pending` at session start. Backed by CLAUDE.md rule 1 + the post-approval nudge posted to her channel.

---

## 14. Verdict

This is the plan to build. Phase 0 this week; Phase 1 over the next two. No spec-level decisions deferred.

Ship it, Chief.
