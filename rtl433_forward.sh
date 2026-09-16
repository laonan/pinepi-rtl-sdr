#!/usr/bin/env bash
set -Eeuo pipefail

#
# Forward the rtl_433 raw JSON stream to a remote collector over TCP.
#
# Continuously tails the fixed raw JSON file and pipes new lines to a remote
# host:port using socat. If the connection drops or the collector is
# unreachable, it waits briefly and retries forever.
#
# Configuration (via environment, normally set in /etc/rtl433/rtl433.env):
#   RTL433_RAW_FILE   file to tail        (default: $HOME/rtl433_raw.json)
#   FORWARD_HOST      collector host      (default: 192.168.1.100)
#   FORWARD_PORT      collector TCP port  (default: 9000)
#   CONNECT_TIMEOUT   socat connect timeout seconds (default: 5)
#   RETRY_SECONDS     wait between reconnect attempts (default: 5)
#

RAW_FILE="${RTL433_RAW_FILE:-${HOME}/rtl433_raw.json}"
FORWARD_HOST="${FORWARD_HOST:-192.168.1.100}"
FORWARD_PORT="${FORWARD_PORT:-9000}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-5}"
RETRY_SECONDS="${RETRY_SECONDS:-5}"

echo "Forwarding : $RAW_FILE -> ${FORWARD_HOST}:${FORWARD_PORT}"
echo "Retry      : ${RETRY_SECONDS}s (connect timeout ${CONNECT_TIMEOUT}s)"

# Ensure the file exists so tail -F has something to follow immediately.
mkdir -p "$(dirname "$RAW_FILE")"
touch "$RAW_FILE"

while true; do
  # tail -F keeps following across truncation/rotation (copytruncate).
  # If socat exits (connection lost / timeout), the pipe closes and we retry.
  tail -F "$RAW_FILE" \
    | socat - "TCP:${FORWARD_HOST}:${FORWARD_PORT},connect-timeout=${CONNECT_TIMEOUT}" \
    || true

  sleep "$RETRY_SECONDS"
done
