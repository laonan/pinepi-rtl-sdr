# pinepi-rtl-sdr

Turn a Raspberry Pi + RTL-SDR dongle into an always-on rtl_433 receiver that scans China ISM bands, logs decoded events as newline-delimited JSON, rotates the log daily, and forwards the stream to a remote TCP collector.

It installs as two systemd services:

- `rtl433-scan.service` — runs `rtl_433` and writes decoded JSON to a fixed file.
- `rtl433-forward.service` — `tail -F` the JSON file and pipes it to a remote host with `socat`, reconnecting forever.

## Quick install (curl | bash)

On the Raspberry Pi (Debian / Raspberry Pi OS):

```bash
curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh | sudo bash
```

Set the collector target and other options inline:

```bash
curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh \
  | sudo FORWARD_HOST=192.168.1.100 FORWARD_PORT=9000 NODE_NAME=pi-livingroom bash
```

> If your default branch isn't `main`, update the branch segment of the URL.

### What the installer does

1. Builds and installs `rtl-sdr` (Osmocom, from source) + `rtl-433` via `install_rtlsdr.sh`.
2. Installs `socat`.
3. Copies `rtl433_china_scan.sh` and `rtl433_forward.sh` to `/opt/pinepi-rtl-sdr`.
4. Writes config to `/etc/rtl433/rtl433.env` (existing file is preserved).
5. Writes logrotate rules to `/etc/logrotate.d/rtl433`.
6. Enables and (re)starts both systemd services.

### Installer options (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `REPO_RAW_BASE` | `https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main` | Where files are fetched from |
| `TARGET_USER` | `$SUDO_USER` | User that owns the raw file and runs the services |
| `FORWARD_HOST` | `192.168.1.100` | Remote collector host |
| `FORWARD_PORT` | `9000` | Remote collector TCP port |
| `NODE_NAME` | hostname | Logical node name |
| `HOP_SECONDS` | `180` | Frequency hop interval |
| `INSTALL_DIR` | `/opt/pinepi-rtl-sdr` | Where scripts are installed |
| `SKIP_SDR_BUILD` | `0` | Set `1` to skip the source build (rtl_433 already installed) |

## Configuration

Edit `/etc/rtl433/rtl433.env`, then restart:

```bash
sudo nano /etc/rtl433/rtl433.env
sudo systemctl restart rtl433-scan rtl433-forward
```

Key settings: `RTL433_RAW_FILE` (must match the path in `/etc/logrotate.d/rtl433`), `FORWARD_HOST`, `FORWARD_PORT`, `HOP_SECONDS`.

## Updating / re-installing

Re-running the installer pulls the latest scripts, systemd units, and logrotate
rules from GitHub, overwrites them, and **restarts** both services so the new
code takes effect immediately:

```bash
curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh | sudo bash
```

One deliberate exception: an existing `/etc/rtl433/rtl433.env` is **never
overwritten**, so your settings are preserved across re-installs. That also
means a re-install will not fix a bad config value. To reset config to the
current defaults, remove it first, then re-run:

```bash
sudo rm /etc/rtl433/rtl433.env
curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh | sudo bash
```

If a run aborts partway (for example, `install_rtlsdr.sh` reports the DVB kernel
driver could not be unloaded), reboot and re-run:

```bash
sudo reboot
# after reboot
curl -sSL https://raw.githubusercontent.com/laonan/pinepi-rtl-sdr/main/install.sh | sudo bash
```

## Managing the services

```bash
# status
systemctl status rtl433-scan rtl433-forward

# live logs
journalctl -u rtl433-scan -f
journalctl -u rtl433-forward -f

# stop / start
sudo systemctl stop rtl433-scan rtl433-forward
sudo systemctl start rtl433-scan rtl433-forward

# disable at boot
sudo systemctl disable rtl433-scan rtl433-forward
```

## Data flow

```
rtl_433  ->  ~/rtl433_raw.json  ->  tail -F | socat  ->  FORWARD_HOST:FORWARD_PORT
                     |
                 logrotate (daily, keep 2, copytruncate)
```

`copytruncate` lets logrotate rotate the file while `rtl_433` keeps writing to the
same descriptor, and `tail -F` keeps following across the truncation.

## Log rotation

Installed to `/etc/logrotate.d/rtl433`, rotating the raw JSON file daily, keeping
2 compressed copies. To test:

```bash
sudo logrotate --force /etc/logrotate.d/rtl433
```

## Files in this repo

| Path | Role |
|---|---|
| `install.sh` | curl-pipe entrypoint |
| `install_rtlsdr.sh` | builds rtl-sdr + installs rtl-433 |
| `rtl433_china_scan.sh` | the scanner (writes fixed JSON file) |
| `rtl433_forward.sh` | the forwarder (`tail -F \| socat` loop) |
| `config/rtl433.env` | config template -> `/etc/rtl433/rtl433.env` |
| `config/logrotate-rtl433` | logrotate template -> `/etc/logrotate.d/rtl433` |
| `systemd/rtl433-scan.service` | scan service unit |
| `systemd/rtl433-forward.service` | forward service unit |

## Manual / standalone use

You can still run the scanner by hand without the services. See [USAGE.md](USAGE.md).
