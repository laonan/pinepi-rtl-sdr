"""Extract weather / environment readings from decoded rtl_433 events.

This is a *reporting-only* companion to :mod:`rtl433_collector.heatmap`. It does
**not** touch classification, category matching, or any of the heatmap counts —
it just scans the same stream of events a second time and pulls out anything
that looks like a weather / environment measurement (temperature, humidity,
rain, wind, pressure, soil moisture, air quality, ...), so the daily push can
append a short "Weather candidates" block to the WeCom follow-up text message.

Rules (per spec):

  * Only top-level successfully decoded events are inspected. ``rtl_433`` stats
    frames (a ``stats`` key and no ``model``) are skipped, and decoder names
    *inside* stats are never inspected.
  * ``temperature_C`` is ignored for TPMS events (tire sensors report tire
    temperature, not the environment).
  * A reading is included even if the value looks unrealistic / may be a false
    decode — we do no range validation.
  * Repeated receptions of the same device are merged into one entry with a
    count.
  * Output is kept short: identical rendered entries are de-duplicated, and if
    more than ten entries remain only the first five and last five are shown
    with an ``...`` marker in between.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .heatmap import _freq_of, _is_tpms, _str, parse_event_time

# ----------------------------------------------------------------------------
# Field catalogue
#
# Each entry maps an rtl_433 measurement field to a compact renderer. Order
# here defines the order fields appear within a single candidate line. Keeping
# it data-driven means new sensor fields are a one-line addition.
# ----------------------------------------------------------------------------


def _fmt_temp_c(v: float) -> str:
    return f"{v:.1f}°C"


def _fmt_humidity(v: float) -> str:
    return f"{v:.0f}% RH"


def _fmt_rain(v: float) -> str:
    return f"{v:.1f}mm rain"


def _fmt_wind_avg(v: float) -> str:
    return f"{v:.1f}km/h wind"


def _fmt_wind_max(v: float) -> str:
    return f"gust {v:.1f}km/h"


def _fmt_wind_dir(v: float) -> str:
    return f"{v:.0f}°dir"


def _fmt_pressure(v: float) -> str:
    return f"{v:.0f}hPa"


def _fmt_soil(v: float) -> str:
    return f"soil {v:.0f}%"


def _fmt_aqi(v: float) -> str:
    return f"AQI {v:.0f}"


def _fmt_pm25(v: float) -> str:
    return f"PM2.5 {v:.0f}"


def _fmt_pm10(v: float) -> str:
    return f"PM10 {v:.0f}"


def _fmt_uv(v: float) -> str:
    return f"UV {v:.0f}"


def _fmt_lux(v: float) -> str:
    return f"{v:.0f}lux"


def _fmt_co2(v: float) -> str:
    return f"CO2 {v:.0f}ppm"


#: (event-field, renderer). Any numeric field present on an event is emitted.
#: ``temperature_C`` is special-cased (excluded for TPMS) in :func:`_readings`.
_FIELDS: list[tuple[str, Callable[[float], str]]] = [
    ("temperature_C", _fmt_temp_c),
    ("humidity", _fmt_humidity),
    ("rain_mm", _fmt_rain),
    ("wind_avg_km_h", _fmt_wind_avg),
    ("wind_max_km_h", _fmt_wind_max),
    ("wind_dir_deg", _fmt_wind_dir),
    ("pressure_hPa", _fmt_pressure),
    ("moisture", _fmt_soil),
    ("soil_moisture", _fmt_soil),
    ("aqi", _fmt_aqi),
    ("pm2_5_ug_m3", _fmt_pm25),
    ("pm10_0_ug_m3", _fmt_pm10),
    ("uv", _fmt_uv),
    ("uvi", _fmt_uv),
    ("lux", _fmt_lux),
    ("co2_ppm", _fmt_co2),
]

#: Set of fields whose mere presence marks an event as an environment reading.
_ENV_FIELDS = frozenset(f for f, _ in _FIELDS)


def _num(value: object) -> Optional[float]:
    """Return ``value`` as a float if it is numeric (and not a bool)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _readings(event: dict, *, is_tpms: bool) -> list[str]:
    """Rendered measurement fragments for one event, in catalogue order.

    ``temperature_C`` is dropped for TPMS events; everything else numeric that
    appears in the catalogue is rendered as-is (no range validation).
    """
    out: list[str] = []
    seen: set[str] = set()
    for field_name, render in _FIELDS:
        if field_name in seen:
            continue
        if field_name == "temperature_C" and is_tpms:
            continue
        num = _num(event.get(field_name))
        if num is None:
            continue
        out.append(render(num))
        seen.add(field_name)
    return out


@dataclass
class _Candidate:
    """One merged weather-capable device and its most recent reading."""

    device: str
    freq: Optional[float]
    time_hm: str
    readings: list[str]
    count: int = 1

    def render(self) -> str:
        parts: list[str] = [self.time_hm, self.device]
        if self.freq is not None:
            parts.append(f"{self.freq:.3f} MHz")
        parts.extend(self.readings)
        line = " · ".join(parts)
        if self.count > 1:
            line += f" (×{self.count})"
        return line


def _device_key(event: dict) -> str:
    """Stable identity for merging repeated receptions of one device."""
    model = event.get("model")
    model = model if isinstance(model, str) else "unknown"
    ident = event.get("id")
    channel = event.get("channel")
    key = model
    if ident is not None:
        key += f"/{ident}"
    elif channel is not None:
        key += f"/ch{channel}"
    return key


def extract_candidates(events: Iterable[dict]) -> list[str]:
    """Scan ``events`` and return rendered weather/environment candidate lines.

    Repeated receptions of the same device are merged (latest reading wins, with
    a count). The returned lines are ordered by first appearance.
    """
    merged: dict[str, _Candidate] = {}
    order: list[str] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        # Skip stats frames — never inspect decoder names inside stats.
        if "stats" in event and "model" not in event:
            continue
        if not any(f in event for f in _ENV_FIELDS):
            continue

        is_tpms = _is_tpms(event)
        readings = _readings(event, is_tpms=is_tpms)
        if not readings:
            # e.g. a TPMS event whose only env field was temperature_C.
            continue

        key = _device_key(event)
        ts = parse_event_time(event)
        time_hm = ts.strftime("%H:%M") if ts is not None else "--:--"
        device = event.get("model")
        device = device if isinstance(device, str) else "unknown"
        freq = _freq_of(event)

        existing = merged.get(key)
        if existing is None:
            merged[key] = _Candidate(
                device=device,
                freq=freq,
                time_hm=time_hm,
                readings=readings,
            )
            order.append(key)
        else:
            # Merge: keep the latest reading, bump the count.
            existing.count += 1
            existing.time_hm = time_hm
            existing.readings = readings
            existing.freq = freq

    return [merged[k].render() for k in order]


def _dedupe(lines: list[str]) -> list[str]:
    """Drop exact-duplicate rendered lines, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _clamp(lines: list[str], *, head: int = 5, tail: int = 5) -> list[str]:
    """If more than ten entries, keep first ``head`` + ``...`` + last ``tail``."""
    if len(lines) <= head + tail:
        return lines
    return lines[:head] + ["..."] + lines[-tail:]


def format_weather_block(events: Iterable[dict]) -> str:
    """Build the "Weather candidates" text block, or ``""`` when none found.

    The result is designed to be appended to the WeCom follow-up text message,
    e.g.::

        Weather candidates:
        - 19:21 Eurochron-EFTH800 · 433.953 MHz · -32.0°C · 40% RH
        - 21:39 GT-WT03 · 868.037 MHz · 0.0°C · 64% RH
    """
    lines = _clamp(_dedupe(extract_candidates(events)))
    if not lines:
        return ""
    body = "\n".join(("..." if ln == "..." else f"- {ln}") for ln in lines)
    return "Weather candidates:\n" + body
