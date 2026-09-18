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
from .weather import format_weather_block

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
        # Append any weather/environment readings found in the rolled-out data.
        # This is a second, independent pass over the archive so the heatmap
        # aggregation (stats + classification) above is left completely intact.
        if archive is not None:
            weather = format_weather_block(iter_json_lines(archive))
            if weather:
                caption = f"{caption}\n{weather}"
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


def _next_fire_after(reference: datetime, hour: int, minute: int) -> datetime:
    """Return the first ``HH:MM`` strictly after ``reference``.

    Used to compute the *next* scheduled time once the current one has fired.
    """
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reference:
        candidate += timedelta(days=1)
    return candidate


class Scheduler:
    """Fires :meth:`DailySnapshot.run_once` every day at ``HH:MM`` local time.

    The loop tracks an explicit next-fire timestamp and fires as soon as the
    clock has reached or passed it (``now >= target``), then advances to the
    following day. This can neither skip a day (no knife-edge "exactly now"
    window) nor double-fire (the target only moves forward after a fire).
    """

    def __init__(self, snapshot: DailySnapshot, *, hour: int, minute: int) -> None:
        self.snapshot = snapshot
        self.hour = hour
        self.minute = minute
        self._stop = threading.Event()

    def run_forever(self) -> None:
        log.info("daily snapshot scheduled for %02d:%02d", self.hour, self.minute)
        # First target: the next HH:MM at or after startup. If we start exactly
        # at HH:MM we still want to fire, so compute from one second earlier.
        target = _next_fire_after(
            datetime.now() - timedelta(seconds=1), self.hour, self.minute
        )
        while not self._stop.is_set():
            now = datetime.now()
            remaining = (target - now).total_seconds()

            if remaining <= 0:
                # Reached (or passed, e.g. after a clock jump) the target: fire.
                try:
                    self.snapshot.run_once()
                except Exception:  # noqa: BLE001 — a bad cycle must not kill the loop
                    log.exception("snapshot cycle failed")
                # Schedule the next day's fire strictly after the one we just did.
                target = _next_fire_after(target, self.hour, self.minute)
                log.info("next snapshot at %s", target.isoformat(timespec="minutes"))
                continue

            log.info("next snapshot in %.0f min", remaining / 60)
            # Sleep in bounded chunks so stop() stays responsive and clock
            # changes (NTP steps, DST) are re-evaluated rather than trusted for
            # hours. Cap the chunk so we never overshoot the target.
            if self._stop.wait(min(remaining, 60)):
                break

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
