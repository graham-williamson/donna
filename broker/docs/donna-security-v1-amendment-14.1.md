# Amendment to donna-security-v1.1 §14.1 — browser access via the gated browser-goal capability

**Date:** 2026-06-14
**Status:** Amendment (pending deploy)
**Refs:** spec `~/daru/docs/superpowers/specs/2026-06-14-secure-browser-goal-agent-design.md`;
plans `2026-06-14-browser-goal-security-core.md` (Plan 1), `2026-06-14-browser-goal-integration.md` (Plan 2).

## The amendment

§14.1 of donna-security-v1.1 states **Playwright is permanently blocked**. This amends
that as follows:

- The **raw Playwright MCP tools remain permanently blocked** (unchanged). No agent,
  routine, or chat path may call `mcp__plugin_playwright_playwright__*`. The manifest
  entries marking each as `blocked` stay.
- Browser automation is permitted **only** through the `browser_goal.*` broker
  capabilities (`browser_goal.plan`, `browser_goal.commit`), which run a constrained,
  broker-mediated browser. They are subject to the full broker approval flow and enforce
  the security invariants of the design spec §3:
  1. the agent never receives a credential (broker substitutes `{{cred:*}}`);
  2. page content is untrusted data, never instructions;
  3. the agent has no out-of-vocabulary power;
  4. **no mutation without an approved, single-use commit token**;
  5. off-allowlist network access is blocked at the network layer;
  6. fail-closed everywhere;
  7. every action is recorded to an append-only ledger.

In short: the blanket "no browser" becomes "no *ungoverned* browser." The only browser
that runs is the one inside the broker's trust boundary, behind the action gate.

`browser_goal.commit` is in `NO_STANDING_GRANTS` (policy.py): the committing/financial
step always requires a fresh per-use Telegram approval and is £-capped — it can never be
granted standing autonomy.

## Why this is safe despite re-enabling a browser

The security core (Plan 1, six pure modules, 100% covered, adversarially reviewed) enforces
the invariants in code that has no browser or model dependency. The live executor (Plan 2)
wires a real browser onto that already-verified core. Even a fully prompt-injected agent
holds no levers: it cannot leave the allowlist, read a secret, or commit without your nod.

## Deploy runbook (privileged — run by Graham, once per change)

These are the only steps that are NOT app+Telegram. Recurring use (request a goal → approve
in Telegram → run → approve payment in Telegram) needs none of this.

1. **Install the headless browser** (once) in the donna-broker venv:
   ```
   sudo -u donna-broker /Users/donna-broker/venv/bin/playwright install chromium-headless-shell
   ```
2. **Store the site credential** (once per site) — preferably via the app's Connect sheet
   (`store_credential` → age vault, no terminal). CLI fallback:
   ```
   /Users/grahamwilliamson/donna/ops/create-vault-entry.sh everyone_active
   ```
3. **Deploy the manifest** (after any capability/profile change):
   ```
   sudo /Users/grahamwilliamson/donna/ops/deploy-manifests.sh     # mirrors source→live + runs verify-manifests
   ```
4. **Smoke test** (mobile, after deploy):
   - App: request a `browser_goal.plan` for a known goal → inspect the returned plan + the
     run ledger.
   - App: request `browser_goal.commit` with a small `max_price` → approve in Telegram →
     confirm the action + the £-cap held.
5. **Roll back:** revert the `browser_goal.*` blocks in `manifests/capabilities.yaml` and
   re-run `deploy-manifests.sh`.

## What's still required for a fully mobile-only enable

This runbook is the interim (terminal) path. Plan 3 (the signed-capability promoter)
replaces steps 1–3 with: app proposes → Telegram approve → the promoter installs a
**signed** capability over a local socket — no terminal, with signing preventing the app
from self-granting arbitrary powers. The promoter itself is installed by **one** SSH
bootstrap (it cannot install itself); after that, enabling new site integrations is
app + Telegram over Tailscale.
