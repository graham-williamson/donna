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

## IMPORTANT: no broker restart after a pack install

The donna broker is a **per-call CLI** (`/usr/local/bin/donna-broker`, invoked
via sudo) that **reloads `capabilities.yaml` on every invocation**. A manifest
merge therefore needs **no broker restart** — the very next broker call already
sees the new pack.

Accordingly the daemon's restart hook is wired as a **deliberate no-op**
(`_no_restart` in `promoter_daemon.main`). The module keeps a
`_kickstart_broker` helper and a `BROKER_LAUNCHD_LABEL` constant for a *future*
resident-broker world, but **both are intentionally unused today**. Do **not**
wire a `launchctl kickstart` of the broker into the plist or anywhere else: there
is no resident broker service to kick, so it would always fail and mislabel a
perfectly good install as `installed_restart_failed`.

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
| `--live-manifests-dir` | `/Users/donna-broker/broker/manifests` |
| `--broker-db` | `/Users/donna-broker/.config/donna/requests.db` |
| `--ledger` | `/var/log/donna/promoter.jsonl` |

Allowed peer uids (`{0, donna-broker}`) are resolved by the daemon itself, not
passed on the command line. `PYTHONPATH=/Users/donna-broker` (so `import broker`
resolves) mirrors `ops/donna-broker.sh`.
