#!/usr/bin/env python3
"""donna-cost.py — local token usage and cost estimate for Donna's sessions.

Reads Claude Code session JSONL files from Donna's project directory,
tallies the `usage` block on each assistant turn, applies per-model
pricing, and reports totals (total USD, tokens, turns) scoped to a
time window. No network calls, no state writes.

Usage:
    python3 donna-cost.py --since today
    python3 donna-cost.py --since week --json

The hook's §14.1 allowlist is tight about argv — only `--since <window>`
and `--json` are permitted. Keep it that way.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SESSION_DIR = Path(
    "/Users/grahamwilliamson/.claude/projects/-Users-grahamwilliamson-donna"
)

# Per-million-token USD prices. Numbers as of 2026-04. Sonnet tiers
# (4.5, 4.6) are priced identically; 3.7 has the same list but older
# generations may change — verify before relying on these for billing.
# Haiku 4.5 is the daemon's cheap-router option. Opus 4.7 appears when
# Donna delegates heavy tasks to Opus via the escalation prompt.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00, "cache_read": 0.30, "cache_write": 3.75, "output": 15.00,
    },
    "claude-sonnet-4-5": {
        "input": 3.00, "cache_read": 0.30, "cache_write": 3.75, "output": 15.00,
    },
    "claude-sonnet-3-7": {
        "input": 3.00, "cache_read": 0.30, "cache_write": 3.75, "output": 15.00,
    },
    "claude-haiku-4-5": {
        "input": 1.00, "cache_read": 0.10, "cache_write": 1.25, "output": 5.00,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.00, "cache_read": 0.10, "cache_write": 1.25, "output": 5.00,
    },
    "claude-opus-4-7": {
        "input": 15.00, "cache_read": 1.50, "cache_write": 18.75, "output": 75.00,
    },
}

DEFAULT_PRICES = PRICES["claude-sonnet-4-6"]


def cutoff_for(since: str) -> datetime:
    now = datetime.now(timezone.utc)
    if since == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if since == "week":
        return now - timedelta(days=7)
    if since == "month":
        return now - timedelta(days=30)
    if since == "all":
        return datetime.min.replace(tzinfo=timezone.utc)
    raise ValueError(f"unknown window: {since!r}")


def cost_for(model: str, usage: dict[str, Any]) -> float:
    p = PRICES.get(model, DEFAULT_PRICES)
    return (
        usage.get("input_tokens", 0) * p["input"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"] / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write"] / 1_000_000
        + usage.get("output_tokens", 0) * p["output"] / 1_000_000
    )


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def scan(cutoff: datetime) -> dict[str, Any]:
    totals = {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "turns": 0,
    }
    by_day: dict[str, float] = {}
    by_model: dict[str, float] = {}

    for jsonl in sorted(SESSION_DIR.glob("*.jsonl")):
        try:
            with open(jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    msg = entry.get("message") or {}
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage") or {}
                    if not isinstance(usage, dict):
                        continue
                    ts = parse_ts(entry.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    model = msg.get("model") or "unknown"
                    c = cost_for(model, usage)
                    totals["cost_usd"] += c
                    totals["input_tokens"] += usage.get("input_tokens", 0)
                    totals["output_tokens"] += usage.get("output_tokens", 0)
                    totals["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                    totals["cache_write_tokens"] += usage.get("cache_creation_input_tokens", 0)
                    totals["turns"] += 1
                    day_key = ts.date().isoformat()
                    by_day[day_key] = by_day.get(day_key, 0.0) + c
                    by_model[model] = by_model.get(model, 0.0) + c
        except OSError:
            # Permission / missing / truncated — skip, log via stderr.
            print(f"warning: could not read {jsonl}", file=sys.stderr)
            continue

    return {"totals": totals, "by_day": by_day, "by_model": by_model}


def format_human(since: str, data: dict[str, Any]) -> str:
    t = data["totals"]
    lines = [f"Donna cost report — since {since}"]
    lines.append(f"  Total:    ${t['cost_usd']:.2f}")
    lines.append(f"  Turns:    {t['turns']:,}")
    lines.append(f"  Input:    {t['input_tokens']:,} tokens")
    lines.append(f"  Output:   {t['output_tokens']:,} tokens")
    lines.append(f"  Cache R:  {t['cache_read_tokens']:,} tokens")
    lines.append(f"  Cache W:  {t['cache_write_tokens']:,} tokens")
    if data["by_day"]:
        lines.append("")
        lines.append("By day:")
        for day in sorted(data["by_day"].keys()):
            lines.append(f"  {day}  ${data['by_day'][day]:.2f}")
    if data["by_model"]:
        lines.append("")
        lines.append("By model:")
        for model, cost in sorted(
            data["by_model"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {model:40s}  ${cost:.2f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", choices=("today", "week", "month", "all"), default="today"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit structured JSON"
    )
    args = parser.parse_args()
    data = scan(cutoff_for(args.since))
    if args.json:
        print(json.dumps({"since": args.since, **data}, default=str))
    else:
        print(format_human(args.since, data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
