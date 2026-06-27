#!/usr/bin/env bash
# install-promoter.sh — one-time privileged bootstrap for the signed-
# capability promoter daemon.
#
# Spec: 2026-06-14-signed-capability-promoter-design.md §6/§9/§11;
# plan 2026-06-15-promoter-daemon-bootstrap.md Task 8.
# Runbook: broker/docs/promoter-deploy.md.
#
# This is the SINGLE SSH step that stands up the promoter. The promoter
# cannot install itself (it would be a self-granting hole), so Graham
# runs this once via SSH. It is IDEMPOTENT: re-running is safe and only
# adds/updates trusted keys + reloads the daemon.
#
# It REFUSES to run as non-root, and installs the daemon plist + keys
# ONLY into root-owned locations the donna-broker / daru-api users
# CANNOT write (that separation is the whole point of the boundary).
#
# Usage:
#   sudo bash ops/install-promoter.sh <key_id>:<pubhex> [<key_id>:<pubhex> ...]
#
#   <key_id>  a bare label for the key (no path separators), e.g. "graham-2026".
#   <pubhex>  the 64-hex-char Ed25519 PUBLIC key printed by
#             `python tools/sign_pack.py keygen <priv_out>` on the AUTHORING
#             device. The PRIVATE key stays OFF this Mac.
#
# Each key is installed as /etc/donna/promoter/trusted_keys/<key_id>.ed25519.pub
# containing the hex (the exact format broker/pack_keys.py expects).

set -euo pipefail

# ---- preflight: must be root ----
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  echo "  The promoter is the privileged install boundary; its keys and" >&2
  echo "  daemon must live where donna-broker/daru-api cannot write." >&2
  exit 1
fi

if [[ "$#" -lt 1 ]]; then
  echo "ERROR: at least one <key_id>:<pubhex> argument is required." >&2
  echo "  usage: sudo bash ops/install-promoter.sh <key_id>:<pubhex> [...]" >&2
  exit 1
fi

# Resolve the repo so we can find the plist next to this script, even
# when invoked via an absolute path or from another cwd.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="${SCRIPT_DIR}/com.donna.promoter.plist"

if [[ ! -f "${PLIST_SRC}" ]]; then
  echo "ERROR: ${PLIST_SRC} not found (run from the donna repo)." >&2
  exit 1
fi

# ---- canonical paths (must match com.donna.promoter.plist exactly) ----
BROKER_USER="donna-broker"
TRUSTED_KEYS_DIR="/etc/donna/promoter/trusted_keys"
PACKS_DIR="/Users/donna-broker/broker/packs/available"
SOCK_DIR="/var/run/donna"
LOG_DIR="/var/log/donna"
LEDGER="${LOG_DIR}/promoter.jsonl"
LAUNCHD_LABEL="com.donna.promoter"
PLIST_DST="/Library/LaunchDaemons/${LAUNCHD_LABEL}.plist"

# ---- 1. trusted-keys dir: root-owned, 0700 ----
echo "==> creating ${TRUSTED_KEYS_DIR} (root:wheel 0700)"
install -d -m 0700 -o root -g wheel "$(dirname "${TRUSTED_KEYS_DIR}")"
install -d -m 0700 -o root -g wheel "${TRUSTED_KEYS_DIR}"

# ---- 2. install each public key, validating the hex ----
INSTALLED_KEY_IDS=()
for arg in "$@"; do
  if [[ "${arg}" != *:* ]]; then
    echo "ERROR: argument '${arg}' is not in <key_id>:<pubhex> form." >&2
    exit 1
  fi
  key_id="${arg%%:*}"
  pubhex="${arg#*:}"

  # key_id must be a bare label — never a path component (it becomes a
  # filename under the root-owned trusted-keys dir).
  if [[ -z "${key_id}" ]]; then
    echo "ERROR: empty key_id in '${arg}'." >&2
    exit 1
  fi
  if [[ "${key_id}" == *"/"* || "${key_id}" == *".."* || "${key_id}" == "." ]]; then
    echo "ERROR: unsafe key_id '${key_id}' (no '/', '..', or '.')." >&2
    exit 1
  fi

  # Normalise + validate the hex: exactly 64 lowercase hex chars (raw
  # 32-byte Ed25519 public key, per broker/pack_keys.py).
  pubhex="$(printf '%s' "${pubhex}" | tr '[:upper:]' '[:lower:]')"
  if [[ ! "${pubhex}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: public key for '${key_id}' is not 64 hex chars." >&2
    echo "  got ${#pubhex} char(s): expected the raw Ed25519 pubkey hex" >&2
    echo "  printed by 'sign_pack.py keygen'." >&2
    exit 1
  fi

  key_file="${TRUSTED_KEYS_DIR}/${key_id}.ed25519.pub"
  # Write atomically with the right perms (0600, root) via a temp file.
  tmp_key="$(mktemp "${TRUSTED_KEYS_DIR}/.${key_id}.XXXXXX")"
  printf '%s' "${pubhex}" > "${tmp_key}"
  chmod 0600 "${tmp_key}"
  chown root:wheel "${tmp_key}"
  mv -f "${tmp_key}" "${key_file}"
  echo "==> installed trusted key ${key_id} -> ${key_file}"
  INSTALLED_KEY_IDS+=("${key_id}")
done

# ---- 3. packs dir: readable by the root daemon ----
# Owned by donna-broker (packs are dropped here by the deploy/app flow),
# 0755 so the root daemon can read them. Created idempotently.
echo "==> ensuring packs dir ${PACKS_DIR}"
if id -u "${BROKER_USER}" >/dev/null 2>&1; then
  install -d -m 0755 -o "${BROKER_USER}" -g staff "${PACKS_DIR}"
else
  echo "WARNING: user ${BROKER_USER} not found; creating packs dir root-owned." >&2
  echo "  Run ops/setup-donna-broker.sh first if this is a fresh box." >&2
  install -d -m 0755 -o root -g wheel "${PACKS_DIR}"
fi

# ---- 4. socket dir + ledger: root-owned ----
# The daemon creates the socket itself at 0600; we only provide the dir.
echo "==> creating socket dir ${SOCK_DIR} (root:wheel 0755)"
install -d -m 0755 -o root -g wheel "${SOCK_DIR}"

echo "==> creating log dir ${LOG_DIR} (root:wheel 0755)"
install -d -m 0755 -o root -g wheel "${LOG_DIR}"

if [[ ! -f "${LEDGER}" ]]; then
  echo "==> creating ledger ${LEDGER} (root:wheel 0600)"
  install -m 0600 -o root -g wheel /dev/null "${LEDGER}"
else
  echo "==> ledger ${LEDGER} already exists (keeping it — append-only)"
fi

# ---- 5. install the LaunchDaemon plist + (re)load it ----
echo "==> installing ${PLIST_DST} (root:wheel 0644)"
install -m 0644 -o root -g wheel "${PLIST_SRC}" "${PLIST_DST}"

# Validate the plist before asking launchd to load it.
if ! plutil -lint "${PLIST_DST}" >/dev/null; then
  echo "ERROR: ${PLIST_DST} failed plutil -lint." >&2
  exit 1
fi

# Idempotent (re)load: bootout an existing instance, then bootstrap.
# Fall back to legacy unload/load if bootstrap is unavailable.
echo "==> (re)loading launchd daemon ${LAUNCHD_LABEL}"
if launchctl bootout "system/${LAUNCHD_LABEL}" 2>/dev/null; then
  echo "    booted out existing instance"
fi
if ! launchctl bootstrap system "${PLIST_DST}" 2>/dev/null; then
  echo "    bootstrap unavailable/failed; trying legacy load" >&2
  launchctl unload "${PLIST_DST}" 2>/dev/null || true
  launchctl load -w "${PLIST_DST}"
fi

# ---- 6. verification summary ----
echo ""
echo "========================================"
echo "Promoter bootstrap complete."
echo "========================================"
echo "Paths:"
echo "  trusted keys : ${TRUSTED_KEYS_DIR} (0700 root:wheel)"
echo "  packs dir    : ${PACKS_DIR}"
echo "  socket dir   : ${SOCK_DIR} (daemon creates promoter.sock 0600)"
echo "  ledger       : ${LEDGER}"
echo "  plist        : ${PLIST_DST}"
echo ""
echo "Trusted key ids installed:"
for kid in "${INSTALLED_KEY_IDS[@]}"; do
  echo "  - ${kid}"
done
echo ""
echo "Daemon status (launchctl):"
launchctl print "system/${LAUNCHD_LABEL}" 2>/dev/null | grep -E "state|pid" | sed 's/^/  /' \
  || launchctl list | grep "${LAUNCHD_LABEL}" | sed 's/^/  /' \
  || echo "  (not listed yet — check 'launchctl print system/${LAUNCHD_LABEL}')"
echo ""
echo "To revoke a key:  echo '<key_id>' | sudo tee -a ${TRUSTED_KEYS_DIR}/revoked"
echo "To uninstall:     sudo launchctl bootout system/${LAUNCHD_LABEL}; sudo rm ${PLIST_DST}"
