# Ralph prompt — broker/executor.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §8
(execution binding absolute), §13.4 (revalidation), §11 (replay
semantics), §5 (executing → terminal states).

**Wave C.** Depends on all Wave A + B modules merged. `--max-iterations
10` and a manual review gate before merge (§23.5).

## Contract

```python
class ExecutionError(Exception): ...

def execute(capability, params, state_conn, audit_writer) -> dict: ...
```

## Behavioural requirements

1. **Dispatch by capability name only.** Unknown capability raises
   `ExecutionError(error_code="unknown_capability")`.
2. **No aliasing.** Capability A cannot execute via B's executor; a
   test that constructs an Approved row referencing A but passes
   capability B's object must raise
   `ExecutionError(error_code="capability_mismatch")`.
3. **Revalidation (§13.4).** If the capability declares
   `revalidate.handler`, call it with the declared arguments.
   Failure → transition `executing` → `failed{reason: stale, detail:
   <handler output>}`, audit `request_execution_failed`, return.
4. **Durable start (§11 rule 2):**
   In a single broker transaction:
     - Transition row → `executing`.
     - Audit `request_execution_started`.
     - Record PID field (to be added to schema in tests if missing).
     - COMMIT.
   Only then spawn.
5. **Subprocess executor:**
   - `env = {"PATH": "/usr/bin:/bin"}` plus any capability-declared
     credential env vars sourced from the age vault (Phase 2; for
     Phase 1 subprocess tests, use a dummy env).
   - `pass_fds = ()`.
   - `cwd = /tmp/donna-exec-<uuid>`.
   - `timeout = capability.executor.timeout_seconds`.
   - Exit 0 → `succeeded`, capture stdout as `result_json`.
   - Non-zero exit → `failed{reason: executor_crashed, exit_code}`.
   - Timeout → `failed{reason: executor_timeout}`.
6. **MCP-tool executor:**
   - Returns metadata, does NOT call the tool. Caller (Donna) re-runs
     the MCP tool on her next turn; the PostToolUse `audit-result`
     hook transitions `executing` → `succeeded` on success.
7. **Outcome recording:** `executed_at`, `result_json`, `error_code`,
   `error_message` set under the same transaction as the final state
   transition.

## Test surface

- Unknown capability → `ExecutionError`.
- Capability mismatch → `ExecutionError`.
- Revalidation fail → row → `failed{stale}`, audit present.
- Subprocess happy path: dummy executor script returns JSON.
- Subprocess exit 1 → `failed{executor_crashed, 1}`.
- Subprocess timeout → `failed{executor_timeout}`.
- MCP-tool path returns metadata without side effects.
- Durable-start ordering: if the subprocess fails to spawn, the row is
  already `executing` with PID=None; recovery path handles via
  `broker_crash_pre_execute`.

## Success bars

1. `pytest broker/tests/test_executor.py` clean.
2. `mypy --strict` clean.
3. ≥ 95% coverage on `broker/executor.py`.
4. Every failure path produces a structured audit event.
5. No subprocess path runs without explicit capability binding (test
   asserts dispatch goes through the capability-keyed table).

## Completion promise

`<promise>MODULE_COMPLETE</promise>` when all five bars are met.

## Invocation

```
/ralph-loop "implement broker/executor.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/executor.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 10
```
