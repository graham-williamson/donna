"""Capability-bound executor dispatch.

Spec: security-v1.1 §8 (execution binding absolute, no aliasing),
§13.4 (revalidation), §11 (replay semantics), §5 (executing → terminal).

Contract:
  - Dispatch by capability name only. No fuzzy matching, no substitution.
    Capability A approved only executes via A's declared executor.
  - Subprocess executors: env sanitised, fds closed, cwd ephemeral,
    timeout capability-configurable.
  - MCP-tool executors: return metadata describing the tool to re-run;
    the actual MCP invocation happens on Donna's next turn after the
    hook allows it (§12 worked example 19.2 steps 7–11).
  - Revalidate before execute for capabilities with `revalidate.handler`
    (§13.4). Failure → failed{reason: stale}.
  - Durable start (§11 rule 2): broker transaction writes
    `request_execution_started` + state=executing + PID, commits, THEN
    spawns. Crash pre-spawn surfaces as broker_crash_pre_execute on
    recovery.
  - Cancel during executing (Phase 2 addition per §17): SIGTERM → 5s
    grace → SIGKILL → failed{aborted_by_user}.

Phase 1 Ralph target — see `broker/ralph-prompts/executor.md`.
"""
from __future__ import annotations

from typing import Any


class ExecutionError(Exception):
    """Raised to signal structured failure. Carries error_code + message."""


def execute(
    capability: Any,
    params: dict[str, Any],
    state_conn: Any,
    audit_writer: Any,
) -> dict[str, Any]:
    """Run a capability. Performs revalidation, durable start, subprocess
    spawn (or MCP-tool metadata emit), outcome recording, state
    transition to succeeded/failed."""
    raise NotImplementedError("execute: Phase 1 Ralph target")
