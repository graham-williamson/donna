from __future__ import annotations

import pytest

from broker import browser_nav as nav


ALLOW = ("everyoneactive.com", "account.everyoneactive.com")
ORIGIN = "https://account.everyoneactive.com"


def test_relative_path_resolved_against_origin():
    assert nav.resolve_path(ORIGIN, "/bookings") == "https://account.everyoneactive.com/bookings"


def test_allowlist_allows_exact_and_subdomain():
    assert nav.is_allowed("https://account.everyoneactive.com/x", ALLOW)
    assert nav.is_allowed("https://www.everyoneactive.com/y", ALLOW)


def test_offdomain_refused():
    assert not nav.is_allowed("https://evil.com/", ALLOW)
    assert not nav.is_allowed("https://everyoneactive.com.evil.com/", ALLOW)


def test_non_https_refused():
    assert not nav.is_allowed("http://account.everyoneactive.com/", ALLOW)
    assert not nav.is_allowed("file:///etc/passwd", ALLOW)


def test_resolve_path_rejects_absolute_url():
    with pytest.raises(nav.NavError):
        nav.resolve_path(ORIGIN, "https://evil.com/x")
    with pytest.raises(nav.NavError):
        nav.resolve_path(ORIGIN, "//evil.com/x")


def test_check_allows_or_raises():
    assert nav.check("https://www.everyoneactive.com/ok", ALLOW) == "https://www.everyoneactive.com/ok"
    with pytest.raises(nav.NavError):
        nav.check("https://evil.com", ALLOW)
