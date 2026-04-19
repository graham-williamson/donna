#!/usr/bin/env bash
# donna-broker — wrapper for /usr/local/bin/donna-broker.
#
# Spec: security-v1.1 §17 Phase 1. Runs the broker Python entry point
# under the `donna-broker` OS user with a sanitised environment, as
# the sudoers rule expects.
#
# Install location: /usr/local/bin/donna-broker (owned root:wheel, 0755).
# See ops/setup-donna-broker.sh for the install step.

set -euo pipefail

# ---- hardcoded paths ----
BROKER_ROOT="/Users/donna-broker/broker"
VENV_PYTHON="${BROKER_ROOT}/.venv/bin/python3"
BROKER_CONFIG_HOME="/Users/donna-broker/.config/donna"

# ---- validation ----
if [[ $# -lt 1 ]]; then
  echo '{"status":"error","error_code":"usage","message":"usage: donna-broker <mode>"}' >&2
  exit 1
fi

MODE="$1"
shift

# Valid modes (kept in sync with broker/main.py MODES).
VALID_MODES=(
  request policy-check execute cancel reconcile
  status status-by-code list-pending list-recent
  audit-result rotate-hmac verify-audit
)
FOUND=0
for m in "${VALID_MODES[@]}"; do
  [[ "$MODE" == "$m" ]] && FOUND=1 && break
done
if [[ "$FOUND" -eq 0 ]]; then
  printf '{"status":"error","error_code":"unknown_mode","message":"unknown mode: %s"}\n' "$MODE" >&2
  exit 1
fi

# ---- sanitised env ----
# Mirror §9.2 approach: PATH only, no inherited secrets. Config paths
# are passed explicitly so the broker never reads unset defaults.
exec env -i \
  PATH="/usr/bin:/bin:/usr/local/bin" \
  HOME="/Users/donna-broker" \
  DONNA_BROKER_HOME="${BROKER_CONFIG_HOME}" \
  DONNA_BROKER_DB="${BROKER_CONFIG_HOME}/requests.db" \
  DONNA_BROKER_AUDIT_DIR="/Users/donna-broker/audit" \
  DONNA_BROKER_HMAC_KEY="${BROKER_CONFIG_HOME}/hmac.key" \
  DONNA_BROKER_CAPABILITIES="${BROKER_CONFIG_HOME}/capabilities.yaml" \
  DONNA_BROKER_MCP_TOOLS="${BROKER_CONFIG_HOME}/mcp-tools.yaml" \
  DONNA_BROKER_QUEUE_DIR="${BROKER_CONFIG_HOME}/approval-queue" \
  DONNA_BROKER_RESPONSES_DIR="${BROKER_CONFIG_HOME}/approval-responses" \
  PYTHONPATH="${BROKER_ROOT}/.." \
  "${VENV_PYTHON}" -m broker.main "${MODE}" "$@"
