"""Offline checks on the storage layer and the rollup grains. No network, no
credentials.

Runs against a scratch database so it can never touch the real archive:

    .venv/bin/python dbtest.py
"""
from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from octoscope import costing, db

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'}  {label:<52} {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def hours(n: float) -> dt.timedelta:
    return dt.timedelta(hours=n)


T0 = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)


def rows(count: int, step_seconds: int = 10, watts: float = 500.0) -> list[dict]:
    return [
        {
            "readAt": (T0 + dt.timedelta(seconds=i * step_seconds)).isoformat(),
            "consumptionDelta": "1.0000",
            "demand": str(watts),
            "consumption": str(1_000_000 + i),
            "costDelta": "0.0311",
        }
        for i in range(count)
    ]


def main() -> None:
    from octoscope.store import TelemetryStore

    print("=== kv cache ===")
    from octoscope import cache

    cache.put("thing", {"a": 1})
    check("roundtrip", cache.get("thing", ttl=60), {"a": 1})
    check("expired returns None", cache.get("thing", ttl=-1), None)
    check("get_stale ignores ttl", cache.get_stale("thing"), {"a": 1})
    check("missing key", cache.get("nope", ttl=60), None)
    check("age of missing key", cache.age("nope"), None)
    cache.put("thing", {"a": 2})
    check("overwrite", cache.get("thing", ttl=60), {"a": 2})

    print("\n=== telemetry archive ===")
    check("add returns count", db.add_telemetry("dev", "TEN_SECONDS", rows(180)), 180)
    check("count", db.telemetry_count("dev", "TEN_SECONDS"), 180)
    check(
        "re-adding is idempotent",
        (db.add_telemetry("dev", "TEN_SECONDS", rows(180)),
         db.telemetry_count("dev", "TEN_SECONDS"))[1],
        180,
    )
    check(
        "groupings are separate",
        (db.add_telemetry("dev", "ONE_MINUTE", rows(10, 60)),
         db.telemetry_count("dev", "TEN_SECONDS"))[1],
        180,
    )
    check("devices are separate", db.telemetry_count("other", "TEN_SECONDS"), 0)

    sliced = db.telemetry_slice("dev", "TEN_SECONDS", T0, T0 + dt.timedelta(seconds=100))
    check("slice is half-open [start, end)", len(sliced), 10)
    check("slice is ordered", sliced == sorted(sliced, key=lambda r: r["readAt"]), True)
    check("api field names preserved", sorted(sliced[0]),
          ["consumption", "consumptionDelta", "costDelta", "demand", "readAt"])
    check("values are numeric", sliced[0]["demand"], 500.0)

    # A refetched trailing edge must win: the meter may not have finished
    # reporting an instant the first time it was asked for.
    db.add_telemetry("dev", "TEN_SECONDS", [
        {"readAt": T0.isoformat(), "demand": "999.0", "consumptionDelta": "2.0"}
    ])
    again = db.telemetry_slice("dev", "TEN_SECONDS", T0, T0 + dt.timedelta(seconds=10))
    check("newer reading overwrites", again[0]["demand"], 999.0)
    check("no row added by overwrite", db.telemetry_count("dev", "TEN_SECONDS"), 180)

    check("null field stays null", again[0]["costDelta"], None)
    check("timestamps normalised to UTC", again[0]["readAt"], "2026-07-20T12:00:00+00:00")
    db.add_telemetry("dev", "TEN_SECONDS", [{"readAt": "2026-07-20T13:00:00Z", "demand": "1"}])
    check(
        "Z and +00:00 are the same instant",
        len(db.telemetry_slice("dev", "TEN_SECONDS", T0 + hours(1), T0 + hours(1.1))),
        1,
    )
    check("row without readAt is dropped", db.add_telemetry("dev", "TEN_SECONDS", [{"demand": "5"}]), 0)

    print("\n=== coverage and gaps ===")
    store = TelemetryStore("dev2", "HALF_HOURLY")
    check("everything missing when empty",
          store.missing(T0, T0 + hours(4)), [(T0, T0 + hours(4))])
    db.add_coverage("dev2", "HALF_HOURLY", T0 + hours(1), T0 + hours(2))
    check("gap before and after covered range",
          store.missing(T0, T0 + hours(4)),
          [(T0, T0 + hours(1)), (T0 + hours(2), T0 + hours(4))])
    check("fully covered window has no gaps",
          store.missing(T0 + hours(1), T0 + hours(2)), [])
    db.add_coverage("dev2", "HALF_HOURLY", T0 + hours(2), T0 + hours(3))
    check("adjoining ranges merge",
          len(db.coverage_ranges("dev2", "HALF_HOURLY")), 1)
    check("merged range spans both",
          store.missing(T0 + hours(1), T0 + hours(3)), [])
    db.add_coverage("dev2", "HALF_HOURLY", T0 + hours(5), T0 + hours(6))
    check("disjoint range stays separate",
          len(db.coverage_ranges("dev2", "HALF_HOURLY")), 2)
    check("empty range is ignored",
          (db.add_coverage("dev2", "HALF_HOURLY", T0, T0),
           len(db.coverage_ranges("dev2", "HALF_HOURLY")))[1], 2)

    print("\n=== fetch planning ===")
    now = T0 + hours(10)
    ten = TelemetryStore("dev3", "TEN_SECONDS")
    # TEN_SECONDS reaches 12h; a gap older than that is unfetchable and must be
    # dropped rather than spent on.
    old = (now - hours(20), now - hours(18))
    check("gap beyond api reach is dropped", ten.fetchable([old], now), [])
    recent = (now - hours(2), now - hours(1))
    check("recent gap survives", ten.fetchable([recent], now), [recent])
    straddle = (now - hours(13), now - hours(11))
    trimmed = ten.fetchable([straddle], now)
    check("straddling gap is trimmed to the horizon",
          trimmed, [(now - hours(12), now - hours(11))])

    start, end = ten.widen((now - dt.timedelta(minutes=1), now), now)
    check("small gap widens to a full span", end - start, hours(12))
    check("widening never exceeds the span",
          (lambda s, e: e - s)(*ten.widen((now - hours(30), now), now)), hours(12))
    check("widening clamps to the api horizon", start, now - hours(12))
    _, capped = ten.widen((now - hours(1), now + hours(5)), now)
    check("end never runs past now", capped, now)

    print("\n=== empty ranges get retried ===")
    # A range Octopus served nothing for is covered, so it is not re-asked on
    # every render - but the Mini can upload late, so it must not be written off
    # forever either.
    quiet = TelemetryStore("dev4", "HALF_HOURLY")
    now4 = dt.datetime.now(dt.timezone.utc)
    window = (now4 - hours(30), now4 - hours(28))
    db.add_coverage("dev4", "HALF_HOURLY", *window)
    check("empty range counts as covered", quiet.missing(*window), [])
    check("not retried while fresh", quiet.stale_empty(*window, now4), [])
    # Age the fetch so the retry window has passed.
    conn = db.connect()
    conn.execute("UPDATE coverage SET fetched_at = 0 WHERE device_id = 'dev4'")
    conn.commit()
    check("retried once stale", quiet.stale_empty(*window, now4), [window])
    db.add_telemetry("dev4", "HALF_HOURLY", [
        {"readAt": (now4 - hours(29)).isoformat(), "demand": "300"}
    ])
    check("not retried once it holds rows", quiet.stale_empty(*window, now4), [])
    # Beyond the API's reach there is no point retrying: nothing can arrive.
    old_window = (now4 - hours(200), now4 - hours(198))
    db.add_coverage("dev4", "HALF_HOURLY", *old_window)
    conn.execute("UPDATE coverage SET fetched_at = 0 WHERE device_id = 'dev4'")
    conn.commit()
    check("not retried beyond api reach", quiet.stale_empty(*old_window, now4), [])
    # Merging must take the NEWEST fetch time, or a range that just merged with
    # an ancient one (everything imported from the old cache carries 0) would be
    # instantly eligible for retry again - fetch, merge, retry, forever.
    db.add_coverage("dev4", "HALF_HOURLY", now4 - hours(31), now4 - hours(29))
    merged = [
        f for a, b, f in db.coverage_ranges("dev4", "HALF_HOURLY")
        if a <= now4 - hours(30) < b
    ]
    check("merging does not inherit an ancient fetch time", merged and merged[0] > 0, True)
    check("no retry immediately after a fetch",
          quiet.stale_empty(now4 - hours(31), now4 - hours(28), now4), [])

    print("\n=== deriving a coarse view from finer data ===")
    from octoscope.store import best_source
    from octoscope import costing

    base = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    fine = [
        {"readAt": (base + dt.timedelta(seconds=10 * i)).isoformat(),
         "consumptionDelta": "1.0000", "demand": str(360.0 if i % 2 else 0.0),
         "consumption": str(9000 + i), "costDelta": "0.01"}
        for i in range(360)          # one hour of ten-second readings
    ]
    db.add_telemetry("d5", "TEN_SECONDS", fine)
    db.add_coverage("d5", "TEN_SECONDS", base, base + hours(1))
    store5 = TelemetryStore("d5", "TEN_SECONDS")
    check("dense fine data covers the window", store5.covers(base, base + hours(1)), True)
    check("fine data preferred over coarse",
          best_source("d5", "HALF_HOURLY", base, base + hours(1)), "TEN_SECONDS")
    check("no substitute for the finest grouping itself",
          best_source("d5", "TEN_SECONDS", base, base + hours(1)), None)
    check("unknown device falls through", best_source("", "HALF_HOURLY", base, base + hours(1)), None)

    # The point of the exercise: bucketing the fine rows to half hours must
    # reproduce the energy exactly.
    buckets = costing.aggregate_power(store5.slice(base, base + hours(1)), 1800)
    check("two half-hour buckets", len(buckets), 2)
    check("energy is exact under aggregation", sum(b.wh for b in buckets), 360.0)
    check("demand averages across all samples", round(buckets[0].net_watts), 180)

    # Covered but empty, or covered but sparse, must NOT be substituted -
    # serving those as data would report real usage as zero.
    db.add_coverage("d6", "HALF_HOURLY", base, base + hours(1))
    check("covered but empty is not usable",
          TelemetryStore("d6", "HALF_HOURLY").covers(base, base + hours(1)), False)
    db.add_telemetry("d7", "TEN_SECONDS", fine[:20])       # 20 of 360
    db.add_coverage("d7", "TEN_SECONDS", base, base + hours(1))
    check("covered but too sparse is not usable",
          TelemetryStore("d7", "TEN_SECONDS").covers(base, base + hours(1)), False)
    check("sparse data is not substituted",
          best_source("d7", "HALF_HOURLY", base, base + hours(1)), None)

    print("\n=== the trailing edge is forgiven, holes are not ===")
    from octoscope.store import TRAILING_TOLERANCE

    # The live view snaps its window end to the bucket grid, which lands past
    # the archive's edge - written by the last poll - for part of every minute.
    # Refusing over that sliver sent the caller off to fetch a window it
    # already held, at whatever granularity had no recent coverage at all.
    edge = base + hours(1)
    short = TRAILING_TOLERANCE / 2
    check("a sliver past the edge is not covered without tolerance",
          store5.covers(base, edge + short), False)
    check("but is with it",
          store5.covers(base, edge + short, tolerance=TRAILING_TOLERANCE), True)
    check("a shortfall wider than the tolerance still is not",
          store5.covers(base, edge + TRAILING_TOLERANCE * 2,
                        tolerance=TRAILING_TOLERANCE), False)
    check("best_source substitutes across the edge rather than fetching",
          best_source("d5", "ONE_MINUTE", base, edge + short), "TEN_SECONDS")

    # Only the trailing edge. A hole in the middle is real missing data, and
    # forgiving it would silently draw an outage as zero usage.
    db.add_telemetry("d8", "TEN_SECONDS", fine[:180] + fine[240:])
    db.add_coverage("d8", "TEN_SECONDS", base, base + dt.timedelta(minutes=30))
    db.add_coverage("d8", "TEN_SECONDS", base + dt.timedelta(minutes=40), edge)
    store8 = TelemetryStore("d8", "TEN_SECONDS")
    check("an interior hole is not forgiven",
          store8.covers(base, edge, tolerance=TRAILING_TOLERANCE), False)
    check("nor substituted for",
          best_source("d8", "ONE_MINUTE", base, edge), None)
    check("tolerance of zero is the old behaviour",
          store5.covers(base, edge + short, tolerance=dt.timedelta(0)), False)

    print("\n=== reach ===")
    from octoscope.store import max_reach, reach

    check("api reach unchanged", max_reach("TEN_SECONDS"), hours(12))
    check("no archive falls back to api reach", reach("nothing", "TEN_SECONDS"), hours(12))
    db.add_telemetry("deep", "TEN_SECONDS", [
        {"readAt": (dt.datetime.now(dt.timezone.utc) - hours(72)).isoformat(), "demand": "1"}
    ])
    check("archive extends reach past the api", reach("deep", "TEN_SECONDS") > hours(71), True)

    print("\n=== settled consumption ===")
    settled = [
        {
            "interval_start": (T0 + dt.timedelta(minutes=30 * i)).isoformat(),
            "interval_end": (T0 + dt.timedelta(minutes=30 * (i + 1))).isoformat(),
            "consumption": 0.5,
        }
        for i in range(48)
    ]
    check("add returns count", db.add_consumption("mpan", "serial", settled), 48)
    check("idempotent", (db.add_consumption("mpan", "serial", settled),
                         len(db.consumption_slice("mpan", "serial", T0, T0 + hours(24))))[1], 48)
    check("latest", db.consumption_latest("mpan", "serial"), T0 + hours(23.5))
    check("slice respects bounds",
          len(db.consumption_slice("mpan", "serial", T0, T0 + hours(1))), 2)
    check("rest shape preserved",
          sorted(db.consumption_slice("mpan", "serial", T0, T0 + hours(1))[0]),
          ["consumption", "interval_end", "interval_start"])
    db.add_consumption("mpan", "serial", [
        {"interval_start": T0.isoformat(), "interval_end": None, "consumption": 9.9}
    ])
    check("revision overwrites",
          db.consumption_slice("mpan", "serial", T0, T0 + hours(0.5))[0]["consumption"], 9.9)
    check("other meter unaffected", db.consumption_latest("mpan", "other"), None)

    print("\n=== schema migration ===")
    check("user_version stamped",
          db.connect().execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
    # A v1 database has no fetched_at; adding it must not lose coverage rows.
    conn = db.connect()
    conn.execute("DROP TABLE coverage")
    conn.execute("CREATE TABLE coverage (device_id TEXT NOT NULL, grouping TEXT NOT NULL,"
                 " start_at TEXT NOT NULL, end_at TEXT NOT NULL)")
    conn.execute("INSERT INTO coverage VALUES ('v1', 'HALF_HOURLY', ?, ?)",
                 (db.iso(T0), db.iso(T0 + hours(1))))
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    db._migrate(conn)
    conn.commit()
    ranges = db.coverage_ranges("v1", "HALF_HOURLY")
    check("v1 row survives upgrade", len(ranges), 1)
    check("backfilled fetched_at defaults to 0", ranges[0][2], 0.0)
    check("version restamped",
          conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    print("\n=== rollup grains ===")
    # A day of half-hourly readings, 1 kWh each, in high summer so no clock
    # change muddies the counts.
    midnight = dt.datetime(2026, 7, 20, 0, 0, tzinfo=costing.UK)
    settled = [
        {"interval_start": (midnight + dt.timedelta(minutes=30 * i)).isoformat(),
         "consumption": 1.0}
        for i in range(48)
    ]
    epoch = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    flat = lambda p: costing.RateTimeline(records=[(epoch, None, p)])  # noqa: E731
    cal = costing.DEFAULT_CALIBRATION

    def buckets(period: str) -> list[costing.Bucket]:
        return costing.rollup(settled, period, cal, flat(25.0), flat(12.0), flat(48.0))

    check("6hr splits a day four ways", len(buckets("6hr")), 4)
    check("12hr splits a day two ways", len(buckets("12hr")), 2)
    check("6hr blocks start on the quarters",
          [b.start.astimezone(costing.UK).hour for b in buckets("6hr")], [0, 6, 12, 18])
    check("12hr blocks start at midnight and noon",
          [b.start.astimezone(costing.UK).hour for b in buckets("12hr")], [0, 12])
    check("6hr blocks hold twelve half-hours",
          [b.slots for b in buckets("6hr")], [12] * 4)

    # The property the whole rollup design rests on: slicing the same readings
    # more finely must not create or destroy energy or money.
    every = ["30min", "60min", "6hr", "12hr", "day"]
    check("energy is the same at every grain",
          {p: round(sum(b.kwh for b in buckets(p)), 9) for p in every},
          {p: 48.0 for p in every})
    check("cost is the same at every grain",
          len({round(sum(b.total_cost_p for b in buckets(p)), 6) for p in every}), 1)
    check("standing charge lands once per day, not once per bucket",
          round(sum(b.standing_p for b in buckets("6hr")), 6), 48.0)

    # Clock changes: the 00:00-06:00 block really is five hours long in March
    # and seven in October, and expected_slots has to say so or a complete
    # block gets marked part-recorded and dropped from the mean and peak.
    def first_block(day: dt.date, period: str) -> costing.Bucket:
        start = costing._bucket_start(
            dt.datetime.combine(day, dt.time(0, 15), tzinfo=costing.UK), period)
        return costing.Bucket(start=start, end=costing._bucket_end(start, period))

    check("spring forward shortens the first 6hr block",
          first_block(dt.date(2026, 3, 29), "6hr").expected_slots, 10)
    check("fall back lengthens it",
          first_block(dt.date(2026, 10, 25), "6hr").expected_slots, 14)
    check("and the 12hr block with it",
          [first_block(dt.date(2026, 3, 29), "12hr").expected_slots,
           first_block(dt.date(2026, 10, 25), "12hr").expected_slots], [22, 26])
    check("an ordinary day is unaffected",
          first_block(dt.date(2026, 7, 20), "6hr").expected_slots, 12)

    print("\n=== period comparison ===")

    def day_of(date: dt.date, kwh: float, slots: int = 48) -> list[dict]:
        # Stepped in real time from local midnight, the way the meter reports:
        # on a clock-change day the half-hours keep coming every half hour
        # whatever the clock says, so the day holds 46 or 50 of them.
        start = (dt.datetime.combine(date, dt.time(0), tzinfo=costing.UK)
                 .astimezone(dt.timezone.utc))
        return [
            {"interval_start": (start + dt.timedelta(minutes=30 * i)).isoformat(),
             "consumption": kwh}
            for i in range(slots)
        ]

    def compare(pool, frame, when, offset=0):
        return costing.compare_periods(
            pool, frame, cal, flat(25.0), flat(25.0), flat(48.0),
            offset=offset, now=when)

    # Yesterday ran all day at 1 kWh a slot; today has done its first twelve
    # hours at 2. It is noon, so today is level with yesterday's whole day and
    # well ahead of where yesterday had got to by now - and those two facts are
    # the entire reason the like-for-like figure exists.
    noon = dt.datetime(2026, 7, 21, 12, 0, tzinfo=costing.UK)
    two_days = day_of(dt.date(2026, 7, 20), 1.0) + day_of(dt.date(2026, 7, 21), 2.0, 24)
    today = compare(two_days, "day", noon)
    check("today so far", round(today.current.kwh, 6), 48.0)
    check("yesterday in full", round(today.previous.kwh, 6), 48.0)
    check("yesterday by the same hour", round(today.baseline.kwh, 6), 24.0)
    check("so the change is like for like", round(today.delta("kwh"), 6), 24.0)
    check("as a percentage", round(today.delta_pct("kwh"), 6), 100.0)
    check("a running period knows it", today.running, True)
    check("the day is drawn in hours", len(today.current.starts), 24)
    check("hours that have not happened stay blank, not zero",
          today.current.recorded, 12)
    check("both periods are named", (today.current.label, today.previous.label),
          ("TODAY", "YESTERDAY"))
    # An unmistakable spike in one finished hour, and a second one in the hour
    # still running. The running hour is short by however much of it is yet to
    # come, so pointing at it would be an artefact of the clock rather than a
    # change in behaviour - it must lose to the real one.
    spiked = [dict(row) for row in two_days]
    for slot in (14, 15):          # 07:00-08:00 today
        spiked[48 + slot]["consumption"] = 5.0
    swing = compare(spiked, "day", noon).swing("kwh")
    check("the hour that differs most",
          (f"{swing[0].astimezone(costing.UK):%H:%M}", round(swing[1], 6)),
          ("07:00", 8.0))
    running = spiked + [
        {"interval_start": noon.astimezone(dt.timezone.utc).isoformat(),
         "consumption": 40.0}      # 12:00, the hour that has only just started
    ]
    check("and the hour in progress cannot win it",
          f"{compare(running, 'day', noon).swing('kwh')[0].astimezone(costing.UK):%H:%M}",
          "07:00")

    # Stepping back lands on two complete periods, so there is nothing to clip.
    back = compare(two_days, "day", noon, offset=1)
    check("stepping back compares yesterday with the day before",
          (back.current.label, back.previous.label), ("YESTERDAY", "SUN 19 JUL"))
    check("and no longer clips", back.running, False)
    check("the baseline is then the full previous period",
          back.baseline is back.previous, True)

    # Months are not the same length as each other, which is exactly the case
    # a fixed 30-day step would get wrong.
    march = dt.datetime(2026, 3, 5, 9, 0, tzinfo=costing.UK)
    monthly: list[dict] = []
    for day in range(1, 29):
        monthly += day_of(dt.date(2026, 2, day), 1.0)
    for day in range(1, 5):
        monthly += day_of(dt.date(2026, 3, day), 1.0)
    month = compare(monthly, "month", march)
    check("march is drawn in 31 days", len(month.current.starts), 31)
    check("february in 28", len(month.previous.starts), 28)
    check("february in full", round(month.previous.kwh, 6), 28 * 48.0)
    check("clipped to the same point in the month",
          round(month.baseline.kwh, 6), 4 * 48.0 + 18.0)
    check("which is the same date and time",
          f"{month.cutoff.astimezone(costing.UK):%d %b %H:%M}", "05 Feb 09:00")
    check("month names", (month.current.label, month.previous.label),
          ("THIS MONTH", "LAST MONTH"))
    check("stepping back a month crosses into last year",
          compare(monthly, "month", march, offset=2).previous.label, "DEC 2025")

    # The clock-change days. An hourly grid has to have a column for every hour
    # the day really has, or the readings in the missing one are dropped.
    fall = dt.datetime(2026, 10, 25, 12, 0, tzinfo=costing.UK)
    spring = dt.datetime(2026, 3, 29, 12, 0, tzinfo=costing.UK)
    check("the fall-back day has 25 hourly columns",
          len(compare([], "day", fall).current.starts), 25)
    check("and they are all distinct instants",
          len({m.timestamp() for m in compare([], "day", fall).current.starts}), 25)
    check("the spring-forward day has 23",
          len(compare([], "day", spring).current.starts), 23)
    check("a clock-change week is still seven days",
          len(compare([], "week", fall).current.starts), 7)
    check("starting on the monday",
          f"{compare([], 'week', fall).current.starts[0].astimezone(costing.UK):%a %d %b}",
          "Mon 19 Oct")

    # Every reading in the period has to land in exactly one column, clock
    # change or not - the same property the rollup grains are held to above.
    fall_day = day_of(dt.date(2026, 10, 25), 1.0, slots=50)
    fall_stat = compare(fall_day, "day", fall).current
    check("a 25-hour day holds all 50 of its half-hours",
          round(fall_stat.kwh, 6), 50.0)
    check("and the columns sum to the same",
          round(sum(v for v in fall_stat.series if v is not None), 6), 50.0)

    check("a year is drawn in months",
          len(compare([], "year", noon).current.starts), 12)
    check("and named by it", compare([], "year", noon, offset=2).current.label, "2024")

    print("\n=== an empty response is still cached ===")
    import asyncio

    from octoscope.api import OctopusClient
    from octoscope.config import Config

    client = OctopusClient(Config(api_key="not-used", account="not-used"))
    sent: list[str] = []

    async def fake_graphql(query, variables):
        sent.append(variables["s"])
        return {"smartMeterTelemetry": fake_graphql.rows}
    fake_graphql.rows = []
    client._graphql = fake_graphql

    async def fetch(key):
        return await client.telemetry(
            "dq", minutes=30, grouping="HALF_HOURLY", cache_ttl=600, cache_key=key)

    # The Mini has gone quiet. Before this fix nothing was written to the cache
    # on an empty response, so every poll re-asked - the today-totals call
    # going from 6/hour to one per poll exactly when the budget matters most.
    check("empty response returns nothing", asyncio.run(fetch("quiet")), [])
    spent = client.budget.used
    check("a second call is served from cache", asyncio.run(fetch("quiet")), [])
    check("and costs no request", len(sent), 1)
    check("nor a budget slot", client.budget.used, spent)

    # A non-empty response must still cache, and a different key must still ask.
    fake_graphql.rows = [{"readAt": T0.isoformat(), "consumptionDelta": "5.0",
                          "demand": "100", "consumption": "1", "costDelta": "0.1"}]
    check("a real response is returned", len(asyncio.run(fetch("loud"))), 1)
    check("a new key costs a request", len(sent), 2)
    check("and is cached too", len(asyncio.run(fetch("loud"))), 1)
    check("still one request for it", len(sent), 2)
    asyncio.run(client.aclose())

    print("\n=== totals ===")
    print(f"        {db.stats()}")

    if failures:
        print(f"\n{len(failures)} FAILURES")
        for line in failures:
            print(f"  - {line}")
        raise SystemExit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "test.db"
        try:
            main()
        finally:
            db.close()
