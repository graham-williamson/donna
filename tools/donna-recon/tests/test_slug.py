"""Tests for donna_recon.slug."""
from __future__ import annotations

import pytest

from donna_recon.slug import slug_for_label, slug_for_url


class TestSlugForUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://example.com/", "example-com"),
            ("https://example.com/login", "login"),
            ("https://example.com/login/", "login"),
            ("https://example.com/a/b/c", "c"),
            ("https://example.com/a/b/c?d=1", "c"),
            ("https://example.com/?foo=bar", "example-com"),
            ("about:blank", "about-blank"),
        ],
    )
    def test_basic(self, url: str, expected: str) -> None:
        assert slug_for_url(url) == expected

    def test_strips_bad_chars(self) -> None:
        out = slug_for_url("https://example.com/a b/c#$%d")
        # Must be filename-safe — no spaces, no shell specials.
        assert " " not in out
        assert "#" not in out
        assert "$" not in out
        assert "%" not in out
        # Some meaningful content preserved.
        assert out  # non-empty

    def test_caps_length(self) -> None:
        out = slug_for_url("https://example.com/" + "x" * 500)
        assert len(out) <= 60

    def test_empty_becomes_page(self) -> None:
        assert slug_for_url("") == "page"
        assert slug_for_url("://") == "page"


class TestSlugForLabel:
    def test_preserves_meaning(self) -> None:
        assert slug_for_label("Bookable class row") == "bookable-class-row"

    def test_strips_specials(self) -> None:
        out = slug_for_label("checkout / step 2!")
        assert " " not in out
        assert "/" not in out
        assert "!" not in out
        assert "checkout" in out
        assert "step" in out

    def test_empty_becomes_page(self) -> None:
        assert slug_for_label("") == "page"
        assert slug_for_label("   ") == "page"

    def test_caps_length(self) -> None:
        out = slug_for_label("x" * 500)
        assert len(out) <= 60

    def test_collapses_whitespace(self) -> None:
        assert slug_for_label("foo   bar") == "foo-bar"

    def test_unicode_falls_back_gracefully(self) -> None:
        # Non-ASCII should be dropped or transliterated; output still a valid
        # slug and non-empty.
        out = slug_for_label("café")
        assert out  # non-empty
        assert all(c.isalnum() or c == "-" for c in out)
