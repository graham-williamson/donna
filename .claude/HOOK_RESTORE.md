# Re-enable PreToolUse capability-guard

The Phase 0 PreToolUse hook was **temporarily disabled** on 2026-04-19 for
the duration of Phase 1 broker build, so Claude could run git/pytest/mypy
directly instead of round-tripping every command through Graham.

PostToolUse audit-post hook is **still live** — paper trail intact.

## When to restore

**Before Phase 1 ships.** The moment the broker is built, committed, and
Wave A/B/C are merged to master — restore this hook before the broker goes
into live use.

Stronger trigger: restore it **any** time Donna needs to process real
attacker-controllable input (email bodies, Notion pages, Telegram messages
from untrusted senders). Build-time work has none of that. Live assistant
work does.

## How to restore

Open `/Users/grahamwilliamson/donna/.claude/settings.local.json` and add
the `PreToolUse` block back at the top of the `hooks` object. The exact
block:

```json
    "PreToolUse": [
      {
        "matcher": "Bash|mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/grahamwilliamson/donna/hooks/capability-guard.sh",
            "timeout": 5
          }
        ]
      }
    ],
```

The `hooks` object should end up looking exactly like this:

```json
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/grahamwilliamson/donna/hooks/capability-guard.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/grahamwilliamson/donna/hooks/audit-post.sh",
            "timeout": 2
          }
        ]
      }
    ]
  }
```

## How to verify the restore

Ask Claude to run any command that's not in the §14.1 allowlist, e.g.:

```
curl https://example.com
```

Expected: Claude sees a `capability-guard: Bash argv [...] not in §14.1
allowlist` deny response and can't run it.

If the hook is restored correctly, Donna's damage ceiling is back to
"tells Graham wrong things" — not "sends emails/books flights/scrapes
browsers autonomously."
