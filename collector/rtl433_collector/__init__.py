"""rtl433_collector — receiver side of the pinepi-rtl-sdr pipeline.

This package runs on the *collector* Raspberry Pi (no RTL-SDR dongle). It:

  1. listens on a TCP port for the newline-delimited JSON stream forwarded by
     the sensor Pi (``rtl433_forward.sh`` -> ``socat``),
  2. persists everything it receives to a rolling raw JSON file,
  3. once a day (default 20:30) buckets the past 24h of events into a
     device-type x hour heatmap, renders it to a PNG snapshot,
  4. pushes that snapshot through a pluggable notification channel
     (WeCom webhook first), and
  5. rotates the raw JSON file so the next day starts clean.
"""

__version__ = "0.1.0"
