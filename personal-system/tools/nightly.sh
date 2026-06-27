#!/bin/bash
# Nightly: (1) take ROLLING, timestamped, consistent snapshots of memory.db AND
# daru.db FIRST — captured BEFORE dream consolidates, so a rogue delete or bad
# correction is always recoverable from a prior day (kept 14 deep, in data/backups/).
# (2) consolidate memory (dream — only ever archives/promotes, never deletes).
# (3) keep the legacy single .backup of the floor for Time Machine to grab off-box.
PS=/Users/grahamwilliamson/donna/personal-system
LOG="$PS/data/dream.log"
echo "=== nightly run $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG"
/usr/bin/python3 "$PS/tools/backup_memory.py" --keep 14 >> "$LOG" 2>&1
/usr/bin/python3 "$PS/tools/dream.py" >> "$LOG" 2>&1
/usr/bin/sqlite3 "$PS/data/memory.db" ".backup '$PS/data/memory.backup.db'" >> "$LOG" 2>&1
echo "backup ok" >> "$LOG"
