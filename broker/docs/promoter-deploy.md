# Promoter deploy runbook (privileged — run by Graham)

The **signed-capability promoter** is a tiny root launchd daemon
(`com.donna.promoter`) that installs signed capability packs into the live
broker manifests over a local Unix socket. It is the one piece that lets new
capabilities be enabled **app + Telegram only**, with signing preventing the
app from self-granting arbitrary powers.

- Daemon: `broker/promoter_daemon.py` (`-m broker.promoter_daemon`), runs as
  **root**.
- Broker-side executor: `broker/executors/promoter_client` (runs as
  `donna-broker`, holds no authority — just relays one frame to the socket).
- Off-device signer: `broker/tools/sign_pack.py`.
- Bootstrap: `ops/install-promoter.sh` + `ops/com.donna.promoter.plist`.

> **The promoter cannot install itself** (that would be a self-granting hole).
> Standing it up — and updating it later — is the only SSH step. Everything
> after that (enabling a new capability) is app + Telegram over Tailscale.

This **replaces interim steps 1–3** of the §14.1 amendment runbook
(`broker/docs/donna-security-v1-amendment-14.1.md`) for the **data-only pack
case**: where that doc had you SSH in to install a browser, store a credential,
and `deploy-manifests.sh`, a signed data-only pack is now proposed in the app,
approved in Telegram, and merged by the promoter — no terminal.

---

## 1. On the AUTHORING device (NOT the Mac): generate a signing key

The private key must **never** touch the Mac mini. Generate it on a device you
control (laptop, phone with Python, etc.):

```
python tools/sign_pack.py keygen <priv_out>
```

This writes the **private** key hex to `<priv_out>` (keep it OFF the Mac —
password manager / hardware-backed store) and prints:

```
public_key_hex: <64-hex-chars>
install this as <key_id>.ed25519.pub in the Mac trusted-keys dir
```

Copy the printed **public** hex. Choose a `key_id` label (a bare name, no
slashes), e.g. `graham-2026`.

## 2. ONE SSH session: bootstrap the promoter on the Mac

From the donna repo on the Mac, run **once** (idempotent — safe to re-run to
add/rotate keys):

```
sudo ops/install-promoter.sh <key_id>:<pubhex>
```

Multiple keys are allowed:

```
sudo ops/install-promoter.sh graham-2026:<hexA> backup-key:<hexB>
```

The script (refuses to run as non-root):

- creates `/etc/donna/promoter/trusted_keys/` (root:wheel, 0700) and installs
  each key as `<key_id>.ed25519.pub` (0600, root) containing the validated hex
  — the exact format `broker/pack_keys.py` reads;
- ensures the packs dir `/Users/donna-broker/broker/packs/available/`
  (root-readable);
- creates `/var/run/donna/` (the daemon makes the 0600 socket itself) and the
  root-owned ledger `/var/log/donna/promoter.jsonl`;
- installs `com.donna.promoter.plist` into `/Library/LaunchDaemons/`
  (root:wheel, 0644), lints it, and `launchctl bootstrap system`s it;
- prints a verification summary (paths, key ids, daemon status).

> Keys and daemon live **only** in root-owned locations the `donna-broker` /
> `daru-api` users cannot write — that separation is the trust boundary.

## 3. Author + sign packs off-device, then drop them in

On the authoring device:

```
python tools/sign_pack.py sign <pack_dir> <priv_hex_file>
```

This writes `<pack_dir>/pack.sig` (a detached Ed25519 signature over the pack's
canonical bytes). Copy the **signed** pack directory (including `pack.sig`) into
`/Users/donna-broker/broker/packs/available/<pack_id>/` on the Mac.

## 4. Thereafter: activate from the app + Telegram (NO terminal)

In the Daru app, "Set up" the capability → a normal broker request for
`promoter.install_pack {pack_id, pack_hash}` → Telegram approval. On approval
the broker runs `promoter_client` (as `donna-broker`), which connects to the
root promoter socket. The promoter **independently re-verifies** the approval
(reading the broker DB itself) + the pack signature/safety, atomically merges
the manifests, re-verifies, ledgers the outcome, and rolls back on any failure.
No SSH, no `deploy-manifests.sh`.

## 5. Revocation (SSH)

To drop a key from trust without deleting its file, append the `key_id` to the
`revoked` file (one id per line):

```
echo '<key_id>' | sudo tee -a /etc/donna/promoter/trusted_keys/revoked
```

`pack_keys.load_trusted_keys` excludes revoked ids at load time — a pack signed
only by a revoked key fails verification (fail-closed). No daemon restart is
needed; the keys are re-read per install.

## 6. Updating the promoter itself (SSH)

The promoter **cannot update itself** (it would be a self-granting hole).
After changing `promoter_daemon.py` / the orchestrator, re-deploy the broker
package (`ops/setup-donna-broker.sh` rsyncs it to
`/Users/donna-broker/broker/`) and reload the daemon:

```
sudo ops/install-promoter.sh <key_id>:<pubhex>   # idempotent reload, or:
sudo launchctl bootout system/com.donna.promoter
sudo launchctl bootstrap system /Library/LaunchDaemons/com.donna.promoter.plist
```

---

## IMPORTANT: merge into the manifests-only dir, then publish to the config dir

The promoter does its whole-directory atomic swap against the **manifests-only**
dir `--live-manifests-dir` (`/Users/donna-broker/broker/manifests`), which holds
`capabilities.yaml`, `schemas/`, `profiles/` and **nothing else**. That whole-dir
swap is only safe *because* that dir contains no other state.

The dir the broker actually **reads** is `--config-dir`
(`/Users/donna-broker/.config/donna`) — and that dir **also** holds the requests
DB, the HMAC key, the `creds/` age vault, and the approval queue. So after a
successful merge the daemon **publishes** the merged manifest into the config dir
via `promoter_fs.publish_to_config`: a **per-file atomic copy** of only
`capabilities.yaml`, `mcp-tools.yaml` (if present), `schemas/*.json`, and
`profiles/*`. It writes each file to a temp file in the same directory and
`os.replace`s it onto the target — it **never** does a whole-dir swap of the
config dir and **never** touches `requests.db`, `hmac.key`, `creds/`, or
`approval-queue/`. The published `capabilities.yaml` is re-validated (schema
`$ref`s resolved against the config dir) before publish is considered done.

## IMPORTANT: no broker restart after a pack install

The donna broker is a **per-call CLI** (`/usr/local/bin/donna-broker`, invoked
via sudo) that **reloads `capabilities.yaml` on every invocation**. A manifest
merge + publish therefore needs **no broker restart** — the very next broker
call already reads the freshly-published config.

Accordingly the post-merge action is the **publish**, not a launchctl kickstart.
There is no resident broker service to restart; do **not** wire a
`launchctl kickstart` of the broker into the plist or anywhere else (it would
always fail and mislabel a perfectly good install). A publish failure surfaces as
the ledger outcome `installed_publish_failed` (the merge into the manifests-only
dir still stands).

## Config: the plist and the executor must agree on the socket

`com.donna.promoter.plist` passes `--socket /var/run/donna/promoter.sock`, which
is exactly `promoter_client`'s `DEFAULT_SOCK`. If you change one, change both
(or set `DONNA_PROMOTER_SOCK` for the executor and `--socket` /
`DONNA_PROMOTER_SOCKET` for the daemon to the same value) — otherwise the
executor cannot reach the daemon.

The full set of config keys the plist passes (matching
`promoter_daemon.main`):

| arg | value |
|---|---|
| `--socket` | `/var/run/donna/promoter.sock` |
| `--packs-dir` | `/Users/donna-broker/broker/packs/available` |
| `--trusted-keys-dir` | `/etc/donna/promoter/trusted_keys` |
| `--live-manifests-dir` | `/Users/donna-broker/broker/manifests` (manifests-only; safe to swap) |
| `--config-dir` | `/Users/donna-broker/.config/donna` (dir the broker reads; publish target) |
| `--broker-db` | `/Users/donna-broker/.config/donna/requests.db` |
| `--ledger` | `/var/log/donna/promoter.jsonl` |

Allowed peer uids (`{0, donna-broker}`) are resolved by the daemon itself, not
passed on the command line. `PYTHONPATH=/Users/donna-broker` (so `import broker`
resolves) mirrors `ops/donna-broker.sh`.

## App setup flow (what Graham sees — no terminal)

Once the promoter is bootstrapped and a signed pack sits in `packs/available/`,
enabling it is entirely app + Telegram over Tailscale:

1. **Daru → Donna → Powers → Connections → "Set up new integration".** The app
   lists each signed, not-yet-installed pack by its plain-English title (e.g.
   "Waitrose groceries"). The broker is never named; the pack hash is computed by
   the broker, never supplied by the app.
2. **Tap "Set up".** The app calls `POST /api/donna/connections/packs/{id}/setup`,
   which proposes a `promoter.install_pack` action through the normal broker path.
   The app shows "Approve in Telegram".
3. **Approve in Telegram.** This is the human gate. `promoter.install_pack` is in
   `NO_STANDING_GRANTS` — every install needs a fresh per-use approval; it can never
   be granted standing autonomy.
4. **The broker executes the approved action** → runs the `promoter_client`
   executor (as `donna-broker`) → connects to the root promoter socket. The promoter
   independently re-verifies the signature, the data-only/reserved/policy-immutability
   rules, and that a real approved/executing install request exists for this exact
   pack + hash, then stages → `verify-manifests` → atomic-merge → re-verify →
   ledger. **No broker restart** is issued (the per-call CLI reloads the manifest on
   its next invocation).
5. **The app mirrors completion** via the existing approvals poll; the pack shows
   "Installed ✓" and its capabilities (e.g. `browser_goal.plan`/`browser_goal.commit`
   for a new site) are live under the unchanged browser-goal gate.

If anything fails verification, the install is refused, the live manifests are left
untouched (or rolled back byte-for-byte), and the refusal is recorded in
`/var/log/donna/promoter.jsonl`. Inspect that ledger to see every attempt.
