# browser_runner.py
"""The conductor (design §9). Runs the plan or execute phase by looping:
snapshot -> sanitise -> agent proposes ONE action -> gate validates -> execute (if
allowed) -> ledger. Pure orchestration: the browser and the agent are injected, so
this is fully testable without Playwright or a model. The gate is the security
authority; this module only sequences and records.
"""
from __future__ import annotations

from typing import Any, Protocol

from broker import browser_gate as gate
from broker import browser_ledger as bl
from broker import browser_sanitise as san


class BrowserLike(Protocol):
    def snapshot(self) -> dict[str, Any]: ...  # pragma: no cover - Protocol stub
    def execute(self, action: dict[str, Any]) -> None: ...  # pragma: no cover - Protocol stub


class AgentLike(Protocol):
    def next(self, sanitised: dict[str, Any]) -> dict[str, Any]: ...  # pragma: no cover - Protocol stub


def run(*, browser: BrowserLike, agent: AgentLike, the_gate: gate.Gate,
        ledger: bl.Ledger, phase: str, max_actions: int,
        commit_token: str | None = None) -> dict[str, Any]:
    """Drive the agent toward its goal for one phase. Returns a structured result.
    Never raises (fail-closed).

    In the plan phase, the gate refuses propose_commit (a plan-phase run is read-only
    and cannot mint tokens). The runner intercepts propose_commit in plan phase and
    returns the proposal directly so the caller can present it for human approval.
    In the execute phase, propose_commit goes through the gate normally and returns
    needs_approval, which the runner also surfaces as {"status": "planned", ...}.
    """
    step = 0
    while step < max_actions:
        step += 1
        snapshot = browser.snapshot()
        sanitised = san.sanitise(snapshot)
        action = agent.next(sanitised)
        if action.get("kind") == "click" and action.get("commit") and commit_token:
            action = {**action, "commit_token": commit_token}
        # In plan phase the gate refuses propose_commit (invariant: no commit without token).
        # Intercept it here to surface the proposal for human review without executing.
        if phase == "plan" and action.get("kind") == "propose_commit":
            proposal = {
                "summary": str(action.get("summary") or ""),
                "price": action.get("price"),
                "target_ref": str(action.get("ref") or ""),
                "expected_text": str(action.get("expected_text") or ""),
                "snapshot_hash": san.snapshot_hash(snapshot),
            }
            ledger.record(step=step, snapshot_hash=san.snapshot_hash(snapshot),
                          action=dict(action), gate_decision="planned", outcome="pending")
            return {"status": "planned", "proposal": proposal}
        decision = the_gate.check(action, snapshot)
        ledger.record(step=step, snapshot_hash=san.snapshot_hash(snapshot),
                      action=decision.log_action or action,
                      gate_decision=decision.decision, outcome="pending")
        if decision.decision == "refuse":
            # A refused action is fed back implicitly: the agent re-sees the same page
            # next loop and can correct or give up. give_up is always gate-allowed and
            # so is handled on the allow path below; this guard is defence-in-depth so
            # an explicit give_up still terminates even under a future gate policy that
            # refused it — give_up must always exit, never loop to the cap.
            if action.get("kind") == "give_up":  # pragma: no cover - defensive: gate always allows give_up
                return {"status": "gave_up", "reason": str(action.get("reason") or "")}
            continue
        if decision.decision == "needs_approval":
            return {"status": "planned", "proposal": decision.proposal}
        kind = action.get("kind")
        if kind == "done":
            return {"status": "done", "result": action.get("result")}
        if kind == "give_up":
            return {"status": "gave_up", "reason": str(action.get("reason") or "")}
        if kind in ("navigate", "click", "type"):
            browser.execute(decision.action)
    return {"status": "aborted", "reason": f"max_actions cap ({max_actions}) reached"}
