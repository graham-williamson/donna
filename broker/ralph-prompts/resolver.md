# Ralph prompt — broker/resolver.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §9
(policy-check purity + resolver subprocess isolation), §12.5 (field
provenance), §12.6 (approval prompt content), §7.7 (resolver-returned
strings are attacker-tainted for display).

**Wave B.** Depends on `validator` being merged.

## Contract

```python
def policy_check_mode(capability_name: str, params: dict) -> dict: ...
def request_mode(capability_name: str, params: dict, audit_writer) -> dict: ...
```

Both return dicts of fields for queue-file rendering. Each field is
`{"label": str, "value": str, "provenance": "broker" | "donna"}`.

## Behavioural requirements

### policy_check_mode (§9.1)

- Deterministic given manifest + params. No randomness, no clock reads.
- Does NOT spawn subprocesses, touch the network, or open files
  outside the manifest path.
- Hard 1s budget (caller enforces; resolver cooperates by being fast).
- Tests monkey-patch `subprocess.run` and `socket.socket` to raise on
  any call, and verify no invocation.

### request_mode (§9.2)

When a capability's declared resolver touches the network:

- Spawn a subprocess with `subprocess.run` and these guarantees:
  - `env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "<resolver-deps>"}`
    only — HMAC_KEY, BROKER_DB_PATH, any `*_TOKEN` absent.
  - `pass_fds = ()`.
  - `cwd = /tmp/donna-resolver-<uuid>` created fresh and removed
    afterwards.
  - `timeout` from capability manifest, default 10s.
  - `stdin` = JSON (capability name + params).
  - `stdout` = JSON validated against the per-resolver output schema.
  - `stderr` = captured, truncated to 4KB with marker, written via
    `audit_writer` as `resolver_stderr`.
- Enrichment failure (timeout / non-zero exit / schema fail) is
  non-blocking: return a degraded summary dict with a
  `provenance: "broker"` `"resolved_summary": "<...> (enrichment
  failed)"` and emit `audit.enrichment_failed`.
- Attacker-tainted output tagging (§7.7 / §12.5):
  - String fields from resolver → `provenance: "donna"`.
  - Integers / booleans / enums validated against schema → `broker`.

## Test surface

- policy_check_mode purity: monkey-patch blocks subprocess and socket.
- request_mode happy path: fake resolver returns valid schema; output
  provenance-tagged correctly.
- request_mode env sanitisation: resolver script that echoes its env
  back; assert no secrets present.
- request_mode fd isolation: resolver script that tries to read an
  open broker fd fails.
- request_mode timeout: resolver sleeps longer than timeout; broker
  emits `enrichment_failed`, returns degraded.
- request_mode stderr cap: resolver spams 10KB to stderr; broker logs
  exactly 4KB with truncation marker.
- request_mode schema-fail: resolver returns unexpected JSON;
  `enrichment_failed` emitted; graceful degrade.

## Success bars

1. `pytest broker/tests/test_resolver.py` clean.
2. `mypy --strict` clean.
3. ≥ 95% coverage on `broker/resolver.py`.
4. No `policy-check` test ever executes a subprocess (assert
   via monkey-patch).
5. Every env-sanitisation test names the secret var it checks for
   absence — no generic "env is clean".

## Completion promise

`<promise>MODULE_COMPLETE</promise>` when all five bars are met.

## Invocation

```
/ralph-loop "implement broker/resolver.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/resolver.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 15
```
