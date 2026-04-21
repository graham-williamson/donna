"""Tests for broker.validator.

Spec: security-v1.1 §8, §8.1, §13.4, §14.1.

Coverage aims:
  - capabilities.yaml happy path with subprocess and mcp_tool executors.
  - Missing required field raises for every REQUIRED_CAPABILITY_FIELDS.
  - Invalid risk_level, executor.type, windows, revalidate shape all raise.
  - Medium/high without revalidate → ManifestError.
  - not_applicable reason outside allowed set → ManifestError.
  - Both handler and not_applicable set → ManifestError.
  - param_schema $ref resolution (valid + missing file + invalid JSON).
  - mcp-tools.yaml happy path, duplicate tool, unknown risk, Playwright
    not blocked → raises.
  - validate_params accepts good, rejects bad with path in message.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from broker import validator


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def manifest_dir(tmp_path):
    """A manifest directory with a small param schema file and a working
    capabilities.yaml that we mutate per-test."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["class_id", "date"],
        "additionalProperties": False,
        "properties": {
            "class_id": {"type": "string"},
            "date": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        },
    }
    (tmp_path / "puregym_book.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    return tmp_path


def _write_manifest(manifest_dir: Path, yaml_content: str) -> Path:
    path = manifest_dir / "capabilities.yaml"
    path.write_text(yaml_content, encoding="utf-8")
    return path


GOOD_SUBPROCESS_YAML = """
capabilities:
  - name: puregym.book_class
    executor:
      type: subprocess
      binary: /usr/local/bin/puregym_book.py
      timeout_seconds: 120
    param_schema:
      $ref: ./puregym_book.json
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: medium
    revalidate:
      handler: puregym.check_class_bookable
      arguments: [class_id, date]
    idempotency_date_from: params.date
    approval_window_minutes: 120
    execution_window_minutes: 60
"""


GOOD_MCP_TOOL_YAML = """
capabilities:
  - name: gmail.create_draft
    executor:
      type: mcp_tool
      tool: mcp__claude_ai_Gmail__create_draft
    param_schema:
      $ref: ./puregym_book.json
    params_exact_match_required: true
    derived_fields_allowed: []
    risk_level: medium
    revalidate:
      not_applicable: stateless_write
    idempotency_date_from: created_utc
    approval_window_minutes: 1440
    execution_window_minutes: 720
"""


# ---- module surface ------------------------------------------------------


def test_module_importable():
    assert hasattr(validator, "load_capabilities")
    assert hasattr(validator, "load_mcp_tools")
    assert hasattr(validator, "validate_params")
    assert hasattr(validator, "ManifestError")
    assert hasattr(validator, "ParamValidationError")
    assert hasattr(validator, "Capability")


# ---- capabilities.yaml happy paths ---------------------------------------


def test_load_good_subprocess_manifest(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_SUBPROCESS_YAML)
    caps = validator.load_capabilities(str(path))
    assert "puregym.book_class" in caps
    cap = caps["puregym.book_class"]
    assert cap.executor_type == "subprocess"
    assert cap.executor_target == "/usr/local/bin/puregym_book.py"
    assert cap.risk_level == "medium"
    assert cap.revalidate["handler"] == "puregym.check_class_bookable"
    assert cap.approval_window_minutes == 120
    assert cap.execution_window_minutes == 60


def test_load_good_mcp_tool_manifest(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_MCP_TOOL_YAML)
    caps = validator.load_capabilities(str(path))
    cap = caps["gmail.create_draft"]
    assert cap.executor_type == "mcp_tool"
    assert cap.executor_target == "mcp__claude_ai_Gmail__create_draft"
    assert cap.revalidate["not_applicable"] == "stateless_write"


def test_load_capabilities_missing_file_raises(tmp_path):
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(tmp_path / "nope.yaml"))
    assert "not found" in str(exc.value)


def test_load_capabilities_invalid_yaml_raises(manifest_dir):
    path = _write_manifest(manifest_dir, "::: not: valid: yaml: [")
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "YAML parse error" in str(exc.value)


def test_load_capabilities_no_top_level_list_raises(manifest_dir):
    path = _write_manifest(manifest_dir, "foo: bar\n")
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_load_capabilities_entries_not_list_raises(manifest_dir):
    path = _write_manifest(manifest_dir, "capabilities: not-a-list\n")
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_duplicate_capability_name_raises(manifest_dir):
    yaml_content = GOOD_SUBPROCESS_YAML + GOOD_SUBPROCESS_YAML.replace(
        "capabilities:\n  -", "  -"
    )
    path = _write_manifest(manifest_dir, yaml_content)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "duplicate" in str(exc.value).lower()


# ---- required field presence --------------------------------------------


@pytest.mark.parametrize("field", list(validator.REQUIRED_CAPABILITY_FIELDS))
def test_missing_required_field_raises(manifest_dir, field):
    if field == "name":
        # Omitting `name` is a different failure path — caught by the
        # name-validation check rather than the generic required loop.
        bad = GOOD_SUBPROCESS_YAML.replace("name: puregym.book_class\n    ", "")
    else:
        lines = GOOD_SUBPROCESS_YAML.splitlines(keepends=True)
        # Drop the line(s) where this field is set at the capability level.
        filtered = [l for l in lines if not l.lstrip().startswith(f"{field}:")]
        bad = "".join(filtered)
        # If the field has nested content (executor, param_schema, revalidate),
        # also strip the next-level continuation lines.
        if field in {"executor", "param_schema", "revalidate"}:
            out = []
            skipping = False
            base_indent = None
            for l in bad.splitlines(keepends=True):
                # This filter already removed the header, so nested lines
                # that were under it will appear as deeper-indented orphans.
                # Drop them until indentation returns to <= base.
                stripped = l.lstrip()
                if not stripped or stripped.startswith("#"):
                    out.append(l)
                    continue
                indent = len(l) - len(stripped)
                if skipping:
                    if indent <= (base_indent or 0):
                        skipping = False
                    else:
                        continue
                out.append(l)
            bad = "".join(out)
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_missing_name_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace("name: puregym.book_class\n    ", "")
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "name" in str(exc.value).lower()


# ---- field-level validation ---------------------------------------------


def test_invalid_risk_level_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "risk_level: medium", "risk_level: extreme"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "risk_level" in str(exc.value)


def test_invalid_executor_type_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "type: subprocess", "type: magic_wand"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "executor.type" in str(exc.value)


def test_subprocess_missing_binary_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "binary: /usr/local/bin/puregym_book.py\n", ""
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_subprocess_non_positive_timeout_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "timeout_seconds: 120", "timeout_seconds: 0"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_mcp_tool_missing_tool_raises(manifest_dir):
    bad = GOOD_MCP_TOOL_YAML.replace(
        "      tool: mcp__claude_ai_Gmail__create_draft\n", ""
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_non_positive_approval_window_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "approval_window_minutes: 120", "approval_window_minutes: 0"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


# ---- revalidate rules ---------------------------------------------------


def test_medium_without_revalidate_raises(manifest_dir):
    lines = GOOD_SUBPROCESS_YAML.splitlines(keepends=True)
    # Remove the revalidate block (3 lines).
    filtered = []
    skip = 0
    for l in lines:
        if skip > 0:
            skip -= 1
            continue
        if l.lstrip().startswith("revalidate:"):
            skip = 2  # consume handler + arguments lines
            continue
        filtered.append(l)
    bad = "".join(filtered)
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "revalidate" in str(exc.value)


def test_invalid_not_applicable_reason_raises(manifest_dir):
    bad = GOOD_MCP_TOOL_YAML.replace(
        "not_applicable: stateless_write",
        "not_applicable: because_i_said_so",
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "not_applicable" in str(exc.value)


def test_both_handler_and_not_applicable_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "revalidate:\n      handler: puregym.check_class_bookable\n      arguments: [class_id, date]",
        "revalidate:\n      handler: puregym.check_class_bookable\n      arguments: [class_id, date]\n      not_applicable: stateless_write",
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_low_risk_no_revalidate_ok(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace("risk_level: medium", "risk_level: low")
    # Drop revalidate — low-risk doesn't require it.
    lines = bad.splitlines(keepends=True)
    filtered = []
    skip = 0
    for l in lines:
        if skip > 0:
            skip -= 1
            continue
        if l.lstrip().startswith("revalidate:"):
            skip = 2
            continue
        filtered.append(l)
    bad_no_reval = "".join(filtered)
    path = _write_manifest(manifest_dir, bad_no_reval)
    caps = validator.load_capabilities(str(path))
    assert caps["puregym.book_class"].risk_level == "low"


# ---- param_schema $ref resolution ---------------------------------------


def test_param_schema_ref_missing_file_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "$ref: ./puregym_book.json", "$ref: ./does_not_exist.json"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "does not exist" in str(exc.value)


def test_param_schema_ref_invalid_json_raises(manifest_dir):
    (manifest_dir / "broken.json").write_text(
        "not valid json {{{", encoding="utf-8"
    )
    bad = GOOD_SUBPROCESS_YAML.replace(
        "$ref: ./puregym_book.json", "$ref: ./broken.json"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "not valid JSON" in str(exc.value)


def test_param_schema_inline_ok(manifest_dir):
    inline = GOOD_SUBPROCESS_YAML.replace(
        "param_schema:\n      $ref: ./puregym_book.json",
        'param_schema:\n      type: object\n      properties: {}',
    )
    path = _write_manifest(manifest_dir, inline)
    caps = validator.load_capabilities(str(path))
    assert caps["puregym.book_class"].param_schema["type"] == "object"


def test_param_schema_invalid_schema_raises(manifest_dir):
    inline = GOOD_SUBPROCESS_YAML.replace(
        "param_schema:\n      $ref: ./puregym_book.json",
        'param_schema:\n      type: "not_a_valid_type"',
    )
    path = _write_manifest(manifest_dir, inline)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "JSON Schema" in str(exc.value)


# ---- mcp-tools.yaml -----------------------------------------------------


GOOD_MCP_YAML = """
tools:
  mcp__claude_ai_Gmail__gmail_search_messages: low
  mcp__claude_ai_Gmail__create_draft: medium
  mcp__plugin_playwright_playwright__browser_navigate: blocked
"""


def test_load_mcp_tools_happy(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    path.write_text(GOOD_MCP_YAML, encoding="utf-8")
    tools = validator.load_mcp_tools(str(path))
    assert tools["mcp__claude_ai_Gmail__gmail_search_messages"] == "low"
    assert tools["mcp__claude_ai_Gmail__create_draft"] == "medium"
    assert tools["mcp__plugin_playwright_playwright__browser_navigate"] == "blocked"


def test_load_mcp_tools_missing_file_raises(tmp_path):
    with pytest.raises(validator.ManifestError):
        validator.load_mcp_tools(str(tmp_path / "nope.yaml"))


def test_load_mcp_tools_invalid_yaml_raises(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    path.write_text(":::", encoding="utf-8")
    with pytest.raises(validator.ManifestError):
        validator.load_mcp_tools(str(path))


def test_load_mcp_tools_no_top_level_raises(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    path.write_text("other: stuff\n", encoding="utf-8")
    with pytest.raises(validator.ManifestError):
        validator.load_mcp_tools(str(path))


def test_load_mcp_tools_invalid_risk_raises(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    path.write_text("tools:\n  some_tool: nuclear\n", encoding="utf-8")
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_mcp_tools(str(path))
    assert "risk" in str(exc.value).lower()


def test_load_mcp_tools_playwright_not_blocked_raises(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    path.write_text(
        "tools:\n  mcp__plugin_playwright_playwright__browser_navigate: low\n",
        encoding="utf-8",
    )
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_mcp_tools(str(path))
    assert "Playwright" in str(exc.value)


# ---- validate_params ----------------------------------------------------


def test_validate_params_accepts_good(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_SUBPROCESS_YAML)
    caps = validator.load_capabilities(str(path))
    cap = caps["puregym.book_class"]
    validator.validate_params(cap, {"class_id": "hiit", "date": "2026-04-21"})


def test_validate_params_rejects_missing_required(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_SUBPROCESS_YAML)
    caps = validator.load_capabilities(str(path))
    cap = caps["puregym.book_class"]
    with pytest.raises(validator.ParamValidationError) as exc:
        validator.validate_params(cap, {"class_id": "hiit"})  # missing date
    assert "date" in str(exc.value)


def test_validate_params_rejects_wrong_type(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_SUBPROCESS_YAML)
    caps = validator.load_capabilities(str(path))
    cap = caps["puregym.book_class"]
    with pytest.raises(validator.ParamValidationError):
        validator.validate_params(cap, {"class_id": 42, "date": "2026-04-21"})


def test_validate_params_rejects_extra_properties(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_SUBPROCESS_YAML)
    caps = validator.load_capabilities(str(path))
    cap = caps["puregym.book_class"]
    with pytest.raises(validator.ParamValidationError):
        validator.validate_params(
            cap,
            {"class_id": "x", "date": "2026-04-21", "unexpected": 1},
        )


def test_validate_params_rejects_pattern_violation(manifest_dir):
    path = _write_manifest(manifest_dir, GOOD_SUBPROCESS_YAML)
    caps = validator.load_capabilities(str(path))
    cap = caps["puregym.book_class"]
    with pytest.raises(validator.ParamValidationError):
        validator.validate_params(cap, {"class_id": "x", "date": "bad"})


# ---- type-shape edge cases ------------------------------------------------


def test_revalidate_not_a_mapping_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "revalidate:\n      handler: puregym.check_class_bookable\n      arguments: [class_id, date]",
        "revalidate: just_a_string",
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError) as exc:
        validator.load_capabilities(str(path))
    assert "revalidate" in str(exc.value)


def test_revalidate_handler_not_string_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "      handler: puregym.check_class_bookable",
        "      handler: 42",
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_revalidate_handler_empty_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "      handler: puregym.check_class_bookable",
        '      handler: ""',
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_revalidate_arguments_not_list_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "      arguments: [class_id, date]",
        '      arguments: "class_id"',
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_executor_not_a_mapping_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "executor:\n      type: subprocess\n      binary: /usr/local/bin/puregym_book.py\n      timeout_seconds: 120",
        'executor: "string_instead_of_map"',
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_param_schema_not_a_mapping_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "param_schema:\n      $ref: ./puregym_book.json",
        'param_schema: "not_a_map"',
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_param_schema_ref_not_string_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "$ref: ./puregym_book.json", "$ref: 42"
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_idempotency_date_from_not_string_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "idempotency_date_from: params.date",
        "idempotency_date_from: 42",
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_params_exact_match_not_bool_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "params_exact_match_required: true",
        'params_exact_match_required: "yes"',
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_derived_fields_not_list_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "derived_fields_allowed: []",
        'derived_fields_allowed: "not_a_list"',
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_derived_fields_non_string_entry_raises(manifest_dir):
    bad = GOOD_SUBPROCESS_YAML.replace(
        "derived_fields_allowed: []",
        "derived_fields_allowed: [42]",
    )
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


def test_capability_entry_not_mapping_raises(manifest_dir):
    bad = "capabilities:\n  - just_a_string\n"
    path = _write_manifest(manifest_dir, bad)
    with pytest.raises(validator.ManifestError):
        validator.load_capabilities(str(path))


# ---- mcp-tools type-shape edge cases ------------------------------------


def test_mcp_tools_not_mapping_raises(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    path.write_text("tools: [a, b, c]\n", encoding="utf-8")
    with pytest.raises(validator.ManifestError):
        validator.load_mcp_tools(str(path))


def test_mcp_tools_name_not_string_raises(tmp_path):
    path = tmp_path / "mcp-tools.yaml"
    # YAML key "42" is a string by default; force an actual non-string
    # key by using !!int. Easier: test via empty string.
    path.write_text('tools:\n  "": low\n', encoding="utf-8")
    with pytest.raises(validator.ManifestError):
        validator.load_mcp_tools(str(path))


def test_validate_params_error_on_nested_path(manifest_dir):
    """ValidationError path surfaces the exact field that failed."""
    nested_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["outer"],
        "properties": {
            "outer": {
                "type": "object",
                "required": ["inner"],
                "properties": {"inner": {"type": "integer"}},
            },
        },
    }
    (manifest_dir / "nested.json").write_text(
        json.dumps(nested_schema), encoding="utf-8"
    )
    yaml_content = GOOD_SUBPROCESS_YAML.replace(
        "$ref: ./puregym_book.json", "$ref: ./nested.json"
    )
    path = _write_manifest(manifest_dir, yaml_content)
    caps = validator.load_capabilities(str(path))
    cap = caps["puregym.book_class"]
    with pytest.raises(validator.ParamValidationError) as exc:
        validator.validate_params(cap, {"outer": {"inner": "not an int"}})
    assert "outer/inner" in str(exc.value)


# ---- §4.2 creds block validation ----------------------------------------


def _good_subprocess_capability_yaml(extra: str = "") -> str:
    """Helper to generate a good subprocess capability YAML with optional
    extra fields appended to the capability entry."""
    return f"""capabilities:
  - name: gmail.create_draft
    executor:
      type: subprocess
      binary: /usr/local/bin/donna-exec-gmail
      timeout_seconds: 30
    param_schema:
      type: object
    risk_level: medium
    idempotency_date_from: created_at
    approval_window_minutes: 15
    execution_window_minutes: 5
    revalidate:
      not_applicable: stateless_write{extra}
"""


def _write_caps(manifest_dir: Path, capability_yaml: str) -> str:
    """Write a capability YAML to the manifest directory and return its path."""
    manifest = manifest_dir / "capabilities.yaml"
    manifest.write_text(capability_yaml, encoding="utf-8")
    return str(manifest)


def test_capability_without_creds_block_parses_with_none(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml())
    caps = validator.load_capabilities(caps_path)
    cap = next(iter(caps.values()))
    assert cap.creds is None


def test_capability_with_valid_creds_block_parses(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra="\n    creds:\n      delivery: fd3\n      entry: everyone_active"
    ))
    caps = validator.load_capabilities(caps_path)
    cap = next(iter(caps.values()))
    assert cap.creds is not None
    assert cap.creds.delivery == "fd3"
    assert cap.creds.entry == "everyone_active"


def test_creds_missing_entry_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra="\n    creds:\n      delivery: fd3"
    ))
    with pytest.raises(validator.ManifestError, match="entry"):
        validator.load_capabilities(caps_path)


def test_creds_invalid_delivery_enum_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra="\n    creds:\n      delivery: smoke_signals\n      entry: foo"
    ))
    with pytest.raises(validator.ManifestError, match="delivery"):
        validator.load_capabilities(caps_path)


def test_creds_invalid_entry_pattern_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra='\n    creds:\n      delivery: fd3\n      entry: "Has Spaces"'
    ))
    with pytest.raises(validator.ManifestError, match="entry"):
        validator.load_capabilities(caps_path)


def test_creds_string_instead_of_dict_raises(manifest_dir):
    caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
        extra='\n    creds: "yes"'
    ))
    with pytest.raises(validator.ManifestError, match="creds"):
        validator.load_capabilities(caps_path)


def test_creds_null_or_list_raises(manifest_dir):
    for bad in ("    creds: null", "    creds: []"):
        caps_path = _write_caps(manifest_dir, _good_subprocess_capability_yaml(
            extra=f"\n{bad}"
        ))
        with pytest.raises(validator.ManifestError, match="creds"):
            validator.load_capabilities(caps_path)
