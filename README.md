# OCTOSCOPE

A terminal dashboard for tracking your own electricity usage and spend on
Octopus Energy: live demand from the Home Mini, daily usage and cost trends,
and a projected monthly bill.

```
./run.sh
```

| Key | Action |
| --- | --- |
| `tab` | cycle views: **chart → compare → table → live → tariffs** |
| `1`–`9` | pick the **time range** — see below · live view: trace resolution |
| `g` | pick the **grain** the range is sliced into |
| `←` `→` | step the range earlier / later — the previous day, week, month… |
| `home` | back to the present |
| `shift`+`←` `→` | scroll *within* the range, when it is wider than the screen |
| `↑` `↓` | table/tariffs: move the cursor — on TARIFFS this picks the comparison |
| `m` | compare view: swap kWh for cost |
| `o` | chart view: overlay settled billing against the Home Mini |
| `r` | force refresh |
| `p` | pause/resume background polling — see [Stepping away](#stepping-away) |
| `l` | swap the spikes pane for the event log |
| `q` | quit |

## Range and grain

Two controls, and they mean the same thing on every view: a **range** is which
stretch of time you are looking at, a **grain** is how finely it is sliced.

```
 1 TODAY  2 YESTERDAY  3 THIS WEEK  4 THIS MONTH │ 5 LAST 24 HOURS  6 LAST 7 DAYS  7 LAST 30 DAYS  8 LAST 90 DAYS │ 9 ALL │ g 6 HOUR  12 HOUR  DAY  WEEK
```

That row sits above the panes at all times and is the authority on what the
number keys will do — including on the live view, which relabels it, being the
one view about the present moment rather than a stretch of history. The active
option is highlighted, so "what am I looking at?" is answered by looking.

`1`–`4` are **calendar** ranges: today is today, this week starts on Monday.
`5`–`8` are **rolling** windows ending now, which is a genuinely different
question — worth its own key rather than being approximated by the nearest
calendar period. `9` is everything on record.

Grain is offered **per range**. A range names the grains it can sensibly be
drawn at and `g` cycles through those, so there is no month-wide bar across a
single day and no four thousand half-hour columns across a year. Switching
range keeps the closest grain to the one you were on rather than resetting.

Ranges step. `←` moves to the week before, the month before, the day before;
`home` returns to the present, and pressing the range's own number key again
does the same. The chart, table, compare and tariffs views all read this one
selection, so switching view keeps you where you were in time.

## The five views

**CHART** — usage bars (day/night stacked) with a separate cost chart and its
own money axis, at the selected range and grain.

**COMPARE** — the selected range against the one before it: today vs yesterday,
this week vs last, this month vs last. The period in progress is compared
against the previous one measured to the same point in itself, so the change is
like for like all day rather than only at midnight. `m` swaps kWh for cost.

**TABLE** — the chart's own buckets as exact figures, at the same range and
grain. Finer than 30 minutes is live telemetry, which is the LIVE view.

**LIVE** — current draw as a large readout, cost per hour at that draw, and a
power trace with a real clock axis. Keys `1`–`4` change granularity and window
together: 10 sec/30 min · 1 min/2 hr · 5 min/6 hr · 30 min/24 hr. The caption
counts down to the next poll (`update in 34s`) and shows how much telemetry
budget is left this hour.

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

Telemetry is kept in a local archive per granularity (`db.py`, planned by
`store.py`) rather than cached per window. Each fetch records the range it
covered, so a new window asks only for the parts not already held, and gaps are
padded **backwards** to six hours before fetching — scrolling back opens gaps at
the leading edge, so padding forwards would refetch what is already stored and
buy only one new bucket. Measured result: **ten single-bucket steps cost zero
API calls.**

Because the archive outlives the API's retention, scrolling is bounded by what
has been *recorded*, not by what Octopus will still serve — see
[The archive](#the-archive). Gaps older than the API's reach are dropped from
the fetch plan rather than requested and refused, since an impossible request
still costs a slot in the hourly budget.

**Scrolling never waits on the network.** Widening a gap backwards is right for
the budget — one request buys eleven hours instead of one bucket — but it means
a 90-second hole triggers a 3,600-row, half-megabyte request. Blocking the
render on that made scrolling feel broken while the archive it was waiting for
already held almost all of the answer. The query now returns immediately and the
fetch runs in a worker that redraws when it lands:

```
step  1:  110.1 ms   step  5:  107.2 ms   step  9:  109.8 ms
step  2:  113.0 ms   step  6:  108.8 ms   step 10:  110.4 ms
step  3:  151.1 ms   step  7:  108.4 ms   step 11:  128.1 ms
step  4:  110.4 ms   step  8:  109.3 ms   step 12:  133.6 ms
```

That floor is the terminal event loop settling, not the data: the archive read
behind it is **0.1 ms** for a 30-minute window. Only genuinely absent hours
involve Octopus at all.

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
day), a cost trace beneath, and totals/mean/peak for the selected range.

### Range and grain

Two axes, and both matter:

| Key | Controls | Options |
| --- | --- | --- |
| `1`–`9` | which stretch of time | today, yesterday, this week, this month · last 24 hours, 7 / 30 / 90 days · all |
| `g` | how finely it is sliced | 30 min, hour, 6 hour, 12 hour, day, week, month — whichever the range allows |

**6 hour and 12 hour** exist because the step from HOUR to DAY was a cliff: a
month at HOUR is 720 bars you have to scroll through, and the same month at DAY
is 30 bars that have thrown away every trace of when you actually used the
electricity. The blocks are anchored to local midnight — 00/06/12/18 and
00/12 — so they line up with days rather than drifting off whatever hour you
happened to open the app. Note that the 12 hour boundary is midnight/noon, not
your Economy 7 changeover — the whole night window falls inside the first bar,
so read the day/night stacking within a bar rather than treating the two bars
as the two registers.

They cost nothing to add. Settled data is half-hourly, so these are sums of
records already on disk; no new call fetches them.

Ranges come in two kinds, because the two answer different questions. A
**calendar** range snaps to real boundaries — TODAY is midnight to now, THIS
WEEK starts on Monday — which is what people mean when they ask. A **rolling**
range is a window ending now, which is the right shape for "how have the last
24 hours gone" and is only expressible because the unsettled tail is filled
from the Home Mini.

Grain is not free-running: each range names the grains it can be drawn at, and
`g` only offers those. TODAY goes down to half-hours and no coarser than
6-hour blocks; ALL goes day, week, month. Changing range keeps the nearest
grain to the one you had rather than snapping to a default, so stepping from
LAST 7 DAYS to LAST 30 DAYS at 6 HOUR stays at 6 HOUR.

This replaced a rule that inferred a grain from a rolling window only when the
window would have drawn fewer than four bars — which meant the relationship
between the two controls held sometimes, in one direction, and only on one of
the five views. The control bar showing which grains are on offer is half the
fix; the other half is that there is now one selection behind every view.

### One pool, so the totals cannot drift

Every figure on this chart comes from a single merged half-hourly series:
settled records, with Home Mini telemetry substituted per day where settlement
has published less than the meter recorded. Buckets are formed from that pool,
so **each reading lands in exactly one bar at every grain** and the period total
is arithmetically identical however it is sliced:

```
PERIOD     GRAIN    BARS        kWh    COST £  WHOLE  PART
ALL        30 MIN   5857   1481.875    443.67   5857     0
ALL        HOUR     2929   1481.875    443.67   2928     1
ALL        6 HOUR    491   1481.875    443.67    487     4
ALL        12 HOUR   247   1481.875    443.67    243     4
ALL        DAY       125   1481.875    443.67    121     4
ALL        WEEK       18   1481.875    443.67     15     3
ALL        MONTH       5   1481.875    443.67      2     3
```

Seven grains, one number. That column does not drift by a penny across a
2,000× change in bar count, and `dbtest.py` asserts it rather than leaving it to
be eyeballed.

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
MIN is 1,440 — so the chart scrolls. Scrolling pans the viewport within the
range; it does not change what is selected, which is why it is the modified key:

| Key | Does |
| --- | --- |
| `shift+←` / `shift+→` | pan back / forward half a screen |
| `←` / `→` | step to the range before / after this one |
| `home` | jump back to the latest data |

The visible span is always spelled out beneath the chart:

```
showing  11/07 19:00 → 17/07 21:00  (147 of 720 hours, totals above cover all of them)   shift+← → scroll   home = latest
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

## Compare view (`tab`)

Usage across two adjacent periods of the same length. It reads the same range
as every other view — `1` (TODAY) compares today with yesterday, `3` (THIS
WEEK) this week with last, `9` (ALL) this year with last — and `←` `→` step the
pair back through history a whole period at a time, `home` returns to the
present. `m` swaps the chart between kWh and money; the headline shows both
either way. Grain is fixed by the frame here (a day is read hour by hour, a
year month by month), so the control bar shows no grain picker rather than
offering one that does nothing.

```
THIS WEEK                  51.72 kWh     £15.06   so far · day 7 of 7
LAST WEEK                  85.67 kWh     £24.08   to the same point
CHANGE                    -33.95 kWh     -£9.02   -39.6% by kWh
LAST WEEK in full          87.97 kWh     £24.79
  biggest day apart  Thu 30 Jul  -20.00 kWh

 25.6┤                                                 ────────────────
 21.9┤
     │
 14.6┤                ────────────────                                     ████████████████
     │────────────────████████████████─────────────────      ─────────────████████████████─────────────────
  7.3┤▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅████████████████▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅────────────────
     │███████████████████████████████████████████████████████████████████████████████████████▁▁▁▁▁▁▁▁▁▁▁▁▁
  0.0└──────────────────────────────────────────────────────────────────────────────────────────────────────  kWh
      Mon             Tue             Wed              Thu             Fri              Sat             Sun
  █ this week    ─── last week, day by day
```

### Like for like

A period in progress has only got so far into itself. Holding a Tuesday morning
up against the whole of Monday reports a collapse in usage every morning and a
recovery every evening, which is an artefact of the clock rather than anything
about the house. So while the current period is running, the previous one is
measured **to the same point in itself** — the same time of day, the same day
of the week, the same date in the month — and that is what `CHANGE` is computed
from. Its full total is kept on the line below, because where the previous
period ended up is the other thing worth knowing.

Stepping back to two finished periods drops both the clipping and that line:
there is nothing left to be fair about.

### Reading the chart

Bars are the current period; the cyan line is the previous one at the same hour,
day or month. Where the line crosses a bar, the gap between them is the change
in that bucket — a bar poking above its line used more than its counterpart did,
and one stopping short of it used less. Buckets that have not happened yet are
blank rather than zero, so the day does not appear to fall off a cliff at the
current hour.

`biggest ... apart` names the single bucket that moved most. Only buckets both
periods recorded count, and only ones that have finished — the hour in progress
is short by however much of it is still to come, and would otherwise win most of
the time.

Nothing here costs an API call. It reads the same merged pool the chart does, so
the two views cannot disagree about what a day used, and stepping back through
history is free however far you go.

## Table view (`tab`)

The chart's own buckets as exact figures, with columns for period, usage,
tariff register, and total cost — the same range and the same grain, from the
same merged pool, so the table and the bars above it cannot disagree about what
a day cost. It used to keep a private granularity and no window at all, which
is precisely how they came to.

Finer than 30 minutes is per-minute Home Mini telemetry rather than settled
consumption, and that lives in the LIVE view.

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
  db.py        SQLite: the telemetry archive, settled consumption, kv cache
  migrate.py   one-shot import of the old .cache/ JSON blobs
  cache.py     TTL'd cache over db.kv; every API response goes through it
  store.py     coverage tracking and fetch planning over the archive
  api.py       REST + GraphQL clients, account/meter discovery
  costing.py   calibration, rate timelines, day/night costing, forecasting
  model.py     the NOW tile's sparkline
  widgets.py   the panes
  app.py       layout, polling schedule, alerting
  app.tcss     green-phosphor styling
smoketest.py   exercises the API and costing engine without the TUI
dbtest.py      offline checks on the storage layer and the grains
uitest.py      drives the TUI headless with every API call stubbed and counted
```

## Where the data comes from

- **REST** (`api.octopus.energy/v1`) — account, unit rates, standing charges,
  half-hourly consumption. Billing-grade, lags a day.
- **GraphQL** (`api.octopus.energy/v1/graphql/`) — `smartMeterTelemetry`, the
  only route to Home Mini data. The Mini has **no local network API**; it reads
  your meter over Zigbee and pushes to Octopus's cloud, so live data is fetched
  back out rather than read off your LAN.

## The archive

Everything used to live in TTL'd JSON blobs under `.cache/` — 527 files and
33 MB by the end, largely the same rate records written out again and again
under overlapping window keys. For reference data that was merely wasteful. For
telemetry it was **destructive**, because Home Mini readings are only
retrievable from Octopus for a short window:

| Grouping | Retrievable for |
| --- | --- |
| `TEN_SECONDS` | 12 hours |
| `ONE_MINUTE` | 72 hours |
| `HALF_HOURLY` | 144 hours (6 days) |

Past those, the data does not exist anywhere else. The Mini has no local API and
no onboard history, so Octopus holds the only copy and it expires.

The live poll made this concrete. Every 60 seconds it fetched ~180 rows of
ten-second data, rendered them into the NOW tile, and dropped them — no cache
key, no store. The `TelemetryStore` was only reached from `load_series`, which
is skipped entirely in the default view. So the highest-resolution record of the
house was being destroyed continuously, by the one code path that always runs.

Readings now land in SQLite keyed on `(device_id, grouping, read_at)` and are
never deleted. **One row per slot** — the key is the instant the meter reported,
not the moment we happened to pull it, so re-reading the same half hour a
hundred times stores it once. There is no `pulled_at`; when a slot is fetched
again the row is overwritten in place.

That matters for the trailing edge in particular, where a window is often
refetched before the meter has finished reporting it — the later answer is the
better one and simply replaces the earlier.

Two consequences:

**Archiving happens in the API client, not at the call sites.** Every telemetry
response is written down before it is returned, so no caller can forget — which
is precisely the bug that existed. Later readings for the same instant overwrite
earlier ones, because the trailing edge of a live window is often refetched
before the meter has finished reporting it.

**Live history is bounded by what was recorded, not by what Octopus will
serve.** Measured after the migration:

```
TEN_SECONDS   api    12h   archive   47.2h    15064 rows
ONE_MINUTE    api    72h   archive   95.1h     5703 rows
HALF_HOURLY   api   144h   archive  167.4h      335 rows
```

Reading a 30-minute window from 30 hours ago — 18 hours past the ten-second
cutoff — returns 161 rows for **zero API calls**. That window is not retrievable
from Octopus by any means, and it happens to be a solar export period averaging
−972 W. Before this change it would simply not exist.

### A coarser view is a summary of a finer one

Octopus serves the same hours at three resolutions, and the app used to fetch
each independently — so scrolling the 30-minute view over hours already held
second-by-second went to the network for a digest of data sitting on disk.

It no longer does. `aggregate_power` already buckets whatever resolution it is
handed, so the finer rows are passed straight through and the coarse series is
never requested. Nothing is synthesised and **no derived row is written to the
archive**, where it could later be mistaken for something the meter said.

The substitution is only safe because the arithmetic is exact. Summing
ten-second `consumptionDelta` across 94 complete half hours reproduces Octopus's
own half-hourly figures to **0.000%**, with total drift of +0.0000% over 15.5
kWh. Energy is not approximated; it is the same number.

Demand is *better*, not merely equal. The API reports one `demand` value per
half hour and it is a spot reading, not an average — measured at 357 W for a
slot whose true mean was 521 W. Since export is detected by testing whether
average net flow is negative, a spot value can miss an export window outright.
Over one 12-hour stretch:

```
TEN_SECONDS   3879 rows  ->  2.074 kWh   exporting buckets: 14
ONE_MINUTE     720 rows  ->  2.071 kWh   exporting buckets: 14
HALF_HOURLY     24 rows  ->  2.000 kWh   exporting buckets: 12
```

The half-hourly series misses two export windows and reads 3.6% low, the latter
because a window that does not land on half-hour boundaries has to take or drop
edge buckets whole, while finer data slices exactly where asked.

A finer series is used only when it is genuinely complete: coverage must span
the window *and* hold at least half the readings its resolution implies. A range
recorded from an empty response is "covered" while holding nothing, and serving
that as data would report an hour of real usage as zero.

Result — 32 scroll steps across all four granularities, out to four days back:

```
rollup 0 (10 SEC · 30 MIN):  worst 156.1 ms   rollup 2 (5 MIN · 6 HR):  worst 129.1 ms
rollup 1 (1 MIN · 2 HR):     worst 130.5 ms   rollup 3 (30 MIN · 24 HR): worst 128.5 ms

API calls for all 32 steps: 0
```

### A ten-second feed does not mean 360 rows an hour

Worth knowing before reading a shortfall as data loss. The archive holds ~323
readings per hour, not the 360 a strict 10-second grid implies. Measured over
the whole ten-second history — 15,318 readings across 47.4 hours — the intervals
between consecutive rows are:

```
10s  x13572        20s  x1744        30s  x1
```

**Zero gaps longer than 30 seconds.** The series is continuous; the Mini just
skips a slot now and then, and 323/hour is its real reporting rate. The
arithmetic closes exactly: 13,572×10s + 1,744×20s ≈ 47.4 hours.

Coverage is recorded even for responses that returned nothing, so a quiet hour
is not re-requested on every render. But empty is not treated as final. The Mini
uploads over the internet and can fall behind, so "Octopus has nothing for this
hour" is a statement about *now*, not about the hour — and taking it as
permanent would let a single connectivity blip punch a hole in the archive that
never heals. Each covered range carries the time it was fetched; one that still
holds no readings is retried after an hour, until it passes out of the API's
reach and nothing can arrive for it any more.

### What is still a cache

`kv` holds what genuinely is one — account, products, bills, rates, calibration,
the telemetry budget. The distinction is whether losing it costs anything: a
cached API response can be fetched again, an expired reading cannot. Settled
consumption sits in between and gets its own table, fetched incrementally with a
two-day trailing overlap because settlement revises recently published half
hours. The full history used to be re-pulled every 30 minutes; the incremental
fetch asks for three days instead of eight months and serves the other 5,857
records from disk.

An **empty response is cached too**. Skipping it looks right — why record that
Octopus had nothing? — but it meant that the moment the Mini went quiet, every
cached call stopped being cached and fired on each poll instead: today's totals
going from 6/hour to 60/hour exactly when the budget most needed conserving. The
durable record of an empty range is the archive's `coverage` row, which has its
own hour-long retry; this cache is only a rate limiter, and it should limit
hardest when things are failing. The cost is up to one TTL before a recovery
shows in the derived figures, and the uncached live feed notices immediately
regardless.

### The trailing edge

The live view snaps its window end to the bucket grid, while the archive's
leading edge is written by the poll and so lags `now` by up to one interval. For
part of every minute the former sits past the latter, and a window held entirely
on disk failed its coverage test by a few seconds.

The consequence was out of all proportion to the cause. `best_source` returned
None, the caller fell through to fetching the *wanted* granularity — which had
no recent coverage at all — and `widen` padded the resulting gap to a full
72-hour `ONE_MINUTE` request. Because `load_series` only filters trailing gaps
under 90 seconds, that fired about every other poll:

```
a 95-second trailing gap on ONE_MINUTE widens to a 72-hour request
-> about 30 calls/hour while sitting on the 1 MIN · 2 HR view
```

Measured across one poll cycle, second by second, against the real archive:

```
before:  8 of 183 renders would fetch   {'1 MIN · 2 HR': 8}
after :  0 of 183 renders would fetch   {}
```

The fix is `TRAILING_TOLERANCE` — one poll interval of slack, applied only to
the end of the window. A hole anywhere else still counts however small, because
forgiving one would draw a real outage as zero usage. Refetching the sliver was
never useful anyway: the trailing edge is the poll's job, and the next poll
brings it in a call already being spent. Beyond the tolerance the fallback
returns, which is correct — an archive two minutes stale means a poll was missed.

### Migrating the old cache

`python -m octoscope.migrate` runs once automatically on first launch. It
rescued **20,870 readings** from the old blobs, including ten-second data
already two days old and therefore already irretrievable.

Recovering the *keys* took some care. The old scheme wrote
`{sanitised_key[:60]}-{sha256(key)[:16]}.json`, which is lossy twice: characters
outside `[A-Za-z0-9-_]` became underscores, and anything past 60 characters was
cut. Every `rates-*` key carries an ISO timestamp, so reversing the filename
gives a key no lookup will ever ask for — importing those would have put 30 MB
of unreachable blobs in the database.

The digest settles it without guessing. It was taken over the *original* key, so
a recovered key that hashes to the same digest **is** the original; anything else
is discarded and refetched. That kept 25 entries and dropped 443. The one that
mattered most is `telemetry-budget`: losing it would reset the hourly call count
to zero while the server's rolling window carried on, and the app would spend an
allowance it had already used.

The result is 33 MB and 527 files down to a single database. `.cache/` has since
been deleted, after verifying the import was complete: all 28,358 telemetry rows
in it were present in the database, with six values differing — and in every one
of those the database held the *larger* figure. Those were trailing-edge buckets
captured mid-interval and refetched once the meter had finished reporting them,
which is exactly what the `(device_id, grouping, read_at)` upsert exists to do.
The cache held only stale copies of readings the database has better.

`migrate.py` is kept even though it is now inert here. It is the upgrade path
for any other checkout, and it no-ops safely when there is no `.cache/`.

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
generation exceeds consumption — measured at up to **-1,927 W**, with the import
register not advancing at all across two hours of it. That two-hour window is
646 consecutive ten-second readings, every one of them negative, with
`consumption` frozen at 1485157 Wh throughout: 0.000 kWh imported is the correct
answer, not a gap in the data.

(The previous recorded peak was -1,256 W. The larger figure came out of the
archive — it is the kind of thing that was being thrown away every minute before
telemetry was kept.)

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

The overlay reads only what is already on disk — the telemetry archive plus this
session's provisional buckets — so opening it **never costs an API call**. It
used to be bounded by how far back Octopus serves half-hourly telemetry, about
six days; now it is bounded by the archive, so the comparison gains a day for
each day the app is run instead of sliding forward and forgetting. If the two
sources happen not to overlap, it says so rather than fetching to manufacture an
answer.

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

### Stepping away

Sixty calls an hour is a fine rate to pay while you are watching. It is a poor
one to pay while you are not: an afternoon out spends most of the 125/h budget
redrawing a screen nobody is reading, and then you come back, scroll, and find
there is nothing left to fetch with.

`p` pauses. It stops both polls and, just as importantly, stops the gap
backfills that scrolling queues — so a paused Octoscope is a **pure archive
browser**. Every granularity, every window the database holds, at no cost. The
status row keeps `⏸ paused` on screen from whichever view you are in, because a
state you have to go looking for is one you will forget you left on.

Two deliberate asymmetries:

- **`r` still works while paused**, and does not un-pause. Pausing is about the
  calls you are not there to authorise, not about locking the app.
- **Resuming polls immediately** rather than waiting out the interval.
  Consumption is on a 30-minute timer, so otherwise you would come back to a
  screen showing figures up to half an hour stale with nothing saying so.

Nothing about pause is persisted; it lasts the session.

The live caption counts down to the next poll — `update in 34s` — so the state
is legible either way: either you can see when the next call is coming, or you
can see that none is. The countdown tracks the interval timer rather than time
since the last reading landed, because the timer is what actually fetches: `r`
and resuming both refresh without moving it, and a countdown that reset on
those would be promising an update that is not coming.

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

Both are gitignored, along with `octoscope.db`.

Storage lives in `octoscope.db` (SQLite, WAL). It is created on first run and
the old `.cache/` is imported into it once, automatically. Three checks:

```
.venv/bin/python dbtest.py      # storage layer and grains, offline, no credentials
.venv/bin/python uitest.py      # the TUI, offline, every API call stubbed and counted
.venv/bin/python smoketest.py   # API and costing engine, no TUI
```

Only the last one touches the network.

**Back it up.** The telemetry in there cannot be re-fetched once it has aged out
of Octopus's retention window — unlike everything else in the file, it is not a
cache, and deleting it loses history permanently.

## Possible next steps

- Desktop notification / ntfy.sh push (currently in-TUI only, by choice)
- Per-half-hour heatmap to spot which times of day dominate the bill
- A compare frame for the same period a year back, rather than the one before
  it, once there is more than a year of history to line up
# octoscope
