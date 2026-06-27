"""Append-only promoter install ledger (design §6.3i, §9.6).

One JSON line per install attempt — success or refusal — for accountability
and replay. Never writes a signature, key, or secret. Append-only: each
record() opens in 'a' mode and fsyncs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

# The complete, fixed set of fields a ledger row may contain. Writing exactly
# these keys — and only these — is the precise form of the "no secret leakage"
# guarantee (§9.6): a signature, key, or other secret can never be a row field.
_ALLOWED_FIELDS = (
    "ts",
    "pack_id",
    "pack_hash",
    "key_id",
    "approval_id",
    "outcome",
    "reason",
)


@dataclass
class Ledger:
    path: str
    now: Callable[[], float]

    def record(
        self,
        *,
        pack_id: str,
        pack_hash: str,
        key_id: str,
        approval_id: str,
        outcome: str,
        reason: str,
    ) -> None:
        row: dict[str, Any] = {
            "ts": self.now(),
            "pack_id": pack_id,
            "pack_hash": pack_hash,
            "key_id": key_id,
            "approval_id": approval_id,
            "outcome": outcome,
            "reason": reason,
        }
        line = json.dumps({k: row[k] for k in _ALLOWED_FIELDS}, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def read_all(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows
