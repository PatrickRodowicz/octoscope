"""Rendering helpers.

This module used to carry Agile price slots and plunge-window detection. Both
are gone: the tariff comparison view answers the question plunge alerts were
standing in for ("would a different tariff be cheaper, and when"), and it does
it against real consumption rather than a tariff this account is not on.
"""
from __future__ import annotations

import datetime as dt

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int | None = None, floor: float | None = None) -> str:
    """Render values as block-drawing characters.

    `floor` pins the bottom of the scale instead of letting it float to the
    minimum value. Passing 0 keeps zero at the baseline, which matters wherever
    values can go negative: scaling from a negative minimum puts zero halfway up
    the glyph range, so an exporting house draws a mid-height bar and the
    picture contradicts the number printed next to it.
    """
    if not values:
        return ""
    if width and len(values) > width:
        # Downsample by averaging into `width` buckets.
        bucket = len(values) / width
        values = [
            sum(values[int(i * bucket) : max(int((i + 1) * bucket), int(i * bucket) + 1)])
            / max(1, len(values[int(i * bucket) : max(int((i + 1) * bucket), int(i * bucket) + 1)]))
            for i in range(width)
        ]
    if floor is not None:
        values = [max(v, floor) for v in values]
    low = floor if floor is not None else min(values)
    high = max(values)
    span = high - low
    if span < 1e-9:
        return SPARK_CHARS[0] * len(values) if floor is not None else (
            SPARK_CHARS[len(SPARK_CHARS) // 2] * len(values)
        )
    out = []
    for v in values:
        idx = int((v - low) / span * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[max(0, min(len(SPARK_CHARS) - 1, idx))])
    return "".join(out)


def humanise(delta: dt.timedelta) -> str:
    """'2h 15m' style formatting for a positive timedelta."""
    total = int(delta.total_seconds())
    if total < 0:
        return "now"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"
