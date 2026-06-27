"""Append-only audit/replay ledger (design §8, invariant 7). One JSON line per
action, plus a run header. Secrets and credential placeholders are scrubbed
before write — the ledger must be safe to read and keep."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

_CRED_PLACEHOLDER = re.compile(r"\{\{cred:[^}]*\}\}")
_REDACTED = "<redacted-credential>"


def _scrub(value: Any) -> Any:
    """Recursively replace any credential placeholder with a marker. (The real
    secret never reaches the ledger; this also guards against the placeholder
    itself leaking, which would reveal which field was a credential.)"""
    if isinstance(value, str):
        return _CRED_PLACEHOLDER.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class Ledger:
    def __init__(self, path: Path, run_id: str, now: Callable[[], float]) -> None:
        self._path = Path(path)
        self._run_id = run_id
        self._now = now

    def _append(self, obj: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_scrub(obj), separators=(",", ":")) + "\n")

    def run_header(self, *, site: str, goal: str, caps: dict[str, Any]) -> None:
        self._append({"type": "run", "run_id": self._run_id, "ts": self._now(),
                      "site": site, "goal": goal, "caps": caps})

    def record(self, *, step: int, snapshot_hash: str, action: dict[str, Any],
               gate_decision: str, outcome: str, approval_id: str | None = None,
               commit_token: str | None = None,
               network_events: list[Any] | None = None) -> None:
        self._append({"type": "action", "run_id": self._run_id, "ts": self._now(),
                      "step": step, "snapshot_hash": snapshot_hash, "action": action,
                      "gate_decision": gate_decision, "approval_id": approval_id,
                      "commit_token": commit_token,
                      "network_events": network_events or [], "outcome": outcome})
