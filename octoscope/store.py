"""A local time-series view over archived telemetry.

Scrolling the live view used to key its cache on the exact window, so nudging
back one bucket produced a brand new key and a brand new API call - even though
almost all of the data was already on disk. Past telemetry never changes, so it
is worth keeping properly: readings are accumulated per granularity, covered
time ranges are tracked, and only genuine gaps are fetched.

Gaps are also widened before fetching, so stepping repeatedly through history
costs one request per chunk rather than one per step.

The rows themselves now live in SQLite (`db.py`) rather than a JSON blob that
was rewritten in full on every append. This class is the window onto them: what
is covered, what is missing, and what to ask for. Writing is `db.add_telemetry`,
called from the API client so that no response can reach the UI without being
archived first.
"""
from __future__ import annotations

import datetime as dt

from . import db

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

# How long to leave a range that came back empty before asking again. The Home
# Mini uploads over the internet and can fall behind, so "Octopus has nothing
# for this hour" is a statement about now, not about the hour. Without a retry
# a single connectivity blip would leave a permanent hole in the archive; with
# one on every render it would spend the hourly budget re-learning the same
# nothing.
EMPTY_RETRY = dt.timedelta(hours=1)


# Nominal seconds per reading, finest first. Used to judge whether an archived
# range is dense enough to stand in for a coarser one.
GRAIN_SECONDS = {"TEN_SECONDS": 10, "ONE_MINUTE": 60, "HALF_HOURLY": 1800}
FINEST_FIRST = ("TEN_SECONDS", "ONE_MINUTE", "HALF_HOURLY")

# A grouping may stand in for a coarser one only if it holds at least this share
# of the readings its resolution implies. The Mini skips slots - ten-second data
# arrives at about 323 rows an hour against a nominal 360 - so the test has to
# tolerate real sparseness while still rejecting a range that is covered on
# paper but nearly empty, which would silently under-report energy.
MIN_DENSITY = 0.5


def max_reach(grouping: str) -> dt.timedelta:
    """How far back this granularity is still fetchable from Octopus."""
    return TELEMETRY_SPAN.get(grouping, DEFAULT_SPAN)


def best_source(
    device_id: str, wanted: str, start: dt.datetime, end: dt.datetime
) -> str | None:
    """The finest archived granularity that can answer [start, end) in full.

    A coarser series is a strict summary of a finer one, so holding ten-second
    readings means never needing to fetch the half-hourly view of the same
    hours. `aggregate_power` buckets whatever resolution it is handed, so the
    finer rows are passed through as they are - nothing is synthesised, and no
    derived row is ever written to the archive where it could later be mistaken
    for something the meter said.

    Energy is identical either way: summing ten-second `consumptionDelta` over
    94 half hours reproduced Octopus's own figures to 0.000%. Demand is better,
    not merely equal - the API reports one spot value per half hour, measured at
    357 W where the true average over the same slot was 521 W, so a half-hourly
    row can miss an export window that 180 ten-second samples resolve.

    Returns None when nothing held is complete enough and the caller should
    fetch `wanted` as usual.
    """
    if not device_id:
        return None
    for grouping in FINEST_FIRST:
        if grouping == wanted:
            break  # nothing finer was usable; no point preferring a coarser one
        if TelemetryStore(device_id, grouping).covers(start, end):
            return grouping
    return None


def reach(device_id: str, grouping: str) -> dt.timedelta:
    """How far back this granularity can be *shown*.

    Not the same question as `max_reach`. That one bounds what Octopus will
    still serve; this one includes everything already archived, which is the
    whole point of keeping it. Once a day of ten-second data is recorded it
    stays readable long after the API has forgotten it.
    """
    api = max_reach(grouping)
    if not device_id:
        return api
    extent = db.telemetry_extent(device_id, grouping)
    if not extent:
        return api
    held = dt.datetime.now(dt.timezone.utc) - extent[0]
    return max(api, held)


class TelemetryStore:
    def __init__(self, device_id: str, grouping: str) -> None:
        self.device_id = device_id
        self.grouping = grouping
        self.span = TELEMETRY_SPAN.get(grouping, DEFAULT_SPAN)

    # ---------------- coverage ----------------

    def missing(self, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
        """Sub-ranges of [start, end) not already held."""
        gaps: list[tuple[dt.datetime, dt.datetime]] = []
        cursor = start
        for begin, finish, _ in db.coverage_ranges(self.device_id, self.grouping):
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

    def fetchable(
        self, gaps: list[tuple[dt.datetime, dt.datetime]], now: dt.datetime
    ) -> list[tuple[dt.datetime, dt.datetime]]:
        """Drop the parts of `gaps` Octopus will no longer serve.

        A window can now reach further back than the API does, because the
        archive outlives it. Asking for those hours returns nothing and still
        costs a slot in the hourly budget, so trim to what is actually
        retrievable and let the archive answer for the rest.
        """
        horizon = now - max_reach(self.grouping)
        trimmed = []
        for begin, finish in gaps:
            begin = max(begin, horizon)
            if begin < finish:
                trimmed.append((begin, finish))
        return trimmed

    def covers(self, start: dt.datetime, end: dt.datetime) -> bool:
        """Whether this granularity can answer [start, end) without fetching.

        Coverage alone is not enough. A range recorded from an empty response is
        "covered" while holding nothing, and serving it as though it were data
        would report an hour of real usage as zero. So the readings have to
        actually be there, at a plausible density for the resolution.
        """
        if end <= start or self.missing(start, end):
            return False
        seconds = GRAIN_SECONDS.get(self.grouping)
        if not seconds:
            return False
        expected = (end - start).total_seconds() / seconds
        held = db.telemetry_count_between(self.device_id, self.grouping, start, end)
        return held >= expected * MIN_DENSITY

    def stale_empty(
        self, start: dt.datetime, end: dt.datetime, now: dt.datetime
    ) -> list[tuple[dt.datetime, dt.datetime]]:
        """Covered stretches holding no readings that are worth asking about again.

        Coverage is recorded even for an empty response, otherwise a quiet hour
        would be re-requested on every render. But empty is not always final:
        the Mini pushes over the internet and can upload late, so a range that
        had nothing when asked may have been filled in since. Ranges still
        within the API's reach are retried once `EMPTY_RETRY` has passed.
        """
        out: list[tuple[dt.datetime, dt.datetime]] = []
        horizon = now - max_reach(self.grouping)
        cutoff = now.timestamp() - EMPTY_RETRY.total_seconds()
        for begin, finish, fetched_at in db.coverage_ranges(self.device_id, self.grouping):
            low = max(begin, start, horizon)
            high = min(finish, end, now)
            if low >= high or fetched_at > cutoff:
                continue
            if db.telemetry_count_between(self.device_id, self.grouping, low, high):
                continue
            out.append((low, high))
        return out

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
        # Nor for history it no longer holds.
        start = max(start, now - max_reach(self.grouping))
        return start, min(end, now)

    # ---------------- data ----------------

    def slice(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        return db.telemetry_slice(self.device_id, self.grouping, start, end)

    def all(self) -> list[dict]:
        """Everything ever archived at this granularity, oldest first."""
        extent = db.telemetry_extent(self.device_id, self.grouping)
        if not extent:
            return []
        return db.telemetry_slice(
            self.device_id, self.grouping, extent[0], extent[1] + dt.timedelta(seconds=1)
        )

    @property
    def size(self) -> int:
        return db.telemetry_count(self.device_id, self.grouping)
