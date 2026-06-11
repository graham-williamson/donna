#!/usr/bin/env python3
"""capability-guard.sh — Donna security v1.1 Phase 0 PreToolUse hook.

Spec: /Users/grahamwilliamson/.claude/plans/donna-security-v1.md §14.1

Policy surface (Phase 0):
  - Unconditional block: every mcp__plugin_playwright_* tool (§8.1).
  - MCP default-deny: allow only the low-risk read set hardcoded below.
    TODO(phase-1): replace with loader for broker/mcp-tools.yaml (§8).
  - Bash: reject shell metacharacters / env-var prefix at the string layer,
    then require exact tokenised argv match against the §14.1 allowlist,
    with banned git flags enumerated as a belt-and-braces negative filter.

Stdin: Claude Code PreToolUse hook JSON envelope.
Stdout: JSON decision envelope. Exit 0 in all allow/deny paths; fail-closed
        on parse errors by emitting deny.

Shebang is python3 (stdlib only) because the policy logic needs shlex
tokenisation and safe JSON I/O. The .sh extension is kept to match the
deliverable filename in the spec.
"""
import json
import re
import shlex
import sys


# §8.1 low-risk MCP read allowlist. Hardcoded for Phase 0.
# TODO(phase-1): swap for broker/mcp-tools.yaml loader (§8).
MCP_ALLOW_EXACT = {
    # Gmail reads
    "mcp__claude_ai_Gmail__gmail_search_messages",
    "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__list_drafts",
    "mcp__claude_ai_Gmail__list_labels",
    "mcp__claude_ai_Gmail__search_threads",
    # Google Calendar reads
    "mcp__claude_ai_Google_Calendar__list_calendars",
    "mcp__claude_ai_Google_Calendar__list_events",
    "mcp__claude_ai_Google_Calendar__get_event",
    "mcp__claude_ai_Google_Calendar__suggest_time",
    # Notion reads
    "mcp__plugin_Notion_notion__notion-fetch",
    "mcp__plugin_Notion_notion__notion-search",
    "mcp__plugin_Notion_notion__notion-get-users",
    "mcp__plugin_Notion_notion__notion-get-teams",
    "mcp__plugin_Notion_notion__notion-get-comments",
    # Telegram low-risk outbound + reads
    "mcp__plugin_telegram_telegram__reply",
    "mcp__plugin_telegram_telegram__react",
    "mcp__plugin_telegram_telegram__edit_message",
    "mcp__plugin_telegram_telegram__get_history",
    "mcp__plugin_telegram_telegram__search_messages",
    # Sentry whoami + Seer analysis + docs
    "mcp__claude_ai_Sentry__whoami",
    "mcp__claude_ai_Sentry__analyze_issue_with_seer",
    "mcp__claude_ai_Sentry__get_doc",
    "mcp__claude_ai_Sentry__search_docs",
    # Gamma reads
    "mcp__claude_ai_Gamma__read_gamma",
    "mcp__claude_ai_Gamma__get_folders",
    "mcp__claude_ai_Gamma__get_themes",
    "mcp__claude_ai_Gamma__get_generation_status",
}

MCP_ALLOW_PREFIXES = (
    "mcp__claude_ai_Sentry__search_",
    "mcp__claude_ai_Sentry__find_",
    "mcp__claude_ai_Sentry__get_",
)

# §13.1 broker mode names (Phase 0 has no broker — list is present so the
# Bash allowlist is already complete ahead of Phase 1 rollout).
BROKER_MODES = {
    "request", "policy-check", "execute", "cancel", "reconcile", "status",
    "status-by-code", "list-pending", "list-recent", "audit-result",
    "rotate-hmac", "verify-audit",
}

# §14.1 string-level rejects. `$` alone is banned (not just `$(`) to stop
# `$VAR` env expansion reaching any `sh -c` wrapper beneath the hook.
SHELL_METACHARS = frozenset(";&|" + chr(0x60) + "<>*?$" + chr(0x5C))
ENV_PREFIX_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")

# §14.1 banned git flags. Long-form exact + `=value` prefix to catch both
# `--output /tmp/x` and `--output=/tmp/x`; short-form regex to catch `-G`,
# `-Gpattern`, `-S`, `-o`, `-oFILE`, `-c`, `-cSECTION.KEY=val` without
# false-positives on long flags like `--oneline` or `--output`.
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


def emit(decision, reason):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.exit(0)


def deny(reason):
    emit("deny", "capability-guard: " + reason)


def allow(reason="allowed by Phase 0 capability guard"):
    emit("allow", "capability-guard: " + reason)


def check_banned_git_flags(tokens):
    for tok in tokens:
        if tok in BANNED_GIT_FLAGS_EXACT:
            deny("banned git flag: {0!r}".format(tok))
        for pref in BANNED_GIT_LONG_PREFIXES:
            if tok.startswith(pref):
                deny("banned git flag prefix: {0!r}".format(tok))
        if BANNED_GIT_SHORT_RE.match(tok):
            deny("banned git short flag: {0!r}".format(tok))


def check_bash_allowlist(t):
    # Global negative filter: any git invocation goes through banned-flag
    # enumeration before the positive allowlist. Belt-and-braces vs the
    # exact-match forms; keeps deny reasons specific.
    if t and t[0] == "git":
        check_banned_git_flags(t)

    # sudo -u donna-broker /usr/local/bin/donna-broker <mode> <json>
    if (
        len(t) == 6
        and t[0] == "sudo"
        and t[1] == "-u"
        and t[2] == "donna-broker"
        and t[3] == "/usr/local/bin/donna-broker"
    ):
        mode = t[4]
        if mode not in BROKER_MODES:
            deny("broker mode {0!r} not in §13.1 allowlist".format(mode))
        try:
            json.loads(t[5])
        except Exception as e:
            deny("broker payload is not valid JSON: {0}".format(e))
        allow("sudo donna-broker")

    # sudo -n /usr/local/bin/donna-broker-via-session <mode> <json>
    # Root trampoline that re-homes the broker into donna-broker's own
    # launchd session — required for browser executors since Chromium 149
    # SIGTRAPs in a borrowed GUI-session Mach namespace (2026-06-11). It
    # reaches exactly the same mode-validated CLI as the direct form, so
    # the same BROKER_MODES + JSON checks apply.
    if (
        len(t) == 5
        and t[0] == "sudo"
        and t[1] == "-n"
        and t[2] == "/usr/local/bin/donna-broker-via-session"
    ):
        mode = t[3]
        if mode not in BROKER_MODES:
            deny("broker mode {0!r} not in §13.1 allowlist".format(mode))
        try:
            json.loads(t[4])
        except Exception as e:
            deny("broker payload is not valid JSON: {0}".format(e))
        allow("sudo donna-broker-via-session")

    # ls <path>
    if len(t) == 2 and t[0] == "ls":
        path = t[1]
        if not DONNA_ROOT_RE.match(path):
            deny("ls path {0!r} outside /Users/grahamwilliamson/donna".format(path))
        if ".." in path:
            deny("ls path {0!r} contains '..' (directory-escape reject)".format(path))
        allow("ls under donna root")

    # git status
    if len(t) == 2 and t[0] == "git" and t[1] == "status":
        allow("git status")

    # git log --oneline -n <N>
    if (
        len(t) == 5
        and t[0] == "git"
        and t[1] == "log"
        and t[2] == "--oneline"
        and t[3] == "-n"
    ):
        n = t[4]
        if not re.fullmatch(r"[1-9][0-9]*", n):
            deny("git log -n value {0!r} not a positive integer".format(n))
        if int(n) > 200:
            deny("git log -n value {0} exceeds 200".format(n))
        allow("git log --oneline -n N")

    # git diff --stat
    if len(t) == 3 and t[0] == "git" and t[1] == "diff" and t[2] == "--stat":
        allow("git diff --stat")

    # git diff <path>  (single non-flag arg)
    if (
        len(t) == 3
        and t[0] == "git"
        and t[1] == "diff"
        and not t[2].startswith("-")
    ):
        allow("git diff <path>")

    deny("Bash argv {0!r} not in §14.1 allowlist".format(t))


def check_bash(command):
    if not isinstance(command, str):
        deny("Bash tool_input.command missing or not a string")

    for ch in command:
        if ch in SHELL_METACHARS:
            deny("Bash shell metacharacter rejected: {0!r}".format(ch))

    if ENV_PREFIX_RE.match(command.lstrip()):
        deny("Bash env-var prefix rejected (e.g. FOO=bar cmd)")

    try:
        tokens = shlex.split(command)
    except ValueError as e:
        deny("Bash tokenisation failed: {0}".format(e))

    if not tokens:
        deny("Bash command empty")

    check_bash_allowlist(tokens)


def check_mcp(tool_name):
    if tool_name.startswith("mcp__plugin_playwright_"):
        deny("Playwright blocked per §8.1 / §14.1 (tool={0})".format(tool_name))

    if tool_name in MCP_ALLOW_EXACT:
        allow("low-risk MCP read: " + tool_name)

    for pref in MCP_ALLOW_PREFIXES:
        if tool_name.startswith(pref):
            allow("low-risk MCP read prefix: " + tool_name)

    deny(
        "MCP tool {0!r} not on Phase 0 low-risk read allowlist "
        "(§8.1; Phase 1 switches to mcp-tools.yaml per §8)".format(tool_name)
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        deny("hook input not valid JSON: {0}".format(e))

    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}

    if tool_name == "Bash":
        check_bash(tool_input.get("command", ""))

    if tool_name.startswith("mcp__"):
        check_mcp(tool_name)

    # Phase 0 only gates Bash + MCP. Other built-in tools (Read, Edit, Write,
    # Grep, Glob, TodoWrite, Task, Skill, etc.) pass through untouched.
    allow("out of Phase 0 gate scope: " + tool_name)


if __name__ == "__main__":
    main()
