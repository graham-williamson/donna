# Phase 1 pre-flight handoff

Pre-flight work for security-v1.1 Phase 1 is complete. Everything below
is what Graham needs to do to get to the green baseline and fire Wave A.

Spec reference: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md`.

## What's in the repo now

- `broker/` — package skeleton. Every module has typed signatures,
  spec-section docstrings, and `NotImplementedError` bodies.
- `broker/tests/` — scaffolding. `test_canonicalize.py` drives off
  `canonicalize_vectors.json` with 24 vectors; other test files have
  smoke imports and TODO pointers to their Ralph prompt.
- `broker/tests/schemas/` — JSON-Schema Draft-07 for 4 representative
  capabilities (puregym_book, gmail_create_draft, gcal_create_event,
  notion_create_pages).
- `broker/ralph-prompts/` — one prompt per module (canonicalize,
  requests_db, audit, validator, policy, resolver, executor).
- `broker/requirements.in`, `broker/requirements-dev.in` — pinned
  top-level deps.
- `broker/pyproject.toml` — pytest / mypy strict / coverage config.
- `broker/.importlinter` — no-network contracts for policy,
  canonicalize, requests_db, audit.
- `broker/CLAUDE.md` — conventions + test-run guide + review bar.

## One-time setup (Graham, ~20 min)

```bash
cd /Users/grahamwilliamson/donna/broker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip pip-tools
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --output-file=requirements-dev.txt requirements-dev.in
pip-sync requirements-dev.txt
```

`pip-sync` installs the hash-locked dev env. Commit both
`.txt` files — they are the security-relevant artefact.

## Green baseline check (Graham)

```bash
cd /Users/grahamwilliamson/donna/broker
source .venv/bin/activate
pytest                 # all canonicalize vectors SKIPPED; other tests pass on smoke imports
mypy broker tests      # strict clean
lint-imports           # all 4 no-network contracts kept
```

Expected:
- pytest: `~50 passed, ~27 skipped` — vector tests skip cleanly because
  `canonicalize()` raises NotImplementedError.
- mypy: 0 errors.
- lint-imports: all contracts kept (modules don't import network libs yet
  — they're stubs).

If anything red, stop and let me know — pre-flight isn't done yet.

## Wave A — four parallel worktrees (Graham fires)

Per security-v1.1 §23.3. These four modules are independent; fire all
four ralph-loop invocations together, one per worktree.

```bash
# Create the four worktrees
git worktree add ../donna-canonicalize -b phase1/canonicalize
git worktree add ../donna-requests-db  -b phase1/requests-db
git worktree add ../donna-audit        -b phase1/audit
git worktree add ../donna-validator    -b phase1/validator

# Fire one ralph-loop per worktree (separate terminals)
cd ../donna-canonicalize && /ralph-loop "implement broker/canonicalize.py per $PWD/broker/ralph-prompts/canonicalize.md" --completion-promise "MODULE_COMPLETE" --max-iterations 15
cd ../donna-requests-db  && /ralph-loop "implement broker/requests_db.py per $PWD/broker/ralph-prompts/requests_db.md" --completion-promise "MODULE_COMPLETE" --max-iterations 15
cd ../donna-audit        && /ralph-loop "implement broker/audit.py        per $PWD/broker/ralph-prompts/audit.md"        --completion-promise "MODULE_COMPLETE" --max-iterations 15
cd ../donna-validator    && /ralph-loop "implement broker/validator.py    per $PWD/broker/ralph-prompts/validator.md"    --completion-promise "MODULE_COMPLETE" --max-iterations 15
```

Each completes with `<promise>MODULE_COMPLETE</promise>` only when its
success bars are met. Per-module human review (5-min scan) before
merge to master.

## Wave B — two parallel worktrees (after A merges)

```bash
git worktree add ../donna-policy   -b phase1/policy
git worktree add ../donna-resolver -b phase1/resolver
# policy depends on canonicalize + requests_db (both merged from A).
# resolver depends on validator (merged from A).
```

## Wave C — sequential, in master

- `executor.py` — `/ralph-loop --max-iterations 10`, human review
  gate before merge (§23.5).
- `main.py` — written manually, not via Ralph.

## What I cannot do from here

The live Phase 0 hook blocks me from running `git add`, `git commit`,
`pip`, `pytest`, or any Bash outside §14.1's six allowlist forms. That
means every step in this document — the pip-compile, the pytest
baseline, the `git add broker/` + initial commit, the worktree creation
and ralph-loop invocations — is yours to run.

When you're ready, start with the one-time setup. If pip-compile hits a
resolution issue or a test surprises you on the baseline, tell me and
I'll adjust the scaffolding. Otherwise, fire Wave A and ping me when
any worktree hits `MODULE_COMPLETE`.

## Out of scope for pre-flight (Phase 1 later work)

- OS user `donna-broker` + group `donna-bridge` (§23.6) — you run
  `dscl`, I verify.
- `/usr/local/bin/donna-broker` wrapper + `/etc/sudoers.d/donna-broker`
  (§17 Phase 1).
- HMAC key generation (§7.3).
- Telegram server extension (§12) — lives in
  `claude-telegram-hardened/`, not `broker/`.
- `/telegram:approval` skill (§12.7).
- Rewire Phase 0 hook to call `donna-broker policy-check` and
  `donna-broker audit-result` (§13.5).
- Activate CLAUDE.md Phase 1 rules 1–3 (§16).

These come after the broker package is complete. Big lump, but none of
it is on the critical path until Wave A+B are green.
