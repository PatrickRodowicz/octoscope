"""Octopus Energy API clients.

Two transports, because Octopus splits the data across them:

  REST     (api.octopus.energy/v1)  - account, tariffs, unit rates, standing
                                      charges, half-hourly consumption. This is
                                      the billing-grade record and goes back to
                                      move-in. Basic auth: API key as username,
                                      blank password.

  GraphQL  (api.octopus.energy/v1/graphql/) - smartMeterTelemetry, the only
                                      route to Home Mini data. The Mini has no
                                      local network API; it pushes to Octopus
                                      and we read it back. Auth is a Kraken JWT.

Note on money: REST unit rates carry both value_exc_vat and value_inc_vat, and
come in DIRECT_DEBIT / NON_DIRECT_DEBIT variants. GraphQL telemetry costDelta
is computed EX-VAT. All costing in this app is done from REST inc-VAT rates so
one convention holds throughout - see costing.py.
"""
from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import cache, db
from .config import (
    CONSUMPTION_OVERLAP_DAYS,
    GRAPHQL_URL,
    REST_BASE,
    TELEMETRY_BUDGET_PER_HOUR,
    TTL_ACCOUNT,
    TTL_BILLS,
    TTL_CONSUMPTION,
    TTL_RATE_LIMIT,
    TTL_RATES,
    Config,
)

GRAPHQL_COOLDOWN = dt.timedelta(minutes=10)

# Octopus signals field-level throttling with this error code.
RATE_LIMIT_CODE = "KT-CT-1199"


class TelemetryBudget:
    """Client-side cap on smartMeterTelemetry calls per rolling hour.

    The server allows 125/h on this field and blocks the whole field once
    exceeded, so it is much better to decline a call locally than to spend the
    breach. Callers that are refused simply reuse cached data.

    Call times are persisted. The server's window is a rolling hour that takes
    no notice of restarts, so an in-memory counter would reset to zero on every
    launch and cheerfully spend an allowance that was already gone.
    """

    CACHE_KEY = "telemetry-budget"

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        stored = cache.get_stale(self.CACHE_KEY) or []
        self._calls = [c for c in (_parse(s) for s in stored) if c]
        self._prune(dt.datetime.now(dt.timezone.utc))

    def _prune(self, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(hours=1)
        self._calls = [c for c in self._calls if c > cutoff]

    def allow(self) -> bool:
        now = dt.datetime.now(dt.timezone.utc)
        self._prune(now)
        return len(self._calls) < self.per_hour

    def record(self) -> None:
        self._calls.append(dt.datetime.now(dt.timezone.utc))
        cache.put(self.CACHE_KEY, [c.isoformat() for c in self._calls])

    @property
    def resets_at(self) -> dt.datetime | None:
        """When the oldest call ages out and a slot frees up."""
        self._prune(dt.datetime.now(dt.timezone.utc))
        if not self._calls:
            return None
        return min(self._calls) + dt.timedelta(hours=1)

    @property
    def used(self) -> int:
        self._prune(dt.datetime.now(dt.timezone.utc))
        return len(self._calls)

    @property
    def remaining(self) -> int:
        return max(0, self.per_hour - self.used)

# Verified against the live API: THIRTY_MINUTES and DAY are rejected with a 400.
_GROUPINGS = {"TEN_SECONDS", "ONE_MINUTE", "HALF_HOURLY"}


class OctopusError(RuntimeError):
    pass


@dataclass
class MeterPoint:
    mpan: str
    serial: str
    tariff_code: str
    moved_in: dt.datetime | None = None
    device_id: str | None = None
    registers: list[str] = field(default_factory=list)

    @property
    def region(self) -> str:
        """GSP region letter, taken from the trailing char of the tariff code."""
        return self.tariff_code[-1] if self.tariff_code else "A"

    @property
    def is_economy_7(self) -> bool:
        """Two-register tariffs (E-2R-*) bill day and night separately."""
        return "-2R-" in self.tariff_code

    @property
    def product_code(self) -> str:
        """Strip the E-1R-/E-2R- prefix and the region suffix off a tariff code."""
        parts = self.tariff_code.split("-")
        return "-".join(parts[2:-1]) if len(parts) > 3 else self.tariff_code


class OctopusClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        token = base64.b64encode(f"{config.api_key}:".encode()).decode()
        self._http = httpx.AsyncClient(timeout=60.0, headers={"User-Agent": "octoscope/2.0"})
        self._basic = f"Basic {token}"
        self._kraken_token: str | None = None
        self._kraken_expires: dt.datetime | None = None
        self._cooldown_until: dt.datetime | None = None
        self.budget = TelemetryBudget(TELEMETRY_BUDGET_PER_HOUR)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------- REST plumbing ----------------

    async def _rest(self, path: str, params: dict | None = None) -> Any:
        resp = await self._http.get(
            f"{REST_BASE}{path}", params=params, headers={"Authorization": self._basic}
        )
        if resp.status_code == 429:
            raise OctopusError("rate limited by Octopus (HTTP 429) - backing off")
        resp.raise_for_status()
        return resp.json()

    async def _rest_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow pagination and return every result row."""
        rows: list[dict] = []
        data = await self._rest(path, params)
        rows.extend(data.get("results", []))
        next_url = data.get("next")
        while next_url:
            resp = await self._http.get(next_url, headers={"Authorization": self._basic})
            resp.raise_for_status()
            data = resp.json()
            rows.extend(data.get("results", []))
            next_url = data.get("next")
        return rows

    async def _cached(self, key: str, ttl: float, fetch) -> Any:
        hit = cache.get(key, ttl)
        if hit is not None:
            return hit
        try:
            data = await fetch()
        except Exception:
            stale = cache.get_stale(key)
            if stale is not None:
                return stale
            raise
        cache.put(key, data)
        return data

    # ---------------- account / discovery ----------------

    async def account(self) -> dict:
        return await self._cached(
            f"account-{self.config.account}",
            TTL_ACCOUNT,
            lambda: self._rest(f"/accounts/{self.config.account}/"),
        )

    async def discover(self) -> MeterPoint:
        """Resolve the active import meter point, tariff, and Home Mini device."""
        data = await self.account()
        for prop in data.get("properties", []):
            if prop.get("moved_out_at"):
                continue
            for mp in prop.get("electricity_meter_points", []):
                if mp.get("is_export"):
                    continue
                agreement = _current_agreement(mp.get("agreements", []))
                meters = mp.get("meters", [])
                if not agreement or not meters:
                    continue
                # Prefer the most recently installed meter - the one the Home
                # Mini is paired against and the one currently settling.
                meter = meters[-1]
                point = MeterPoint(
                    mpan=str(mp.get("mpan")),
                    serial=str(meter.get("serial_number", "")),
                    tariff_code=agreement.get("tariff_code", ""),
                    moved_in=_parse(prop.get("moved_in_at")),
                    registers=[r.get("rate", "") for r in meter.get("registers", []) or []],
                )
                point.device_id = await self.find_device_id()
                return point
        raise OctopusError("No active electricity import meter point found on account")

    # ---------------- tariff rates ----------------

    # Rate windows are snapped out to whole UTC days. These bounds go into the
    # disk-cache key, and callers naturally pass "now + 2 days" as the horizon:
    # to-the-second bounds meant a brand new key on every call, so the cache
    # never once hit and every tariff comparison refetched the lot. Snapping
    # only ever widens the window, so no rate can be missed.
    async def unit_rate_records(
        self,
        product_code: str,
        tariff_code: str,
        register: str,
        period_from: dt.datetime,
        period_to: dt.datetime,
    ) -> list[dict]:
        """All unit-rate records in a window, oldest first.

        `register` is standard, day, or night. Two-register (Economy 7) tariffs
        have no standard-unit-rates endpoint; they expose day- and night-.
        Rows carry a payment_method - callers must filter, since the endpoint
        returns both Direct Debit and non-Direct-Debit variants for the same
        period and they differ by several percent.
        """
        params = {
            "period_from": _day_floor(period_from),
            "period_to": _day_ceil(period_to),
            "page_size": 1500,
        }
        path = f"/products/{product_code}/electricity-tariffs/{tariff_code}/{register}-unit-rates/"
        key = f"rates-{tariff_code}-{register}-{params['period_from']}-{params['period_to']}"
        rows = await self._cached(key, TTL_RATES, lambda: self._rest_all(path, params))
        return sorted(rows, key=lambda r: r.get("valid_from") or "")

    async def standing_charge_records(
        self,
        product_code: str,
        tariff_code: str,
        period_from: dt.datetime,
        period_to: dt.datetime,
    ) -> list[dict]:
        params = {
            "period_from": _day_floor(period_from),
            "period_to": _day_ceil(period_to),
            "page_size": 500,
        }
        path = f"/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"
        key = f"standing-{tariff_code}-{params['period_from']}-{params['period_to']}"
        rows = await self._cached(key, TTL_RATES, lambda: self._rest_all(path, params))
        return sorted(rows, key=lambda r: r.get("valid_from") or "")

    AGILE_PRODUCT = "AGILE-24-10-01"

    async def agile_rates(
        self,
        region: str,
        period_from: dt.datetime | None = None,
        period_to: dt.datetime | None = None,
    ) -> list[dict]:
        """Agile half-hourly rates, for the comparison pane and plunge watch.

        Defaults to the live window (yesterday through tomorrow); pass a wider
        range to price a historical counterfactual.
        """
        now = dt.datetime.now(dt.timezone.utc)
        return await self.unit_rate_records(
            self.AGILE_PRODUCT,
            f"E-1R-{self.AGILE_PRODUCT}-{region}",
            "standard",
            period_from or (now - dt.timedelta(hours=24)),
            period_to or (now + dt.timedelta(hours=48)),
        )

    # ---------------- consumption ----------------

    # Products worth comparing a normal household against. Deliberately
    # excludes prepayment, export, and tariffs that require hardware you would
    # have to own (Flux needs solar/battery, Intelligent needs a smart EV
    # charger) - pricing those would produce a number you could not act on.
    # The trailing four are fixed-term (12 month) products. They carry an exit
    # fee the variable ones do not, so a win here is not free - but leaving them
    # out made the pane look like Octopus sells nothing at a fixed price.
    COMPARABLE_PRODUCTS = (
        "VAR-22-11-01",
        "AGILE-24-10-01",
        "GO-VAR-22-10-14",
        "COSY-22-12-08",
        "SNUG-24-11-07",
        "OE-FIX-12M-26-07-25",
        "OE-FIX-12M-LOWSC-26-07-25",
        "COSY-FIX-12M-26-06-25",
        "GO-FIX-12M-26-06-30",
    )

    async def product(self, code: str) -> dict:
        return await self._cached(
            f"product-{code}", TTL_ACCOUNT, lambda: self._rest(f"/products/{code}/")
        )

    async def tariff_code_for(self, product_code: str, region: str) -> tuple[str, bool] | None:
        """Find a product's tariff code for a region.

        Returns (tariff_code, is_dual_register). Prefers the Direct Debit
        variant, matching how this account is billed.
        """
        try:
            data = await self.product(product_code)
        except Exception:  # noqa: BLE001
            return None
        for key, dual in (
            ("dual_register_electricity_tariffs", True),
            ("single_register_electricity_tariffs", False),
        ):
            region_map = (data.get(key) or {}).get(f"_{region}") or {}
            variant = region_map.get("direct_debit_monthly") or next(
                iter(region_map.values()), None
            )
            if variant and variant.get("code"):
                return variant["code"], dual
        return None

    async def consumption(
        self, point: MeterPoint, period_from: dt.datetime, period_to: dt.datetime | None = None
    ) -> list[dict]:
        """Half-hourly settled consumption, oldest first.

        Lags real time by roughly a day, so the most recent day is partial.

        Served from the archive, with only the tail refetched. This used to pull
        the entire history every 30 minutes and rewrite it as one blob - 5,800
        rows re-read and re-serialised to learn about the couple of dozen that
        were new, under a cache key carrying `period_to` to the hour, so the
        window shifted out from under itself and the blob was written afresh
        each time.
        """
        period_to = period_to or dt.datetime.now(dt.timezone.utc)
        stored = db.consumption_latest(point.mpan, point.serial)
        since_fetch = cache.age(f"consumption-fetched-{point.serial}")

        # The freshness marker says when the *tail* was last extended, which
        # says nothing about how far back the archive reaches. If this call
        # wants history older than anything previously asked for, the marker
        # must not suppress it. Tracked as the earliest period_from requested
        # rather than the oldest row held, because those differ permanently
        # whenever settlement starts later than move-in - as here, where the
        # meter was replaced and records begin four months after moving in.
        horizon_key = f"consumption-from-{point.serial}"
        asked_from = db.parse(cache.get_stale(horizon_key))
        backfill = asked_from is None or period_from < asked_from

        if backfill or since_fetch is None or since_fetch > TTL_CONSUMPTION:
            start = period_from
            if not backfill and stored is not None and stored > period_from:
                # Re-ask for a trailing overlap: settlement revises recently
                # published half hours, and a strictly incremental fetch would
                # keep the first, wrong answer forever.
                start = max(
                    period_from, stored - dt.timedelta(days=CONSUMPTION_OVERLAP_DAYS)
                )
            params = {
                "period_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "period_to": period_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "order_by": "period",
                "page_size": 25000,
            }
            path = (
                f"/electricity-meter-points/{point.mpan}/meters/{point.serial}/consumption/"
            )
            try:
                rows = await self._rest_all(path, params)
            except Exception:
                # Nothing archived yet means there is nothing to fall back on.
                if stored is None:
                    raise
            else:
                db.add_consumption(point.mpan, point.serial, rows)
                cache.put(f"consumption-fetched-{point.serial}", db.iso(period_to))
                if backfill:
                    cache.put(horizon_key, db.iso(min(period_from, asked_from or period_from)))

        return db.consumption_slice(point.mpan, point.serial, period_from, period_to)

    # ---------------- GraphQL ----------------

    async def _kraken_token_value(self) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        if self._kraken_token and self._kraken_expires and now < self._kraken_expires:
            return self._kraken_token
        resp = await self._http.post(
            GRAPHQL_URL,
            json={
                "query": "mutation($k:String!){obtainKrakenToken(input:{APIKey:$k}){token}}",
                "variables": {"k": self.config.api_key},
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise OctopusError(f"auth failed: {payload['errors'][0].get('message')}")
        self._kraken_token = payload["data"]["obtainKrakenToken"]["token"]
        # Kraken tokens live ~1h; refresh a little early.
        self._kraken_expires = now + dt.timedelta(minutes=50)
        return self._kraken_token

    @property
    def graphql_cooling_down(self) -> bool:
        return (
            self._cooldown_until is not None
            and dt.datetime.now(dt.timezone.utc) < self._cooldown_until
        )

    async def _graphql(self, query: str, variables: dict) -> dict:
        # Octopus rate-limits GraphQL harder than REST. Once throttled, stop
        # asking for a while rather than hammering it with retries.
        if self.graphql_cooling_down:
            raise OctopusError("cooling down after rate limit")
        token = await self._kraken_token_value()
        resp = await self._http.post(
            GRAPHQL_URL, json={"query": query, "variables": variables},
            headers={"Authorization": token},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            error = payload["errors"][0]
            message = error.get("message", "graphql error")
            code = ((error.get("extensions") or {}).get("errorCode") or "")
            throttled = (
                code == RATE_LIMIT_CODE
                or "too many requests" in message.lower()
                or "throttled" in message.lower()
            )
            if throttled:
                # Prefer the server's own unblock time over a fixed guess.
                until = await self._blocked_until("smartMeterTelemetry")
                self._cooldown_until = until or (
                    dt.datetime.now(dt.timezone.utc) + GRAPHQL_COOLDOWN
                )
                wait = self._cooldown_until - dt.datetime.now(dt.timezone.utc)
                raise OctopusError(
                    f"telemetry rate limited - resumes in {int(wait.total_seconds() / 60)}m"
                )
            raise OctopusError(message)
        self._cooldown_until = None
        return payload.get("data") or {}

    async def _blocked_until(self, field: str) -> dt.datetime | None:
        """Ask the API when this field unblocks. `ttl` is an absolute epoch."""
        try:
            info = await self.rate_limit_info()
        except Exception:  # noqa: BLE001 - diagnostics only
            return None
        for entry in info.get("fields", []):
            if entry.get("field", "").endswith(field) and entry.get("isBlocked"):
                ttl = entry.get("ttl")
                if ttl:
                    return dt.datetime.fromtimestamp(ttl, dt.timezone.utc)
        return None

    async def rate_limit_info(self) -> dict:
        """Points allowance plus per-field limits, as the API reports them."""
        hit = cache.get("rate-limit", TTL_RATE_LIMIT)
        if hit is not None:
            return hit
        query = """query{ rateLimitInfo{
            pointsAllowanceRateLimit{ limit remainingPoints usedPoints isBlocked }
            fieldSpecificRateLimits(first:100){ edges{ node{ field rate ttl isBlocked } } } } }"""
        token = await self._kraken_token_value()
        resp = await self._http.post(
            GRAPHQL_URL, json={"query": query, "variables": {}},
            headers={"Authorization": token},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise OctopusError(payload["errors"][0].get("message", "graphql error"))
        info = payload["data"]["rateLimitInfo"]
        result = {
            "points": info.get("pointsAllowanceRateLimit") or {},
            "fields": [e["node"] for e in
                       (info.get("fieldSpecificRateLimits") or {}).get("edges", [])],
        }
        cache.put("rate-limit", result)
        return result

    def telemetry_limit(self, info: dict) -> dict | None:
        for entry in info.get("fields", []):
            if entry.get("field") == "Query.smartMeterTelemetry":
                return entry
        return None

    async def bills(self, count: int = 12) -> list[dict]:
        """Issued statements: period covered plus gross/net/VAT totals in pence.

        Not rate-limited per-field, so this is cheap next to telemetry.
        """
        hit = cache.get(f"bills-{self.config.account}", TTL_BILLS)
        if hit is not None:
            return hit
        query = """query($a:String!,$n:Int!){ account(accountNumber:$a){
            bills(first:$n){ edges{ node{ id billType fromDate toDate issuedDate
              ... on StatementType { closingBalance openingBalance
                consumptionStartDate consumptionEndDate
                totalCharges{ netTotal taxTotal grossTotal } } } } } } }"""
        try:
            data = await self._graphql(query, {"a": self.config.account, "n": count})
        except (OctopusError, httpx.HTTPError):
            return cache.get_stale(f"bills-{self.config.account}") or []
        rows = [
            e["node"]
            for e in ((data.get("account") or {}).get("bills") or {}).get("edges", [])
        ]
        cache.put(f"bills-{self.config.account}", rows)
        return rows

    async def find_device_id(self) -> str | None:
        key = f"device-{self.config.account}"
        hit = cache.get(key, TTL_ACCOUNT)
        if hit is not None:
            return hit or None
        query = """query($a:String!){ account(accountNumber:$a){
            electricityAgreements(active:true){
              meterPoint{ meters{ smartDevices{ deviceId type } } } } } }"""
        try:
            data = await self._graphql(query, {"a": self.config.account})
        except (OctopusError, httpx.HTTPError):
            return cache.get_stale(key) or None
        for ag in (data.get("account") or {}).get("electricityAgreements") or []:
            for meter in ((ag.get("meterPoint") or {}).get("meters")) or []:
                for device in meter.get("smartDevices") or []:
                    if device.get("deviceId"):
                        cache.put(key, device["deviceId"])
                        return device["deviceId"]
        cache.put(key, "")
        return None

    async def telemetry(
        self,
        device_id: str,
        minutes: int = 30,
        grouping: str = "TEN_SECONDS",
        start_at: dt.datetime | None = None,
        end_at: dt.datetime | None = None,
        cache_ttl: float | None = None,
        cache_key: str | None = None,
    ) -> list[dict]:
        """Near-real-time readings from the Home Mini, oldest first.

        `demand` is instantaneous watts, `consumptionDelta` watt-hours in the
        interval, `costDelta` pence EX-VAT. Only DAY-grouping is unsupported by
        the API; anything longer than a few days should come from REST instead.
        """
        if grouping not in _GROUPINGS:
            raise ValueError(f"unsupported grouping: {grouping}")
        now = dt.datetime.now(dt.timezone.utc)
        end_dt = end_at or now
        start_dt = start_at or (end_dt - dt.timedelta(minutes=minutes))

        # Aggregated views change slowly; only the live 10-second feed needs to
        # be fetched every poll.
        if cache_ttl is not None and cache_key is not None:
            hit = cache.get(cache_key, cache_ttl)
            if hit is not None:
                return hit

        if not self.budget.allow():
            # Spend nothing rather than trip the server-side block, which would
            # take the whole field offline for everyone using this account.
            stale = cache.get_stale(cache_key) if cache_key else None
            if stale is not None:
                return stale
            raise OctopusError(
                f"telemetry budget spent ({self.budget.per_hour}/h) - waiting"
            )
        self.budget.record()
        # Inlined rather than passed as a variable: the enum's schema type name
        # is not documented, and the value is ours, not user input.
        query = f"""query($d:String!,$s:DateTime!,$e:DateTime!){{
          smartMeterTelemetry(deviceId:$d, grouping:{grouping}, start:$s, end:$e){{
            readAt consumptionDelta demand consumption costDelta }} }}"""
        data = await self._graphql(
            query,
            {
                "d": device_id,
                "s": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "e": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        rows = data.get("smartMeterTelemetry") or []

        # Archive before anything else touches the response. Home Mini readings
        # expire at source - ten-second data within 12 hours - and Octopus holds
        # the only copy, so a response that reaches the UI without being written
        # down is not cached data, it is destroyed data. Coverage is recorded
        # even when `rows` is empty: a range Octopus served nothing for is a
        # range known to be empty, and re-asking spends budget to learn it twice.
        db.add_telemetry(device_id, grouping, rows)
        db.add_coverage(device_id, grouping, start_dt, end_dt)

        if cache_ttl is not None and cache_key is not None and rows:
            cache.put(cache_key, rows)
        return rows


def _day_floor(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def _day_ceil(when: dt.datetime) -> str:
    return _day_floor(when.astimezone(dt.timezone.utc) + dt.timedelta(days=1))


def _current_agreement(agreements: list[dict]) -> dict | None:
    now = dt.datetime.now(dt.timezone.utc)
    best = None
    for ag in agreements:
        valid_from = _parse(ag.get("valid_from"))
        valid_to = _parse(ag.get("valid_to"))
        if valid_from and valid_from > now:
            continue
        if valid_to and valid_to < now:
            continue
        best = ag
    return best or (agreements[-1] if agreements else None)


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
