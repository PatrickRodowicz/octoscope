"""Headless checks on the app itself. No network.

Every network entry point on the client is replaced with a counter, so "paused
made no calls" is asserted as a number rather than left to be noticed later.
Credentials still have to be present - the app builds a client at startup - but
nothing is sent anywhere.

    .venv/bin/python uitest.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from octoscope import api
from octoscope.app import GRAINS, RANGES, Octoscope

calls: dict[str, int] = {}
failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'}  {label:<52} {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


POINT = api.MeterPoint(
    mpan="1234567890123", serial="TESTSERIAL", tariff_code="E-2R-VAR-22-11-01-A",
    moved_in=dt.datetime(2026, 3, 23, tzinfo=dt.timezone.utc),
    device_id="test-device", registers=["day", "night"],
)
WINDOW = (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
          dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))


def stub(name: str):
    async def inner(*args, **kwargs):
        calls[name] = calls.get(name, 0) + 1
        return POINT if name == "discover" else []
    return inner


async def settle(pilot, ticks: int = 10, until=None) -> None:
    for _ in range(ticks):
        await pilot.pause()
        if until is not None and until():
            return


async def main() -> int:
    app = Octoscope()
    client = app.client
    for name in ("discover", "telemetry", "consumption", "unit_rate_records",
                 "standing_charge_records", "bills", "rate_limits"):
        setattr(client, name, stub(name))

    async with app.run_test() as pilot:
        await settle(pilot, 40, lambda: app.point is not None)
        check("bootstrapped", app.point is not None, True)
        check("starts unpaused", app.paused, False)

        print("\n=== pause stops the polls ===")
        calls.clear()
        await app._poll_telemetry()
        await app._poll_consumption()
        check("polls call out while running", calls.get("telemetry", 0) > 0, True)

        await pilot.press("p")
        await pilot.pause()
        check("p pauses", app.paused, True)

        calls.clear()
        await app._poll_telemetry()
        await app._poll_consumption()
        check("no telemetry while paused", calls.get("telemetry", 0), 0)
        check("no consumption while paused", calls.get("consumption", 0), 0)

        print("\n=== and the backfills scrolling queues ===")
        app._schedule_fill("test-device", "ONE_MINUTE", [WINDOW])
        await settle(pilot, 5)
        check("no backfill while paused", calls.get("telemetry", 0), 0)
        check("nothing left marked in-flight", app._filling, set())

        print("\n=== but the app stays usable ===")
        calls.clear()
        await app.action_refresh()
        check("manual r still fetches", calls.get("telemetry", 0) > 0, True)
        check("r does not silently unpause", app.paused, True)
        check("paused shows on the status row",
              "paused" in str(app.query_one("#status").content), True)

        print("\n=== resuming ===")
        calls.clear()
        await pilot.press("p")
        await settle(pilot, 20, lambda: bool(calls.get("telemetry")))
        check("p resumes", app.paused, False)
        check("resuming polls at once rather than waiting out the interval",
              calls.get("telemetry", 0) > 0, True)
        check("status row clears", str(app.query_one("#status").content), "")

        calls.clear()
        app._schedule_fill("test-device", "ONE_MINUTE", [WINDOW])
        await settle(pilot, 10, lambda: bool(calls.get("telemetry")))
        check("backfill works again", calls.get("telemetry", 0) > 0, True)

        print("\n=== countdown to the next poll ===")
        # Tab to the live view: chart -> table -> live.
        while app.view != "live":
            await pilot.press("tab")
            await pilot.pause()

        def caption() -> str:
            return str(app.query_one("#live-title").content)

        app._next_poll = time.monotonic() + 42.5
        app.tick_countdown()
        check("counts down in seconds", "update in 42s" in caption(), True)

        app._next_poll = time.monotonic() + 95.5
        app.tick_countdown()
        check("and in m:ss past a minute", "update in 1:35" in caption(), True)

        app._next_poll = time.monotonic() - 3
        app.tick_countdown()
        check("never goes negative", "update in 0s" in caption(), True)

        app._next_poll = None
        app.tick_countdown()
        check("says nothing before a poll is scheduled",
              "update in" in caption(), False)

        app._next_poll = time.monotonic() + 30
        await pilot.press("p")
        await pilot.pause()
        check("pause replaces the countdown", "update in" in caption(), False)
        check("and says why", "paused" in caption(), True)
        app.tick_countdown()
        check("ticking while paused does not bring it back",
              "update in" in caption(), False)
        await pilot.press("p")
        await settle(pilot, 20, lambda: "update in" in caption())
        check("resuming brings it back", "update in" in caption(), True)

        # The timer rearms even while paused, so the countdown never sits at
        # zero waiting for a poll that already came and went.
        app.paused = True
        before = app._next_poll
        await app._poll_telemetry()
        check("the deadline moves whether or not the poll ran",
              app._next_poll > before, True)
        app.paused = False

        print("\n=== ranges and grains ===")
        # The number keys mean the same thing on every view but live, so this
        # section is really testing the whole dashboard's controls at once.
        while app.view != "chart":
            await pilot.press("tab")
            await pilot.pause()
        check("on the chart view", app.view, "chart")
        check("6 and 12 hour sit between hour and day", list(GRAINS),
              ["30min", "60min", "6hr", "12hr", "day", "week", "month"])

        await pilot.press("1")
        await pilot.pause()
        check("1 is today, not a rolling 24 hours", app.time_range.key, "today")
        check("and the title says so",
              "TODAY" in str(app.query_one("#trend-title").content), True)
        await pilot.press("3")
        await pilot.pause()
        check("3 is this week", app.time_range.key, "week")
        check("which starts on a Monday", app.range_window[0].weekday(), 0)

        seen = []
        for _ in range(len(app.time_range.grains)):
            await pilot.press("g")
            await pilot.pause()
            seen.append(app.grain)
        check("g cycles this range's grains exactly once",
              sorted(seen), sorted(app.time_range.grains))
        check("and never offers one the range cannot be drawn at",
              all(g in app.time_range.grains for g in seen), True)

        while app.grain != "6hr":
            await pilot.press("g")
            await pilot.pause()
        check("chart names the unit it is sliced into",
              "per 6-hour block" in str(app.query_one("#trend-title").content), True)
        check("and the chart agrees", app.query_one("#trend").unit, "6-hour block")

        # A range too short for the current grain has to move it, and to the
        # nearest thing that still works rather than all the way to a default.
        await pilot.press("1")  # TODAY: 30 MIN / HOUR / 6 HOUR
        await pilot.pause()
        check("today keeps a 6-hour grain, which it can draw", app.grain, "6hr")
        await pilot.press("9")  # ALL: DAY / WEEK / MONTH
        await pilot.pause()
        check("ALL cannot draw 6-hour blocks and moves to the nearest",
              app.grain, "day")

        print("\n=== stepping back through time ===")
        await pilot.press("3")  # THIS WEEK
        await pilot.pause()
        check("a fresh range starts at the present", app.range_offset, 0)
        await pilot.press("left")
        await pilot.pause()
        check("← steps a whole week back", app.range_offset, 1)
        check("and the title renames itself",
              "LAST WEEK" in str(app.query_one("#trend-title").content), True)
        await pilot.press("right")
        await pilot.press("right")
        await pilot.pause()
        check("→ comes forward and stops at the present", app.range_offset, 0)
        await pilot.press("left")
        await pilot.press("home")
        await pilot.pause()
        check("home returns to now", app.range_offset, 0)

        print("\n=== comparing periods ===")
        # Every figure here comes out of the reading pool already in memory, so
        # the whole view - including stepping back through history - must cost
        # nothing on the wire.
        calls.clear()
        while app.view != "compare":
            await pilot.press("tab")
            await pilot.pause()
        check("tab reaches the compare view", app.view, "compare")

        def heading() -> str:
            return str(app.query_one("#compare-title").content)

        # The compare view reads the same range as everything else, so the key
        # that means "today" on the chart means "today vs yesterday" here.
        await pilot.press("1")
        await pilot.pause()
        check("1 compares today with yesterday",
              "TODAY vs YESTERDAY" in heading(), True)
        for key, frame in (("3", "week"), ("4", "month"), ("9", "year")):
            await pilot.press(key)
            await pilot.pause()
            check(f"{key} reads the range as a {frame}", app.time_range.frame, frame)
        check("and says which two periods it is showing",
              "THIS YEAR vs LAST YEAR" in heading(), True)

        await pilot.press("m")
        await pilot.pause()
        check("m plots cost instead of kWh", app.compare_metric, "cost")
        await pilot.press("m")
        await pilot.pause()
        check("and back again", app.compare_metric, "kwh")

        await pilot.press("2")  # YESTERDAY
        await pilot.pause()
        check("yesterday is simply the day frame, one step back",
              app.compare_offset, 1)
        check("which moves both halves along", "YESTERDAY vs" in heading(), True)
        await pilot.press("1")
        await pilot.pause()
        check("and today comes back to the period in progress",
              app.compare_offset, 0)

        await pilot.press("left")
        await pilot.press("left")
        await pilot.pause()
        check("← keeps stepping", app.compare_offset, 2)
        await pilot.press("home")
        await pilot.pause()
        check("home comes back to now", app.compare_offset, 0)
        await pilot.press("right")
        await pilot.pause()
        check("right at the present stays put", app.compare_offset, 0)

        print("\n=== the control bar ===")
        bar = app.query_one("#controls")

        def controls() -> str:
            return str(bar.content)

        check("it offers every range", [o[1] for o in bar.options],
              [r.label for r in RANGES])
        check("and one number key each", [o[0] for o in bar.options],
              [str((i + 1) % 10) for i in range(len(RANGES))])
        # Narrow terminals fall back to the abbreviations rather than clipping
        # the tail of the picker off - which is exactly where ALL lives.
        check("nothing is cut off when it will not fit",
              all(r.brief in controls() or r.label in controls() for r in RANGES), True)
        check("and hides the grain picker where grain does nothing",
              bar.secondary, [])
        while app.view != "chart":
            await pilot.press("tab")
            await pilot.pause()
        check("the chart gets one", len(bar.secondary) > 0, True)
        check("showing only the grains this range allows",
              len(bar.secondary), len(app.time_range.grains))
        while app.view != "live":
            await pilot.press("tab")
            await pilot.pause()
        check("and live says it is not a time range at all",
              "resolution" in bar.note, True)
        check("with its own four options", len(bar.options), 4)

        check("none of it called out", sum(calls.values()), 0)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
