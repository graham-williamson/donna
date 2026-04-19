# Ralph prompt — broker/audit.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §7.6
(posture + rotation + chain), §15 (event names + forbidden fields), §5
(integrity-failure scope: JSONL wins on conflict).

## Contract

```python
def write_event(audit_dir: str, event: dict) -> str: ...
def rotate_if_needed(audit_dir: str) -> Optional[str]: ...
def verify_chain(audit_dir: str) -> Optional[dict]: ...
```

## Behavioural requirements

1. **Append-only.** Open with `O_APPEND`. Never `seek()` or `truncate()`.
   Permissions: mode 0600 on the file, owner is the broker user.
2. **Canonical entry form.** Each event serialised deterministically
   (sorted keys, no whitespace, UTF-8) before hashing. Do not reuse
   `broker.canonicalize` — audit serialisation is its own contract so
   an audit bug can't be masked by a canonicalize bug.
3. **Chain.** Every entry carries `prev_hash = sha256(previous canonical
   entry)`. First entry of the very first segment uses 64 zeros.
4. **Rotation.** On write, check size > 100MB or segment age > 30 days;
   if either, append a final `{"event": "segment_seal",
   "segment_end_hash": "<sha>"}` to the current file, rename it to
   `audit-YYYY-MM-DD-NNN.log.sealed`, open a fresh segment, and write
   the new first entry with `prev_hash = segment_end_hash`.
5. **Redaction.** Reject write (raise AuditViolation) if the event dict
   contains any top-level or nested key matching §15 forbidden set:
   `{"params_json", "body", "email_body", "screenshot", "hmac_key",
   "bot_token"}`. Provenance-check before writing — never trust
   callers.
6. **verify_chain** walks all segments in lex order, verifies each
   entry's prev_hash matches the preceding canonical entry, and that
   each sealed segment's `segment_end_hash` matches the next segment's
   first `prev_hash`. Returns None on clean. Returns
   `{"file", "line", "reason"}` for the first break.

## Test surface

Build up `broker/tests/test_audit.py`:
- Basic append + chain-on-three-events.
- Forbidden-key rejection for each §15 banned field.
- Simulated rotation: monkey-patch size/age thresholds; assert sealed
  segment named correctly, next segment's prev_hash == prior tail.
- verify_chain on a clean 100-entry file → None.
- verify_chain after flipping one byte on line N → returns file/line/reason.
- verify_chain across two sealed segments — break on seal handoff.

## Success bars

1. `pytest broker/tests/test_audit.py` clean.
2. `mypy --strict` clean.
3. ≥ 95% coverage on `broker/audit.py`.
4. `lint-imports` reports `audit-no-network` kept.
5. Forbidden-fields test covers every entry in the §15 never-written
   list.

## Completion promise

`<promise>MODULE_COMPLETE</promise>` when all five bars are met.

## Invocation

```
/ralph-loop "implement broker/audit.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/audit.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 15
```
