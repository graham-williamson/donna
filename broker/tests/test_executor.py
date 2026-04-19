"""Tests for broker.executor.

Spec: security-v1.1 §8 (binding absolute), §13.4 (revalidation), §11
(replay), §5 (executing → terminal).

Ralph target scope (see ralph-prompts/executor.md):
  - Dispatch by capability name only; unknown capability raises.
  - Capability A's approved row refuses to execute via B's executor
    (no aliasing).
  - Revalidation runs at execute time when handler declared; failure →
    row → failed{reason: stale}.
  - Durable-start transaction: writes execution_started + state +
    PID and commits BEFORE spawning the executor. Crash pre-spawn leaves
    row in a state resolvable via reconciliation on next startup.
  - Subprocess executor: sanitised env, closed fds, cwd ephemeral,
    timeout respected.
  - Executor crash mid-run → failed{reason: executor_crashed, exit_code}.
"""
from __future__ import annotations

import pytest

from broker import executor


def test_module_importable():
    assert hasattr(executor, "execute")
    assert hasattr(executor, "ExecutionError")


# TODO(phase-1 ralph): full coverage per spec-ref list above.
