"""Configuration resolved from environment variables.

Mirrors the sensor side: values are normally provided by the systemd unit via
``EnvironmentFile=/etc/rtl433/rtl433.env``. Every setting has a sensible
default so the collector also runs from a bare shell for testing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    node_name: str
    listen_host: str
    listen_port: int
    raw_file: Path
    snapshot_dir: Path
    deliver_hour: int
    deliver_minute: int
    window_hours: int
    keep_snapshots: int
    push_channel: str
    wecom_webhook_url: str
    push_timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        home = Path(os.environ.get("HOME", "/tmp"))
        deliver = os.environ.get("DELIVER_AT", "20:30")
        try:
            hh, mm = (int(x) for x in deliver.split(":", 1))
        except (ValueError, TypeError):
            hh, mm = 20, 30
        return cls(
            node_name=os.environ.get("NODE_NAME", "collector"),
            listen_host=os.environ.get("LISTEN_HOST", "0.0.0.0"),
            # Reuse the sensor's FORWARD_PORT so both ends share one setting.
            listen_port=_int("FORWARD_PORT", 9000),
            raw_file=Path(
                os.environ.get("COLLECTOR_RAW_FILE", str(home / "rtl433_received.json"))
            ),
            snapshot_dir=Path(
                os.environ.get("SNAPSHOT_DIR", str(home / "rtl433_snapshots"))
            ),
            deliver_hour=hh,
            deliver_minute=mm,
            window_hours=_int("WINDOW_HOURS", 24),
            keep_snapshots=_int("KEEP_SNAPSHOTS", 14),
            push_channel=os.environ.get("PUSH_CHANNEL", "wecom"),
            wecom_webhook_url=os.environ.get("WECOM_WEBHOOK_URL", ""),
            push_timeout=float(_int("PUSH_TIMEOUT", 10)),
        )

    def as_notifier_config(self) -> dict:
        return {
            "WECOM_WEBHOOK_URL": self.wecom_webhook_url,
            "PUSH_TIMEOUT": self.push_timeout,
        }
