# OCTOSCOPE

A terminal dashboard for tracking your own electricity usage and spend on
Octopus Energy: live demand from the Home Mini, daily usage and cost trends,
and a projected monthly bill.

```
./run.sh
```

| Key | Action |
| --- | --- |
| `tab` | cycle views: **chart → table → live → tariffs** |
| `↑` `↓` | table/tariffs: move the cursor — on TARIFFS this picks the comparison |
| `1`–`5` | chart/tariffs: 7d / 30d / 90d / all · table: 5min / 30min / 60min / day / month · live: granularity |
| `←` `→` | live view: pan history by half a window |
| `shift`+`←` `→` | live view: step one bucket at a time |
| `home` | live view: jump back to now |
| `r` | force refresh |
| `l` | swap the spikes pane for the event log |
| `q` | quit |

## The four views

**CHART** — usage bars (day/night stacked) with a separate cost chart and its
own money axis. Window (`1`–`6`) and granularity (`g`) are chosen
independently, and the chart scrolls.

**TABLE** — rollups at 5 / 30 / 60 minutes, day, month.

**LIVE** — current draw as a large readout, cost per hour at that draw, and a
power trace with a real clock axis. Keys `1`–`4` change granularity and window
together: 10 sec/30 min · 1 min/2 hr · 5 min/6 hr · 30 min/24 hr. Shows how much
telemetry budget is left this hour.

**TARIFFS** — your actual consumption priced on every comparable Octopus tariff
over the selected period, cheapest first, including standing charges. Move the
cursor onto a row to break that tariff down.

## Spikes

The SPIKES pane itemises bursts of demand in whatever window the live view is
showing: when each started and ended, how long it ran, its peak, the energy it
used, and what it cost at the rate in force at the time — so an evening burst
and the same burst inside the cheap night window are priced differently.

The baseline is **local and low-quartile**, not a global median. Two things
forced that:

- A single median over 24 hours sits between the quiet night and the busy day,
  so the entire daytime plateau cleared the threshold and reported as one
  ten-hour "spike".
- A rolling *median* is dragged upward by the very bursts being looked for. On
  real data that pushed the trigger to 2,458 W and lost a genuine 2,353 W
  burst. The low quartile over a ±2 hour window is not, because a burst is a
  small minority of its own window.

A spike is then a run above `local + max(400 W, local × 0.75)`, with hysteresis
(it ends only once demand falls halfway back to ambient) and gap-stitching
capped at **five minutes** — three buckets is 90 minutes at half-hourly
granularity, which welded separate appliance runs into one all-day event.

Validated against a 24-hour window containing four visible bursts: all four are
reported separately, at 13:30, 15:00, 16:00 and 19:00.

Scrubbing the live window back with `←` recomputes spikes for that period, so
you can walk back through the day and see what each burst cost.

## Scrolling live history

`←`/`→` pan by half a window; `shift`+`←`/`→` step one bucket, which is what you
want when lining the window up on a specific event.

Telemetry is kept in a local time series per granularity (`store.py`) rather
than cached per window. Each fetch records the range it covered, so a new window
asks only for the parts not already held, and gaps are padded **backwards** to
six hours before fetching — scrolling back opens gaps at the leading edge, so
padding forwards would refetch what is already stored and buy only one new
bucket. Measured result: **ten single-bucket steps cost zero API calls.**

The headline reading is deliberately independent of the chart. It always shows
the meter's latest value, so changing granularity or scrolling into history
never alters what the house is drawing right now.

## What it shows

**Top row — where you are right now**

| Tile | Answers |
| --- | --- |
| NOW | What am I drawing this second, and what does that cost per hour? |
| TODAY | How much have I used and spent today, vs yesterday *at the same time of day*? |
| THIS MONTH | Month-to-date kWh and spend, including standing charge |
| FORECAST | What is this month's bill likely to come to? |

**Middle — the trend.** Usage as stacked columns (blue = night register, green =
day), a cost trace beneath, and totals/mean/peak for the selected period.
Timeframe and granularity are chosen independently — see below.

### Timeframe and granularity

Two separate axes, and both matter:

| Key | Controls | Options |
| --- | --- | --- |
| `1`–`6` | how much history | 12 hours, 24 hours, 7 days, 30 days, 90 days, all |
| `g` | how finely it is sliced | 30 min, hour, day, week, month |

A period is a **window ending now**, not a count of complete days — which is
what makes 12 and 24 hours expressible at all, since the last day is mostly
unsettled and gets filled from the Home Mini.

Picking a short period steps the grain finer if the current one would draw
fewer than four bars (12 HOURS while on MONTH becomes 12 HOURS · HOUR). It only
ever refines, and only on a period change — `g` afterwards goes wherever you
send it.

### One pool, so the totals cannot drift

Every figure on this chart comes from a single merged half-hourly series:
settled records, with Home Mini telemetry substituted per day where settlement
has published less than the meter recorded. Buckets are formed from that pool,
so **each reading lands in exactly one bar at every grain** and the period total
is arithmetically identical however it is sliced:

```
PERIOD     GRAIN    BARS        kWh    COST £  WHOLE  PART
ALL        30 MIN   5882   1485.058    444.52   5882     0
ALL        HOUR     2942   1485.058    444.52   2940     2
ALL        DAY       125   1485.058    444.52    120     5
ALL        WEEK       18   1485.058    444.52     15     3
ALL        MONTH       5   1485.058    444.52      2     3
```

This is not a coincidence to be re-checked by hand; it is the reason the code is
shaped this way. Totals used to be assembled per grain from whichever buckets
survived filtering, and they disagreed — the day path dropped interior
part-recorded days that the hourly path swallowed, and a trailing partial week
silently discarded the complete days inside it.

**Totals and means deliberately use different sets.** The total covers every
bar, part-recorded ones included, because that is the energy the period actually
contains. The mean and peak cover only whole bars, because a half-recorded unit
is not a real dip and averaging it in understates usage. Part-recorded bars are
shaded `▒` and the footnote says how many there are.

Settled consumption arrives from Octopus **half-hourly** — one record per meter
slot, 48 per day. The chart used to aggregate all of it to days before drawing,
which threw away the intraday shape. It no longer does: 30 MIN is the meter's
own resolution and every coarser grain is a plain sum of it. Nothing is
interpolated or smoothed, and the totals in the summary line are identical at
every grain — only the number of bars changes.

This is what makes the tariff pane legible. Cosy's 16:00–19:00 peak band or
Go's 00:30–05:30 cheap window only mean something against your actual usage at
those hours, and at DAY granularity you cannot see it.

### Scrolling

Sub-day grains generate more bars than a terminal has columns — 30 days at 30
MIN is 1,440 — so the chart scrolls, using the same keys as the live view:

| Key | Does |
| --- | --- |
| `←` / `→` | back / forward half a screen |
| `shift+←` / `shift+→` | one bar at a time |
| `home` | jump back to the latest data |

The visible span is always spelled out beneath the chart:

```
showing  11/07 19:00 → 17/07 21:00  (147 of 720 hours, totals above cover all of them)   ← → scroll   home = latest
```

Unlike the live view, scrolling here **never fetches** — it pans over records
already in memory, so it costs nothing and cannot fail.

Two deliberate behaviours: bar width is computed from the whole series rather
than the visible slice, so bars keep their size while you scroll instead of
resizing under the cursor; and scroll position **survives a background
refresh**, so a telemetry poll cannot yank you back to the present while you are
reading last month. Changing period or grain does reset it, because you asked
for different data.

The y-axis rescales to the visible window, so a quiet week still shows detail
rather than a flat line. The axis is labelled, so the scale is never implied.

### Axis labels

At sub-day grains only some columns can be labelled — a bar is one character
wide. Midnight columns carry the **date**, everything else carries the time, so
a week of hourly bars reads `12/07 … 13/07 … 14/07` rather than a useless row of
`00:00`.

**Bottom — day vs night**, which is the lever that actually matters on
Economy 7, and a secondary tariff comparison pane.

## Table view (`tab`)

Rollups at 5 / 30 / 60 minutes, day, and month, with columns for period, usage,
tariff register, and total cost. The 5-minute rollup comes from per-minute Home
Mini telemetry (recent hours only); everything coarser comes from settled
consumption, which reaches back months.

The **cost column is additive**: 30-minute rows sum exactly to their hour, hours
to the day, days to the month. That requires apportioning the daily standing
charge across each reading by elapsed time, rather than dropping it from
sub-day rows — verified to the penny in `smoketest.py`.

The one deliberate exception is the **period in progress**, marked `· in
progress`. Its usage comes from live telemetry rather than lagging settled data
(so it agrees with the TODAY tile), and it carries the day's standing charge in
full, because you incur that whatever the hour.

## How costs are calculated

All figures are pence **including VAT**, computed from REST unit rates rather
than from telemetry's `costDelta` — so every number on screen uses one
convention. The engine cross-checks against Octopus's own costing and matches
it to the penny.

### Does a figure include the standing charge?

| Surface | Standing charge |
| --- | --- |
| TODAY tile | included, broken out on its own line |
| THIS MONTH / FORECAST | included |
| Trend chart + summary | included (labelled `[inc standing]`) |
| Table view, all rollups | included (pro-rata; in-progress period in full) |
| DAY vs NIGHT split | **excluded** — standing is not attributable to a register |
| TARIFF COMPARISON | **excluded** — both sides, so the comparison stays fair |

The two exclusions are labelled in the UI. Your standing charge has changed
several times since move-in (47.70 → 48.39 → 50.86p/day); each day is costed
with the rate in force on that day, not today's.

Two things are **detected at runtime rather than hardcoded**, because both are
easy to get silently wrong:

- **Payment method.** The unit-rate endpoint returns `DIRECT_DEBIT` and
  `NON_DIRECT_DEBIT` rows for the same period, differing by ~5.5%. Picking the
  wrong one inflates every figure. Octoscope infers it by comparing Octopus's
  own per-half-hour charges against both candidates.
- **The Economy 7 night window.** This is not a fixed national time — it is
  whatever your meter was configured with. Octoscope recovers it by asking what
  Octopus actually charged per half hour and finding where the rate steps down.
  On this meter every slot bills at 31.10p except 01:30–08:29, which bills at
  13.04p, with zero variance — so the window is **01:30–08:30**, measured rather
  than assumed.

  The DAY vs NIGHT pane states which it is: `measured` or `ASSUMED`. This
  matters because the fallback default happens to be the same 01:30–08:30, so
  without the label a silent calibration failure would be indistinguishable from
  a successful one.

Settled consumption lags real time, so recent days are often incomplete.
Incomplete days are excluded from trend bars and daily means — otherwise a
half-recorded day reads as a real drop in usage — and are filled in from the
Home Mini where it has them. See [Provisional days](#provisional-days).

## What the comparable tariffs actually are

Only **Agile** is market-priced. Measured over 14 days:

| Product | Registers | Distinct prices | Shape |
| --- | --- | --- | --- |
| Flexible (yours) | 2 | 1 + 1 | flat 32.65p day / 13.70p night |
| Agile | 1 | 546 | repriced every half hour, -6.73p to 52.93p |
| Snug | 1 | 2 | 9.00p 00:30–06:30, else 27.75p |
| Go | 1 | 2 | 8.63p 00:30–05:30, else 31.18p |
| Cosy | 1 | 3 | 13.09p / 26.67p / 40.01p on a fixed daily timetable |
| 12M Fixed products | 1 or 2 | 1–3 | as above, but price-locked for a year |

This is why the breakdown no longer says "varies" for everything. A tariff with
a handful of prices runs a **published timetable you can plan around**; Agile is
a **market price you cannot**. Calling both "varies" implied Cosy and Snug were
Agile-like, which they are not — they are as predictable as your current tariff,
just with more bands.

Fixed-term products are included (`OE-FIX-12M`, `OE-FIX-12M-LOWSC`, `COSY-FIX`,
`GO-FIX`). On this account's usage all four price **above** the variable
equivalents, so leaving them out made the pane look like Octopus sells nothing
at a fixed price when the truth is that fixing currently costs more. They also
carry a £50 exit fee that the comparison does not model, so a fixed-tariff
saving is never quite as free as the number suggests.

## The tariff comparison pane

You are on Flexible Economy 7 (`E-2R-VAR-22-11-01-A`), which has two flat rates
that change roughly quarterly. Agile is a different product with half-hourly
pricing that can go negative ("plunge pricing"). The comparison pane prices your
*actual* consumption against Agile rates over the overlapping period, so you can
see whether switching would have paid off.

Both sides are costed over **exactly the same half hours** — Agile rates only
exist where Octopus has published them, and comparing an Agile subtotal against
a full-period actual would overstate the difference badly.

Plunge alerting was removed. It was answering "is electricity briefly cheap on a
tariff you are not on" — the comparison pane answers the better question against
real consumption, and the alert cost a scheduled API call every 30 minutes to
feed a banner nothing else read.

### Reading the breakdown

Selecting a row explains it as fixed columns — yours, theirs, the difference —
because the question is always a comparison and prose on one line gives the eye
nothing to run down:

```
               YOURS                         COSY OCTOPUS                   DIFFERENCE
rates          13.70p  01:30-08:30           13.09p  04-07, 13-16, 22-00
               32.65p  08:30-01:30           26.67p  00-04, 07-13, 19-22
                                             40.01p  16-19
you'd pay      27.12p/kWh                    25.16p/kWh                         -1.97p
standing       50.86p/day                    52.19p/day                         +1.33p
──────────────────────────────────────────────────────────────────────────────────────
usage 375 kWh     £101.78                       £94.40                          -£7.38
standing 30d       £15.11                       £15.66                          +£0.55
TOTAL             £116.89                      £110.06                          -£6.84
```

**`rates` shows when each price applies**, derived by evaluating the tariff at
all 48 half-hours of a day and merging equal neighbours into bands (a band that
runs through midnight is one window, not two). Saying a tariff ranges from
13.09p to 40.01p without saying *when* is not information you can act on.

This also removes a distinction that was never real. Your Economy 7 meter has
two registers and Cosy's has one, but from where you stand they are the same
mechanism: some prices, and the hours they apply. Register count is a billing
detail, so the summary table's column reports pricing **shape** instead —
`2 by clock`, `3 by clock`, `half-hourly`, `flat`.

Every column is padded to its full width with a fixed gap, so a long value can
never run into the column beside it. Money is right-aligned so decimal points
stack; rates are left-aligned because each carries a time range after it.

`you'd pay` is the effective pence per kWh actually paid over the period. It is
the row that makes a half-hourly tariff comparable to a flat one — Agile's
headline range of -5.38p to 57.17p says nothing on its own, but 23.87p/kWh
against your 27.13p does.

Beneath it, the per-day difference against a zero line, with a money axis:
dearer days in red above, cheaper in cyan below. Rows are split between the two
sides **in proportion to how far each actually reaches**, so a tariff that never
once costs more spends none of its height on empty space above the line.

### Why it used to melt the CPU

Pricing a year of half-hourly readings against Agile means ~17,500 readings
against ~17,500 rate records. `RateTimeline.at()` was a linear scan that did not
even stop at the first match, so that single tariff cost ~300 million
comparisons — on the UI thread, with no indication anything was happening.

Three things fixed it, none of them a database:

- **Binary search** instead of a linear scan (`at()` is ~176x faster; the whole
  comparison ~21x). The records are sorted by `valid_from` already.
- **Build the timelines once**, over the full history, instead of per period.
  A shorter period just looks up fewer of them. This also stopped every `1`-`4`
  keypress from re-fetching and re-parsing every product's rates.
- **Fix the cache key.** `unit_rate_records` put `period_to` into the key to the
  second, and callers pass `now + 2 days` as the horizon — so the key was unique
  on every call and the disk cache never hit once. Windows are now snapped out
  to whole UTC days, which only ever widens them. Cold 6.4s, warm 0.2s.

The remaining pricing work runs in a worker thread, so the event loop stays free
to animate the spinner no matter how much history is loaded.

## The spinner

Anything that can take more than an instant — a network round trip, a re-price
across every tariff — runs inside `app.busy("label")`, which shows a moving bar
and the job name on the status line. Without it a slow load was
indistinguishable from a hang.

Two things about it are deliberate:

- **The row is always present**, never shown and hidden. Toggling `display`
  reflowed every pane below it, so the entire dashboard jumped each time any
  background job started or finished.
- **It is drawn with block glyphs** (`█`/`░`), not braille. Braille needs font
  coverage a terminal may not have, and degrades to boxes or blanks when it does
  not. Blocks are what the charts already draw, so if the dashboard is legible
  at all, so is the indicator.

The busy set is reference-counted rather than a boolean, because the same job
can legitimately overlap itself (a poll landing while a keypress-triggered
render of the same pane is still running) and whichever finishes first must not
clear the indicator for the one still going.

`load_tariff_options` runs in a **shielded** task shared by all callers. The
tariff render is an exclusive worker, so holding `1`-`4` cancels it mid-fetch;
without the shield each press abandoned a part-finished load and started
another, and the options never finished building at all.

## Layout

```
octoscope/
  config.py    credentials, poll intervals, cache TTLs
  cache.py     TTL'd disk cache; every API response goes through it
  api.py       REST + GraphQL clients, account/meter discovery
  costing.py   calibration, rate timelines, day/night costing, forecasting
  model.py     Agile price slots, plunge detection, sparklines
  widgets.py   the panes
  app.py       layout, polling schedule, alerting
  app.tcss     green-phosphor styling
smoketest.py   exercises the API and costing engine without the TUI
```

## Where the data comes from

- **REST** (`api.octopus.energy/v1`) — account, unit rates, standing charges,
  half-hourly consumption. Billing-grade, lags a day.
- **GraphQL** (`api.octopus.energy/v1/graphql/`) — `smartMeterTelemetry`, the
  only route to Home Mini data. The Mini has **no local network API**; it reads
  your meter over Zigbee and pushes to Octopus's cloud, so live data is fetched
  back out rather than read off your LAN.

## The NOW tile and export

When solar covers the whole house, demand goes negative. Three things have to
agree, and all three were wrong at once:

- The **sparkline** scaled from its minimum, so zero sat halfway up the glyph
  range and an exporting house drew a mid-height bar next to a minus number.
  It is now pinned to zero (`sparkline(..., floor=0)`).
- The **cost per hour** multiplied negative watts by the unit rate and printed a
  negative number. There is no export MPAN on this account: surplus earns
  nothing, so import cost floors at £0.00.
- Export is now drawn in its own colour and labelled, rather than being styled
  as if it were very low consumption.

## Solar export

This property generates. The `demand` field goes **negative** when on-site
generation exceeds consumption — measured at up to **-1,256 W**, with the import
register advancing only 3 Wh across 90 minutes of it.

This matters for how the live trace is drawn. `consumptionDelta` floors at zero,
so relying on it alone renders export as flat nothing, indistinguishable from an
empty house. The trace therefore plots signed net flow with a zero baseline:
import above the line, export below it in purple.

There is **no export MPAN on the account**, so the generation offsets your import
but is not metered or paid for through Octopus.

At any instant there are only two states — importing or exporting — and solar
reduces the bill in both. The limitation is not a hidden third state; it is that
the meter reports one number, net flow, which is household demand minus
generation. Two unknowns, one equation. So generation cannot be recovered from
it: a reading of 500 W could be 500 W of demand with no sun, or 2,500 W of demand
with 2,000 W of generation.

The consequence is that **exported energy is measurable** (net flow is negative,
and all of it is generation) while **generation consumed on site is not**. Every
usage figure in this app is therefore metered import — correct for what you are
billed, but lower than true household demand by an unknown amount.

## Provisional days

Settled billing data does not lag a reliable 24 hours. Octopus has been observed
publishing the first hour of a day and then nothing for another day and a half.
Two things follow.

First, a day is only complete when it has all of its half hours (46 or 50 on the
clock-change days, otherwise 48). Judging by date alone meant a day holding 2 of
48 slots counted as a full day, quietly dragging every mean, forecast and
"vs yesterday" figure down — the trend read as a collapse in usage that never
happened.

Second, where settled data is unfinished the Home Mini already has the day, and
agrees with settled data to within 0.001 kWh (see below). Those days are filled
in from telemetry and drawn **hatched in amber** with a `~` axis tick, a legend
naming the dates, and the same treatment on the cost chart. They count towards
means and forecasts, because they are complete and accurate — but they are not
what Octopus has billed, and the chart never pretends otherwise.

The window is bounded by `PROVISIONAL_DAYS` (3). It costs no extra API calls:
the existing half-hourly telemetry request simply starts earlier.

## Live versus settled data — the `o` overlay

Press `o` on the chart to swap it for a day-by-day reconciliation of the Home
Mini's record against settled billing. It is an overlay rather than a view of
its own, because the question it answers — *can I trust the provisional bars?* —
only comes up while looking at the chart.

```
  DATE    SETTLED  HOME MINI     DELTA          AGREEMENT
  18/07    16.851      1.554         -       -  incomplete · 48/48 settled, 3/48 live
  19/07    22.761     22.757    -0.004  -0.02%  ························  exact
  20/07    11.308     11.312    +0.004  +0.04%  ························  exact
  21/07    14.126     14.127    +0.001  +0.01%  ························  exact
  22/07    11.175     11.175    +0.000  +0.00%  ························  exact
  23/07    25.604     25.605    +0.001  +0.00%  ························  exact
  24/07     0.440      8.985         -       -  incomplete · 2/48 settled, 48/48 live

  5 complete days  settled 84.974 kWh  vs  mini 84.976 kWh   drift +0.002%   worst day 20/07 +0.04%
```

**They agree.** Across five fully-recorded days the two sources differ by 2 Wh
in 85 kWh — 0.002%. The worst single day is 0.04% out. The residual is rounding:
telemetry reports whole watt-hours, settlement reports kWh to three decimals,
and the error cancels rather than accumulating.

That is the evidence behind the provisional bars. Substituting Home Mini figures
for days Octopus has not settled is not an approximation to apologise for — it
is the same meter reporting the same energy.

Days incomplete in **either** source are listed but not scored. A day the Mini
covered 3 of 48 half-hours for is not a discrepancy, it is a gap, and reporting
it as a −90% delta would be a lie. The bar is scaled to 5% full-width, beyond
which the exact figure stops mattering.

The overlay reads only what is already on disk — the telemetry store plus this
session's provisional buckets — so opening it **never costs an API call**. Its
reach is bounded by how far back Octopus serves half-hourly telemetry, about six
days. If the two sources happen not to overlap, it says so rather than fetching
to manufacture an answer.

## Rate limits — the actual numbers

`Query.smartMeterTelemetry` is limited to **125 requests per hour**. That is not
a guess: the API reports it itself, and you can check your live standing at any
time:

```graphql
query { rateLimitInfo {
  pointsAllowanceRateLimit { limit remainingPoints usedPoints isBlocked }
  fieldSpecificRateLimits(first:100){ edges{ node{ field rate ttl isBlocked } } } } }
```

Two separate limits exist and it matters which one you are hitting:

- **Points allowance** — 300,000/hour on this account. Never came close; a full
  session uses well under 100 points.
- **Per-field limits** — this is the one that bites. `smartMeterTelemetry` at
  125/h, independent of the points budget.

Polling every 30 s is 120 calls/hour on its own, which leaves no room for
anything else and trips the limit within the hour. The app now budgets for it:

| Call | Transport | Frequency | Calls/hour |
| --- | --- | --- | --- |
| Live demand | GraphQL | every 60 s | 60 |
| Today's totals | GraphQL | cached 10 min | 6 |
| 5-min rollup | GraphQL | cached 10 min | ≤6 |
| Calibration | GraphQL | cached 12 h | ~0 |
| Settled consumption, rates | REST | cached 30 min | — |

### How much one call returns

Measured against the live API. The ceiling is on **time span**, not row count —
`HALF_HOURLY` over 30 days is only 1,440 rows and still fails, while
`ONE_MINUTE` over 24h returns 1,441 rows fine:

| Grouping | Max span | Rows returned | Payload | Beyond that |
| --- | --- | --- | --- | --- |
| `TEN_SECONDS` | 12 h | 3,876 | ~500 KB | 18 h → zero rows |
| `ONE_MINUTE` | 72 h | 4,321 | ~570 KB | 144 h → zero rows |
| `HALF_HOURLY` | 144 h (6 d) | 288 | ~38 KB | 168 h → error |

So a single call carries roughly **4,000 rows / half a megabyte**, at ~131 bytes
per row and 2–3 seconds. Fetches are chunked to these spans so each request earns
its slot in the budget, and scrolling is clamped to each granularity's reach
rather than silently showing an empty chart.

A client-side `TelemetryBudget` hard-caps telemetry at **110/hour** and refuses
calls beyond it, serving cached data instead. Call times are **persisted to
disk**: the server's window is a rolling hour that takes no notice of restarts,
so an in-memory counter would reset to zero on every launch and spend an
allowance that was already gone. The LIVE view shows the remaining API budget
and when the next slot frees up. Declining locally is much better
than spending the breach, because the server blocks the whole field once
exceeded. The LIVE view shows the remaining budget.

When throttling does occur the client reads the API's own `ttl` — an absolute
timestamp — and waits exactly that long rather than guessing, logging it once
instead of once per tick.

**Worth knowing:** Octopus's dynamic limits get progressively stricter each time
they are breached, and per their docs *do not automatically reset*. If telemetry
stays blocked long after the budget should have recovered, that is worth raising
with Octopus support rather than waiting out.

## Bills

`account.bills` returns issued statements with gross/net/VAT and the period
covered, compared in the BILLS pane against what this app computes for the same
window. The most recent full statement matched to **-3.9%**, which is a useful
independent check on the costing engine.

Older statements are marked `no settled data`: consumption is only retrievable
from **2026-03-23**, when the current meter was installed. The previous meter on
the same MPAN returns no records at all through the API, so earlier bills cannot
be reconciled. A statement diverging by more than 25% is flagged
`non-usage chgs?` rather than reported as a discrepancy, since it almost
certainly contains credits or adjustments rather than metered usage.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`.env` needs:

```
octopus_api_key=sk_live_...
account=A-XXXXXXXX
```

Both are gitignored, along with `.cache/`.

## Possible next steps

- Desktop notification / ntfy.sh push (currently in-TUI only, by choice)
- Per-half-hour heatmap to spot which times of day dominate the bill
- Compare against the same month last year once there is a year of history
# octoscope
