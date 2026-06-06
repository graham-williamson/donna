#!/usr/bin/env python3
"""Token telemetry — per-turn context size, the canary for memory/layer bloat.

Reads Claude Code session JSONLs (the daemon's project) and reports, over the
most recent turns: average TOTAL context loaded per turn (fresh input + cached),
average FRESH input per turn (the un-cached part — this is what the persona
overlay + recall injection adds every turn), output per turn, and cache hit rate.
High fresh-input-per-turn = the layering isn't earning its context cost.
Read-only: no writes, no network.
"""
import json
import argparse
from pathlib import Path
from datetime import datetime

SESSION_DIR = Path("/Users/grahamwilliamson/.claude/projects/-Users-grahamwilliamson-donna")


def _parse_ts(v):
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _turns(session_dir=None):
    sd = Path(session_dir) if session_dir else SESSION_DIR
    out = []
    for jsonl in sorted(sd.glob("*.jsonl")):
        try:
            with open(jsonl, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") != "assistant":
                        continue
                    msg = e.get("message") or {}
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    u = msg.get("usage") or {}
                    if not isinstance(u, dict):
                        continue
                    out.append({
                        "ts": _parse_ts(e.get("timestamp")),
                        "model": msg.get("model") or "unknown",
                        "input": u.get("input_tokens", 0),
                        "output": u.get("output_tokens", 0),
                        "cache_read": u.get("cache_read_input_tokens", 0),
                        "cache_write": u.get("cache_creation_input_tokens", 0),
                    })
        except OSError:
            continue
    return out


def summary(recent=50, session_dir=None):
    turns = [t for t in _turns(session_dir) if t["ts"]]
    turns.sort(key=lambda t: t["ts"])
    r = turns[-recent:] if recent else turns
    n = len(r)
    if not n:
        return {"turns": 0}
    tot_in = sum(t["input"] for t in r)
    tot_out = sum(t["output"] for t in r)
    tot_cr = sum(t["cache_read"] for t in r)
    tot_cw = sum(t["cache_write"] for t in r)
    total_context = tot_in + tot_cr + tot_cw
    by_model = {}
    for t in r:
        by_model[t["model"]] = by_model.get(t["model"], 0) + 1
    return {
        "turns": n,
        "avg_context_per_turn": round(total_context / n),
        "avg_fresh_input_per_turn": round(tot_in / n),
        "avg_output_per_turn": round(tot_out / n),
        "cache_hit_rate": round(100 * tot_cr / total_context) if total_context else 0,
        "by_model": by_model,
        "last": [{"ctx": t["input"] + t["cache_read"] + t["cache_write"],
                  "out": t["output"], "model": t["model"]} for t in r[-8:]],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tokens")
    ap.add_argument("--recent", type=int, default=50)
    args = ap.parse_args(argv)
    print(json.dumps(summary(args.recent), indent=2))


if __name__ == "__main__":
    main()
