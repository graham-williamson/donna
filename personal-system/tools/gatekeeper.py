#!/usr/bin/env python3
"""Attention gatekeeper — keeps the four voices from ganging up on Graham.

Single-gatekeeper discipline: all proactive output is routed through propose().
Tiers: interrupt/nudge send now; digest accumulates for the next ritual; silent
is logged only. drain_digest() flushes the queue when a ritual fires.
"""
import os
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = os.environ.get("DIGEST_QUEUE", str(ROOT / "_shared" / "_state" / "digest_queue.json"))
TIERS = {"interrupt", "nudge", "digest", "silent"}


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(path, items):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(items, f)


def propose(persona, text, tier, queue_path=None):
    path = queue_path or DEFAULT_QUEUE
    if tier not in TIERS:
        raise ValueError(f"bad tier: {tier}")
    if tier in ("interrupt", "nudge"):
        return {"action": "send", "persona": persona, "text": text, "tier": tier}
    if tier == "digest":
        items = _load(path)
        items.append({"persona": persona, "text": text})
        _save(path, items)
        return {"action": "queued", "queued": len(items)}
    return {"action": "silent"}


def drain_digest(queue_path=None):
    path = queue_path or DEFAULT_QUEUE
    items = _load(path)
    _save(path, [])
    return items
