# Manual usage — rtl433_china_scan.sh & rtl433_forward.sh

For the recommended systemd + curl-pipe install, see [README.md](README.md).
This page covers running the scripts by hand.

## rtl433_china_scan.sh

Scans common China ISM frequencies (315M, 433.92M, 868.3M) with `rtl_433` and
appends decoded events as newline-delimited JSON to a single fixed file.

Requirements: `rtl_433` installed and an RTL-SDR dongle plugged in.

Run in the foreground:

```bash
chmod +x rtl433_china_scan.sh
./rtl433_china_scan.sh
```

Run in the background (survives closing the terminal):

```bash
nohup ./rtl433_china_scan.sh > ~/rtl433_scan.out 2>&1 &
```

### Options (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `NODE_NAME` | hostname | Logical node name (startup output only) |
| `HOP_SECONDS` | `180` | Frequency hop interval, seconds |
| `RTL433_RAW_FILE` | `~/rtl433_raw.json` | Fixed JSON output path |
| `RUNTIME_LOG` | `~/rtl433_runtime.log` | rtl_433 stderr/runtime log |

Example:

```bash
HOP_SECONDS=120 RTL433_RAW_FILE=~/data/rtl433_raw.json ./rtl433_china_scan.sh
```

## rtl433_forward.sh

Tails the raw JSON file and forwards new lines to a remote TCP collector via
`socat`, retrying forever if the connection drops.

Requirements: `socat` installed.

```bash
chmod +x rtl433_forward.sh
FORWARD_HOST=192.168.1.100 FORWARD_PORT=9000 ./rtl433_forward.sh
```

### Options (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `RTL433_RAW_FILE` | `~/rtl433_raw.json` | File to tail (match the scanner) |
| `FORWARD_HOST` | `192.168.1.100` | Collector host |
| `FORWARD_PORT` | `9000` | Collector TCP port |
| `CONNECT_TIMEOUT` | `5` | socat connect timeout, seconds |
| `RETRY_SECONDS` | `5` | Wait between reconnect attempts |

## Receiving data on the collector

The forwarder connects out to `FORWARD_HOST:FORWARD_PORT`, so the remote
machine needs something listening on that TCP port to accept the stream. The
simplest receiver is `socat` writing everything it gets to a file:

```bash
socat TCP-LISTEN:9000,reuseaddr,fork - >> rtl433_received.json
```

What the options mean:

- `TCP-LISTEN:9000` — listen on TCP port 9000 (match `FORWARD_PORT`).
- `reuseaddr` — allow rebinding the port immediately after a restart.
- `fork` — handle each incoming connection in its own child, so the listener
  stays up when the Pi reconnects (the forwarder reconnects after drops).
- `- >> rtl433_received.json` — copy the received bytes to stdout, which the
  shell appends to `rtl433_received.json`.

Notes:

- Run it from the directory where you want the file, or use an absolute path
  like `>> /var/log/rtl433/rtl433_received.json`.
- Because `fork` spawns a child per connection, concurrent connections could
  interleave lines in the file. With a single Pi that's not an issue; for many
  senders, write to per-connection files or use a real collector.
- Open the port in the collector's firewall (e.g. `sudo ufw allow 9000/tcp`).

Quick end-to-end test without a dongle — start the receiver above, then from
the Pi:

```bash
echo '{"test":"hello"}' | socat - TCP:192.168.1.100:9000,connect-timeout=5
```

The line should land in `rtl433_received.json` on the collector.

## Manage processes

If you installed via `install.sh`, the scanner and forwarder run as systemd
services — use `systemctl`, not `pkill`:

```bash
# status
systemctl status rtl433-scan rtl433-forward

# live logs
journalctl -u rtl433-scan -f
journalctl -u rtl433-forward -f

# start / stop / restart
sudo systemctl start   rtl433-scan rtl433-forward
sudo systemctl stop    rtl433-scan rtl433-forward
sudo systemctl restart rtl433-scan rtl433-forward
```

Only when running the scripts **by hand** (foreground or `nohup`, no services)
manage them as plain processes:

```bash
ps aux | grep -E 'rtl_433|socat'   # check they're running
pkill -f rtl_433                   # stop the scanner
pkill -f rtl433_forward.sh         # stop the forwarder
```
