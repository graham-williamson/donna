"""Tests for hooks/capability-guard-phase1.py.

Focus: the Bash-path handling of legitimate `sudo -u donna-broker`
invocations whose JSON payload contains characters that would
otherwise trip the shell-metacharacter scan (e.g., `<` and `>` in
Notion writes that include HTML/XML-like markup).

Safety goal: the fix must NOT regress detection of shell-injection
patterns that produce a valid-looking 6-token shlex output but whose
bash interpretation differs from shlex (the canonical example being
`'{"a":"'$(whoami)'"}'` — concatenation of two single-quoted runs
with an unquoted `$(...)` between them).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


HOOK_PATH = Path(__file__).parent / "capability-guard-phase1.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "capability_guard_phase1", HOOK_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hook():
    return _load_hook()


def _decision(hook, command: str, capsys) -> dict:
    """Run check_bash and return the emitted hook envelope as a dict."""
    with pytest.raises(SystemExit) as exc:
        hook.check_bash(command)
    assert exc.value.code == 0
    captured = capsys.readouterr().out
    return json.loads(captured)["hookSpecificOutput"]


def _allowed(hook, command, capsys) -> None:
    dec = _decision(hook, command, capsys)
    assert dec["permissionDecision"] == "allow", dec


def _denied(hook, command, capsys) -> None:
    dec = _decision(hook, command, capsys)
    assert dec["permissionDecision"] == "deny", dec


# ---- Positive: legitimate broker invocations ---------------------------


class TestLegitimateBrokerCalls:
    def test_simple_request(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{\"capability\":\"test.op\",\"params\":{}}'"
        )
        _allowed(hook, cmd, capsys)

    def test_execute_with_approval_code(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "execute '{\"approval_code\":\"ABC123\"}'"
        )
        _allowed(hook, cmd, capsys)

    def test_html_tags_in_json_payload(self, hook, capsys):
        """Original false positive: Notion writes containing <tr> / <td>
        trip the metachar scan before the broker pattern is recognised.
        After the fix, these must be allowed — bash treats `<` inside a
        single-quoted run as literal, so there's no redirect."""
        payload = json.dumps({
            "capability": "notion.update_page",
            "params": {
                "children": [
                    {"paragraph": {"rich_text": [
                        {"text": {"content": "<tr>row</tr>"}}
                    ]}}
                ]
            },
        })
        cmd = (
            f"sudo -u donna-broker /usr/local/bin/donna-broker "
            f"request '{payload}'"
        )
        _allowed(hook, cmd, capsys)

    def test_dollar_sign_literal_in_json_string(self, hook, capsys):
        """`$` inside a single-quoted bash token is literal. A Notion
        page saying "price: $10" should not be rejected."""
        payload = json.dumps({
            "capability": "notion.update_page",
            "params": {"text": "price: $10"},
        })
        cmd = (
            f"sudo -u donna-broker /usr/local/bin/donna-broker "
            f"request '{payload}'"
        )
        _allowed(hook, cmd, capsys)

    def test_angle_brackets_in_gmail_body(self, hook, capsys):
        """Gmail drafts containing HTML — same class of regression."""
        payload = json.dumps({
            "capability": "gmail.create_draft",
            "params": {"body": "<p>Hi &mdash; cheers.</p>"},
        })
        cmd = (
            f"sudo -u donna-broker /usr/local/bin/donna-broker "
            f"request '{payload}'"
        )
        _allowed(hook, cmd, capsys)

    def test_list_pending_empty_params(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "list-pending '{}'"
        )
        _allowed(hook, cmd, capsys)


# ---- Negative: shell-injection attempts --------------------------------


class TestInjectionRejected:
    def test_command_substitution_between_quoted_runs(self, hook, capsys):
        """The classic bypass the structural check must survive.
        shlex sees 6 tokens and the concatenated 6th token parses as
        valid JSON, but bash evaluates $(whoami) because it sits in an
        unquoted region between two single-quoted runs."""
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{\"a\":\"'$(whoami)'\"}'"
        )
        _denied(hook, cmd, capsys)

    def test_trailing_semicolon_command(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{}'; rm -rf /"
        )
        _denied(hook, cmd, capsys)

    def test_backtick_substitution_after_quoted_payload(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{}'`id`"
        )
        _denied(hook, cmd, capsys)

    def test_redirect_after_quoted_payload(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{}' < /etc/passwd"
        )
        _denied(hook, cmd, capsys)

    def test_logical_and_after_quoted_payload(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{}' && rm -rf /"
        )
        _denied(hook, cmd, capsys)

    def test_unknown_broker_mode(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "evil-mode '{}'"
        )
        _denied(hook, cmd, capsys)

    def test_invalid_json_payload(self, hook, capsys):
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request 'not-json'"
        )
        _denied(hook, cmd, capsys)

    def test_extra_sudo_flag(self, hook, capsys):
        """`--preserve-env` between `sudo` and `-u` shifts tokens and
        must not match the allowed shape."""
        cmd = (
            "sudo --preserve-env -u donna-broker "
            "/usr/local/bin/donna-broker request '{}'"
        )
        _denied(hook, cmd, capsys)

    def test_wrong_broker_binary(self, hook, capsys):
        """Any binary path other than the canonical one must fail."""
        cmd = (
            "sudo -u donna-broker /tmp/evil-broker "
            "request '{}'"
        )
        _denied(hook, cmd, capsys)

    def test_wrong_target_user(self, hook, capsys):
        cmd = (
            "sudo -u root /usr/local/bin/donna-broker "
            "request '{}'"
        )
        _denied(hook, cmd, capsys)

    def test_no_quotes_around_payload(self, hook, capsys):
        """Unquoted payload could be shell-interpreted — reject."""
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request {}"
        )
        _denied(hook, cmd, capsys)

    def test_double_quotes_around_payload(self, hook, capsys):
        """Double-quoted payload allows bash parameter/command
        expansion inside the payload. Reject — only single-quoted
        payloads are structurally safe."""
        cmd = (
            "sudo -u donna-broker /usr/local/bin/donna-broker "
            "request \"{}\""
        )
        _denied(hook, cmd, capsys)

    def test_leading_whitespace(self, hook, capsys):
        """Leading whitespace on a broker invocation must not bypass
        the strict shape match. Denied (may fall through to the
        metachar/allowlist path, which also rejects)."""
        cmd = (
            "   sudo -u donna-broker /usr/local/bin/donna-broker "
            "request '{}'"
        )
        _denied(hook, cmd, capsys)


# ---- Non-broker Bash: current allowlist still works --------------------


class TestNonBrokerAllowlistIntact:
    def test_ls_under_donna_root(self, hook, capsys):
        _allowed(hook, "ls /Users/grahamwilliamson/donna/broker", capsys)

    def test_ls_outside_donna_root_denied(self, hook, capsys):
        _denied(hook, "ls /tmp", capsys)

    def test_git_status(self, hook, capsys):
        _allowed(hook, "git status", capsys)

    def test_git_config_blocked(self, hook, capsys):
        _denied(hook, "git config user.name evil", capsys)

    def test_rm_not_in_allowlist(self, hook, capsys):
        _denied(hook, "rm foo.txt", capsys)

    def test_pipe_metachar_in_non_broker(self, hook, capsys):
        _denied(hook, "ls | grep foo", capsys)
