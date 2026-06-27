from __future__ import annotations

from broker import browser_gate as gate
from broker import browser_profile as bp
from broker import browser_token as bt
from broker.browser_sanitise import snapshot_hash


def _profile() -> bp.SiteProfile:
    return bp.load({
        "site": "ea", "login_url": "https://account.everyoneactive.com/login",
        "allowlist": ["everyoneactive.com", "account.everyoneactive.com"],
        "success_indicators": [{"type": "url_pattern", "value": "**/home*"}],
        "mfa_rule": "pause_and_ask", "network_strictness": "monitor",
    })


SNAP = {"url": "https://account.everyoneactive.com/x", "nodes": [
    {"ref": "r1", "role": "textbox", "name": "Password", "tag": "input", "editable": True},
    {"ref": "r2", "role": "button", "name": "Confirm booking", "tag": "button", "editable": False},
    {"ref": "r3", "role": "link", "name": "Help", "tag": "a", "editable": False},
    {"ref": "r4", "role": "input", "name": "", "tag": "input", "editable": True},
]}


def _gate(phase: str = "execute") -> gate.Gate:
    return gate.Gate(profile=_profile(), tokens=bt.TokenStore(now=lambda: 0.0),
                     creds={"password": "hunter2", "username": "g@x.com"}, phase=phase)


def test_read_is_allowed():
    assert _gate().check({"kind": "read"}, SNAP).decision == "allow"


def test_navigate_relative_allowed_absolute_refused():
    g = _gate()
    assert g.check({"kind": "navigate", "path": "/bookings"}, SNAP).decision == "allow"
    bad = g.check({"kind": "navigate", "path": "https://evil.com"}, SNAP)
    assert bad.decision == "refuse" and "relative" in bad.reason


def test_type_substitutes_credential_without_leaking():
    g = _gate()
    d = g.check({"kind": "type", "ref": "r1", "expected_label": "Password",
                 "text": "{{cred:password}}"}, SNAP)
    assert d.decision == "allow"
    assert d.action["text"] == "hunter2"
    assert d.log_action["text"] == "{{cred:password}}"


def test_type_wrong_label_refused():
    g = _gate()
    d = g.check({"kind": "type", "ref": "r1", "expected_label": "Username",
                 "text": "x"}, SNAP)
    assert d.decision == "refuse"


def test_type_into_noneditable_refused():
    g = _gate()
    d = g.check({"kind": "type", "ref": "r2", "expected_label": "Confirm booking",
                 "text": "x"}, SNAP)
    assert d.decision == "refuse"


def test_type_label_match_is_case_insensitive():
    # r1 live label is "Password"; the agent supplies "password".
    g = _gate(phase="execute")
    d = g.check({"kind": "type", "ref": "r1", "expected_label": "password",
                 "text": "x"}, SNAP)
    assert d.decision == "allow"


def test_type_empty_live_label_is_accepted():
    # r4 is an editable input the site exposes with no accessible label. The
    # ref-exists + editable gate still applies; the (unmatchable) label does not.
    g = _gate(phase="execute")
    d = g.check({"kind": "type", "ref": "r4", "expected_label": "Email",
                 "text": "{{cred:username}}"}, SNAP)
    assert d.decision == "allow"


def test_click_text_match_is_case_insensitive():
    g = _gate(phase="execute")
    d = g.check({"kind": "click", "ref": "r2", "expected_text": "CONFIRM BOOKING"}, SNAP)
    assert d.decision == "allow"


def test_click_expected_text_mismatch_refused():
    g = _gate()
    d = g.check({"kind": "click", "ref": "r2", "expected_text": "Buy now"}, SNAP)
    assert d.decision == "refuse" and "expected_text" in d.reason


def test_click_unknown_ref_refused():
    g = _gate()
    d = g.check({"kind": "click", "ref": "rX", "expected_text": "Confirm booking"}, SNAP)
    assert d.decision == "refuse"


def test_commit_in_plan_phase_refused():
    g = _gate(phase="plan")
    d = g.check({"kind": "propose_commit", "summary": "book £8"}, SNAP)
    assert d.decision == "refuse" and "plan" in d.reason


def test_propose_commit_needs_approval():
    g = _gate()
    d = g.check({"kind": "propose_commit", "summary": "Book 7pm court £8",
                 "ref": "r2", "expected_text": "Confirm booking"}, SNAP)
    assert d.decision == "needs_approval"
    assert d.proposal["snapshot_hash"] == snapshot_hash(SNAP)


def test_committing_click_requires_token():
    g = _gate()
    d = g.check({"kind": "click", "ref": "r2", "expected_text": "Confirm booking",
                 "commit": True}, SNAP)
    assert d.decision == "refuse" and "token" in d.reason


def test_committing_click_with_valid_token_allowed():
    g = _gate()
    tok = g.tokens.mint(summary="Book 7pm court £8", snapshot_hash=snapshot_hash(SNAP),
                        target_ref="r2", expected_text="Confirm booking", approval_id="A1")
    d = g.check({"kind": "click", "ref": "r2", "expected_text": "Confirm booking",
                 "commit": True, "commit_token": tok}, SNAP)
    assert d.decision == "allow"


def test_committing_click_with_bad_token_refused():
    g = _gate()
    d = g.check({"kind": "click", "ref": "r2", "expected_text": "Confirm booking",
                 "commit": True, "commit_token": "garbage"}, SNAP)
    assert d.decision == "refuse" and "token" in d.reason


def test_unknown_action_kind_refused():
    d = _gate().check({"kind": "evaluate_js", "code": "fetch('/x')"}, SNAP)
    assert d.decision == "refuse"


def test_noncommitting_click_allowed():
    g = _gate()
    d = g.check({"kind": "click", "ref": "r3", "expected_text": "Help"}, SNAP)
    assert d.decision == "allow"
    assert d.action == {"kind": "click", "ref": "r3"}


def test_propose_then_mint_then_commit_roundtrip():
    """The proposal dict's fields feed mint() by name (no ** needed): summary,
    snapshot_hash, target_ref, expected_text — proving the propose->approve->commit
    contract actually wires up (price is display-only, not passed to mint)."""
    g = _gate()
    d = g.check({"kind": "propose_commit", "summary": "Book 7pm court £8", "price": 8.0,
                 "ref": "r2", "expected_text": "Confirm booking"}, SNAP)
    assert d.decision == "needs_approval"
    p = d.proposal
    tok = g.tokens.mint(summary=p["summary"], snapshot_hash=p["snapshot_hash"],
                        target_ref=p["target_ref"], expected_text=p["expected_text"],
                        approval_id="A1")
    out = g.check({"kind": "click", "ref": "r2", "expected_text": "Confirm booking",
                   "commit": True, "commit_token": tok}, SNAP)
    assert out.decision == "allow"
