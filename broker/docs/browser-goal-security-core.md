# Browser-Goal Security Core

Pure, browser-free enforcement layer for the secure browser-goal agent
(spec: ~/daru/docs/superpowers/specs/2026-06-14-secure-browser-goal-agent-design.md).

## Modules
- browser_profile — declarative SiteProfile (login_url, allowlist, success_indicators, mfa_rule, network_strictness).
- browser_nav — resolve relative paths against the approved origin; allowlist check (subdomain-aware, suffix-trick-proof).
- browser_token — one-time, target-bound, adjacent-only commit tokens.
- browser_ledger — append-only, secret-scrubbed audit ledger.
- browser_sanitise — raw DOM snapshot -> untrusted-tagged agent tree + stable snapshot hash.
- browser_gate — validates ONE action: vocabulary, allowlist, credential substitution, expected_text, commit-token.

## Orchestration contract (for Plan 2)
1. snapshot = browser.dom_snapshot(); s = browser_sanitise.sanitise(snapshot)
2. action = agent.next(s)   # agent sees the UNTRUSTED tree only
3. d = gate.check(action, snapshot)
4. d.decision == "allow"          -> browser executes d.action; ledger.record(action=d.log_action, gate_decision="allow")
   d.decision == "refuse"         -> ledger.record(gate_decision="refuse", ...); return refusal to the agent
   d.decision == "needs_approval" -> surface d.proposal to Graham; on approval:
        tok = tokens.mint(**proposal-fields, approval_id=...); re-submit the click with commit=True, commit_token=tok
5. NEVER log d.action (it holds substituted secrets); always log d.log_action.

## Invariants (must hold; see spec §3)
1 agent never receives a secret · 2 page content untrusted · 3 agent has no out-of-vocab power ·
4 no mutation without an approved one-time token · 5 off-allowlist blocked · 6 fail-closed · 7 everything ledgered.

## Plan 2 (not built here)
browser_session (live Playwright), the browser_goal agent runner (claude -p loop), the manifest capability,
the network backstop (off-allowlist hard-block + non-idempotent-without-token anomaly), and the §14.1 amendment.
A committing click MUST be declared (commit=true) to require a token; the Plan-2 network backstop is what
catches an *undeclared* mutation. Until Plan 2, the gate relies on the agent declaring commits.
