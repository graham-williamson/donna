"""Donna broker — capability-gated request lifecycle.

Spec: /Users/grahamwilliamson/.claude/plans/donna-security-v1.md (v1.1)

Phase 1 skeleton. Module responsibilities (see per-module docstrings):
  - main:          CLI dispatcher (§13.1)
  - canonicalize:  RFC 8785 params hashing (§7.1)
  - requests_db:   SQLite schema, WAL, immutable triggers (§6, §7.5)
  - grants_db:     standing-grants store (broker-standing-grants §4)
  - audit:         hash-chained JSONL writer (§7.6)
  - validator:     capability manifest validation (§8)
  - policy:        HMAC, idempotency, rate limits, cooldown (§13.2, §7.2, §7.4)
  - resolver:      mode-aware; subprocess isolation (§9, §12.5)
  - executor:      capability-bound dispatch (§8, §13.4)
  - browser_profile: declarative site profile for the browser-goal agent (§5.1, §9)
  - browser_sanitise: page sanitiser — untrusted envelope + snapshot hash (§5.5, invariant 2)
"""

__version__ = "0.0.1-pre"
