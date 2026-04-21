#!/usr/bin/env bash
# ops/deploy-manifests.sh — atomic manifest + schema deploy for the
# Donna broker.
#
# Spec: security-v1.1 §8 (manifest format), §13.1 (broker modes).
#
# Mirrors /Users/grahamwilliamson/donna/broker/manifests/ into the live
# broker config at /Users/donna-broker/.config/donna/, using install(1)
# so each file lands atomically with owner donna-broker:donna-bridge
# and mode 0644. Then invokes `donna-broker verify-manifests` to
# confirm the live state parses end-to-end (capabilities.yaml,
# mcp-tools.yaml, every $ref'd JSON schema).
#
# Replaces the ad-hoc sudo-cp dance that silently left Donna broken
# for two hours when a single schema file was missed (2026-04-21 —
# see memory: project_deploy_manifest_verification.md). The post-deploy
# verify step means a partial-deploy regression fails loudly here, not
# in the PreToolUse hook at tool-call time.
#
# Usage:
#   sudo /Users/grahamwilliamson/donna/ops/deploy-manifests.sh
#
# Exit codes:
#   0 — all files deployed, verify-manifests passed
#   1 — any problem (missing source file, install failure, verify failure)

set -euo pipefail

# ---- config --------------------------------------------------------------

REPO_ROOT="/Users/grahamwilliamson/donna"
SRC_DIR="${REPO_ROOT}/broker/manifests"
DEST_DIR="/Users/donna-broker/.config/donna"
LOG="/var/log/donna-deploy.log"
BROKER_BIN="/usr/local/bin/donna-broker"
OWNER_USER="donna-broker"
OWNER_GROUP="donna-bridge"
FILE_MODE="0644"
DIR_MODE="0755"

# ---- preamble ------------------------------------------------------------

if [[ $(id -u) -ne 0 ]]; then
  echo "ERROR: this script must run as root. Try: sudo $0" >&2
  exit 1
fi

# Ensure the log file exists and is world-readable (handy for tail).
touch "${LOG}"
chmod 0644 "${LOG}"

log() {
  local ts
  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '%s [deploy-manifests] %s\n' "${ts}" "$*" | tee -a "${LOG}"
}

log "begin deploy from ${SRC_DIR}"

# ---- sanity check source tree -------------------------------------------

if [[ ! -d "${SRC_DIR}" ]]; then
  log "FATAL: source dir ${SRC_DIR} does not exist"
  exit 1
fi

if [[ ! -f "${SRC_DIR}/capabilities.yaml" ]]; then
  log "FATAL: ${SRC_DIR}/capabilities.yaml missing"
  exit 1
fi

if [[ ! -f "${SRC_DIR}/mcp-tools.yaml" ]]; then
  log "FATAL: ${SRC_DIR}/mcp-tools.yaml missing"
  exit 1
fi

# ---- ensure destination tree ---------------------------------------------

install -d -m "${DIR_MODE}" -o "${OWNER_USER}" -g "${OWNER_GROUP}" "${DEST_DIR}"
install -d -m "${DIR_MODE}" -o "${OWNER_USER}" -g "${OWNER_GROUP}" "${DEST_DIR}/schemas"

# ---- per-file deploy -----------------------------------------------------
# install(1) on macOS performs atomic create-via-temp-then-rename, so
# each file lands whole or not at all. Collective atomicity is enforced
# by the verify-manifests step at the end: if a partial deploy produces
# an inconsistent live state, verify catches it and the script exits
# non-zero.

deploy_file() {
  local src="$1"
  local dst="$2"
  if [[ ! -f "${src}" ]]; then
    log "FATAL: source file missing: ${src}"
    exit 1
  fi
  install -m "${FILE_MODE}" -o "${OWNER_USER}" -g "${OWNER_GROUP}" \
    "${src}" "${dst}"
  log "deployed ${src} → ${dst}"
}

deploy_file "${SRC_DIR}/capabilities.yaml" "${DEST_DIR}/capabilities.yaml"
deploy_file "${SRC_DIR}/mcp-tools.yaml" "${DEST_DIR}/mcp-tools.yaml"

# Every schema under schemas/. Uses nullglob so an empty schemas/ dir
# simply deploys no schemas rather than copying a literal "*.json" to
# the destination.
shopt -s nullglob
schema_count=0
for schema in "${SRC_DIR}/schemas/"*.json; do
  deploy_file "${schema}" "${DEST_DIR}/schemas/$(basename "${schema}")"
  schema_count=$((schema_count + 1))
done
shopt -u nullglob
log "deployed ${schema_count} schema file(s)"

# Note: we do NOT prune schemas on the destination that no longer
# exist in the repo. Deliberate — a rename in the repo (e.g.
# puregym_book.json → everyone_active_book.json) would otherwise
# delete the still-referenced old schema. Manual cleanup, when
# needed, is one ordinary rm.

# ---- verify --------------------------------------------------------------

log "running verify-manifests against live state"

set +e
VERIFY_OUT=$(sudo -u "${OWNER_USER}" "${BROKER_BIN}" verify-manifests '{}' 2>&1)
VERIFY_RC=$?
set -e
log "verify-manifests rc=${VERIFY_RC}"
log "verify-manifests output: ${VERIFY_OUT}"

if [[ ${VERIFY_RC} -ne 0 ]]; then
  log "DEPLOY FAILED: verify-manifests returned non-zero"
  exit 1
fi

# verify-manifests JSON shape: {"status":"ok","verified":true,...}
# A grep is adequate because the broker's JSON output is deterministic.
if ! echo "${VERIFY_OUT}" | grep -q '"status": "ok"'; then
  log "DEPLOY FAILED: verify-manifests did not return status:ok"
  exit 1
fi

log "deploy OK — all manifests + schemas live and verified"
exit 0
