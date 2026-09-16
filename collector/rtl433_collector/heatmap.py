"""Classify rtl_433 events into categories and aggregate them into a heatmap.

The heatmap has one row per :data:`CATEGORIES` entry and one column per hour
bucket over a fixed window (default: the past 24 hours). Each cell holds a
count of matching events; the renderer turns that count into an intensity glyph
/ colour.

An rtl_433 event is a decoded JSON object, e.g.::

    {"time": "2026-09-15T22:32:54", "model": "Toyota", "type": "TPMS",
     "id": "f465f831", "freq1": 315.009, ...}
    {"time": "...", "model": "Regency-Remote", "command": "fan_speed",
     "freq": 868.031, ...}

Periodic ``rtl_433`` "stats" frames (objects with a ``stats`` key and no
``model``) are ignored; they are not real device receptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

# ----------------------------------------------------------------------------
# Categories (rows of the heatmap)
#
# Each category has a human label (shown on the row) and a predicate that
# decides whether an event belongs to it. The first matching category wins, so
# order matters: put more specific rules before broad ones.
# ----------------------------------------------------------------------------


def _freq_of(event: dict) -> Optional[float]:
    """Best-effort centre frequency (MHz) for an event.

    rtl_433 uses ``freq`` for ASK/OOK and ``freq1``/``freq2`` for FSK.
    """
    for key in ("freq", "freq1", "freq2"):
        val = event.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _near(freq: Optional[float], target: float, tol: float = 5.0) -> bool:
    return freq is not None and abs(freq - target) <= tol


def _str(event: dict, key: str) -> str:
    val = event.get(key)
    return val.lower() if isinstance(val, str) else ""


@dataclass(frozen=True)
class Category:
    """A heatmap row: a label plus a predicate over an event dict."""

    key: str
    label: str
    match: Callable[[dict], bool]


def _is_tpms(e: dict) -> bool:
    return _str(e, "type") == "tpms" or "tpms" in _str(e, "model")


def _is_315_remote(e: dict) -> bool:
    model = _str(e, "model")
    if _near(_freq_of(e), 315.0):
        return "remote" in model or "ev1527" in model or bool(e.get("command"))
    return "generic remote" in model


def _is_433_remote(e: dict) -> bool:
    model = _str(e, "model")
    if _near(_freq_of(e), 433.92, tol=1.0):
        # Home remotes / switches, but not weather sensors (handled later).
        return any(k in model for k in ("remote", "switch", "door", "pir", "contact"))
    return False


def _is_868_security(e: dict) -> bool:
    model = _str(e, "model")
    if _near(_freq_of(e), 868.3, tol=2.0):
        return any(
            k in model
            for k in ("security", "alarm", "chuango", "regency", "remote", "contact")
        )
    return False


def _is_weather(e: dict) -> bool:
    model = _str(e, "model")
    # Temperature/humidity/rain sensors expose these measurement fields.
    if any(k in e for k in ("temperature_C", "humidity", "rain_mm", "wind_avg_km_h")):
        # TPMS also carries temperature_C; it was matched earlier, so anything
        # reaching here with temperature is a genuine weather sensor.
        return True
    return any(k in model for k in ("weather", "temperature", "humidity", "rain"))


#: Ordered rows of the heatmap. Order defines match precedence.
CATEGORIES: list[Category] = [
    Category("tpms", "TPMS / Tire Pressure", _is_tpms),
    Category("remote_315", "315 MHz Generic Remote", _is_315_remote),
    Category("remote_433", "433 MHz Home Remote", _is_433_remote),
    Category("security_868", "868 MHz Security Protocol Match", _is_868_security),
    Category("weather", "Weather / Temp-Humidity / Rain", _is_weather),
]


def classify(event: dict) -> Optional[str]:
    """Return the category key for an event, or ``None`` if it matches none.

    ``rtl_433`` stats frames (which have a ``stats`` key and no ``model``) are
    explicitly skipped.
    """
    if "stats" in event and "model" not in event:
        return None
    for category in CATEGORIES:
        try:
            if category.match(event):
                return category.key
        except Exception:
            # A malformed event must never crash aggregation.
            continue
    return None


def parse_event_time(event: dict, default: Optional[datetime] = None) -> Optional[datetime]:
    """Parse the ``time`` field of an rtl_433 event.

    rtl_433 with ``-M time:iso`` emits e.g. ``2026-09-15T22:32:54``. Falls back
    to ``default`` when the field is missing or unparseable.
    """
    raw = event.get("time")
    if isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(raw.strip(), fmt)
                except ValueError:
                    continue
    return default


@dataclass
class Heatmap:
    """Aggregated counts: ``rows[category_key][hour_index] -> count``.

    ``start`` is the timestamp of column 0. There are ``hours`` columns, each
    one hour wide.
    """

    start: datetime
    hours: int
    labels: dict[str, str] = field(default_factory=dict)
    rows: dict[str, list[int]] = field(default_factory=dict)
    total_events: int = 0
    classified_events: int = 0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=self.hours)

    def column_labels(self) -> list[str]:
        """Two-digit hour-of-day labels for each column (``00``, ``01`` ...)."""
        return [
            f"{(self.start + timedelta(hours=i)).hour:02d}" for i in range(self.hours)
        ]

    def max_count(self) -> int:
        return max((max(row) for row in self.rows.values() if row), default=0)


def aggregate(
    events: Iterable[dict],
    *,
    window_end: datetime,
    hours: int = 24,
) -> Heatmap:
    """Bucket events into a :class:`Heatmap` covering the ``hours`` ending at
    ``window_end``.

    Column ``i`` covers ``[start + i h, start + (i+1) h)`` where
    ``start = window_end - hours``. Events outside the window are ignored.
    """
    # Align the window start to the top of the hour for stable columns.
    aligned_end = window_end.replace(minute=0, second=0, microsecond=0)
    if aligned_end < window_end:
        aligned_end += timedelta(hours=1)
    start = aligned_end - timedelta(hours=hours)

    heatmap = Heatmap(
        start=start,
        hours=hours,
        labels={c.key: c.label for c in CATEGORIES},
        rows={c.key: [0] * hours for c in CATEGORIES},
    )

    for event in events:
        heatmap.total_events += 1
        key = classify(event)
        if key is None:
            continue
        ts = parse_event_time(event)
        if ts is None:
            continue
        # Compare naive-to-naive: drop tzinfo if present.
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if ts < start or ts >= heatmap.end:
            continue
        col = int((ts - start).total_seconds() // 3600)
        if 0 <= col < hours:
            heatmap.rows[key][col] += 1
            heatmap.classified_events += 1

    return heatmap
