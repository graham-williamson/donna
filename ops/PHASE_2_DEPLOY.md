# Phase 2 deployment

Spec: `/Users/grahamwilliamson/.claude/plans/donna-security-v1.md` (v1.1).

Phase 1 is live (broker + hash-chained audit + Telegram approval +
Phase 1 hooks, per `PHASE_1_DEPLOY.md`). Phase 2 layers on subprocess
executors with real capabilities that can actually do things on
Graham's behalf — classes booked, websites navigated, credentials
unlocked just-in-time via an age vault.

Phase 2 is built in discrete **Pieces**. Each Piece ships machinery
without lighting up a production capability. The first production
capability (Everyone Active class booking) arrives in Piece D.

- **Piece A — Deploy hardening.** Preflight + atomic manifest deploy +
  concurrency sweep + SessionStart hook. **Done.**
- **Piece B — Age-encrypted credential vault.** `broker/creds.py`,
  `ops/create-vault-entry.sh`, forbidden-key audit event, tests.
  **Code landed; operator steps below.**
- **Piece C — Subprocess executor runtime.** Capability-bound binary
  launcher with sanitised env + credential injection via creds.py.
- **Piece D — Everyone Active executor.** First production subprocess
  capability using Piece B + C end-to-end.
- **Piece E — Phase 2 gate.** End-to-end trust check before any real
  credential lands in the vault.

---

## Piece A — Deploy hardening (done)

Three parts landed in the session of 2026-04-21:

- **`donna-broker verify-manifests`** walks every capability + every
  `$ref`'d JSON schema and meta-validates against Draft-07. Exit 1 on
  any gap. Wired into the wrapper and hook allowlists.
- **Supervisor preflight** (`claude-telegram-hardened/.../supervisor.ts`)
  calls `verify-manifests` before every spawn and before every restart-
  kill. On failure: the existing Claude stays alive. Look for
  `preflight ok: N capabilities, M mcp tools` in
  `supervisor-stderr.log`.
- **`ops/deploy-manifests.sh`** — atomic `install(1)` + post-deploy
  verify. Run as `sudo /Users/grahamwilliamson/donna/ops/deploy-manifests.sh`.
  Logs to `/var/log/donna-deploy.log`.

Net effect: the failure mode that bit us on 2026-04-21 (missing schema
silently breaking every Donna tool call with `manifest_error`) can no
longer happen — a partial deploy fails loudly at deploy time rather
than silently at tool-call time.

---

## Piece B — Age vault

### What's in the repo

- `broker/creds.py` — `unlock_creds(capability, creds_dir, identity,
  age_binary, timeout, audit_writer) -> bytes`. Pure function. Spawns
  `age --decrypt -i <identity> <ciphertext>`, returns stdout bytes.
  Emits `creds_unlock` audit event (no plaintext).
- `broker/tests/test_creds.py` — unit tests (shell-stub `age`) plus a
  real-age round-trip (auto-skipped if `age` is not installed).
- `ops/create-vault-entry.sh` — create or replace a vault entry for a
  given capability. Reads plaintext from stdin, encrypts to the
  recipient derived from `identity.age`, lands at the right mode /
  owner via `install(1)`.

No capability depends on the vault yet — that's Piece C + D territory.
Piece B is machinery + tests only.

### Operator steps

**Total time:** ~10 minutes.

#### B.1 — Install age (~2 min)

```bash
brew install age
```

Confirm:

```bash
age --version
age-keygen --version
```

#### B.2 — Create the creds directory (~1 min)

Runs as root. Mode `0750` so `donna-bridge` group members (Graham) can
traverse but not list; `donna-broker` owns.

```bash
sudo install -d -m 0750 -o donna-broker -g donna-bridge \
  /Users/donna-broker/.config/donna/creds
```

#### B.3 — Generate the broker identity (~1 min)

The identity file contains a private X25519 key. It must never leave
`donna-broker`'s home. Generate locally, land via `install(1)`, then
scrub the temp copy.

```bash
umask 077
age-keygen -o /tmp/donna-identity.age
sudo install -m 0400 -o donna-broker -g wheel \
  /tmp/donna-identity.age \
  /Users/donna-broker/.config/donna/creds/identity.age
shred -u /tmp/donna-identity.age 2>/dev/null || rm -f /tmp/donna-identity.age
```

Verify:

```bash
sudo -u donna-broker age-keygen -y \
  /Users/donna-broker/.config/donna/creds/identity.age
```

Expected: one `age1…` recipient string on stdout.

#### B.4 — Run the broker tests (~2 min)

Confirm the vault module works against the real age binary:

```bash
cd /Users/grahamwilliamson/donna/broker
source .venv/bin/activate
pytest tests/test_creds.py -v
```

Expected: all tests pass, including `test_real_age_roundtrip` (which
generates a throwaway identity + ciphertext in `tmp_path` to confirm
the decrypt path is wired correctly).

Then re-run the full suite to confirm the new module doesn't regress
anything:

```bash
pytest
```

#### B.5 — Create a synthetic vault entry (~1 min, optional)

To exercise the setup script without tying anything to a production
capability, create a dummy entry. It'll be cleaned up in Piece E.

```bash
printf 'test-secret-value' | sudo /Users/grahamwilliamson/donna/ops/create-vault-entry.sh synthetic.test
```

Verify:

```bash
sudo ls -la /Users/donna-broker/.config/donna/creds/
```

Expected: `identity.age` and `synthetic.test.age`, both mode `0400`,
owner `donna-broker`.

Decrypt round-trip (run the unlock via the broker user to confirm
group permissions are right):

```bash
cd /Users/grahamwilliamson/donna/broker
sudo -u donna-broker .venv/bin/python -c "
from broker.creds import unlock_creds
from pathlib import Path
root = Path('/Users/donna-broker/.config/donna/creds')
print(unlock_creds('synthetic.test', root, root / 'identity.age').decode())
"
```

Expected: `test-secret-value` (no trailing newline).

### What's NOT done after Piece B

Piece B is machinery. No production capability reads credentials yet.
Piece C wires `creds.py` into the subprocess executor; Piece D adds
the Everyone Active credential blob via `create-vault-entry.sh
everyone_active.book_class`. Don't store a real credential under
`/Users/donna-broker/.config/donna/creds/` yet beyond the synthetic
entry above — it'd be an asset at rest with no legitimate consumer.

---

## Pieces C / D / E — scoped as separate sessions

See the spec §17 for the acceptance criteria. Each piece has its own
deploy section added here when it lands.
