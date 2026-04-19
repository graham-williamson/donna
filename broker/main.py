"""CLI dispatcher — `donna-broker <mode> <json-payload>`.

Spec: security-v1.1 §13.1 (modes + pause scope), §13.5 (hook contracts),
§13.6 (pending-summary surfacing), §10 (failure semantics matrix).

Modes (all accept stdin JSON, emit stdout JSON; never stack traces):
  request, policy-check, execute, cancel, reconcile, status,
  status-by-code, list-pending, list-recent, audit-result,
  rotate-hmac, verify-audit.

Error envelope: {status, error_code, message}.

Phase 1 Ralph target — see `broker/ralph-prompts/executor.md` for
dispatch conventions. main.py is the integration surface — per §23.5
this is written manually, not via Ralph, once Wave A/B modules are in.
"""
from __future__ import annotations

import sys


MODES = {
    "request", "policy-check", "execute", "cancel", "reconcile", "status",
    "status-by-code", "list-pending", "list-recent", "audit-result",
    "rotate-hmac", "verify-audit",
}


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("main: Phase 1 Wave C integration target")


if __name__ == "__main__":
    sys.exit(main())
