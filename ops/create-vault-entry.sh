#!/usr/bin/env bash
# ops/create-vault-entry.sh — encrypt a credential blob to the Donna
# broker's age identity and land it at the right path with the right
# ownership + mode.
#
# Spec: security-v1.1 §17 (Phase 2 age vault), §12.1 (broker home layout).
#
# Writes /Users/donna-broker/.config/donna/creds/<capability>.age
# encrypted to the public recipient derived from identity.age, as
# `donna-broker:donna-bridge 0400`. Idempotent: refuses to overwrite
# an existing entry unless --force is passed.
#
# Plaintext is read from stdin. If stdin is a tty, prompts interactively
# with echo suppressed. Plaintext is never written to disk except as
# age ciphertext; the temporary ciphertext file lives in a tmp dir that
# is torn down on exit.
#
# Usage:
#   sudo /Users/grahamwilliamson/donna/ops/create-vault-entry.sh <capability> [--force]
#   echo "token-value" | sudo /Users/grahamwilliamson/donna/ops/create-vault-entry.sh <capability>
#
# Exit codes:
#   0 — entry created (or replaced with --force)
#   1 — any problem (bad args, identity missing, encrypt failed, etc.)

set -euo pipefail

# ---- config --------------------------------------------------------------

CREDS_DIR="/Users/donna-broker/.config/donna/creds"
IDENTITY_PATH="${CREDS_DIR}/identity.age"
OWNER_USER="donna-broker"
OWNER_GROUP="donna-bridge"
FILE_MODE="0400"

# Must match broker.creds.CAPABILITY_NAME_RE exactly — see creds.py.
# Lowercase, digits, dot / underscore / hyphen, alnum anchor ends.
CAPABILITY_RE='^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$'

# ---- args ---------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: sudo create-vault-entry.sh <capability> [--force]

<capability>  e.g. everyone_active.book_class. Lowercase only.
--force       Replace an existing vault entry for this capability.

Plaintext is read from stdin. Example:
  printf '%s' "$my_secret" | sudo ops/create-vault-entry.sh my.cap
EOF
}

CAPABILITY=""
FORCE=0
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "ERROR: unknown flag ${arg}" >&2; usage >&2; exit 1 ;;
    *)
      if [[ -n "${CAPABILITY}" ]]; then
        echo "ERROR: unexpected positional arg ${arg}" >&2
        exit 1
      fi
      CAPABILITY="${arg}"
      ;;
  esac
done

if [[ -z "${CAPABILITY}" ]]; then
  usage >&2
  exit 1
fi

if ! [[ "${CAPABILITY}" =~ ${CAPABILITY_RE} ]]; then
  echo "ERROR: capability name ${CAPABILITY@Q} doesn't match ${CAPABILITY_RE}" >&2
  exit 1
fi

# ---- preflight ----------------------------------------------------------

if [[ $(id -u) -ne 0 ]]; then
  echo "ERROR: this script must run as root. Try: sudo $0 ${CAPABILITY}" >&2
  exit 1
fi

if ! command -v age >/dev/null 2>&1; then
  echo "ERROR: age binary not found on PATH. Try: brew install age" >&2
  exit 1
fi

if ! command -v age-keygen >/dev/null 2>&1; then
  echo "ERROR: age-keygen not found on PATH. Try: brew install age" >&2
  exit 1
fi

if [[ ! -f "${IDENTITY_PATH}" ]]; then
  cat >&2 <<EOF
ERROR: identity file not found at ${IDENTITY_PATH}

First-time setup: generate the broker identity, e.g.
  umask 077
  age-keygen -o /tmp/donna-identity.age
  sudo install -m 0400 -o ${OWNER_USER} -g wheel \\
    /tmp/donna-identity.age ${IDENTITY_PATH}
  rm /tmp/donna-identity.age
EOF
  exit 1
fi

TARGET="${CREDS_DIR}/${CAPABILITY}.age"
if [[ -f "${TARGET}" && ${FORCE} -ne 1 ]]; then
  echo "ERROR: ${TARGET} already exists. Re-run with --force to replace." >&2
  exit 1
fi

# Ensure the creds dir itself exists with the right layout.
install -d -m 0750 -o "${OWNER_USER}" -g "${OWNER_GROUP}" "${CREDS_DIR}"

# ---- derive recipient ---------------------------------------------------
# age-keygen -y <identity> prints the public recipient. Reading the
# identity this way keeps the private half in memory for one moment
# only — no plaintext on disk outside what age itself touches.

RECIPIENT=$(sudo -u "${OWNER_USER}" age-keygen -y "${IDENTITY_PATH}" 2>/dev/null || true)
if [[ -z "${RECIPIENT}" || "${RECIPIENT}" != age1* ]]; then
  echo "ERROR: could not derive age recipient from ${IDENTITY_PATH}" >&2
  exit 1
fi

# ---- read plaintext -----------------------------------------------------

PLAINTEXT=""
if [[ -t 0 ]]; then
  # Interactive. Hide the echo.
  printf 'Enter credential for %s (input hidden, one line): ' "${CAPABILITY}" >&2
  IFS= read -r -s PLAINTEXT
  printf '\n' >&2
  if [[ -z "${PLAINTEXT}" ]]; then
    echo "ERROR: refusing to store an empty credential" >&2
    exit 1
  fi
else
  # Non-interactive: accept arbitrary bytes (including newlines) from
  # stdin. Read the whole buffer so multi-line blobs are supported.
  PLAINTEXT="$(cat)"
  if [[ -z "${PLAINTEXT}" ]]; then
    echo "ERROR: stdin was empty" >&2
    exit 1
  fi
fi

# ---- encrypt + land -----------------------------------------------------

TMP_DIR=$(mktemp -d "/tmp/donna-vault.XXXXXX")
chmod 0700 "${TMP_DIR}"
trap 'rm -rf "${TMP_DIR}"' EXIT

TMP_CT="${TMP_DIR}/${CAPABILITY}.age"

# printf (not echo) so we don't append a stray newline; --armor off by
# default for compactness. Encrypt via the broker's own uid so TCC /
# sandbox posture during Phase 2+ stays consistent.
printf '%s' "${PLAINTEXT}" \
  | age -r "${RECIPIENT}" -o "${TMP_CT}"

# Drop the in-memory plaintext reference as early as we can.
PLAINTEXT=""

install -m "${FILE_MODE}" -o "${OWNER_USER}" -g "${OWNER_GROUP}" \
  "${TMP_CT}" "${TARGET}"

echo "OK: wrote ${TARGET} (${FILE_MODE} ${OWNER_USER}:${OWNER_GROUP})"
exit 0
