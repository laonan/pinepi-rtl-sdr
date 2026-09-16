#!/usr/bin/env bash
set -Eeuo pipefail

#
# rtl_433 China ISM-band scanner.
#
# Writes decoded events as newline-delimited JSON to a single fixed file so
# that:
#   - a forwarder can `tail -F` it, and
#   - logrotate can rotate it with `copytruncate`.
#
# Configuration (all optional, via environment):
#   NODE_NAME        logical node name          (default: hostname)
#   HOP_SECONDS      frequency hop interval      (default: 180)
#   RTL433_RAW_FILE  fixed JSON output path      (default: $HOME/rtl433_raw.json)
#   RUNTIME_LOG      rtl_433 stderr/runtime log  (default: $HOME/rtl433_runtime.log)
#

NODE_NAME="${NODE_NAME:-$(hostname)}"
HOP_SECONDS="${HOP_SECONDS:-180}"

RAW_FILE="${RTL433_RAW_FILE:-${HOME}/rtl433_raw.json}"
RUNTIME_LOG="${RUNTIME_LOG:-${HOME}/rtl433_runtime.log}"

mkdir -p "$(dirname "$RAW_FILE")" "$(dirname "$RUNTIME_LOG")"
# Make sure the file exists so `tail -F` and logrotate have something to open.
touch "$RAW_FILE"

FREQS=(
  315M
  433.92M
  868.3M
)

ARGS=()
for freq in "${FREQS[@]}"; do
  ARGS+=(-f "$freq")
done

echo "Node         : $NODE_NAME"
echo "Region       : china"
echo "Started      : $(date)"
echo "Hop interval : ${HOP_SECONDS}s"
echo "Frequencies  : ${FREQS[*]}"
echo "Raw JSON     : $RAW_FILE"
echo "Runtime log  : $RUNTIME_LOG"

# Notes:
#   -F json:<file>   append newline-delimited JSON events to the fixed file.
#                    With logrotate `copytruncate`, rtl_433 keeps writing to
#                    the same descriptor after the file is truncated in place.
exec rtl_433 \
  "${ARGS[@]}" \
  -H "$HOP_SECONDS" \
  -Y autolevel \
  -M time:iso \
  -M level \
  -M noise \
  -M stats:600 \
  -F "json:${RAW_FILE}" \
  2>>"$RUNTIME_LOG"
