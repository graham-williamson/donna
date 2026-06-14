# browser_nav.py
"""Navigation guard (design §5.6, §6, invariant 5). The agent never supplies raw
cross-origin URLs: it navigates by relative path (resolved here against the
approved origin) or by an on-page ref (resolved by the live layer in Plan 2).
Every resulting URL is checked against the allowlist before it may load. Pure.
"""
from __future__ import annotations

from urllib.parse import urlsplit


class NavError(ValueError):
    """A navigation target is malformed or off-allowlist. Fail-closed."""


def resolve_path(origin: str, path: str) -> str:
    """Resolve a RELATIVE path against the approved origin. Rejects anything that
    is actually absolute or protocol-relative (no smuggling a cross-origin URL)."""
    p = (path or "").strip()
    if not p.startswith("/") or p.startswith("//"):
        raise NavError(f"navigation path must be origin-relative (start with a single '/'): {p!r}")
    return origin.rstrip("/") + p


def is_allowed(url: str, allowlist: tuple[str, ...]) -> bool:
    """True iff url is https and its host is an allowlisted domain or a subdomain
    of one. Suffix tricks ('everyoneactive.com.evil.com') are rejected."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    for dom in allowlist:
        d = dom.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def check(url: str, allowlist: tuple[str, ...]) -> str:
    """Return url if allowed, else raise NavError (fail-closed)."""
    if not is_allowed(url, allowlist):
        raise NavError(f"navigation off-allowlist refused: {url!r}")
    return url
