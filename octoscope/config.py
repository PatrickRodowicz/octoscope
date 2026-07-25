"""Configuration and runtime constants."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache"

REST_BASE = "https://api.octopus.energy/v1"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

# Query.smartMeterTelemetry is rate-limited to 125 requests/hour. That figure
# is not a guess: it comes from the API's own rateLimitInfo.fieldSpecificRateLimits,
# which reports `rate: "125/h"` for this field. Every telemetry call in the app
# is drawn from that single hourly budget, so the intervals below must sum to
# less than it with room to spare for manual refreshes.
#
#   live demand   at 60s  ->  60/hour
#   today totals  at 10m  ->   6/hour
#   5-min rollup  at 10m  ->   6/hour (only while its view is open)
#   calibration   at 12h  ->  ~0/hour
#                            ---------
#                             ~72/hour, leaving headroom under 125.
TELEMETRY_BUDGET_PER_HOUR = 110
POLL_TELEMETRY = 60
POLL_RATES = 30 * 60
POLL_CONSUMPTION = 30 * 60

TTL_TELEMETRY_TODAY = 10 * 60
TTL_TELEMETRY_MINUTE = 10 * 60
TTL_CALIBRATION = 12 * 3600
TTL_RATE_LIMIT = 120
TTL_BILLS = 6 * 3600

# Cache TTLs (seconds).
TTL_ACCOUNT = 24 * 3600
TTL_PRODUCTS = 24 * 3600
TTL_RATES = 30 * 60
TTL_CONSUMPTION = 30 * 60

# How far back to let Home Mini telemetry stand in for settled billing data.
# Half-hourly telemetry reaches ~6 days; 3 covers the worst observed lag with
# room to spare, without dragging a large payload back on every poll.
PROVISIONAL_DAYS = 3

# A "plunge" is a half-hour slot priced at or below zero. Agile publishes
# tomorrow's rates around 16:00 UK time for the 23:00->23:00 window.
PLUNGE_THRESHOLD_P = 0.0
CHEAP_THRESHOLD_P = 5.0
EXPENSIVE_THRESHOLD_P = 30.0


@dataclass(frozen=True)
class Config:
    api_key: str
    account: str

    @property
    def basic_auth_user(self) -> str:
        return self.api_key


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    # The .env in this project uses lowercase keys; accept either casing.
    key = os.getenv("octopus_api_key") or os.getenv("OCTOPUS_API_KEY")
    account = os.getenv("account") or os.getenv("ACCOUNT")
    if not key or not account:
        raise SystemExit(
            "Missing credentials. Expected octopus_api_key= and account= in "
            f"{PROJECT_ROOT / '.env'}"
        )
    return Config(api_key=key.strip(), account=account.strip())
