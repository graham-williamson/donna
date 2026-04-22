"""Tests for donna_recon.dom_redact.

HTML snapshots are the other secret-bearing write surface. This module
pins every sanitiser rule and asserts a golden-DOM fixture comes out
with zero plaintext secrets.
"""
from __future__ import annotations

import json

import pytest

from donna_recon.dom_redact import sanitise_html

SECRET_TOKENS = [
    "hunter2",
    "csrf-token-abc",
    "otp-123456",
    "meta-csrf-xyz",
    "inline-json-token-v",
    "bearer-abc123",
    "application-json-session-value",
    "data-api-key-value",
]


class TestPasswordInputs:
    def test_value_stripped(self) -> None:
        html = '<html><body><input type="password" value="hunter2"></body></html>'
        out = sanitise_html(html)
        assert "hunter2" not in out
        assert 'data-donna-redacted="password"' in out

    def test_input_without_value_untouched(self) -> None:
        html = '<html><body><input type="password"></body></html>'
        out = sanitise_html(html)
        # No breadcrumb added because there was nothing to strip.
        assert 'data-donna-redacted="password"' not in out


class TestHiddenSecretInputs:
    def test_name_matches(self) -> None:
        html = '<input type="hidden" name="csrf_token" value="csrf-token-abc">'
        out = sanitise_html(html)
        assert "csrf-token-abc" not in out
        assert "csrf_token" in out  # name is preserved for diagnostic value
        assert 'data-donna-redacted="hidden-secret-name"' in out

    def test_id_matches(self) -> None:
        html = '<input type="hidden" id="xsrf_field" value="abc-xyz">'
        out = sanitise_html(html)
        assert "abc-xyz" not in out
        assert 'data-donna-redacted="hidden-secret-name"' in out

    def test_non_secret_hidden_preserved(self) -> None:
        html = '<input type="hidden" name="product_id" value="sku-42">'
        out = sanitise_html(html)
        assert "sku-42" in out
        assert "data-donna-redacted" not in out


class TestAutocompleteHints:
    @pytest.mark.parametrize(
        "hint", ["current-password", "new-password", "one-time-code"]
    )
    def test_stripped(self, hint: str) -> None:
        html = f'<input autocomplete="{hint}" value="otp-123456">'
        out = sanitise_html(html)
        assert "otp-123456" not in out
        assert 'data-donna-redacted=' in out


class TestMetaTokens:
    def test_csrf_meta(self) -> None:
        html = '<meta name="csrf-token" content="meta-csrf-xyz">'
        out = sanitise_html(html)
        assert "meta-csrf-xyz" not in out
        # The name is preserved so recon output is still diagnostic.
        assert "csrf-token" in out

    def test_unrelated_meta_preserved(self) -> None:
        html = '<meta name="viewport" content="width=device-width">'
        out = sanitise_html(html)
        assert "width=device-width" in out


class TestInlineScriptStringLiterals:
    def test_json_ish_double_quoted(self) -> None:
        html = '<script>var cfg = {"token":"inline-json-token-v","page":"home"};</script>'
        out = sanitise_html(html)
        assert "inline-json-token-v" not in out
        assert '"page":"home"' in out

    def test_bearer_literal(self) -> None:
        html = '<script>const h = "Bearer bearer-abc123";</script>'
        out = sanitise_html(html)
        assert "bearer-abc123" not in out

    def test_single_quoted_token(self) -> None:
        html = "<script>var t = {'token':'some-secret-v'};</script>"
        out = sanitise_html(html)
        assert "some-secret-v" not in out


class TestInlineJsonScript:
    def test_application_json_redacted(self) -> None:
        # Using a non-secret top-level key so the inner secret is reachable
        # through structure — `_redact_json_value` replaces whole subtrees under
        # a secret key, matching request-body semantics.
        payload = {"config": {"session": "application-json-session-value"}, "keep": "ok"}
        html = (
            '<script type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        out = sanitise_html(html)
        assert "application-json-session-value" not in out
        assert '"keep"' in out
        start = out.index('<script type="application/json">') + len(
            '<script type="application/json">'
        )
        end = out.index("</script>", start)
        parsed = json.loads(out[start:end])
        assert parsed["keep"] == "ok"
        assert parsed["config"]["session"] == "[REDACTED]"

    def test_top_level_secret_key_subtree_descended(self) -> None:
        # Request-body parity: dict/list structure is always preserved
        # (descended into); only scalar leaves whose key looks secret
        # are redacted. `auth` holding a dict is kept intact as a dict;
        # its inner `session` scalar is redacted.
        payload = {"auth": {"session": "x"}, "keep": "ok"}
        html = (
            '<script type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        out = sanitise_html(html)
        start = out.index('<script type="application/json">') + len(
            '<script type="application/json">'
        )
        end = out.index("</script>", start)
        parsed = json.loads(out[start:end])
        assert parsed["auth"]["session"] == "[REDACTED]"
        assert parsed["keep"] == "ok"


class TestDataAttributes:
    def test_api_key_attr(self) -> None:
        html = '<div data-api-key="data-api-key-value" data-product-id="sku">x</div>'
        out = sanitise_html(html)
        assert "data-api-key-value" not in out
        assert "sku" in out  # non-secret data-* untouched
        assert "[REDACTED]" in out

    def test_session_attr(self) -> None:
        html = '<span data-session="abc">x</span>'
        out = sanitise_html(html)
        assert 'data-session="[REDACTED]"' in out or "data-session='[REDACTED]'" in out


class TestGoldenDom:
    """One fixture that bundles every rule. The output must contain zero
    plaintext secrets from SECRET_TOKENS."""

    def test_no_secrets_leak(self) -> None:
        html = f"""
        <html>
          <head>
            <meta name="csrf-token" content="meta-csrf-xyz">
            <meta name="viewport" content="width=device-width">
            <script>var cfg = {{"token":"inline-json-token-v","page":"home"}};</script>
            <script>const h = "Bearer bearer-abc123";</script>
            <script type="application/json">{{"auth":{{"session":"application-json-session-value"}}}}</script>
          </head>
          <body>
            <form>
              <input name="username" value="alice">
              <input type="password" value="hunter2">
              <input type="hidden" name="csrf_token" value="csrf-token-abc">
              <input autocomplete="one-time-code" value="otp-123456">
            </form>
            <div data-api-key="data-api-key-value" data-product-id="sku">visible text</div>
          </body>
        </html>
        """
        out = sanitise_html(html)
        for tok in SECRET_TOKENS:
            assert tok not in out, f"secret leaked: {tok}"
        # Non-secret round-trip guards.
        assert "alice" in out
        assert "visible text" in out
        assert "sku" in out
        assert "width=device-width" in out


class TestRoundTrip:
    def test_unchanged_html_preserved(self) -> None:
        html = "<html><body><h1>Hello</h1><p>No secrets here.</p></body></html>"
        out = sanitise_html(html)
        assert "Hello" in out
        assert "No secrets here." in out

    def test_non_utf8_bytes_not_input(self) -> None:
        # sanitise_html takes str, not bytes, so encoding is the caller's problem.
        # Passing malformed-but-decodable HTML should not crash.
        out = sanitise_html("<html><body><div>&amp;</div></body></html>")
        assert "amp" in out or "&" in out
