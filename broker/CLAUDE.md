# broker — local context for agents

Scope: the Donna broker Python package. Everything under this directory.

Authoritative spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` (v1.1).
Use v1.1 wording. Do not rely on training data for RFC 8785, JCS, or
HMAC serialisation details — the spec and the test vectors in
`tests/canonicalize_vectors.json` are authoritative.

## Module conventions

- One responsibility per module (see `__init__.py` for the map).
- Public API only — anything not explicitly exported is internal.
- Type hints everywhere. `mypy --strict` is a gate, not a suggestion.
- Docstrings lead with the **spec section(s)** a function implements.
- No silent catches. If an operation fails, raise with a structured
  reason. The broker never returns an `{"ok": false}` without also
  emitting the correct audit event.
- No stack traces to stdout — broker error envelopes are
  `{status, error_code, message}`.

## Import rules

`import-linter` enforces these. See `.importlinter`.

- `policy.py` must not import any network-client module. Policy is pure
  local logic (§9.1).
- `canonicalize.py`, `requests_db.py`, `audit.py` same rule — local,
  deterministic, no outside I/O beyond the DB / audit file.
- `resolver.py` spawns subprocesses per §9.2.
- `executor.py` spawns subprocesses for capability-bound executors (§8).
- `creds.py` spawns a single subprocess (`age --decrypt`) per §17
  (Phase 2 age vault). Pure function boundary: inputs → plaintext
  bytes → audit event. No other module imports `subprocess` —
  `.importlinter`'s `subprocess-boundary` contract enforces this
  automatically.

## HMAC serialisation

Authoritative: §7.3. Read the "Canonical serialisation for HMAC input
is explicit" paragraph. Separator is `\x1f` (ASCII unit separator),
which never appears in any covered field. Integers are decimal
strings, no leading zero, sign only for negatives. Timestamps are
epoch-ms integers. Strings are UTF-8 bytes.

## Running tests

```bash
cd broker
# First-time setup:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
# Each run:
pytest
mypy broker tests
lint-imports
```

Coverage gate per module is `>= 95%` per Ralph's completion promise
(`ralph-prompts/<module>.md`). Suite-wide fail_under is 90 (see
`pyproject.toml`) so isolated Wave A merges don't block on unrelated
module coverage.

## Ralph waves

Per security-v1.1 §23. Only invoke ralph-loop on ONE module at a time.

- Wave A (parallel, independent, 4 worktrees): canonicalize, requests_db,
  audit, validator
- Wave B (parallel, depends on A, 2 worktrees): policy, resolver
- Wave C (sequential with checkpoints): executor, then main

Prompts are in `ralph-prompts/<module>.md`. Each has a pinned
completion promise (`MODULE_COMPLETE`) that only fires when every
success bar is met — do not loosen.

## Review bar

Before a worktree merges back to master:

1. `pytest` clean with no skipped tests (beyond those explicitly
   marked as Phase 2+ in the prompt).
2. `mypy --strict` clean.
3. `lint-imports` clean.
4. Coverage on the module under work `>= 95%`.
5. Spec references in docstrings resolve to live section numbers in
   v1.1. If the spec moved, the module's docstring moves too.
6. Quick human scan for silent-fail patterns (`except: pass`, bare
   `except`, `return None` on unexpected branches without audit).

## Out of scope for this directory

- OS user + sudoers config — done at `/etc/sudoers.d/`, tracked in
  `claude-telegram-hardened/README` + the spec, not here.
- Telegram server extensions — live under `claude-telegram-hardened/`.
- Hook scripts — live under `hooks/` at the repo root; they talk to
  the broker but are not the broker.
- Skills — live under `claude-telegram-hardened/skills/`.
