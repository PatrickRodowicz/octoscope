"""One-shot import of the old `.cache/` JSON blobs into SQLite.

Run automatically on first connect, and standalone with:

    .venv/bin/python -m octoscope.migrate

Most of `.cache/` is ordinary cached API responses that would simply be
refetched. Some of it is not: `series-*`, `live-*`, `today-*` and `minute-*`
hold Home Mini readings, and telemetry expires at source. Ten-second data is
gone from Octopus twelve hours after it is recorded, so rows sitting in those
blobs are, for the older ones, already the only copy in existence. They are
imported first and kept.

The `live-*` blobs are artifacts of an earlier implementation and their keys do
not name a device. Where the rest of the cache identifies exactly one device -
the normal case, since an account has one Home Mini - their rows are attributed
to it. Where it is ambiguous they are skipped, because guessing would file real
readings under the wrong meter and there would be no way to tell later.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import db
from .config import CACHE_DIR

DONE_KEY = "legacy-cache-imported"

# `_path_for` in the old cache appended a 16-hex digest of the key.
_SUFFIX = re.compile(r"-([0-9a-f]{16})\.json$")
_GROUPINGS = ("TEN_SECONDS", "ONE_MINUTE", "HALF_HOURLY")


def _key_of(path: Path) -> str:
    """Recover the cache key from a filename, as far as it was preserved."""
    return _SUFFIX.sub("", path.name)


def _key_is_exact(path: Path) -> bool:
    """Whether the filename's key survived the old encoding intact.

    The old scheme wrote `{sanitised_key[:60]}-{sha256(key)[:16]}.json`, which
    is lossy twice over: characters outside `[A-Za-z0-9-_]` became underscores,
    and anything past 60 characters was cut. Every `rates-*` key carries an ISO
    timestamp, so its colons were mangled and its tail truncated - reversing the
    filename gives a key that no lookup will ever ask for. Importing those would
    put 30 MB of unreachable blobs in the database.

    The digest settles it without guessing: it was taken over the *original*
    key, so a recovered key that hashes to the same digest is the original key.
    Anything else is refetched, which for cached API responses costs a request
    and nothing more.
    """
    match = _SUFFIX.search(path.name)
    if not match:
        return False
    recovered = _key_of(path).encode()
    return hashlib.sha256(recovered).hexdigest()[:16] == match.group(1)


def _rows(payload: object) -> list[dict]:
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def _telemetry_files(paths: list[Path]) -> dict[Path, tuple[str | None, str]]:
    """Map each telemetry-bearing file to its (device_id, grouping)."""
    found: dict[Path, tuple[str | None, str]] = {}
    for path in paths:
        key = _key_of(path)
        if key.startswith("series-"):
            # series-{device_id}-{GROUPING}; device ids contain dashes, the
            # groupings do not, so split off the last segment.
            body, _, grouping = key[len("series-"):].rpartition("-")
            if body and grouping in _GROUPINGS:
                found[path] = (body, grouping)
        elif key.startswith("today-") or key.startswith("minute-"):
            # today-{device_id}-{date}-{date} / minute-{device_id}
            body = key.split("-", 1)[1]
            parts = body.split("-")
            device = "-".join(parts[:8])
            if device:
                found[path] = (
                    device, "HALF_HOURLY" if key.startswith("today-") else "ONE_MINUTE"
                )
        elif key.startswith("live-"):
            for grouping in _GROUPINGS:
                if key.startswith(f"live-{grouping}"):
                    found[path] = (None, grouping)
                    break
    return found


def import_legacy_cache(verbose: bool = False) -> dict[str, int]:
    """Import old blobs. Idempotent - rows upsert, so re-running is harmless."""
    counts = {
        "telemetry": 0, "coverage": 0, "consumption": 0,
        "kv": 0, "unrecoverable": 0, "skipped": 0,
    }
    if not CACHE_DIR.is_dir():
        return counts

    paths = sorted(CACHE_DIR.glob("*.json"))
    telemetry = _telemetry_files(paths)
    known = {device for device, _ in telemetry.values() if device}
    # One device is the normal case; only then can orphaned live-* rows be
    # attributed with any confidence.
    fallback = next(iter(known)) if len(known) == 1 else None

    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            counts["skipped"] += 1
            continue
        if not isinstance(payload, dict) or "value" not in payload:
            counts["skipped"] += 1
            continue
        value = payload["value"]
        key = _key_of(path)

        if path in telemetry:
            device, grouping = telemetry[path]
            device = device or fallback
            if device is None:
                counts["skipped"] += 1
                continue
            if isinstance(value, dict):  # a series-* store: {rows, ranges}
                rows = [r for r in value.get("rows", {}).values() if isinstance(r, dict)]
                counts["telemetry"] += db.add_telemetry(device, grouping, rows)
                for span in value.get("ranges", []):
                    start, end = db.parse(span[0]), db.parse(span[1])
                    if start and end and end > start:
                        db.add_coverage(device, grouping, start, end)
                        counts["coverage"] += 1
            else:
                rows = _rows(value)
                counts["telemetry"] += db.add_telemetry(device, grouping, rows)
                # No coverage recorded: these blobs were keyed by window, not by
                # the range actually served, so claiming coverage from them
                # could suppress a fetch for hours never really held.
            continue

        if key.startswith("consumption-"):
            # The old key named the serial but not the mpan, so these rows
            # cannot be filed against a meter. Dropped rather than carried over
            # as dead kv blobs: settled consumption is permanent at Octopus, so
            # the first fetch repopulates the table for the cost of one REST
            # call. Unlike telemetry, nothing is lost by refetching it.
            counts["skipped"] += 1
            continue

        if not _key_is_exact(path):
            counts["unrecoverable"] += 1
            continue
        db.kv_put(key, value)
        counts["kv"] += 1

    db.kv_put(DONE_KEY, True)
    if verbose:
        for name, n in counts.items():
            print(f"{name:>12}  {n}")
        print(f"\n{'archive now':>12}  {db.stats()}")
    return counts


def run_once() -> dict[str, int] | None:
    """Import unless it has already been done."""
    if db.kv_get(DONE_KEY) is not None:
        return None
    return import_legacy_cache()


if __name__ == "__main__":
    import_legacy_cache(verbose=True)
