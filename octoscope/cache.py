"""Tiny TTL'd disk cache.

Every API response goes through here. The point is to be a well-behaved
API citizen: on restart the dashboard repopulates from disk rather than
re-fetching, and slow-moving data (account, products, historical
consumption) is only refetched when genuinely stale.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .config import CACHE_DIR


def _path_for(key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)[:60]
    return CACHE_DIR / f"{safe}-{digest}.json"


def get(key: str, ttl: float) -> Any | None:
    """Return the cached value for `key`, or None if missing/stale."""
    path = _path_for(key)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            envelope = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - envelope.get("stored_at", 0) > ttl:
        return None
    return envelope.get("value")


def get_stale(key: str) -> Any | None:
    """Return the cached value regardless of age.

    Used as a fallback so a network blip shows last-known data rather than
    blanking a pane.
    """
    return get(key, ttl=float("inf"))


def put(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _path_for(key)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump({"stored_at": time.time(), "value": value}, f)
    tmp.replace(path)


def age(key: str) -> float | None:
    """Seconds since `key` was written, or None if absent."""
    path = _path_for(key)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return time.time() - json.load(f).get("stored_at", 0)
    except (json.JSONDecodeError, OSError):
        return None
