#!/usr/bin/env bash

#
# RTL-SDR / RTL-SDR Blog V4 installer
# For Debian / Raspberry Pi OS / Ubuntu
#
# Features:
#   - installs build dependencies
#   - optionally removes old distro rtl-sdr packages
#   - builds latest Osmocom rtl-sdr from source
#   - installs udev rules
#   - adds user to plugdev
#   - blacklists DVB-TV kernel drivers
#   - enables automatic kernel-driver detach
#   - safe to run repeatedly
#   - suitable for nohup/background execution
#

set -Eeuo pipefail
IFS=$'\n\t'

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

REPO_URL="${REPO_URL:-https://github.com/osmocom/rtl-sdr.git}"
SRC_DIR="${SRC_DIR:-/usr/local/src/rtl-sdr}"

# Remove Debian/Raspberry Pi OS packaged rtl-sdr libraries first.
# Set PURGE_OLD=0 if you explicitly want to keep them.
PURGE_OLD="${PURGE_OLD:-1}"

# Default compile jobs.
# Pi Zero WH will normally return 1 here.
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 1)}"

LOG_FILE="${LOG_FILE:-/var/log/install-rtlsdr.log}"

# Do NOT automatically reboot by default.
AUTO_REBOOT="${AUTO_REBOOT:-0}"

# User who should be allowed to access SDR without root.
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

log() {
    echo
    echo "[$(timestamp)] $*"
}

die() {
    echo
    echo "[ERROR] $*" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    echo
    echo "============================================================"
    echo "[ERROR] Installation failed"
    echo "Exit code : ${exit_code}"
    echo "Line      : ${BASH_LINENO[0]}"
    echo "Command   : ${BASH_COMMAND}"
    echo "Log       : ${LOG_FILE}"
    echo "============================================================"
    exit "${exit_code}"
}

trap on_error ERR


# ------------------------------------------------------------
# Root check
# ------------------------------------------------------------

if [[ "${EUID}" -ne 0 ]]; then
    die "Please run this script with sudo:
sudo ./install_rtlsdr.sh"
fi


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

mkdir -p "$(dirname "${LOG_FILE}")"
touch "${LOG_FILE}"

exec > >(tee -a "${LOG_FILE}") 2>&1

log "Starting RTL-SDR installation"

echo "Repository : ${REPO_URL}"
echo "Source dir : ${SRC_DIR}"
echo "Jobs       : ${JOBS}"
echo "Purge old  : ${PURGE_OLD}"
echo "Target user: ${TARGET_USER:-<not detected>}"
echo "Log file   : ${LOG_FILE}"


# ------------------------------------------------------------
# Platform check
# ------------------------------------------------------------

if ! command -v apt-get >/dev/null 2>&1; then
    die "This installer currently supports Debian/Ubuntu/Raspberry Pi OS systems using apt."
fi

if [[ -f /etc/os-release ]]; then
    log "Operating system"
    cat /etc/os-release
fi

log "Architecture"
uname -a


# ------------------------------------------------------------
# Install dependencies
# ------------------------------------------------------------

log "Updating apt package metadata"

apt-get \
    -o Acquire::Retries=3 \
    update

log "Installing dependencies"

DEBIAN_FRONTEND=noninteractive \
apt-get install -y \
    git \
    cmake \
    pkg-config \
    build-essential \
    libusb-1.0-0-dev \
    usbutils \
    ca-certificates


# ------------------------------------------------------------
# Remove old distro packages
# ------------------------------------------------------------

if [[ "${PURGE_OLD}" == "1" ]]; then

    log "Checking for old packaged RTL-SDR drivers"

    mapfile -t OLD_PACKAGES < <(
        dpkg-query \
            -W \
            -f='${binary:Package} ${db:Status-Abbrev}\n' \
            2>/dev/null |
        awk '
            $2 ~ /^ii/ &&
            $1 ~ /^(rtl-sdr|librtlsdr)/ {
                print $1
            }
        ' || true
    )

    if (( ${#OLD_PACKAGES[@]} > 0 )); then

        echo "Removing:"
        printf '  %s\n' "${OLD_PACKAGES[@]}"

        DEBIAN_FRONTEND=noninteractive \
        apt-get purge -y "${OLD_PACKAGES[@]}"

    else
        echo "No installed distro RTL-SDR packages found."
    fi

else
    log "Skipping old package removal (PURGE_OLD=0)"
fi


# ------------------------------------------------------------
# Remove stale /usr/local source installations
# ------------------------------------------------------------

log "Cleaning possible stale /usr/local RTL-SDR installation"

rm -f /usr/local/bin/rtl_adc
rm -f /usr/local/bin/rtl_adsb
rm -f /usr/local/bin/rtl_biast
rm -f /usr/local/bin/rtl_eeprom
rm -f /usr/local/bin/rtl_fm
rm -f /usr/local/bin/rtl_power
rm -f /usr/local/bin/rtl_power_fftw
rm -f /usr/local/bin/rtl_sdr
rm -f /usr/local/bin/rtl_tcp
rm -f /usr/local/bin/rtl_test

rm -f /usr/local/lib/librtlsdr.so*
rm -f /usr/local/lib/librtlsdr.a

rm -f /usr/local/include/rtl-sdr.h
rm -f /usr/local/include/rtl-sdr_export.h

ldconfig || true


# ------------------------------------------------------------
# Download fresh source tree
# ------------------------------------------------------------

log "Downloading latest Osmocom rtl-sdr"

mkdir -p "$(dirname "${SRC_DIR}")"

# A clean clone avoids previous failed CMake/build state.
rm -rf "${SRC_DIR}"

git clone \
    --depth 1 \
    "${REPO_URL}" \
    "${SRC_DIR}"


# ------------------------------------------------------------
# Configure build
# ------------------------------------------------------------

log "Configuring rtl-sdr"

cmake \
    -S "${SRC_DIR}" \
    -B "${SRC_DIR}/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DINSTALL_UDEV_RULES=ON \
    -DDETACH_KERNEL_DRIVER=ON


# ------------------------------------------------------------
# Compile
# ------------------------------------------------------------

log "Compiling rtl-sdr with ${JOBS} job(s)"

cmake \
    --build "${SRC_DIR}/build" \
    --parallel "${JOBS}"


# ------------------------------------------------------------
# Install
# ------------------------------------------------------------

log "Installing rtl-sdr"

cmake --install "${SRC_DIR}/build"

# Belt-and-suspenders install of current rules.
install \
    -m 0644 \
    "${SRC_DIR}/rtl-sdr.rules" \
    /etc/udev/rules.d/rtl-sdr.rules

ldconfig


# ------------------------------------------------------------
# Install rtl-433 decoder
# ------------------------------------------------------------

log "Installing rtl-433"

DEBIAN_FRONTEND=noninteractive \
apt-get install -y \
    rtl-433


# ------------------------------------------------------------
# plugdev access
# ------------------------------------------------------------

log "Configuring SDR device permissions"

if ! getent group plugdev >/dev/null 2>&1; then
    echo "Creating plugdev group"
    groupadd --system plugdev
fi

if [[ -n "${TARGET_USER}" ]] &&
   id "${TARGET_USER}" >/dev/null 2>&1; then

    usermod -aG plugdev "${TARGET_USER}"

    echo "Added ${TARGET_USER} to plugdev group."

else
    echo "Could not automatically determine normal user."
    echo "If required later:"
    echo
    echo "    sudo usermod -aG plugdev YOUR_USERNAME"
    echo
fi


# ------------------------------------------------------------
# Blacklist DVB kernel drivers
# ------------------------------------------------------------

log "Disabling DVB-TV kernel drivers"

cat > /etc/modprobe.d/blacklist-rtl-sdr-dvb.conf <<'EOF'
#
# Dedicated RTL-SDR receiver
#
# Prevent Linux DVB drivers from claiming RTL2832U devices.
#

blacklist dvb_usb_rtl28xxu
blacklist rtl2832
EOF

echo "Created:"
echo "  /etc/modprobe.d/blacklist-rtl-sdr-dvb.conf"


# ------------------------------------------------------------
# Reload udev
# ------------------------------------------------------------

log "Reloading udev rules"

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb || true


# ------------------------------------------------------------
# Try unloading DVB drivers now
# ------------------------------------------------------------

log "Checking currently loaded DVB drivers"

NEED_REBOOT=0

if lsmod | grep -q '^dvb_usb_rtl28xxu'; then

    echo "dvb_usb_rtl28xxu is currently loaded."

    if modprobe -r dvb_usb_rtl28xxu 2>/dev/null; then
        echo "Successfully unloaded dvb_usb_rtl28xxu."
    else
        echo "Could not unload dvb_usb_rtl28xxu."
        NEED_REBOOT=1
    fi

else
    echo "dvb_usb_rtl28xxu is not loaded."
fi


if lsmod | grep -q '^rtl2832'; then

    echo "rtl2832 is currently loaded."

    if modprobe -r rtl2832 2>/dev/null; then
        echo "Successfully unloaded rtl2832."
    else
        echo "Could not unload rtl2832."
        NEED_REBOOT=1
    fi

fi


# ------------------------------------------------------------
# Verify installed commands
# ------------------------------------------------------------

log "Checking installed binaries"

for CMD in rtl_test rtl_tcp rtl_fm rtl_sdr rtl_eeprom rtl_433; do

    if command -v "${CMD}" >/dev/null 2>&1; then
        printf "%-12s %s\n" "${CMD}" "$(command -v "${CMD}")"
    else
        echo "WARNING: ${CMD} not found"
    fi

done


# ------------------------------------------------------------
# USB check
# ------------------------------------------------------------

log "Looking for RTL2832 USB device"

if lsusb | grep -qiE '0bda:(2832|2838)'; then

    echo "RTL2832 USB device detected:"
    lsusb | grep -iE '0bda:(2832|2838)'

else

    echo "No 0bda:2832/2838 RTL2832 device currently detected."
    echo "This does not affect driver installation."
fi


# ------------------------------------------------------------
# Diagnostic test
# ------------------------------------------------------------

log "Running short RTL-SDR diagnostic"

if command -v rtl_test >/dev/null 2>&1; then

    echo
    echo "----- rtl_test output -----"
    echo

    # rtl_test -t may return a non-zero status on some tuner types,
    # so its exit status is intentionally not treated as installation failure.
    timeout 15 rtl_test -t || true

    echo
    echo "----- end rtl_test output -----"
    echo

fi


# ------------------------------------------------------------
# Finish
# ------------------------------------------------------------

log "Installation completed"

echo
echo "============================================================"
echo "RTL-SDR installation finished."
echo "============================================================"
echo
echo "Log:"
echo "  ${LOG_FILE}"
echo

if [[ "${NEED_REBOOT}" == "1" ]]; then

    echo "IMPORTANT:"
    echo "  DVB kernel driver could not be unloaded."
    echo "  Reboot is recommended:"
    echo
    echo "      sudo reboot"
    echo

elif lsmod | grep -qE '^(dvb_usb_rtl28xxu|rtl2832)'; then

    echo "A DVB driver is still loaded."
    echo "Reboot is recommended:"
    echo
    echo "    sudo reboot"
    echo

else

    echo "No conflicting RTL DVB driver appears to be loaded."
    echo
    echo "You can try:"
    echo
    echo "    rtl_test -t"
    echo

fi


if [[ "${AUTO_REBOOT}" == "1" ]]; then

    echo "AUTO_REBOOT=1 -- rebooting in 5 seconds..."
    sleep 5
    reboot

fi

exit 0
