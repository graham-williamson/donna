"""HTML snapshot sanitisation.

Applied before any snapshot is written. Runs coarse-grained redaction on
the known places secrets hide in a live DOM: password inputs, hidden
token fields, CSRF meta tags, inline script string literals, inline JSON
blobs, and secret-bearing ``data-*`` attributes.

Screenshots are NOT covered — see README for the corresponding warning.
"""
from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

from donna_recon.redact import (
    REDACTED,
    SECRET_KEY_RE,
    _redact_json_value,
)

SENSITIVE_AUTOCOMPLETE = frozenset(
    {"current-password", "new-password", "one-time-code"}
)

# `"key":"value"` and `'key':'value'` inside script text. Coarse by design:
# a small false-positive rate is preferable to leaking a token literal.
_JSON_ISH_DQ = re.compile(
    r'"(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)"\s*:\s*"(?P<val>(?:\\.|[^"\\])*)"'
)
_JSON_ISH_SQ = re.compile(
    r"'(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)'\s*:\s*'(?P<val>(?:\\.|[^'\\])*)'"
)
_BEARER_DQ = re.compile(r'"(Bearer\s+)([^"]+)"')
_BEARER_SQ = re.compile(r"'(Bearer\s+)([^']+)'")


def _is_secret_key(name: str) -> bool:
    return SECRET_KEY_RE.search(name) is not None


def _sanitise_password_input(inp: Tag) -> None:
    if inp.has_attr("value"):
        inp["value"] = ""
        inp["data-donna-redacted"] = "password"


def _sanitise_hidden_input(inp: Tag) -> None:
    name = str(inp.get("name") or "")
    id_ = str(inp.get("id") or "")
    if _is_secret_key(name) or _is_secret_key(id_):
        if inp.has_attr("value"):
            inp["value"] = ""
        inp["data-donna-redacted"] = "hidden-secret-name"


def _sanitise_autocomplete(inp: Tag) -> None:
    ac = str(inp.get("autocomplete") or "").lower()
    if ac in SENSITIVE_AUTOCOMPLETE:
        if inp.has_attr("value"):
            inp["value"] = ""
        inp["data-donna-redacted"] = "autocomplete-sensitive"


def _sanitise_meta(meta: Tag) -> None:
    name = str(meta.get("name") or "")
    if _is_secret_key(name) and meta.has_attr("content"):
        meta["content"] = ""


def _redact_script_text(text: str) -> str:
    def repl_dq(m: re.Match[str]) -> str:
        if _is_secret_key(m.group("key")):
            return f'"{m.group("key")}":"{REDACTED}"'
        return m.group(0)

    def repl_sq(m: re.Match[str]) -> str:
        if _is_secret_key(m.group("key")):
            return f"'{m.group('key')}':'{REDACTED}'"
        return m.group(0)

    text = _JSON_ISH_DQ.sub(repl_dq, text)
    text = _JSON_ISH_SQ.sub(repl_sq, text)
    text = _BEARER_DQ.sub(rf'"\1{REDACTED}"', text)
    text = _BEARER_SQ.sub(rf"'\1{REDACTED}'", text)
    return text


def _sanitise_script(script: Tag) -> None:
    script_type = str(script.get("type") or "").lower()
    text = script.string
    if text is None:
        return

    if script_type == "application/json" or script_type.endswith("+json"):
        try:
            parsed: Any = json.loads(str(text))
        except ValueError:
            return
        script.clear()
        script.append(json.dumps(_redact_json_value(parsed)))
        return

    # Any other script: coarse regex pass.
    new_text = _redact_script_text(str(text))
    if new_text != str(text):
        script.clear()
        script.append(new_text)


def _sanitise_data_attrs(tag: Tag) -> None:
    if not tag.attrs:
        return
    for attr_name in list(tag.attrs.keys()):
        if attr_name == "data-donna-redacted":
            continue
        if attr_name.startswith("data-"):
            suffix = attr_name[len("data-"):]
            if _is_secret_key(suffix):
                tag[attr_name] = REDACTED


def sanitise_html(html: str) -> str:
    """Return a sanitised copy of *html* with secret-bearing bits stripped.

    Pure function. Input and output are both ``str`` — the caller handles
    encoding.
    """
    soup = BeautifulSoup(html, "html.parser")

    for inp in soup.find_all("input"):
        type_ = str(inp.get("type") or "text").lower()
        if type_ == "password":
            _sanitise_password_input(inp)
        elif type_ == "hidden":
            _sanitise_hidden_input(inp)
        _sanitise_autocomplete(inp)

    for meta in soup.find_all("meta"):
        _sanitise_meta(meta)

    for script in soup.find_all("script"):
        _sanitise_script(script)

    for tag in soup.find_all(True):
        _sanitise_data_attrs(tag)

    return str(soup)
