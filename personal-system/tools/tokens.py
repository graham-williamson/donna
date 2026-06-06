#!/usr/bin/env python3
"""Token telemetry — per-turn context size, split by CHANNEL and AGENT.

Reads Claude Code session JSONLs (the daemon's project) and reports, over the
most recent turns in each channel, the average TOTAL context loaded per turn
(fresh input + cached), the average FRESH input per turn (the un-cached part —
what the persona overlay + recall injection adds every turn), output per turn,
and cache hit rate.

Two axes, because they answer different questions:

  - CHANNEL — "telegram" (the live bot Graham talks to) vs "cli" (a dev
    terminal session). Decided structurally: a Telegram-originated user turn
    carries a top-level  "origin":{"kind":"channel"}  field (the harness adds
    it for messages arriving over the Telegram MCP channel); CLI turns have
    "entrypoint":"cli" and no channel origin. A session is wholly one or the
    other, so channel is decided per file. This is model-independent — it does
    NOT rely on haiku-vs-opus, which mixes across both channels historically.

  - AGENT — within telegram, which persona was speaking (donna/nike/esme/
    bodhi). The active persona is NOT persisted in the transcript (it rides in
    UserPromptSubmit additionalContext, which Claude Code doesn't write to
    disk, and haiku doesn't reliably echo its glyph). So persona_dispatch.py
    writes a one-line turn-marker per Telegram turn to data/turns.jsonl, and
    we join each assistant turn to the nearest preceding marker by timestamp.
    Turns with no marker (history before the sidecar existed) read "unknown".

High fresh-input-per-turn on a given agent = that persona's layering isn't
earning its context cost. Read-only: no writes, no network.
"""
import json
import bisect
import argparse
from pathlib import Path
from datetime import datetime

SESSION_DIR = Path("/Users/grahamwilliamson/.claude/projects/-Users-grahamwilliamson-donna")
TURNS_LOG = Path(__file__).resolve().parents[1] / "data" / "turns.jsonl"
AGENTS = ("donna", "nike", "esme", "bodhi")
# Not real conversational turns: Claude Code's internal helper completions
# (title generation, etc.) log as "claude-api", and harness-injected entries
# (interruptions, etc.) log as "<synthetic>". Both skew per-turn averages.
SKIP_MODELS = {"claude-api", "<synthetic>"}


def _parse_ts(v):
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_channel_user(e):
    return (isinstance(e, dict) and e.get("type") == "user"
            and isinstance(e.get("origin"), dict)
            and e["origin"].get("kind") == "channel")


def _persona_markers(path=None):
    """(sorted_ts_list, persona_list) from the dispatch sidecar."""
    p = Path(path) if path else TURNS_LOG
    recs = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(r.get("ts"))
                persona = r.get("persona")
                if ts and persona in AGENTS:
                    recs.append((ts, persona))
    except OSError:
        pass
    recs.sort(key=lambda x: x[0])
    return [t for t, _ in recs], [p for _, p in recs]


def _attribute(turn_ts, rec_ts, rec_persona, slack_s=180):
    """Persona of the marker most recently preceding turn_ts (the hook fires
    before the model replies, so marker ts <= assistant turn ts). Small slack
    forward covers clock skew on the very first marker."""
    if not rec_ts or turn_ts is None:
        return "unknown"
    i = bisect.bisect_right(rec_ts, turn_ts) - 1
    if i >= 0:
        return rec_persona[i]
    if (rec_ts[0] - turn_ts).total_seconds() <= slack_s:
        return rec_persona[0]
    return "unknown"


def _turns(session_dir=None, markers=None):
    sd = Path(session_dir) if session_dir else SESSION_DIR
    rec_ts, rec_persona = markers if markers is not None else _persona_markers()
    out = []
    seen = set()  # dedup: streaming writes one assistant message as N entries
    for jsonl in sorted(sd.glob("*.jsonl")):
        try:
            with open(jsonl, encoding="utf-8") as f:
                entries = []
                for ln in f:
                    try:
                        entries.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        channel = "telegram" if any(_is_channel_user(e) for e in entries) else "cli"
        for e in entries:
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            mid = msg.get("id") or e.get("uuid")
            if mid is not None:
                if mid in seen:
                    continue
                seen.add(mid)
            model = msg.get("model") or "unknown"
            if model in SKIP_MODELS:
                continue
            u = msg.get("usage") or {}
            if not isinstance(u, dict):
                continue
            ts = _parse_ts(e.get("timestamp"))
            agent = (_attribute(ts, rec_ts, rec_persona)
                     if channel == "telegram" else "cli")
            out.append({
                "ts": ts,
                "channel": channel,
                "agent": agent,
                "model": model,
                "input": u.get("input_tokens", 0),
                "output": u.get("output_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0),
                "cache_write": u.get("cache_creation_input_tokens", 0),
            })
    return out


def _agg(rows):
    n = len(rows)
    if not n:
        return {"turns": 0}
    tot_in = sum(t["input"] for t in rows)
    tot_out = sum(t["output"] for t in rows)
    tot_cr = sum(t["cache_read"] for t in rows)
    tot_cw = sum(t["cache_write"] for t in rows)
    total_context = tot_in + tot_cr + tot_cw
    by_model = {}
    for t in rows:
        by_model[t["model"]] = by_model.get(t["model"], 0) + 1
    return {
        "turns": n,
        "avg_context_per_turn": round(total_context / n),
        "avg_fresh_input_per_turn": round(tot_in / n),
        "avg_output_per_turn": round(tot_out / n),
        "cache_hit_rate": round(100 * tot_cr / total_context) if total_context else 0,
        "by_model": by_model,
    }


def summary(recent=50, session_dir=None, markers_path=None):
    """Per-channel aggregates (telegram, cli). Telegram is also broken down
    per agent. `recent` is applied PER CHANNEL — the last N turns of each — so
    a long dev session can't drown out the bot's numbers."""
    markers = _persona_markers(markers_path)
    turns = [t for t in _turns(session_dir, markers) if t["ts"]]
    turns.sort(key=lambda t: t["ts"])
    result = {}
    for ch in ("telegram", "cli"):
        ch_rows = [t for t in turns if t["channel"] == ch]
        ch_rows = ch_rows[-recent:] if recent else ch_rows
        agg = _agg(ch_rows)
        if ch == "telegram":
            by_agent = {}
            for ag in AGENTS + ("unknown",):
                ag_rows = [t for t in ch_rows if t["agent"] == ag]
                if ag_rows:
                    by_agent[ag] = _agg(ag_rows)
            agg["by_agent"] = by_agent
        agg["last"] = [{"ctx": t["input"] + t["cache_read"] + t["cache_write"],
                        "out": t["output"], "model": t["model"], "agent": t["agent"]}
                       for t in ch_rows[-6:]]
        result[ch] = agg
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tokens")
    ap.add_argument("--recent", type=int, default=50)
    args = ap.parse_args(argv)
    print(json.dumps(summary(args.recent), indent=2))


if __name__ == "__main__":
    main()
