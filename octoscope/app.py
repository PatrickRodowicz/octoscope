"""OCTOSCOPE - Octopus Energy usage and spend dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter
from contextlib import contextmanager

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Label, RichLog

from . import cache, costing, db, migrate
from .api import MeterPoint, OctopusClient
from .config import (
    POLL_CONSUMPTION,
    POLL_TELEMETRY,
    PROVISIONAL_DAYS,
    TTL_CALIBRATION,
    TTL_TELEMETRY_TODAY,
    load_config,
)
from .costing import ROLLUPS, UK, Calibration, DayTotal, RateTimeline
from .store import TelemetryStore
from .store import best_source as store_best
from .store import reach as store_reach
from .widgets import (
    BillsPane,
    Column,
    ForecastTile,
    LiveView,
    MonthTile,
    NowTile,
    ReconcilePane,
    RollupTable,
    SpikeTable,
    SplitPane,
    TariffBreakdown,
    TariffTable,
    TodayTile,
    TrendChart,
)

# Activity indicator. Anything that can take more than an instant - a network
# round trip, a re-price across every tariff - runs inside `busy()` so the
# status line keeps moving and a stall is never mistaken for a crash.
#
# A block sweeping back and forth rather than a braille spinner: braille needs
# font coverage a terminal may not have, and renders as boxes or blanks when it
# does not. These are the same block glyphs the charts already draw, so if the
# dashboard is legible at all, so is this.
SPINNER_WIDTH = 12
SPINNER_BLOCK = 4
SPINNER_INTERVAL = 0.1


def _sweep(frame: int) -> str:
    """A `SPINNER_BLOCK`-wide bar bouncing across `SPINNER_WIDTH` cells."""
    span = SPINNER_WIDTH - SPINNER_BLOCK
    cycle = span * 2 or 1
    position = frame % cycle
    if position > span:
        position = cycle - position
    return "░" * position + "█" * SPINNER_BLOCK + "░" * (span - position)

# Trend periods, selectable with the number keys. A window ending now rather
# than a count of complete days: the last 12 hours are mostly unsettled, so a
# day-counting period could not express them at all.
PERIODS: list[tuple[str, dt.timedelta | None]] = [
    ("12 HOURS", dt.timedelta(hours=12)),
    ("24 HOURS", dt.timedelta(hours=24)),
    ("7 DAYS", dt.timedelta(days=7)),
    ("30 DAYS", dt.timedelta(days=30)),
    ("90 DAYS", dt.timedelta(days=90)),
    ("ALL", None),
]

# Chart granularities, cycled with `g`: label, rollup key, noun for "per X".
# Settled consumption arrives half-hourly, so 30 MIN is the meter's own
# resolution and everything coarser is a sum of it - nothing is interpolated.
GRAINS: list[tuple[str, str, str]] = [
    ("30 MIN", "30min", "half-hour"),
    ("HOUR", "60min", "hour"),
    ("DAY", "day", "day"),
    ("WEEK", "week", "week"),
    ("MONTH", "month", "month"),
]

# Live trace granularities: label, bucket seconds, API grouping, window minutes,
# cache seconds. Longer windows are cached harder because they move slowly and
# every call comes out of the 125/h telemetry budget.
LIVE_ROLLUPS: list[tuple[str, int, str, int, int]] = [
    ("10 SEC · 30 MIN", 10, "TEN_SECONDS", 30, 0),
    ("1 MIN · 2 HR", 60, "ONE_MINUTE", 120, 120),
    ("5 MIN · 6 HR", 300, "ONE_MINUTE", 360, 300),
    ("30 MIN · 24 HR", 1800, "HALF_HOURLY", 1440, 900),
]

# Tab cycles through these: view name, the pane it shows, and the widget that
# should take focus so the arrow keys act on what you are looking at. Views
# whose content is not navigable take focus away from the previous table, so a
# stray keypress cannot move a cursor that is no longer on screen.
VIEWS: list[tuple[str, str, str | None]] = [
    ("chart", "trend-pane", None),
    ("table", "table-pane", "table"),
    ("live", "live-pane", None),
    ("tariffs", "tariff-pane", "tariffs"),
]


class Octoscope(App):
    CSS_PATH = "app.tcss"
    TITLE = "OCTOSCOPE"

    # priority=True so these still fire when the DataTable has focus, and so
    # Tab reaches us rather than being eaten by focus navigation.
    BINDINGS = [
        Binding("q", "quit", "quit", priority=True),
        Binding("tab", "toggle_view", "view", priority=True),
        Binding("r", "refresh", "refresh", priority=True),
        Binding("1", "period(0)", "12h", priority=True),
        Binding("2", "period(1)", "24h", priority=True),
        Binding("3", "period(2)", "7d", priority=True),
        Binding("4", "period(3)", "30d", priority=True),
        Binding("5", "period(4)", "90d", priority=True),
        Binding("6", "period(5)", "all", priority=True),
        Binding("left", "scrub(1)", "back", priority=True),
        Binding("right", "scrub(-1)", "fwd", priority=True),
        Binding("shift+left", "nudge(1)", "step", priority=True),
        Binding("shift+right", "nudge(-1)", "step", priority=True),
        Binding("home", "scrub_now", "now", priority=True),
        Binding("g", "grain", "grain", priority=True),
        Binding("o", "toggle_reconcile", "vs live", priority=True),
        Binding("l", "toggle_log", "log", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.client = OctopusClient(self.config)
        self.point: MeterPoint | None = None
        self.calibration: Calibration = costing.DEFAULT_CALIBRATION
        self.day_rates = RateTimeline()
        self.night_rates = RateTimeline()
        self.standing = RateTimeline()
        self.readings: list[dict] = []
        self.totals: list[DayTotal] = []
        self.today: DayTotal | None = None
        self.provisional: list[DayTotal] = []
        self.provisional_readings: list[dict] = []
        self.period_index = 3  # 30 days
        self.grain_index = 2   # day
        # Settled readings with Home Mini filling anything Octopus has not
        # published. Every chart figure comes from this one series.
        self.pool: list[dict] = []
        self.provisional_dates: set[dt.date] = set()
        self.reconciling = False
        self.rollup_index = 3  # day
        self.view_index = 0
        self.live_rollup_index = 0
        self.live_offset = dt.timedelta(0)
        self.live_buckets: list[costing.PowerBucket] = []
        self.live_readings: list[dict] = []
        self.live_status = ""
        self.bills: list[dict] = []
        self._telemetry_failed = False
        self._series_lock = asyncio.Lock()
        self._filling: set[str] = set()
        self._busy: Counter[str] = Counter()
        self._spinner_frame = 0
        self._tariff_options: list[costing.TariffOption] | None = None
        self._tariff_results: list[costing.TariffResult] = []
        self._tariff_readings: list[dict] = []
        self._tariff_load: asyncio.Future | None = None

    # ---------------- layout ----------------

    def compose(self) -> ComposeResult:
        yield Label("", id="status")
        with Horizontal(id="tiles"):
            with Vertical(classes="pane"):
                yield Label("┤ NOW ├", classes="pane-title")
                yield NowTile(id="now")
            with Vertical(classes="pane"):
                yield Label("┤ TODAY ├", classes="pane-title")
                yield TodayTile(id="today")
            with Vertical(classes="pane"):
                yield Label("┤ THIS MONTH ├", classes="pane-title")
                yield MonthTile(id="month")
            with Vertical(classes="pane"):
                yield Label("┤ FORECAST ├", classes="pane-title")
                yield ForecastTile(id="forecast")
        with Vertical(classes="pane", id="trend-pane"):
            yield Label("┤ USAGE & SPEND ├", classes="pane-title", id="trend-title")
            yield TrendChart(id="trend")
            yield ReconcilePane(id="reconcile", classes="hidden")
        with Vertical(classes="pane hidden", id="table-pane"):
            yield Label("┤ ROLLUP ├", classes="pane-title", id="table-title")
            yield RollupTable(id="table")
        with Vertical(classes="pane hidden", id="live-pane"):
            yield Label("┤ LIVE ├", classes="pane-title", id="live-title")
            yield LiveView(id="live")
        with Vertical(classes="pane hidden", id="tariff-pane"):
            yield Label("┤ TARIFFS ├", classes="pane-title", id="tariff-title")
            yield TariffTable(id="tariffs")
            yield TariffBreakdown(id="tariff-detail")
        with Horizontal(id="lower"):
            with Vertical(classes="pane", id="split-pane"):
                yield Label("┤ DAY vs NIGHT ├", classes="pane-title")
                yield SplitPane(id="split")
            with Vertical(classes="pane", id="compare-pane"):
                yield Label("┤ BILLS vs COMPUTED ├", classes="pane-title")
                yield BillsPane(id="bills")
            with Vertical(classes="pane", id="spike-pane"):
                yield Label("┤ SPIKES ├", classes="pane-title", id="spike-title")
                yield SpikeTable(id="spikes")
            with Vertical(classes="pane hidden", id="log-pane"):
                yield Label("┤ EVENT LOG ├", classes="pane-title")
                yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer()

    async def on_mount(self) -> None:
        self.log_line("[dim]octoscope online[/dim]")
        imported = await asyncio.to_thread(migrate.run_once)
        if imported:
            self.log_line(
                f"[green]imported[/green] {imported['telemetry']} readings and "
                f"{imported['kv']} cache entries from .cache/"
            )
        counts = await asyncio.to_thread(db.stats)
        self.log_line(
            f"[dim]archive: {counts['telemetry']} readings, "
            f"{counts['consumption']} settled half-hours[/dim]"
        )
        self.set_interval(SPINNER_INTERVAL, self.tick_spinner)
        self.run_worker(self.bootstrap(), exclusive=False)

    async def on_unmount(self) -> None:
        await self.client.aclose()
        db.close()

    # ---------------- busy indicator ----------------

    @contextmanager
    def busy(self, label: str):
        """Mark `label` as in flight for as long as the block runs.

        Counted rather than boolean, because the same job can legitimately
        overlap itself - a poll landing while a keypress-triggered render of
        the same pane is still going - and the last one to finish must not
        clear the indicator for one still running.
        """
        self._busy[label] += 1
        self.tick_spinner()
        try:
            yield
        finally:
            if self._busy[label] <= 1:
                del self._busy[label]
            else:
                self._busy[label] -= 1
            self.tick_spinner()

    def tick_spinner(self) -> None:
        """Redraw the status line in place.

        The row is always present, never shown and hidden. Toggling `display`
        reflowed every pane below it, so the whole dashboard jumped each time
        any background job started or finished.
        """
        status = self.query_one("#status", Label)
        if not self._busy:
            self._spinner_frame = 0
            status.update("")
            return
        self._spinner_frame += 1
        bar = _sweep(self._spinner_frame)
        jobs = " · ".join(sorted(self._busy))
        status.update(f"[#00ff41]{bar}[/#00ff41]  [#b8e600]{jobs}[/#b8e600]")

    async def bootstrap(self) -> None:
        with self.busy("finding meter"):
            try:
                self.point = await self.client.discover()
            except Exception as exc:  # noqa: BLE001 - surfaced to the log
                self.log_line(f"[red]discovery failed:[/red] {exc}")
                return

        point = self.point
        self.log_line(f"[green]meter[/green] ...{point.serial[-4:]} region {point.region}")
        self.log_line(f"[green]tariff[/green] {point.tariff_code}")

        for label, step in (
            ("rates", self.load_rates),
            ("calibrating", self.calibrate),
            ("consumption", self.refresh_consumption),
            ("live meter", self.refresh_telemetry),
            ("bills", self.refresh_bills),
        ):
            with self.busy(label):
                await step()
        await self.report_limits()

        self.set_interval(POLL_TELEMETRY, self.refresh_telemetry)
        self.set_interval(POLL_CONSUMPTION, self.refresh_consumption)

    # ---------------- setup ----------------

    async def load_rates(self) -> None:
        """Fetch the full rate history so past consumption is priced correctly."""
        point = self.point
        if not point:
            return
        now = dt.datetime.now(dt.timezone.utc)
        start = point.moved_in or (now - dt.timedelta(days=365))
        horizon = now + dt.timedelta(days=2)
        try:
            if point.is_economy_7:
                self._day_rows = await self.client.unit_rate_records(
                    point.product_code, point.tariff_code, "day", start, horizon)
                self._night_rows = await self.client.unit_rate_records(
                    point.product_code, point.tariff_code, "night", start, horizon)
            else:
                self._day_rows = await self.client.unit_rate_records(
                    point.product_code, point.tariff_code, "standard", start, horizon)
                self._night_rows = []
            self._standing_rows = await self.client.standing_charge_records(
                point.product_code, point.tariff_code, start, horizon)
        except Exception as exc:  # noqa: BLE001
            self.log_line(f"[red]rates:[/red] {exc}")
            self._day_rows = self._night_rows = self._standing_rows = []

    async def calibrate(self) -> None:
        """Work out the payment method and Economy 7 night window from billing."""
        point = self.point
        if not point:
            return
        buckets = []
        if point.device_id:
            try:
                # Cached for half a day: the night window and payment method do
                # not move, and this is a 48-hour pull.
                buckets = await self.client.telemetry(
                    point.device_id, minutes=48 * 60, grouping="HALF_HOURLY",
                    cache_ttl=TTL_CALIBRATION,
                    cache_key=f"calibration-{point.device_id}")
            except Exception as exc:  # noqa: BLE001
                self.log_line(f"[yellow]calibration:[/yellow] {exc}")
        key = f"calibration-result-{point.device_id}"
        if buckets:
            self.calibration = costing.calibrate(buckets, self._day_rows, self._night_rows)
            if self.calibration.confident:
                cache.put(key, self.calibration.to_dict())
        else:
            # Telemetry unavailable (throttled or offline). A previously derived
            # calibration is far better than falling back to assumptions.
            stored = cache.get_stale(key)
            self.calibration = (
                costing.Calibration.from_dict(stored) if stored
                else costing.DEFAULT_CALIBRATION
            )
            if stored:
                self.log_line("[dim]calibration: reused last known[/dim]")
        self.day_rates = RateTimeline.from_records(self._day_rows, self.calibration.payment_method)
        self.night_rates = RateTimeline.from_records(
            self._night_rows, self.calibration.payment_method)
        self.standing = RateTimeline.from_records(
            self._standing_rows, self.calibration.payment_method)

        if self.day_rates.latest is None:
            self.log_line("[yellow]no rates published[/yellow]")
            return
        method = self.calibration.payment_method.lower().replace("_", " ")
        self.log_line(f"[green]billing[/green] {method}")
        self.log_line(
            f"[green]rates[/green] day {self.day_rates.latest:.2f}p "
            f"night {self.night_rates.latest:.2f}p"
        )
        window = self.calibration.night_window
        if window:
            flag = "" if self.calibration.confident else " [yellow](assumed)[/yellow]"
            self.log_line(f"[green]night[/green] {window[0]}-{window[1]}{flag}")

    # ---------------- refreshers ----------------

    async def refresh_consumption(self) -> None:
        point = self.point
        if not point:
            return
        now = dt.datetime.now(dt.timezone.utc)
        start = point.moved_in or (now - dt.timedelta(days=365))
        with self.busy("consumption"):
            try:
                self.readings = await self.client.consumption(point, start)
            except Exception as exc:  # noqa: BLE001
                self.log_line(f"[red]consumption:[/red] {exc}")
                return
            # Fresh readings may extend the span the tariff timelines cover.
            self._tariff_options = None
            self._tariff_load = None
            self.totals = await asyncio.to_thread(
                costing.cost_halfhourly, self.readings, self.calibration,
                self.day_rates, self.night_rates, self.standing)
            self._rebuild_pool()
        self.log_line(f"[dim]settled consumption: {len(self.totals)} days[/dim]")
        self.render_period()

    async def refresh_telemetry(self) -> None:
        point = self.point
        if not point or not point.device_id:
            return
        now_local = dt.datetime.now(UK)
        is_night = self.calibration.is_night(now_local)
        rate = (self.night_rates if is_night else self.day_rates).at(
            dt.datetime.now(dt.timezone.utc))

        if self.client.graphql_cooling_down:
            self.live_status = "telemetry paused - rate limited"
            self.query_one("#now", NowTile).show_waiting(self.live_status)
            if self.view == "live":
                self.run_worker(self.render_live(), group="live")
            return  # already reported; stay quiet until the cooldown expires
        try:
            live = await self.client.telemetry(point.device_id, minutes=30)
            self._telemetry_failed = False
            self.live_status = ""
        except Exception as exc:  # noqa: BLE001
            # Log the first failure only, so a throttle does not fill the log
            # with one line every poll.
            if not self._telemetry_failed:
                self.log_line(f"[red]telemetry:[/red] {exc}")
                self._telemetry_failed = True
            self.live_status = str(exc)
            self.query_one("#now", NowTile).show_waiting("telemetry unavailable")
            if self.view == "live":
                self.run_worker(self.render_live(), group="live")
            return
        self.live_readings = live
        self.query_one("#now", NowTile).update_live(live, rate, is_night)
        if self.view == "live":
            self.run_worker(self.render_live(), group="live")

        # Today's figures come from telemetry, priced with our own inc-VAT
        # rates so they agree with every other number on screen. The window
        # reaches back far enough to also cover any recent day settled billing
        # has not finished publishing - same single call, just a wider span.
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = min(midnight, self._provisional_from(now_local))
        try:
            # Half-hourly buckets only change twice an hour, so there is no
            # point refetching this on every 30-second live tick.
            buckets = await self.client.telemetry(
                point.device_id, grouping="HALF_HOURLY",
                start_at=start_local.astimezone(dt.timezone.utc),
                cache_ttl=TTL_TELEMETRY_TODAY,
                cache_key=f"today-{point.device_id}-{start_local:%Y-%m-%d}-{now_local:%Y-%m-%d}")
        except Exception:  # noqa: BLE001 - non-fatal
            return
        days = costing.cost_halfhourly(
            buckets, self.calibration, self.day_rates, self.night_rates, self.standing,
            start_key="readAt", value_key="consumptionDelta", scale=0.001)
        self.today = days[-1] if days else None
        # Everything before today is a candidate to stand in for settled data.
        self.provisional = [d for d in days if d.date < now_local.date()]
        self.provisional_readings = buckets
        self._rebuild_pool()
        if self.today is not None:
            # Today's standing charge is incurred in full regardless of the
            # hour, so the tile can show a like-for-like total.
            self.today.standing_p = self.standing.latest or 0.0
        self.render_today()
        if self.provisional:
            self.render_period()
            self.render_month()

    def _rebuild_pool(self) -> None:
        """Refresh the merged reading series both sources feed into."""
        self.pool, self.provisional_dates = costing.merged_readings(
            self.readings, self.provisional_readings)

    def _provisional_from(self, now_local: dt.datetime) -> dt.datetime:
        """Midnight of the oldest recent day settled data has not finished.

        Bounded by PROVISIONAL_DAYS because half-hourly telemetry only reaches
        back so far, and a wider span is a bigger, slower payload for data we
        would throw away.
        """
        floor = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        earliest = floor - dt.timedelta(days=PROVISIONAL_DAYS)
        gaps = [
            t.date for t in self.totals
            if t.partial and t.date < now_local.date() and t.date >= earliest.date()
        ]
        if not gaps:
            return floor
        return dt.datetime.combine(min(gaps), dt.time(0), tzinfo=UK)

    # ---------------- rendering ----------------

    @property
    def merged_totals(self) -> list[DayTotal]:
        """Settled days, with unfinished ones filled in from the Home Mini."""
        return costing.merge_provisional(self.totals, self.provisional)

    @property
    def complete_totals(self) -> list[DayTotal]:
        """Days good enough to average - settled, or provisionally complete."""
        return [t for t in self.merged_totals if not t.partial]

    def render_period(self, reset_scroll: bool = False) -> None:
        label, span = PERIODS[self.period_index]
        grain_label, grain, unit = GRAINS[self.grain_index]

        columns = self._columns(span, grain)
        # Scroll position survives a background refresh - a telemetry poll must
        # not yank you back to now while you are reading last month - but a
        # deliberate change of period or grain starts from the latest data.
        self.query_one("#trend", TrendChart).update_columns(
            columns, label, grain=grain_label, unit=unit,
            reset_scroll=reset_scroll)
        if not self.reconciling:
            # The overlay owns the title while it is up, so a background
            # refresh cannot relabel the pane with the chart it is hiding.
            self.query_one("#trend-title", Label).update(
                f"┤ USAGE & SPEND · {label} · {grain_label} ├"
                "   [dim](1-6 period · g grain · o settled vs live)[/dim]"
            )
        # Day vs night is a property of the day however the chart is sliced, so
        # this pane stays on whole days regardless of the selected granularity.
        self.query_one("#split", SplitPane).update_split(
            self._split_days(span), self.calibration.night_window,
            self.calibration.confident,
            day_rate=self.day_rates.latest,
            night_rate=self.night_rates.latest,
            standing=self.standing.latest)
        self.render_month()
        if self.reconciling:
            self.render_reconcile()

    def _columns(self, span: dt.timedelta | None, grain: str) -> list[Column]:
        """Bucket the merged reading pool for `span` at `grain`.

        One code path for every period and grain. Each reading falls in exactly
        one bucket whatever the grain, so the period total is the same however
        it is sliced - which is the property that kept breaking when days and
        sub-day buckets were assembled separately.
        """
        pool = self.pool
        if span is not None:
            cutoff = dt.datetime.now(UK) - span
            pool = [
                r for r in pool
                if (when := costing.parse_time(r.get("interval_start"))) is not None
                and when >= cutoff
            ]
        if not pool:
            return []
        buckets = costing.rollup(
            pool, grain, self.calibration, self.day_rates, self.night_rates,
            self.standing)
        columns = [Column.from_bucket(b, grain) for b in buckets]
        # Flag the bars whose energy came from the meter rather than a bill.
        # Only meaningful per day or finer; a week straddles both sources.
        if grain in ("30min", "60min", "day"):
            provisional = self.provisional_dates
            for column in columns:
                if column.start.astimezone(UK).date() in provisional:
                    column.provisional = True
        return columns

    def _split_days(self, span: dt.timedelta | None) -> list[DayTotal]:
        """Whole days in the period, for the day-vs-night pane.

        A sub-day period contains no whole day, and the split of a half
        finished one says nothing. Rather than blanking the pane, fall back to
        the most recent complete day - which is the like-for-like comparison
        someone looking at the last 12 hours actually wants.
        """
        complete = self.complete_totals
        if span is None:
            return complete
        cutoff = (dt.datetime.now(UK) - span).date()
        window = [t for t in complete if t.date >= cutoff]
        return window or complete[-1:]

    def render_reconcile(self) -> None:
        """Check the Home Mini's record against settled billing.

        Reads only what is already on disk - the telemetry archive plus whatever
        provisional buckets this session fetched - so opening the overlay never
        costs an API call. If the two sources happen not to overlap, it says so
        rather than fetching to manufacture an answer.
        """
        rows: list[dict] = list(self.provisional_readings)
        seen = {r.get("readAt") for r in rows}
        point = self.point
        if point and point.device_id:
            held = TelemetryStore(point.device_id, "HALF_HOURLY")
            rows.extend(r for r in held.all() if r.get("readAt") not in seen)

        result = costing.reconcile(self.readings, rows)
        # Octopus only serves about six days of half-hourly telemetry, but the
        # archive keeps every day it has ever seen - so this comparison widens
        # by a day for each day the app is run, rather than sliding.
        reach = (
            f"{len(result)} days held locally; Octopus itself only serves about six"
        )
        self.query_one("#reconcile", ReconcilePane).update_rows(result, reach)

    def render_today(self) -> None:
        now = dt.datetime.now(UK)
        yesterday = now.date() - dt.timedelta(days=1)
        yesterday_by_now = costing.kwh_up_to(self.readings, yesterday, now)
        settled_yesterday = next((t for t in self.totals if t.date == yesterday), None)
        if settled_yesterday is None or settled_yesterday.partial:
            # Settled data has not finished yesterday, so comparing against it
            # would report a huge false jump. Use the meter's own record.
            from_meter = costing.kwh_up_to(
                self.provisional_readings, yesterday, now,
                start_key="readAt", value_key="consumptionDelta", scale=0.001)
            if from_meter is not None:
                yesterday_by_now = from_meter
        self.query_one("#today", TodayTile).update_today(self.today, yesterday_by_now)
        self.render_month()
        # Today's bar lives on the trend chart too, so redraw it.
        self.render_period()

    def render_month(self) -> None:
        if not self.totals:
            return
        forecast = costing.forecast_month(
            self.merged_totals, self.today, self.standing.latest or 0.0)
        month_start = dt.datetime.now(UK).date().replace(day=1)
        month_days = [t for t in self.complete_totals if t.date >= month_start]
        kwh = sum(t.total_kwh for t in month_days)
        if self.today:
            kwh += self.today.total_kwh
        standing_total = (self.standing.latest or 0.0) * (
            forecast.days_elapsed if forecast else len(month_days))

        self.query_one("#month", MonthTile).update_month(forecast, kwh, standing_total)
        self.query_one("#forecast", ForecastTile).update_forecast(forecast)

    async def render_table(self) -> None:
        """Build the rollup table for the selected granularity.

        Sub-hour rollups need per-minute telemetry from the Home Mini; 30
        minutes and coarser come from settled consumption, which reaches much
        further back.
        """
        period = list(ROLLUPS)[self.rollup_index]
        table = self.query_one("#table", RollupTable)
        point = self.point
        if not point:
            return

        if period == "5min":
            if not point.device_id:
                self.query_one("#table-title", Label).update("┤ ROLLUP · 5 MIN ├  no home mini")
                table.update_buckets([], period)
                return
            try:
                with self.busy("5-min rollup"):
                    readings = await self.client.telemetry(
                        point.device_id, minutes=6 * 60, grouping="ONE_MINUTE",
                        cache_ttl=TTL_TELEMETRY_TODAY,
                        cache_key=f"minute-{point.device_id}")
            except Exception as exc:  # noqa: BLE001
                self.log_line(f"[red]5-min rollup:[/red] {exc}")
                table.update_buckets([], period)
                return
            buckets = costing.rollup(
                readings, period, self.calibration, self.day_rates, self.night_rates,
                self.standing, start_key="readAt", value_key="consumptionDelta",
                scale=0.001, interval_minutes=1.0)
            source = "home mini · last 6h"
        else:
            buckets = costing.rollup(
                self.readings, period, self.calibration, self.day_rates,
                self.night_rates, self.standing, interval_minutes=30.0)
            source = "settled consumption"
            if period in ("day", "month"):
                # Today's settled share, so it can be swapped for live telemetry.
                today_only = [
                    r for r in self.readings
                    if costing.same_local_day(r.get("interval_start"))
                ]
                settled_today = costing.rollup(
                    today_only, "day", self.calibration, self.day_rates,
                    self.night_rates, self.standing, interval_minutes=30.0)
                buckets = costing.patch_today(
                    buckets, self.today,
                    settled_today[0] if settled_today else None, period)
                source = "settled + live today"

        label = ROLLUPS[period]
        self.query_one("#table-title", Label).update(
            f"┤ ROLLUP · {label} ├   [dim]{source} · cost inc standing · "
            f"1-5 to change · tab for chart[/dim]"
        )
        table.update_buckets(buckets, period)

    async def render_live(self) -> None:
        point = self.point
        now_local = dt.datetime.now(UK)
        is_night = self.calibration.is_night(now_local)
        rate = (self.night_rates if is_night else self.day_rates).at(
            dt.datetime.now(dt.timezone.utc))
        label, seconds, grouping, minutes, ttl = LIVE_ROLLUPS[self.live_rollup_index]

        live_now = self.live_offset == dt.timedelta(0)
        # Always the live feed, whatever the chart is showing.
        readings = self.live_readings
        status = self.live_status if live_now else ""

        if live_now and self.live_rollup_index == 0:
            source = self.live_readings
        else:
            source = []
            if point and point.device_id:
                now = dt.datetime.now(dt.timezone.utc)
                end = now - self.live_offset
                # Snap the window to the bucket grid so the same instants are
                # always requested, whatever route the user took to get here.
                epoch = int(end.timestamp())
                end = dt.datetime.fromtimestamp(epoch - epoch % seconds, dt.timezone.utc)
                start = end - dt.timedelta(minutes=minutes)
                try:
                    # A coarser series is a summary of a finer one, so if the
                    # archive already holds this window at higher resolution,
                    # use that instead of asking Octopus for the digest. Costs
                    # nothing, and resolves demand far better - see
                    # store.best_source.
                    finer = store_best(point.device_id, grouping, start, end)
                    if finer:
                        source = TelemetryStore(point.device_id, finer).slice(start, end)
                    else:
                        source = await self.load_series(
                            point.device_id, grouping, start, end, live_now)
                except Exception as exc:  # noqa: BLE001
                    status = str(exc)

        self.live_buckets = costing.aggregate_power(source, seconds)
        self.query_one("#live", LiveView).update_live(
            readings, self.live_buckets, rate, is_night, self.today, status)

        budget = self.client.budget
        if live_now:
            position = f"refresh {POLL_TELEMETRY}s"
        else:
            end_local = (dt.datetime.now(UK) - self.live_offset)
            position = f"[yellow]scrolled back to {end_local:%d %b %H:%M}[/yellow] · home=now"
        spent = ""
        if budget.used and budget.resets_at:
            spent = f" · frees at {budget.resets_at.astimezone(UK):%H:%M}"
        self.query_one("#live-title", Label).update(
            f"┤ LIVE · {label} ├   [dim]1-4 granularity · ←→ scroll · {position} · "
            f"API budget {budget.remaining}/{budget.per_hour}{spent}[/dim]"
        )
        self.render_spikes()

    async def load_series(
        self,
        device_id: str,
        grouping: str,
        start: dt.datetime,
        end: dt.datetime,
        live_now: bool,
    ) -> list[dict]:
        """Serve a window from the archive at once, filling any holes behind it.

        Scrolling must not wait on the network. A gap is padded backwards into
        as much history as one call will carry, which is the right thing for the
        hourly budget - one request buys eleven hours instead of one bucket -
        but it means a 90-second hole triggers a 3,600-row, half-megabyte
        request. Blocking the render on that made scrolling feel broken while
        the archive it was waiting for already held almost all of the answer.

        So the query returns immediately and the fetch runs in a worker, which
        re-renders when it lands. Reading is sub-millisecond; only genuinely
        absent hours ever involve Octopus.

        The archive outlives the API's retention, so a window can legitimately
        reach back further than Octopus will serve. Those hours are answered
        from storage rather than asked for and refused.
        """
        series = TelemetryStore(device_id, grouping)
        now = dt.datetime.now(dt.timezone.utc)
        gaps = series.missing(start, end)
        if live_now and gaps:
            # The trailing edge is always "missing" because time moves; only
            # refetch it once the stored tail is genuinely stale.
            gaps = [g for g in gaps if (g[1] - g[0]) >= dt.timedelta(seconds=90)]

        # Plus anything covered but empty that the meter may have since
        # uploaded - see TelemetryStore.stale_empty.
        plan = sorted(series.fetchable(gaps, now) + series.stale_empty(start, end, now))
        if plan:
            self._schedule_fill(device_id, grouping, plan)
        return series.slice(start, end)

    def _schedule_fill(
        self, device_id: str, grouping: str,
        plan: list[tuple[dt.datetime, dt.datetime]],
    ) -> None:
        """Queue a background backfill, one per granularity at a time.

        Without the guard every render of a window containing a hole would start
        another fetch for the same hole - and the 60-second poll re-renders the
        live view on its own, so a single unfillable gap would spend the entire
        hourly budget on it.
        """
        if grouping in self._filling:
            return
        self._filling.add(grouping)
        self.run_worker(
            self._fill_gaps(device_id, grouping, plan), group="fill", exclusive=False
        )

    async def _fill_gaps(
        self, device_id: str, grouping: str,
        plan: list[tuple[dt.datetime, dt.datetime]],
    ) -> None:
        series = TelemetryStore(device_id, grouping)
        try:
            # Serialised: holding rapid keypresses would otherwise start several
            # overlapping requests, and a cancelled one still costs its slot in
            # the hourly budget while storing nothing.
            async with self._series_lock:
                now = dt.datetime.now(dt.timezone.utc)
                for gap in plan:
                    fetch_start, fetch_end = series.widen(gap, now)
                    with self.busy("filling meter history"):
                        # Archived by the client as it lands, so nothing here
                        # has to remember to write it down.
                        rows = await self.client.telemetry(
                            device_id, grouping=grouping,
                            start_at=fetch_start, end_at=fetch_end)
                    self.log_line(
                        f"[dim]filled {grouping.lower()} "
                        f"{fetch_start.astimezone(UK):%H:%M}-{fetch_end.astimezone(UK):%H:%M}"
                        f" ({len(rows)} rows, archive {series.size})[/dim]"
                    )
        except Exception as exc:  # noqa: BLE001 - the view already has what it had
            self.log_line(f"[yellow]backfill {grouping.lower()}:[/yellow] {exc}")
        finally:
            self._filling.discard(grouping)
        # Redraw with whatever arrived. Safe against looping: the range is now
        # covered, and add_coverage stamps it as freshly fetched.
        if self.view == "live":
            self.run_worker(self.render_live(), group="live")

    def render_spikes(self) -> None:
        """Pick bursts out of whatever window the live view is showing."""
        def rate_at(when: dt.datetime) -> float | None:
            local = when.astimezone(UK)
            timeline = self.night_rates if self.calibration.is_night(local) else self.day_rates
            return timeline.at(when)

        spikes = costing.find_spikes(self.live_buckets, rate_at)
        baseline = spikes[0].baseline_watts if spikes else None
        self.query_one("#spikes", SpikeTable).update_spikes(spikes, baseline)
        total = sum(s.cost_p for s in spikes)
        label = LIVE_ROLLUPS[self.live_rollup_index][0]
        self.query_one("#spike-title", Label).update(
            f"┤ SPIKES ├ [dim]{len(spikes)} in {label.split('·')[-1].strip()}"
            + (f" · {_money_p(total)}[/dim]" if spikes else "[/dim]")
        )

    async def render_tariffs(self) -> None:
        """Price the selected period on every comparable Octopus tariff."""
        point = self.point
        if not point or not self.readings:
            return
        label, span = PERIODS[self.period_index]
        cutoff = dt.datetime.now(dt.timezone.utc) - span if span else None
        readings = [
            r for r in self.readings
            if cutoff is None or (r.get("interval_start") or "") >= cutoff.isoformat()
        ]
        if not readings:
            return

        self.query_one("#tariff-title", Label).update(
            f"┤ TARIFFS · {label} ├   [dim]pricing your actual usage...[/dim]"
        )
        with self.busy(f"pricing {label.lower()}"):
            options = await self.load_tariff_options()
            # Pricing a year against Agile's half-hourly rates is real work, so
            # keep it off the event loop - a frozen UI reads as a crash.
            results = await asyncio.to_thread(
                self._price_all, options, readings, self.calibration)
        if not results:
            return

        current = next((r for r in results if r.option.is_current), None)
        results.sort(key=lambda r: r.total_cost_p)
        self._tariff_results = results
        self._tariff_readings = readings
        table = self.query_one("#tariffs", TariffTable)
        shapes = {
            r.option.code: costing.schedule_label(r.option, self.calibration)
            for r in results
        }
        table.update_results(
            results, current.total_cost_p if current else None, shapes)
        # Keep the breakdown pointed at whatever row is highlighted; the period
        # just changed underneath it, so its numbers are stale.
        await self.show_tariff_detail(table.cursor_row)
        kwh = results[0].kwh
        self.query_one("#tariff-title", Label).update(
            f"┤ TARIFFS · {label} ├   [dim]{kwh:.0f} kWh over {results[0].days} days · "
            f"inc standing · ↑↓ to explain a row · 1-4 period[/dim]"
        )

    async def on_data_table_row_highlighted(self, event) -> None:
        """Explain whichever comparison row the cursor is on."""
        if event.data_table.id == "tariffs":
            await self.show_tariff_detail(event.cursor_row)

    async def show_tariff_detail(self, row: int) -> None:
        """Break down one candidate tariff against the current one."""
        pane = self.query_one("#tariff-detail", TariffBreakdown)
        results = self._tariff_results
        if not results or not (0 <= row < len(results)):
            pane.show_detail(None, "")
            return
        candidate = results[row].option
        current = next((r.option for r in results if r.option.is_current), None)
        if current is None:
            pane.show_detail(None, "")
            return
        label = PERIODS[self.period_index][0]
        if candidate.is_current:
            pane.show_detail(
                costing.TariffDetail(
                    option=candidate, current=current, days=[], usage_p=0.0,
                    current_usage_p=0.0, standing_p=0.0, current_standing_p=0.0,
                    rates="", current_rates="",
                ),
                label,
            )
            return
        with self.busy(f"explaining {candidate.name.lower()}"):
            detail = await asyncio.to_thread(
                costing.compare_tariffs, candidate, current,
                self._tariff_readings, self.calibration)
        pane.show_detail(detail, label)

    @staticmethod
    def _price_all(
        options: list[costing.TariffOption],
        readings: list[dict],
        calibration: Calibration,
    ) -> list[costing.TariffResult]:
        """Price one set of readings on every option. Runs in a worker thread."""
        priced = (costing.cost_on_tariff(o, readings, calibration) for o in options)
        return [result for result in priced if result is not None]

    async def load_tariff_options(self) -> list[costing.TariffOption]:
        """Build a priced timeline for each comparable product, once.

        Built over the full history rather than per period: the timelines are
        the same either way, a shorter period simply looks up fewer of them.
        Rebuilding per period meant re-fetching and re-parsing every product's
        rates on each 1-4 keypress.

        The work runs in a shielded task shared by every caller. The tariff
        render is an exclusive worker, so holding 1-4 cancels it mid-fetch;
        without the shield each press would abandon a part-finished load and
        start another, and the options would never finish building at all.
        """
        if self._tariff_options is not None:
            return self._tariff_options
        if self._tariff_load is None:
            self._tariff_load = asyncio.ensure_future(self._load_tariff_options())
        try:
            return await asyncio.shield(self._tariff_load)
        except Exception:  # noqa: BLE001 - logged inside; let the render bail
            self._tariff_load = None
            raise

    async def _load_tariff_options(self) -> list[costing.TariffOption]:
        point = self.point
        if not point:
            return []
        first = costing._parse((self.readings[0] if self.readings else {}).get("interval_start"))
        start = first or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90))
        horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        options: list[costing.TariffOption] = []
        for code in self.client.COMPARABLE_PRODUCTS:
            with self.busy(f"rates · {_product_name(code).lower()}"):
                found = await self.client.tariff_code_for(code, point.region)
                if not found:
                    continue
                tariff_code, dual = found
                try:
                    if dual:
                        day = await self.client.unit_rate_records(
                            code, tariff_code, "day", start, horizon)
                        night = await self.client.unit_rate_records(
                            code, tariff_code, "night", start, horizon)
                    else:
                        day = await self.client.unit_rate_records(
                            code, tariff_code, "standard", start, horizon)
                        night = []
                    standing = await self.client.standing_charge_records(
                        code, tariff_code, start, horizon)
                except Exception as exc:  # noqa: BLE001 - skip products that error
                    self.log_line(f"[yellow]{code}:[/yellow] {exc}")
                    continue
                # A year of Agile is ~17,500 rate rows to parse; off the loop.
                options.append(
                    await asyncio.to_thread(
                        self._build_option, code, day, night, standing,
                        self.calibration.payment_method,
                        code == point.product_code,
                    )
                )
        self._tariff_options = options
        return options

    @staticmethod
    def _build_option(
        code: str, day: list[dict], night: list[dict], standing: list[dict],
        method: str, is_current: bool,
    ) -> costing.TariffOption:
        return costing.TariffOption(
            code=code,
            name=_product_name(code),
            unit=RateTimeline.from_records(day, method),
            night=RateTimeline.from_records(night, method) if night else None,
            standing=RateTimeline.from_records(standing, method),
            is_current=is_current,
        )

    async def report_limits(self) -> None:
        """Log the API's own view of our rate-limit standing."""
        try:
            info = await self.client.rate_limit_info()
        except Exception:  # noqa: BLE001 - purely diagnostic
            return
        points = info.get("points") or {}
        limit = self.client.telemetry_limit(info)
        if points.get("limit"):
            self.log_line(
                f"[dim]points {points.get('remainingPoints'):,}/{points.get('limit'):,}"
                f" left[/dim]"
            )
        if limit:
            state = "[red]BLOCKED[/red]" if limit.get("isBlocked") else "ok"
            budget = self.client.budget
            self.log_line(
                f"[dim]telemetry limit {limit.get('rate')} · {state} · "
                f"API budget {budget.remaining}/{budget.per_hour} left[/dim]"
            )
            if limit.get("isBlocked") and limit.get("ttl"):
                until = dt.datetime.fromtimestamp(limit["ttl"], dt.timezone.utc)
                self.client._cooldown_until = until
                self.log_line(
                    f"[yellow]telemetry blocked until "
                    f"{until.astimezone(UK):%H:%M:%S}[/yellow]"
                )

    async def refresh_bills(self) -> None:
        try:
            self.bills = await self.client.bills()
        except Exception as exc:  # noqa: BLE001
            self.log_line(f"[yellow]bills:[/yellow] {exc}")
            return
        self.render_bills()

    def render_bills(self) -> None:
        """Compare each issued statement with what this app computes."""
        computed: dict[str, float] = {}
        for bill in self.bills:
            start, end = bill.get("fromDate"), bill.get("toDate")
            if not start or not end:
                continue
            window = [
                r for r in self.readings
                if start <= (r.get("interval_start") or "")[:10] <= end
            ]
            if not window:
                continue
            days = costing.cost_halfhourly(
                window, self.calibration, self.day_rates, self.night_rates, self.standing)
            # Only claim a comparison when the settled data actually covers the
            # whole billed period; a partial overlap would look like an error.
            billed_days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
            if len(days) >= billed_days - 1:
                computed[f"{start}:{end}"] = sum(d.total_cost_p for d in days)
        self.query_one("#bills", BillsPane).update_bills(self.bills, computed)

    # ---------------- actions ----------------

    def action_toggle_view(self) -> None:
        self.view_index = (self.view_index + 1) % len(VIEWS)
        for name, pane, _ in VIEWS:
            self.query_one(f"#{pane}").set_class(name != self.view, "hidden")
        self.focus_view()
        self.refresh_view()

    def focus_view(self) -> None:
        """Put the cursor on the view being shown, so arrows just work."""
        target = VIEWS[self.view_index][2]
        self.set_focus(self.query_one(f"#{target}") if target else None)

    @property
    def view(self) -> str:
        return VIEWS[self.view_index][0]

    def refresh_view(self) -> None:
        if self.view == "chart":
            self.render_period()
        elif self.view == "table":
            # Scoped to its own group: a bare exclusive=True would cancel the
            # bootstrap worker still fetching rates and comparison data.
            self.run_worker(self.render_table(), exclusive=True, group="view")
        elif self.view == "live":
            # Not exclusive: cancelling a live render would abandon an
            # in-flight fetch already charged against the budget.
            self.run_worker(self.render_live(), group="live")
        else:
            self.run_worker(self.render_tariffs(), exclusive=True, group="view")

    def action_period(self, index: int) -> None:
        if self.view == "table":
            if index >= len(ROLLUPS):
                return
            self.rollup_index = index
            self.run_worker(self.render_table(), exclusive=True, group="view")
            return
        if self.view == "live":
            if index >= len(LIVE_ROLLUPS):
                return
            self.live_rollup_index = index
            self.run_worker(self.render_live(), group="live")
            return
        if index >= len(PERIODS):
            return
        self.period_index = index
        self._fit_grain()
        if self.view == "tariffs":
            self.run_worker(self.render_tariffs(), exclusive=True, group="view")
        else:
            self.render_period(reset_scroll=True)

    async def action_refresh(self) -> None:
        self.log_line("[dim]manual refresh[/dim]")
        await self.refresh_consumption()
        await self.refresh_telemetry()

    # Roughly how long each grain's bucket is, for deciding whether a period
    # can sensibly be drawn at it.
    _GRAIN_SPAN = {
        "30min": dt.timedelta(minutes=30),
        "60min": dt.timedelta(hours=1),
        "day": dt.timedelta(days=1),
        "week": dt.timedelta(days=7),
        "month": dt.timedelta(days=30),
    }
    _MIN_BARS = 4

    def _fit_grain(self) -> None:
        """Step to a finer grain if the new period would be a couple of bars.

        Picking 12 HOURS while the chart is on MONTH would otherwise draw a
        single block. Only ever refines, and only on a period change - cycling
        `g` afterwards still goes wherever you send it.
        """
        span = PERIODS[self.period_index][1]
        if span is None:
            return
        while self.grain_index > 0:
            bucket = self._GRAIN_SPAN[GRAINS[self.grain_index][1]]
            if span >= bucket * self._MIN_BARS:
                return
            self.grain_index -= 1

    def action_grain(self) -> None:
        """Cycle the chart's granularity, coarsest-to-finest and round again."""
        self.grain_index = (self.grain_index + 1) % len(GRAINS)
        self.render_period(reset_scroll=True)

    def action_toggle_reconcile(self) -> None:
        """Overlay the home mini against settled billing, and back."""
        self.reconciling = not self.reconciling
        # A swap inside a fixed-height pane, so nothing below it moves.
        self.query_one("#trend").set_class(self.reconciling, "hidden")
        self.query_one("#reconcile").set_class(not self.reconciling, "hidden")
        title = self.query_one("#trend-title", Label)
        if self.reconciling:
            self.render_reconcile()
            title.update(
                "┤ SETTLED vs HOME MINI ├   [dim](o to return to the chart)[/dim]")
        else:
            self.render_period()

    def action_toggle_log(self) -> None:
        """Swap the spikes pane for the diagnostic log, and back."""
        showing_log = self.query_one("#log-pane").has_class("hidden")
        self.query_one("#log-pane").set_class(not showing_log, "hidden")
        self.query_one("#spike-pane").set_class(showing_log, "hidden")

    def action_scrub(self, direction: int) -> None:
        """Pan the window back or forward by half its width.

        Same keys on both scrollable charts. The live view moves a time window
        and may fetch; the trend chart moves over data already in hand, so it
        is purely local.
        """
        if self.view == "chart":
            self._scroll_trend(direction, page=True)
            return
        _, _, _, minutes, _ = LIVE_ROLLUPS[self.live_rollup_index]
        self._shift_window(dt.timedelta(minutes=minutes / 2) * direction)

    def action_nudge(self, direction: int) -> None:
        """Step by a single bucket, for lining the window up on an event."""
        if self.view == "chart":
            self._scroll_trend(direction, page=False)
            return
        _, seconds, _, _, _ = LIVE_ROLLUPS[self.live_rollup_index]
        self._shift_window(dt.timedelta(seconds=seconds) * direction)

    def _scroll_trend(self, direction: int, page: bool) -> None:
        """Scroll the trend chart; `direction` is positive for back in time."""
        if self.reconciling:
            return
        chart = self.query_one("#trend", TrendChart)
        step = chart.page if page else 1
        if chart.scroll_columns(step * direction):
            return
        if chart.max_column_offset == 0:
            self.log_line("[dim]the whole period already fits on screen[/dim]")
        elif direction > 0:
            grain = GRAINS[self.grain_index][0].lower()
            self.log_line(
                f"[yellow]start of the selected period - 1-4 for more "
                f"history, or a coarser grain than {grain}[/yellow]")
        else:
            self.log_line("[dim]already at the latest data[/dim]")

    def _shift_window(self, delta: dt.timedelta) -> None:
        if self.view != "live":
            return
        _, _, grouping, minutes, _ = LIVE_ROLLUPS[self.live_rollup_index]
        # Bounded by what can be *shown*, not by what Octopus will still serve.
        # The archive keeps ten-second data long after the API's 12-hour window
        # has closed over it, so the limit grows as history accumulates.
        device_id = self.point.device_id if self.point else ""
        available = store_reach(device_id, grouping)
        limit = max(dt.timedelta(0), available - dt.timedelta(minutes=minutes))
        wanted = self.live_offset + delta
        self.live_offset = min(max(dt.timedelta(0), wanted), limit)
        if wanted > limit:
            self.log_line(
                f"[yellow]{grouping.lower().replace('_', ' ')} history goes back "
                f"{int(available.total_seconds() // 3600)}h[/yellow]"
            )
        # Not exclusive: cancelling a live render would abandon an in-flight
        # telemetry fetch that has already been charged against the budget.
        self.run_worker(self.render_live(), group="live")

    def action_scrub_now(self) -> None:
        if self.view == "chart":
            if not self.reconciling:
                self.query_one("#trend", TrendChart).scroll_home()
            return
        self.live_offset = dt.timedelta(0)
        # Not exclusive: cancelling a live render would abandon an in-flight
        # telemetry fetch that has already been charged against the budget.
        self.run_worker(self.render_live(), group="live")

    def log_line(self, message: str) -> None:
        stamp = dt.datetime.now(UK).strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{stamp}[/dim] {message}")


_PRODUCT_NAMES = {
    "VAR-22-11-01": "Flexible Octopus",
    "AGILE-24-10-01": "Agile Octopus",
    "GO-VAR-22-10-14": "Octopus Go",
    "COSY-22-12-08": "Cosy Octopus",
    "SNUG-24-11-07": "Snug Octopus",
    "OE-FIX-12M-26-07-25": "Octopus 12M Fixed",
    "OE-FIX-12M-LOWSC-26-07-25": "12M Fixed Low Standing",
    "COSY-FIX-12M-26-06-25": "Cosy 12M Fixed",
    "GO-FIX-12M-26-06-30": "Go 12M Fixed",
}

# Products tied to a term, so a saving comes with an exit fee attached.
_FIXED_TERM = frozenset(code for code in _PRODUCT_NAMES if "-FIX-" in code)


def _product_name(code: str) -> str:
    return _PRODUCT_NAMES.get(code, code)


def _money_p(pence: float) -> str:
    return f"£{pence / 100:,.2f}"


def main() -> None:
    Octoscope().run()


if __name__ == "__main__":
    main()
