# Phase 1 deployment

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` (v1.1).

The broker code, manifests, hooks, Telegram bridge, and launchd
plists are all in the repo. Everything still needs Graham's hands is
a sudo / TCC-prompt / launchctl operation. This document is the
step-by-step.

**Total time:** ~90 minutes if nothing surprises us.

**Order matters.** Each step depends on the one before.

---

## Prerequisites

- macOS on the Mac Mini.
- Your macOS username is `grahamwilliamson` (the scripts hardcode
  this; edit `GRAHAM_USER` at the top of
  `ops/setup-donna-broker.sh` if different).
- Repository at `/Users/grahamwilliamson/donna/`, master branch
  clean.
- Python 3.11+ on `/usr/bin/python3` (the broker venv uses it).
- The broker passes its test suite locally:
  ```
  cd /Users/grahamwilliamson/donna/broker
  source .venv/bin/activate
  pytest
  ```
  (should be 361 passed.)

## Step 0 — Back out the Phase 0 hook-disabled state

If you've been running with the PreToolUse hook disabled (per
`.claude/HOOK_RESTORE.md`), leave it off for now. We'll swap it to
the Phase 1 hook after the broker is running.

---

## Step 1 — Run the OS setup script (~20 min)

```
cd /Users/grahamwilliamson/donna
sudo bash ops/setup-donna-broker.sh
```

What it does:
- Creates the `donna-bridge` group (GID 600) and `donna-broker`
  system user (UID 600).
- Adds both `donna-broker` and `grahamwilliamson` to the group.
- Creates `/Users/donna-broker/` with the directory layout from
  spec §12.1 and the right ownership / modes (2770 on shared dirs,
  0700 on secrets).
- Rsyncs the broker Python package to `/Users/donna-broker/broker/`.
- Copies seed manifests to `/Users/donna-broker/.config/donna/`.
- Creates the broker venv and installs hash-locked deps.
- Generates the 32-byte HMAC key at
  `/Users/donna-broker/.config/donna/hmac.key`, mode 0400.
- Installs the wrapper at `/usr/local/bin/donna-broker`.
- Installs the sudoers rule at `/etc/sudoers.d/donna-broker`.
- Adds Time Machine exclusions for secrets / audit / DB (§24.2).
- Runs a smoke test: `donna-broker verify-audit`.

**Expected output:** a final "Setup complete." line, then next-steps.

**If it fails** at the `dscl` step, the group or user may already
exist from a prior attempt — re-run; it's idempotent. For other
failures, read the error and fix before proceeding.

---

## Step 2 — Verify the smoke test (~2 min)

Run each manually and confirm the output:

```
sudo -u donna-broker /usr/local/bin/donna-broker verify-audit < /dev/null
```
Expected: `{"status":"ok","verified":true,"pending_count":0}`.

```
echo '{}' | sudo -u donna-broker /usr/local/bin/donna-broker list-pending
```
Expected: `{"status":"ok","requests":[],"pending_count":0}`.

```
echo '{"tool_name":"mcp__claude_ai_Gmail__gmail_search_messages"}' \
  | sudo -u donna-broker /usr/local/bin/donna-broker policy-check
```
Expected: `{"status":"ok","decision":"allow","risk_level":"low",...}`.

```
echo '{"tool_name":"mcp__plugin_playwright_playwright__browser_navigate"}' \
  | sudo -u donna-broker /usr/local/bin/donna-broker policy-check
```
Expected: `{"status":"ok","decision":"deny","reason":"... blocked ..."}`.

If all four match — the broker is live.

---

## Step 3 — Install launchd audit verification (~2 min)

```
bash ops/install-launchd.sh
launchctl list | grep com.donna.broker
```

Expected: the `com.donna.broker.verify-audit` job is loaded. It will
run daily at 03:15. You can trigger it early to test:

```
launchctl kickstart -k gui/$(id -u)/com.donna.broker.verify-audit
cat /Users/donna-broker/audit/verify-audit.stdout.log
```

---

## Step 4 — Configure Telegram bridge environment (~5 min)

The Telegram server extension only activates when
`DONNA_BROKER_BRIDGE=1` is set in its environment. Edit
`~/.claude/channels/telegram/.env`:

```
# existing config...
TELEGRAM_BOT_TOKEN=<existing>

# Add:
DONNA_BROKER_BRIDGE=1
DONNA_BROKER_HOME=/Users/donna-broker/.config/donna
DONNA_BROKER_CHAT_ID=<your Telegram numeric user id>
```

The chat ID is your private DM chat ID with the bot — same as the
first entry in `~/.claude/channels/telegram/access.json`'s
`allowFrom`. If left unset, the bridge auto-picks `allowFrom[0]`.

---

## Step 5 — Restart the Telegram server (~1 min)

However you run the server — launchd, `bun run`, `supervisor.ts` —
restart it so it picks up the new env and the new `donna_broker.ts`
module.

When it starts, stderr should include:
```
telegram channel: donna broker bridge started
```

If it doesn't: set `DONNA_BROKER_BRIDGE=1` in the active env,
confirm the broker dirs exist with the right perms (grahamwilliamson
must be in `donna-bridge` group — verify with `id`), restart again.

---

## Step 6 — Swap the hooks to Phase 1 (~2 min)

Edit `/Users/grahamwilliamson/donna/.claude/settings.local.json`.

First, re-add the `PreToolUse` block if you removed it during the
hook-relaxation window (see `.claude/HOOK_RESTORE.md`). Then change
both hook commands to the Phase 1 versions:

- `PreToolUse[0].hooks[0].command` →
  `/Users/grahamwilliamson/donna/hooks/capability-guard-phase1.py`
- `PostToolUse[0].hooks[0].command` →
  `/Users/grahamwilliamson/donna/hooks/audit-post-phase1.py`

The Phase 1 versions:
- **`capability-guard-phase1.py`** routes MCP tool checks through
  `donna-broker policy-check`. Bash uses the same Phase 0 allowlist
  (it's not broker-gated in v1). Broker unreachable → fail closed.
- **`audit-post-phase1.py`** routes every tool outcome through
  `donna-broker audit-result` into the hash-chained JSONL. Broker
  unreachable (>2s) → falls back to `.claude/audit-fallback.log`,
  never blocks the tool pipeline (§13.5).

Save. Claude Code picks up the change on the next tool call.

**Important:** the Phase 0 hook files stay in the repo for fallback.
If a Phase 1 hook ever misbehaves (broker down, sudoers broken), swap
the paths back to `capability-guard.sh` / `audit-post.sh` and you're
protected at the Phase 0 level.

---

## Step 7 — Run the Phase 1 gate (~2 hours)

Follow `ops/PHASE_1_GATE.md` end-to-end. 13 scenarios. All must pass.

If any fail: stop, fix, retry. The gate is the final trust check;
don't let a real request flow until it's clean.

---

## Step 8 — Activate the CLAUDE.md Phase 1 rules

Edit `/Users/grahamwilliamson/donna/CLAUDE.md`. In the "Security &
Broker" section, move rules 1 / 2 / 3 from **"Activates in Phase 1"**
into **"Active in Phase 0 (now)"** — or just retitle the section to
"Active now". Commit.

These rules (pending check, never silent-fail an approval, never claim
done without `succeeded`) become load-bearing once the broker is
actually running.

---

## Step 9 — Post-install hygiene

### iPhone / Mac lock-screen notification settings (§24.3)

- Mac Mini: System Settings → Notifications → Telegram → "Show
  previews" = **"When Unlocked"**.
- iPhone: Settings → Notifications → Telegram → Show Previews = "When
  Unlocked".
- Optionally enable "Hide sender and message" on Donna's approval DM
  thread specifically.

These prevent anyone with physical access to a locked device from
reading the 6-char code off a push notification and opening Telegram
to approve.

### Bot token rotation reminder (§24.5)

The Telegram bot token in `~/.claude/channels/telegram/.env` is a
shared secret. Add a calendar reminder for 6 months from today to
rotate via BotFather.

---

## Rollback

If Phase 1 goes sideways and you need to back out:

```
# 1. Swap the hook back to Phase 0 (edit settings.local.json).
# 2. Remove the Telegram bridge env var and restart the server.
# 3. If full teardown:
sudo bash ops/teardown-donna-broker.sh
```

Phase 0 guardrails (capability-guard.sh + audit-post.sh) keep working
without the broker. Donna operates at Phase 0 trust level until you
rebuild.

---

## What's still not done at the end of Phase 1

Per the spec's roadmap — these are explicit Phase 2+ items, not gaps:

- **Browser executors + age vault (§17 Phase 2).** First browser
  capability (`puregym.book_class`) with a real headless chromium
  flow + age-encrypted credentials. Phase 1's PureGym manifest entry
  is a placeholder.
- **Operator dashboard (§22, Phase 1.5).** Localhost read-only web
  view over SQLite + audit. Deferred until the broker has live data.
- **Integrity alert rendering in the Telegram bridge (§12.4).**
  Current bridge reads normal queue files; `ALERT-integrity-<ts>.json`
  handling is a follow-up.
- **`override` broker mode (§7.4 + §13.1).** Bridge accepts
  `/override <code>`, but the broker handler is not yet implemented.
- **Reconcile mode (§11 rule 9, §13.1).** Manual CLI tool to resolve
  rows stuck in `reconciliation_needed`. Phase 1.1.
- **HMAC key rotation mode (§7.3).** Manual, rare.

All of these are tracked in the codebase with explicit TODOs or
`{error_code: "not_implemented"}` responses — nothing silently
accepts broken state.
