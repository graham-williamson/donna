#!/usr/bin/env bash
# ops/install-capability.sh <draft_id>
#
# Promote an APPROVED, app-proposed capability draft to a LIVE broker capability.
#
# SECURITY MODEL
#   The Daru app only ever PROPOSES — capability drafts live in daru.db; the app
#   never writes the broker manifest. THIS script, run by Graham, is the single
#   bridge from an approved draft to the live broker. It is safe by construction:
#     - it installs ONLY mcp_tool capabilities (daru.capability_install.stage
#       refuses subprocess / unknown-tool / loose-schema / collision drafts);
#     - it goes live through the EXISTING privileged deploy, deploy-manifests.sh,
#       which mirrors source→live atomically and runs `donna-broker
#       verify-manifests` (a loud preflight). That deploy is the unchanged gate.
#
# Run as grahamwilliamson (NOT under sudo). It sudo's only the deploy sub-step.
#   /Users/grahamwilliamson/donna/ops/install-capability.sh <draft_id>
#
# Idempotent: if a prior run staged the entry but the deploy failed, re-running
# skips re-staging and just re-deploys.
set -euo pipefail

ID="${1:?usage: install-capability.sh <draft_id>}"
DARU_API="/Users/grahamwilliamson/daru/api"
PY="${DARU_API}/.venv/bin/python"
DEPLOY="/Users/grahamwilliamson/donna/ops/deploy-manifests.sh"
SRC_CAP="/Users/grahamwilliamson/donna/broker/manifests/capabilities.yaml"

run_py() { PYTHONPATH="${DARU_API}" "${PY}" -m daru.capability_install "$@"; }

NAME="$(run_py "${ID}" --print-name)" || { echo "ERROR: draft ${ID} not found" >&2; exit 1; }
echo "==> capability: ${NAME} (draft ${ID})"

if grep -Eq "^[[:space:]]*-[[:space:]]*name:[[:space:]]*${NAME}[[:space:]]*$" "${SRC_CAP}"; then
  echo "==> already staged in the source manifest (prior run) — going straight to deploy"
else
  echo "==> validating"
  run_py "${ID}" --check
  echo "==> staging into the broker source manifest"
  run_py "${ID}"
fi

echo "==> deploying to the live broker (sudo; runs verify-manifests)"
sudo "${DEPLOY}"

echo "==> marking draft ${ID} installed"
run_py "${ID}" --mark-installed

echo "==> done. '${NAME}' is live and will be enforced on the next broker call."
