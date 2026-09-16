"""Daily snapshot pipeline + scheduler, and the long-running service loop.

The daily job (default 20:30 local time) does, in order:

  1. Roll the raw JSON file: the current file is renamed aside and a fresh one
     is opened, so the stream keeps flowing into the new file with no gap.
  2. Aggregate the *rolled-out* data into a past-24h heatmap.
  3. Render the heatmap to a timestamped PNG snapshot on disk.
  4. Push the snapshot through the configured notifier (WeCom, ...).
  5. Delete the consumed raw archive (the snapshot is the durable artifact).

Rolling *before* rendering guarantees we never lose events that arrive while
the image is being produced/pushed, and that "remove the used json, start a
fresh one" happens exactly as specified.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .heatmap import aggregate
from .notifiers import Notifier, NotifyError
from .render import render_png
from .store import RawStore, Receiver, iter_json_lines

log = logging.getLogger("rtl433_collector.service")


class DailySnapshot:
    """Runs one snapshot cycle: roll -> aggregate -> render -> push -> cleanup."""

    def __init__(
        self,
        store: RawStore,
        notifier: Notifier,
        *,
        snapshot_dir: Path | str,
        node_name: str = "collector",
        window_hours: int = 24,
        keep_snapshots: int = 14,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.snapshot_dir = Path(snapshot_dir)
        self.node_name = node_name
        self.window_hours = window_hours
        self.keep_snapshots = keep_snapshots

    def run_once(self, *, now: Optional[datetime] = None) -> Optional[Path]:
        """Execute one full cycle. Returns the snapshot path, or ``None``."""
        now = now or datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")

        # 1. Roll the raw file so new events land in a fresh file immediately.
        archive = self.store.roll(archive_suffix=stamp)
        if archive is None:
            log.warning("no data to snapshot for %s", stamp)

        # 2. Aggregate whatever was rolled out (empty -> a blank heatmap).
        events = iter_json_lines(archive) if archive else iter([])
        heatmap = aggregate(events, window_end=now, hours=self.window_hours)

        # 3. Render.
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot_dir / f"{self.node_name}_heatmap_{stamp}.png"
        render_png(
            heatmap,
            snapshot,
            node_name=self.node_name,
            generated_at=now,
        )
        log.info(
            "rendered %s (%d events, %d matched)",
            snapshot.name,
            heatmap.total_events,
            heatmap.classified_events,
        )

        # 4. Push.
        caption = "RTL-433 {} · {}→{} · {} events".format(
            self.node_name,
            heatmap.start.strftime("%m-%d %H:%M"),
            heatmap.end.strftime("%m-%d %H:%M"),
            heatmap.classified_events,
        )
        try:
            self.notifier.send_image_file(snapshot, caption=caption)
            log.info("pushed snapshot via %s", self.notifier.name)
        except NotifyError as exc:
            # Keep the archive so a later manual push is possible.
            log.error("push failed (%s); keeping raw archive %s", exc, archive)
            self._prune_snapshots()
            return snapshot

        # 5. Cleanup: the snapshot is the durable artifact; drop the raw data.
        if archive is not None:
            try:
                Path(archive).unlink()
                log.info("removed consumed raw archive %s", Path(archive).name)
            except OSError as exc:
                log.warning("could not remove archive %s: %s", archive, exc)

        self._prune_snapshots()
        return snapshot

    def _prune_snapshots(self) -> None:
        """Keep only the most recent ``keep_snapshots`` PNGs."""
        if self.keep_snapshots <= 0:
            return
        pngs = sorted(
            self.snapshot_dir.glob(f"{self.node_name}_heatmap_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in pngs[self.keep_snapshots :]:
            try:
                stale.unlink()
            except OSError:
                pass


def _seconds_until(target_hour: int, target_minute: int, *, now: datetime) -> float:
    """Seconds from ``now`` until the next ``HH:MM`` (today or tomorrow)."""
    target = now.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class Scheduler:
    """Fires :meth:`DailySnapshot.run_once` every day at ``HH:MM`` local time."""

    def __init__(self, snapshot: DailySnapshot, *, hour: int, minute: int) -> None:
        self.snapshot = snapshot
        self.hour = hour
        self.minute = minute
        self._stop = threading.Event()

    def run_forever(self) -> None:
        log.info("daily snapshot scheduled for %02d:%02d", self.hour, self.minute)
        while not self._stop.is_set():
            wait = _seconds_until(self.hour, self.minute, now=datetime.now())
            log.info("next snapshot in %.0f min", wait / 60)
            # Wake up periodically so stop() is responsive and clock changes
            # (NTP steps, DST) are re-evaluated rather than trusted for hours.
            if self._stop.wait(min(wait, 300)):
                break
            if _seconds_until(self.hour, self.minute, now=datetime.now()) > 1:
                continue
            try:
                self.snapshot.run_once()
            except Exception:  # noqa: BLE001 — a bad cycle must not kill the loop
                log.exception("snapshot cycle failed")
            # Avoid double-firing within the same minute.
            self._stop.wait(61)

    def stop(self) -> None:
        self._stop.set()


class CollectorService:
    """Wires the receiver and scheduler together for the systemd unit."""

    def __init__(
        self,
        *,
        raw_file: Path | str,
        snapshot_dir: Path | str,
        notifier: Notifier,
        host: str = "0.0.0.0",
        port: int = 9000,
        node_name: str = "collector",
        deliver_hour: int = 20,
        deliver_minute: int = 30,
        window_hours: int = 24,
        keep_snapshots: int = 14,
    ) -> None:
        self.store = RawStore(raw_file)
        self.receiver = Receiver(self.store, host=host, port=port)
        self.snapshot = DailySnapshot(
            self.store,
            notifier,
            snapshot_dir=snapshot_dir,
            node_name=node_name,
            window_hours=window_hours,
            keep_snapshots=keep_snapshots,
        )
        self.scheduler = Scheduler(
            self.snapshot, hour=deliver_hour, minute=deliver_minute
        )

    def run_forever(self) -> None:
        self.receiver.start()
        try:
            self.scheduler.run_forever()
        finally:
            self.receiver.stop()
            self.store.close()

    def stop(self) -> None:
        self.scheduler.stop()
        self.receiver.stop()
