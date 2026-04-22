"""Redaction for URL, header, and request-body data on the write path.

These functions run before anything is written to disk. Raw secret bytes must
never reach the filesystem, not even transiently. Everything here is pure —
no I/O, no globals with state — so tests can exercise them directly.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

# Matches any substring that looks like a secret-bearing identifier. Coarse
# by design — a false positive loses diagnostic value, a false negative
# leaks a token to disk.
SECRET_KEY_RE = re.compile(
    r"(?i)password|pwd|passwd|secret|token|api[_-]?key|auth|session|csrf|xsrf|state|code|bearer"
)

DROP_HEADERS: frozenset[str] = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization"}
)


def _is_secret_key(name: str) -> bool:
    return SECRET_KEY_RE.search(name) is not None


def redact_url(url: str) -> str:
    """Redact values of query params whose name looks secret. Name preserved."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    cleaned = [
        (name, REDACTED if _is_secret_key(name) else value) for name, value in pairs
    ]
    new_query = urlencode(cleaned, doseq=False)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop secret-bearing headers wholesale. Case-insensitive on the name."""
    return {k: v for k, v in headers.items() if k.lower() not in DROP_HEADERS}


def _redact_json_value(value: Any, key_is_secret: bool = False) -> Any:
    """Recursively redact scalar leaves whose key looks secret.

    Always descends into dicts and lists so a consuming session can see
    the request/response shape. A secret-looking key only forces redaction
    at the scalar leaf level — a dict or list under a secret-looking key
    is descended into and its own leaves are evaluated by their own keys.
    This preserves diagnostic structure while still scrubbing every
    individual value that might carry a secret.
    """
    if isinstance(value, dict):
        return {
            k: _redact_json_value(v, _is_secret_key(str(k)))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(v, key_is_secret) for v in value]
    if key_is_secret:
        return REDACTED
    return value


def _main_type(content_type: str) -> str:
    """Return the main type token from a Content-Type header (lowercased)."""
    return content_type.split(";", 1)[0].strip().lower()


def redact_request_body(content_type: str, body_bytes: bytes) -> dict[str, Any]:
    """Return a JSON-serialisable summary of a request body, redacted.

    Never includes raw bytes for opaque or malformed inputs — only a shape
    and size. For form and JSON inputs, scalar values whose keys look
    secret are replaced with ``[REDACTED]``; dict/list structure is
    always preserved (descended into) so a consuming session can still
    diff requests by shape. A dict under a secret-looking key is still
    descended into — its own leaves are evaluated by their own keys.
    """
    main = _main_type(content_type)

    if main == "application/x-www-form-urlencoded":
        try:
            pairs = parse_qsl(body_bytes.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return {"_shape": main, "_size": len(body_bytes)}
        cleaned = [
            (name, REDACTED if _is_secret_key(name) else value)
            for name, value in pairs
        ]
        return {"_shape": "form", "value": urlencode(cleaned, doseq=False)}

    if main == "application/json" or main.endswith("+json"):
        try:
            parsed = json.loads(body_bytes)
        except (ValueError, UnicodeDecodeError):
            return {"_shape": "unparseable", "_size": len(body_bytes)}
        return {"_shape": "json", "value": _redact_json_value(parsed)}

    return {"_shape": content_type, "_size": len(body_bytes)}
