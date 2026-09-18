#!/usr/bin/env bash
set -Eeuo pipefail

#
# pinepi-rtl-sdr one-shot installer.
#
# Intended for curl-pipe-bash use on a Raspberry Pi (Debian/Raspberry Pi OS):
#
#     curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh | sudo bash
#
# Two roles (select with --role or ROLE=, default: sensor):
#
#   sensor    (default) The RTL-SDR node. Builds rtl-sdr + rtl-433, installs the
#             scanner + forwarder, and starts:
#                 rtl433-scan.service      (rtl_433 -> fixed raw JSON file)
#                 rtl433-forward.service   (tail -F | socat -> TCP collector)
#
#   collector The receiver node (NO dongle). Skips the whole SDR build. Installs
#             the Python collector into a venv and starts:
#                 rtl433-collect.service   (TCP receiver + daily heatmap push)
#             It listens on FORWARD_PORT, buckets 24h of events into a heatmap
#             PNG, and pushes it (WeCom webhook) daily at DELIVER_AT (20:30).
#
# Examples:
#     # sensor (default)
#     curl -sSL .../install.sh | sudo bash
#
#     # collector, pointing WeCom at a webhook, delivering at 20:30
#     curl -sSL .../install.sh | sudo bash -s -- --role collector
#     # (set WECOM_WEBHOOK_URL in /etc/rtl433/rtl433.env afterwards, or inline:)
#     curl -sSL .../install.sh | sudo WECOM_WEBHOOK_URL=https://qyapi... bash -s -- --role collector
#
# Configuration (via environment):
#   ROLE            sensor | collector          (default: sensor; --role wins)
#   REPO_RAW_BASE   raw file base URL of the repo
#                   default: https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main
#   TARGET_USER     user that owns files / runs the services
#                   default: SUDO_USER, else the invoking user
#   FORWARD_HOST    remote collector host (sensor)   (default: 192.168.1.100)
#   FORWARD_PORT    collector TCP port (both roles)  (default: 9000)
#   NODE_NAME       logical node name     (default: hostname)
#   HOP_SECONDS     hop interval seconds  (sensor)   (default: 180)
#   SKIP_SDR_BUILD  set to 1 to skip install_rtlsdr.sh (sensor only)
#   WECOM_WEBHOOK_URL  WeCom group-bot webhook   (collector; can set later)
#   DELIVER_AT      daily delivery HH:MM (collector) (default: 20:30)
#

# ------------------------------------------------------------
# Parse CLI args (env vars remain the fallback)
# ------------------------------------------------------------

ROLE="${ROLE:-sensor}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)
            [[ $# -ge 2 ]] || { echo "--role needs a value" >&2; exit 2; }
            ROLE="$2"; shift 2 ;;
        --role=*)
            ROLE="${1#*=}"; shift ;;
        -h|--help)
            sed -n '2,60p' "$0" 2>/dev/null || true
            exit 0 ;;
        *)
            echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$ROLE" in
    sensor|collector) ;;
    *) echo "Invalid ROLE '$ROLE' (expected: sensor | collector)" >&2; exit 2 ;;
esac


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

# Collector-only settings (harmless defaults for the sensor role).
WECOM_WEBHOOK_URL="${WECOM_WEBHOOK_URL:-}"
DELIVER_AT="${DELIVER_AT:-20:30}"
PUSH_CHANNEL="${PUSH_CHANNEL:-wecom}"


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

# Collector paths.
COLLECTOR_RAW_FILE="${TARGET_HOME}/rtl433_received.json"
SNAPSHOT_DIR="${TARGET_HOME}/rtl433_snapshots"

log "Configuration"
echo "Role         : ${ROLE}"
echo "Repo base    : ${REPO_RAW_BASE}"
echo "Install dir  : ${INSTALL_DIR}"
echo "Target user  : ${TARGET_USER}"
echo "Home         : ${TARGET_HOME}"
echo "Node name    : ${NODE_NAME}"
echo "Port         : ${FORWARD_PORT}"
if [[ "${ROLE}" == "sensor" ]]; then
    echo "Raw file     : ${RAW_FILE}"
    echo "Forward to   : ${FORWARD_HOST}:${FORWARD_PORT}"
    echo "Hop interval : ${HOP_SECONDS}s"
else
    echo "Received file: ${COLLECTOR_RAW_FILE}"
    echo "Snapshots    : ${SNAPSHOT_DIR}"
    echo "Deliver at   : ${DELIVER_AT}"
    echo "Push channel : ${PUSH_CHANNEL}"
fi


# ------------------------------------------------------------
# Working directory for downloads
# ------------------------------------------------------------

WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "${WORK_DIR}"; }
trap 'cleanup' EXIT

# Files common to both roles.
fetch "config/rtl433.env"          "${WORK_DIR}/rtl433.env"

if [[ "${ROLE}" == "sensor" ]]; then
    fetch "install_rtlsdr.sh"              "${WORK_DIR}/install_rtlsdr.sh"
    fetch "rtl433_china_scan.sh"           "${WORK_DIR}/rtl433_china_scan.sh"
    fetch "rtl433_forward.sh"              "${WORK_DIR}/rtl433_forward.sh"
    fetch "config/logrotate-rtl433"        "${WORK_DIR}/logrotate-rtl433"
    fetch "systemd/rtl433-scan.service"    "${WORK_DIR}/rtl433-scan.service"
    fetch "systemd/rtl433-forward.service" "${WORK_DIR}/rtl433-forward.service"
else
    fetch "systemd/rtl433-collect.service" "${WORK_DIR}/rtl433-collect.service"
    fetch "collector/requirements.txt"     "${WORK_DIR}/requirements.txt"
    # Collector Python package modules.
    install -d -m 0755 "${WORK_DIR}/rtl433_collector"
    for mod in __init__ __main__ config heatmap notifiers render service store weather; do
        fetch "collector/rtl433_collector/${mod}.py" \
              "${WORK_DIR}/rtl433_collector/${mod}.py"
    done
fi


# ============================================================
# SENSOR ROLE
# ============================================================
install_sensor() {
    # 1. Build/install rtl-sdr + rtl-433
    if [[ "${SKIP_SDR_BUILD}" == "1" ]]; then
        log "Skipping rtl-sdr/rtl-433 build (SKIP_SDR_BUILD=1)"
        command -v rtl_433 >/dev/null 2>&1 \
            || die "rtl_433 not found and SKIP_SDR_BUILD=1."
    else
        log "Running rtl-sdr / rtl-433 installer"
        chmod +x "${WORK_DIR}/install_rtlsdr.sh"
        TARGET_USER="${TARGET_USER}" bash "${WORK_DIR}/install_rtlsdr.sh"
    fi

    # 2. socat (forwarder dependency)
    log "Installing socat"
    DEBIAN_FRONTEND=noninteractive apt-get install -y socat

    # 3. scripts
    log "Installing scripts to ${INSTALL_DIR}"
    install -d -m 0755 "${INSTALL_DIR}"
    install -m 0755 "${WORK_DIR}/rtl433_china_scan.sh" "${INSTALL_DIR}/rtl433_china_scan.sh"
    install -m 0755 "${WORK_DIR}/rtl433_forward.sh"    "${INSTALL_DIR}/rtl433_forward.sh"

    # 4. config (do not clobber an existing env file)
    write_env_file

    # 5. raw file + logrotate
    log "Preparing raw JSON file and logrotate"
    touch "${RAW_FILE}" "${RUNTIME_LOG}"
    chown "${TARGET_USER}:${TARGET_USER}" "${RAW_FILE}" "${RUNTIME_LOG}"

    sed "s#__RTL433_RAW_FILE__#${RAW_FILE}#g" \
        "${WORK_DIR}/logrotate-rtl433" > "${LOGROTATE_FILE}"
    chmod 0644 "${LOGROTATE_FILE}"
    echo "Wrote ${LOGROTATE_FILE}"
    if command -v logrotate >/dev/null 2>&1; then
        logrotate --debug "${LOGROTATE_FILE}" >/dev/null 2>&1 \
            && echo "logrotate config OK" \
            || echo "WARNING: logrotate reported issues with ${LOGROTATE_FILE}"
    fi

    # 6. systemd units
    log "Installing systemd services"
    for unit in rtl433-scan.service rtl433-forward.service; do
        render_unit "${unit}"
    done
    systemctl daemon-reload
    systemctl enable rtl433-scan.service rtl433-forward.service
    systemctl restart rtl433-scan.service rtl433-forward.service
}


# ============================================================
# COLLECTOR ROLE
# ============================================================
install_collector() {
    # 1. Python + venv toolchain (no SDR build at all).
    log "Installing Python + venv"
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip

    # 2. Install the collector package + a venv with its deps.
    log "Installing collector package to ${INSTALL_DIR}"
    install -d -m 0755 "${INSTALL_DIR}/rtl433_collector"
    install -m 0644 "${WORK_DIR}/rtl433_collector/"*.py "${INSTALL_DIR}/rtl433_collector/"
    install -m 0644 "${WORK_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"

    log "Creating virtualenv and installing dependencies (Pillow)"
    python3 -m venv "${INSTALL_DIR}/venv"
    "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
    "${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

    # 3. config (do not clobber an existing env file)
    write_env_file

    # 4. Runtime dirs owned by the target user.
    log "Preparing collector data directories"
    touch "${COLLECTOR_RAW_FILE}"
    install -d -m 0755 -o "${TARGET_USER}" -g "${TARGET_USER}" "${SNAPSHOT_DIR}"
    chown "${TARGET_USER}:${TARGET_USER}" "${COLLECTOR_RAW_FILE}"

    # 5. systemd unit
    log "Installing systemd service"
    render_unit "rtl433-collect.service"
    systemctl daemon-reload
    systemctl enable rtl433-collect.service
    systemctl restart rtl433-collect.service
}


# ------------------------------------------------------------
# Shared: render the env template + systemd units
# ------------------------------------------------------------

write_env_file() {
    log "Installing configuration"
    install -d -m 0755 "${CONFIG_DIR}"

    if [[ -f "${ENV_FILE}" ]]; then
        echo "Keeping existing ${ENV_FILE} (not overwritten)."
        return
    fi

    # Render the env template with resolved values for BOTH roles; unused keys
    # are simply ignored by the services that don't read them.
    sed \
        -e "s#^NODE_NAME=.*#NODE_NAME=${NODE_NAME}#" \
        -e "s#^RTL433_RAW_FILE=.*#RTL433_RAW_FILE=${RAW_FILE}#" \
        -e "s#^RUNTIME_LOG=.*#RUNTIME_LOG=${RUNTIME_LOG}#" \
        -e "s#^HOP_SECONDS=.*#HOP_SECONDS=${HOP_SECONDS}#" \
        -e "s#^FORWARD_HOST=.*#FORWARD_HOST=${FORWARD_HOST}#" \
        -e "s#^FORWARD_PORT=.*#FORWARD_PORT=${FORWARD_PORT}#" \
        -e "s#^COLLECTOR_RAW_FILE=.*#COLLECTOR_RAW_FILE=${COLLECTOR_RAW_FILE}#" \
        -e "s#^SNAPSHOT_DIR=.*#SNAPSHOT_DIR=${SNAPSHOT_DIR}#" \
        -e "s#^DELIVER_AT=.*#DELIVER_AT=${DELIVER_AT}#" \
        -e "s#^PUSH_CHANNEL=.*#PUSH_CHANNEL=${PUSH_CHANNEL}#" \
        -e "s#^WECOM_WEBHOOK_URL=.*#WECOM_WEBHOOK_URL=${WECOM_WEBHOOK_URL}#" \
        "${WORK_DIR}/rtl433.env" > "${ENV_FILE}"
    chmod 0644 "${ENV_FILE}"
    echo "Wrote ${ENV_FILE}"
}

# render_unit <unit-file-name>
render_unit() {
    local unit="$1"
    sed \
        -e "s#__USER__#${TARGET_USER}#g" \
        -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
        "${WORK_DIR}/${unit}" > "${SYSTEMD_DIR}/${unit}"
    chmod 0644 "${SYSTEMD_DIR}/${unit}"
    echo "Wrote ${SYSTEMD_DIR}/${unit}"
}


# ------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------

if [[ "${ROLE}" == "sensor" ]]; then
    install_sensor
else
    install_collector
fi


# ------------------------------------------------------------
# Done
# ------------------------------------------------------------

log "Installation complete"
echo
echo "============================================================"
echo "pinepi-rtl-sdr installed  (role: ${ROLE})."
echo "============================================================"
echo
echo "Config     : ${ENV_FILE}"

if [[ "${ROLE}" == "sensor" ]]; then
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
else
    echo "Received   : ${COLLECTOR_RAW_FILE}"
    echo "Snapshots  : ${SNAPSHOT_DIR}"
    echo "Listening  : ${FORWARD_PORT}/tcp"
    echo "Deliver at : ${DELIVER_AT} (past ${WINDOW_HOURS:-24}h heatmap)"
    echo
    if [[ -z "${WECOM_WEBHOOK_URL}" && "${PUSH_CHANNEL}" == "wecom" ]]; then
        echo "NOTE: WECOM_WEBHOOK_URL is empty. Set it in ${ENV_FILE} then:"
        echo "        sudo systemctl restart rtl433-collect"
        echo
    fi
    echo "Check status:"
    echo "  systemctl status rtl433-collect"
    echo "  journalctl -u rtl433-collect -f"
    echo
    echo "Send a test snapshot now (renders + pushes immediately):"
    echo "  sudo -u ${TARGET_USER} env PYTHONPATH=${INSTALL_DIR} \$(grep -v '^#' ${ENV_FILE} | xargs) \\"
    echo "       ${INSTALL_DIR}/venv/bin/python -m rtl433_collector snapshot"
    echo
    echo "After editing ${ENV_FILE}:"
    echo "  sudo systemctl restart rtl433-collect"
fi
echo

exit 0
