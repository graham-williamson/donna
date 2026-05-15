#!/usr/bin/env python3
"""donna-memory.py — Donna's long-term operational memory.

SQLite-backed store for granular facts, routine logs, and knowledge
that doesn't belong in the always-loaded memory files or in Notion.
Designed for token efficiency: store granularly, query narrowly.

Write operations use a JSON file (avoids shell metacharacter issues
with the capability guard). Read operations use CLI args.

Usage:
    # Write — pass a JSON file
    python3 donna-memory.py add /tmp/donna-mem-xyz.json

    # Read — CLI args, output is JSON
    python3 donna-memory.py query --category motivation --limit 10
    python3 donna-memory.py query --category motivation --since 30
    python3 donna-memory.py search --text obstacle --limit 20
    python3 donna-memory.py recent --limit 5
    python3 donna-memory.py stats
    python3 donna-memory.py categories

    # Delete by ID
    python3 donna-memory.py delete /tmp/donna-mem-xyz.json

The capability guard allowlist permits only these subcommands and
flags. Keep it tight.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/Users/grahamwilliamson/donna/data/donna-memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    subcategory TEXT DEFAULT '',
    date TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    tags TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_category ON entries(category);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
CREATE INDEX IF NOT EXISTS idx_entries_content_hash ON entries(content_hash);
CREATE INDEX IF NOT EXISTS idx_entries_category_date ON entries(category, date);
"""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def cmd_add(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.file).read_text())
    category = data["category"]
    content = data["content"]
    subcategory = data.get("subcategory", "")
    tags = ",".join(data.get("tags", []))
    metadata = json.dumps(data.get("metadata", {}))
    date = data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ch = content_hash(content)

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM entries WHERE content_hash = ? AND category = ?",
        (ch, category),
    ).fetchone()
    if existing:
        json.dump({"status": "duplicate", "id": existing["id"],
                    "message": "entry already exists"}, sys.stdout)
        return

    conn.execute(
        "INSERT INTO entries (category, subcategory, date, content, "
        "content_hash, tags, metadata_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (category, subcategory, date, content, ch, tags, metadata,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    json.dump({"status": "added", "id": row_id}, sys.stdout)


def cmd_delete(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.file).read_text())
    entry_id = data["id"]
    conn = get_db()
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    json.dump({"status": "deleted", "id": entry_id}, sys.stdout)


def cmd_query(args: argparse.Namespace) -> None:
    conn = get_db()
    conditions = []
    params: list = []

    if args.category:
        conditions.append("category = ?")
        params.append(args.category)
    if args.subcategory:
        conditions.append("subcategory = ?")
        params.append(args.subcategory)
    if args.since:
        conditions.append("date >= date('now', ?)")
        params.append(f"-{args.since} days")

    where = " AND ".join(conditions) if conditions else "1=1"
    limit = min(args.limit or 20, 100)

    rows = conn.execute(
        f"SELECT id, category, subcategory, date, content, tags "
        f"FROM entries WHERE {where} ORDER BY date DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    json.dump([dict(r) for r in rows], sys.stdout)


def cmd_search(args: argparse.Namespace) -> None:
    conn = get_db()
    limit = min(args.limit or 20, 100)
    rows = conn.execute(
        "SELECT id, category, subcategory, date, content, tags "
        "FROM entries WHERE content LIKE ? ORDER BY date DESC LIMIT ?",
        (f"%{args.text}%", limit),
    ).fetchall()
    json.dump([dict(r) for r in rows], sys.stdout)


def cmd_recent(args: argparse.Namespace) -> None:
    conn = get_db()
    limit = min(args.limit or 10, 100)
    rows = conn.execute(
        "SELECT id, category, subcategory, date, content, tags "
        "FROM entries ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    json.dump([dict(r) for r in rows], sys.stdout)


def cmd_stats(args: argparse.Namespace) -> None:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    cats = conn.execute(
        "SELECT category, COUNT(*) as count FROM entries "
        "GROUP BY category ORDER BY count DESC"
    ).fetchall()
    oldest = conn.execute(
        "SELECT date FROM entries ORDER BY date ASC LIMIT 1"
    ).fetchone()
    json.dump({
        "total_entries": total,
        "categories": {r["category"]: r["count"] for r in cats},
        "oldest_entry": oldest["date"] if oldest else None,
    }, sys.stdout)


def cmd_categories(args: argparse.Namespace) -> None:
    conn = get_db()
    cats = conn.execute(
        "SELECT category, COUNT(*) as count, MAX(date) as latest "
        "FROM entries GROUP BY category ORDER BY count DESC"
    ).fetchall()
    json.dump([dict(r) for r in cats], sys.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add")
    p_add.set_defaults(func=cmd_add)
    p_add.add_argument("file")

    p_del = sub.add_parser("delete")
    p_del.set_defaults(func=cmd_delete)
    p_del.add_argument("file")

    p_query = sub.add_parser("query")
    p_query.set_defaults(func=cmd_query)
    p_query.add_argument("--category")
    p_query.add_argument("--subcategory")
    p_query.add_argument("--since", type=int)
    p_query.add_argument("--limit", type=int, default=20)

    p_search = sub.add_parser("search")
    p_search.set_defaults(func=cmd_search)
    p_search.add_argument("--text", required=True)
    p_search.add_argument("--limit", type=int, default=20)

    p_recent = sub.add_parser("recent")
    p_recent.set_defaults(func=cmd_recent)
    p_recent.add_argument("--limit", type=int, default=10)

    p_stats = sub.add_parser("stats")
    p_stats.set_defaults(func=cmd_stats)

    p_cats = sub.add_parser("categories")
    p_cats.set_defaults(func=cmd_categories)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
