"""Filename-safe slug helpers.

Slugs appear in snapshot filenames. Restricted to ``[a-z0-9-]`` so nothing
a recording filename contains could ever surprise a shell or a filesystem.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_LEN = 60
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slugify(raw: str) -> str:
    lower = raw.lower()
    collapsed = _SLUG_STRIP.sub("-", lower).strip("-")
    if not collapsed:
        return "page"
    return collapsed[:MAX_LEN].rstrip("-") or "page"


def slug_for_url(url: str) -> str:
    """Derive a filename-safe slug from a URL's path, falling back to host."""
    if not url:
        return "page"
    try:
        parts = urlsplit(url)
    except ValueError:
        return _slugify(url)

    if parts.netloc:
        # Prefer the last non-empty path segment (e.g. /login/ → "login").
        segments = [s for s in parts.path.split("/") if s]
        if segments:
            return _slugify(segments[-1])
        # No path → host (e.g. https://example.com/ → "example-com").
        return _slugify(parts.netloc)

    # No netloc — about:blank, data:*, chrome:*, etc. Keep the scheme
    # so snapshots named for these pages are still distinguishable.
    if parts.scheme:
        return _slugify(f"{parts.scheme}-{parts.path}")

    return _slugify(url)


def slug_for_label(label: str) -> str:
    """Slug a user-provided marker label."""
    return _slugify(label)
