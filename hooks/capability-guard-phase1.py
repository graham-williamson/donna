#!/usr/bin/env python3
"""capability-guard-phase1.py — Donna security v1.1 Phase 1 PreToolUse hook.

Spec: security-v1.1 §13.5 (hook contracts), §14 (hook model), §10
(failure semantics matrix).

Replaces the Phase 0 capability-guard.sh once the broker + OS setup +
sudoers line are in place. To activate, point the PreToolUse hook in
.claude/settings.local.json at this file instead of capability-guard.sh.

Policy:
  - Every MCP tool and every Bash call is routed through
    `sudo -u donna-broker /usr/local/bin/donna-broker policy-check`.
  - The broker consults mcp-tools.yaml and returns a structured
    decision (`allow` / `deny` / `block` + reason).
  - Broker unavailable or slow (>5s) → fail-closed block per §13.5 and
    §10 "Universal rule: ambiguity → fail closed".
  - Bash is NOT broker-gated for v1 (the broker manifest is for MCP
    tools only). For Bash, fall back to the Phase 0 string-level /
    argv allowlist so we still protect the shell surface.

Stdin: Claude Code PreToolUse hook envelope.
Stdout: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "allow"|"deny", "permissionDecisionReason": ...}}
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from typing import Any, Optional


BROKER_BIN = "/usr/local/bin/donna-broker"
BROKER_TIMEOUT_SECONDS = 5.0  # §13.5

# ---- Phase 0 Bash allowlist (kept for Bash, which is not broker-gated) -

SHELL_METACHARS = frozenset(";&|`<>*?$\\")
ENV_PREFIX_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")

BANNED_GIT_FLAGS_EXACT = {
    "--output", "--output-file", "--upload-pack", "--receive-pack",
    "--exec", "--config",
}
BANNED_GIT_LONG_PREFIXES = (
    "--output=", "--output-file=", "--format=", "--exec=", "--config=",
    "--upload-pack=", "--receive-pack=",
)
BANNED_GIT_SHORT_RE = re.compile(r"^-[GSoc]")

DONNA_ROOT_RE = re.compile(r"^/Users/grahamwilliamson/donna($|/)")

BROKER_MODES = {
    "request", "policy-check", "execute", "cancel", "reconcile",
    "status", "status-by-code", "list-pending", "list-recent",
    "audit-result", "rotate-hmac", "verify-audit",
}


def emit(decision: str, reason: str) -> None:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def deny(reason: str) -> None:
    emit("deny", f"capability-guard(phase1): {reason}")


def allow(reason: str = "allowed") -> None:
    emit("allow", f"capability-guard(phase1): {reason}")


# ---- Bash gating (unchanged from Phase 0) -------------------------------


def check_bash(command: str) -> None:
    if not isinstance(command, str):
        deny("Bash tool_input.command missing or not a string")
    for ch in command:
        if ch in SHELL_METACHARS:
            deny(f"Bash shell metacharacter rejected: {ch!r}")
    if ENV_PREFIX_RE.match(command.lstrip()):
        deny("Bash env-var prefix rejected (e.g. FOO=bar cmd)")
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        deny(f"Bash tokenisation failed: {e}")
    if not tokens:
        deny("Bash command empty")
    _check_bash_allowlist(tokens)


def _check_bash_allowlist(t: list[str]) -> None:
    if t and t[0] == "git":
        _check_banned_git(t)

    if (
        len(t) == 6
        and t[0] == "sudo" and t[1] == "-u" and t[2] == "donna-broker"
        and t[3] == BROKER_BIN
    ):
        mode = t[4]
        if mode not in BROKER_MODES:
            deny(f"broker mode {mode!r} not in §13.1 allowlist")
        try:
            json.loads(t[5])
        except Exception as e:
            deny(f"broker payload is not valid JSON: {e}")
        allow("sudo donna-broker")

    if len(t) == 2 and t[0] == "ls":
        path = t[1]
        if not DONNA_ROOT_RE.match(path):
            deny(f"ls path {path!r} outside /Users/grahamwilliamson/donna")
        if ".." in path:
            deny(f"ls path {path!r} contains '..' (directory-escape reject)")
        allow("ls under donna root")

    if len(t) == 2 and t[0] == "git" and t[1] == "status":
        allow("git status")
    if (
        len(t) == 5 and t[0] == "git" and t[1] == "log"
        and t[2] == "--oneline" and t[3] == "-n"
    ):
        n = t[4]
        if not re.fullmatch(r"[1-9][0-9]*", n):
            deny(f"git log -n value {n!r} not a positive integer")
        if int(n) > 200:
            deny(f"git log -n value {n} exceeds 200")
        allow("git log --oneline -n N")
    if len(t) == 3 and t[0] == "git" and t[1] == "diff" and t[2] == "--stat":
        allow("git diff --stat")
    if (
        len(t) == 3 and t[0] == "git" and t[1] == "diff"
        and not t[2].startswith("-")
    ):
        allow("git diff <path>")

    deny(f"Bash argv {t!r} not in §14.1 allowlist")


def _check_banned_git(t: list[str]) -> None:
    for tok in t:
        if tok in BANNED_GIT_FLAGS_EXACT:
            deny(f"banned git flag: {tok!r}")
        for pref in BANNED_GIT_LONG_PREFIXES:
            if tok.startswith(pref):
                deny(f"banned git flag prefix: {tok!r}")
        if BANNED_GIT_SHORT_RE.match(tok):
            deny(f"banned git short flag: {tok!r}")


# ---- MCP gating via broker policy-check --------------------------------


def check_mcp(tool_name: str, tool_input: dict[str, Any]) -> None:
    """Route MCP tool calls through `donna-broker policy-check`."""
    payload = {"tool_name": tool_name, "params": tool_input or {}}
    try:
        result = subprocess.run(
            ["sudo", "-u", "donna-broker", BROKER_BIN, "policy-check"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=BROKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # §13.5 / §10: fail closed on broker timeout.
        deny(f"broker timeout on policy-check for {tool_name}")
    except FileNotFoundError:
        # Broker not installed yet. Fail closed — this hook version
        # requires the broker. Fall back to capability-guard.sh Phase 0
        # if you need to work without it.
        deny("broker binary not installed (fall back to Phase 0 hook)")
    except Exception as e:
        deny(f"broker invocation error: {type(e).__name__}: {e}")

    if result.returncode not in (0, 1):
        # Exit 2 means internal broker bug — fail closed but be specific.
        deny(
            f"broker internal error (exit {result.returncode}); "
            f"stderr={result.stderr.decode('utf-8', errors='replace')[:300]}"
        )

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        deny(
            f"broker returned non-JSON output for {tool_name}: "
            f"{result.stdout[:300]!r}"
        )

    status = response.get("status")
    if status == "error":
        deny(
            f"broker error: {response.get('error_code')}: "
            f"{response.get('message')}"
        )

    decision = response.get("decision")
    if decision == "allow":
        allow(f"broker allowed {tool_name}")
    if decision == "deny":
        deny(f"broker denied {tool_name}: {response.get('reason', 'n/a')}")
    if decision == "block":
        # Medium/high tools require a broker request flow. The hook
        # blocks the direct MCP call; Donna must call `donna-broker
        # request` to start the approval flow.
        deny(
            f"{tool_name} requires approval; call "
            f"`donna-broker request` to start the approval flow"
        )
    deny(f"unexpected broker decision {decision!r} for {tool_name}")


# ---- main ---------------------------------------------------------------


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        deny(f"hook input not valid JSON: {e}")

    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}

    if tool_name == "Bash":
        check_bash(tool_input.get("command", ""))

    if tool_name.startswith("mcp__"):
        check_mcp(tool_name, tool_input)

    allow(f"out of Phase 1 gate scope: {tool_name}")


if __name__ == "__main__":
    main()
