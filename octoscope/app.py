"""OCTOSCOPE - Octopus Energy usage and spend dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
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
from .costing import UK, Calibration, DayTotal, RateTimeline
from .store import TelemetryStore
from .store import best_source as store_best
from .store import reach as store_reach
from .widgets import (
    BillsPane,
    Column,
    ComparePane,
    ControlBar,
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

# ---------------------------------------------------------------------------
# The time model
#
# Two ideas, and they are the same two in every view that looks at history: a
# RANGE says which stretch of time is on screen, and a GRAIN says how finely
# that stretch is sliced. The number keys always pick the range; `g` always
# picks the grain; ←/→ always step the range; `home` always returns to now.
#
# Each view used to own a private meaning for the number keys - a rolling
# window on the chart, a bucket size in the table, a calendar frame in compare,
# a fetch resolution in live - so the same keypress did four unrelated things
# and "just today" could not be asked for at all. Ranges are calendar-first for
# exactly that reason: TODAY and THIS WEEK are what people actually want, and a
# rolling 24 hours is a different question that now has its own key rather than
# being the closest available approximation.
#
# Grain is constrained by range rather than free-running. Every range names the
# grains it can be drawn at, so the pair on screen is always one that makes
# sense: no month-wide bars across a single day, no 4,000 half-hour columns
# across a year. The relationship holds in both directions and in every view,
# which is what makes it predictable.
# ---------------------------------------------------------------------------

# Bucket sizes, finest first: label, the noun for "per X" in a caption, and an
# abbreviation for a control bar with no room for the label.
# Settled consumption arrives half-hourly, so 30 MIN is the meter's own
# resolution and everything coarser is a sum of it - nothing is interpolated.
# Finer than that is live telemetry, which is what the LIVE view is for.
GRAINS: dict[str, tuple[str, str, str]] = {
    "30min": ("30 MIN", "half-hour", "30M"),
    "60min": ("HOUR", "hour", "1H"),
    "6hr": ("6 HOUR", "6-hour block", "6H"),
    "12hr": ("12 HOUR", "12-hour block", "12H"),
    "day": ("DAY", "day", "DAY"),
    "week": ("WEEK", "week", "WK"),
    "month": ("MONTH", "month", "MTH"),
}


@dataclass(frozen=True)
class Range:
    """One selectable stretch of time.

    Calendar ranges (`unit` set) snap to real boundaries - a day starts at
    midnight, a week on Monday - so "this week" means the week, not the last
    seven days. Rolling ranges (`span` set) end now, for the questions where
    the boundary is the wrong thing to care about. ALL sets neither.

    `base` is how many whole units back the range sits before any stepping, so
    YESTERDAY is simply the day range at base 1.
    """

    key: str
    label: str
    grains: tuple[str, ...]
    grain: str
    frame: str                          # calendar frame the compare view uses
    short: str = ""                     # for a control bar too narrow for labels
    unit: str | None = None             # calendar unit this range snaps to
    base: int = 0                       # whole units back before stepping
    span: dt.timedelta | None = None    # rolling window length

    @property
    def brief(self) -> str:
        return self.short or self.label

    @property
    def unbounded(self) -> bool:
        """Has no window at all - the whole archive, and nothing to step."""
        return self.unit is None and self.span is None

    def window(
        self, offset: int = 0, now: dt.datetime | None = None
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Bounds of this range, `offset` steps further back. None means open."""
        now = now or dt.datetime.now(UK)
        if self.unit is not None:
            return costing.period_window(self.unit, self.base + offset, now)
        if self.span is None:
            return None, None
        end = now - self.span * offset
        return end - self.span, end

    def name(self, offset: int = 0, now: dt.datetime | None = None) -> str:
        """What to call this range once it has been stepped back `offset`.

        A stepped calendar range renames itself properly - THIS WEEK becomes
        LAST WEEK becomes WEEK OF 14 JUL - because a caption still reading
        "THIS WEEK" over July's bars is worse than no caption at all.
        """
        now = now or dt.datetime.now(UK)
        if self.unit is not None:
            start, _ = self.window(offset, now)
            return costing.period_name(self.unit, start, now)
        if offset == 0 or self.span is None:
            return self.label
        start, end = self.window(offset, now)
        stamp = "%d %b %H:%M" if self.span < dt.timedelta(days=2) else "%d %b"
        return f"{start:{stamp}} - {end:{stamp}}".upper()


# Selectable with the number keys, in this order. Calendar ranges first because
# they answer the question people ask; the rolling ones follow; ALL last.
RANGES: list[Range] = [
    Range("today", "TODAY", ("30min", "60min", "6hr"), "60min", "day",
          unit="day"),
    Range("yesterday", "YESTERDAY", ("30min", "60min", "6hr"), "60min", "day",
          short="YDAY", unit="day", base=1),
    Range("week", "THIS WEEK", ("60min", "6hr", "12hr", "day"), "day", "week",
          short="WEEK", unit="week"),
    Range("month", "THIS MONTH", ("6hr", "12hr", "day", "week"), "day", "month",
          short="MONTH", unit="month"),
    Range("24h", "LAST 24 HOURS", ("30min", "60min", "6hr"), "30min", "day",
          short="24H", span=dt.timedelta(hours=24)),
    Range("7d", "LAST 7 DAYS", ("60min", "6hr", "12hr", "day"), "day", "week",
          short="7D", span=dt.timedelta(days=7)),
    Range("30d", "LAST 30 DAYS", ("6hr", "12hr", "day", "week"), "day", "month",
          short="30D", span=dt.timedelta(days=30)),
    Range("90d", "LAST 90 DAYS", ("day", "week", "month"), "day", "month",
          short="90D", span=dt.timedelta(days=90)),
    Range("all", "ALL", ("day", "week", "month"), "week", "year"),
]

# Where the calendar block ends and the rolling block begins, so the control
# bar can rule a line between two kinds of question rather than showing nine
# undifferentiated options.
RANGE_GROUPS = [(0, 4), (4, 8), (8, 9)]

# Live trace resolutions: label, bucket seconds, API grouping, window minutes,
# cache seconds. Longer windows are cached harder because they move slowly and
# every call comes out of the 125/h telemetry budget.
#
# This is the one view whose number keys cannot mean a time range - it is about
# the present moment only, and its options trade resolution against budget
# rather than picking a stretch of history. The control bar relabels itself
# accordingly rather than leaving you to infer the difference.
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
    ("compare", "compare-pane", None),
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
    ] + [
        # One key per range, in the order the control bar draws them. Generated
        # so the keys, the bar and RANGES can never drift apart.
        Binding(str((index + 1) % 10), f"pick({index})", r.label.lower(),
                priority=True, show=False)
        for index, r in enumerate(RANGES)
    ] + [
        Binding("left", "scrub(1)", "earlier", priority=True),
        Binding("right", "scrub(-1)", "later", priority=True),
        Binding("shift+left", "nudge(1)", "scroll", priority=True),
        Binding("shift+right", "nudge(-1)", "scroll", priority=True),
        Binding("home", "scrub_now", "now", priority=True),
        Binding("g", "grain", "grain", priority=True),
        Binding("m", "metric", "kWh/cost", priority=True, show=False),
        Binding("p", "pause", "pause", priority=True),
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
        # The one piece of state every history view reads: which stretch of
        # time, how many steps back, and how finely to slice it.
        self.range_index = RANGES.index(next(r for r in RANGES if r.key == "30d"))
        self.range_offset = 0
        self.grain = self.time_range.grain
        # Settled readings with Home Mini filling anything Octopus has not
        # published. Every chart figure comes from this one series.
        self.pool: list[dict] = []
        self.provisional_dates: set[dt.date] = set()
        self.reconciling = False
        self.view_index = 0
        self.compare_metric = "kwh"
        self.live_rollup_index = 0
        self.live_offset = dt.timedelta(0)
        self.live_buckets: list[costing.PowerBucket] = []
        self.live_readings: list[dict] = []
        self.live_status = ""
        self.bills: list[dict] = []
        # Suspends every unattended call: the two polls, and the backfills that
        # scrolling queues. See action_pause.
        self.paused = False
        # Monotonic deadline of the next telemetry poll, for the live caption's
        # countdown. None until bootstrap starts the timer.
        self._next_poll: float | None = None
        self._telemetry_failed = False
        self._series_lock = asyncio.Lock()
        self._filling: set[str] = set()
        self._busy: Counter[str] = Counter()
        self._spinner_frame = 0
        self._tariff_options: list[costing.TariffOption] | None = None
        self._tariff_results: list[costing.TariffResult] = []
        self._tariff_readings: list[dict] = []
        self._tariff_load: asyncio.Future | None = None

    # ---------------- the selected stretch of time ----------------

    @property
    def time_range(self) -> Range:
        return RANGES[self.range_index]

    @property
    def range_label(self) -> str:
        return self.time_range.name(self.range_offset)

    @property
    def grain_label(self) -> str:
        return GRAINS[self.grain][0]

    @property
    def grain_unit(self) -> str:
        return GRAINS[self.grain][1]

    @property
    def range_window(self) -> tuple[dt.datetime | None, dt.datetime | None]:
        return self.time_range.window(self.range_offset)

    @property
    def compare_offset(self) -> int:
        """The selected range expressed as whole frames back, for compare.

        The compare view is the same range asked as a question about change, so
        it follows the number keys like everything else rather than keeping a
        second, separate notion of where you are in time.
        """
        return self.time_range.base + self.range_offset

    def _fit_grain(self, previous: str | None = None) -> None:
        """Settle on a grain this range can actually be drawn at.

        Keeps the intent behind the old choice rather than snapping to the
        default every time: if you were looking at hours and the new range only
        goes down to 6-hour blocks, you get 6-hour blocks - the closest thing to
        what you asked for - and only a range you have not expressed a
        preference for falls back to its own default.
        """
        allowed = self.time_range.grains
        if self.grain in allowed:
            return
        if previous is None:
            self.grain = self.time_range.grain
            return
        order = list(GRAINS)
        want = order.index(previous)
        self.grain = min(allowed, key=lambda key: abs(order.index(key) - want))

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
        # Directly above the pane it drives, and never hidden: whatever view is
        # up, this row is what the number keys will do to it.
        yield ControlBar(id="controls")
        with Vertical(classes="pane", id="trend-pane"):
            yield Label("┤ USAGE & SPEND ├", classes="pane-title", id="trend-title")
            yield TrendChart(id="trend")
            yield ReconcilePane(id="reconcile", classes="hidden")
        with Vertical(classes="pane hidden", id="compare-pane"):
            yield Label("┤ COMPARE ├", classes="pane-title", id="compare-title")
            yield ComparePane(id="compare")
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
            with Vertical(classes="pane", id="bills-pane"):
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
        # Before any data lands: the controls work from the first frame, so
        # they are on screen from the first frame rather than after a fetch.
        self.render_controls()
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
        # The spinner timer and any in-flight job can both tick after the
        # screen has gone during shutdown, and a redraw of a status row that no
        # longer exists is not worth crashing the teardown over.
        try:
            status = self.query_one("#status", Label)
        except NoMatches:
            return
        # Paused shows on every view, not just the live pane: the whole point
        # is to be able to walk away, and a state you have to go looking for is
        # one you will forget you left on.
        held = "[yellow]⏸ paused[/yellow]" if self.paused else ""
        if not self._busy:
            self._spinner_frame = 0
            status.update(held)
            return
        self._spinner_frame += 1
        bar = _sweep(self._spinner_frame)
        jobs = " · ".join(sorted(self._busy))
        line = f"[#00ff41]{bar}[/#00ff41]  [#b8e600]{jobs}[/#b8e600]"
        status.update(f"{line}  {held}" if held else line)

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

        self.set_interval(POLL_TELEMETRY, self._poll_telemetry)
        self.set_interval(POLL_CONSUMPTION, self._poll_consumption)
        self._next_poll = time.monotonic() + POLL_TELEMETRY
        self.set_interval(1, self.tick_countdown)

    # ---------------- pause ----------------

    # The timers go through these rather than calling the refreshers directly,
    # so that `r` still fetches on demand while paused. Pausing is about the
    # calls you are not there to authorise, not about locking the app.
    async def _poll_telemetry(self) -> None:
        # Rearmed whether or not it runs: the timer fires on its own cadence
        # regardless of pause, so the countdown stays honest about when the
        # next one is due rather than freezing at zero while paused.
        self._next_poll = time.monotonic() + POLL_TELEMETRY
        if not self.paused:
            await self.refresh_telemetry()

    async def _poll_consumption(self) -> None:
        if not self.paused:
            await self.refresh_consumption()

    def action_pause(self) -> None:
        """Stop and restart the background polling.

        Telemetry runs at one call a minute, so an afternoon away burns most of
        the 125/hour budget rendering a screen nobody is reading - and the
        window that matters is the one you come back to, which gets refetched
        anyway. Pausing also stops gap backfills, so the archive stays
        browsable at every granularity without spending anything.

        Resuming polls at once rather than waiting out the interval, since
        otherwise coming back to a paused screen shows stale figures for up to
        half an hour with no sign anything is wrong.
        """
        self.paused = not self.paused
        if self.paused:
            self.log_line(
                "[yellow]paused[/yellow] - no polling or backfill until p")
        else:
            self.log_line("[green]resumed[/green]")
            self.run_worker(self._resume(), group="resume")
        self.tick_spinner()
        if self.view == "live":
            self.run_worker(self.render_live(), group="live")

    async def _resume(self) -> None:
        await self.refresh_telemetry()
        await self.refresh_consumption()

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

    def render_controls(self) -> None:
        """Redraw the picker above the panes for whichever view is up.

        Called from every render path, so the bar cannot end up describing a
        view that is no longer on screen.
        """
        try:
            bar = self.query_one("#controls", ControlBar)
        except NoMatches:  # tearing down
            return
        if self.view == "live":
            # Not a time range: see LIVE_ROLLUPS. Labelled as what it is so the
            # difference from every other view is visible rather than inferred.
            bar.update_controls(
                [(str(index + 1), label, label.split("·")[0].strip())
                 for index, (label, *_) in enumerate(LIVE_ROLLUPS)],
                self.live_rollup_index,
                note="trace resolution · always ending now · ←→ scrolls the window",
            )
            return

        selected = self.time_range
        note_bits: list[str] = []
        if self.range_offset:
            note_bits.append(f"{self.range_label} · home returns to now")
        elif not selected.unbounded:
            note_bits.append("←→ earlier/later")
        if self.view == "compare":
            note_bits.append(f"m plots {'kWh' if self.compare_metric == 'cost' else 'cost'}")
        elif self.view == "chart":
            note_bits.append("o settled vs live")
        bar.update_controls(
            [(str((index + 1) % 10), r.label, r.brief)
             for index, r in enumerate(RANGES)],
            self.range_index,
            groups=RANGE_GROUPS,
            # The compare view slices by its own frame - a day into hours, a
            # year into months - so offering a grain that does nothing would be
            # a lie. Nothing is shown rather than something inert.
            secondary=(
                [] if self.view == "compare"
                else [(GRAINS[key][0], GRAINS[key][2]) for key in selected.grains]
            ),
            secondary_active=(
                selected.grains.index(self.grain)
                if self.grain in selected.grains else 0
            ),
            note=" · ".join(note_bits),
        )

    def render_period(self, reset_scroll: bool = False) -> None:
        label = self.range_label
        columns = self._columns()
        # Scroll position survives a background refresh - a telemetry poll must
        # not yank you back to now while you are reading last month - but a
        # deliberate change of range or grain starts from the latest data.
        self.query_one("#trend", TrendChart).update_columns(
            columns, label, grain=self.grain_label, unit=self.grain_unit,
            reset_scroll=reset_scroll)
        if not self.reconciling:
            # The overlay owns the title while it is up, so a background
            # refresh cannot relabel the pane with the chart it is hiding.
            self.query_one("#trend-title", Label).update(
                f"┤ USAGE & SPEND · {label} · per {self.grain_unit} ├"
            )
        # Day vs night is a property of the day however the chart is sliced, so
        # this pane stays on whole days regardless of the selected granularity.
        self.query_one("#split", SplitPane).update_split(
            self._split_days(), self.calibration.night_window,
            self.calibration.confident,
            day_rate=self.day_rates.latest,
            night_rate=self.night_rates.latest,
            standing=self.standing.latest)
        self.render_month()
        self.render_controls()
        if self.reconciling:
            self.render_reconcile()
        if self.view == "compare":
            # Same pool, so a poll that moves the chart moves this too.
            self.render_compare()

    def _window_pool(self) -> list[dict]:
        """The merged reading pool clipped to the selected range."""
        start, end = self.range_window
        if start is None and end is None:
            return self.pool
        return [
            r for r in self.pool
            if (when := costing.parse_time(r.get("interval_start"))) is not None
            and (start is None or when >= start)
            and (end is None or when < end)
        ]

    def _buckets(self) -> list[costing.Bucket]:
        """Bucket the selected range at the selected grain.

        One code path for every range, grain and view - the chart, the table and
        the rollups behind them are the same numbers sliced the same way, which
        is the property that kept breaking while each view rolled its own.
        Every reading falls in exactly one bucket whatever the grain, so the
        range total is the same however it is sliced.
        """
        pool = self._window_pool()
        if not pool:
            return []
        return costing.rollup(
            pool, self.grain, self.calibration, self.day_rates, self.night_rates,
            self.standing)

    def _columns(self) -> list[Column]:
        columns = [Column.from_bucket(b, self.grain) for b in self._buckets()]
        # Flag the bars whose energy came from the meter rather than a bill.
        # Only meaningful per day or finer; a week straddles both sources.
        if self.grain in costing.SUB_DAY_PERIODS or self.grain == "day":
            provisional = self.provisional_dates
            for column in columns:
                if column.start.astimezone(UK).date() in provisional:
                    column.provisional = True
        return columns

    def _split_days(self) -> list[DayTotal]:
        """Whole days in the range, for the day-vs-night pane.

        A range shorter than a day contains no whole day, and the split of a
        half finished one says nothing. Rather than blanking the pane, fall
        back to the most recent complete day - which is the like-for-like
        comparison someone looking at today actually wants.
        """
        complete = self.complete_totals
        start, end = self.range_window
        if start is None and end is None:
            return complete
        window = [
            total for total in complete
            if (start is None or total.date >= start.date())
            and (end is None or total.date < end.date())
        ]
        return window or complete[-1:]

    def render_compare(self) -> None:
        """Draw the selected period against the one before it.

        Reads the same merged pool the chart does, so the two views can never
        disagree about what a day used, and costs nothing in API calls however
        far back you step.
        """
        frame = self.time_range.frame
        comparison = costing.compare_periods(
            self.pool, frame, self.calibration, self.day_rates, self.night_rates,
            self.standing, offset=self.compare_offset)
        self.query_one("#compare", ComparePane).update_comparison(
            comparison, self.compare_metric)
        self.query_one("#compare-title", Label).update(
            f"┤ COMPARE · {comparison.current.label} vs {comparison.previous.label} ├"
        )
        self.render_controls()

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

    def render_table(self) -> None:
        """The chart's own buckets, as numbers.

        Literally the same call the chart makes, so the table can no longer
        disagree with the bars above it about what a day cost - which it could,
        and did, while it kept a private granularity and no window at all. The
        table's job is the exact figures, not a different question.
        """
        self.query_one("#table-title", Label).update(
            f"┤ ROLLUP · {self.range_label} · per {self.grain_unit} ├"
            "   [dim]settled consumption, home mini for anything unbilled · "
            "cost inc standing · finer than 30 min lives in the LIVE view[/dim]"
        )
        self.query_one("#table", RollupTable).update_buckets(
            self._buckets(), self.grain)
        self.render_controls()

    async def render_live(self) -> None:
        point = self.point
        now_local = dt.datetime.now(UK)
        is_night = self.calibration.is_night(now_local)
        rate = (self.night_rates if is_night else self.day_rates).at(
            dt.datetime.now(dt.timezone.utc))
        _, seconds, grouping, minutes, _ = LIVE_ROLLUPS[self.live_rollup_index]

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

        self.render_live_title()
        self.render_spikes()

    def render_live_title(self) -> None:
        """Redraw the live pane's caption.

        Split out of render_live because the countdown has to tick once a
        second and the rest of that method does not - it queries the archive
        and rebuilds every bucket. This touches one Label.
        """
        try:
            title = self.query_one("#live-title", Label)
        except NoMatches:  # tearing down
            return
        label = LIVE_ROLLUPS[self.live_rollup_index][0]
        live_now = self.live_offset == dt.timedelta(0)
        budget = self.client.budget

        # Pause replaces the countdown, which is the one thing here that stops
        # being true - a scroll position still is, so the two combine.
        bits = []
        if self.paused:
            bits.append("[yellow]paused · p to resume[/yellow]")
        elif live_now and (left := self._until_next_poll()) is not None:
            bits.append(f"update in {left}")
        if not live_now:
            end_local = dt.datetime.now(UK) - self.live_offset
            bits.append(
                f"[yellow]scrolled back to {end_local:%d %b %H:%M}[/yellow] · home=now")
        position = " · ".join(bits)
        spent = ""
        if budget.used and budget.resets_at:
            spent = f" · frees at {budget.resets_at.astimezone(UK):%H:%M}"
        title.update(
            f"┤ LIVE · {label} ├   [dim]{position} · "
            f"API budget {budget.remaining}/{budget.per_hour}{spent}[/dim]"
        )
        self.render_controls()

    def _until_next_poll(self) -> str | None:
        """Time to the next telemetry poll, or None before one is scheduled.

        Counts down the interval timer rather than time since the last reading
        landed, because the timer is what actually fetches: `r` and resuming
        both refresh without moving it, and a countdown that reset on those
        would promise an update that is not coming.
        """
        if self._next_poll is None:
            return None
        # Floored, not rounded: rounding up shows a minute that has not
        # arrived, and a countdown should never overstate what is left.
        left = max(0, int(self._next_poll - time.monotonic()))
        return f"{left // 60}:{left % 60:02d}" if left >= 60 else f"{left}s"

    def tick_countdown(self) -> None:
        """Second hand for the live caption. Nothing else redraws this often."""
        if self.view == "live" and not self.paused:
            self.render_live_title()

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
        if self.paused or grouping in self._filling:
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
        """Price the selected range on every comparable Octopus tariff."""
        point = self.point
        if not point or not self.readings:
            return
        label = self.range_label
        self.render_controls()
        start, end = self.range_window
        readings = [
            r for r in self.readings
            if (when := costing.parse_time(r.get("interval_start"))) is not None
            and (start is None or when >= start)
            and (end is None or when < end)
        ]
        if not readings:
            self.query_one("#tariff-title", Label).update(
                f"┤ TARIFFS · {label} ├   [dim]no settled readings in this "
                f"range - try a longer one[/dim]"
            )
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
            f"inc standing · ↑↓ to explain a row[/dim]"
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
        label = self.range_label
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
        # Ahead of the render, which may be a worker: the bar must never be
        # left describing the view you just tabbed away from.
        self.render_controls()
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
            self.render_table()
        elif self.view == "compare":
            self.render_compare()
        elif self.view == "live":
            # Not exclusive: cancelling a live render would abandon an
            # in-flight fetch already charged against the budget.
            self.run_worker(self.render_live(), group="live")
        else:
            # Scoped to its own group: a bare exclusive=True would cancel the
            # bootstrap worker still fetching rates and comparison data.
            self.run_worker(self.render_tariffs(), exclusive=True, group="view")

    def action_pick(self, index: int) -> None:
        """Number keys: choose the stretch of time - or, in live, the trace.

        The single place a number key is handled. Every history view reads the
        same range afterwards, so switching views never moves you in time and
        the answer to "what am I looking at?" is the same one everywhere.
        """
        if self.view == "live":
            if index >= len(LIVE_ROLLUPS):
                return
            self.live_rollup_index = index
            self.run_worker(self.render_live(), group="live")
            return
        if index >= len(RANGES) or index == self.range_index:
            # Re-pressing the range you are on returns it to the present, which
            # is the obvious meaning and saves reaching for `home`.
            if index == self.range_index and self.range_offset:
                self.range_offset = 0
                self.refresh_view()
            return
        previous = self.grain
        self.range_index = index
        # Stepping is counted in the range's own units, so an offset carried
        # from a range of a different length would land somewhere arbitrary.
        self.range_offset = 0
        self._fit_grain(previous)
        self.refresh_view()
        if self.view == "chart":
            self.query_one("#trend", TrendChart).scroll_home()

    def action_grain(self) -> None:
        """Cycle how finely the selected range is sliced.

        Only through the grains this range can be drawn at, so the key can
        never land you on a month-wide bar across a single day. That the offer
        changes with the range is the point - and the control bar shows which
        grains are on offer rather than leaving you to press `g` and find out.
        """
        if self.view in ("compare", "live"):
            self.log_line(
                "[dim]grain is fixed here - " + (
                    "the compare view slices by its frame (m plots cost)"
                    if self.view == "compare"
                    else "1-4 pick the live trace resolution") + "[/dim]")
            return
        grains = self.time_range.grains
        current = grains.index(self.grain) if self.grain in grains else -1
        self.grain = grains[(current + 1) % len(grains)]
        self.refresh_view()
        if self.view == "chart":
            self.query_one("#trend", TrendChart).scroll_home()

    def action_metric(self) -> None:
        """Compare view: plot cost instead of kWh, and back."""
        if self.view != "compare":
            return
        self.compare_metric = "cost" if self.compare_metric == "kwh" else "kwh"
        self.render_compare()

    async def action_refresh(self) -> None:
        self.log_line("[dim]manual refresh[/dim]")
        await self.refresh_consumption()
        await self.refresh_telemetry()

    def _step_range(self, direction: int) -> None:
        """Move the selected range earlier or later; positive goes earlier.

        Bounded by the data rather than by an arbitrary depth: stepping past the
        oldest reading would draw an empty range and look like a bug, so it says
        why instead.
        """
        selected = self.time_range
        if selected.unbounded:
            self.log_line("[dim]ALL already covers everything there is[/dim]")
            return
        target = self.range_offset + direction
        if target < 0:
            self.log_line("[dim]already at the present[/dim]")
            return
        earliest = (
            costing.parse_time(self.pool[0]["interval_start"]) if self.pool else None
        )
        # The compare view draws the period before this one too, so it is that
        # one which would empty out first.
        probe = target + 1 if self.view == "compare" else target
        _, end = selected.window(probe)
        if earliest is not None and end is not None and end <= earliest:
            self.log_line("[yellow]nothing that far back in the archive[/yellow]")
            return
        self.range_offset = target
        self.refresh_view()

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
        """←/→: move through time by one of whatever is selected.

        One meaning everywhere. On a history view that is the range itself - the
        previous day, the previous week - which is the step someone reaching for
        the left arrow is asking for. The live view has no range to step, so
        there it pans the trace window instead, by half a screen.
        """
        if self.view == "live":
            _, _, _, minutes, _ = LIVE_ROLLUPS[self.live_rollup_index]
            self._shift_window(dt.timedelta(minutes=minutes / 2) * direction)
            return
        self._step_range(direction)

    def action_nudge(self, direction: int) -> None:
        """Shift+←/→: scroll within what is selected rather than moving it.

        A range wider than the screen - ninety days of half-hours - still has to
        be scrollable, but that is panning the viewport, not changing the
        question, so it gets the modified key.
        """
        if self.view == "live":
            _, seconds, _, _, _ = LIVE_ROLLUPS[self.live_rollup_index]
            self._shift_window(dt.timedelta(seconds=seconds) * direction)
            return
        if self.view == "chart":
            self._scroll_trend(direction)
            return
        self._step_range(direction)

    def _scroll_trend(self, direction: int) -> None:
        """Scroll the trend chart; `direction` is positive for back in time."""
        if self.reconciling:
            return
        chart = self.query_one("#trend", TrendChart)
        if chart.scroll_columns(chart.page * direction):
            return
        if chart.max_column_offset == 0:
            self.log_line(
                "[dim]the whole range already fits - ←→ to step to the one "
                "before it[/dim]")
        elif direction > 0:
            self.log_line(
                f"[yellow]start of {self.range_label} - ← for the range before "
                f"it, or a grain coarser than {self.grain_label.lower()}[/yellow]")
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
        """home: back to the present, whatever "back" means here."""
        if self.view == "live":
            self.live_offset = dt.timedelta(0)
            # Not exclusive: cancelling a live render would abandon an in-flight
            # telemetry fetch that has already been charged against the budget.
            self.run_worker(self.render_live(), group="live")
            return
        self.range_offset = 0
        self.refresh_view()
        if self.view == "chart" and not self.reconciling:
            self.query_one("#trend", TrendChart).scroll_home()

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
