"""A local time-series store for telemetry.

Scrolling the live view used to key its cache on the exact window, so nudging
back one bucket produced a brand new key and a brand new API call - even though
almost all of the data was already on disk. Past telemetry never changes, so it
is worth keeping properly: readings are accumulated per granularity, covered
time ranges are tracked, and only genuine gaps are fetched.

Gaps are also widened before fetching, so stepping repeatedly through history
costs one request per chunk rather than one per step.
"""
from __future__ import annotations

import datetime as dt

from . import cache

# Measured against the live API, per grouping: how much history a single call
# will actually return, and how far back that granularity exists at all.
# Beyond the span the API errors; beyond the reach it returns zero rows.
#
#   TEN_SECONDS   12h ->  3,876 rows   (18h returns nothing)
#   ONE_MINUTE    72h ->  4,321 rows   (144h returns nothing)
#   HALF_HOURLY  144h ->    288 rows   (168h errors)
#
# Fetching in chunks this size makes each request earn its place in the
# 125/hour budget instead of pulling a few minutes at a time.
TELEMETRY_SPAN = {
    "TEN_SECONDS": dt.timedelta(hours=12),
    "ONE_MINUTE": dt.timedelta(hours=72),
    "HALF_HOURLY": dt.timedelta(hours=144),
}
DEFAULT_SPAN = dt.timedelta(hours=12)


def max_reach(grouping: str) -> dt.timedelta:
    """How far back this granularity still has data."""
    return TELEMETRY_SPAN.get(grouping, DEFAULT_SPAN)


class TelemetryStore:
    def __init__(self, device_id: str, grouping: str) -> None:
        self.key = f"series-{device_id}-{grouping}"
        self.span = TELEMETRY_SPAN.get(grouping, DEFAULT_SPAN)
        payload = cache.get_stale(self.key) or {}
        self.rows: dict[str, dict] = payload.get("rows", {})
        self.ranges: list[list[str]] = payload.get("ranges", [])

    # ---------------- coverage ----------------

    def _covered(self) -> list[tuple[dt.datetime, dt.datetime]]:
        spans = [(_parse(a), _parse(b)) for a, b in self.ranges]
        return sorted([s for s in spans if s[0] and s[1]])

    def missing(self, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
        """Sub-ranges of [start, end) not already held."""
        gaps: list[tuple[dt.datetime, dt.datetime]] = []
        cursor = start
        for begin, finish in self._covered():
            if finish <= cursor:
                continue
            if begin >= end:
                break
            if begin > cursor:
                gaps.append((cursor, min(begin, end)))
            cursor = max(cursor, finish)
            if cursor >= end:
                break
        if cursor < end:
            gaps.append((cursor, end))
        return gaps

    def widen(
        self, gap: tuple[dt.datetime, dt.datetime], now: dt.datetime
    ) -> tuple[dt.datetime, dt.datetime]:
        """Pad a small gap into as much history as one call will carry.

        Padding runs *backwards*. Scrolling back opens gaps at the leading edge
        of the window, so extending forwards would refetch data already held
        and buy only the single new bucket - which is precisely the behaviour
        this store exists to avoid.
        """
        start, end = gap
        if end - start < self.span:
            start = end - self.span
        # Never ask for a wider span than the API will serve in one response.
        if end - start > self.span:
            start = end - self.span
        return start, min(end, now)

    # ---------------- data ----------------

    def add(self, start: dt.datetime, end: dt.datetime, rows: list[dict]) -> None:
        for row in rows:
            read_at = row.get("readAt")
            if read_at:
                self.rows[read_at] = row
        self.ranges.append([_iso(start), _iso(end)])
        self._compact()
        cache.put(self.key, {"rows": self.rows, "ranges": self.ranges})

    def _compact(self) -> None:
        spans = self._covered()
        merged: list[list[dt.datetime]] = []
        for begin, finish in spans:
            if merged and begin <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], finish)
            else:
                merged.append([begin, finish])
        self.ranges = [[_iso(a), _iso(b)] for a, b in merged]

    def slice(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        out = []
        for read_at, row in self.rows.items():
            when = _parse(read_at)
            if when and start <= when < end:
                out.append(row)
        out.sort(key=lambda r: r["readAt"])
        return out

    @property
    def size(self) -> int:
        return len(self.rows)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
