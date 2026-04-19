# Ralph prompt — broker/validator.py

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` §8
(manifest format, validation rules), §8.1 (MCP risk tiers), §13.4
(revalidate contract).

## Contract

```python
class ManifestError(Exception): ...

@dataclass(frozen=True)
class Capability: ...   # see broker/validator.py for fields

def load_capabilities(path: str) -> dict[str, Capability]: ...
def load_mcp_tools(path: str) -> dict[str, str]: ...
def validate_params(capability: Capability, params: Any) -> None: ...
```

## Behavioural requirements

1. `load_capabilities`:
   - Parses YAML at `path`; expects top-level `capabilities:` list.
   - For each capability: `executor`, `param_schema`, `risk_level`,
     `idempotency_date_from`, `approval_window_minutes`,
     `execution_window_minutes`, `params_exact_match_required`,
     `derived_fields_allowed` all required.
   - `risk_level` ∈ `{low, medium, high}`.
   - `executor.type` ∈ `{subprocess, mcp_tool}`; subprocess has
     `binary`+`timeout_seconds`, mcp_tool has `tool`.
   - Medium/high capabilities must have either
     `revalidate.handler` with arguments list, OR
     `revalidate.not_applicable` ∈
     `{stateless_write, idempotent_create, no_external_state}`.
   - `param_schema: {$ref: ...}` resolves to a local file; parses as
     valid JSON Schema Draft-07.
   - Any violation → `ManifestError` with structured reason.
2. `load_mcp_tools`:
   - Parses YAML; expects `tools:` dict of `{name: risk}`.
   - Valid risks: `{low, medium, high, blocked}`.
   - Duplicate names → `ManifestError`.
   - Every `mcp__plugin_playwright_*` pattern must be absent or
     `blocked` — Playwright is never allowed, even by manifest.
3. `validate_params`:
   - Uses `jsonschema` to validate `params` against
     `capability.param_schema`.
   - On failure, raises with a reason and path into the param tree.

## Test surface

Build up `broker/tests/test_validator.py`:
- Good capabilities.yaml (use one with puregym + gmail shapes) parses.
- Omit each required field → raises with that field in the reason.
- Medium capability missing revalidate → raises.
- Invalid `not_applicable` reason → raises.
- Unknown `risk_level` → raises.
- mcp-tools.yaml with duplicate tool → raises.
- mcp-tools.yaml with Playwright not `blocked` → raises.
- validate_params: good + bad params round-trip for each shipped schema.

## Success bars

1. `pytest broker/tests/test_validator.py` clean.
2. `mypy --strict` clean.
3. ≥ 95% coverage on `broker/validator.py`.
4. No network imports (policy module too strict here — ok since
   validator parses local files only).
5. Every required-field-missing path is tested (explicit parametrize
   listing all required fields).

## Completion promise

`<promise>MODULE_COMPLETE</promise>` when all five bars are met.

## Invocation

```
/ralph-loop "implement broker/validator.py per /Users/grahamwilliamson/donna/broker/ralph-prompts/validator.md" \
  --completion-promise "MODULE_COMPLETE" --max-iterations 15
```
