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
TRAMPOLINE_BIN = "/usr/local/bin/donna-broker-via-session"
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
    "audit-result", "rotate-hmac", "verify-audit", "verify-manifests",
}

# Structural fast-path for broker invocations. Bash treats every
# character inside a single-quoted run as literal, so a payload like
# '{"body":"<p>hi</p>"}' is safe even though `<` is in SHELL_METACHARS.
# The anchored regex pins the command to exactly:
#     sudo -u donna-broker <BROKER_BIN> <mode> '<json>'
# with [^']* for the JSON slot. Anything outside the quotes is pinned
# to literal characters by the regex — shell-substitution patterns
# ($(...), backticks, redirects, `;cmd`, `&& cmd`) cannot be smuggled
# because they would require characters the regex does not accept in
# those positions. This runs BEFORE the raw-string metachar scan so
# legitimate broker calls carrying angle brackets / `$` / etc. in
# JSON string values are not false-positive-rejected.
BROKER_CMD_RE = re.compile(
    r"^sudo -u donna-broker " + re.escape(BROKER_BIN)
    + r" (?P<mode>[a-z-]+) '(?P<json>[^']*)'$"
)

# Structural fast-path for the session trampoline — browser executors need
# the broker re-homed into donna-broker's own launchd session (Chromium ≥149
# SIGTRAPs in a borrowed GUI-session Mach namespace, 2026-06-11).
# Pins the command to exactly:
#     sudo -n <TRAMPOLINE_BIN> execute '<json>'
# Only `execute` is permitted — the trampoline is the broker CLI in disguise
# so all other modes are served by BROKER_CMD_RE instead. `-n` (non-interactive)
# is safe here because the binary path is pinned and single-quoting prevents
# shell expansion of the JSON payload.
TRAMPOLINE_CMD_RE = re.compile(
    r"^sudo -n " + re.escape(TRAMPOLINE_BIN)
    + r" execute '(?P<json>[^']*)'$"
)

# Structural fast-path for python3 syntax-check invocations.
# Pins the command to exactly:
#     python3 -c "import py_compile; py_compile.compile('<path>', doraise=True)"
# The path slot is constrained to characters that cannot escape the donna root
# check performed after the regex matches. Shell-substitution patterns cannot
# be smuggled because the outer double-quote run is anchored at both ends and
# the path slot allows only safe filesystem characters.
PYCOMPILE_CMD_RE = re.compile(
    r'^python3 -c "import py_compile; py_compile\.compile\(\'(?P<path>[^\']+)\', doraise=True\)"$'
)


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

    # Structural fast-path for broker invocations — see BROKER_CMD_RE
    # docstring. Validates mode + JSON and short-circuits before the
    # raw-string metachar scan, which would otherwise false-positive
    # on `<`, `>`, `$` etc. appearing inside JSON string values.
    m = BROKER_CMD_RE.match(command)
    if m:
        mode = m.group("mode")
        if mode not in BROKER_MODES:
            deny(f"broker mode {mode!r} not in §13.1 allowlist")
        try:
            json.loads(m.group("json"))
        except Exception as e:
            deny(f"broker payload is not valid JSON: {e}")
        allow("sudo donna-broker")

    # Structural fast-path for the session trampoline (browser executors).
    # Only `execute` is allowed — see TRAMPOLINE_CMD_RE docstring.
    m_tramp = TRAMPOLINE_CMD_RE.match(command)
    if m_tramp:
        try:
            json.loads(m_tramp.group("json"))
        except Exception as e:
            deny(f"trampoline payload is not valid JSON: {e}")
        allow("sudo -n donna-broker-via-session execute")

    # Structural fast-path for python3 py_compile syntax checks — see
    # PYCOMPILE_CMD_RE docstring. Short-circuits before the raw-string
    # metachar scan, which would otherwise reject `;` inside the -c arg.
    m2 = PYCOMPILE_CMD_RE.match(command)
    if m2:
        path = m2.group("path")
        if not DONNA_ROOT_RE.match(path):
            deny(f"py_compile path {path!r} outside /Users/grahamwilliamson/donna")
        if ".." in path:
            deny(f"py_compile path {path!r} contains '..'")
        allow("python3 py_compile syntax check under donna root")

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


PS_TOOLS_DIR = "/Users/grahamwilliamson/donna/personal-system/tools/"
PS_IDENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,49}")
PS_COLOURS = {"green", "purple", "red", "black", "pink", "gold", "white", "blue"}


def _ps_flags_ok(args: list[str], flag_specs: dict, bool_flags: set) -> bool:
    """Validate a flag/value list for personal-system tools."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in bool_flags:
            i += 1
            continue
        if tok in flag_specs:
            if i + 1 >= len(args):
                return False
            val = args[i + 1]
            spec = flag_specs[tok]
            if spec == "int100":
                if not re.fullmatch(r"[1-9][0-9]*", val) or int(val) > 100:
                    return False
            elif not PS_IDENT_RE.fullmatch(val):
                return False
            i += 2
            continue
        return False
    return True


def _check_personal_system(rest: list[str], script: str) -> None:
    """Allowlist for personal-system memory tools. Local SQLite + JSON only:
    no secrets, no network, never-delete — low blast radius. Reached only after
    the metachar scan + shlex tokenisation have already run on the command."""
    if script == "pmem.py":
        if not rest:
            deny("pmem.py: subcommand required")
        cmd = rest[0]
        if cmd == "add":
            if len(rest) == 2 and re.fullmatch(r"/tmp/pmem-[a-zA-Z0-9_\-]+\.json", rest[1]):
                allow("pmem.py add")
            deny("pmem.py add: requires /tmp/pmem-*.json")
        if cmd == "sweep" and len(rest) == 1:
            allow("pmem.py sweep")
        if cmd == "verify" and len(rest) == 2 and re.fullmatch(r"[1-9][0-9]*", rest[1]):
            allow("pmem.py verify")
        if cmd in ("recall", "promote"):
            specs = {"--topic": "id", "--persona": "id", "--owner": "id",
                     "--kind": "id", "--limit": "int100", "--threshold": "int100"}
            if _ps_flags_ok(rest[1:], specs, {"--include-stale"}):
                allow(f"pmem.py {cmd}")
            deny(f"pmem.py {cmd}: bad flags")
        deny(f"pmem.py: bad invocation {rest!r}")
    if script == "goals.py":
        if not rest:
            deny("goals.py: subcommand required")
        cmd = rest[0]
        if cmd == "add" and len(rest) >= 3 and rest[2] in PS_COLOURS:
            allow("goals.py add")
        if cmd in ("commit", "achieve") and len(rest) == 2 and re.fullmatch(r"[1-9][0-9]*", rest[1]):
            allow(f"goals.py {cmd}")
        if cmd == "list":
            allow("goals.py list")
        deny(f"goals.py: bad invocation {rest!r}")
    if script == "evidence.py":
        if not rest:
            allow("evidence.py surface")
        if rest[0] == "log" and len(rest) >= 2:
            allow("evidence.py log")
        deny(f"evidence.py: bad invocation {rest!r}")
    if script == "dream.py":
        if not rest:
            allow("dream.py")
        deny("dream.py: no args")
    if script == "tokens.py":
        if not rest:
            allow("tokens.py")
        if rest[0] == "--recent" and len(rest) == 2 and re.fullmatch(r"[1-9][0-9]*", rest[1]):
            allow("tokens.py --recent")
        deny(f"tokens.py: bad invocation {rest!r}")
    if script == "habits.py":
        if not rest:
            deny("habits.py: subcommand required")
        cmd = rest[0]
        if cmd in ("list", "seed", "due") and len(rest) == 1:
            allow(f"habits.py {cmd}")
        if cmd in ("done", "streak") and len(rest) == 2 and re.fullmatch(r"[1-9][0-9]*", rest[1]):
            allow(f"habits.py {cmd}")
        if cmd == "add" and len(rest) >= 3:
            allow("habits.py add")
        deny(f"habits.py: bad invocation {rest!r}")
    deny(f"personal-system tool not allowed: {script!r}")


def _check_bash_allowlist(t: list[str]) -> None:
    # Broker invocations are handled by the BROKER_CMD_RE fast-path in
    # check_bash(); they never reach this allowlist. The structural
    # regex is the single source of truth for what counts as a valid
    # broker call.
    if t and t[0] == "git":
        _check_banned_git(t)

    if len(t) == 2 and t[0] == "ls":
        path = t[1]
        if not DONNA_ROOT_RE.match(path):
            deny(f"ls path {path!r} outside /Users/grahamwilliamson/donna")
        if ".." in path:
            deny(f"ls path {path!r} contains '..' (directory-escape reject)")
        allow("ls under donna root")

    # Local cost tally — read-only aggregate over session JSONLs. Narrow
    # argv whitelist: only the documented flags are accepted. Any extra
    # token fails closed so injection via unexpected options doesn't work.
    DONNA_COST_SCRIPT = "/Users/grahamwilliamson/donna/tools/donna-cost.py"
    if len(t) >= 2 and t[0] == "python3" and t[1] == DONNA_COST_SCRIPT:
        valid_since = {"today", "week", "month", "all"}
        rest = t[2:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "--since":
                if i + 1 >= len(rest) or rest[i + 1] not in valid_since:
                    deny(
                        f"donna-cost.py: --since requires one of "
                        f"{sorted(valid_since)}"
                    )
                i += 2
                continue
            if tok == "--json":
                i += 1
                continue
            deny(f"donna-cost.py: unexpected arg {tok!r}")
        allow("donna-cost.py")

    # donna-memory.py: long-term operational memory (SQLite-backed).
    # Write ops use a JSON file (avoids metachar issues with content).
    # Read ops use CLI args with constrained values.
    DONNA_MEMORY_SCRIPT = "/Users/grahamwilliamson/donna/tools/donna-memory.py"
    if len(t) >= 2 and t[0] == "python3" and t[1] == DONNA_MEMORY_SCRIPT:
        valid_commands = {"query", "search", "recent", "stats", "categories"}
        valid_file_commands = {"add", "delete"}
        rest = t[2:]
        if not rest:
            deny("donna-memory.py: subcommand required")
        cmd = rest[0]
        if cmd in valid_file_commands:
            if len(rest) == 2 and re.fullmatch(
                r"/tmp/donna-mem-[a-zA-Z0-9_\-]+\.json", rest[1]
            ):
                allow(f"donna-memory.py {cmd}")
            deny(f"donna-memory.py {cmd}: requires /tmp/donna-mem-*.json")
        if cmd in valid_commands:
            valid_flags = {
                "--category", "--subcategory", "--since",
                "--limit", "--text",
            }
            i = 1
            while i < len(rest):
                tok = rest[i]
                if tok in valid_flags:
                    if i + 1 >= len(rest):
                        deny(f"donna-memory.py: {tok} requires a value")
                    val = rest[i + 1]
                    if tok in ("--since", "--limit"):
                        if not re.fullmatch(r"[1-9][0-9]*", val):
                            deny(
                                f"donna-memory.py: {tok} value "
                                f"{val!r} not a positive integer"
                            )
                        if tok == "--limit" and int(val) > 100:
                            deny(f"donna-memory.py: --limit exceeds 100")
                        if tok == "--since" and int(val) > 3650:
                            deny(f"donna-memory.py: --since exceeds 3650")
                    if tok in ("--category", "--subcategory"):
                        if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", val):
                            deny(
                                f"donna-memory.py: {tok} value "
                                f"{val!r} not a valid identifier"
                            )
                    if tok == "--text":
                        if not re.fullmatch(r"[a-zA-Z0-9_ -]{1,100}", val):
                            deny(
                                f"donna-memory.py: --text value "
                                f"{val!r} contains invalid characters"
                            )
                    i += 2
                    continue
                deny(f"donna-memory.py: unexpected arg {tok!r}")
            allow(f"donna-memory.py {cmd}")
        deny(f"donna-memory.py: unknown subcommand {cmd!r}")

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

    # donna-broker-send.py: reads a JSON file and pipes it to the broker
    # via stdin, bypassing the single-quote constraint in BROKER_CMD_RE.
    # Constrained to a specific script path + known mode + /tmp/donna-*.json.
    DONNA_BROKER_SEND = "/Users/grahamwilliamson/donna/tools/donna-broker-send.py"
    if len(t) == 4 and t[0] == "python3" and t[1] == DONNA_BROKER_SEND:
        if t[2] in BROKER_MODES:
            if re.fullmatch(r"/tmp/donna-[a-zA-Z0-9_\-]+\.json", t[3]):
                allow("donna-broker-send.py")
        deny("donna-broker-send.py: invalid invocation")

    # chmod +x for executor scripts under donna root only.
    if len(t) == 3 and t[0] == "chmod" and t[1] == "+x":
        path = t[2]
        if not DONNA_ROOT_RE.match(path):
            deny(f"chmod path {path!r} outside /Users/grahamwilliamson/donna")
        if ".." in path:
            deny(f"chmod path {path!r} contains '..' (directory-escape reject)")
        allow("chmod +x under donna root")

    # python3 syntax check: py_compile on files under donna root.
    # Accepts: python3 -c "import py_compile; py_compile.compile('<path>', doraise=True)"
    if (
        len(t) == 3 and t[0] == "python3" and t[1] == "-c"
        and t[2].startswith("import py_compile; py_compile.compile(")
    ):
        inner = t[2]
        path_match = re.search(r"py_compile\.compile\('([^']+)',\s*doraise=True\)", inner)
        if path_match:
            path = path_match.group(1)
            if not DONNA_ROOT_RE.match(path):
                deny(f"py_compile path {path!r} outside /Users/grahamwilliamson/donna")
            if ".." in path:
                deny(f"py_compile path {path!r} contains '..'")
            allow("python3 py_compile syntax check under donna root")
        deny("python3 -c py_compile: path not parseable or missing doraise=True")

    # personal-system memory tools (local SQLite + goals JSON; no secrets/network).
    if len(t) >= 2 and t[0] == "python3" and t[1].startswith(PS_TOOLS_DIR):
        _check_personal_system(t[2:], t[1][len(PS_TOOLS_DIR):])

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
        # request` to start the approval flow. If the broker surfaced
        # a `params_mismatch` diagnostic (an executing row exists for
        # this capability but params don't canonically match), include
        # it so Donna can fix the shape instead of re-requesting.
        mismatch = response.get("params_mismatch")
        if isinstance(mismatch, dict):
            deny(
                f"{tool_name} blocked: params don't match approved row "
                f"{mismatch.get('request_id')} — diff: "
                f"{json.dumps(mismatch.get('diff'), sort_keys=True)[:400]}"
            )
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
