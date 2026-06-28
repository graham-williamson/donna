# browser_sanitise.py
"""Page sanitiser (design §5.5, invariant 2). Turns a raw DOM snapshot dict into
the compact, script-stripped representation the agent sees — wrapped in an
envelope that formally tags it source=webpage / trust=untrusted. Suspicious text
is NOT silently removed (the agent must see the real page); the trust tag + the
agent's system prompt are the contract that page content is data, never commands.
Pure."""
from __future__ import annotations

import hashlib
import json
from typing import Any

_DROP_ROLES = frozenset({"script", "style"})


def sanitise(raw: dict[str, Any]) -> dict[str, Any]:
    """Raw snapshot → {source, trust, url, elements:[{ref, role, text, editable}]}."""
    elements: list[dict[str, Any]] = []
    for node in raw.get("nodes", []):
        if node.get("role") in _DROP_ROLES or node.get("tag") in _DROP_ROLES:
            continue
        el: dict[str, Any] = {
            "ref": str(node.get("ref") or ""),
            "role": str(node.get("role") or "text"),
            "text": str(node.get("name") or ""),
            "editable": bool(node.get("editable")),
        }
        # Link destination ('host/path', tokens already stripped upstream) and
        # dropdown option labels — both page-controlled DATA, surfaced so the
        # agent can pick a real link/option instead of guessing. Added only when
        # present, keeping token-free snapshots byte-identical. trust=untrusted
        # still applies: these are data, never instructions.
        dest = node.get("dest")
        if dest:
            el["dest"] = str(dest)
        options = node.get("options")
        if isinstance(options, list) and options:
            el["options"] = [str(o) for o in options]
        elements.append(el)
    return {"source": "webpage", "trust": "untrusted",
            "url": str(raw.get("url") or ""), "elements": elements}


def snapshot_hash(raw: dict[str, Any]) -> str:
    """A stable content hash of the sanitised snapshot, for commit-token binding
    (so an approval is void if the page changed underneath it)."""
    s = sanitise(raw)
    payload = json.dumps(s, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
