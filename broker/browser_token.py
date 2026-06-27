# browser_token.py
"""Commit-token store (design §5.4, invariant 4). Replaces heuristic commit
detection: a state-changing action is only permitted if it consumes a one-time,
human-approved token bound to the exact approved target, and only as the
IMMEDIATELY next action. Single-use, short-lived, one live token per run.

Token ids are opaque (secrets.token_hex — server-side broker code). `now` is
injected so tests are deterministic.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable


class TokenError(ValueError):
    """A commit token is unknown, expired, already used, superseded, or does not
    match the action trying to consume it. Fail-closed — the action is refused."""


@dataclass
class _Token:
    summary: str
    snapshot_hash: str
    target_ref: str
    expected_text: str
    approval_id: str
    minted_at: float
    used: bool = False


class TokenStore:
    def __init__(self, now: Callable[[], float], ttl_seconds: float = 120.0) -> None:
        self._now = now
        self._ttl = ttl_seconds
        self._tokens: dict[str, _Token] = {}
        self.live_token_id: str | None = None

    def mint(self, *, summary: str, snapshot_hash: str, target_ref: str,
             expected_text: str, approval_id: str) -> str:
        """Create a one-time token for an APPROVED commit. A new mint supersedes
        any previously-live token (a fresh proposal replaces a stale one)."""
        tid = secrets.token_hex(16)
        self._tokens[tid] = _Token(summary=summary, snapshot_hash=snapshot_hash,
                                   target_ref=target_ref, expected_text=expected_text,
                                   approval_id=approval_id, minted_at=self._now())
        self.live_token_id = tid
        return tid

    def consume(self, token_id: str, *, snapshot_hash: str, target_ref: str,
                expected_text: str) -> _Token:
        """Validate + consume the token for THIS action. Raises TokenError unless
        the token is the live one, unused, unexpired, and bound to exactly this
        target_ref + expected_text + approved snapshot."""
        tok = self._tokens.get(token_id)
        if tok is None:
            raise TokenError("unknown commit token")
        if tok.used:
            raise TokenError("commit token already used")
        if token_id != self.live_token_id:
            raise TokenError("commit token is not the live one (a newer proposal superseded it)")
        if self._now() - tok.minted_at > self._ttl:
            raise TokenError("commit token expired")
        if target_ref != tok.target_ref or expected_text != tok.expected_text:
            raise TokenError("commit action does not match the approved target")
        if snapshot_hash != tok.snapshot_hash:
            raise TokenError("page changed since approval — re-approve required")
        tok.used = True
        self.live_token_id = None
        return tok
