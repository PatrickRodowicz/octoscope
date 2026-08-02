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
from octoscope.app import COMPARE_FRAMES, GRAINS, Octoscope

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

        print("\n=== chart grains ===")
        # Back to the chart: `1`-`6` mean the live granularity on the live view.
        while app.view != "chart":
            await pilot.press("tab")
            await pilot.pause()
        check("on the chart view", app.view, "chart")
        seen = []
        for _ in range(len(GRAINS)):
            await pilot.press("g")
            await pilot.pause()
            seen.append(GRAINS[app.grain_index][0])
        check("g cycles every grain exactly once",
              sorted(seen), sorted(g[0] for g in GRAINS))
        check("6 and 12 hour sit between hour and day",
              [g[0] for g in GRAINS],
              ["30 MIN", "HOUR", "6 HOUR", "12 HOUR", "DAY", "WEEK", "MONTH"])

        while GRAINS[app.grain_index][0] != "6 HOUR":
            await pilot.press("g")
            await pilot.pause()
        check("chart titles the grain",
              "6 HOUR" in str(app.query_one("#trend-title").content), True)
        check("and names its unit", app.query_one("#trend").unit, "6-hour block")

        # Refining on a period change has to keep working now that there are
        # two more grains for it to walk past.
        await pilot.press("1")  # 12 HOURS
        await pilot.pause()
        check("12 HOURS refines a 6 HOUR chart to HOUR",
              GRAINS[app.grain_index][0], "HOUR")

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

        check("it opens on today vs yesterday",
              "TODAY vs YESTERDAY" in heading(), True)
        for key, frame in (("2", "WEEK"), ("3", "MONTH"), ("4", "YEAR")):
            await pilot.press(key)
            await pilot.pause()
            check(f"{key} selects the {frame.lower()} frame",
                  COMPARE_FRAMES[app.frame_index][0], frame)
        check("and says which two periods it is showing",
              "THIS YEAR vs LAST YEAR" in heading(), True)

        await pilot.press("g")
        await pilot.pause()
        check("g plots cost instead of kWh", app.compare_metric, "cost")
        check("and the hint says how to get back", "g kWh" in heading(), True)
        await pilot.press("g")
        await pilot.pause()
        check("and back again", app.compare_metric, "kwh")

        await pilot.press("1")
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        check("← steps back a whole period", app.compare_offset, 1)
        check("which moves both halves along",
              "YESTERDAY vs SAT" in heading() or "YESTERDAY vs" in heading(), True)
        await pilot.press("2")
        await pilot.pause()
        check("changing frame returns to the period in progress",
              app.compare_offset, 0)

        await pilot.press("left")
        await pilot.press("left")
        await pilot.pause()
        check("and it keeps stepping", app.compare_offset, 2)
        await pilot.press("home")
        await pilot.pause()
        check("home comes back to now", app.compare_offset, 0)
        await pilot.press("right")
        await pilot.pause()
        check("right at the present stays put", app.compare_offset, 0)

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
