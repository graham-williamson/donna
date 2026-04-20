#!/usr/bin/env bash
# setup-donna-broker.sh — one-shot OS setup for Phase 1.
#
# Spec: security-v1.1 §17 Phase 1 "OS setup (first, 1–2 hours)",
# §7.3 (HMAC key), §12.1 (directory layout), §24.2 (Time Machine
# exclusions), §24.1 (TCC binary-path notes).
#
# This script is IDEMPOTENT: re-running is safe. It checks whether
# each step is already done before acting.
#
# Requires: sudo. Run from the donna repo root:
#   sudo bash ops/setup-donna-broker.sh

set -euo pipefail

# ---- preflight ----
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

if [[ ! -f broker/main.py ]]; then
  echo "ERROR: run from the donna repo root (broker/main.py not found here)." >&2
  exit 1
fi

REPO_ROOT="$(pwd)"
GRAHAM_USER="grahamwilliamson"  # change if your macOS user differs

# Python interpreter for the broker venv. Override on the command line:
#   sudo PYTHON=/opt/homebrew/bin/python3.12 bash ops/setup-donna-broker.sh
# macOS /usr/bin/python3 is typically 3.9 — too old for our pinned deps
# (jsonschema 4.26+ requires 3.10, broker spec requires 3.11).
PYTHON="${PYTHON:-/usr/bin/python3}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "ERROR: PYTHON=${PYTHON} not found or not executable." >&2
  exit 1
fi

# Python must be ≥ 3.11. Check before doing any irreversible work.
PYTHON_VER=$("${PYTHON}" -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=${PYTHON_VER%.*}
PYTHON_MINOR=${PYTHON_VER#*.}
if [[ "${PYTHON_MAJOR}" -lt 3 ]] || { [[ "${PYTHON_MAJOR}" -eq 3 ]] && [[ "${PYTHON_MINOR}" -lt 11 ]]; }; then
  echo "ERROR: Python ${PYTHON_VER} is too old. Need 3.11+." >&2
  echo "  Try one of:" >&2
  echo "    sudo PYTHON=/opt/homebrew/bin/python3.12 bash ops/setup-donna-broker.sh" >&2
  echo "    sudo PYTHON=/opt/miniconda3/bin/python3   bash ops/setup-donna-broker.sh" >&2
  echo "  Or install Homebrew Python: brew install python@3.12" >&2
  exit 1
fi

echo "==> using Python ${PYTHON_VER} from ${PYTHON}"

# donna-broker also needs to be able to READ this Python. /opt/homebrew/
# and /opt/miniconda3/ are typically world-readable; if you've changed
# defaults, the venv will fail later with a confusing PermissionError.
if ! sudo -u donna-broker test -r "${PYTHON}" 2>/dev/null; then
  echo "WARNING: donna-broker may not be able to read ${PYTHON}." >&2
  echo "  Continuing anyway — if venv creation fails with permission" >&2
  echo "  errors, chmod the Python directory tree to be readable by all." >&2
fi

# ---- 1. create donna-bridge group ----
if ! dscl . -read /Groups/donna-bridge >/dev/null 2>&1; then
  echo "==> creating group donna-bridge"
  dscl . -create /Groups/donna-bridge
  dscl . -create /Groups/donna-bridge PrimaryGroupID 600
else
  echo "==> group donna-bridge already exists"
fi

# ---- 2. create donna-broker system user ----
if ! dscl . -read /Users/donna-broker >/dev/null 2>&1; then
  echo "==> creating user donna-broker"
  dscl . -create /Users/donna-broker
  dscl . -create /Users/donna-broker UserShell /usr/bin/false
  dscl . -create /Users/donna-broker RealName "Donna Broker"
  dscl . -create /Users/donna-broker UniqueID 600
  dscl . -create /Users/donna-broker PrimaryGroupID 600
  dscl . -create /Users/donna-broker NFSHomeDirectory /Users/donna-broker
  dscl . -create /Users/donna-broker IsHidden 1
else
  echo "==> user donna-broker already exists"
fi

# ---- 3. add members to group ----
for MEMBER in donna-broker "${GRAHAM_USER}"; do
  if ! dscl . -read /Groups/donna-bridge GroupMembership 2>/dev/null | grep -qw "${MEMBER}"; then
    echo "==> adding ${MEMBER} to donna-bridge group"
    dscl . -append /Groups/donna-bridge GroupMembership "${MEMBER}"
  fi
done

# ---- 4. create home + directory tree ----
BROKER_HOME="/Users/donna-broker"
CONFIG="${BROKER_HOME}/.config/donna"

mkdir -p "${BROKER_HOME}/broker"
mkdir -p "${BROKER_HOME}/audit"
mkdir -p "${CONFIG}/secrets"
mkdir -p "${CONFIG}/approval-queue"
mkdir -p "${CONFIG}/approval-responses"
mkdir -p "${CONFIG}/backups"

echo "==> chown'ing ${BROKER_HOME} to donna-broker:donna-bridge"
chown -R donna-broker:donna-bridge "${BROKER_HOME}"

# 2770 on the shared dirs so donna-broker + grahamwilliamson (both in
# donna-bridge) can read/write, but no one else. Setgid makes new
# files inherit the group.
echo "==> setting 2770 on shared directories"
chmod 2770 "${CONFIG}/approval-queue"
chmod 2770 "${CONFIG}/approval-responses"

# Restrict the secrets dir to donna-broker only.
echo "==> setting 0700 on secrets dir"
chmod 0700 "${CONFIG}/secrets"

# ---- 5. copy broker package into donna-broker home ----
echo "==> rsyncing broker package to ${BROKER_HOME}/broker"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'tests/' \
  --exclude '*.pyc' \
  "${REPO_ROOT}/broker/" "${BROKER_HOME}/broker/"

# Manifests go into the config dir (so edits don't require a repo
# rsync). Copy seed manifests on first install only.
for m in capabilities.yaml mcp-tools.yaml; do
  if [[ ! -f "${CONFIG}/${m}" ]]; then
    cp "${REPO_ROOT}/broker/manifests/${m}" "${CONFIG}/${m}"
    echo "==> installed seed ${m}"
  fi
done

# Schemas referenced from capabilities.yaml live in ./schemas/, so copy
# them alongside.
mkdir -p "${CONFIG}/schemas"
cp -n "${REPO_ROOT}/broker/manifests/schemas/"*.json "${CONFIG}/schemas/" 2>/dev/null || true

chown -R donna-broker:donna-bridge "${BROKER_HOME}"

# ---- 6. create broker venv as donna-broker ----
# Three quirks combine to make this the gnarliest step on a fresh box:
#   1. macOS /usr/bin/python3 ships ensurepip that can fail in
#      non-interactive contexts (CLT-stub quirk). We use --without-pip
#      and bootstrap pip via get-pip.py to sidestep ensurepip entirely.
#   2. If the invoking user has conda's (base) active, PYTHONPATH /
#      PYTHONHOME can leak through sudo and point at /opt/miniconda3/...
#      that donna-broker can't read. `env -i` strips the env clean.
#   3. Python prepends cwd to sys.path on stdin scripts. If this script
#      ran from /Users/grahamwilliamson/donna (700), donna-broker hits
#      PermissionError during early import. We `cd /tmp` for the
#      donna-broker invocations so cwd is world-readable.
# All three failure modes surfaced on the same Mac Mini install.
VENV_PY="${BROKER_HOME}/broker/.venv/bin/python3"
CLEAN_ENV=(env -i HOME=/Users/donna-broker PATH=/usr/bin:/bin)
SAFE_CWD=/tmp

if [[ ! -x "${VENV_PY}" ]]; then
  echo "==> creating broker venv (without-pip, ~5s)"
  ( cd "${SAFE_CWD}" && sudo -u donna-broker "${CLEAN_ENV[@]}" \
      "${PYTHON}" -m venv --without-pip "${BROKER_HOME}/broker/.venv" )
fi

if ! ( cd "${SAFE_CWD}" && sudo -u donna-broker "${CLEAN_ENV[@]}" "${VENV_PY}" -m pip --version >/dev/null 2>&1 ); then
  echo "==> bootstrapping pip via get-pip.py"
  curl -sS https://bootstrap.pypa.io/get-pip.py \
    | ( cd "${SAFE_CWD}" && sudo -u donna-broker "${CLEAN_ENV[@]}" "${VENV_PY}" )
fi

echo "==> installing hash-locked broker dependencies"
( cd "${SAFE_CWD}" && sudo -u donna-broker "${CLEAN_ENV[@]}" "${VENV_PY}" -m pip install --upgrade pip --quiet )
( cd "${SAFE_CWD}" && sudo -u donna-broker "${CLEAN_ENV[@]}" "${VENV_PY}" -m pip install \
    --require-hashes -r "${BROKER_HOME}/broker/requirements.txt" --quiet )

# ---- 7. generate HMAC key (idempotent: only if missing) ----
# cd /tmp before the donna-broker subshell — same cwd-leak trap that
# the Python invocations dodged. New file inherits donna-bridge group
# automatically because that's donna-broker's primary group; no chown
# needed (the previous version of this section had `chown donna-broker:
# donna-broker` which fails because there is no donna-broker group).
HMAC_KEY="${CONFIG}/hmac.key"
if [[ ! -f "${HMAC_KEY}" ]]; then
  echo "==> generating HMAC key"
  ( cd "${SAFE_CWD}" && sudo -u donna-broker sh -c "openssl rand 32 > '${HMAC_KEY}'" )
  chmod 0400 "${HMAC_KEY}"
  chown donna-broker:donna-bridge "${HMAC_KEY}"
else
  echo "==> HMAC key already exists (keeping it)"
fi

# ---- 8. install wrapper ----
WRAPPER="/usr/local/bin/donna-broker"
echo "==> installing ${WRAPPER}"
install -m 0755 -o root -g wheel "${REPO_ROOT}/ops/donna-broker.sh" "${WRAPPER}"

# ---- 9. install sudoers fragment ----
# Spec §17 wrote the sudoers line as
#   grahamwilliamson ALL=(donna-broker) NOPASSWD: CLOSEFROM=3: /usr/local/bin/donna-broker
# but CLOSEFROM is a sudo Defaults setting, not a per-command tag —
# visudo rejects that exact form. The standard sudoers way is:
#   - Defaults!<command> closefrom=3       (per-command default)
#   - <user> <host>=(<runas>) NOPASSWD: <command>
# sudo's default closefrom is already 3, so the Defaults line is
# documentation/explicit-intent rather than a behaviour change.
# fd inheritance is also blocked by the wrapper's `env -i` + `exec`.
SUDOERS_FILE="/etc/sudoers.d/donna-broker"
if [[ ! -f "${SUDOERS_FILE}" ]]; then
  echo "==> installing sudoers rule at ${SUDOERS_FILE}"
  cat > "${SUDOERS_FILE}" <<SUDOERS
# Managed by ops/setup-donna-broker.sh — do not edit by hand.
# Spec: security-v1.1 §17 Phase 1 sudoers rule.
Defaults!/usr/local/bin/donna-broker closefrom=3
${GRAHAM_USER} ALL=(donna-broker) NOPASSWD: /usr/local/bin/donna-broker
SUDOERS
  chmod 0440 "${SUDOERS_FILE}"
  visudo -cf "${SUDOERS_FILE}" || { echo "sudoers validation failed!" >&2; rm "${SUDOERS_FILE}"; exit 1; }
else
  echo "==> sudoers fragment already exists"
fi

# ---- 10. Time Machine exclusions (§24.2) ----
echo "==> adding Time Machine exclusions"
for p in \
  "${CONFIG}/secrets" \
  "${CONFIG}/hmac.key" \
  "${CONFIG}/requests.db" \
  "${BROKER_HOME}/audit"; do
  if [[ -e "${p}" ]]; then
    tmutil addexclusion "${p}" 2>/dev/null || true
  fi
done

# Belt-and-braces xattr on the HMAC key.
xattr -w com.apple.metadata:com_apple_backup_excludeItem \
  "com.apple.backupd" "${HMAC_KEY}" 2>/dev/null || true

# ---- 11. smoke test ----
echo ""
echo "==> smoke test: donna-broker verify-audit"
# cd ${SAFE_CWD} avoids the cwd leak — the wrapper itself sanitises env
# but the outer sudo -u graham invocation can carry graham's cwd which
# the broker may not need but warns noisily about.
( cd "${SAFE_CWD}" && sudo -u "${GRAHAM_USER}" /usr/local/bin/donna-broker verify-audit < /dev/null ) || {
  echo "SMOKE TEST FAILED" >&2
  exit 1
}

echo ""
echo "========================================"
echo "Setup complete."
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Edit ${CONFIG}/capabilities.yaml if you want to tune limits."
echo "  2. Edit ${CONFIG}/mcp-tools.yaml if a new MCP tool needs classification."
echo "  3. Run ops/install-launchd.sh to register audit verification cron."
echo "  4. Run the Telegram server extension (see ops/PHASE_1_DEPLOY.md §5)."
echo "  5. Follow ops/PHASE_1_GATE.md to validate end-to-end before enabling."
echo ""
echo "To UNDO this install (remove user/group/dirs):"
echo "  sudo bash ops/teardown-donna-broker.sh"
