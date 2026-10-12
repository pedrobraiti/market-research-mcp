from datetime import UTC, datetime
from decimal import Decimal

from scout.adapters.derivatives import DerivativesAggregator

# Fixtures mirror the REAL public payloads (captured 2026-10-12): Binance premiumIndex /
# openInterest / fundingInfo, Bybit v5 tickers (retCode envelope + fundingIntervalHour), OKX v5
# funding-rate (fundingTime = next settlement, nextFundingTime = the one after) / open-interest.
_NEXT_SETTLEMENT_MS = 1791849600000  # 2026-10-13 00:00 UTC
_HOUR_MS = 3_600_000

# Binance lists only symbols with adjusted funding parameters; BTCUSDT appears at 8h, TRBUSDT at 4h.
_BINANCE_FUNDING_INFO = [
    {
        "symbol": "TRBUSDT",
        "adjustedFundingRateCap": "0.02000000",
        "adjustedFundingRateFloor": "-0.02000000",
        "fundingIntervalHours": 4,
        "disclaimer": False,
        "updateTime": 1697703073725,
    },
    {
        "symbol": "BTCUSDT",
        "adjustedFundingRateCap": "0.00300",
        "adjustedFundingRateFloor": "-0.00300",
        "fundingIntervalHours": 8,
        "disclaimer": True,
        "updateTime": None,
    },
]


def _binance_premium(rate: str, mark: str = "60000.0") -> dict:
    return {
        "symbol": "BTCUSDT",
        "markPrice": mark,
        "indexPrice": mark,
        "estimatedSettlePrice": mark,
        "lastFundingRate": rate,
        "interestRate": "0.00010000",
        "nextFundingTime": _NEXT_SETTLEMENT_MS,
        "time": 1791846773003,
    }


def _bybit_ticker(rate: str, interval_hour: str | None = "8") -> dict:
    row = {
        "symbol": "BTCUSDT",
        "markPrice": "60010.0",
        "openInterest": "8000",
        "openInterestValue": "480080000",
        "fundingRate": rate,
        "nextFundingTime": str(_NEXT_SETTLEMENT_MS),
        "fundingCap": "0.00333",
    }
    if interval_hour is not None:
        row["fundingIntervalHour"] = interval_hour
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "linear", "list": [row]},
        "retExtInfo": {},
        "time": 1791846774013,
    }


def _okx_funding(rate: str, interval_hours: int = 8) -> dict:
    return {
        "code": "0",
        "data": [
            {
                "fundingRate": rate,
                "fundingTime": str(_NEXT_SETTLEMENT_MS),
                "nextFundingTime": str(_NEXT_SETTLEMENT_MS + interval_hours * _HOUR_MS),
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "method": "current_period",
            }
        ],
        "msg": "",
    }


_OKX_OI = {
    "code": "0",
    "data": [
        {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "oiCcy": "5000", "oiUsd": "300000000"}
    ],
    "msg": "",
}


def _fetch_factory(
    *,
    binance_rate: str = "0.00004111",
    funding_info=_BINANCE_FUNDING_INFO,
    bybit=None,
    okx_funding=None,
    okx_oi=_OKX_OI,
):
    bybit = bybit if bybit is not None else _bybit_ticker("0.00005")
    okx_funding = okx_funding if okx_funding is not None else _okx_funding("0.00003")

    async def _fetch(url: str):
        if "fapi.binance.com" in url and "premiumIndex" in url:
            return _binance_premium(binance_rate)
        if "fapi.binance.com" in url and "openInterest" in url:
            return {"symbol": "BTCUSDT", "openInterest": "10000.0", "time": 1791846766604}
        if "fapi.binance.com" in url and "fundingInfo" in url:
            if isinstance(funding_info, Exception):
                raise funding_info
            return funding_info
        if "bybit.com" in url:
            return bybit
        if "okx.com" in url and "funding-rate" in url:
            return okx_funding
        if "okx.com" in url and "open-interest" in url:
            return okx_oi
        raise AssertionError(url)

    return _fetch


_fetch = _fetch_factory()


async def test_aggregates_three_venues():
    result = await DerivativesAggregator(fetch_json=_fetch).get_derivatives("BTC")
    assert result.base == "BTC"
    venues = {v.exchange: v for v in result.venues}
    assert set(venues) == {"binance", "bybit", "okx"}
    assert venues["binance"].funding_rate == Decimal("0.00004111")
    # binance OI value = 10000 * 60000
    assert venues["binance"].open_interest_value == Decimal("600000000.0")
    assert venues["bybit"].open_interest_value == Decimal("480080000")
    assert venues["okx"].open_interest_value == Decimal("300000000")
    assert result.venue_status == {"binance": "ok", "bybit": "ok", "okx": "ok"}
    assert result.unavailable_venues == []


async def test_derived_funding_annualized_and_cross_venue():
    result = await DerivativesAggregator(fetch_json=_fetch).get_derivatives("BTC")
    venues = {v.exchange: v for v in result.venues}
    # All three at 8h here: 0.00004111 × 3 × 365.
    assert venues["binance"].funding_rate_annualized == Decimal("0.045015")
    assert {v.funding_interval_hours for v in venues.values()} == {Decimal(8)}
    # OI-weighted consensus sits between the venues' funding rates.
    assert result.funding_oi_weighted is not None
    assert Decimal("0.00003") < result.funding_oi_weighted < Decimal("0.00005")
    assert result.funding_annualized_oi_weighted is not None
    # Dispersion = max − min funding = 0.00005 − 0.00003.
    assert result.funding_dispersion == Decimal("0.00002000")
    # Total OI in USD = 600,000,000 + 480,080,000 + 300,000,000.
    assert result.total_open_interest_value == Decimal("1380080000.00")
    assert result.note is not None and "8h" in result.note
    assert result.partial is False  # all three venues returned


async def test_interval_read_per_venue_and_normalized_before_aggregating():
    # Same economic funding (0.01%/8h) quoted on three different schedules: Binance 4h (listed
    # in fundingInfo), Bybit 1h (fundingIntervalHour), OKX 8h (fundingTime → nextFundingTime).
    # Raw rates differ 8×; normalized they must agree and the dispersion must vanish.
    info = [dict(_BINANCE_FUNDING_INFO[1], fundingIntervalHours=4)]
    fetch = _fetch_factory(
        binance_rate="0.00005",
        funding_info=info,
        bybit=_bybit_ticker("0.0000125", interval_hour="1"),
        okx_funding=_okx_funding("0.0001", interval_hours=8),
    )
    result = await DerivativesAggregator(fetch_json=fetch).get_derivatives("BTC")
    venues = {v.exchange: v for v in result.venues}
    assert venues["binance"].funding_interval_hours == Decimal(4)
    assert venues["binance"].funding_interval_source == "venue"
    assert venues["bybit"].funding_interval_hours == Decimal(1)
    assert venues["bybit"].funding_interval_source == "venue"
    assert venues["okx"].funding_interval_hours == Decimal(8)
    assert venues["okx"].funding_interval_source == "schedule"
    for venue in venues.values():
        assert venue.funding_rate_8h == Decimal("0.0001")
        # 0.0001 per 8h × 3 × 365 = 0.1095 — each venue annualized on its OWN interval.
        assert venue.funding_rate_annualized == Decimal("0.109500")
    assert result.funding_dispersion == Decimal("0")
    assert result.funding_oi_weighted == Decimal("0.00010000")
    assert result.funding_annualized_oi_weighted == Decimal("0.109500")


async def test_binance_symbol_absent_from_funding_info_uses_documented_default():
    fetch = _fetch_factory(funding_info=[_BINANCE_FUNDING_INFO[0]])  # only TRBUSDT listed
    result = await DerivativesAggregator(fetch_json=fetch).get_derivatives("BTC")
    binance = next(v for v in result.venues if v.exchange == "binance")
    assert binance.funding_interval_hours == Decimal(8)
    assert binance.funding_interval_source == "venue_default"


async def test_unknown_interval_keeps_raw_rate_but_leaves_aggregates():
    # fundingInfo down → Binance's interval is unknown: keep the venue and its raw rate, but no
    # normalized/annualized figure and no seat in the funding consensus (never a guessed 8h).
    fetch = _fetch_factory(binance_rate="0.009", funding_info=RuntimeError("fundingInfo down"))
    result = await DerivativesAggregator(fetch_json=fetch).get_derivatives("BTC")
    binance = next(v for v in result.venues if v.exchange == "binance")
    assert binance.funding_rate == Decimal("0.009")
    assert binance.funding_interval_hours is None
    assert binance.funding_rate_8h is None
    assert binance.funding_rate_annualized is None
    assert binance.funding_interval_source.startswith("unknown")
    assert result.venue_status["binance"] == "ok"
    # Consensus/dispersion come only from Bybit (0.00005) and OKX (0.00003).
    assert result.funding_dispersion == Decimal("0.00002000")
    assert Decimal("0.00003") < result.funding_oi_weighted < Decimal("0.00005")
    assert result.total_open_interest_value == Decimal("1380080000.00")  # OI still counts
    assert "binance" in result.note and "interval unknown" in result.note.lower()


async def test_okx_next_funding_time_is_the_next_settlement():
    result = await DerivativesAggregator(fetch_json=_fetch).get_derivatives("BTC")
    okx = next(v for v in result.venues if v.exchange == "okx")
    assert okx.next_funding_time == datetime(2026, 10, 13, tzinfo=UTC)


async def test_venue_error_payloads_are_unavailable_not_null_rows():
    # Real error envelopes: Bybit/OKX answer HTTP 200 with a non-zero code. They must NOT become
    # venues full of nulls; each is named with its reason and `partial` is set.
    fetch = _fetch_factory(
        bybit={
            "retCode": 10001,
            "retMsg": "params error: symbol invalid",
            "result": {},
            "retExtInfo": {},
            "time": 1791846795204,
        },
        okx_funding={
            "code": "51001",
            "data": [],
            "msg": "Instrument ID, Instrument ID code, or Spread ID doesn't exist.",
        },
    )
    result = await DerivativesAggregator(fetch_json=fetch).get_derivatives("BTC")
    assert {v.exchange for v in result.venues} == {"binance"}
    assert result.partial is True
    assert result.unavailable_venues == ["bybit", "okx"]
    assert result.venue_status["binance"] == "ok"
    assert result.venue_status["bybit"].startswith("unavailable: error")
    assert "10001" in result.venue_status["bybit"]
    assert result.venue_status["okx"].startswith("unavailable: error")
    assert "51001" in result.venue_status["okx"]
    assert "bybit" in result.note and "okx" in result.note


async def test_binance_error_object_is_unavailable():
    async def fetch(url: str):
        if "premiumIndex" in url:
            return {"code": -1121, "msg": "Invalid symbol."}
        return await _fetch(url)

    result = await DerivativesAggregator(fetch_json=fetch).get_derivatives("BTC")
    assert "binance" in result.unavailable_venues
    assert "-1121" in result.venue_status["binance"]


async def test_partial_failure_drops_only_that_venue():
    async def flaky(url: str):
        if "bybit.com" in url:
            raise RuntimeError("bybit down")
        return await _fetch(url)

    result = await DerivativesAggregator(fetch_json=flaky).get_derivatives("BTC")
    exchanges = {v.exchange for v in result.venues}
    assert exchanges == {"binance", "okx"}
    assert result.partial is True  # a venue was dropped → aggregates are thinner
    assert result.unavailable_venues == ["bybit"]
    assert result.venue_status["bybit"].startswith("unavailable: error")


async def test_all_fail_returns_note():
    async def dead(url: str):
        raise RuntimeError("network down")

    result = await DerivativesAggregator(fetch_json=dead).get_derivatives("BTC")
    assert result.venues == []
    assert result.note is not None
    assert result.unavailable_venues == ["binance", "bybit", "okx"]
