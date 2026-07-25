"""Textual widgets. Green-phosphor terminal aesthetic throughout.

Layout priority: what you are using and what it costs comes first. The Agile
comparison is one pane at the bottom, because it describes a tariff this
account is not on.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from rich.console import Group
from rich.text import Text
from textual.widgets import DataTable, Static

from . import costing
from .costing import UK, Bucket, DayTotal, Forecast, PowerBucket, Spike, TariffDetail
from .model import sparkline

DAY_STYLE = "#b8e600"
NIGHT_STYLE = "#00b8ff"
COST_STYLE = "#00ff9f"
EXPORT_STYLE = "#c77dff"
# Home Mini figures standing in for billing data Octopus has not published.
PROVISIONAL_STYLE = "#ffb347"

# Bottom-anchored eighths: correct for bars growing upward from a baseline.
_LOWER_BLOCKS = " ▁▂▃▄▅▆▇█"


def _column_widths(count: int, total: int) -> list[int]:
    """Split `total` columns across `count` bars, filling the width exactly."""
    if count <= 0:
        return []
    return [(i + 1) * total // count - i * total // count for i in range(count)]


GREEN = "#00ff41"
DIM = "dim #00ff41"
RED = "#ff2e2e"
CYAN = "#00ffff"


def _power_style(watts: float) -> str:
    if watts < 300:
        return GREEN
    if watts < 1500:
        return DAY_STYLE
    return RED


def _money(pence: float) -> str:
    return f"£{pence / 100:,.2f}"


class StatTile(Static):
    """A single headline figure with supporting lines beneath."""

    def show(self, headline: Text, lines: list[Text]) -> None:
        self.update(Group(headline, *lines))

    def show_waiting(self, message: str = "waiting for data...") -> None:
        self.update(Text(f"  {message}", style=DIM))


class NowTile(StatTile):
    """Live demand from the Home Mini."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.trace: list[float] = []

    def update_live(self, readings: list[dict], unit_rate_p: float | None, is_night: bool) -> None:
        if not readings:
            self.show_waiting("no telemetry")
            return
        self.trace = [float(r["demand"]) for r in readings if r.get("demand") is not None]
        if not self.trace:
            self.show_waiting("no telemetry")
            return

        watts = self.trace[-1]
        exporting = watts < 0
        style = EXPORT_STYLE if exporting else _power_style(watts)

        headline = Text()
        headline.append(f"{watts / 1000:,.3f}", style=f"bold {style}")
        headline.append(" kW", style=DIM)

        # Pinned to zero so the picture agrees with the headline. Left to float,
        # the scale ran from the most negative sample, drawing an exporting
        # house as a mid-height bar next to a minus number.
        lines = [Text(sparkline(self.trace, width=26, floor=0.0), style=style)]

        if unit_rate_p is not None:
            if exporting:
                # Solar covering the whole house. There is no export MPAN on
                # this account, so the surplus earns nothing - the import cost
                # bottoms out at zero and must never be shown as negative.
                lines.append(
                    Text.assemble(
                        (f"{_money(0)}/hr", GREEN),
                        ("  exporting ", EXPORT_STYLE),
                        (f"{abs(watts):,.0f} W", EXPORT_STYLE),
                    )
                )
                lines.append(Text("solar covering the house", style=EXPORT_STYLE))
            else:
                # What this draw costs per hour if it held steady.
                per_hour = watts / 1000 * unit_rate_p
                rate_line = Text()
                rate_line.append(f"{_money(per_hour)}/hr", style=GREEN)
                rate_line.append("  at ", style=DIM)
                rate_line.append(
                    f"{unit_rate_p:.2f}p", style=NIGHT_STYLE if is_night else DAY_STYLE
                )
                lines.append(rate_line)
                lines.append(
                    Text("night rate" if is_night else "day rate",
                         style=NIGHT_STYLE if is_night else DIM)
                )
        self.show(headline, lines)


class TodayTile(StatTile):
    def update_today(self, today: DayTotal | None, yesterday_by_now: float | None) -> None:
        if today is None:
            self.show_waiting()
            return
        headline = Text()
        headline.append(_money(today.total_cost_p), style=f"bold {GREEN}")
        headline.append(" today", style=DIM)

        lines = [
            Text.assemble((f"{today.total_kwh:.2f} kWh", GREEN), ("  used", DIM)),
            # Spelled out so it is never ambiguous which costs are included.
            Text.assemble(
                (f"{_money(today.usage_cost_p)} use", DIM),
                (" + ", DIM), (f"{_money(today.standing_p)} standing", DIM),
            ),
            Text.assemble(
                ("day ", DIM), (f"{today.day_kwh:.2f}", DAY_STYLE),
                ("  night ", DIM), (f"{today.night_kwh:.2f}", NIGHT_STYLE),
            ),
        ]
        if yesterday_by_now and yesterday_by_now > 0:
            # Like-for-like: yesterday measured to this same time of day.
            delta = (today.total_kwh - yesterday_by_now) / yesterday_by_now * 100
            lines.append(
                Text.assemble(
                    ("vs yest by now ", DIM),
                    (f"{delta:+.0f}%", RED if delta > 0 else GREEN),
                )
            )
        self.show(headline, lines)


class MonthTile(StatTile):
    def update_month(self, forecast: Forecast | None, kwh: float, standing_p: float) -> None:
        if forecast is None:
            self.show_waiting()
            return
        headline = Text()
        headline.append(_money(forecast.month_to_date_p), style=f"bold {GREEN}")
        headline.append(" so far", style=DIM)

        self.show(
            headline,
            [
                Text.assemble((f"{kwh:.1f} kWh", GREEN), ("  this month", DIM)),
                Text.assemble(("incl ", DIM), (_money(standing_p), DAY_STYLE),
                              (" standing", DIM)),
                Text.assemble(
                    ("day ", DIM),
                    (f"{forecast.days_elapsed} of {forecast.days_in_month}", GREEN),
                ),
            ],
        )


class ForecastTile(StatTile):
    def update_forecast(self, forecast: Forecast | None) -> None:
        if forecast is None:
            self.show_waiting("need a few days of history")
            return
        headline = Text()
        headline.append(_money(forecast.projected_p), style=f"bold {DAY_STYLE}")
        headline.append(" est.", style=DIM)

        self.show(
            headline,
            [
                Text("projected month bill", style=DIM),
                Text.assemble(("mean ", DIM), (_money(forecast.mean_daily_p), GREEN),
                              ("/day", DIM)),
                Text.assemble((f"{forecast.days_remaining} days", DAY_STYLE),
                              (" remaining", DIM)),
            ],
        )


@dataclass
class Column:
    """One bar on the trend chart, at whatever granularity is selected.

    The chart used to take `DayTotal` directly, which quietly made a day the
    only thing it could ever draw - even though the underlying settled data is
    half-hourly. This is the small surface the chart actually needs, so a day,
    an hour and a month all render through the same code.
    """

    start: dt.datetime
    kwh: float
    night_kwh: float
    cost_p: float
    label: str
    tick: bool = False           # mark this column on the axis
    partial: bool = False
    provisional: bool = False

    @classmethod
    def from_bucket(cls, bucket: Bucket, grain: str) -> "Column":
        local = bucket.start.astimezone(UK)
        midnight = local.hour == 0 and local.minute == 0
        if grain in ("30min", "60min"):
            # Only midnights get labelled once the bars are narrow, and a
            # column of "00:00" tells you nothing - so midnight carries the
            # date and every other slot carries the time.
            label = local.strftime("%d/%m") if midnight else local.strftime("%H:%M")
            tick = midnight
        elif grain == "week":
            label, tick = local.strftime("%d/%m"), True
        elif grain == "month":
            label, tick = local.strftime("%b"), True
        else:
            label, tick = local.strftime("%d/%m"), local.weekday() == 0
        return cls(
            start=bucket.start,
            kwh=bucket.kwh,
            night_kwh=bucket.night_kwh,
            cost_p=bucket.total_cost_p,
            label=label,
            tick=tick,
            partial=bucket.partial,
        )


class TrendChart(Static):
    """Usage as stacked day/night columns, with a cost trace beneath.

    Columns are whatever granularity is selected - half-hours through months -
    because the settled feed is half-hourly and aggregating it to days by
    default threw away the intraday shape that decides which tariff wins.

    The current period is drawn as a distinct shaded bar at the right rather
    than omitted. It is still incomplete, so it stays out of the totals and
    means - but leaving it off the chart entirely made the previous bar read as
    "now", which is a far worse error than showing a partial one.
    """

    CHART_ROWS = 6
    COST_ROWS = 5
    GUTTER = 5

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Every reading in the period lands in exactly one column at every
        # grain, so summing all of them - partial ones included - is the same
        # number however the period is sliced. Excluding partial columns from
        # the total is what let a trailing week quietly drop the complete days
        # inside it, and made the headline change when you pressed `g`.
        self.columns: list[Column] = []
        self.label = ""
        self.grain = "DAY"
        self.unit = "day"
        self.shown = 0
        self.available_columns = 0
        # Columns scrolled back from the right edge. Held on the widget rather
        # than the app because only the widget knows how many bars fit.
        self.column_offset = 0
        self.max_column_offset = 0

    def update_columns(
        self,
        columns: list[Column],
        label: str,
        grain: str = "DAY",
        unit: str = "day",
        reset_scroll: bool = False,
    ) -> None:
        self.columns = columns
        self.label = label
        self.grain = grain
        self.unit = unit
        if reset_scroll:
            self.column_offset = 0
        self.refresh_content()

    # ---------------- scrolling ----------------

    def scroll_columns(self, delta: int) -> bool:
        """Pan the window by `delta` columns; positive goes back in time.

        Returns whether the window actually moved, so the caller can say why
        nothing happened rather than leaving a dead keypress.
        """
        target = max(0, min(self.column_offset + delta, self.max_column_offset))
        if target == self.column_offset:
            return False
        self.column_offset = target
        self.refresh_content()
        return True

    def scroll_home(self) -> bool:
        if self.column_offset == 0:
            return False
        self.column_offset = 0
        self.refresh_content()
        return True

    @property
    def page(self) -> int:
        """Half a screen of bars - the same idiom as the live view."""
        return max(1, self.shown // 2)

    def on_resize(self) -> None:
        self.refresh_content()

    def refresh_content(self) -> None:
        if not self.columns:
            self.update(Text("  no consumption data for this period", style=DIM))
            return

        available = max(20, self.size.width - 4 - self.GUTTER)
        every = self.columns
        total_columns = len(every)

        # Bar width comes from the whole series, not the visible slice, so bars
        # keep their size while you scroll instead of resizing under the cursor.
        per_day = max(1, min(16, available // total_columns))
        fit = max(1, available // per_day)
        self.max_column_offset = max(0, total_columns - fit)
        self.column_offset = min(self.column_offset, self.max_column_offset)
        end = total_columns - self.column_offset
        columns = every[max(0, end - fit):end]
        self.shown = len(columns)
        self.available_columns = total_columns

        peak = max(c.kwh for c in columns) or 1.0
        levels = self.CHART_ROWS * 8
        blocks = " ▁▂▃▄▅▆▇█"

        lines: list[Text] = []
        for row in range(self.CHART_ROWS - 1, -1, -1):
            line = Text()
            # Label the top of every other row with its kWh value.
            if row % 2 == 1:
                line.append(f"{(row + 1) / self.CHART_ROWS * peak:>4.0f}", style=DIM)
                line.append("┤", style=DIM)
            else:
                line.append(" " * (self.GUTTER - 1), style=DIM)
                line.append("│", style=DIM)

            for column in columns:
                filled = int(column.kwh / peak * levels)
                night_filled = int(column.night_kwh / peak * levels)
                cell = filled - row * 8
                char = "█" if cell >= 8 else (blocks[cell] if cell > 0 else " ")
                if column.partial:
                    # Shaded, so a half-finished day cannot be mistaken for a
                    # complete one. The two states stack: today is always both
                    # unfinished and from the meter, so the hatch carries
                    # "incomplete" and the colour still says where it came
                    # from - otherwise the legend named a day no bar matched.
                    char = "▒" if cell > 0 else " "
                    style = PROVISIONAL_STYLE if column.provisional else DIM
                elif column.provisional:
                    # Full height - the day really is complete - but hatched and
                    # in its own colour, because these figures came from the
                    # Home Mini and Octopus has not billed them yet.
                    char = "▓" if cell > 0 else " "
                    style = PROVISIONAL_STYLE
                else:
                    # Colour the cell by the midpoint of the part of it that is
                    # actually filled, not of the whole cell. The topmost cell
                    # is usually a fraction tall, so testing its full-height
                    # midpoint put that midpoint above the bar - and painted a
                    # day-coloured cap on bars that were entirely night.
                    top = min(filled, row * 8 + 8)
                    midpoint = (row * 8 + top) / 2
                    style = NIGHT_STYLE if midpoint < night_filled else DAY_STYLE
                line.append(char * per_day, style=style)
            lines.append(line)

        lines.append(self._axis(columns, per_day))
        lines.append(self._labels(columns, per_day))

        lines.extend(self._cost_chart(columns, per_day))
        lines.append(self._summary(columns))

        self.update(Group(*lines))

    def _axis(self, columns: list[Column], per_day: int) -> Text:
        axis = Text()
        axis.append(f"{0:>4.0f}", style=DIM)
        axis.append("└", style=DIM)
        for column in columns:
            tick = "┴" if column.tick or column.partial else "─"
            if column.provisional:
                tick = "~"
            axis.append(tick + "─" * (per_day - 1), style=DIM)
        return axis

    def _labels(self, columns: list[Column], per_day: int) -> Text:
        """Label under each bar when they fit, else on ticks only."""
        labels = Text(" " * self.GUTTER)
        every = per_day >= 6
        skip = 0
        for index, column in enumerate(columns):
            if skip > 0:
                skip -= 1
                continue
            last = index == len(columns) - 1
            # Only the newest bar is "now". A window's leading edge is often
            # part-covered too, and labelling that end "now" as well put the
            # present at both sides of the chart.
            if column.partial and last and self.column_offset == 0:
                text = "today" if self.unit == "day" else "now"
            elif every or column.tick or last:
                text = column.label
            else:
                labels.append(" " * per_day)
                continue
            style = DAY_STYLE if column.partial else (
                PROVISIONAL_STYLE if column.provisional else DIM)
            labels.append(text.ljust(per_day)[: max(per_day, len(text))], style=style)
            # A label wider than its column borrows space from the next ones.
            skip = max(0, -(-len(text) // per_day) - 1)
        return labels

    def _cost_chart(self, columns: list[Column], per_day: int) -> list[Text]:
        """A full cost chart with its own money axis, not a token sparkline.

        Cost deserves equal billing with volume: a night-heavy day and a
        day-heavy one can use identical kWh for very different money, and the
        bill is what actually matters.
        """
        peak = max(c.cost_p for c in columns) / 100 or 1.0
        levels = self.COST_ROWS * 8
        blocks = " ▁▂▃▄▅▆▇█"
        lines: list[Text] = []
        for row in range(self.COST_ROWS - 1, -1, -1):
            line = Text()
            value = (row + 1) / self.COST_ROWS * peak
            if row % 2 == 1 or row == self.COST_ROWS - 1:
                line.append(f"{value:>4.1f}", style=DIM)
                line.append("┤", style=DIM)
            else:
                line.append(" " * (self.GUTTER - 1))
                line.append("│", style=DIM)
            for column in columns:
                filled = int(column.cost_p / 100 / peak * levels)
                cell = filled - row * 8
                char = "█" if cell >= 8 else (blocks[cell] if cell > 0 else " ")
                if column.partial:
                    cost_style = PROVISIONAL_STYLE if column.provisional else DIM
                elif column.provisional:
                    cost_style = PROVISIONAL_STYLE
                else:
                    cost_style = COST_STYLE
                line.append(char * per_day, style=cost_style)
            lines.append(line)
        axis = Text(f"{0:>4.1f}", style=DIM)
        axis.append("└", style=DIM)
        axis.append("─" * (len(columns) * per_day), style=DIM)
        axis.append(f"  £/{self.unit}", style=DIM)
        lines.append(axis)
        return lines

    def _summary(self, visible: list[Column]) -> Text:
        # The total covers every column, partial ones included: it is the
        # energy the period actually contains, and must not move when the
        # grain changes. The mean and peak deliberately do not - a half
        # recorded unit is not a real dip, and averaging it in understates
        # usage. So the two are computed over different sets, on purpose.
        total_kwh = sum(c.kwh for c in self.columns)
        total_cost = sum(c.cost_p for c in self.columns)
        whole = [c for c in self.columns if not c.partial]
        summary = Text()
        summary.append(f"{self.label} · {self.grain}  ", style=f"bold {GREEN}")
        summary.append(f"{total_kwh:,.1f} kWh ", style=GREEN)
        summary.append(_money(total_cost), style=f"bold {GREEN}")
        if whole:
            count = len(whole)
            summary.append("   mean ", style=DIM)
            summary.append(
                f"{sum(c.kwh for c in whole) / count:.2f} kWh", style=DAY_STYLE)
            summary.append("/", style=DIM)
            summary.append(
                _money(sum(c.cost_p for c in whole) / count), style=DAY_STYLE)
            summary.append(f" per {self.unit}   peak ", style=DIM)
            peak = max(whole, key=lambda c: c.cost_p)
            summary.append(_money(peak.cost_p), style=RED)
            summary.append(f" {self._stamp(peak)}", style=DIM)
        summary.append("   [inc standing]", style=DIM)
        # Say so when the window is wider than the terminal can draw, rather
        # than silently cropping the left of the chart and letting the bars
        # disagree with the totals printed beside them. Above the footnotes:
        # this one tells you a key does something.
        if self.shown < self.available_columns and visible:
            first = self._stamp(visible[0])
            last = self._stamp(visible[-1])
            summary.append("\n  showing ", style=DIM)
            summary.append(f"{first} → {last}", style=CYAN)
            summary.append(
                f"  ({self.shown} of {self.available_columns} {self.unit}s"
                f", totals above cover all of them)", style=DIM)
            summary.append("   ← → scroll", style=DIM)
            if self.column_offset:
                summary.append("   home = latest", style=DIM)
        running = [c for c in self.columns if c.partial]
        if running:
            plural = "s" if len(running) > 1 else ""
            summary.append(
                f"\n▒ {len(running)} part-recorded {self.unit}{plural} - "
                f"counted in the total, kept out of the mean and peak",
                style=DIM)
        # By date, not by column: at HOUR grain one label per bar listed every
        # hour of the day individually and ran off the end of the pane.
        days = sorted({
            c.start.astimezone(UK).date() for c in self.columns if c.provisional
        })
        if days:
            shown = ", ".join(f"{d:%d/%m}" for d in days[:4])
            if len(days) > 4:
                shown += f" +{len(days) - 4} more"
            summary.append("\n▓ ", style=PROVISIONAL_STYLE)
            summary.append(
                f"{shown} from home mini - not yet settled by octopus",
                style=PROVISIONAL_STYLE,
            )
        return summary

    def _stamp(self, column: Column) -> str:
        local = column.start.astimezone(UK)
        if self.unit in ("half-hour", "hour"):
            return local.strftime("%d/%m %H:%M")
        return local.strftime("%d/%m")


class ReconcilePane(Static):
    """Home Mini against settled billing, for the days that have both.

    An overlay rather than a view of its own: the question it answers - "can I
    trust the provisional bars?" - only comes up while looking at the chart,
    and it has nothing to say about days the Mini never covered.
    """

    BAR = 24

    def update_rows(self, rows: list[costing.Reconciliation], reach: str = "") -> None:
        if not rows:
            self.update(Text(
                "  no overlap yet - the home mini's history and settled billing\n"
                "  do not currently cover any of the same days", style=DIM))
            return

        # One width table for headers and data, so the two cannot drift apart.
        # Numeric headers right-align over their figures; the rest left-align.
        widths = (("DATE", 5, False), ("SETTLED", 8, True), ("HOME MINI", 9, True),
                  ("DELTA", 8, True), ("", 6, True))
        gap = "  "
        lines: list[Text] = []
        header = Text("  ", style=DIM)
        for title, width, numeric in widths:
            header.append(
                (title.rjust(width) if numeric else title.ljust(width))[:width] + gap,
                style=DIM)
        header.append("AGREEMENT", style=DIM)
        lines.append(header)

        complete = [r for r in rows if r.complete]
        for row in rows:
            line = Text("  ")
            line.append(f"{row.date:%d/%m}" + gap,
                        style=GREEN if row.complete else DIM)
            line.append(f"{row.settled_kwh:8.3f}" + gap, style=DAY_STYLE)
            line.append(f"{row.live_kwh:9.3f}" + gap, style=PROVISIONAL_STYLE)
            if row.complete:
                style = self._delta_style(row)
                line.append(f"{row.delta_kwh:+8.3f}" + gap, style=style)
                pct = row.delta_pct
                line.append((f"{pct:+5.2f}%" if pct is not None else "-").rjust(6) + gap,
                            style=style)
                line.append(self._verdict(row))
            else:
                # Slot counts, not a delta: a day either source has not finished
                # will differ by however much is missing, which is not an error.
                line.append("-".rjust(8) + gap, style=DIM)
                line.append("-".rjust(6) + gap, style=DIM)
                line.append(
                    f"incomplete · {row.settled_slots}/48 settled, "
                    f"{row.live_slots}/48 live", style=DIM)
            lines.append(line)

        lines.append(Text(""))
        lines.append(self._verdict_summary(complete, reach))
        self.update(Group(*lines))

    def _delta_style(self, row: costing.Reconciliation) -> str:
        pct = abs(row.delta_pct or 0.0)
        if pct < 1.0:
            return COST_STYLE
        return DAY_STYLE if pct < 5.0 else RED

    def _verdict(self, row: costing.Reconciliation) -> Text:
        """A bar, because a column of near-zeroes is hard to read as a shape."""
        pct = abs(row.delta_pct or 0.0)
        # 5% full scale: beyond that the meter and the bill disagree enough
        # that the exact figure stops mattering.
        filled = min(self.BAR, int(pct / 5.0 * self.BAR))
        text = Text()
        text.append("█" * filled, style=self._delta_style(row))
        text.append("·" * (self.BAR - filled), style=DIM)
        text.append("  exact" if pct < 0.05 else f"  {pct:.2f}% off",
                    style=self._delta_style(row))
        return text

    def _verdict_summary(self, complete: list[costing.Reconciliation], reach: str) -> Text:
        summary = Text("  ")
        if not complete:
            summary.append("no day is complete in both sources yet", style=DIM)
            return summary
        worst = max(complete, key=lambda r: abs(r.delta_pct or 0.0))
        total_settled = sum(r.settled_kwh for r in complete)
        total_live = sum(r.live_kwh for r in complete)
        drift = (total_live - total_settled) / total_settled * 100 if total_settled else 0.0
        summary.append(f"{len(complete)} complete days  ", style=f"bold {GREEN}")
        summary.append(f"settled {total_settled:.3f} kWh", style=DAY_STYLE)
        summary.append("  vs  ", style=DIM)
        summary.append(f"mini {total_live:.3f} kWh", style=PROVISIONAL_STYLE)
        summary.append("   drift ", style=DIM)
        summary.append(f"{drift:+.3f}%", style=COST_STYLE if abs(drift) < 1 else RED)
        summary.append(f"   worst day {worst.date:%d/%m} ", style=DIM)
        summary.append(f"{worst.delta_pct:+.2f}%", style=self._delta_style(worst))
        if reach:
            summary.append(f"\n  {reach}", style=DIM)
        return summary


class SplitPane(Static):
    """Day versus night, which is the lever that matters on Economy 7."""

    def update_split(self, totals: list[DayTotal], night_window: tuple[str, str] | None,
                     confident: bool, day_rate: float | None = None,
                     night_rate: float | None = None,
                     standing: float | None = None) -> None:
        if not totals:
            self.update(Text("  no data", style=DIM))
            return
        day_kwh = sum(t.day_kwh for t in totals)
        night_kwh = sum(t.night_kwh for t in totals)
        day_cost = sum(t.day_cost_p for t in totals)
        night_cost = sum(t.night_cost_p for t in totals)
        total_kwh = day_kwh + night_kwh
        if total_kwh <= 0:
            self.update(Text("  no data", style=DIM))
            return

        night_share = night_kwh / total_kwh
        bar_width = 28
        night_cells = int(night_share * bar_width)
        bar = Text()
        bar.append("█" * night_cells, style=NIGHT_STYLE)
        bar.append("█" * (bar_width - night_cells), style=DAY_STYLE)

        window = f"{night_window[0]}-{night_window[1]}" if night_window else "unknown"
        rows = [
            bar,
            Text.assemble(
                ("night ", NIGHT_STYLE), (f"{night_share * 100:.0f}%", NIGHT_STYLE),
                ("   day ", DAY_STYLE), (f"{(1 - night_share) * 100:.0f}%", DAY_STYLE),
            ),
            Text(""),
        ]

        # The rates themselves, so you never have to go and look them up.
        if day_rate is not None:
            rows.append(
                Text.assemble(
                    ("day    ", DIM), (f"{day_rate:6.2f}p/kWh  ", f"bold {DAY_STYLE}"),
                    (f"{day_kwh:6.1f} kWh  ", DIM), (_money(day_cost), DAY_STYLE),
                )
            )
        if night_rate is not None:
            rows.append(
                Text.assemble(
                    ("night  ", DIM), (f"{night_rate:6.2f}p/kWh  ", f"bold {NIGHT_STYLE}"),
                    (f"{night_kwh:6.1f} kWh  ", DIM), (_money(night_cost), NIGHT_STYLE),
                )
            )
        if standing is not None:
            rows.append(
                Text.assemble(
                    ("standing ", DIM), (f"{standing:.2f}p/day", f"bold {COST_STYLE}"),
                    ("  = ", DIM), (f"{_money(standing * 365)}/yr", DIM),
                )
            )
        rows.append(
            Text.assemble(
                ("window ", DIM), (window, GREEN),
                ("  measured" if confident else "  ASSUMED",
                 GREEN if confident else RED),
            )
        )
        rows.append(Text("kWh/cost are usage only, ex standing", style=DIM))
        self.update(Group(*rows))


class RollupTable(DataTable):
    """Tabular view of usage and cost at a selectable granularity."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("PERIOD", "USAGE", "TARIFF", "COST")

    def update_buckets(self, buckets: list[Bucket], period: str) -> None:
        self.clear()
        if not buckets:
            self.add_row("no data", "", "", "")
            return

        # Newest first: the most recent period is what you want to see.
        for bucket in reversed(buckets[-500:]):
            label = _period_label(bucket, period)
            if bucket.partial:
                label += "  · in progress"
            style = DIM if bucket.partial else GREEN
            self.add_row(
                Text(label, style=style),
                Text(f"{bucket.kwh:.3f} kWh", style=DIM if bucket.partial else DAY_STYLE,
                     justify="right"),
                Text(bucket.tariff, style=DIM if bucket.partial else _tariff_style(bucket)),
                Text(_money(bucket.total_cost_p),
                     style=DIM if bucket.partial else COST_STYLE, justify="right"),
            )


def _tariff_style(bucket: Bucket) -> str:
    if bucket.tariff == "night":
        return NIGHT_STYLE
    if bucket.tariff == "day":
        return DAY_STYLE
    return DIM


def _period_label(bucket: Bucket, period: str) -> str:
    start = bucket.start
    if period == "month":
        return start.strftime("%B %Y")
    if period == "day":
        return start.strftime("%a %d %b")
    if period == "60min":
        return f"{start:%a %d %b  %H:%M}-{bucket.end:%H:%M}"
    return f"{start:%a %d %b  %H:%M}-{bucket.end:%H:%M}"


# A 5-row block font, so the live reading is legible at a glance rather than
# being just another line of text.
_DIGITS = {
    "0": ("███", "█ █", "█ █", "█ █", "███"),
    "1": ("  █", "  █", "  █", "  █", "  █"),
    "2": ("███", "  █", "███", "█  ", "███"),
    "3": ("███", "  █", "███", "  █", "███"),
    "4": ("█ █", "█ █", "███", "  █", "  █"),
    "5": ("███", "█  ", "███", "  █", "███"),
    "6": ("███", "█  ", "███", "█ █", "███"),
    "7": ("███", "  █", "  █", "  █", "  █"),
    "8": ("███", "█ █", "███", "█ █", "███"),
    "9": ("███", "█ █", "███", "  █", "███"),
    ".": ("   ", "   ", "   ", "   ", "  █"),
}


def _big_number(text: str) -> list[str]:
    """Render digits as five rows of block characters."""
    rows = ["", "", "", "", ""]
    for char in text:
        glyph = _DIGITS.get(char)
        if glyph is None:
            continue
        for i in range(5):
            rows[i] += glyph[i] + " "
    return rows


class LiveView(Static):
    """Live demand, front and centre: a large readout and a 30-minute trace."""

    GUTTER = 7

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.readings: list[dict] = []
        self.buckets: list[PowerBucket] = []
        self.rate_p: float | None = None
        self.is_night = False
        self.today: DayTotal | None = None
        self.status = ""

    def update_live(
        self,
        readings: list[dict],
        buckets: list[PowerBucket],
        rate_p: float | None,
        is_night: bool,
        today: DayTotal | None,
        status: str = "",
    ) -> None:
        self.readings = readings
        self.buckets = buckets
        self.rate_p = rate_p
        self.is_night = is_night
        self.today = today
        self.status = status
        self.refresh_content()

    def on_resize(self) -> None:
        self.refresh_content()

    def refresh_content(self) -> None:
        watts = [float(r["demand"]) for r in self.readings if r.get("demand") is not None]
        if not watts and not self.buckets:
            self.update(Text(f"  {self.status or 'no live telemetry'}", style=DIM))
            return

        # The headline is the meter's latest reading, deliberately independent
        # of the chart: changing granularity or scrolling back through history
        # must not alter what the house is drawing right now.
        current = watts[-1] if watts else self.buckets[-1].net_watts
        if current < 0:
            style = EXPORT_STYLE
        elif current < 300:
            style = GREEN
        elif current < 1500:
            style = DAY_STYLE
        else:
            style = RED

        # Large numerals so the current draw is readable across the room.
        digits = _big_number(f"{abs(current) / 1000:.3f}")
        stats = self._stats(watts or [b.watts for b in self.buckets], current)
        lines = []
        for glyph_row, stat in zip(digits, stats):
            line = Text(glyph_row, style=style)
            line.append("   ")
            line.append_text(stat)
            lines.append(line)
        lines.append(Text(""))
        lines.extend(self._trace())
        self.update(Group(*lines))

    def _stats(self, watts: list[float], current: float) -> list[Text]:
        rows: list[Text] = []
        head = Text()
        head.append("kW", style=DIM)
        if current < 0:
            head.append(f"   {abs(current):,.0f} W ", style=EXPORT_STYLE)
            head.append("EXPORTING", style=f"bold {EXPORT_STYLE}")
        else:
            head.append(f"   {current:,.0f} W", style=GREEN)
        rows.append(head)

        if current < 0:
            row = Text()
            row.append("importing nothing", style=EXPORT_STYLE)
            row.append("  ·  solar covering the house", style=DIM)
            rows.append(row)
        elif self.rate_p is not None:
            row = Text()
            row.append(f"{_money(current / 1000 * self.rate_p)}/hr", style=f"bold {COST_STYLE}")
            row.append("  at this draw", style=DIM)
            rows.append(row)
            band = Text()
            band.append("night rate " if self.is_night else "day rate ", style=DIM)
            band.append(f"{self.rate_p:.2f}p", style=NIGHT_STYLE if self.is_night else DAY_STYLE)
            rows.append(band)
        else:
            rows.extend([Text(""), Text("")])

        if self.today:
            row = Text()
            row.append("today ", style=DIM)
            row.append(f"{self.today.total_kwh:.2f} kWh", style=GREEN)
            row.append("  ", style=DIM)
            row.append(_money(self.today.total_cost_p), style=COST_STYLE)
            rows.append(row)
        else:
            rows.append(Text(""))

        if self.buckets:
            span = self.buckets[-1].end - self.buckets[0].start
            energy = sum(b.kwh for b in self.buckets)
            net = [b.net_watts for b in self.buckets]
            window = Text()
            window.append(f"window {_duration(span)}  ", style=DIM)
            window.append(f"{energy:.3f} kWh in", style=GREEN)
            exporting = [b for b in self.buckets if b.exporting]
            if exporting:
                minutes = sum(
                    (b.end - b.start).total_seconds() / 60 for b in exporting
                )
                window.append(f"  ·  {_duration(dt.timedelta(minutes=minutes))} exporting",
                              style=EXPORT_STYLE)
                window.append(f" (peak {abs(min(net)):,.0f} W)", style=DIM)
            else:
                window.append(f"  peak {max(net):,.0f} W", style=DIM)
            rows.append(window)
        if self.status:
            rows.append(Text(self.status, style=RED))
        while len(rows) < 5:
            rows.append(Text(""))
        return rows[:5]

    def _trace(self) -> list[Text]:
        """Power trace with a real clock axis, one column per bucket."""
        if not self.buckets:
            return [Text("  no trace data", style=DIM)]
        available = max(20, self.size.width - self.GUTTER - 3)
        # Spread the buckets across the full width. Integer bar widths alone
        # cannot fill it - 73 buckets in 131 columns rounds down to 1 each and
        # leaves the right-hand half empty - so the remainder is distributed.
        buckets = self.buckets[-available:]
        widths = _column_widths(len(buckets), available)
        values = [b.net_watts for b in buckets]
        high = max(max(values), 0.0)
        low = min(min(values), 0.0)

        rows = self._trace_rows()
        span = (high - low) or 1.0
        if low >= 0:
            export_rows, import_rows = 0, rows
        else:
            export_rows = max(1, min(rows - 2, round(rows * (-low) / span)))
            import_rows = max(2, rows - export_rows)

        lines: list[Text] = []
        for row in range(import_rows - 1, -1, -1):
            labelled = (import_rows - 1 - row) % 2 == 0
            line = self._gutter((row + 1) / import_rows * high, labelled)
            for value, width in zip(values, widths):
                level = int(max(value, 0.0) / (high or 1.0) * import_rows * 8)
                cell = level - row * 8
                char = "█" if cell >= 8 else (_LOWER_BLOCKS[cell] if cell > 0 else " ")
                line.append(char * width, style=_power_style(value))
            lines.append(line)

        lines.append(self._zero_line(buckets, widths))

        for row in range(export_rows):
            labelled = row % 2 == 1 or row == export_rows - 1
            line = self._gutter(-(row + 1) / export_rows * (-low), labelled)
            for value, width in zip(values, widths):
                # Export hangs downward, so partial cells must fill from the
                # top. Only whole and half blocks exist as upper-anchored
                # glyphs with dependable font coverage, so quantise to halves
                # rather than leave gaps floating above the bar.
                level = int(max(-value, 0.0) / (-low or 1.0) * export_rows * 2)
                cell = level - row * 2
                char = "█" if cell >= 2 else ("▀" if cell == 1 else " ")
                line.append(char * width, style=EXPORT_STYLE)
            lines.append(line)

        lines.append(self._time_labels(buckets, widths))
        return lines

    def _trace_rows(self) -> int:
        """Rows left for the chart after the readout, axis, and labels."""
        return max(4, self.size.height - 5 - 1 - 2)

    def _gutter(self, value: float, labelled: bool) -> Text:
        if labelled:
            line = Text(f"{value:>{self.GUTTER - 1},.0f}", style=DIM)
            line.append("┤", style=DIM)
        else:
            line = Text(" " * (self.GUTTER - 1))
            line.append("│", style=DIM)
        return line

    def _zero_line(self, buckets: list[PowerBucket], widths: list[int]) -> Text:
        axis = Text(f"{0:>{self.GUTTER - 1},.0f}", style=DIM)
        axis.append("└", style=DIM)
        ticks = set(self._tick_columns(buckets, widths).values())
        for column in range(sum(widths)):
            axis.append("┬" if column in ticks else "─", style=DIM)
        axis.append(" W", style=DIM)
        return axis

    def _time_labels(self, buckets: list[PowerBucket], widths: list[int]) -> Text:
        labels = Text(" " * self.GUTTER)
        cursor = 0
        for index, column in sorted(self._tick_columns(buckets, widths).items()):
            if column < cursor:
                continue
            labels.append(" " * (column - cursor))
            text = buckets[index].start.astimezone(UK).strftime("%H:%M")
            labels.append(text, style=DIM)
            cursor = column + len(text)
        return labels

    def _tick_columns(
        self, buckets: list[PowerBucket], widths: list[int]
    ) -> dict[int, int]:
        """Map tick bucket index -> screen column, spaced so labels cannot collide."""
        if len(buckets) < 2:
            return {0: 0}
        offsets = []
        running = 0
        for width in widths:
            offsets.append(running)
            running += width
        total = running
        # An HH:MM label plus a gap needs about 8 columns.
        count = max(2, min(len(buckets), total // 9))
        step = max(1, len(buckets) // count)
        return {i: offsets[i] for i in range(0, len(buckets), step)}


def _duration(span: dt.timedelta) -> str:
    total = int(span.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _downsample(values: list[float], width: int) -> list[float]:
    if len(values) <= width:
        return values
    step = len(values) / width
    out = []
    for i in range(width):
        chunk = values[int(i * step): max(int((i + 1) * step), int(i * step) + 1)]
        out.append(sum(chunk) / len(chunk))
    return out


class TariffTable(DataTable):
    """What the same consumption would have cost on other Octopus tariffs."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        # Not "registers": a meter having one or two of them is a billing
        # detail. What matters is how many prices the tariff has and whether
        # they follow a clock you can plan around.
        self.add_columns("TARIFF", "RATES", "USAGE", "STANDING", "TOTAL", "VS YOURS")

    def update_results(
        self, results: list, current_total: float | None, shapes: dict | None = None
    ) -> None:
        self.clear()
        if not results:
            self.add_row("no comparison data yet", "", "", "", "", "")
            return
        shapes = shapes or {}
        for result in results:
            option = result.option
            delta = (
                result.total_cost_p - current_total if current_total is not None else None
            )
            if option.is_current:
                verdict, verdict_style = "— your tariff —", f"bold {GREEN}"
            elif delta is None:
                verdict, verdict_style = "-", DIM
            elif delta < 0:
                verdict, verdict_style = f"{_money(abs(delta))} cheaper", CYAN
            else:
                verdict, verdict_style = f"{_money(delta)} dearer", RED
            name_style = f"bold {GREEN}" if option.is_current else GREEN
            self.add_row(
                Text(option.name, style=name_style),
                Text(shapes.get(option.code, option.registers), style=DIM),
                Text(_money(result.usage_cost_p), style=DAY_STYLE, justify="right"),
                Text(_money(result.standing_p), style=DIM, justify="right"),
                Text(_money(result.total_cost_p), style=COST_STYLE, justify="right"),
                Text(verdict, style=verdict_style),
            )


class TariffBreakdown(Static):
    """Why one tariff beats another.

    Laid out as fixed columns - yours, theirs, the difference - because the
    question is always a comparison, and prose on one line gives the eye nothing
    to run down. Rates first, then money, then the day-by-day shape of it.
    """

    LABEL_W = 15
    COL_W = 30
    DIFF_W = 11
    GAP = 2
    GUTTER = 8

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.detail: TariffDetail | None = None
        self.label = ""

    def show_detail(self, detail: TariffDetail | None, label: str) -> None:
        self.detail = detail
        self.label = label
        self.refresh_content()

    def on_resize(self) -> None:
        self.refresh_content()

    def _row(
        self,
        label: str,
        yours: str,
        theirs: str,
        diff: str = "",
        *,
        diff_style: str | None = None,
        bold: bool = False,
        value_style: str = GREEN,
        money: bool = False,
    ) -> Text:
        """One line of the comparison.

        Money is right-aligned so the decimal points stack; rates are left
        aligned because they carry a time range after them. Every column is
        padded to its full width and separated by a fixed gap, so nothing can
        run into the column beside it however long the text gets.
        """
        pad = " " * self.GAP
        cell = (lambda v: v.rjust(self.COL_W - self.GAP)) if money else (
            lambda v: v.ljust(self.COL_W - self.GAP))
        line = Text()
        line.append(label.ljust(self.LABEL_W)[: self.LABEL_W],
                    style=f"bold {GREEN}" if bold else DIM)
        line.append(cell(yours)[: self.COL_W - self.GAP], style=GREEN if bold else DIM)
        line.append(pad)
        line.append(cell(theirs)[: self.COL_W - self.GAP], style=value_style)
        line.append(pad)
        if diff:
            line.append(diff.rjust(self.DIFF_W)[-self.DIFF_W:], style=diff_style or DIM)
        return line

    @staticmethod
    def _signed_money(pence: float) -> str:
        return f"{'-' if pence < 0 else '+'}{_money(abs(pence))}"

    @staticmethod
    def _delta_style(delta: float) -> str:
        return CYAN if delta < 0 else RED

    @staticmethod
    def _window(start: int, end: int) -> str:
        """`04-07` when both ends sit on the hour, else `01:30-08:30`."""
        first, last = costing.slot_time(start), costing.slot_time(end)
        if first.endswith(":00") and last.endswith(":00"):
            return f"{first[:2]}-{last[:2]}"
        return f"{first}-{last}"

    def _band_lines(self, bands: list | None, summary) -> list[str]:
        """A tariff's prices as `9.00p  00:30-06:30`, cheapest first."""
        if bands is None:
            return [summary.unit, "repriced every 30 min"]
        return [
            f"{band.price_p:>6.2f}p  "
            + ", ".join(self._window(s, e) for s, e in band.windows)
            for band in bands
        ]

    def refresh_content(self) -> None:
        detail = self.detail
        if detail is None:
            self.update(Text("  select a tariff above to see where the difference comes from",
                             style=DIM))
            return
        if detail.option.is_current:
            self.update(Text("  this is your tariff - move the cursor to another row to compare",
                             style=DIM))
            return

        lines: list[Text] = [
            Text(""),   # breathing room under the table above
            self._row("", "YOURS", detail.option.name.upper(), "DIFFERENCE", bold=True,
                      value_style=f"bold {GREEN}"),
        ]

        # --- when each tariff charges what --------------------------------
        mine = self._band_lines(detail.current_bands, detail.current_rates)
        theirs = self._band_lines(detail.bands, detail.rates)
        for index in range(max(len(mine), len(theirs))):
            lines.append(self._row(
                "rates" if index == 0 else "",
                mine[index] if index < len(mine) else "",
                theirs[index] if index < len(theirs) else "",
            ))

        if detail.effective_p is not None and detail.current_effective_p is not None:
            delta = detail.effective_p - detail.current_effective_p
            lines.append(self._row(
                "you'd pay", f"{detail.current_effective_p:.2f}p/kWh",
                f"{detail.effective_p:.2f}p/kWh",
                f"{delta:+.2f}p", diff_style=self._delta_style(delta),
            ))
        if detail.current_rates.standing_p is not None and detail.rates.standing_p is not None:
            delta = detail.rates.standing_p - detail.current_rates.standing_p
            lines.append(self._row(
                "standing", f"{detail.current_rates.standing_p:.2f}p/day",
                f"{detail.rates.standing_p:.2f}p/day",
                f"{delta:+.2f}p", diff_style=self._delta_style(delta),
            ))

        # --- what it adds up to -------------------------------------------
        days = detail.days
        width = self.LABEL_W + (self.COL_W * 2) + self.DIFF_W
        lines.append(Text("─" * width, style=DIM))
        lines.append(self._row(
            f"usage {detail.kwh:,.0f} kWh",
            _money(detail.current_usage_p), _money(detail.usage_p),
            self._signed_money(detail.usage_delta_p),
            diff_style=self._delta_style(detail.usage_delta_p), money=True,
        ))
        lines.append(self._row(
            f"standing {len(days)}d",
            _money(detail.current_standing_p), _money(detail.standing_p),
            self._signed_money(detail.standing_delta_p),
            diff_style=self._delta_style(detail.standing_delta_p), money=True,
        ))
        lines.append(self._row(
            "TOTAL",
            _money(detail.current_usage_p + detail.current_standing_p),
            _money(detail.usage_p + detail.standing_p),
            self._signed_money(detail.total_delta_p),
            diff_style=f"bold {self._delta_style(detail.total_delta_p)}",
            bold=True, value_style=f"bold {COST_STYLE}", money=True,
        ))

        # --- and how it played out day to day -----------------------------
        if days:
            best = min(days, key=lambda d: d.delta_p)
            worst = max(days, key=lambda d: d.delta_p)
            lines.append(Text(""))
            lines.append(
                Text.assemble(
                    ("per day".ljust(self.LABEL_W), DIM),
                    (f"cheaper on {detail.cheaper_days} of {len(days)} days   ", CYAN),
                    ("best ", DIM),
                    (f"{best.date:%d/%m} {self._signed_money(best.delta_p)}   ",
                     self._delta_style(best.delta_p)),
                    ("worst ", DIM),
                    (f"{worst.date:%d/%m} {self._signed_money(worst.delta_p)}",
                     self._delta_style(worst.delta_p)),
                )
            )
            # A chart is a zero line, a date axis and at least one row of bars.
            spare = self.size.height - len(lines)
            if spare >= 3:
                lines.extend(self._delta_chart(days, spare))
        self.update(Group(*lines))

    def _delta_chart(self, days: list, rows: int) -> list[Text]:
        """Per-day difference against a zero line, with a money axis.

        Rows are split between the dearer and cheaper sides in proportion to
        how far each actually reaches, so a tariff that never once costs more
        spends none of its height on empty space above the line.
        """
        width = max(10, self.size.width - self.GUTTER - 1)
        if len(days) > width:
            days = days[-width:]
        widths = _column_widths(len(days), min(width, len(days) * 4))
        span = sum(widths)

        up = max((d.delta_p for d in days), default=0.0)
        down = -min((d.delta_p for d in days), default=0.0)
        body = max(1, rows - 2)                     # minus the zero line and dates
        if up <= 0:
            rows_up, rows_down = 0, body
        elif down <= 0:
            rows_up, rows_down = body, 0
        else:
            rows_up = max(1, min(body - 1, round(body * up / (up + down))))
            rows_down = body - rows_up

        lines: list[Text] = []
        for row in range(rows_up - 1, -1, -1):
            line = Text(self._tick(up if row == rows_up - 1 else None), style=DIM)
            for day, cell_w in zip(days, widths):
                filled = int(max(day.delta_p, 0.0) / up * rows_up * 8) if up > 0 else 0
                cell = filled - row * 8
                char = "█" if cell >= 8 else (_LOWER_BLOCKS[cell] if cell > 0 else " ")
                line.append(char * cell_w, style=RED)
            lines.append(line)

        zero = Text("£0.00 ┼".rjust(self.GUTTER), style=DIM)
        zero.append("─" * span, style=DIM)
        lines.append(zero)

        for row in range(rows_down):
            line = Text(self._tick(-down if row == rows_down - 1 else None), style=DIM)
            for day, cell_w in zip(days, widths):
                filled = int(max(-day.delta_p, 0.0) / down * rows_down * 8) if down > 0 else 0
                cell = filled - row * 8
                # Hangs downward from the zero line, so fill from the cell top.
                char = "█" if cell >= 8 else ("▀" if cell > 0 else " ")
                line.append(char * cell_w, style=CYAN)
            lines.append(line)

        dates = Text(" " * self.GUTTER)
        first, last = f"{days[0].date:%d/%m}", f"{days[-1].date:%d/%m}"
        dates.append(f"{first}{' ' * max(1, span - len(first) - len(last))}{last}", style=DIM)
        lines.append(dates)
        return lines

    def _tick(self, value: float | None) -> str:
        """Left gutter: a money label on the outermost row, else just the axis."""
        if value is None:
            return "│".rjust(self.GUTTER)
        return f"{self._signed_money(value)} ┤".rjust(self.GUTTER)


class SpikeTable(DataTable):
    """Bursts of demand picked out of the live trace, newest first."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("WHEN", "FOR", "PEAK", "USED", "COST")

    def update_spikes(self, spikes: list[Spike], baseline: float | None) -> None:
        self.clear()
        if not spikes:
            self.add_row("no spikes in window", "", "", "", "")
            return
        for spike in spikes[:40]:
            peak_style = RED if spike.peak_watts >= 1500 else DAY_STYLE
            self.add_row(
                Text(f"{spike.start.astimezone(UK):%H:%M}"
                     f"-{spike.end.astimezone(UK):%H:%M}", style=GREEN),
                Text(_duration(spike.duration), style=DIM, justify="right"),
                Text(f"{spike.peak_watts / 1000:.2f} kW", style=peak_style,
                     justify="right"),
                Text(f"{spike.kwh:.3f}", style=DAY_STYLE, justify="right"),
                Text(_money(spike.cost_p), style=COST_STYLE, justify="right"),
            )


class BillsPane(Static):
    """Issued statements, next to what this app computed for the same period."""

    # Beyond this divergence a statement almost certainly contains charges that
    # are not metered usage - credits, adjustments, or a catch-up after a meter
    # exchange - so calling it a discrepancy would be misleading.
    DIVERGENCE_LIMIT_PCT = 25.0

    def update_bills(self, rows: list[dict], computed: dict[str, float]) -> None:
        if not rows:
            self.update(Text("  no issued bills found", style=DIM))
            return
        lines: list[Text] = []
        for row in rows[:5]:
            totals = row.get("totalCharges") or {}
            gross = totals.get("grossTotal")
            start, end = row.get("fromDate"), row.get("toDate")
            if gross is None or not start or not end:
                continue
            line = Text()
            line.append(f"{start[5:]}→{end[5:]} ", style=DIM)
            line.append(f"{_money(gross):>9}", style=COST_STYLE)
            ours = computed.get(f"{start}:{end}")
            if ours is None:
                line.append("  no settled data", style=DIM)
            else:
                pct = ((ours - gross) / gross * 100) if gross else 0
                line.append(f"  ours {_money(ours):>8}", style=DAY_STYLE)
                if abs(pct) > self.DIVERGENCE_LIMIT_PCT:
                    line.append("  non-usage chgs?", style=RED)
                else:
                    line.append(f" {pct:+.1f}%", style=GREEN if abs(pct) < 8 else DAY_STYLE)
            lines.append(line)
        lines.append(Text(""))
        lines.append(Text("billed vs computed, inc VAT", style=DIM))
        self.update(Group(*lines))



    def clear(self) -> None:
        self.add_class("hidden")
        self.update("")
