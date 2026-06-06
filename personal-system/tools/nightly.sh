#!/bin/bash
# Nightly: consolidate memory (dream) then take a CONSISTENT backup of the floor.
# The sqlite .backup is the one irreplaceable snapshot Time Machine then captures
# off-box. dream() only ever archives/promotes — it never deletes.
PS=/Users/grahamwilliamson/donna/personal-system
LOG="$PS/data/dream.log"
echo "=== nightly run $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG"
/usr/bin/python3 "$PS/tools/dream.py" >> "$LOG" 2>&1
/usr/bin/sqlite3 "$PS/data/memory.db" ".backup '$PS/data/memory.backup.db'" >> "$LOG" 2>&1
echo "backup ok" >> "$LOG"
