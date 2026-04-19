"""Tests for broker.validator.

Spec: security-v1.1 §8, §8.1, §13.4.

Ralph target scope (see ralph-prompts/validator.md):
  - Good capabilities.yaml parses to Capability objects.
  - Missing any of executor / param_schema / idempotency_date_from
    raises ManifestError.
  - Every medium/high capability without revalidate declaration raises
    ManifestError. not_applicable reasons outside the allowed set raise.
  - mcp-tools.yaml parses to {tool_name: risk_level}; unknown risk
    values raise; duplicate tool entries raise.
  - validate_params() accepts good params, rejects bad with structured
    reason.
"""
from __future__ import annotations

import pytest

from broker import validator


def test_module_importable():
    assert hasattr(validator, "load_capabilities")
    assert hasattr(validator, "load_mcp_tools")
    assert hasattr(validator, "ManifestError")


# TODO(phase-1 ralph): full coverage per spec-ref list above.
