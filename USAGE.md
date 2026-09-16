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

## Collector role (second Pi, no dongle)

The collector is the receiver side of the pipeline. It does **not** use an
RTL-SDR dongle or `rtl_433`. Instead it listens on the forward port for the
JSON stream the sensor Pi sends, persists it, and once a day turns the past 24h
into a heatmap image that it pushes to a chat channel.

Install it with `--role collector` (see [README.md](README.md#quick-install-curl--bash)).
The core is a small Python package, `rtl433_collector`, installed into a venv at
`/opt/pinepi-rtl-sdr/venv`.

### What it does, in order

Every day at `DELIVER_AT` (default **20:30**, local time):

1. **Roll** the received JSON file: the current file is renamed aside and a
   fresh one is opened immediately, so incoming events keep landing with no gap.
2. **Aggregate** the rolled-out data into a heatmap — one row per device
   category, one column per hour over the past 24h.
3. **Render** the heatmap to a timestamped PNG in `SNAPSHOT_DIR`.
4. **Push** the PNG through the configured channel (WeCom webhook), as a
   base64 image message.
5. **Delete** the consumed raw JSON archive — the PNG is the durable artifact,
   and the fresh file from step 1 is already collecting the next day's data.

If the push fails, the raw archive is kept so you can retry manually.

### Heatmap categories (rows)

Events are classified into these rows (first match wins), based on
`type` / `model` / frequency:

- **TPMS / Tire Pressure**
- **315 MHz Generic Remote**
- **433 MHz Home Remote**
- **868 MHz Security Protocol Match**
- **Weather / Temp-Humidity / Rain**

`rtl_433` periodic "stats" frames are ignored (they aren't real receptions).

### Configuration (in `/etc/rtl433/rtl433.env`)

| Variable | Default | Purpose |
|---|---|---|
| `FORWARD_PORT` | `9000` | TCP port the collector listens on (match the sensor) |
| `LISTEN_HOST` | `0.0.0.0` | Bind address |
| `COLLECTOR_RAW_FILE` | `~/rtl433_received.json` | Rolling received-stream file |
| `SNAPSHOT_DIR` | `~/rtl433_snapshots` | Where heatmap PNGs are written |
| `DELIVER_AT` | `20:30` | Daily delivery time, `HH:MM` local |
| `WINDOW_HOURS` | `24` | Heatmap window |
| `KEEP_SNAPSHOTS` | `14` | How many past PNGs to keep on disk |
| `PUSH_CHANNEL` | `wecom` | `wecom` or `log` (dry-run, just logs) |
| `WECOM_WEBHOOK_URL` | (empty) | WeCom group-bot webhook URL |
| `PUSH_TIMEOUT` | `10` | HTTP timeout (seconds) for the push |

After editing, restart: `sudo systemctl restart rtl433-collect`.

### WeCom webhook

Create a group bot in WeCom (企业微信) and copy its webhook URL, which looks
like `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...`. Put it in
`WECOM_WEBHOOK_URL`. The collector sends the heatmap as an `image` message
(base64 + md5 of the raw bytes) followed by a short text caption. See the
[official webhook docs](https://developer.work.weixin.qq.com/document/path/99110).

### Send a snapshot right now

To test without waiting for 20:30 — this renders and pushes immediately:

```bash
sudo -u pi env $(grep -v '^#' /etc/rtl433/rtl433.env | xargs) \
  /opt/pinepi-rtl-sdr/venv/bin/python -m rtl433_collector snapshot
```

Add `--dry-run` to render only and log the push instead of sending:

```bash
... python -m rtl433_collector snapshot --dry-run
```

### Adding another push channel

Push channels are pluggable. To add one (e.g. Telegram, Slack, e-mail):

1. Subclass `Notifier` in `collector/rtl433_collector/notifiers.py` and
   implement `send_image(self, image_bytes, *, caption="")`.
2. Register it in the `build_notifier(channel, config)` factory.
3. Set `PUSH_CHANNEL=<your-channel>` (and any keys it needs) in the env file.

The rest of the pipeline (aggregate → render → push → cleanup) is unchanged.

### Manage the service

```bash
systemctl status rtl433-collect
journalctl -u rtl433-collect -f
sudo systemctl restart rtl433-collect
```
