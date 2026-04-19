# Ralph prompt — broker/canonicalize.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §7.1.

## Contract

```python
def canonicalize(value: Any) -> bytes: ...
def params_hash(params: Any) -> str: ...
```

- `canonicalize(value)` returns RFC 8785 JCS canonical UTF-8 bytes.
  - Keys sorted by UTF-16 code unit sequence.
  - Numbers per ECMAScript `Number::toString` (JCS §3.2.2.3).
  - Strings with minimal JSON escapes (JCS §3.2.2.2).
  - No insignificant whitespace.
- `params_hash(params)` returns the hex sha256 of `canonicalize(params)`.

## Dependencies

- `rfc8785` (already in `requirements.in`). Prefer the library to
  hand-rolling; hand-rolled canonicalization is a supply-chain blast
  zone of its own.

## Test vectors

`broker/tests/canonicalize_vectors.json` is authoritative. Every entry
must pass. Add new vectors for any number/Unicode edge case before
changing behaviour — vectors-first.

## Success bars

All must be green before you emit the completion promise:

1. `pytest broker/tests/test_canonicalize.py` — zero skipped, zero
   failed. Every vector round-trips.
2. `mypy --strict broker/canonicalize.py` clean.
3. `pytest --cov=broker.canonicalize --cov-report=term-missing` reports
   ≥ 95% line+branch coverage for `canonicalize.py`.
4. `lint-imports` reports `canonicalize-no-network` contract as kept.
5. `canonicalize(json.loads(canonicalize(x).decode()))` == `canonicalize(x)`
   for every vector (idempotence).

## Completion promise

Emit exactly `<promise>MODULE_COMPLETE</promise>` only when all five
bars are met. Do not loosen any bar; a skipped test or a dropped
vector is a failure.

## Invocation

```
/ralph-loop "implement broker/canonicalize.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/canonicalize.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 15
```
