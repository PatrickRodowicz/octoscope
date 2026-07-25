"""Tiny TTL'd cache, backed by the `kv` table.

Every API response still goes through here, and the point is unchanged: be a
well-behaved API citizen, repopulate from disk on restart rather than
re-fetching, and only refetch slow-moving data when genuinely stale.

What changed is where it lands. This used to be one JSON file per key under
`.cache/`, which reached 527 files and 33 MB - largely the same rate records
written out again and again under overlapping window keys. It is now rows in
`octoscope.db`. The four functions below keep their old signatures, so no
caller needed to change.

Telemetry no longer belongs here at all. A cache entry is something you can
afford to lose because you can ask for it again; Home Mini readings expire at
source and cannot be. Those go to the `telemetry` table in `db.py` and are
never deleted - this layer only decides whether an API call is worth making.
"""
from __future__ import annotations

import time
from typing import Any

from . import db


def get(key: str, ttl: float) -> Any | None:
    """Return the cached value for `key`, or None if missing/stale."""
    entry = db.kv_get(key)
    if entry is None:
        return None
    value, stored_at = entry
    if time.time() - stored_at > ttl:
        return None
    return value


def get_stale(key: str) -> Any | None:
    """Return the cached value regardless of age.

    Used as a fallback so a network blip shows last-known data rather than
    blanking a pane.
    """
    return get(key, ttl=float("inf"))


def put(key: str, value: Any) -> None:
    db.kv_put(key, value)


def age(key: str) -> float | None:
    """Seconds since `key` was written, or None if absent."""
    entry = db.kv_get(key)
    return None if entry is None else time.time() - entry[1]
