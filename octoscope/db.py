"""SQLite storage - the permanent record.

The app used to keep everything in TTL'd JSON blobs under `.cache/`. For
slow-moving reference data (rates, products, bills) that was fine. For
telemetry it was quietly destructive, because Home Mini readings are only
retrievable from Octopus for a short window:

    TEN_SECONDS    12 hours
    ONE_MINUTE     72 hours
    HALF_HOURLY   144 hours (6 days)

Past those, the data does not exist anywhere else. The Mini has no local API
and no onboard history; Octopus is the only copy, and it expires. Anything not
written down before it ages out is gone permanently.

The live poll made that concrete: every 60 seconds it pulled ~180 rows of
ten-second data, rendered them, and dropped them on the floor. The highest
resolution record of the house was being destroyed continuously by the one code
path that runs all the time.

So telemetry now lands in a table keyed on (device_id, grouping, read_at) and is
never deleted. Coverage - which ranges have actually been asked for - is tracked
alongside it, so a window can be served from the archive without re-asking, and
so history older than the API's reach stays readable long after Octopus has
forgotten it.

`kv` carries everything that genuinely is just a cache: the same TTL'd blobs as
before, minus 500-odd files. Row counts here are small and access is bursty, so
one connection with a lock beats a pool.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from typing import Any

from .config import DB_PATH

SCHEMA_VERSION = 2

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

# The telemetry fields Octopus returns, mapped to their column. `consumption` is
# the cumulative meter register; the deltas are per-interval. The API sends them
# all as strings ("549.0"); they are stored as REAL and every consumer already
# calls float() on them.
_TELEMETRY_FIELDS = (
    ("readAt", "read_at"),
    ("consumptionDelta", "consumption_delta"),
    ("demand", "demand"),
    ("consumption", "consumption"),
    ("costDelta", "cost_delta"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    device_id         TEXT NOT NULL,
    grouping          TEXT NOT NULL,
    read_at           TEXT NOT NULL,
    consumption_delta REAL,
    demand            REAL,
    consumption       REAL,
    cost_delta        REAL,
    PRIMARY KEY (device_id, grouping, read_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS coverage (
    device_id  TEXT NOT NULL,
    grouping   TEXT NOT NULL,
    start_at   TEXT NOT NULL,
    end_at     TEXT NOT NULL,
    fetched_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS coverage_lookup ON coverage (device_id, grouping, start_at);

CREATE TABLE IF NOT EXISTS consumption (
    mpan           TEXT NOT NULL,
    serial         TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    interval_end   TEXT,
    consumption    REAL,
    PRIMARY KEY (mpan, serial, interval_start)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS kv (
    key       TEXT PRIMARY KEY,
    stored_at REAL NOT NULL,
    value     TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """Open (once) and return the shared connection."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL so a costing worker reading history cannot block the live poll
        # writing to it. NORMAL sync is the right trade for data we can refetch
        # within its retention window but would rather not lose to a hard kill.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
        _conn = conn
        return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to `SCHEMA_VERSION`.

    `CREATE TABLE IF NOT EXISTS` covers a fresh file but does nothing for one
    already on disk, so added columns need saying twice.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(coverage)")}
        if "fetched_at" not in columns:
            # Defaults to 0, i.e. "fetched long ago" - so ranges recorded before
            # this column existed are eligible for a backfill retry, which is
            # the safe direction to be wrong in.
            conn.execute(
                "ALTER TABLE coverage ADD COLUMN fetched_at REAL NOT NULL DEFAULT 0"
            )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# ---------------- time ----------------
#
# Stored timestamps are always UTC ISO with a "+00:00" offset, so a lexicographic
# comparison in SQL is a chronological one and range scans can use the index.


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _canonical(value: str | None) -> str | None:
    """Normalise an API timestamp to the stored form."""
    parsed = parse(value)
    return iso(parsed) if parsed else None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------- kv cache ----------------


def kv_get(key: str) -> tuple[Any, float] | None:
    """Return `(value, stored_at)` for `key`, or None if absent."""
    with _lock:
        row = connect().execute(
            "SELECT value, stored_at FROM kv WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"]), row["stored_at"]
    except json.JSONDecodeError:
        return None


def kv_put(key: str, value: Any) -> None:
    payload = json.dumps(value)
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO kv (key, stored_at, value) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET stored_at = excluded.stored_at, "
            "value = excluded.value",
            (key, time.time(), payload),
        )
        conn.commit()


# ---------------- telemetry ----------------


def add_telemetry(device_id: str, grouping: str, rows: list[dict]) -> int:
    """Archive telemetry rows. Idempotent; returns the number accepted.

    Later readings for the same instant overwrite earlier ones - the trailing
    edge of a live window can be refetched before the meter has finished
    reporting it, and the newer answer is the better one.
    """
    if not device_id or not rows:
        return 0
    payload = []
    for row in rows:
        read_at = _canonical(row.get("readAt"))
        if read_at is None:
            continue
        payload.append((
            device_id, grouping, read_at,
            *(_number(row.get(api_field)) for api_field, _ in _TELEMETRY_FIELDS[1:]),
        ))
    if not payload:
        return 0
    with _lock:
        conn = connect()
        conn.executemany(
            "INSERT INTO telemetry (device_id, grouping, read_at, consumption_delta,"
            " demand, consumption, cost_delta) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id, grouping, read_at) DO UPDATE SET "
            "consumption_delta = excluded.consumption_delta, demand = excluded.demand, "
            "consumption = excluded.consumption, cost_delta = excluded.cost_delta",
            payload,
        )
        conn.commit()
    return len(payload)


def telemetry_slice(
    device_id: str, grouping: str, start: dt.datetime, end: dt.datetime
) -> list[dict]:
    """Readings in [start, end), oldest first, in the API's own shape."""
    with _lock:
        rows = connect().execute(
            "SELECT read_at, consumption_delta, demand, consumption, cost_delta "
            "FROM telemetry WHERE device_id = ? AND grouping = ? "
            "AND read_at >= ? AND read_at < ? ORDER BY read_at",
            (device_id, grouping, iso(start), iso(end)),
        ).fetchall()
    return [
        {api_field: row[column] for api_field, column in _TELEMETRY_FIELDS}
        for row in rows
    ]


def telemetry_count(device_id: str, grouping: str) -> int:
    with _lock:
        row = connect().execute(
            "SELECT count(*) AS n FROM telemetry WHERE device_id = ? AND grouping = ?",
            (device_id, grouping),
        ).fetchone()
    return row["n"] if row else 0


def telemetry_count_between(
    device_id: str, grouping: str, start: dt.datetime, end: dt.datetime
) -> int:
    with _lock:
        row = connect().execute(
            "SELECT count(*) AS n FROM telemetry WHERE device_id = ? AND grouping = ? "
            "AND read_at >= ? AND read_at < ?",
            (device_id, grouping, iso(start), iso(end)),
        ).fetchone()
    return row["n"] if row else 0


def telemetry_extent(device_id: str, grouping: str) -> tuple[dt.datetime, dt.datetime] | None:
    """Oldest and newest reading held for this granularity."""
    with _lock:
        row = connect().execute(
            "SELECT min(read_at) AS lo, max(read_at) AS hi FROM telemetry "
            "WHERE device_id = ? AND grouping = ?",
            (device_id, grouping),
        ).fetchone()
    if not row or not row["lo"]:
        return None
    low, high = parse(row["lo"]), parse(row["hi"])
    return (low, high) if low and high else None


# ---------------- coverage ----------------


def coverage_ranges(
    device_id: str, grouping: str
) -> list[tuple[dt.datetime, dt.datetime, float]]:
    """Covered ranges as (start, end, fetched_at), oldest first."""
    with _lock:
        rows = connect().execute(
            "SELECT start_at, end_at, fetched_at FROM coverage "
            "WHERE device_id = ? AND grouping = ? ORDER BY start_at",
            (device_id, grouping),
        ).fetchall()
    spans = [(parse(r["start_at"]), parse(r["end_at"]), r["fetched_at"]) for r in rows]
    return sorted([s for s in spans if s[0] and s[1]], key=lambda s: s[0])


def add_coverage(
    device_id: str, grouping: str, start: dt.datetime, end: dt.datetime
) -> None:
    """Record that [start, end) has been asked for, merging adjoining ranges.

    Recorded even when the response was empty, so that a range Octopus served
    nothing for is not asked about again on every render. `fetched_at` is kept
    so an empty range can still be retried later - the Home Mini can upload late
    after a connectivity gap, and a range that was genuinely empty when asked
    may not be empty now.

    Merged ranges take the *newest* fetch time. That direction is not a
    preference, it is what stops a loop: a range just fetched must not come
    straight back as eligible for retry. Taking the oldest would mean any range
    merging with one carrying an ancient timestamp - every range imported from
    the old cache carries 0 - would inherit it, be retried immediately, merge
    again, and re-fetch forever, emptying the hourly budget. The cost is that an
    old empty stretch adjacent to a fresh fetch inherits the fresh time and is
    not retried; this is a backstop, not a guarantee.
    """
    if end <= start:
        return
    now = time.time()
    with _lock:
        conn = connect()
        merged: list[list] = []
        spans = coverage_ranges(device_id, grouping) + [(start, end, now)]
        for begin, finish, fetched in sorted(spans, key=lambda s: s[0]):
            if merged and begin <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], finish)
                merged[-1][2] = max(merged[-1][2], fetched)
            else:
                merged.append([begin, finish, fetched])
        conn.execute(
            "DELETE FROM coverage WHERE device_id = ? AND grouping = ?",
            (device_id, grouping),
        )
        conn.executemany(
            "INSERT INTO coverage (device_id, grouping, start_at, end_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(device_id, grouping, iso(a), iso(b), f) for a, b, f in merged],
        )
        conn.commit()


# ---------------- settled consumption ----------------


def add_consumption(mpan: str, serial: str, rows: list[dict]) -> int:
    """Archive settled half-hourly records, newest answer winning."""
    if not rows:
        return 0
    payload = []
    for row in rows:
        start = _canonical(row.get("interval_start"))
        if start is None:
            continue
        payload.append((
            mpan, serial, start,
            _canonical(row.get("interval_end")),
            _number(row.get("consumption")),
        ))
    if not payload:
        return 0
    with _lock:
        conn = connect()
        conn.executemany(
            "INSERT INTO consumption (mpan, serial, interval_start, interval_end,"
            " consumption) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mpan, serial, interval_start) DO UPDATE SET "
            "interval_end = excluded.interval_end, consumption = excluded.consumption",
            payload,
        )
        conn.commit()
    return len(payload)


def consumption_slice(
    mpan: str, serial: str, start: dt.datetime, end: dt.datetime
) -> list[dict]:
    """Settled records in [start, end), oldest first, in the REST shape."""
    with _lock:
        rows = connect().execute(
            "SELECT interval_start, interval_end, consumption FROM consumption "
            "WHERE mpan = ? AND serial = ? AND interval_start >= ? AND interval_start < ? "
            "ORDER BY interval_start",
            (mpan, serial, iso(start), iso(end)),
        ).fetchall()
    return [
        {
            "interval_start": row["interval_start"],
            "interval_end": row["interval_end"],
            "consumption": row["consumption"],
        }
        for row in rows
    ]


def consumption_latest(mpan: str, serial: str) -> dt.datetime | None:
    with _lock:
        row = connect().execute(
            "SELECT max(interval_start) AS hi FROM consumption WHERE mpan = ? AND serial = ?",
            (mpan, serial),
        ).fetchone()
    return parse(row["hi"]) if row and row["hi"] else None



# ---------------- housekeeping ----------------


def stats() -> dict[str, int]:
    """Row counts per table, for the event log."""
    with _lock:
        conn = connect()
        return {
            table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in ("telemetry", "coverage", "consumption", "kv")
        }
