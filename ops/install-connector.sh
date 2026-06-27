#!/usr/bin/env bash
# ops/install-connector.sh <draft_id>
#
# Promote a VALIDATED, app-proposed connector draft to live read/write tools in
# the broker's mcp-tools.yaml. Same security model as install-capability.sh:
# the app only PROPOSES; THIS script (run by Graham) stages into the SOURCE
# manifest and goes live through the unchanged privileged deploy.
set -euo pipefail
ID="${1:?usage: install-connector.sh <draft_id>}"
DARU_API="/Users/grahamwilliamson/daru/api"
PY="${DARU_API}/.venv/bin/python"
DEPLOY="/Users/grahamwilliamson/donna/ops/deploy-manifests.sh"

run_py() { PYTHONPATH="${DARU_API}" "${PY}" -m daru.connector_install "$@"; }

SERVER="$(run_py "${ID}" --print-server)" || { echo "ERROR: draft ${ID} not found" >&2; exit 1; }
echo "==> connector: ${SERVER} (draft ${ID})"
echo "==> validating"
run_py "${ID}" --check
echo "==> staging assessed tools into the broker source manifest"
run_py "${ID}"
echo "==> deploying to the live broker (sudo; runs verify-manifests)"
sudo "${DEPLOY}"
echo "==> marking draft ${ID} live"
run_py "${ID}" --mark-installed
echo "==> done. ${SERVER}'s tools are live on the next broker call."
