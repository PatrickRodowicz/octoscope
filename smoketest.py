"""Non-TUI smoke test: exercises the API and costing engine and prints the
numbers the dashboard will show. Responses are cached, so re-running is free.
"""
import asyncio
import datetime as dt

from octoscope import costing
from octoscope.api import OctopusClient
from octoscope.config import load_config
from octoscope.costing import UK, RateTimeline


def _local_date(reading: dt.datetime | dict):
    return dt.datetime.fromisoformat(reading["interval_start"]).astimezone(UK).date()


async def main() -> None:
    client = OctopusClient(load_config())
    try:
        point = await client.discover()
        print(f"tariff     {point.tariff_code}  (economy7={point.is_economy_7})")
        print(f"registers  {point.registers}")
        print(f"moved in   {point.moved_in:%Y-%m-%d}")
        print(f"device     {point.device_id}")

        now = dt.datetime.now(dt.timezone.utc)
        history_start = point.moved_in or (now - dt.timedelta(days=365))

        day_rows = await client.unit_rate_records(
            point.product_code, point.tariff_code, "day", history_start, now + dt.timedelta(days=2)
        )
        night_rows = await client.unit_rate_records(
            point.product_code, point.tariff_code, "night", history_start, now + dt.timedelta(days=2)
        )
        standing_rows = await client.standing_charge_records(
            point.product_code, point.tariff_code, history_start, now + dt.timedelta(days=2)
        )

        print("\n=== calibration ===")
        buckets = await client.telemetry(point.device_id, minutes=48 * 60, grouping="HALF_HOURLY")
        cal = costing.calibrate(buckets, day_rows, night_rows)
        print(f"payment    {cal.payment_method}")
        print(f"night      {cal.night_window}  ({len(cal.night_slots)} slots)")
        print(f"confident  {cal.confident}  - {cal.note}")

        day_rates = RateTimeline.from_records(day_rows, cal.payment_method)
        night_rates = RateTimeline.from_records(night_rows, cal.payment_method)
        standing = RateTimeline.from_records(standing_rows, cal.payment_method)
        print(f"day rate   {day_rates.latest:.3f}p inc VAT")
        print(f"night rate {night_rates.latest:.3f}p inc VAT")
        print(f"standing   {standing.latest:.3f}p/day inc VAT")

        print("\n=== settled history ===")
        readings = await client.consumption(point, history_start)
        print(f"{len(readings)} half-hourly records")
        totals = costing.cost_halfhourly(readings, cal, day_rates, night_rates, standing)
        print(f"{len(totals)} days, {totals[0].date} .. {totals[-1].date}")
        print(f"{'date':<12}{'kWh':>8}{'day':>8}{'night':>8}{'cost':>9}")
        for t in totals[-10:]:
            print(
                f"{t.date!s:<12}{t.total_kwh:>8.2f}{t.day_kwh:>8.2f}"
                f"{t.night_kwh:>8.2f}{'£%.2f' % (t.total_cost_p / 100):>9}"
            )

        span_kwh = sum(t.total_kwh for t in totals)
        span_cost = sum(t.total_cost_p for t in totals)
        print(f"\nlifetime   {span_kwh:.1f} kWh  £{span_cost / 100:.2f}")
        print(f"mean/day   {span_kwh / len(totals):.2f} kWh  "
              f"£{span_cost / len(totals) / 100:.2f}")

        print("\n=== today (live) ===")
        midnight = dt.datetime.now(UK).replace(hour=0, minute=0, second=0, microsecond=0)
        live = await client.telemetry(
            point.device_id, grouping="HALF_HOURLY",
            start_at=midnight.astimezone(dt.timezone.utc))
        today_totals = costing.cost_halfhourly(
            live, cal, day_rates, night_rates, None,
            start_key="readAt", value_key="consumptionDelta", scale=0.001)
        today = today_totals[-1] if today_totals else None
        if today:
            print(f"{today.total_kwh:.2f} kWh  day {today.day_kwh:.2f}  "
                  f"night {today.night_kwh:.2f}  £{today.usage_cost_p / 100:.2f} (usage only)")
            # Cross-check our costing against Octopus's own ex-VAT figure.
            theirs = sum(float(b.get("costDelta") or 0) for b in live)
            print(f"cross-check: ours ex-VAT £{today.usage_cost_p / 1.05 / 100:.2f} "
                  f"vs octopus £{theirs / 100:.2f}")

        print("\n=== forecast ===")
        fc = costing.forecast_month(totals, today, standing.latest or 0.0)
        if fc:
            print(f"month to date  £{fc.month_to_date_p / 100:.2f}  "
                  f"(day {fc.days_elapsed}/{fc.days_in_month})")
            print(f"mean daily     £{fc.mean_daily_p / 100:.2f} over {fc.basis_days} days")
            print(f"projected      £{fc.projected_p / 100:.2f}")

        print("\n=== agile counterfactual (last 30 days) ===")
        agile = await client.agile_rates(
            point.region, now - dt.timedelta(days=30), now + dt.timedelta(days=1)
        )
        print(f"{len(agile)} agile rate records")
        result = costing.agile_counterfactual(readings, agile)
        if result is None:
            print("no overlap between settled consumption and published agile rates")
        else:
            alt_p, matched = result
            # Cost the SAME half hours on the real tariff, else the comparison lies.
            actual = costing.cost_halfhourly(matched, cal, day_rates, night_rates, None)
            actual_p = sum(t.usage_cost_p for t in actual)
            kwh = sum(t.total_kwh for t in actual)
            days = len({_local_date(r) for r in matched})
            print(f"overlap    {len(matched)} half hours across {days} days, {kwh:.1f} kWh")
            print(f"actual     £{actual_p / 100:.2f}  (usage only, ex standing)")
            print(f"agile      £{alt_p / 100:.2f}")
            print(f"difference £{(alt_p - actual_p) / 100:+.2f} "
                  f"({'agile cheaper' if alt_p < actual_p else 'agile dearer'})")
    finally:
        await client.aclose()


asyncio.run(main())
