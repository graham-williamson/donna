# Phase 1 gate checklist

Spec: security-v1.1 §18. 13 scenarios that validate the broker end-to-
end before Phase 1 is considered shipped. Run after `ops/setup-donna-
broker.sh` completes and the Telegram server extension is live with
`DONNA_BROKER_BRIDGE=1`.

Every scenario lists the inputs, the expected outcome, and how to
check. Do not skip any: a false positive at this stage lets something
through that the guardrails were meant to stop.

Abbreviations:
- `BROKER = sudo -u donna-broker /usr/local/bin/donna-broker`
- `AUDIT_LOG = /Users/donna-broker/audit/audit.log`
- `DB = /Users/donna-broker/.config/donna/requests.db`
- `RESPONSES = /Users/donna-broker/.config/donna/approval-responses`

---

## 1. Low-risk path

**Setup:** Donna's Claude Code session has the Phase 1 hook registered
(see `.claude/settings.local.json` — the `PreToolUse` matcher points
to `hooks/capability-guard-phase1.py`).

**Input:** Ask Donna to search your Gmail:

```
Can you find emails from Sarah about Japan?
```

**Expected:**
- Hook invokes `BROKER policy-check`; broker consults mcp-tools.yaml
  and returns `{decision: "allow", risk_level: "low"}`.
- Gmail MCP tool runs.
- PostToolUse hook runs `BROKER audit-result`; audit log gains
  `mcp_tool_allowed` entry.

**Verify:**
```
tail -5 $AUDIT_LOG | python3 -c 'import json,sys;[print(json.loads(l)["event"]) for l in sys.stdin]'
```

Should show `mcp_tool_allowed` for the Gmail search tool.

---

## 2. Medium-risk first attempt

**Input:** Ask Donna to draft an email:

```
Draft a reply to Sarah thanking her for the Japan pics.
```

**Expected:**
- Hook invokes `BROKER policy-check` on
  `mcp__claude_ai_Gmail__create_draft` (medium risk).
- Broker returns `{decision: "block", reason: "... requires approval
  via broker request"}`.
- Donna sees the block and calls `BROKER request` explicitly.
- Broker creates a `pending_approval` row, generates a 6-char code,
  writes the queue file to `/Users/donna-broker/.config/donna/
  approval-queue/`.
- Telegram server's broker bridge picks up the queue file within 1s
  and sends Graham a MarkdownV2 prompt with inline `Approve` / `Deny`
  buttons.

**Verify:**
- Graham's Telegram app shows the approval prompt with the code,
  capability, and a "Donna says:" monospaced context block.
- `BROKER list-pending` shows one row in `pending_approval` state.
- Audit log has `request_created` and `request_pending` entries.

---

## 3. Inline-button approval

**Input:** Graham taps the ✓ **Approve** button on the Telegram prompt.

**Expected:**
- Bridge's callback handler verifies sender gate (graham's user_id ∈
  allowFrom, chat is the private DM, `chat.id == config.chatId`).
- Bridge writes an atomic approval-response file to
  `/Users/donna-broker/.config/donna/approval-responses/<request_id>.json`
  with `{"decision": "approve", "approved_by": "<graham_id>", ...}`.
- Bridge sends a confirmation message to Graham.
- Bridge posts a nudge (optional — not in this implementation) OR
  Donna polls on her next turn.

**Verify:**
```
ls -la $RESPONSES
cat $RESPONSES/<request_id>.json
```

The file exists and parses. Bridge sends ack message to Graham.

---

## 4. Re-attempt succeeds

**Input:** Donna re-runs the email-draft request with the exact same
params (next Claude turn).

**Expected:**
- `BROKER request` returns `{status: "existing", request_id, state:
  "pending_approval" | "approved"}` because the idempotency key
  matches.
- Donna then calls `BROKER execute {"approval_code": "<code>"}`.
- Broker reads the approval-response file, verifies `params_hash` and
  creation HMAC, transitions `pending_approval` → `approved` →
  `executing`, dispatches to the `mcp_tool` executor which returns
  metadata, row stays `executing` for the PostToolUse hook to close.
- The MCP tool actually runs; PostToolUse hook emits `audit-result`;
  row transitions `executing` → `succeeded` with a confirmation ref.

**Verify:**
- `BROKER status-by-code {"approval_code": "<code>"}` shows state
  `succeeded`.
- Audit log has `request_execution_started`, `mcp_tool_allowed`,
  `request_execution_succeeded`.

---

## 5. Mutated re-attempt blocked

**Input:** Donna calls the same capability but with one character
changed in the `subject` field.

**Expected:**
- Different `params_hash` → different `idempotency_key` → new
  `pending_approval` row with a fresh code.
- The old approval does NOT cover the new request.

**Verify:**
- `BROKER list-pending` shows two distinct rows (or one if the first
  is now terminal).

---

## 6. Text-command approval

**Input:** Trigger a new approval (as in Scenario 2). In Telegram DM,
type:
```
/approve <code>
```

**Expected:**
- Server text-command regex matches anchored `^/(approve|deny|override)
  \s+([A-HJKMNP-TV-Z2-7]{6})\s*$`.
- Sender gate passes.
- Response file written atomically.
- Bridge replies with "✓ <code> approved".

**Verify:** same as Scenario 3.

Then test rejection variants that should be silent-dropped:
- `please /approve A2B7KQ now` (not anchored) — bridge should ignore.
- `/approve A2B7KQ` from a **different** user — bridge should silent-
  reject. No reply, no file written.

---

## 7. Denial + cooldown

**Input:** Trigger a new approval. In Telegram, tap ✗ **Deny**.

**Expected:**
- Broker writes the deny response; `execute` returns `status: "denied"`.
- Row transitions to `denied`.
- For 30 minutes, re-requesting the same `(capability, params, date)`
  returns `{status: "cooldown", retry_after_seconds: <n>}`.

**Verify:**
- Immediately re-request the same payload → `cooldown` response with
  positive seconds.

---

## 8. Override

**Input:** During Scenario 7's cooldown window, in Telegram:
```
/override <code>
```

**Expected (Phase 1.1 — not in Phase 1 scope, but tests current
behaviour):**
- **Current behaviour:** the text command is parsed but the broker
  does not expose an `override` mode yet (see `MODE_HANDLERS` and
  `NOT_YET_IMPLEMENTED`). The bridge writes an approval response
  with `decision: "override"`, but `BROKER execute` does not act on
  that decision.
- **When `override` ships:** row transitions `denied` → `pending_
  approval` with a fresh window; `audit.override_issued` event.

**Verify (Phase 1 baseline):** `/override` does not crash. Row stays
`denied`. **TODO: implement broker `override` mode before cooldown
override is production-ready.**

---

## 9. Expiry

**Input:** Trigger a medium-risk approval. Wait until the approval
window elapses without tapping anything. Then on Donna's next turn,
attempt `BROKER execute`.

**Expected:**
- `execute` sees the approval-expiry and returns a terminal state
  (the broker's state machine + the bridge's lazy expiry check).

**Verify:** `BROKER status-by-code` shows `expired`.

---

## 10. Channel unavailable

**Input:** Stop the Telegram server (`launchctl bootout` on the
telegram supervisor, or kill the bun process). Wait >90 seconds.
Then trigger a medium-risk approval.

**Expected:**
- Heartbeat file's `last_poll_ts` goes stale.
- `BROKER request` on a medium/high capability still succeeds (§10's
  failure semantics matrix does not gate `request` on heartbeat staleness
  — that gate is nominally in `BROKER` itself; verify that current
  behaviour matches or update).

**Verify:** heartbeat file at
`/Users/donna-broker/.config/donna/heartbeat/telegram.heartbeat`
has a `last_poll_ts` older than 90s.

---

## 11. Audit integrity

**Input:**
```
echo '{}' | $BROKER verify-audit
```

**Expected after ~10 scenarios above:**
- `{"status": "ok", "verified": true}`.

Then mutate one line in the audit log:
```
sudo -u donna-broker sed -i '' '3s/request_created/request_CREATED/' $AUDIT_LOG
echo '{}' | $BROKER verify-audit
```

**Expected:**
- `{"status": "integrity_break", "verified": false, "break": {"file":
  "...", "line": 4, "reason": "prev_hash mismatch: ..."}}`.

**Verify:** break reported on the line AFTER the mutated one (because
the mutated line's own hash changed, breaking the next line's
prev_hash check).

---

## 12. HMAC tamper

**Input:** Open the SQLite DB as donna-broker and mutate `params_json`
directly (temporarily drop the immutable trigger):

```
sudo -u donna-broker /Users/donna-broker/broker/.venv/bin/python3 <<'PY'
import sqlite3
conn = sqlite3.connect("/Users/donna-broker/.config/donna/requests.db")
conn.execute("DROP TRIGGER IF EXISTS trg_immutable_params_json")
# Get the latest pending row...
r = conn.execute("SELECT request_id FROM requests WHERE state='pending_approval' LIMIT 1").fetchone()
conn.execute("UPDATE requests SET params_json = '{\"tampered\":true}' WHERE request_id = ?", (r[0],))
conn.commit()
PY
```

Then try to execute it (simulate an approval response first, as in
Scenario 3).

**Expected:**
- Broker recomputes `params_hash` from the mutated `params_json`,
  finds mismatch, transitions row → `integrity_failed`, emits
  `audit.params_hash_mismatch`, returns structured error.
- Bridge will pick up an `ALERT-integrity-<ts>.json` file (when the
  broker starts writing those — §12.4; current Phase 1 bridge reads
  only normal queue files, so this is a **known follow-up**).

**Verify:**
- Row state = `integrity_failed`.
- Audit log contains `audit.params_hash_mismatch`.

---

## 13. Pending surfacing across sessions

**Input:** Approve a request in one Claude Code session. Close the
session. Start a new Claude Code session.

**Expected:**
- Donna's first broker call in the new session returns a response
  with `pending_summary` containing the approved-not-executed code.
- Donna surfaces this to Graham before doing anything else (CLAUDE.md
  rule — activates in Phase 1).

**Verify:**
- Any broker call (e.g. `BROKER list-pending`) returns
  `pending_count > 0` and `pending_summary: [{code, capability, ...}]`.
- Donna's next message to Graham mentions the pending item.

---

## Pass criteria

All 13 scenarios pass → Phase 1 is done. File a reminder to restore
the Phase 0 hook (if you've been running bridge-only during testing)
or swap `.claude/settings.local.json` to the Phase 1 hook
permanently.

One scenario fails → stop. Fix before proceeding. The whole point of
this gate is that it's the last check before a live assistant that
can act on your behalf.
