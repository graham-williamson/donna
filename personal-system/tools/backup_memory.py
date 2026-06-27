#!/usr/bin/env python3
"""Nightly local backup of Graham's data — the safety net behind the
archive-never-delete stance (so a rogue delete or a bad correction is always
recoverable).

Backs up the SHARED brain (memory.db) AND the Daru app DB (daru.db — goals,
curiosity, corrections) by default. Uses SQLite's online backup API so the
snapshot is CONSISTENT even while the app is mid-write (a plain cp of a WAL
database can tear). Timestamped copies land in <data>/backups/; old ones are
pruned to a rolling window.

Run:  python3 backup_memory.py [--keep N] [--db PATH ...]
Defaults: memory.db + daru.db, keep the last 14 of each.
"""
# launchd runs this under the system /usr/bin/python3 (3.9) — keep all annotations
# lazy so `str | None` / `list[str]` don't blow up at import on the old interpreter.
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "data"))

DEFAULT_DBS = [
    os.path.join(_DATA, "memory.db"),
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "daru", "daru.db")),
]
BACKUP_DIR = os.path.join(_DATA, "backups")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot(db_path: str, backup_dir: str = BACKUP_DIR) -> str | None:
    """Consistent online-backup snapshot of one DB → backups/<name>.<ts>.db.
    Returns the snapshot path, or None if the source doesn't exist."""
    if not os.path.exists(db_path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    name = os.path.splitext(os.path.basename(db_path))[0]
    dest = os.path.join(backup_dir, f"{name}.{_stamp()}.db")
    # VACUUM INTO writes a CONSISTENT, defragmented, single-file copy in plain
    # rollback-journal mode — even when the source is WAL — so each snapshot is ONE
    # self-contained .db with no -wal/-shm sidecars (which prune() wouldn't clean).
    src = sqlite3.connect(db_path)
    try:
        src.execute("VACUUM INTO ?", (dest,))
    finally:
        src.close()
    return dest


def prune(db_path: str, keep: int, backup_dir: str = BACKUP_DIR) -> list[str]:
    """Keep only the most recent `keep` snapshots for this DB; delete the rest.
    Returns the deleted paths. Manual (`.manual.`) snapshots are never pruned."""
    name = os.path.splitext(os.path.basename(db_path))[0]
    pat = re.compile(rf"^{re.escape(name)}\.\d{{8}}T\d{{6}}Z\.db$")
    if not os.path.isdir(backup_dir):
        return []
    snaps = sorted(f for f in os.listdir(backup_dir) if pat.match(f))
    doomed = snaps[:-keep] if keep > 0 else []
    removed = []
    for f in doomed:
        path = os.path.join(backup_dir, f)
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def run(dbs: list[str], keep: int = 14) -> dict:
    made, pruned = [], []
    for db in dbs:
        snap = snapshot(db)
        if snap:
            made.append(snap)
            pruned += prune(db, keep)
    return {"snapshots": made, "pruned": pruned}


def main():
    ap = argparse.ArgumentParser(description="Nightly local backup of memory.db + daru.db")
    ap.add_argument("--keep", type=int, default=14, help="rolling snapshots to retain per DB")
    ap.add_argument("--db", action="append", help="DB path(s) to back up (repeatable)")
    args = ap.parse_args()
    res = run(args.db or DEFAULT_DBS, keep=args.keep)
    for s in res["snapshots"]:
        print(f"backed up → {s}")
    for p in res["pruned"]:
        print(f"pruned    → {p}")
    if not res["snapshots"]:
        print("nothing backed up (no source DBs found)")


if __name__ == "__main__":
    main()
