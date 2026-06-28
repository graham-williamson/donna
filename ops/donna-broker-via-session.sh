#!/usr/bin/env bash
# donna-broker-via-session — root trampoline that runs the donna-broker CLI
# inside donna-broker's OWN launchd session (user-domain Mach bootstrap).
#
# Why this exists (2026-06-11): Chromium ≥149 / chromium-headless-shell
# helper processes rendezvous with the browser process through the per-user
# Mach bootstrap namespace. Under plain `sudo -u donna-broker` the process
# tree inherits the *caller's* GUI-session namespace (uid mismatch), the
# rendezvous fails ("No rendezvous client") and Chromium SIGTRAPs at launch.
# `launchctl asuser <uid>` re-homes the tree into donna-broker's own domain,
# where browser executors work. Diagnosed + proven on this machine — see
# ops/CONNECTED_SITES_DEPLOY.md "browser session fix".
#
# Security posture: runs as root ONLY for the launchctl re-homing, then
# immediately drops to donna-broker via the existing root-owned wrapper.
# Fixed content, fixed paths, argv passed through untouched — callers can
# reach exactly the same mode-validated broker CLI they could already reach
# via `sudo -u donna-broker /usr/local/bin/donna-broker`. No new surface.
#
# Install (root): see ops/CONNECTED_SITES_DEPLOY.md. Pairs with the sudoers
# fragment /etc/sudoers.d/donna-broker-via-session:
#   grahamwilliamson ALL=(root) NOPASSWD: /usr/local/bin/donna-broker-via-session
#
# Usage (identical contract to the wrapper):
#   sudo -n /usr/local/bin/donna-broker-via-session <mode> [<json>]

set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo '{"status":"error","error_code":"not_root","message":"donna-broker-via-session must run via sudo"}' >&2
  exit 1
fi

DONNA_UID="$(/usr/bin/id -u donna-broker)"

# Mark that we are inside the donna-broker launchd session. The broker refuses
# to spawn a `requires_session` (browser) executor unless this is set, so it can
# never SIGTRAP by being run outside the session (e.g. a standing-grant inline
# execute or a plain `sudo -u donna-broker execute`). `/usr/bin/env` sets it
# after the sudo drop, avoiding sudoers env-passing rules.
exec /bin/launchctl asuser "${DONNA_UID}" \
  /usr/bin/sudo -n -u donna-broker /usr/bin/env DONNA_VIA_SESSION=1 \
  /usr/local/bin/donna-broker "$@"
