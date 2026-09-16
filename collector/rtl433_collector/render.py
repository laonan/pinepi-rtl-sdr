"""Render a :class:`~rtl433_collector.heatmap.Heatmap` to a PNG snapshot.

Uses Pillow only (a prebuilt wheel on Raspberry Pi OS), avoiding a matplotlib /
numpy toolchain on the collector Pi. The layout mirrors the ASCII mock:

    row label column | one square per hour column

Cell intensity is bucketed from the per-cell count into a small number of
levels so a glance conveys "quiet vs busy", matching the ░ / █ glyphs of the
mock.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .heatmap import Heatmap

# Intensity buckets. Each level maps a count threshold to an RGB colour, going
# from "empty" through increasingly saturated blues, like ░ -> █.
_LEVELS: list[tuple[int, tuple[int, int, int]]] = [
    (0, (0x1B, 0x1E, 0x27)),   # no events — near-background
    (1, (0x27, 0x3A, 0x52)),
    (3, (0x2F, 0x5C, 0x8A)),
    (6, (0x2E, 0x86, 0xC1)),
    (12, (0x3F, 0xB6, 0xDA)),
    (24, (0x6F, 0xE7, 0xC7)),  # busiest
]

_BG = (0x14, 0x16, 0x1C)
_FG = (0xE6, 0xE9, 0xEF)
_MUTED = (0x8A, 0x93, 0xA3)
_GRID = (0x2A, 0x2E, 0x38)


def _level_color(count: int) -> tuple[int, int, int]:
    color = _LEVELS[0][1]
    for threshold, rgb in _LEVELS:
        if count >= threshold:
            color = rgb
        else:
            break
    return color


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType font, falling back to Pillow's bitmap default.

    DejaVu ships with Pillow's test data on most distros; if unavailable we
    degrade gracefully rather than fail the daily job.
    """
    for name in (
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_png(
    heatmap: Heatmap,
    dest: Path | str,
    *,
    title: str = "RTL-433 Activity - past 24h",
    node_name: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> Path:
    """Render ``heatmap`` to ``dest`` as a PNG and return the path written."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    labels = [heatmap.labels[c] for c in heatmap.rows]
    keys = list(heatmap.rows)
    col_labels = heatmap.column_labels()

    # --- layout metrics -----------------------------------------------------
    cell = 26          # square size
    gap = 4            # gap between squares
    label_w = 320      # left gutter for row labels
    top = 96           # header height
    left = 24
    bottom = 64        # footer height

    grid_w = heatmap.hours * (cell + gap) - gap
    grid_h = len(keys) * (cell + gap) - gap
    width = left + label_w + grid_w + left
    height = top + grid_h + bottom

    img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)

    font_title = _load_font(22)
    font_label = _load_font(15)
    font_small = _load_font(12)

    # --- header --------------------------------------------------------------
    draw.text((left, 22), title, font=font_title, fill=_FG)
    subtitle = "{} - {}".format(
        heatmap.start.strftime("%Y-%m-%d %H:%M"),
        heatmap.end.strftime("%Y-%m-%d %H:%M"),
    )
    if node_name:
        subtitle = f"{node_name}   {subtitle}"
    draw.text((left, 54), subtitle, font=font_small, fill=_MUTED)

    grid_x0 = left + label_w
    grid_y0 = top

    # --- column (hour) headers ----------------------------------------------
    for i, clabel in enumerate(col_labels):
        cx = grid_x0 + i * (cell + gap)
        draw.text((cx + 2, top - 20), clabel, font=font_small, fill=_MUTED)

    # --- rows ----------------------------------------------------------------
    for r, key in enumerate(keys):
        cy = grid_y0 + r * (cell + gap)
        # Row label, right-aligned against the grid.
        label = labels[r]
        tw = draw.textlength(label, font=font_label)
        max_w = label_w - 16
        # Truncate over-long labels so they never overrun the grid.
        while tw > max_w and len(label) > 1:
            label = label[:-2] + "…"
            tw = draw.textlength(label, font=font_label)
        draw.text(
            (grid_x0 - 12 - tw, cy + (cell - 15) // 2),
            label,
            font=font_label,
            fill=_FG,
        )
        # Cells.
        for i in range(heatmap.hours):
            count = heatmap.rows[key][i]
            cx = grid_x0 + i * (cell + gap)
            draw.rectangle(
                [cx, cy, cx + cell - 1, cy + cell - 1],
                fill=_level_color(count),
                outline=_GRID,
            )

    # --- footer / legend -----------------------------------------------------
    gen = generated_at or datetime.now()
    footer = "generated {}   -   {} events ({} matched)".format(
        gen.strftime("%Y-%m-%d %H:%M:%S"),
        heatmap.total_events,
        heatmap.classified_events,
    )
    ly = height - 44
    draw.text((left, ly), footer, font=font_small, fill=_MUTED)

    # Legend swatches: low -> high. Right-aligned to the grid's right edge so
    # it never collides with the footer text on the left.
    swatch = 14
    swatch_gap = 4
    swatches_w = len(_LEVELS) * (swatch + swatch_gap) - swatch_gap
    less_w = draw.textlength("less", font=font_small)
    more_w = draw.textlength("more", font=font_small)
    grid_right = grid_x0 + heatmap.hours * (cell + gap) - gap
    legend_w = less_w + 8 + swatches_w + 8 + more_w
    lx = grid_right - legend_w

    draw.text((lx, ly), "less", font=font_small, fill=_MUTED)
    sx = lx + less_w + 8
    for _, rgb in _LEVELS:
        draw.rectangle([sx, ly, sx + swatch, ly + swatch], fill=rgb, outline=_GRID)
        sx += swatch + swatch_gap
    draw.text((sx + 4, ly), "more", font=font_small, fill=_MUTED)

    img.save(dest, format="PNG")
    return dest
