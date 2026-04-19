#!/usr/bin/env bash
# teardown-donna-broker.sh — reverse setup-donna-broker.sh.
#
# Removes the donna-broker user, donna-bridge group, home directory,
# sudoers fragment, and /usr/local/bin/donna-broker. Does NOT remove
# the audit log by default (JSONL wins on conflict per §7.6 — you may
# want to keep it for forensics). Pass `--purge-audit` to nuke it.

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

PURGE_AUDIT=0
for arg in "$@"; do
  [[ "${arg}" == "--purge-audit" ]] && PURGE_AUDIT=1
done

BROKER_HOME="/Users/donna-broker"

# Confirm before destructive action.
echo "This will remove:"
echo "  - user donna-broker"
echo "  - group donna-bridge"
echo "  - ${BROKER_HOME}/broker (code + venv)"
echo "  - ${BROKER_HOME}/.config/donna (requests.db, hmac.key, secrets)"
if [[ "${PURGE_AUDIT}" -eq 1 ]]; then
  echo "  - ${BROKER_HOME}/audit (AUDIT LOG WILL BE DESTROYED)"
fi
echo "  - /usr/local/bin/donna-broker"
echo "  - /etc/sudoers.d/donna-broker"
echo ""
read -p "Proceed? [y/N] " confirm
[[ "${confirm}" == "y" || "${confirm}" == "Y" ]] || exit 1

rm -f /usr/local/bin/donna-broker
rm -f /etc/sudoers.d/donna-broker

if [[ "${PURGE_AUDIT}" -eq 1 ]]; then
  rm -rf "${BROKER_HOME}"
else
  # Preserve audit; remove the rest.
  rm -rf "${BROKER_HOME}/broker"
  rm -rf "${BROKER_HOME}/.config"
fi

dscl . -delete /Users/donna-broker 2>/dev/null || true
dscl . -delete /Groups/donna-bridge 2>/dev/null || true

echo "Teardown complete."
if [[ "${PURGE_AUDIT}" -eq 0 && -d "${BROKER_HOME}/audit" ]]; then
  echo "Audit log preserved at ${BROKER_HOME}/audit/"
fi
