# Ralph prompt — broker/policy.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §7.2
(idempotency), §7.3 (HMAC), §7.4 (cooldown + override), §7.7
(context_reason sanitisation), §13.2 (rate limits), §9.1 (purity).

**Wave B.** Depends on `canonicalize` and `requests_db` being merged.

## Contract

See `broker/policy.py` for the exact signatures. Summary:

```python
def idempotency_key(capability, canonical_params, date_component) -> str
def generate_approval_code() -> str
def compute_creation_hmac(key, request_id, capability, params_hash,
                          idempotency_key, risk_level, created_at,
                          approval_expires_at) -> str
def compute_approval_hmac(key, creation_msg, execution_expires_at,
                          approved_at) -> str
def verify_hmac(key, message, expected_hex) -> bool
def rate_limit_check(conn, capability, daily_cap, utc_date) -> bool
def rate_limit_increment(conn, capability, utc_date) -> None
def cooldown_remaining_seconds(conn, idempotency_key, cooldown_minutes) -> int
def sanitise_context_reason(raw) -> tuple[str, list[str]]
```

## Behavioural requirements

1. **No network imports.** Enforced by `policy-no-network` contract.
2. **HMAC serialisation** per §7.3:
   - Separator `\x1f`.
   - Integers as decimal strings, no leading zero, sign only for
     negatives.
   - Timestamps as epoch-ms integers.
   - Strings as UTF-8 bytes.
   - Every field the spec lists must be included in the message; adding
     a field to the §6 "immutable at creation" set without extending
     HMAC coverage is a Ralph failure.
3. **verify_hmac** uses `hmac.compare_digest` — not `==`.
4. **idempotency_key** matches explicit test vectors provided in the
   test file; use
   `sha256(capability || \x1f || canonical_params || \x1f || date_component)`.
5. **generate_approval_code**:
   - 6 chars from the RFC 4648 base32 alphabet minus `I L O U` (so 32
     symbols remain).
   - Use `secrets` module for randomness.
   - Over 10k samples, no character outside the alphabet appears.
6. **rate_limit_check**: returns True if under cap, False otherwise.
   `rate_limit_increment` adds 1 to `(capability, date_utc)` row,
   creating if absent. Denied/expired rows do NOT refund (test both
   paths).
7. **cooldown_remaining_seconds**: for a row in `denied` state within
   `cooldown_minutes`, returns positive seconds. 0 when expired.
8. **sanitise_context_reason** (§7.7):
   - Hard cap 200 chars → raise with `invalid_input` for longer.
   - Redact patterns in order:
     - URLs: `https?://\S+`
     - Base64 ≥ 24 chars: `[A-Za-z0-9+/=]{24,}`
     - Hex ≥ 24 chars: `[A-Fa-f0-9]{24,}`
     - Digits ≥ 6: `\d{6,}`
     - Non-ASCII outside standard Latin + typographic punctuation.
   - Return `(sanitised_text, redaction_types_list)`. Empty list on
     no redactions.

## Test surface

- HMAC vectors: explicit input→hex-digest pairs for at least 5 distinct
  shapes (covers field-type boundaries).
- idempotency_key vectors: same.
- sanitise_context_reason: explicit input → (output, redaction_types)
  for each pattern.
- Rate-limit path: concurrent-ish (single-threaded but through the DB)
  behaviour: burst, exceed, reset at UTC midnight.

## Success bars

1. `pytest broker/tests/test_policy.py` clean.
2. `mypy --strict` clean.
3. ≥ 95% coverage on `broker/policy.py`.
4. `lint-imports` reports `policy-no-network` kept.
5. Every §6 immutable-at-creation field appears in the creation-HMAC
   message — explicit assertion in a test that counts field inclusions.

## Completion promise

`<promise>MODULE_COMPLETE</promise>` when all five bars are met.

## Invocation

```
/ralph-loop "implement broker/policy.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/policy.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 15
```
