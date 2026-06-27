#!/usr/bin/env python3
"""Nightly "dream" — consolidate memory without forgetting.

Auto-discovers every (owner, topic) with active observations, promotes those that
recur past threshold into semantic memory (sources archived with provenance, never
deleted), then decays stale items (active -> stale, never deleted), then AUDITS
the brain (near-duplicates deterministically; contradictions via a best-effort
LLM pass over same-topic facts) into memory_issues. Run nightly.

2026-06-10: promotion summaries are now LLM-synthesised (one readable insight via
the local `claude` CLI, haiku tier) instead of a semicolon concatenation; every
LLM step degrades to the deterministic path when the CLI is unavailable.
"""
import json
import importlib.util
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

_MAX_CONTRADICTION_CHECKS = 20  # cap the nightly LLM pair-checks


def _pmem():
    spec = importlib.util.spec_from_file_location("pmem", ROOT / "tools" / "pmem.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _claude(prompt, timeout=60):
    """One best-effort headless `claude -p` (haiku) call → text or None."""
    try:
        p = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--model", "claude-haiku-4-5", "--allowedTools", ""],
            capture_output=True, text=True, timeout=timeout, cwd="/tmp")
        if p.returncode != 0:
            return None
        outer = json.loads(p.stdout)
        if not isinstance(outer, dict) or outer.get("is_error"):
            return None
        return str(outer.get("result") or "").strip() or None
    except Exception:
        return None


def _llm_summary(topic, contents):
    """Synthesise recurring observations into ONE readable third-person insight."""
    lines = "\n".join(f"- {c}" for c in contents)
    out = _claude(
        "These observations about Graham recur. Write ONE clear third-person "
        "sentence capturing the durable pattern (no preamble, no list, just the "
        f"sentence). Topic: {topic}\n{lines}")
    if out and len(out) < 300:
        return out.splitlines()[0].strip()
    return None  # caller falls back to the deterministic concat


def _llm_contradicts(a, b):
    """Best-effort: do these two facts contradict? Conservative on failure."""
    out = _claude(
        "Do these two stored facts about the same person CONTRADICT each other "
        "(could not both be true)? Answer exactly YES or NO.\n"
        f"A: {a}\nB: {b}")
    return bool(out and out.strip().upper().startswith("YES"))


def _audit_contradictions(pmem, checker):
    """LLM pass over same-(owner, topic) active semantic/episodic pairs, capped.
    Near-dups were already flagged deterministically by pmem.audit()."""
    conn = pmem.get_db()
    rows = conn.execute(
        "SELECT m.id, m.owner, m.content, t.topic FROM memories m "
        "JOIN memory_topics t ON t.memory_id=m.id "
        "WHERE m.status='active' AND m.kind IN ('semantic','episodic')").fetchall()
    by_key = {}
    for r in rows:
        by_key.setdefault((r["owner"], r["topic"]), []).append(r)
    found = checks = 0
    for group in by_key.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if checks >= _MAX_CONTRADICTION_CHECKS:
                    return found
                a, b = group[i], group[j]
                checks += 1
                try:
                    if checker(a["content"], b["content"]):
                        pmem.flag_issue(a["id"], b["id"], "contradiction",
                                        "flagged by nightly dream audit")
                        found += 1
                except Exception:
                    continue
    return found


def dream(promote_threshold=None, summarizer=None, contradiction_checker=None,
          use_llm=True):
    pmem = _pmem()
    if summarizer is None and use_llm:
        summarizer = _llm_summary
    if contradiction_checker is None and use_llm:
        contradiction_checker = _llm_contradicts
    conn = pmem.get_db()
    pairs = conn.execute(
        "SELECT DISTINCT m.owner, t.topic FROM memories m "
        "JOIN memory_topics t ON t.memory_id = m.id "
        "WHERE m.kind = 'observation' AND m.status = 'active'").fetchall()
    promoted = []
    for r in pairs:
        kw = {} if promote_threshold is None else {"threshold": promote_threshold}
        res = pmem.promote(r["topic"], r["owner"], summarizer=summarizer, **kw)
        if res.get("promoted"):
            promoted.append({"owner": r["owner"], "topic": r["topic"],
                             "semantic_id": res["semantic_id"]})
    swept = pmem.sweep()
    # the audit: deterministic near-dups, then (best-effort) LLM contradictions
    audit = pmem.audit()
    contradictions = (_audit_contradictions(pmem, contradiction_checker)
                      if contradiction_checker else 0)
    return {"promoted": promoted, "staled": swept["staled"], "deleted": 0,
            "issues": {"near_dups": audit["near_dups"],
                       "contradictions": contradictions}}


if __name__ == "__main__":
    print(json.dumps(dream(), indent=2))
