#!/usr/bin/env bash
set -Eeuo pipefail

#
# pinepi-rtl-sdr one-shot installer.
#
# Intended for curl-pipe-bash use on a Raspberry Pi (Debian/Raspberry Pi OS):
#
#     curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh | sudo bash
#
# What it does:
#   1. Builds/installs rtl-sdr + rtl-433  (via install_rtlsdr.sh)
#   2. Installs socat
#   3. Installs the scanner + forwarder scripts to /opt/pinepi-rtl-sdr
#   4. Installs config to /etc/rtl433/rtl433.env
#   5. Installs logrotate rules to /etc/logrotate.d/rtl433
#   6. Installs + enables + starts two systemd services:
#        rtl433-scan.service      (rtl_433 -> fixed raw JSON file)
#        rtl433-forward.service   (tail -F | socat -> TCP collector)
#
# Configuration (via environment):
#   REPO_RAW_BASE   raw file base URL of the repo
#                   default: https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main
#   TARGET_USER     user that owns the raw file / runs the services
#                   default: SUDO_USER, else the invoking user
#   FORWARD_HOST    remote collector host (default: 192.168.1.100)
#   FORWARD_PORT    remote collector port (default: 9000)
#   NODE_NAME       logical node name     (default: hostname)
#   HOP_SECONDS     hop interval seconds  (default: 180)
#   SKIP_SDR_BUILD  set to 1 to skip install_rtlsdr.sh (e.g. already installed)
#

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

REPO_RAW_BASE="${REPO_RAW_BASE:-https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main}"

INSTALL_DIR="${INSTALL_DIR:-/opt/pinepi-rtl-sdr}"
CONFIG_DIR="/etc/rtl433"
ENV_FILE="${CONFIG_DIR}/rtl433.env"
LOGROTATE_FILE="/etc/logrotate.d/rtl433"
SYSTEMD_DIR="/etc/systemd/system"

TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(id -un)}}"

FORWARD_HOST="${FORWARD_HOST:-192.168.1.100}"
FORWARD_PORT="${FORWARD_PORT:-9000}"
NODE_NAME="${NODE_NAME:-$(hostname)}"
HOP_SECONDS="${HOP_SECONDS:-180}"
SKIP_SDR_BUILD="${SKIP_SDR_BUILD:-0}"


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

log() { echo; echo "[install] $*"; }
die() { echo; echo "[install][ERROR] $*" >&2; exit 1; }

on_error() {
    local code=$?
    echo
    echo "============================================================"
    echo "[install][ERROR] Installation failed"
    echo "Exit code : ${code}"
    echo "Line      : ${BASH_LINENO[0]}"
    echo "Command   : ${BASH_COMMAND}"
    echo "============================================================"
    exit "${code}"
}
trap on_error ERR

# fetch <relative-path> <destination>
fetch() {
    local rel="$1" dest="$2"
    log "Downloading ${rel}"
    curl -fsSL "${REPO_RAW_BASE}/${rel}" -o "$dest" \
        || die "Failed to download ${REPO_RAW_BASE}/${rel}"
}


# ------------------------------------------------------------
# Preflight
# ------------------------------------------------------------

if [[ "${EUID}" -ne 0 ]]; then
    die "Please run as root, e.g.:
    curl -sSL ${REPO_RAW_BASE}/install.sh | sudo bash"
fi

command -v apt-get >/dev/null 2>&1 \
    || die "This installer supports Debian/Ubuntu/Raspberry Pi OS (apt) only."

command -v curl >/dev/null 2>&1 \
    || die "curl is required but not installed."

TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6 || true)"
[[ -n "${TARGET_HOME}" ]] \
    || die "Could not resolve home directory for user '${TARGET_USER}'. Set TARGET_USER=..."

RAW_FILE="${TARGET_HOME}/rtl433_raw.json"
RUNTIME_LOG="${TARGET_HOME}/rtl433_runtime.log"

log "Configuration"
echo "Repo base    : ${REPO_RAW_BASE}"
echo "Install dir  : ${INSTALL_DIR}"
echo "Target user  : ${TARGET_USER}"
echo "Home         : ${TARGET_HOME}"
echo "Raw file     : ${RAW_FILE}"
echo "Node name    : ${NODE_NAME}"
echo "Forward to   : ${FORWARD_HOST}:${FORWARD_PORT}"
echo "Hop interval : ${HOP_SECONDS}s"


# ------------------------------------------------------------
# Working directory for downloads
# ------------------------------------------------------------

WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "${WORK_DIR}"; }
trap 'cleanup' EXIT

fetch "install_rtlsdr.sh"          "${WORK_DIR}/install_rtlsdr.sh"
fetch "rtl433_china_scan.sh"       "${WORK_DIR}/rtl433_china_scan.sh"
fetch "rtl433_forward.sh"          "${WORK_DIR}/rtl433_forward.sh"
fetch "config/rtl433.env"          "${WORK_DIR}/rtl433.env"
fetch "config/logrotate-rtl433"    "${WORK_DIR}/logrotate-rtl433"
fetch "systemd/rtl433-scan.service"    "${WORK_DIR}/rtl433-scan.service"
fetch "systemd/rtl433-forward.service" "${WORK_DIR}/rtl433-forward.service"


# ------------------------------------------------------------
# 1. Build/install rtl-sdr + rtl-433
# ------------------------------------------------------------

if [[ "${SKIP_SDR_BUILD}" == "1" ]]; then
    log "Skipping rtl-sdr/rtl-433 build (SKIP_SDR_BUILD=1)"
    command -v rtl_433 >/dev/null 2>&1 \
        || die "rtl_433 not found and SKIP_SDR_BUILD=1."
else
    log "Running rtl-sdr / rtl-433 installer"
    chmod +x "${WORK_DIR}/install_rtlsdr.sh"
    TARGET_USER="${TARGET_USER}" bash "${WORK_DIR}/install_rtlsdr.sh"
fi


# ------------------------------------------------------------
# 2. Install socat (needed by the forwarder)
# ------------------------------------------------------------

log "Installing socat"
DEBIAN_FRONTEND=noninteractive apt-get install -y socat


# ------------------------------------------------------------
# 3. Install scripts
# ------------------------------------------------------------

log "Installing scripts to ${INSTALL_DIR}"
install -d -m 0755 "${INSTALL_DIR}"
install -m 0755 "${WORK_DIR}/rtl433_china_scan.sh" "${INSTALL_DIR}/rtl433_china_scan.sh"
install -m 0755 "${WORK_DIR}/rtl433_forward.sh"    "${INSTALL_DIR}/rtl433_forward.sh"


# ------------------------------------------------------------
# 4. Install config (do not clobber an existing env file)
# ------------------------------------------------------------

log "Installing configuration"
install -d -m 0755 "${CONFIG_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
    echo "Keeping existing ${ENV_FILE} (not overwritten)."
else
    # Render the env template with resolved values.
    sed \
        -e "s#^NODE_NAME=.*#NODE_NAME=${NODE_NAME}#" \
        -e "s#^RTL433_RAW_FILE=.*#RTL433_RAW_FILE=${RAW_FILE}#" \
        -e "s#^RUNTIME_LOG=.*#RUNTIME_LOG=${RUNTIME_LOG}#" \
        -e "s#^HOP_SECONDS=.*#HOP_SECONDS=${HOP_SECONDS}#" \
        -e "s#^FORWARD_HOST=.*#FORWARD_HOST=${FORWARD_HOST}#" \
        -e "s#^FORWARD_PORT=.*#FORWARD_PORT=${FORWARD_PORT}#" \
        "${WORK_DIR}/rtl433.env" > "${ENV_FILE}"
    chmod 0644 "${ENV_FILE}"
    echo "Wrote ${ENV_FILE}"
fi


# ------------------------------------------------------------
# 5. Seed the raw file (owned by target user) + logrotate
# ------------------------------------------------------------

log "Preparing raw JSON file and logrotate"
touch "${RAW_FILE}" "${RUNTIME_LOG}"
chown "${TARGET_USER}:${TARGET_USER}" "${RAW_FILE}" "${RUNTIME_LOG}"

sed "s#__RTL433_RAW_FILE__#${RAW_FILE}#g" \
    "${WORK_DIR}/logrotate-rtl433" > "${LOGROTATE_FILE}"
chmod 0644 "${LOGROTATE_FILE}"
echo "Wrote ${LOGROTATE_FILE}"

# Validate the logrotate config if the tool is available.
if command -v logrotate >/dev/null 2>&1; then
    logrotate --debug "${LOGROTATE_FILE}" >/dev/null 2>&1 \
        && echo "logrotate config OK" \
        || echo "WARNING: logrotate reported issues with ${LOGROTATE_FILE}"
fi


# ------------------------------------------------------------
# 6. Install systemd units (substitute tokens) + start
# ------------------------------------------------------------

log "Installing systemd services"
for unit in rtl433-scan.service rtl433-forward.service; do
    sed \
        -e "s#__USER__#${TARGET_USER}#g" \
        -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
        "${WORK_DIR}/${unit}" > "${SYSTEMD_DIR}/${unit}"
    chmod 0644 "${SYSTEMD_DIR}/${unit}"
    echo "Wrote ${SYSTEMD_DIR}/${unit}"
done

systemctl daemon-reload
# enable at boot, then restart so a re-install always picks up updated
# scripts/units even when the services were already running.
systemctl enable rtl433-scan.service rtl433-forward.service
systemctl restart rtl433-scan.service rtl433-forward.service


# ------------------------------------------------------------
# Done
# ------------------------------------------------------------

log "Installation complete"
echo
echo "============================================================"
echo "pinepi-rtl-sdr installed."
echo "============================================================"
echo
echo "Config     : ${ENV_FILE}"
echo "Raw JSON   : ${RAW_FILE}"
echo "Logrotate  : ${LOGROTATE_FILE}"
echo "Forward to : ${FORWARD_HOST}:${FORWARD_PORT}"
echo
echo "Check status:"
echo "  systemctl status rtl433-scan rtl433-forward"
echo "  journalctl -u rtl433-scan -f"
echo "  journalctl -u rtl433-forward -f"
echo
echo "After editing ${ENV_FILE}:"
echo "  sudo systemctl restart rtl433-scan rtl433-forward"
echo

exit 0
