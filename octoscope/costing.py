"""Turning kWh into money.

Everything here works in pence INCLUDING VAT, so one convention holds across
the whole app. Two facts about this account drive the design, and both are
detected at runtime rather than hardcoded, because both are easy to get wrong:

  Payment method - the unit-rate endpoint returns DIRECT_DEBIT and
    NON_DIRECT_DEBIT rows for the same period, differing by ~5.5%. Picking the
    wrong one silently inflates every number on screen.

  Night window - Economy 7's cheap window is not a fixed national time. It is
    whatever the meter was configured with. We recover it by asking Octopus what
    it actually charged per half hour and seeing where the rate steps down.

Both are calibrated from GraphQL telemetry, which reports Octopus's own costing.
Note that telemetry costDelta is EX-VAT, so calibration compares against
value_exc_vat, while all display costing uses value_inc_vat.
"""
from __future__ import annotations

import datetime as dt
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")

# A half-hourly bucket must carry at least this much energy before its implied
# unit rate is trustworthy enough to calibrate from.
_MIN_KWH_FOR_CALIBRATION = 0.02
_RATE_MATCH_TOLERANCE_P = 0.15

# Above this many distinct prices a tariff is market-priced rather than running
# a published daily timetable. Cosy, the busiest timetable, uses three.
_SCHEDULE_MAX_BANDS = 6


@dataclass
class RateTimeline:
    """Unit rates over time, newest-last, already filtered to one payment method."""

    records: list[tuple[dt.datetime, dt.datetime | None, float]] = field(default_factory=list)
    _starts: list[dt.datetime] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._starts = [record[0] for record in self.records]

    @classmethod
    def from_records(cls, rows: list[dict], payment_method: str | None) -> "RateTimeline":
        entries = []
        for row in rows:
            method = row.get("payment_method")
            # A null payment_method means the rate applies to every method.
            if payment_method and method and method != payment_method:
                continue
            valid_from = _parse(row.get("valid_from"))
            if valid_from is None:
                continue
            entries.append((valid_from, _parse(row.get("valid_to")), float(row["value_inc_vat"])))
        entries.sort(key=lambda e: e[0])
        return cls(records=entries)

    def at(self, when: dt.datetime) -> float | None:
        """Rate in effect at `when`, in pence inc VAT.

        Agile publishes a fresh rate every half hour, so a year of it is ~17,500
        records and this gets called once per reading - a linear scan here is
        what made tariff comparison chew the CPU. Records are sorted by
        valid_from, so binary-search to the newest one that could apply and walk
        back from there. Periods do not overlap in practice, so the first
        candidate almost always wins; the walk only does real work across a gap.
        """
        found = None
        index = bisect_right(self._starts, when)
        while index > 0:
            index -= 1
            valid_from, valid_to, value = self.records[index]
            if valid_to is None or when < valid_to:
                found = value
                break
        if found is None and self.records:
            # Consumption predating the earliest published rate: fall back to
            # the oldest known rate rather than dropping the reading.
            return self.records[0][2]
        return found

    @property
    def latest(self) -> float | None:
        return self.records[-1][2] if self.records else None


@dataclass
class Calibration:
    """What we worked out about how this account is actually billed."""

    payment_method: str
    night_slots: frozenset[int]  # local half-hour indices, 0..47
    confident: bool
    note: str = ""

    def is_night(self, local_time: dt.datetime) -> bool:
        return _slot_index(local_time) in self.night_slots

    def to_dict(self) -> dict:
        return {
            "payment_method": self.payment_method,
            "night_slots": sorted(self.night_slots),
            "confident": self.confident,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        return cls(
            payment_method=data["payment_method"],
            night_slots=frozenset(data["night_slots"]),
            confident=data.get("confident", False),
            note=data.get("note", ""),
        )

    @property
    def night_window(self) -> tuple[str, str] | None:
        """Render the night slots as a start-end pair, if contiguous."""
        if not self.night_slots:
            return None
        slots = sorted(self.night_slots)
        # Handle a window that wraps past midnight by rotating to the gap.
        runs, run = [], [slots[0]]
        for prev, cur in zip(slots, slots[1:]):
            if cur == prev + 1:
                run.append(cur)
            else:
                runs.append(run)
                run = [cur]
        runs.append(run)
        if len(runs) == 2 and runs[0][0] == 0 and runs[-1][-1] == 47:
            run = runs[-1] + runs[0]
        elif len(runs) == 1:
            run = runs[0]
        else:
            return None
        return _slot_label(run[0]), _slot_label((run[-1] + 1) % 48)


DEFAULT_CALIBRATION = Calibration(
    payment_method="DIRECT_DEBIT",
    # Fallback only: the most common UK Economy 7 window, used when there is no
    # telemetry to calibrate from. Flagged as unconfident so the UI can say so.
    night_slots=frozenset(range(3, 17)),  # 01:30 - 08:30
    confident=False,
    note="assumed - no telemetry to calibrate from",
)


def calibrate(
    buckets: list[dict], day_rows: list[dict], night_rows: list[dict]
) -> Calibration:
    """Recover payment method and night window from Octopus's own costing.

    `buckets` is HALF_HOURLY telemetry. For each bucket the implied unit rate is
    costDelta / consumptionDelta, which we match against the published ex-VAT
    candidates.
    """
    samples: list[tuple[int, float]] = []
    for bucket in buckets:
        kwh = float(bucket.get("consumptionDelta") or 0) / 1000.0
        cost = float(bucket.get("costDelta") or 0)
        if kwh < _MIN_KWH_FOR_CALIBRATION or cost <= 0:
            continue
        read_at = _parse(bucket.get("readAt"))
        if read_at is None:
            continue
        samples.append((_slot_index(read_at.astimezone(UK)), cost / kwh))

    if not samples:
        return DEFAULT_CALIBRATION

    # Which payment method's ex-VAT rates do the observed rates match?
    best_method, best_hits = None, -1
    for method in ("DIRECT_DEBIT", "NON_DIRECT_DEBIT"):
        candidates = [
            float(r["value_exc_vat"])
            for r in (*day_rows, *night_rows)
            if r.get("payment_method") in (method, None)
        ]
        if not candidates:
            continue
        hits = sum(
            1
            for _, observed in samples
            if any(abs(observed - c) <= _RATE_MATCH_TOLERANCE_P for c in candidates)
        )
        if hits > best_hits:
            best_method, best_hits = method, hits

    if not best_method or best_hits < len(samples) * 0.5:
        return Calibration(
            payment_method=DEFAULT_CALIBRATION.payment_method,
            night_slots=_night_slots_by_split(samples),
            confident=False,
            note="rates did not match either payment method cleanly",
        )

    # Night slots are those billed at the lower of the two observed rates.
    night_slots = _night_slots_by_split(samples)
    return Calibration(
        payment_method=best_method,
        night_slots=night_slots,
        confident=bool(night_slots),
        note="calibrated from Octopus billing data",
    )


def _night_slots_by_split(samples: list[tuple[int, float]]) -> frozenset[int]:
    """Split observed rates into cheap/expensive about their midpoint."""
    rates = [r for _, r in samples]
    low, high = min(rates), max(rates)
    if high - low < 1.0:
        # Single-rate day, or all readings landed in one register.
        return frozenset()
    midpoint = (low + high) / 2
    night: dict[int, list[bool]] = {}
    for slot, rate in samples:
        night.setdefault(slot, []).append(rate < midpoint)
    # Majority vote per slot, so one odd reading cannot flip a whole half hour.
    return frozenset(s for s, votes in night.items() if sum(votes) > len(votes) / 2)


@dataclass
class DayTotal:
    date: dt.date
    day_kwh: float = 0.0
    night_kwh: float = 0.0
    day_cost_p: float = 0.0
    night_cost_p: float = 0.0
    standing_p: float = 0.0
    partial: bool = False
    slots: int = 0            # half-hours actually recorded for this day
    provisional: bool = False  # filled from the Home Mini, not settled billing

    @property
    def total_kwh(self) -> float:
        return self.day_kwh + self.night_kwh

    @property
    def usage_cost_p(self) -> float:
        return self.day_cost_p + self.night_cost_p

    @property
    def total_cost_p(self) -> float:
        return self.usage_cost_p + self.standing_p


def cost_halfhourly(
    readings: list[dict],
    calibration: Calibration,
    day_rates: RateTimeline,
    night_rates: RateTimeline,
    standing: RateTimeline | None = None,
    *,
    start_key: str = "interval_start",
    value_key: str = "consumption",
    scale: float = 1.0,
) -> list[DayTotal]:
    """Aggregate half-hourly readings into per-day totals, oldest first.

    Works for both REST consumption records (kWh, `interval_start`) and GraphQL
    telemetry buckets (watt-hours, `readAt`) via the key/scale arguments.
    """
    days: dict[dt.date, DayTotal] = {}
    for reading in readings:
        start = _parse(reading.get(start_key))
        if start is None:
            continue
        raw = reading.get(value_key)
        if raw is None:
            continue
        kwh = float(raw) * scale
        local = start.astimezone(UK)
        bucket = days.setdefault(local.date(), DayTotal(date=local.date()))
        bucket.slots += 1
        if calibration.is_night(local):
            rate = night_rates.at(start) or 0.0
            bucket.night_kwh += kwh
            bucket.night_cost_p += kwh * rate
        else:
            rate = day_rates.at(start) or 0.0
            bucket.day_kwh += kwh
            bucket.day_cost_p += kwh * rate

    if standing is not None:
        for date, bucket in days.items():
            noon = dt.datetime.combine(date, dt.time(12), tzinfo=UK)
            bucket.standing_p = standing.at(noon) or 0.0

    # Settled data lags real time, so the newest day is almost always partial.
    # Flagging it keeps it out of means and trend bars, where a half-recorded
    # day would read as a real drop in usage.
    #
    # The lag is not reliably one day. Octopus has been seen to publish only the
    # first hour of a day and then nothing for another day and a half, so days
    # in the past get the same slot-count test rather than being trusted purely
    # for being old - otherwise a 2-of-48 day counts as a full one and silently
    # drags every mean and forecast down.
    today = dt.datetime.now(UK).date()
    for date, bucket in days.items():
        if date >= today or bucket.slots < expected_slots(date):
            bucket.partial = True

    return [days[d] for d in sorted(days)]


def expected_slots(date: dt.date) -> int:
    """Half-hours in a local day: 48, or 46/50 on the clock-change days."""
    start = dt.datetime.combine(date, dt.time(0), tzinfo=UK)
    end = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0), tzinfo=UK)
    return int((end - start).total_seconds() // 1800)


def merge_provisional(
    settled: list[DayTotal], provisional: list[DayTotal]
) -> list[DayTotal]:
    """Fill gaps in settled billing data with Home Mini figures.

    Only days the settled feed has not finished are candidates, and only where
    the meter actually has more of the day than billing does. The Home Mini
    agrees with settled data to within 0.001 kWh over a couple of days, so the
    numbers are trustworthy - but they are not what Octopus has billed, so the
    result is flagged `provisional` and drawn differently.
    """
    by_date = {day.date: day for day in provisional}
    out: list[DayTotal] = []
    for day in settled:
        candidate = by_date.get(day.date)
        if (
            day.partial
            and candidate is not None
            and candidate.slots > day.slots
            and candidate.slots >= expected_slots(day.date)
        ):
            filled = replace(
                candidate,
                standing_p=day.standing_p or candidate.standing_p,
                partial=False,
                provisional=True,
            )
            out.append(filled)
        else:
            out.append(day)
    return out


def merged_readings(
    settled: list[dict], telemetry: list[dict]
) -> tuple[list[dict], set[dt.date]]:
    """One half-hourly series, settled where it exists and live where it does not.

    Returns the series and the dates taken from the meter rather than a bill.
    The caller needs that set to shade those bars, and it has to come from here:
    working it out separately from the day totals missed today entirely - whose
    data is *always* live - so yesterday drew as provisional while today, the
    least settled day of all, drew as though Octopus had billed it.

    Every chart figure is derived from this single pool. Totals used to be
    assembled per grain from whatever buckets survived filtering, which meant
    the same period reported different energy at different granularities -
    interior part-recorded days were dropped by the day path and swallowed by
    the hourly one, and a trailing partial week silently discarded the complete
    days inside it.

    Substitution is per calendar day and follows the same rule as the chart's
    provisional bars: the Home Mini wins a day only when it recorded more of it
    than settlement did. Readings are normalised to the REST shape - kWh under
    `interval_start` - so downstream code has exactly one format to handle.
    """
    by_date: dict[dt.date, list[dict]] = {}
    for row in settled:
        start = _parse(row.get("interval_start"))
        if start is None or row.get("consumption") is None:
            continue
        by_date.setdefault(start.astimezone(UK).date(), []).append(row)

    live_by_date: dict[dt.date, list[dict]] = {}
    for row in telemetry:
        start = _parse(row.get("readAt"))
        delta = row.get("consumptionDelta")
        if start is None or delta is None:
            continue
        live_by_date.setdefault(start.astimezone(UK).date(), []).append({
            "interval_start": start.isoformat(),
            "consumption": float(delta) / 1000.0,
        })

    from_meter: set[dt.date] = set()
    for date, rows in live_by_date.items():
        if len(rows) > len(by_date.get(date, ())):
            by_date[date] = rows
            from_meter.add(date)

    out: list[dict] = []
    for date in sorted(by_date):
        out.extend(by_date[date])
    out.sort(key=lambda r: r["interval_start"])
    return out, from_meter


@dataclass
class Reconciliation:
    """One day measured twice: by the Home Mini, and by settlement."""

    date: dt.date
    settled_kwh: float = 0.0
    live_kwh: float = 0.0
    settled_slots: int = 0
    live_slots: int = 0

    @property
    def delta_kwh(self) -> float:
        return self.live_kwh - self.settled_kwh

    @property
    def delta_pct(self) -> float | None:
        if self.settled_kwh <= 0:
            return None
        return self.delta_kwh / self.settled_kwh * 100

    @property
    def complete(self) -> bool:
        """Both sources recorded a whole day, so the delta means something."""
        want = expected_slots(self.date)
        return self.settled_slots >= want and self.live_slots >= want


def reconcile(settled: list[dict], live: list[dict]) -> list[Reconciliation]:
    """Line the Home Mini's record up against settled billing, day by day.

    The two should agree: they are the same meter. They rarely agree exactly,
    because the Mini reports what it saw over the wire while settlement is what
    the supplier accepted, and a dropped bucket on the Mini shows up as a
    shortfall rather than a gap. Days present in only one source are dropped -
    a day the Mini never covered is not evidence of a discrepancy.
    """
    days: dict[dt.date, Reconciliation] = {}

    def accumulate(rows, start_key, value_key, scale, kwh_field, slot_field):
        for row in rows:
            start = _parse(row.get(start_key))
            raw = row.get(value_key)
            if start is None or raw is None:
                continue
            date = start.astimezone(UK).date()
            entry = days.setdefault(date, Reconciliation(date=date))
            setattr(entry, kwh_field, getattr(entry, kwh_field) + float(raw) * scale)
            setattr(entry, slot_field, getattr(entry, slot_field) + 1)

    accumulate(settled, "interval_start", "consumption", 1.0,
               "settled_kwh", "settled_slots")
    accumulate(live, "readAt", "consumptionDelta", 0.001,
               "live_kwh", "live_slots")

    return sorted(
        (d for d in days.values() if d.settled_slots and d.live_slots),
        key=lambda d: d.date,
    )


def kwh_up_to(
    readings: list[dict],
    target: dt.date,
    cutoff: dt.datetime,
    *,
    start_key: str = "interval_start",
    value_key: str = "consumption",
    scale: float = 1.0,
) -> float | None:
    """Energy used on `target` up to the same time of day as `cutoff`.

    Lets today-so-far be compared against yesterday-by-now, rather than
    against yesterday's full 24 hours.
    """
    limit = _slot_index(cutoff.astimezone(UK))
    total = 0.0
    seen = False
    for reading in readings:
        start = _parse(reading.get(start_key))
        if start is None or reading.get(value_key) is None:
            continue
        local = start.astimezone(UK)
        if local.date() != target or _slot_index(local) >= limit:
            continue
        seen = True
        total += float(reading[value_key]) * scale
    return total if seen else None


@dataclass
class Bucket:
    """One row of a rollup table."""

    start: dt.datetime
    end: dt.datetime
    day_kwh: float = 0.0
    night_kwh: float = 0.0
    day_cost_p: float = 0.0
    night_cost_p: float = 0.0
    standing_p: float = 0.0

    @property
    def kwh(self) -> float:
        return self.day_kwh + self.night_kwh

    @property
    def usage_cost_p(self) -> float:
        return self.day_cost_p + self.night_cost_p

    @property
    def total_cost_p(self) -> float:
        return self.usage_cost_p + self.standing_p

    partial: bool = False
    slots: int = 0  # half-hours actually recorded in this bucket

    @property
    def expected_slots(self) -> int:
        """Half-hours this bucket would hold if fully recorded.

        Measured from the real elapsed time between its bounds, so the clock
        change days come out at 46 and 50 rather than a hardcoded 48.

        Converted to UTC first, and that is the whole trick: both bounds carry
        the same ZoneInfo object, and subtracting two datetimes that share a
        tzinfo ignores the offsets and gives the wall-clock difference. Without
        the conversion this returned the nominal count on exactly the two days
        it exists to get right, marking a real full day partial and dropping it
        out of the mean and peak.
        """
        span = self.end.astimezone(dt.timezone.utc) - self.start.astimezone(dt.timezone.utc)
        return int(span.total_seconds() // 1800)

    @property
    def tariff(self) -> str:
        """Which register this period was billed on - or the split, if both."""
        if self.kwh <= 0:
            return "-"
        if self.night_kwh <= 0:
            return "day"
        if self.day_kwh <= 0:
            return "night"
        return f"{self.day_kwh / self.kwh * 100:.0f}/{self.night_kwh / self.kwh * 100:.0f} d/n"


# Rollup granularities offered by the table view. Sub-hour rollups need Home
# Mini telemetry; the rest come from settled half-hourly consumption.
ROLLUPS: dict[str, str] = {
    "5min": "5 MIN",
    "30min": "30 MIN",
    "60min": "60 MIN",
    "day": "DAY",
    "month": "MONTH",
}


# Rollup periods shorter than a day. These label their bars with a time rather
# than a bare date, and are fine-grained enough that "did the home mini supply
# this?" is a meaningful question about a single bar. Named once here so adding
# a granularity is one edit rather than several scattered string literals.
SUB_DAY_PERIODS = frozenset({"5min", "30min", "60min", "6hr", "12hr"})


def _bucket_start(when: dt.datetime, period: str) -> dt.datetime:
    local = when.astimezone(UK)
    if period == "5min":
        return local.replace(minute=local.minute // 5 * 5, second=0, microsecond=0)
    if period == "30min":
        return local.replace(minute=0 if local.minute < 30 else 30, second=0, microsecond=0)
    if period == "60min":
        return local.replace(minute=0, second=0, microsecond=0)
    if period == "6hr":
        return local.replace(hour=local.hour // 6 * 6, minute=0, second=0, microsecond=0)
    if period == "12hr":
        return local.replace(hour=local.hour // 12 * 12, minute=0, second=0, microsecond=0)
    if period == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - dt.timedelta(days=midnight.weekday())
    if period == "month":
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        return local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown rollup period: {period}")


def _bucket_end(start: dt.datetime, period: str) -> dt.datetime:
    if period == "5min":
        return start + dt.timedelta(minutes=5)
    if period == "30min":
        return start + dt.timedelta(minutes=30)
    if period == "60min":
        return start + dt.timedelta(hours=1)
    # Adding to a tz-aware datetime is wall-clock arithmetic, so a block that
    # straddles a clock change still ends on a 6- or 12-hour boundary rather
    # than an hour either side of one. Its true length then falls out of
    # Bucket.expected_slots, which converts before subtracting.
    if period == "6hr":
        return start + dt.timedelta(hours=6)
    if period == "12hr":
        return start + dt.timedelta(hours=12)
    if period == "day":
        return start + dt.timedelta(days=1)
    if period == "week":
        return start + dt.timedelta(days=7)
    if period == "year":
        return start.replace(year=start.year + 1)
    # Months vary in length, so step by day-of-month rather than a fixed delta.
    return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def rollup(
    readings: list[dict],
    period: str,
    calibration: Calibration,
    day_rates: RateTimeline,
    night_rates: RateTimeline,
    standing: RateTimeline | None = None,
    *,
    start_key: str = "interval_start",
    value_key: str = "consumption",
    scale: float = 1.0,
    interval_minutes: float = 30.0,
) -> list[Bucket]:
    """Aggregate readings into buckets of `period`, oldest first.

    The standing charge is apportioned across each reading by elapsed time
    rather than dropped or applied whole. That keeps the cost column additive:
    twelve 5-minute rows sum to their hour, hours sum to the day, days to the
    month. Without it, sub-day rows would silently exclude a real cost.
    """
    buckets: dict[dt.datetime, Bucket] = {}
    standing_per_minute_cache: dict[dt.date, float] = {}

    for reading in readings:
        start = _parse(reading.get(start_key))
        raw = reading.get(value_key)
        if start is None or raw is None:
            continue
        kwh = float(raw) * scale
        local = start.astimezone(UK)
        key = _bucket_start(start, period)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = Bucket(start=key, end=_bucket_end(key, period))
            buckets[key] = bucket

        bucket.slots += 1
        if calibration.is_night(local):
            bucket.night_kwh += kwh
            bucket.night_cost_p += kwh * (night_rates.at(start) or 0.0)
        else:
            bucket.day_kwh += kwh
            bucket.day_cost_p += kwh * (day_rates.at(start) or 0.0)

        if standing is not None:
            date = local.date()
            if date not in standing_per_minute_cache:
                noon = dt.datetime.combine(date, dt.time(12), tzinfo=UK)
                standing_per_minute_cache[date] = (standing.at(noon) or 0.0) / (24 * 60)
            bucket.standing_p += standing_per_minute_cache[date] * interval_minutes

    now = dt.datetime.now(UK)
    for bucket in buckets.values():
        # Under-covered as well as still-running. A bucket missing half-hours
        # is not a real dip in usage and must stay out of means and peaks,
        # whether it is the week in progress or a day the meter part-recorded
        # back in March.
        if bucket.end > now or bucket.slots < bucket.expected_slots:
            bucket.partial = True
    return [buckets[k] for k in sorted(buckets)]


def patch_today(
    buckets: list[Bucket],
    today: DayTotal | None,
    settled_today: Bucket | None,
    period: str,
) -> list[Bucket]:
    """Swap today's stale settled figures for live telemetry.

    Settled consumption lags by most of a day, so without this the current day
    and month rows disagree with the TODAY tile, which reads from the Home Mini
    and is hours fresher. For a day row we substitute outright; for a month row
    we exchange only today's share and keep the settled remainder.
    """
    if today is None or period not in ("day", "month"):
        return buckets

    key = _bucket_start(dt.datetime.now(UK), period)
    stale_day_kwh = settled_today.day_kwh if settled_today else 0.0
    stale_night_kwh = settled_today.night_kwh if settled_today else 0.0
    stale_day_cost = settled_today.day_cost_p if settled_today else 0.0
    stale_night_cost = settled_today.night_cost_p if settled_today else 0.0
    stale_standing = settled_today.standing_p if settled_today else 0.0

    for index, bucket in enumerate(buckets):
        if bucket.start != key:
            continue
        if period == "day":
            buckets[index] = Bucket(
                start=bucket.start, end=bucket.end,
                day_kwh=today.day_kwh, night_kwh=today.night_kwh,
                day_cost_p=today.day_cost_p, night_cost_p=today.night_cost_p,
                # The day's standing charge is incurred in full whatever the
                # hour, so show all of it rather than the settled fraction.
                standing_p=today.standing_p, partial=True,
            )
        else:
            buckets[index] = Bucket(
                start=bucket.start, end=bucket.end,
                day_kwh=bucket.day_kwh - stale_day_kwh + today.day_kwh,
                night_kwh=bucket.night_kwh - stale_night_kwh + today.night_kwh,
                day_cost_p=bucket.day_cost_p - stale_day_cost + today.day_cost_p,
                night_cost_p=bucket.night_cost_p - stale_night_cost + today.night_cost_p,
                standing_p=bucket.standing_p - stale_standing + today.standing_p,
                partial=True,
            )
        return buckets

    # No row for the current period yet (nothing settled today): add one.
    if period == "day":
        buckets.append(
            Bucket(
                start=key, end=_bucket_end(key, period),
                day_kwh=today.day_kwh, night_kwh=today.night_kwh,
                day_cost_p=today.day_cost_p, night_cost_p=today.night_cost_p,
                standing_p=today.standing_p, partial=True,
            )
        )
    return buckets


# ---------------- period comparison ----------------

# Each comparison frame, and the grain its two periods are drawn at. A day is
# read hour by hour and a year month by month, so that the two periods being
# compared always hold the same kind of sub-bucket in the same order - which is
# what makes overlaying them mean anything.
COMPARE_GRAINS: dict[str, str] = {
    "day": "60min",
    "week": "day",
    "month": "day",
    "year": "month",
}

_PERIOD_CURRENT = {
    "day": "TODAY", "week": "THIS WEEK", "month": "THIS MONTH", "year": "THIS YEAR",
}
_PERIOD_PREVIOUS = {
    "day": "YESTERDAY", "week": "LAST WEEK", "month": "LAST MONTH", "year": "LAST YEAR",
}
# How to name a period once it is further back than "last".
_PERIOD_FORMAT = {"day": "%a %d %b", "week": "week of %d %b", "month": "%b %Y", "year": "%Y"}


@dataclass
class PeriodStat:
    """One period's usage, both as a total and bucket by bucket.

    `series` is one entry per sub-bucket of the period, in order, with None for
    buckets nothing was recorded in. None rather than zero because the two are
    genuinely different: the hours of today that have not happened yet are not
    hours in which the house used nothing, and drawing them as zero would make
    every day look like it collapsed at the current time.
    """

    label: str
    start: dt.datetime
    end: dt.datetime
    grain: str
    kwh: float = 0.0
    day_kwh: float = 0.0
    night_kwh: float = 0.0
    cost_p: float = 0.0
    starts: list[dt.datetime] = field(default_factory=list)
    series: list[float | None] = field(default_factory=list)
    cost_series: list[float | None] = field(default_factory=list)

    def values(self, metric: str) -> list[float | None]:
        return self.cost_series if metric == "cost" else self.series

    def total(self, metric: str) -> float:
        return self.cost_p if metric == "cost" else self.kwh

    @property
    def recorded(self) -> int:
        return sum(1 for value in self.series if value is not None)

    @property
    def empty(self) -> bool:
        return self.recorded == 0


@dataclass
class Comparison:
    """Two adjacent periods of the same length, lined up against each other."""

    frame: str
    grain: str
    current: PeriodStat
    previous: PeriodStat
    # The previous period measured only as far into itself as the current one
    # has got. None once the current period is over and the full totals are
    # already like for like.
    previous_to_date: PeriodStat | None
    now: dt.datetime
    cutoff: dt.datetime | None

    @property
    def running(self) -> bool:
        """Is the current period still in progress?"""
        return self.previous_to_date is not None

    @property
    def baseline(self) -> PeriodStat:
        """Whichever previous figure the current one can fairly be judged against."""
        return self.previous_to_date or self.previous

    def delta(self, metric: str) -> float:
        return self.current.total(metric) - self.baseline.total(metric)

    def delta_pct(self, metric: str) -> float | None:
        before = self.baseline.total(metric)
        return self.delta(metric) / before * 100 if before > 0 else None

    def swing(self, metric: str) -> tuple[dt.datetime, float] | None:
        """The sub-bucket that moved most, so the change has somewhere to point.

        Only buckets both periods recorded, and only ones that have finished: a
        bucket the previous period is simply missing has not changed, it is
        unknown, and the hour in progress is short by however much of it is
        still to come - which would name it the biggest change most of the day.
        """
        mine = self.current.values(metric)
        theirs = self.previous.values(metric)
        finished = len(self.current.starts)
        if self.running:
            finished = sum(1 for m in self.current.starts if m <= self.now) - 1
        best: tuple[dt.datetime, float] | None = None
        for index, moment in enumerate(self.current.starts[:max(0, finished)]):
            if index >= len(theirs):
                break
            here, there = mine[index], theirs[index]
            if here is None or there is None:
                continue
            change = here - there
            if best is None or abs(change) > abs(best[1]):
                best = (moment, change)
        return best


def period_window(
    frame: str, offset: int = 0, now: dt.datetime | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """Bounds of the frame's period, `offset` whole periods back from now."""
    now = (now or dt.datetime.now(UK)).astimezone(UK)
    start = _step_back(_bucket_start(now, frame), frame, offset)
    return start, _bucket_end(start, frame)


def _step_back(start: dt.datetime, frame: str, count: int) -> dt.datetime:
    """`count` whole periods earlier than `start`.

    Walks back a second from each boundary and re-buckets rather than
    subtracting a fixed span, so months of different lengths and the weeks
    containing a clock change all land on real period starts.
    """
    for _ in range(count):
        start = _bucket_start(start - dt.timedelta(seconds=1), frame)
    return start


# Grains whose buckets are a fixed length of real time, however the clock is
# behaving. Days and up are not: those are wall-clock periods, and a local day
# is 23 or 25 hours long twice a year on purpose.
_FIXED_GRAIN_SECONDS = {"5min": 300, "30min": 1800, "60min": 3600}


def _bucket_grid(start: dt.datetime, end: dt.datetime, grain: str) -> list[dt.datetime]:
    """Every bucket start between `start` and `end`, at `grain`.

    Hourly and finer grids step in elapsed time rather than by adding to the
    local clock, because on the two clock-change days those are different
    things. Adding an hour to the wall clock across the autumn change walks
    straight over the repeated hour - so its readings would have no column to
    land in and would silently vanish from the totals - and across the spring
    one it names the same instant twice.
    """
    seconds = _FIXED_GRAIN_SECONDS.get(grain)
    grid: list[dt.datetime] = []
    cursor = start
    while cursor < end:
        grid.append(_bucket_start(cursor, grain))
        if seconds is None:
            cursor = _bucket_end(cursor, grain)
        else:
            cursor = cursor.astimezone(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return grid


def period_name(frame: str, start: dt.datetime, now: dt.datetime) -> str:
    """`TODAY`, `LAST WEEK`, or a date once it is further back than that."""
    current = _bucket_start(now, frame)
    if start == current:
        return _PERIOD_CURRENT[frame]
    if start == _step_back(current, frame, 1):
        return _PERIOD_PREVIOUS[frame]
    return start.astimezone(UK).strftime(_PERIOD_FORMAT[frame]).upper()


def period_stat(
    readings: list[dict],
    start: dt.datetime,
    end: dt.datetime,
    grain: str,
    calibration: Calibration,
    day_rates: RateTimeline,
    night_rates: RateTimeline,
    standing: RateTimeline | None = None,
    label: str = "",
) -> PeriodStat:
    """Total and per-bucket usage for one window of the reading pool.

    Positions each bucket on the period's own grid rather than just listing the
    buckets that had data, so index 3 means the same thing - the fourth hour,
    the fourth day - in both periods of a comparison even when one of them has
    a gap in the middle.
    """
    window = [
        reading for reading in readings
        if (when := _parse(reading.get("interval_start"))) is not None
        and start <= when < end
    ]
    grid = _bucket_grid(start, end, grain)
    stat = PeriodStat(
        label=label, start=start, end=end, grain=grain, starts=grid,
        series=[None] * len(grid), cost_series=[None] * len(grid),
    )
    position = {moment: index for index, moment in enumerate(grid)}
    for bucket in rollup(
        window, grain, calibration, day_rates, night_rates, standing
    ):
        index = position.get(bucket.start)
        if index is None:
            continue
        stat.series[index] = bucket.kwh
        stat.cost_series[index] = bucket.total_cost_p
        stat.kwh += bucket.kwh
        stat.day_kwh += bucket.day_kwh
        stat.night_kwh += bucket.night_kwh
        stat.cost_p += bucket.total_cost_p
    return stat


def compare_periods(
    readings: list[dict],
    frame: str,
    calibration: Calibration,
    day_rates: RateTimeline,
    night_rates: RateTimeline,
    standing: RateTimeline | None = None,
    *,
    offset: int = 0,
    now: dt.datetime | None = None,
) -> Comparison:
    """This day/week/month/year against the one before it.

    The comparison anyone actually wants is like for like: the period in
    progress has only got so far into itself, and holding a Tuesday morning up
    against a whole Monday reports a collapse in usage every morning. So while
    the current period is running, the previous one is also measured to the
    same point, and that is what the headline change is computed from - with
    its full total kept alongside, since where the day is heading matters too.
    """
    now = (now or dt.datetime.now(UK)).astimezone(UK)
    grain = COMPARE_GRAINS[frame]
    current_start, current_end = period_window(frame, offset, now)
    previous_start = _step_back(current_start, frame, 1)

    def stat(start: dt.datetime, end: dt.datetime, label: str) -> PeriodStat:
        return period_stat(
            readings, start, end, grain, calibration, day_rates, night_rates,
            standing, label)

    current = stat(
        current_start, current_end, period_name(frame, current_start, now))
    previous = stat(
        previous_start, current_start, period_name(frame, previous_start, now))

    to_date: PeriodStat | None = None
    cutoff: dt.datetime | None = None
    if current_start <= now < current_end:
        # Wall-clock arithmetic, so "as far in as we are now" means the same
        # time of day rather than the same number of elapsed seconds - which
        # would land an hour out either side of a clock change.
        cutoff = min(previous_start + (now - current_start), current_start)
        to_date = stat(previous_start, cutoff, previous.label)
    return Comparison(
        frame=frame, grain=grain, current=current, previous=previous,
        previous_to_date=to_date, now=now, cutoff=cutoff,
    )


@dataclass
class Forecast:
    month_to_date_p: float
    projected_p: float
    days_elapsed: int
    days_in_month: int
    mean_daily_p: float
    basis_days: int

    @property
    def days_remaining(self) -> int:
        return max(0, self.days_in_month - self.days_elapsed)


def forecast_month(
    totals: list[DayTotal],
    today: DayTotal | None,
    standing_today_p: float,
    now: dt.datetime | None = None,
) -> Forecast | None:
    """Project this calendar month's bill from complete days so far.

    Today is included in the month-to-date figure but excluded from the daily
    mean, since a partial day would drag the projection down.
    """
    now = (now or dt.datetime.now(UK)).astimezone(UK)
    month_start = now.date().replace(day=1)
    days_in_month = _days_in_month(now.date())

    complete = [t for t in totals if t.date >= month_start and not t.partial]
    month_to_date = sum(t.total_cost_p for t in complete)
    if today is not None:
        month_to_date += today.usage_cost_p + standing_today_p

    # Prefer a recent window for the mean - it tracks seasonal drift better
    # than averaging the whole month. Partial days would bias it downwards.
    basis = [t for t in totals if not t.partial][-14:]
    if not basis:
        return None
    mean_daily = sum(t.total_cost_p for t in basis) / len(basis)

    days_elapsed = (now.date() - month_start).days + 1
    remaining = days_in_month - days_elapsed
    return Forecast(
        month_to_date_p=month_to_date,
        projected_p=month_to_date + mean_daily * remaining,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        mean_daily_p=mean_daily,
        basis_days=len(basis),
    )


def agile_counterfactual(
    readings: list[dict],
    agile_rates: list[dict],
    *,
    start_key: str = "interval_start",
    value_key: str = "consumption",
) -> tuple[float, list[dict]] | None:
    """What the same consumption would have cost on Agile, in pence inc VAT.

    Returns the Agile cost together with the exact readings it covers. Agile
    rates only exist for half hours Octopus has published, so the caller must
    cost that same subset on the real tariff - comparing an Agile subtotal
    against a full-period actual would overstate the saving badly.
    """
    by_start: dict[dt.datetime, float] = {}
    for row in agile_rates:
        start = _parse(row.get("valid_from"))
        if start is not None:
            by_start[start] = float(row["value_inc_vat"])
    if not by_start:
        return None

    total = 0.0
    matched: list[dict] = []
    for reading in readings:
        start = _parse(reading.get(start_key))
        rate = by_start.get(start) if start else None
        if rate is None or reading.get(value_key) is None:
            continue
        matched.append(reading)
        total += float(reading[value_key]) * rate
    return (total, matched) if matched else None


def same_local_day(value: str | None, when: dt.datetime | None = None) -> bool:
    """Is this timestamp on the same UK-local calendar day as `when`?"""
    parsed = _parse(value)
    if parsed is None:
        return False
    reference = (when or dt.datetime.now(UK)).astimezone(UK)
    return parsed.astimezone(UK).date() == reference.date()


@dataclass
class PowerBucket:
    """Average power over a slice of time, for the live trace.

    `wh` is imported energy, which the meter floors at zero. `demand` is the
    signed net flow, so it goes negative while on-site generation exceeds
    consumption. Both are kept because neither alone tells the whole story:
    without demand, export looks identical to an idle house.
    """

    start: dt.datetime
    end: dt.datetime
    wh: float = 0.0
    demand_sum: float = 0.0
    samples: int = 0

    @property
    def watts(self) -> float:
        """Average imported power, from metered energy."""
        minutes = (self.end - self.start).total_seconds() / 60.0
        return self.wh / (minutes / 60.0) if minutes else 0.0

    @property
    def net_watts(self) -> float:
        """Average net flow; negative means exporting."""
        return self.demand_sum / self.samples if self.samples else 0.0

    @property
    def exporting(self) -> bool:
        return self.net_watts < 0

    @property
    def kwh(self) -> float:
        return self.wh / 1000.0


def aggregate_power(readings: list[dict], bucket_seconds: int) -> list[PowerBucket]:
    """Bucket telemetry into fixed slices of average power, oldest first."""
    buckets: dict[int, PowerBucket] = {}
    for reading in readings:
        start = _parse(reading.get("readAt"))
        if start is None:
            continue
        epoch = int(start.timestamp())
        key = epoch - (epoch % bucket_seconds)
        bucket = buckets.get(key)
        if bucket is None:
            begin = dt.datetime.fromtimestamp(key, dt.timezone.utc)
            bucket = PowerBucket(
                start=begin, end=begin + dt.timedelta(seconds=bucket_seconds)
            )
            buckets[key] = bucket
        delta = reading.get("consumptionDelta")
        if delta is not None:
            bucket.wh += float(delta)
        demand = reading.get("demand")
        if demand is not None:
            bucket.demand_sum += float(demand)
            bucket.samples += 1
    return [buckets[k] for k in sorted(buckets)]


@dataclass
class Spike:
    """A burst of demand well above the surrounding baseline."""

    start: dt.datetime
    end: dt.datetime
    peak_watts: float
    wh: float
    baseline_watts: float
    cost_p: float = 0.0
    excess_cost_p: float = 0.0

    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start

    @property
    def kwh(self) -> float:
        return self.wh / 1000.0


def find_spikes(
    buckets: list[PowerBucket],
    rate_at,
    *,
    min_watts: float = 400.0,
    multiplier: float = 1.75,
) -> list[Spike]:
    """Detect appliance-sized bursts against the run's own baseline.

    The threshold is relative to the median rather than absolute, so it adapts
    to a quiet house and a busy one. `rate_at(when)` returns pence per kWh so
    each burst can be costed at the rate in force when it happened - a spike in
    the cheap night window genuinely costs less.
    """
    if len([b for b in buckets if b.samples]) < 5:
        return []

    values = [max(b.net_watts, 0.0) for b in buckets]
    # A *local* baseline, not a global one. Over 24 hours a single median sits
    # between the quiet night and the busy day, so the whole daytime plateau
    # reads as one enormous spike.
    #
    # The low quartile rather than the median, because a median taken across a
    # busy stretch is dragged upward by the very spikes being looked for -
    # which pushed the trigger above a real 2.35 kW burst and lost it.
    baselines = _rolling_quantile(values, _baseline_window(buckets), 0.25)
    baseline = sorted(baselines)[len(baselines) // 2]

    def limits(index: int) -> tuple[float, float]:
        local = baselines[index]
        trigger = local + max(min_watts, local * (multiplier - 1.0))
        # Hysteresis: a burst ends only once demand falls well back toward
        # ambient, so a cycling appliance stays one event.
        return trigger, local + (trigger - local) * 0.5

    runs: list[list[PowerBucket]] = []
    run: list[PowerBucket] = []
    active = False
    for index, bucket in enumerate(buckets):
        trigger, release = limits(index)
        adjoins = not run or bucket.start == run[-1].end
        if not adjoins and run:
            runs.append(run)
            run, active = [], False
        if bucket.net_watts >= trigger:
            active = True
            run.append(bucket)
        elif active and bucket.net_watts >= release:
            run.append(bucket)          # still winding down
        elif run:
            runs.append(run)
            run, active = [], False
    if run:
        runs.append(run)

    # Stitch bursts separated by only a brief lull. Capped in absolute time as
    # well as buckets: three buckets is 90 minutes at half-hourly granularity,
    # which would weld separate appliance runs into one all-day event.
    merge_gap = dt.timedelta(seconds=min(_bucket_seconds(buckets) * 3, 300))
    merged: list[list[PowerBucket]] = []
    for group in runs:
        if merged and group[0].start - merged[-1][-1].end <= merge_gap:
            merged[-1].extend(group)
        else:
            merged.append(list(group))

    spikes: list[Spike] = []
    for group in merged:
        wh = sum(b.wh for b in group)
        span_hours = sum((b.end - b.start).total_seconds() / 3600 for b in group)
        baseline_wh = baseline * span_hours
        rate = rate_at(group[0].start) or 0.0
        spikes.append(
            Spike(
                start=group[0].start,
                end=group[-1].end,
                peak_watts=max(b.net_watts for b in group),
                wh=wh,
                baseline_watts=baseline,
                cost_p=wh / 1000.0 * rate,
                excess_cost_p=max(0.0, wh - baseline_wh) / 1000.0 * rate,
            )
        )
    return sorted(spikes, key=lambda s: s.start, reverse=True)


def _bucket_seconds(buckets: list[PowerBucket]) -> float:
    if not buckets:
        return 60.0
    return (buckets[0].end - buckets[0].start).total_seconds() or 60.0


def _baseline_window(buckets: list[PowerBucket]) -> int:
    """Half-width of the rolling baseline: about two hours either side.

    Wide enough that a burst is a small minority of its own window, so it
    cannot lift the baseline out from under itself.
    """
    seconds = _bucket_seconds(buckets)
    return max(2, min(len(buckets) // 3, int(7200 / seconds) or 2))


def _rolling_quantile(values: list[float], half_width: int, q: float) -> list[float]:
    out: list[float] = []
    for index in range(len(values)):
        low = max(0, index - half_width)
        high = min(len(values), index + half_width + 1)
        window = sorted(values[low:high])
        out.append(window[min(len(window) - 1, int(len(window) * q))])
    return out


@dataclass
class TariffOption:
    """A candidate tariff, priced against real consumption."""

    code: str
    name: str
    unit: RateTimeline                      # day register, or the only register
    night: RateTimeline | None              # None for single-register products
    standing: RateTimeline
    is_current: bool = False

    @property
    def registers(self) -> str:
        return "day/night" if self.night else "single"


@dataclass
class TariffResult:
    option: TariffOption
    usage_cost_p: float
    standing_p: float
    days: int
    kwh: float

    @property
    def total_cost_p(self) -> float:
        return self.usage_cost_p + self.standing_p


def cost_on_tariff(
    option: TariffOption,
    readings: list[dict],
    calibration: Calibration,
    *,
    start_key: str = "interval_start",
    value_key: str = "consumption",
) -> TariffResult | None:
    """Price real consumption on a different tariff.

    Single-register products such as Go and Cosy encode their cheap windows as
    time-varying standard unit rates, so looking the rate up by timestamp
    handles them and Agile alike. Two-register products are split using the
    meter's measured night window.
    """
    usage = 0.0
    kwh = 0.0
    dates: set[dt.date] = set()
    for reading in readings:
        start = _parse(reading.get(start_key))
        raw = reading.get(value_key)
        if start is None or raw is None:
            continue
        local = start.astimezone(UK)
        if option.night is not None and calibration.is_night(local):
            rate = option.night.at(start)
        else:
            rate = option.unit.at(start)
        if rate is None:
            continue
        energy = float(raw)
        usage += energy * rate
        kwh += energy
        dates.add(local.date())

    if not dates:
        return None
    standing = 0.0
    for date in dates:
        noon = dt.datetime.combine(date, dt.time(12), tzinfo=UK)
        standing += option.standing.at(noon) or 0.0
    return TariffResult(
        option=option, usage_cost_p=usage, standing_p=standing,
        days=len(dates), kwh=kwh,
    )


@dataclass
class TariffDay:
    """One day priced on two tariffs side by side."""

    date: dt.date
    kwh: float
    day_kwh: float
    night_kwh: float
    yours_p: float
    theirs_p: float

    @property
    def delta_p(self) -> float:
        """Positive means the candidate cost more that day."""
        return self.theirs_p - self.yours_p


@dataclass
class RateBand:
    """One price, and the times of day it applies."""

    price_p: float
    windows: list[tuple[int, int]]   # (start slot, end slot); start > end wraps midnight


def daily_schedule(
    option: "TariffOption", calibration: Calibration, on_date: dt.date | None = None
) -> list[RateBand] | None:
    """What this tariff charges through a day, as priced bands.

    Economy 7's two registers and Go/Snug/Cosy's single register are the same
    thing from where you stand: some prices, and the hours they apply. The
    meter's register count is a billing detail, so it is not what gets shown -
    this is. Returns None for a market tariff, where there is no timetable.
    """
    date = on_date or dt.datetime.now(UK).date()
    midnight = dt.datetime.combine(date, dt.time(0), tzinfo=UK)
    prices: list[float] = []
    for slot in range(48):
        local = midnight + dt.timedelta(minutes=30 * slot)
        when = local.astimezone(dt.timezone.utc)
        if option.night is not None and calibration.is_night(local):
            rate = option.night.at(when)
        else:
            rate = option.unit.at(when)
        if rate is None:
            return None
        prices.append(round(rate, 2))

    if len(set(prices)) > _SCHEDULE_MAX_BANDS:
        return None

    runs: list[tuple[int, int, float]] = []
    start = 0
    for slot in range(1, 48):
        if prices[slot] != prices[slot - 1]:
            runs.append((start, slot, prices[start]))
            start = slot
    runs.append((start, 48, prices[start]))
    # A band running through midnight is one window, not two.
    if len(runs) > 1 and runs[0][2] == runs[-1][2]:
        wrap_start, _, price = runs.pop()
        runs[0] = (wrap_start, runs[0][1], price)

    bands: dict[float, list[tuple[int, int]]] = {}
    for run_start, run_end, price in runs:
        bands.setdefault(price, []).append((run_start, run_end % 48))
    return [RateBand(price_p=p, windows=w) for p, w in sorted(bands.items())]


def schedule_label(option: "TariffOption", calibration: Calibration) -> str:
    """Short description of a tariff's pricing shape, for the summary table."""
    bands = daily_schedule(option, calibration)
    if bands is None:
        return "half-hourly"
    if len(bands) == 1:
        return "flat"
    return f"{len(bands)} by clock"


def slot_time(slot: int) -> str:
    return _slot_label(slot % 48)


@dataclass
class RateSummary:
    """A tariff's actual prices, kept as fields so they can be column-aligned."""

    unit: str                  # "32.65p", or a range for a tariff that moves
    night: str | None          # second register, or None for single-register
    standing_p: float | None
    note: str = ""             # how the price moves, if it moves at all


@dataclass
class TariffDetail:
    """Everything needed to explain one row of the comparison table."""

    option: TariffOption
    current: TariffOption
    days: list[TariffDay]
    usage_p: float
    current_usage_p: float
    standing_p: float
    current_standing_p: float
    rates: RateSummary
    current_rates: RateSummary
    bands: list[RateBand] | None = None
    current_bands: list[RateBand] | None = None

    @property
    def kwh(self) -> float:
        return sum(d.kwh for d in self.days)

    @property
    def effective_p(self) -> float | None:
        """Pence per kWh actually paid, which is what makes a varying tariff
        comparable to a flat one."""
        return self.usage_p / self.kwh if self.kwh else None

    @property
    def current_effective_p(self) -> float | None:
        return self.current_usage_p / self.kwh if self.kwh else None

    @property
    def total_delta_p(self) -> float:
        return (self.usage_p + self.standing_p) - (
            self.current_usage_p + self.current_standing_p)

    @property
    def usage_delta_p(self) -> float:
        return self.usage_p - self.current_usage_p

    @property
    def standing_delta_p(self) -> float:
        return self.standing_p - self.current_standing_p

    @property
    def cheaper_days(self) -> int:
        return sum(1 for d in self.days if d.delta_p < 0)


def compare_tariffs(
    candidate: TariffOption,
    current: TariffOption,
    readings: list[dict],
    calibration: Calibration,
    *,
    start_key: str = "interval_start",
    value_key: str = "consumption",
) -> TariffDetail:
    """Price the same readings on two tariffs, broken down by day.

    A single total tells you a tariff is cheaper but not why. Splitting usage
    from standing charge, and per day, separates the two things that actually
    differ: the unit rates, and the daily fee you pay before using anything.
    """
    days: dict[dt.date, TariffDay] = {}
    for reading in readings:
        start = _parse(reading.get(start_key))
        raw = reading.get(value_key)
        if start is None or raw is None:
            continue
        local = start.astimezone(UK)
        is_night = calibration.is_night(local)
        energy = float(raw)
        entry = days.setdefault(
            local.date(),
            TariffDay(date=local.date(), kwh=0.0, day_kwh=0.0, night_kwh=0.0,
                      yours_p=0.0, theirs_p=0.0),
        )
        entry.kwh += energy
        if is_night:
            entry.night_kwh += energy
        else:
            entry.day_kwh += energy
        for option, attr in ((candidate, "theirs_p"), (current, "yours_p")):
            timeline = option.night if (option.night and is_night) else option.unit
            rate = timeline.at(start)
            if rate is not None:
                setattr(entry, attr, getattr(entry, attr) + energy * rate)

    ordered = [days[d] for d in sorted(days)]
    standing = current_standing = 0.0
    for date in days:
        noon = dt.datetime.combine(date, dt.time(12), tzinfo=UK)
        standing += candidate.standing.at(noon) or 0.0
        current_standing += current.standing.at(noon) or 0.0
    # Fold the daily fee in so a per-day delta is the real difference.
    for entry in ordered:
        noon = dt.datetime.combine(entry.date, dt.time(12), tzinfo=UK)
        entry.theirs_p += candidate.standing.at(noon) or 0.0
        entry.yours_p += current.standing.at(noon) or 0.0

    return TariffDetail(
        option=candidate,
        current=current,
        days=ordered,
        usage_p=sum(e.theirs_p for e in ordered) - standing,
        current_usage_p=sum(e.yours_p for e in ordered) - current_standing,
        standing_p=standing,
        current_standing_p=current_standing,
        rates=describe_rates(candidate, readings, start_key=start_key),
        current_rates=describe_rates(current, readings, start_key=start_key),
        bands=daily_schedule(candidate, calibration),
        current_bands=daily_schedule(current, calibration),
    )


def describe_rates(
    option: TariffOption, readings: list[dict], *, start_key: str = "interval_start"
) -> RateSummary:
    """What a tariff actually charges.

    Two-register tariffs have a day and a night rate. Single-register ones are
    either flat or, like Agile, re-priced every half hour - so for those the
    spread over the period being compared says far more than any one number.
    """
    standing = option.standing.latest
    if option.night is not None:
        day = option.unit.latest
        night = option.night.latest
        return RateSummary(
            unit=f"{day:.2f}p" if day is not None else "n/a",
            night=f"{night:.2f}p" if night is not None else "n/a",
            standing_p=standing,
            note="flat",
        )

    seen: list[float] = []
    for reading in readings:
        start = _parse(reading.get(start_key))
        if start is None:
            continue
        rate = option.unit.at(start)
        if rate is not None:
            seen.append(rate)
    if not seen:
        return RateSummary(unit="n/a", night=None, standing_p=standing)
    low, high = min(seen), max(seen)
    # How a single-register tariff moves matters more than that it moves. A
    # handful of distinct prices is a published timetable you can plan around;
    # hundreds is a market price you cannot. Calling both "varies" told you
    # nothing and implied Cosy and Snug were Agile-like, which they are not.
    distinct = {round(rate, 2) for rate in seen}
    if len(distinct) == 1:
        return RateSummary(unit=f"{low:.2f}p", night=None, standing_p=standing, note="flat")
    if len(distinct) <= _SCHEDULE_MAX_BANDS:
        return RateSummary(
            unit=f"{low:.2f}p - {high:.2f}p", night=None, standing_p=standing,
            note=f"{len(distinct)} set rates, by clock",
        )
    return RateSummary(
        unit=f"{low:.2f}p - {high:.2f}p", night=None, standing_p=standing,
        note="market, every 30 min",
    )


def _slot_index(local: dt.datetime) -> int:
    return local.hour * 2 + (1 if local.minute >= 30 else 0)


def _slot_label(slot: int) -> str:
    return f"{(slot // 2) % 24:02d}:{'30' if slot % 2 else '00'}"


def _days_in_month(date: dt.date) -> int:
    if date.month == 12:
        return 31
    return (date.replace(day=1, month=date.month + 1) - dt.timedelta(days=1)).day


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# Callers outside this module need the same lenient timestamp parsing when they
# filter raw readings before handing them back in.
parse_time = _parse
