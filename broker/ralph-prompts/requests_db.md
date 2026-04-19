# Ralph prompt — broker/requests_db.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §6
(schema), §7.5 (durability), §5 (state machine), §11 (replay semantics).

## Contract

See `broker/requests_db.py` for the exact signatures. Summary:

```python
SCHEMA_SQL: str        # transcribed verbatim from §6

@dataclass(frozen=True)
class Request: ...     # one attribute per column in §6

def open_db(path: str) -> sqlite3.Connection: ...
def insert_request(conn, request: Request) -> None: ...
def get_request(conn, request_id: str) -> Optional[Request]: ...
def get_by_approval_code(conn, code: str) -> Optional[Request]: ...
def get_by_idempotency_key(conn, key: str) -> Optional[Request]: ...
def transition(conn, request_id: str, from_state: str, to_state: str, **mutable_fields) -> None: ...
def count_pending(conn) -> int: ...
def daily_backup(conn, backup_dir: str) -> Optional[str]: ...
```

## Behavioural requirements

1. `open_db` sets `PRAGMA journal_mode=WAL`, installs schema,
   idempotent across calls.
2. Immutable-field triggers: attempting to `UPDATE` any field listed in
   §6 "Immutable at creation" or "Set once on approval" raises a SQLite
   error *except* for the integrity-failed transition path, which is a
   broker-owned quarantine write — it sets `state='integrity_failed'`
   and no other immutable field.
3. Partial unique indexes exactly as in §6 text. Test: two inserts
   with the same approval_code in `pending_approval` fail on the
   second.
4. `transition` consults a transition table derived from §5 and rejects
   any unlisted pair. Writes are atomic (single UPDATE per call).
5. `count_pending` is a `COUNT(*)` on `WHERE state='approved'`. Cheap.
6. `daily_backup` uses `sqlite3 connection.backup`. Is once-per-UTC-day
   idempotent (file name: `requests-YYYY-MM-DD.db`; if exists, no-op).
   Rotates to retain 14 days; older files hard-deleted.

## Test surface

Start from `broker/tests/test_requests_db.py` skeleton. Add tests for
each behavioural requirement above. Use the `broker_home` fixture
(conftest.py) for file paths. Tests must leave no state outside
`tmp_path`.

## Success bars

1. `pytest broker/tests/test_requests_db.py` clean.
2. `mypy --strict` on `broker/requests_db.py` + tests clean.
3. `pytest --cov=broker.requests_db` reports ≥ 95%.
4. `lint-imports` reports `requests-db-no-network` kept.
5. SQL injection attempt via any public API fails safely: every SQL
   call uses parameterised queries (verifiable by grep `?` vs `%s`
   patterns — no string-formatted SQL).

## Completion promise

`<promise>MODULE_COMPLETE</promise>` only after all five bars.

## Invocation

```
/ralph-loop "implement broker/requests_db.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/requests_db.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 15
```
