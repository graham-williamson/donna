# Personal multi-agent system

Graham's personal AI crew — 💁‍♀️ Donna (PA), 💪 Nike (trainer), 🌱 Esme (coach/therapist), 🗻 Bodhi (contemplative) — one brain, one set of hands (the donna-broker), one daemon.

Design spec: `../docs/superpowers/specs/2026-06-05-personal-multi-agent-system-design.md`
Build plans: `../docs/superpowers/plans/2026-06-05-personal-system-foundation-memory.md` (Plan 1) …

## Memory engine (`tools/pmem.py`)

Local, never-forget SQLite store (`data/memory.db`). Three strata in one table — episodic floor (permanent), observations (recurring), semantic (consolidated by nightly "dreaming"). Nothing is ever deleted; decay and consolidation only transition `status` and write provenance.

```
python3 tools/pmem.py add <entry.json>
python3 tools/pmem.py recall --topic <t> --persona <donna|nike|esme|bodhi>
python3 tools/pmem.py sweep
python3 tools/pmem.py promote --topic <t> --owner <persona>
python3 tools/pmem.py verify <id>
```

Run tests: `pytest personal-system/tests/ -v`
