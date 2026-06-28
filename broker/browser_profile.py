# browser_profile.py
"""Declarative site profile for the browser-goal agent (design §5.1, §9).

A profile is DATA, not code: it replaces hundreds of lines of bespoke Playwright
with a handful of validated fields. Pure — no I/O beyond the dict it is given.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

_MFA_RULES = frozenset({"pause_and_ask"})
_STRICTNESS = frozenset({"monitor", "hard_abort"})
_INDICATOR_TYPES = frozenset({"url_pattern", "element", "cookie"})


class ProfileError(ValueError):
    """A site profile is malformed or unsafe. Structured; never silently ignored."""


@dataclass(frozen=True)
class SiteProfile:
    site: str
    login_url: str
    origin: str
    allowlist: tuple[str, ...]
    success_indicators: tuple[dict[str, object], ...]
    mfa_rule: str
    network_strictness: str


def load(raw: dict[str, object]) -> SiteProfile:
    """Validate a raw profile dict → a frozen SiteProfile. Raises ProfileError on
    anything malformed or unsafe (fail-closed)."""
    site = str(raw.get("site") or "").strip()
    if not site:
        raise ProfileError("site is required")

    login_url = str(raw.get("login_url") or "").strip()
    parts = urlsplit(login_url)
    if parts.scheme != "https" or not parts.netloc:
        raise ProfileError("login_url must be an absolute https URL")
    origin = f"{parts.scheme}://{parts.netloc}"

    allow = raw.get("allowlist") or []
    if not isinstance(allow, list) or not allow:
        raise ProfileError("allowlist must be a non-empty list of domains")
    allowlist = tuple(str(d).strip().lower() for d in allow if str(d).strip())
    if not allowlist:
        raise ProfileError("allowlist must contain at least one domain")

    # success_indicators are OPTIONAL: a recon-drafted profile may carry none,
    # in which case the live engine falls back to the general "navigated off the
    # login page onto an allowlisted page" proof (see executors/browser_goal
    # _logged_in). Explicit indicators, when present, are stricter overrides and
    # are still type-validated.
    inds = raw.get("success_indicators") or []
    if not isinstance(inds, list):
        raise ProfileError("success_indicators must be a list")
    for ind in inds:
        if not isinstance(ind, dict) or ind.get("type") not in _INDICATOR_TYPES:
            raise ProfileError(
                f"success_indicator type must be one of {sorted(_INDICATOR_TYPES)}"
            )

    mfa = str(raw.get("mfa_rule") or "pause_and_ask")
    if mfa not in _MFA_RULES:
        raise ProfileError(f"mfa_rule must be one of {sorted(_MFA_RULES)}")

    strictness = str(raw.get("network_strictness") or "monitor")
    if strictness not in _STRICTNESS:
        raise ProfileError(f"network_strictness must be one of {sorted(_STRICTNESS)}")

    return SiteProfile(
        site=site,
        login_url=login_url,
        origin=origin,
        allowlist=allowlist,
        success_indicators=tuple(dict(ind) for ind in inds),
        mfa_rule=mfa,
        network_strictness=strictness,
    )
