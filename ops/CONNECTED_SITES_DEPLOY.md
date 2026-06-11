# Connected Sites — privileged install (run by Graham)

Deploys the broker half of Connected Sites + secure checkout:
`store-credential` / `site-check` modes and the £50-capped
`everyone_active.checkout` capability + executor.

Contract: `~/daru/docs/connected-sites-broker-handoff.md`.
Everything below is already built and tested in this repo
(`broker/tests/test_sites_modes.py`, 526-test suite green); these steps
just promote it to the live broker. ~2 minutes, all idempotent.

## 0. Preflight (no sudo)

```bash
cd /Users/grahamwilliamson/donna/broker && source .venv/bin/activate && pytest -q && deactivate
```

## 1. age-keygen onto the wrapper's PATH (one-time)

The wrapper sanitises PATH to `/usr/bin:/bin:/usr/local/bin`. `age` is
already symlinked there (29 Apr); `store-credential` also needs
`age-keygen` to derive the vault recipient:

```bash
sudo ln -sf /opt/homebrew/bin/age-keygen /usr/local/bin/age-keygen
```

## 2. Deploy broker code to the service account

```bash
sudo install -m 0644 -o donna-broker -g donna-bridge \
  /Users/grahamwilliamson/donna/broker/main.py \
  /Users/grahamwilliamson/donna/broker/creds.py \
  /Users/grahamwilliamson/donna/broker/executor.py \
  /Users/grahamwilliamson/donna/broker/policy.py \
  /Users/donna-broker/broker/

sudo install -m 0755 -o donna-broker -g donna-bridge \
  /Users/grahamwilliamson/donna/broker/executors/everyone_active_checkout \
  /Users/grahamwilliamson/donna/broker/executors/everyone_active_site_check \
  /Users/donna-broker/broker/executors/
```

## 3. Deploy manifests (atomic, self-verifying)

Lands `capabilities.yaml` (now with `everyone_active.checkout`) +
`schemas/everyone_active_checkout.json`, then runs `verify-manifests`:

```bash
sudo /Users/grahamwilliamson/donna/ops/deploy-manifests.sh
```

## 4. Reinstall the root-owned wrapper (new VALID_MODES)

```bash
sudo install -m 0755 -o root -g wheel \
  /Users/grahamwilliamson/donna/ops/donna-broker.sh /usr/local/bin/donna-broker
```

**sudoers: no change.** `/etc/sudoers.d/donna-broker` allowlists the
wrapper binary without restricting arguments; mode validation lives in
the wrapper + `broker/main.py MODES`.

## 5. Smoke tests

```bash
cd /tmp

# manifest picked up the new capability
sudo -u donna-broker /usr/local/bin/donna-broker verify-manifests '{}' \
  | grep -o 'everyone_active.checkout'

# live login probe with the existing EA vault entry (~20s, headless Chrome)
sudo -u donna-broker /usr/local/bin/donna-broker site-check '{"site":"everyone_active"}'
# expect {"status": "ok", ...}

# store-credential round-trip against a scratch slug (NOT everyone_active —
# don't overwrite the live EA entry)
sudo -u donna-broker /usr/local/bin/donna-broker store-credential \
  '{"site":"scratch_test","username":"u@x.com","password":"p"}'
# expect {"status": "stored", ...}; then clean up:
sudo rm /Users/donna-broker/.config/donna/creds/scratch_test.age

# checkout is per-purchase-only: standing grants must be refused
sudo -u donna-broker /usr/local/bin/donna-broker grant-create \
  '{"capability":"everyone_active.checkout","constraints":{"centre":"chesham"},"purpose":"x","max_per_period":1,"period_seconds":604800}'
# expect error_code "invalid_constraints" (per-action-only)

# the £50 cap rejects at request time, before any Telegram prompt
sudo -u donna-broker /usr/local/bin/donna-broker request \
  '{"capability":"everyone_active.checkout","params":{"activity_name":"Swimming Sessions","centre":"chesham","date":"2026-06-20","max_price":5001},"context_reason":"cap test"}'
# expect error_code "invalid_params" (max_price > 5000)
```

## 6. Restart the daru API

`_CATALOG` (and so `canCheckout`) is read once at server start:

```bash
launchctl kickstart -k gui/$(id -u)/com.user.daru-api
```

Then in the app: **Powers → Connected sites → Everyone Active → Check**
should come back connected/ok, and the card should now offer checkout.

## 7. Browser session fix (added 2026-06-11, after first deploy)

Chromium ≥149 (and current chromium-headless-shell) SIGTRAPs at launch when
its Mach-bootstrap namespace belongs to the **caller's** GUI session — which
is exactly what `sudo -u donna-broker` produces. Symptom: instant
`TargetClosedError ... signal=SIGTRAP`; helper processes log
`No rendezvous client, terminating process`. Proven fix: run the broker tree
inside donna-broker's own launchd user domain via `launchctl asuser`.

Install the root trampoline + its sudoers fragment:

```bash
sudo install -m 0755 -o root -g wheel \
  /Users/grahamwilliamson/donna/ops/donna-broker-via-session.sh \
  /usr/local/bin/donna-broker-via-session
sudo sh -c 'printf "%s\n" \
  "# Managed alongside ops/donna-broker-via-session.sh — do not edit by hand." \
  "grahamwilliamson ALL=(root) NOPASSWD: /usr/local/bin/donna-broker-via-session" \
  > /etc/sudoers.d/donna-broker-via-session'
sudo chmod 0440 /etc/sudoers.d/donna-broker-via-session
sudo visudo -cf /etc/sudoers.d/donna-broker-via-session
```

Callers that touch browser executors use the trampoline form
(`sudo -n /usr/local/bin/donna-broker-via-session <mode> <json>`): the daru
app's `site_check`/`execute` (broker_bridge `via_session=True`) and Donna's
subprocess-capability `execute` (hook allowlist updated in
`hooks/capability-guard.sh`). Everything else stays on the direct form.

## Notes

- The checkout executor enforces the £50 hard cap **and** the approved
  `max_price` before any pay click, fail-closed: if it cannot read the
  order total from the page it refuses with `unexpected_dom` rather
  than pay blind. Card-on-file only — it never types card details
  (`no_card_on_file` if the page asks).
- The basket→payment DOM was written from EA page-family conventions,
  not a live recon. If a first real purchase fails with
  `unexpected_dom`, the detail names the step; tighten the selectors in
  `broker/executors/everyone_active_checkout` and redeploy step 2.
- Browser: `chromium-headless-shell` (installed via
  `playwright install chromium-headless-shell` as donna-broker). System
  Chrome broke at v149 (2026-06-11) — SIGTRAP under the GUI-less service
  account; full bundled Chromium never worked there either. Re-run the
  install after playwright upgrades.
