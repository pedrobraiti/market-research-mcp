"""Perp funding rate & open interest across exchanges — keyless public derivatives data.

The execution side is spot-only, so this is purely positioning/sentiment CONTEXT (funding sign &
size, OI level) — never executed. Aggregates Binance USDⓈ-M futures, Bybit v5 and OKX v5 public
endpoints (all keyless for market data). A venue that fails is dropped from ``venues`` but named in
``venue_status``/``unavailable_venues``; the call still returns.

Funding intervals are NOT uniform: each venue sets 1h/4h/8h per symbol (Binance listed hundreds of
4h symbols in 2026). A raw rate is only meaningful against its own interval, so every venue's
interval is read from the venue itself — Binance ``fundingInfo.fundingIntervalHours`` (symbols it
omits keep the documented 8h default), Bybit ``fundingIntervalHour``, OKX ``nextFundingTime −
fundingTime`` — and rates are normalized to an 8h-equivalent before they are compared or
aggregated. A venue whose interval cannot be established keeps its raw rate but gets no normalized
figure, rather than a silently wrong one.

The JSON fetch is injected so the unit tests run fully offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ...domain.models import CryptoDerivatives, DerivativesVenue
from ..retry import ERROR, SCHEMA_ERROR, SourceUnavailable, unavailable_status

_BINANCE = "https://fapi.binance.com/fapi/v1"
_BYBIT = "https://api.bybit.com/v5"
_OKX = "https://www.okx.com/api/v5"

_HOURS_PER_DAY = Decimal(24)
_DAYS_PER_YEAR = Decimal(365)
# The comparison basis for cross-venue figures: the venues' historical default interval.
REFERENCE_INTERVAL_HOURS = Decimal(8)
# Binance documents fundingInfo as listing only symbols whose funding parameters were adjusted;
# a symbol absent from a successfully read list settles on this default.
BINANCE_DEFAULT_INTERVAL_HOURS = Decimal(8)
# Sanity bound on a derived interval: anything outside (0, 24h] is a garbled schedule, not a rate.
_MAX_INTERVAL_HOURS = Decimal(24)
_MS_PER_HOUR = Decimal(3_600_000)

INTERVAL_FROM_VENUE = "venue"
INTERVAL_VENUE_DEFAULT = "venue_default"
INTERVAL_FROM_SCHEDULE = "schedule"
_INTERVAL_UNKNOWN = "unknown"

_STATUS_OK = "ok"


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _quantize(value: Decimal | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def _valid_interval(hours: Decimal | None) -> Decimal | None:
    if hours is None or hours <= 0 or hours > _MAX_INTERVAL_HOURS:
        return None
    return hours


def _per_8h(rate: Decimal | None, interval_hours: Decimal | None) -> Decimal | None:
    if rate is None or interval_hours is None:
        return None
    return rate * REFERENCE_INTERVAL_HOURS / interval_hours


def _annualize(rate: Decimal | None, interval_hours: Decimal | None) -> Decimal | None:
    if rate is None or interval_hours is None:
        return None
    return _quantize(rate * (_HOURS_PER_DAY / interval_hours) * _DAYS_PER_YEAR, 6)


def _ms_to_dt(ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _venue(
    exchange: str,
    symbol: str,
    funding: Decimal | None,
    interval_hours: Decimal | None,
    interval_source: str,
    **fields: Any,
) -> DerivativesVenue:
    rate_8h = _per_8h(funding, interval_hours)
    return DerivativesVenue(
        exchange=exchange,
        symbol=symbol,
        funding_rate=funding,
        funding_rate_annualized=_annualize(funding, interval_hours),
        funding_interval_hours=interval_hours,
        funding_interval_source=interval_source,
        funding_rate_8h=_quantize(rate_8h, 10),
        **fields,
    )


def _failure_status(exc: BaseException) -> str:
    if not isinstance(exc, Exception):
        return f"unavailable: {ERROR}"
    if isinstance(exc, SourceUnavailable):
        return str(exc)
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return f"{unavailable_status(exc)} — {type(exc).__name__}: {first_line}"


def _aggregate_funding(
    venues: list[DerivativesVenue],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Cross-venue positioning on the 8h-EQUIVALENT rate: OI-weighted funding, funding dispersion,
    total OI in USD. A venue with an unknown interval has no comparable rate and is left out of
    the two funding figures (its OI still counts toward the total)."""
    weighted_pairs = [
        (v.funding_rate_8h, v.open_interest_value)
        for v in venues
        if v.funding_rate_8h is not None and v.open_interest_value and v.open_interest_value > 0
    ]
    oi_weighted = None
    if weighted_pairs:
        denom = sum(oi for _, oi in weighted_pairs)
        if denom > 0:
            oi_weighted = sum(rate * oi for rate, oi in weighted_pairs) / denom
    fundings = [v.funding_rate_8h for v in venues if v.funding_rate_8h is not None]
    dispersion = (max(fundings) - min(fundings)) if len(fundings) >= 2 else None
    oi_values = [v.open_interest_value for v in venues if v.open_interest_value is not None]
    total_oi = sum(oi_values) if oi_values else None
    return oi_weighted, dispersion, total_oi


def _binance_payload(payload: Any, field: str) -> dict:
    """A Binance object that carries ``field`` — an error envelope (``{"code", "msg"}``) or a
    body missing it is a failure, never a venue row of nulls."""
    if not isinstance(payload, dict):
        raise SourceUnavailable(SCHEMA_ERROR, f"binance: expected an object, got {payload!r:.80}")
    if field not in payload:
        if "code" in payload:
            raise SourceUnavailable(
                ERROR, f"binance error {payload.get('code')}: {payload.get('msg')}"
            )
        raise SourceUnavailable(SCHEMA_ERROR, f"binance: response has no '{field}'")
    return payload


def _binance_interval(funding_info: Any, symbol: str) -> tuple[Decimal | None, str]:
    if isinstance(funding_info, BaseException):
        return None, f"{_INTERVAL_UNKNOWN} — fundingInfo {_failure_status(funding_info)}"
    if not isinstance(funding_info, list):
        return None, f"{_INTERVAL_UNKNOWN} — fundingInfo unavailable: {SCHEMA_ERROR}"
    for row in funding_info:
        if isinstance(row, dict) and row.get("symbol") == symbol:
            hours = _valid_interval(_dec(row.get("fundingIntervalHours")))
            if hours is None:
                return None, f"{_INTERVAL_UNKNOWN} — fundingInfo row has no valid interval"
            return hours, INTERVAL_FROM_VENUE
    return BINANCE_DEFAULT_INTERVAL_HOURS, INTERVAL_VENUE_DEFAULT


def _okx_row(payload: Any, what: str) -> dict:
    """OKX answers HTTP 200 even for errors — ``code != "0"`` or an empty ``data`` is a failure."""
    if not isinstance(payload, dict):
        raise SourceUnavailable(SCHEMA_ERROR, f"okx {what}: expected an object")
    if str(payload.get("code")) != "0":
        raise SourceUnavailable(
            ERROR, f"okx {what} error {payload.get('code')}: {payload.get('msg')}"
        )
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise SourceUnavailable(SCHEMA_ERROR, f"okx {what}: empty data")
    return rows[0]


class DerivativesAggregator:
    def __init__(
        self,
        fetch_json: Callable[[str], Awaitable[Any]] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._timeout = timeout
        self._fetch_json = fetch_json or self._default_fetch_json

    async def _default_fetch_json(self, url: str) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def get_derivatives(self, base: str) -> CryptoDerivatives:
        token = base.strip().upper().split("/")[0]
        attempted = ("binance", "bybit", "okx")
        results = await asyncio.gather(
            self._binance(token),
            self._bybit(token),
            self._okx(token),
            return_exceptions=True,
        )
        venues: list[DerivativesVenue] = []
        venue_status: dict[str, str] = {}
        for exchange, result in zip(attempted, results, strict=True):
            if isinstance(result, DerivativesVenue):
                venues.append(result)
                venue_status[exchange] = _STATUS_OK
            else:
                venue_status[exchange] = _failure_status(result)
        unavailable = [name for name in attempted if venue_status[name] != _STATUS_OK]
        if not venues:
            return CryptoDerivatives(
                base=token,
                partial=True,
                venue_status=venue_status,
                unavailable_venues=unavailable,
                note="No derivatives venue returned data for this asset.",
            )
        oi_weighted, dispersion, total_oi = _aggregate_funding(venues)
        notes = [
            "funding_rate is each venue's raw rate per its own interval; funding_rate_8h and the "
            "cross-venue aggregates are normalized to an 8h-equivalent, and annualized figures use "
            "each venue's real interval (24/interval × 365)."
        ]
        unknown_interval = [v.exchange for v in venues if v.funding_interval_hours is None]
        if unknown_interval:
            notes.append(
                f"Funding interval unknown for {', '.join(unknown_interval)} — excluded from the "
                "normalized funding aggregates (raw rate kept)."
            )
        if unavailable:
            notes.append(f"Unavailable venues: {', '.join(unavailable)} (see venue_status).")
        return CryptoDerivatives(
            base=token,
            venues=venues,
            funding_oi_weighted=_quantize(oi_weighted, 8),
            funding_annualized_oi_weighted=_annualize(oi_weighted, REFERENCE_INTERVAL_HOURS),
            funding_dispersion=_quantize(dispersion, 8),
            total_open_interest_value=_quantize(total_oi, 2),
            # Fewer venues back than attempted → the aggregates are thinner; a 1-of-3 consensus
            # must not read as the whole market.
            partial=bool(unavailable),
            venue_status=venue_status,
            unavailable_venues=unavailable,
            note=" ".join(notes),
        )

    async def _binance(self, base: str) -> DerivativesVenue:
        symbol = f"{base}USDT"
        premium_raw, oi_raw, funding_info = await asyncio.gather(
            self._fetch_json(f"{_BINANCE}/premiumIndex?symbol={symbol}"),
            self._fetch_json(f"{_BINANCE}/openInterest?symbol={symbol}"),
            self._fetch_json(f"{_BINANCE}/fundingInfo"),
            return_exceptions=True,
        )
        for leg in (premium_raw, oi_raw):
            if isinstance(leg, BaseException):
                raise leg
        premium = _binance_payload(premium_raw, "lastFundingRate")
        oi = _binance_payload(oi_raw, "openInterest")
        # A missing fundingInfo only costs the normalization, not the venue's raw data.
        interval_hours, interval_source = _binance_interval(funding_info, symbol)
        mark = _dec(premium.get("markPrice"))
        oi_base = _dec(oi.get("openInterest"))
        oi_value = oi_base * mark if oi_base is not None and mark is not None else None
        return _venue(
            "binance",
            symbol,
            _dec(premium.get("lastFundingRate")),
            interval_hours,
            interval_source,
            next_funding_time=_ms_to_dt(premium.get("nextFundingTime")),
            mark_price=mark,
            open_interest=oi_base,
            open_interest_value=oi_value,
        )

    async def _bybit(self, base: str) -> DerivativesVenue:
        symbol = f"{base}USDT"
        data = await self._fetch_json(
            f"{_BYBIT}/market/tickers?category=linear&symbol={symbol}"
        )
        if not isinstance(data, dict):
            raise SourceUnavailable(SCHEMA_ERROR, "bybit: expected an object")
        # Bybit answers HTTP 200 with retCode ≠ 0 for errors (e.g. 10001 "symbol invalid").
        if data.get("retCode") not in (0, "0", None):
            raise SourceUnavailable(
                ERROR, f"bybit retCode {data.get('retCode')}: {data.get('retMsg')}"
            )
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            raise SourceUnavailable(SCHEMA_ERROR, "bybit: no ticker")
        row = rows[0]
        interval_hours = _valid_interval(_dec(row.get("fundingIntervalHour")))
        interval_source = (
            INTERVAL_FROM_VENUE
            if interval_hours is not None
            else f"{_INTERVAL_UNKNOWN} — ticker has no fundingIntervalHour"
        )
        return _venue(
            "bybit",
            symbol,
            _dec(row.get("fundingRate")),
            interval_hours,
            interval_source,
            next_funding_time=_ms_to_dt(row.get("nextFundingTime")),
            mark_price=_dec(row.get("markPrice")),
            open_interest=_dec(row.get("openInterest")),
            open_interest_value=_dec(row.get("openInterestValue")),
        )

    async def _okx(self, base: str) -> DerivativesVenue:
        inst = f"{base}-USDT-SWAP"
        funding, oi = await asyncio.gather(
            self._fetch_json(f"{_OKX}/public/funding-rate?instId={inst}"),
            self._fetch_json(f"{_OKX}/public/open-interest?instType=SWAP&instId={inst}"),
        )
        frow = _okx_row(funding, "funding-rate")
        orow = _okx_row(oi, "open-interest")
        # OKX's `fundingRate` settles at `fundingTime` (the NEXT settlement); `nextFundingTime` is
        # the one after it, so their gap is this instrument's interval.
        settle_ms, following_ms = _dec(frow.get("fundingTime")), _dec(frow.get("nextFundingTime"))
        interval_hours = None
        if settle_ms is not None and following_ms is not None:
            interval_hours = _valid_interval((following_ms - settle_ms) / _MS_PER_HOUR)
        interval_source = (
            INTERVAL_FROM_SCHEDULE
            if interval_hours is not None
            else f"{_INTERVAL_UNKNOWN} — no usable fundingTime/nextFundingTime"
        )
        return _venue(
            "okx",
            inst,
            _dec(frow.get("fundingRate")),
            interval_hours,
            interval_source,
            next_funding_time=_ms_to_dt(frow.get("fundingTime")),
            mark_price=None,
            open_interest=_dec(orow.get("oiCcy")),
            open_interest_value=_dec(orow.get("oiUsd")),
        )
