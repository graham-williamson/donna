from __future__ import annotations

import pytest

from broker import browser_profile as bp


def _valid() -> dict:
    return {
        "site": "everyone_active",
        "login_url": "https://account.everyoneactive.com/login",
        "allowlist": ["everyoneactive.com", "account.everyoneactive.com"],
        "success_indicators": [{"type": "url_pattern", "value": "**/memberHomePage*"}],
        "mfa_rule": "pause_and_ask",
        "network_strictness": "monitor",
    }


def test_load_valid_profile():
    p = bp.load(_valid())
    assert p.site == "everyone_active"
    assert p.origin == "https://account.everyoneactive.com"
    assert "everyoneactive.com" in p.allowlist
    assert p.network_strictness == "monitor"


def test_login_url_must_be_https():
    bad = _valid() | {"login_url": "http://insecure.example.com/login"}
    with pytest.raises(bp.ProfileError, match="https"):
        bp.load(bad)


def test_allowlist_required_nonempty():
    bad = _valid() | {"allowlist": []}
    with pytest.raises(bp.ProfileError, match="allowlist"):
        bp.load(bad)


def test_unknown_strictness_rejected():
    bad = _valid() | {"network_strictness": "yolo"}
    with pytest.raises(bp.ProfileError, match="network_strictness"):
        bp.load(bad)


def test_unknown_mfa_rule_rejected():
    bad = _valid() | {"mfa_rule": "solve_it"}
    with pytest.raises(bp.ProfileError, match="mfa_rule"):
        bp.load(bad)


def test_success_indicators_required():
    bad = _valid() | {"success_indicators": []}
    with pytest.raises(bp.ProfileError, match="success_indicators"):
        bp.load(bad)


def test_ftp_scheme_rejected():
    bad = _valid() | {"login_url": "ftp://account.everyoneactive.com/login"}
    with pytest.raises(bp.ProfileError, match="https"):
        bp.load(bad)


def test_https_without_host_rejected():
    bad = _valid() | {"login_url": "https://"}
    with pytest.raises(bp.ProfileError, match="https"):
        bp.load(bad)


def test_indicator_with_bad_type_rejected():
    bad = _valid() | {"success_indicators": [{"type": "evil"}]}
    with pytest.raises(bp.ProfileError, match="success_indicator"):
        bp.load(bad)


def test_allowlist_is_lowercased():
    p = bp.load(_valid() | {"allowlist": ["EveryoneActive.COM"]})
    assert p.allowlist == ("everyoneactive.com",)


def test_missing_site_rejected():
    bad = _valid() | {"site": ""}
    with pytest.raises(bp.ProfileError, match="site is required"):
        bp.load(bad)


def test_allowlist_all_blank_entries_rejected():
    # allowlist is a non-empty list, but every entry is whitespace-only.
    # After stripping, the tuple is empty → line 50 raises.
    bad = _valid() | {"allowlist": ["  ", ""]}
    with pytest.raises(bp.ProfileError, match="allowlist"):
        bp.load(bad)
