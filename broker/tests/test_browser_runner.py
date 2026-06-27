from __future__ import annotations

from broker import browser_runner as runner
from broker import browser_gate as gate
from broker import browser_profile as bp
from broker import browser_token as bt
from broker import browser_ledger as bl


def _profile() -> bp.SiteProfile:
    return bp.load({"site": "ea", "login_url": "https://account.everyoneactive.com/login",
                    "allowlist": ["everyoneactive.com", "account.everyoneactive.com"],
                    "success_indicators": [{"type": "url_pattern", "value": "**/home*"}],
                    "mfa_rule": "pause_and_ask", "network_strictness": "monitor"})


class FakeBrowser:
    def __init__(self, snapshots: list[dict]):
        self._snaps = snapshots
        self.executed: list[dict] = []
        self._i = 0
    def snapshot(self) -> dict:
        return self._snaps[min(self._i, len(self._snaps) - 1)]
    def execute(self, action: dict) -> None:
        self.executed.append(action)
        self._i += 1


class ScriptedAgent:
    def __init__(self, actions: list[dict]):
        self._actions = actions
        self._i = 0
    def next(self, sanitised: dict) -> dict:
        a = self._actions[self._i]
        self._i += 1
        return a


SNAP = {"url": "https://account.everyoneactive.com/x", "nodes": [
    {"ref": "r1", "role": "link", "name": "Bookings", "tag": "a", "editable": False},
    {"ref": "r2", "role": "button", "name": "Confirm booking", "tag": "button", "editable": False},
]}


def _ledger(tmp_path):
    return bl.Ledger(tmp_path / "run.jsonl", run_id="R1", now=lambda: 0.0)


def test_plan_phase_reads_and_returns_proposal(tmp_path):
    browser = FakeBrowser([SNAP, SNAP])
    agent = ScriptedAgent([
        {"kind": "read"},
        {"kind": "propose_commit", "summary": "Book 7pm court £8", "price": 8.0,
         "ref": "r2", "expected_text": "Confirm booking"},
    ])
    g = gate.Gate(profile=_profile(), tokens=bt.TokenStore(now=lambda: 0.0),
                  creds={}, phase="plan")
    out = runner.run(browser=browser, agent=agent, the_gate=g, ledger=_ledger(tmp_path),
                     phase="plan", max_actions=10)
    assert out["status"] == "planned"
    assert out["proposal"]["summary"] == "Book 7pm court £8"
    assert out["proposal"]["target_ref"] == "r2"
    assert browser.executed == []


def test_execute_phase_runs_until_done(tmp_path):
    browser = FakeBrowser([SNAP, SNAP, SNAP])
    agent = ScriptedAgent([
        {"kind": "navigate", "path": "/bookings"},
        {"kind": "click", "ref": "r2", "expected_text": "Confirm booking",
         "commit": True, "commit_token": "PLACEHOLDER"},
        {"kind": "done", "result": "booked"},
    ])
    tokens = bt.TokenStore(now=lambda: 0.0)
    g = gate.Gate(profile=_profile(), tokens=tokens, creds={}, phase="execute")
    from broker.browser_sanitise import snapshot_hash
    tok = tokens.mint(summary="Book 7pm court £8", snapshot_hash=snapshot_hash(SNAP),
                      target_ref="r2", expected_text="Confirm booking", approval_id="A1")
    out = runner.run(browser=browser, agent=agent, the_gate=g, ledger=_ledger(tmp_path),
                     phase="execute", max_actions=10, commit_token=tok)
    assert out["status"] == "done" and out["result"] == "booked"
    kinds = [a["kind"] for a in browser.executed]
    assert "navigate" in kinds and "click" in kinds


def test_refused_action_is_not_executed_and_is_fed_back(tmp_path):
    browser = FakeBrowser([SNAP, SNAP])
    agent = ScriptedAgent([
        {"kind": "navigate", "path": "https://evil.com"},
        {"kind": "give_up", "reason": "blocked"},
    ])
    g = gate.Gate(profile=_profile(), tokens=bt.TokenStore(now=lambda: 0.0), creds={}, phase="execute")
    out = runner.run(browser=browser, agent=agent, the_gate=g, ledger=_ledger(tmp_path),
                     phase="execute", max_actions=10)
    assert browser.executed == []
    assert out["status"] == "gave_up"


def test_execute_phase_propose_commit_surfaces_needs_approval(tmp_path):
    # In the execute phase, propose_commit is NOT intercepted by the runner's
    # plan-phase shortcut; it goes through the gate, which returns needs_approval.
    # The runner must surface that proposal as {"status": "planned", ...} so the
    # caller can obtain the human approval + commit token before re-submitting.
    browser = FakeBrowser([SNAP, SNAP])
    agent = ScriptedAgent([
        {"kind": "propose_commit", "summary": "Book 7pm court £8", "price": 8.0,
         "ref": "r2", "expected_text": "Confirm booking"},
    ])
    g = gate.Gate(profile=_profile(), tokens=bt.TokenStore(now=lambda: 0.0),
                  creds={}, phase="execute")
    out = runner.run(browser=browser, agent=agent, the_gate=g, ledger=_ledger(tmp_path),
                     phase="execute", max_actions=10)
    assert out["status"] == "planned"
    assert out["proposal"]["summary"] == "Book 7pm court £8"
    assert out["proposal"]["target_ref"] == "r2"
    assert out["proposal"]["expected_text"] == "Confirm booking"
    # the commit itself never fired: no state-changing action was dispatched
    assert browser.executed == []


def test_cap_exceeded_aborts(tmp_path):
    browser = FakeBrowser([SNAP])
    agent = ScriptedAgent([{"kind": "read"}] * 50)
    g = gate.Gate(profile=_profile(), tokens=bt.TokenStore(now=lambda: 0.0), creds={}, phase="plan")
    out = runner.run(browser=browser, agent=agent, the_gate=g, ledger=_ledger(tmp_path),
                     phase="plan", max_actions=3)
    assert out["status"] == "aborted" and "cap" in out["reason"]


def test_ledger_records_every_step(tmp_path):
    path = tmp_path / "run.jsonl"
    browser = FakeBrowser([SNAP, SNAP])
    agent = ScriptedAgent([{"kind": "read"}, {"kind": "done", "result": "ok"}])
    g = gate.Gate(profile=_profile(), tokens=bt.TokenStore(now=lambda: 0.0), creds={}, phase="plan")
    runner.run(browser=browser, agent=agent, the_gate=g,
               ledger=bl.Ledger(path, run_id="R1", now=lambda: 0.0),
               phase="plan", max_actions=10)
    lines = path.read_text().strip().splitlines()
    assert len(lines) >= 2
