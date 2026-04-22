"""Tests for donna_recon.redact.

The redactor is the load-bearing security module for network captures. The
rule is simple: every plausible secret is redacted, everything else round-trips
intact. These tests pin that contract.
"""
from __future__ import annotations

import json

import pytest

from donna_recon.redact import (
    DROP_HEADERS,
    SECRET_KEY_RE,
    redact_headers,
    redact_request_body,
    redact_url,
)


class TestSecretKeyRe:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "Password",
            "PASSWORD",
            "pwd",
            "user_password",
            "passwd",
            "secret",
            "api_key",
            "apiKey",
            "api-key",
            "authToken",
            "csrf_token",
            "xsrf",
            "session_id",
            "state",
            "oauth_code",
            "bearer_token",
        ],
    )
    def test_matches(self, key: str) -> None:
        assert SECRET_KEY_RE.search(key) is not None, key

    @pytest.mark.parametrize(
        "key",
        [
            "username",
            "email",
            "custname",
            "first_name",
            "cart_id",
            "product",
            "quantity",
        ],
    )
    def test_misses(self, key: str) -> None:
        assert SECRET_KEY_RE.search(key) is None, key


class TestRedactUrl:
    def test_simple_token_param(self) -> None:
        out = redact_url("https://ex.com/?token=xyz&foo=bar")
        assert "token=%5BREDACTED%5D" in out or "token=[REDACTED]" in out
        assert "foo=bar" in out
        assert "xyz" not in out

    def test_multiple_secret_params(self) -> None:
        out = redact_url("https://ex.com/?csrf=a&session=b&keep=c")
        assert "a" not in out.split("keep=")[0] or "csrf=[REDACTED]" in out.replace("%5B", "[").replace("%5D", "]")
        assert "keep=c" in out
        # Strict: raw values gone
        assert "csrf=a" not in out
        assert "session=b" not in out

    def test_no_secret_params(self) -> None:
        url = "https://ex.com/path?foo=1&bar=2"
        assert redact_url(url) == url

    def test_preserves_path_and_fragment(self) -> None:
        out = redact_url("https://ex.com/a/b?token=x#frag")
        assert "/a/b" in out
        assert "#frag" in out

    def test_empty_query(self) -> None:
        assert redact_url("https://ex.com/") == "https://ex.com/"

    def test_blank_param_value_preserved_if_not_secret(self) -> None:
        out = redact_url("https://ex.com/?foo=&bar=2")
        assert "foo=" in out
        assert "bar=2" in out


class TestRedactHeaders:
    def test_drops_cookie(self) -> None:
        out = redact_headers({"cookie": "session=abc", "user-agent": "x"})
        assert "cookie" not in {k.lower() for k in out}
        assert out.get("user-agent") == "x"

    def test_drops_set_cookie_case_insensitive(self) -> None:
        out = redact_headers({"Set-Cookie": "a=b", "Accept": "*/*"})
        assert not any(k.lower() == "set-cookie" for k in out)

    def test_drops_authorization(self) -> None:
        out = redact_headers({"Authorization": "Bearer abc", "X-Keep": "yes"})
        assert "Authorization" not in out
        assert "authorization" not in out
        assert out.get("X-Keep") == "yes"

    def test_drops_proxy_authorization(self) -> None:
        out = redact_headers({"proxy-authorization": "Basic abc"})
        assert out == {}

    def test_drop_set_is_exactly_four(self) -> None:
        assert DROP_HEADERS == {
            "cookie",
            "set-cookie",
            "authorization",
            "proxy-authorization",
        }

    def test_non_secret_headers_round_trip(self) -> None:
        headers = {"User-Agent": "Mozilla", "Accept": "text/html", "Host": "ex.com"}
        assert redact_headers(headers) == headers


class TestRedactFormBody:
    def test_password_redacted_field_preserved(self) -> None:
        body = b"custname=alice&password=hunter2&email=a@b.c"
        out = redact_request_body("application/x-www-form-urlencoded", body)
        assert out["_shape"] == "form"
        value = out["value"]
        assert "custname=alice" in value
        assert "email=a%40b.c" in value or "email=a@b.c" in value
        assert "password=%5BREDACTED%5D" in value or "password=[REDACTED]" in value
        assert "hunter2" not in value

    def test_multiple_secret_fields(self) -> None:
        body = b"token=x&csrf=y&keep=z"
        out = redact_request_body("application/x-www-form-urlencoded", body)
        value = out["value"]
        assert "keep=z" in value
        assert "x" not in value.split("keep=")[0].replace("%5BREDACTED%5D", "").replace("[REDACTED]", "")
        assert "y" not in value.split("keep=")[0].replace("%5BREDACTED%5D", "").replace("[REDACTED]", "")

    def test_content_type_with_charset(self) -> None:
        body = b"password=abc&ok=1"
        out = redact_request_body("application/x-www-form-urlencoded; charset=utf-8", body)
        assert out["_shape"] == "form"
        assert "abc" not in out["value"]


class TestRedactJsonBody:
    def test_top_level_password(self) -> None:
        body = b'{"username":"alice","password":"hunter2"}'
        out = redact_request_body("application/json", body)
        assert out["_shape"] == "json"
        data = out["value"]
        assert data["username"] == "alice"
        assert data["password"] == "[REDACTED]"

    def test_nested_api_key(self) -> None:
        body = b'{"auth":{"api_key":"abc","keep":"me"},"other":1}'
        out = redact_request_body("application/json", body)
        data = out["value"]
        assert data["auth"]["api_key"] == "[REDACTED]"
        assert data["auth"]["keep"] == "me"
        assert data["other"] == 1

    def test_redaction_inside_list_of_dicts(self) -> None:
        body = b'{"items":[{"token":"a","name":"n1"},{"token":"b","name":"n2"}]}'
        out = redact_request_body("application/json", body)
        items = out["value"]["items"]
        assert items[0]["token"] == "[REDACTED]"
        assert items[1]["token"] == "[REDACTED]"
        assert items[0]["name"] == "n1"
        assert items[1]["name"] == "n2"

    def test_plus_json_suffix(self) -> None:
        body = b'{"secret":"abc"}'
        out = redact_request_body("application/vnd.api+json", body)
        assert out["_shape"] == "json"
        assert out["value"]["secret"] == "[REDACTED]"

    def test_malformed_json(self) -> None:
        body = b'{"not json'
        out = redact_request_body("application/json", body)
        assert out["_shape"] == "unparseable"
        assert out["_size"] == len(body)
        # Raw bytes must not leak
        assert "not json" not in json.dumps(out)

    def test_nonstring_scalar_secret_values_redacted(self) -> None:
        # Non-string SCALAR values (ints, bools, floats) under a secret
        # key are still redacted at the leaf.
        body = b'{"token":12345,"keep":"me"}'
        out = redact_request_body("application/json", body)
        data = out["value"]
        assert data["token"] == "[REDACTED]"
        assert data["keep"] == "me"

    def test_secret_key_with_dict_value_descended(self) -> None:
        # A dict under a secret-looking key is descended into — diagnostic
        # structure is preserved, leaves are evaluated by their own keys.
        body = b'{"session":{"expires":1,"csrf":"abc"}}'
        out = redact_request_body("application/json", body)
        data = out["value"]
        assert data["session"]["expires"] == 1
        assert data["session"]["csrf"] == "[REDACTED]"


class TestRedactOpaqueBody:
    def test_multipart_metadata_only(self) -> None:
        body = b"--bound\r\npassword=hunter2\r\n--bound--"
        out = redact_request_body(
            "multipart/form-data; boundary=bound", body
        )
        assert out["_shape"].startswith("multipart/form-data")
        assert out["_size"] == len(body)
        # No raw content anywhere in the summary.
        assert "hunter2" not in json.dumps(out)

    def test_unknown_ct(self) -> None:
        body = b"\x00\x01\x02opaque"
        out = redact_request_body("application/octet-stream", body)
        assert out["_shape"] == "application/octet-stream"
        assert out["_size"] == len(body)

    def test_empty_body(self) -> None:
        out = redact_request_body("application/json", b"")
        # Empty body — treat as unparseable (json.loads fails) or empty form.
        # Contract: no raw bytes, no crash.
        assert "_size" in out or "_shape" in out

    def test_no_content_type(self) -> None:
        body = b"something"
        out = redact_request_body("", body)
        assert out["_shape"] == ""
        assert out["_size"] == len(body)
