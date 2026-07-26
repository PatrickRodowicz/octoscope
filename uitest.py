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

from octoscope import api
from octoscope.app import GRAINS, Octoscope

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

        print("\n=== chart grains ===")
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

    if failures:
        print(f"\n{len(failures)} FAILURES")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
