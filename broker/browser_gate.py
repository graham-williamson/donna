# browser_gate.py
"""Action gate (design §5.3, §6 — the security core). Validates ONE agent action
against the allowlist, credential substitution, expected_text element validation,
and the commit-token rule. Pure: no browser, no model. The orchestrator (Plan 2)
executes `Decision.action`, logs `Decision.log_action`, and on `needs_approval`
gets human approval + mints a token then re-submits the committing action.

Invariants enforced here: 3 (agent has no out-of-vocabulary power), 4 (no mutation
without a live token), 5 (off-allowlist refused), plus expected_text validation
and credential substitution that never leaks the secret into the log path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast

from broker import browser_nav as nav
from broker import browser_profile as bp
from broker import browser_sanitise as san
from broker import browser_token as bt

_CRED = re.compile(r"\{\{cred:(username|password)\}\}")
_VOCAB = frozenset({"read", "navigate", "click", "type", "propose_commit", "done", "give_up"})


@dataclass
class Decision:
    decision: str
    reason: str = ""
    action: dict[str, Any] = field(default_factory=dict)
    log_action: dict[str, Any] = field(default_factory=dict)
    proposal: dict[str, Any] = field(default_factory=dict)


class Gate:
    def __init__(self, *, profile: bp.SiteProfile, tokens: bt.TokenStore,
                 creds: dict[str, str], phase: str) -> None:
        self.profile = profile
        self.tokens = tokens
        self._creds = creds
        self.phase = phase

    def _elem(self, snapshot: dict[str, Any], ref: str) -> dict[str, Any] | None:
        elements = cast(list[dict[str, Any]], san.sanitise(snapshot)["elements"])
        for e in elements:
            if e["ref"] == ref:
                return e
        return None

    def check(self, action: dict[str, Any], snapshot: dict[str, Any]) -> Decision:
        kind = action.get("kind")
        if kind not in _VOCAB:
            return Decision("refuse", reason=f"action kind {kind!r} is not in the allowed vocabulary")

        if kind in ("read", "done", "give_up"):
            return Decision("allow", action=dict(action), log_action=dict(action))

        if kind == "navigate":
            path = str(action.get("path") or "")
            try:
                url = nav.resolve_path(self.profile.origin, path)
                nav.check(url, self.profile.allowlist)
            except nav.NavError as e:
                return Decision("refuse", reason=str(e))
            resolved = {"kind": "navigate", "url": url}
            return Decision("allow", action=resolved, log_action=resolved)

        if kind == "type":
            ref = str(action.get("ref") or "")
            el = self._elem(snapshot, ref)
            if el is None or not el["editable"]:
                return Decision("refuse", reason=f"type target {ref!r} is not an editable field")
            if str(action.get("expected_label") or "") != el["text"]:
                return Decision("refuse", reason="type expected_label does not match the live field")
            raw_text = str(action.get("text") or "")

            def _sub(m: re.Match[str]) -> str:
                return self._creds.get(m.group(1), "")

            real = _CRED.sub(_sub, raw_text)
            return Decision("allow",
                            action={"kind": "type", "ref": ref, "text": real},
                            log_action={"kind": "type", "ref": ref, "text": raw_text})

        if kind == "propose_commit":
            if self.phase != "execute":
                return Decision("refuse", reason="cannot commit during the plan (read-only) phase")
            return Decision("needs_approval", proposal={
                "summary": str(action.get("summary") or ""),
                "price": action.get("price"),
                "ref": str(action.get("ref") or ""),
                "expected_text": str(action.get("expected_text") or ""),
                "snapshot_hash": san.snapshot_hash(snapshot),
            }, log_action=dict(action))

        if kind == "click":
            ref = str(action.get("ref") or "")
            el = self._elem(snapshot, ref)
            if el is None:
                return Decision("refuse", reason=f"click target {ref!r} not on the page")
            if str(action.get("expected_text") or "") != el["text"]:
                return Decision("refuse", reason="click expected_text does not match the live element")
            if action.get("commit"):
                token_id = str(action.get("commit_token") or "")
                if not token_id:
                    return Decision("refuse", reason="a committing click requires an approved commit token")
                try:
                    self.tokens.consume(token_id, snapshot_hash=san.snapshot_hash(snapshot),
                                        target_ref=ref, expected_text=el["text"])
                except bt.TokenError as e:
                    return Decision("refuse", reason=f"commit token rejected: {e}")
            resolved = {"kind": "click", "ref": ref}
            return Decision("allow", action=resolved, log_action=dict(action))

        return Decision("refuse", reason="unreachable")  # pragma: no cover  (all vocab kinds handled above)
