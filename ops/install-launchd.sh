#!/usr/bin/env bash
# install-launchd.sh — register launchd jobs for the broker.
#
# Spec: security-v1.1 §3, §17 Phase 1 "broker health-check plist".
#
# Currently installs:
#   - com.donna.broker.verify-audit  (daily 03:15 audit chain check)
#
# Run as grahamwilliamson (NOT root). LaunchAgents live under the
# user's home and run as that user.

set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "ERROR: run as your normal user, NOT root." >&2
  echo "  LaunchAgents run as the user who loads them; running as" >&2
  echo "  root would bypass the sudoers-based invocation path." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="${HOME}/Library/LaunchAgents"
mkdir -p "${AGENT_DIR}"

JOBS=(
  "com.donna.broker.verify-audit"
)

for label in "${JOBS[@]}"; do
  src="${REPO_ROOT}/ops/${label}.plist"
  dst="${AGENT_DIR}/${label}.plist"

  if [[ ! -f "${src}" ]]; then
    echo "ERROR: ${src} not found" >&2
    exit 1
  fi

  # Unload existing job if loaded (idempotent re-install).
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true

  install -m 0644 "${src}" "${dst}"
  launchctl bootstrap "gui/$(id -u)" "${dst}"
  echo "==> loaded ${label}"
done

echo ""
echo "Launchd jobs installed. Verify with:"
echo "  launchctl list | grep com.donna.broker"
echo ""
echo "To uninstall:"
echo "  launchctl bootout gui/\$(id -u)/com.donna.broker.verify-audit"
echo "  rm ~/Library/LaunchAgents/com.donna.broker.verify-audit.plist"
