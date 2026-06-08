#!/usr/bin/env python3
"""Shared tracker core. Local SQLite, verified-write contract.

Used by both Daru (now) and Donna (later). One standard schema, reused by any
coach/owner. The cardinal rule: a write is only ever reported as done when the
stored row is read back and returned. Every mutation re-SELECTs after the write
and returns the persisted row — callers report success only from that row.

Two tables:
  trackers          — a named thing someone tracks (a metric, a renewal, notes)
  tracker_entries   — individual datapoints/events logged against a tracker

kind: metric_log (e.g. workout sets) | date_record (e.g. renewals due) | freeform
visibility: private | shared  (chair/daru reads all; others read own + shared)
"""
import os
import re
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get(
    "TRACKER_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "trackers.db"),
)

VALID_KINDS = {"metric_log", "date_record", "freeform"}
VALID_VISIBILITY = {"private", "shared"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trackers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'freeform',
    spec_json TEXT NOT NULL DEFAULT '[]',
    visibility TEXT NOT NULL DEFAULT 'shared',
    created_at TEXT NOT NULL,
    UNIQUE(owner, slug)
);
CREATE TABLE IF NOT EXISTS tracker_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    date TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    num_value REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trk_owner ON trackers(owner);
CREATE INDEX IF NOT EXISTS idx_ent_tracker ON tracker_entries(tracker_id);
CREATE INDEX IF NOT EXISTS idx_ent_date ON tracker_entries(date);
CREATE INDEX IF NOT EXISTS idx_ent_num ON tracker_entries(num_value);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "tracker"


def get_db() -> sqlite3.Connection:
    """Open the shared tracker DB, creating dirs + schema on connect. WAL mode."""
    path = os.path.abspath(DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _tracker_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["spec"] = json.loads(d.get("spec_json") or "[]")
    except (ValueError, TypeError):
        d["spec"] = []
    return d


def _entry_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["data"] = json.loads(d.get("data_json") or "{}")
    except (ValueError, TypeError):
        d["data"] = {}
    return d


def _require_owner(owner: str) -> str:
    if not owner or not str(owner).strip():
        raise ValueError("owner must be a non-empty string")
    return str(owner).strip()


def define(owner: str, name: str, kind: str = "freeform", spec: list | None = None,
           visibility: str = "shared", slug: str | None = None) -> dict:
    """Create (or fetch, idempotently) a tracker. Returns the stored tracker row.

    Idempotent on (owner, slug); slug auto-derived from name if omitted. If a
    tracker with the same (owner, slug) exists, the existing row is returned
    unchanged (define is a no-op upsert — it never clobbers an existing spec).
    """
    owner = _require_owner(owner)
    if not name or not name.strip():
        raise ValueError("name must be a non-empty string")
    # `kind` is open-ended so the layer generalises to any situation. The three
    # canonical kinds get special handling (date_record → upcoming(); metric_log
    # → num_value charts); any other kind is accepted and treated as freeform
    # (data_json holds whatever fields the spec declares).
    kind = (kind or "freeform").strip() or "freeform"
    if visibility not in VALID_VISIBILITY:
        raise ValueError(f"bad visibility: {visibility}")
    slug = slugify(slug) if slug else slugify(name)
    spec_json = json.dumps(spec or [])
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT * FROM trackers WHERE owner=? AND slug=?", (owner, slug)
        ).fetchone()
        if existing:
            return _tracker_dict(existing)
        cur = conn.execute(
            "INSERT INTO trackers (owner, name, slug, kind, spec_json, visibility, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (owner, name.strip(), slug, kind, spec_json, visibility, now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trackers WHERE id=?", (cur.lastrowid,)).fetchone()
        return _tracker_dict(row)
    finally:
        conn.close()


def list_trackers(owner: str | None = None, include_shared: bool = True) -> list[dict]:
    """List trackers. owner=None → ALL (chair/daru view).

    With an owner, return that owner's trackers plus shared ones (when
    include_shared). Ordered newest first.
    """
    conn = get_db()
    try:
        if owner is None:
            rows = conn.execute("SELECT * FROM trackers ORDER BY id DESC").fetchall()
        elif include_shared:
            rows = conn.execute(
                "SELECT * FROM trackers WHERE owner=? OR visibility='shared' "
                "ORDER BY id DESC", (owner,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trackers WHERE owner=? ORDER BY id DESC", (owner,)
            ).fetchall()
        return [_tracker_dict(r) for r in rows]
    finally:
        conn.close()


def get(tracker_id: int) -> dict | None:
    """Fetch a single tracker by id, or None."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM trackers WHERE id=?", (tracker_id,)).fetchone()
        return _tracker_dict(row) if row else None
    finally:
        conn.close()


def _auto_num(data: dict) -> float | None:
    """Pick a sensible primary numeric to promote. Prefers 'weight', then the
    first numeric field encountered."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("weight"), (int, float)):
        return float(data["weight"])
    for v in data.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
    return None


def log(tracker_id: int, data: dict, date: str | None = None,
        num_value: float | None = None) -> dict:
    """Log an entry, then re-SELECT it and return the persisted row.

    This is the verification contract: the returned dict is the row as actually
    stored (with its new id). Callers MUST treat success as "log returned a row
    with an id" — never as "the insert call didn't raise". date defaults to
    today (UTC). owner is stamped from the tracker. If num_value is None we try
    to auto-promote a sensible numeric from data.
    """
    if not isinstance(data, dict):
        raise ValueError("data must be a dict")
    conn = get_db()
    try:
        trk = conn.execute("SELECT * FROM trackers WHERE id=?", (tracker_id,)).fetchone()
        if not trk:
            raise ValueError(f"no tracker with id {tracker_id}")
        owner = trk["owner"]
        d = date or today_str()
        nv = num_value if num_value is not None else _auto_num(data)
        cur = conn.execute(
            "INSERT INTO tracker_entries (tracker_id, owner, date, data_json, "
            "num_value, created_at) VALUES (?,?,?,?,?,?)",
            (tracker_id, owner, d, json.dumps(data), nv, now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tracker_entries WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        if row is None:  # paranoia: the write did not persist
            raise RuntimeError("verified-write failed: entry not found after insert")
        return _entry_dict(row)
    finally:
        conn.close()


def entries(tracker_id: int, limit: int = 20) -> list[dict]:
    """Newest-first entries for a tracker."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tracker_entries WHERE tracker_id=? "
            "ORDER BY id DESC LIMIT ?", (tracker_id, limit)
        ).fetchall()
        return [_entry_dict(r) for r in rows]
    finally:
        conn.close()


def series(tracker_id: int, field: str | None = None, limit: int = 200) -> list[dict]:
    """[{date, value}] for charting. value = num_value, or data[field] when a
    field is named. Oldest-first (chart order). Rows with no usable value are
    skipped."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tracker_entries WHERE tracker_id=? "
            "ORDER BY id DESC LIMIT ?", (tracker_id, limit)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if field:
            try:
                data = json.loads(r["data_json"] or "{}")
            except (ValueError, TypeError):
                data = {}
            value = data.get(field)
        else:
            value = r["num_value"]
        if value is None:
            continue
        out.append({"date": r["date"], "value": value})
    out.reverse()  # oldest-first for charting
    return out


def upcoming(owner: str | None = None, within_days: int | None = 120) -> list[dict]:
    """Upcoming date_record entries (date >= today), soonest first.

    For "car insurance renews in 3 weeks". Optionally bounded by within_days
    (None = no upper bound). owner=None → all owners (chair view). Each result
    carries the joined tracker name + slug.
    """
    today = today_str()
    conn = get_db()
    try:
        q = (
            "SELECT e.*, t.name AS tracker_name, t.slug AS tracker_slug "
            "FROM tracker_entries e JOIN trackers t ON t.id = e.tracker_id "
            "WHERE t.kind='date_record' AND e.date >= ? "
        )
        params: list = [today]
        if within_days is not None:
            horizon = (datetime.now(timezone.utc).date()
                       + timedelta(days=within_days)).strftime("%Y-%m-%d")
            q += "AND e.date <= ? "
            params.append(horizon)
        if owner is not None:
            q += "AND e.owner = ? "
            params.append(owner)
        q += "ORDER BY e.date ASC"
        rows = conn.execute(q, params).fetchall()
        return [_entry_dict(r) for r in rows]
    finally:
        conn.close()


def summary(owner: str, max_chars: int = 600) -> str:
    """Compact human/LLM-readable digest of an owner's trackers, for prompt
    injection. Each tracker → name + last entry; for date_records, the next
    upcoming. Empty string if the owner has no trackers."""
    owner = _require_owner(owner)
    trks = list_trackers(owner, include_shared=False)
    if not trks:
        return ""
    lines = []
    for t in trks:
        last = entries(t["id"], limit=1)
        bit = f"- {t['name']}"
        if t["kind"] == "date_record":
            up = upcoming(owner=owner, within_days=None)
            nxt = next((u for u in up if u["tracker_id"] == t["id"]), None)
            if nxt:
                label = nxt["data"].get("item") or nxt["data"].get("provider") or "next"
                bit += f": next {label} due {nxt['date']}"
            elif last:
                bit += f": last {last[0]['date']}"
        elif last:
            e = last[0]
            detail = _entry_detail(e)
            bit += f": last on {e['date']}" + (f" — {detail}" if detail else "")
        else:
            bit += ": (no entries yet)"
        lines.append(bit)
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


def _entry_detail(entry: dict) -> str:
    """Short human rendering of an entry's data for the digest."""
    data = entry.get("data") or {}
    if not data:
        if entry.get("num_value") is not None:
            return str(entry["num_value"])
        return ""
    parts = []
    for k, v in data.items():
        parts.append(f"{k}={v}")
    return ", ".join(parts[:5])


# ----------------------------- CLI ----------------------------------------

def _cmd_define(args):
    spec = json.loads(args.spec) if args.spec else None
    print(json.dumps(define(args.owner, args.name, kind=args.kind, spec=spec,
                            visibility=args.visibility, slug=args.slug), indent=2))


def _cmd_log(args):
    data = json.loads(args.data)
    print(json.dumps(log(args.tracker_id, data, date=args.date,
                         num_value=args.num_value), indent=2))


def _cmd_list(args):
    print(json.dumps(list_trackers(args.owner), indent=2))


def _cmd_upcoming(args):
    print(json.dumps(upcoming(owner=args.owner, within_days=args.within_days),
                     indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="tracker")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("define")
    d.add_argument("--owner", required=True)
    d.add_argument("--name", required=True)
    d.add_argument("--kind", default="freeform", choices=sorted(VALID_KINDS))
    d.add_argument("--spec", help="JSON list of field defs")
    d.add_argument("--visibility", default="shared", choices=sorted(VALID_VISIBILITY))
    d.add_argument("--slug")
    d.set_defaults(fn=_cmd_define)

    lg = sub.add_parser("log")
    lg.add_argument("tracker_id", type=int)
    lg.add_argument("--data", required=True, help="JSON object")
    lg.add_argument("--date")
    lg.add_argument("--num-value", type=float, dest="num_value")
    lg.set_defaults(fn=_cmd_log)

    ls = sub.add_parser("list")
    ls.add_argument("--owner")
    ls.set_defaults(fn=_cmd_list)

    up = sub.add_parser("upcoming")
    up.add_argument("--owner")
    up.add_argument("--within-days", type=int, default=120, dest="within_days")
    up.set_defaults(fn=_cmd_upcoming)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
