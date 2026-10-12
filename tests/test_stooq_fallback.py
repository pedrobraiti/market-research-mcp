from datetime import date
from decimal import Decimal

import pytest

from scout.adapters.price_fallback import PriceFallbackMarketData, PriceHistoryUnavailable
from scout.adapters.retry import SourceUnavailable
from scout.adapters.stooq import StooqPrices
from scout.domain.models import PriceBar, PriceHistory

_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2024-05-01,180.0,182.0,179.0,181.5,50000000\n"
    "2024-06-10,190.0,192.0,189.0,191.2,40000000\n"
    "2024-06-11,191.0,193.0,190.5,192.8,42000000\n"
)


def _stooq():
    async def _fetch(url: str) -> str:
        assert "aapl.us" in url
        return _CSV

    return StooqPrices(fetch_csv=_fetch)


async def test_stooq_parses_and_filters_range():
    history = await _stooq().get_price_history(
        "aapl", range="1mo", interval="1d", as_of=date(2024, 6, 11)
    )
    assert history is not None
    assert history.interval == "1d"
    # Range "1mo" ending 2024-06-11 excludes the 2024-05-01 bar.
    assert len(history.bars) == 2
    assert history.bars[-1].close == Decimal("192.8")
    assert history.bars[-1].volume == 42000000


async def test_stooq_returns_none_for_intraday():
    assert await _stooq().get_price_history("AAPL", interval="5m") is None


class _Primary:
    def __init__(self, result=None, raises=False, raises_unavailable=False):
        self.result = result
        self.raises = raises
        self.raises_unavailable = raises_unavailable
        self.fallback_marker = "primary"

    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        if self.raises_unavailable:
            raise SourceUnavailable("rate_limited")
        if self.raises:
            raise RuntimeError("yfinance down")
        return self.result


class _Fallback:
    def __init__(self, result="bars", raises=False):
        self.result = result
        self.raises = raises

    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        if self.raises:
            raise RuntimeError("stooq down")
        if self.result != "bars":
            return self.result
        bars = [PriceBar(date=date(2024, 1, 1), close=Decimal("9"))]
        return PriceHistory(symbol=symbol, interval=interval, bars=bars)


def _good_history():
    return PriceHistory(
        symbol="AAPL", interval="1d", bars=[PriceBar(date=date(2024, 1, 1), close=Decimal("100"))]
    )


async def test_fallback_uses_primary_when_it_has_data():
    wrapped = PriceFallbackMarketData(_Primary(result=_good_history()), _Fallback())
    history = await wrapped.get_price_history("AAPL")
    assert history.bars[0].close == Decimal("100")  # primary
    assert history.source == "yfinance"  # provenance stamped


async def test_fallback_kicks_in_when_primary_empty():
    wrapped = PriceFallbackMarketData(_Primary(result=None), _Fallback())
    history = await wrapped.get_price_history("AAPL")
    assert history.bars[0].close == Decimal("9")  # fallback
    assert history.source == "stooq"  # provenance stamped


async def test_fallback_kicks_in_when_primary_raises():
    wrapped = PriceFallbackMarketData(_Primary(raises=True), _Fallback())
    history = await wrapped.get_price_history("AAPL")
    assert history.bars[0].close == Decimal("9")  # fallback
    assert history.source == "stooq"


async def test_fallback_kicks_in_when_primary_rate_limited():
    # A rate-limited primary with a HEALTHY fallback → the caller gets bars, no exception.
    wrapped = PriceFallbackMarketData(_Primary(raises_unavailable=True), _Fallback())
    history = await wrapped.get_price_history("AAPL")
    assert history.bars[0].close == Decimal("9")
    assert history.source == "stooq"


async def test_rate_limited_primary_propagates_when_fallback_raises():
    # Primary 429 + fallback error: "couldn't fetch" must NOT collapse into a None that reads
    # as "this symbol has no data" — the primary's SourceUnavailable is re-raised.
    wrapped = PriceFallbackMarketData(
        _Primary(raises_unavailable=True), _Fallback(raises=True)
    )
    with pytest.raises(SourceUnavailable):
        await wrapped.get_price_history("AAPL")


async def test_rate_limited_primary_propagates_when_fallback_empty():
    # Primary 429 + fallback returns nothing → same: surface the unavailability, not a None.
    wrapped = PriceFallbackMarketData(
        _Primary(raises_unavailable=True), _Fallback(result=None)
    )
    with pytest.raises(SourceUnavailable):
        await wrapped.get_price_history("AAPL")


async def test_genuinely_empty_primary_with_empty_fallback_stays_none():
    # The primary answered (no data) and the fallback has none either: an honest None —
    # this is "symbol has no data", not an availability failure. Behavior preserved.
    wrapped = PriceFallbackMarketData(_Primary(result=None), _Fallback(result=None))
    assert await wrapped.get_price_history("AAPL") is None


async def test_fallback_delegates_other_methods_to_primary():
    wrapped = PriceFallbackMarketData(_Primary(), _Fallback())
    assert wrapped.fallback_marker == "primary"  # __getattr__ delegation


# --- Real stooq payloads (audit 2026-10-10, evidencia-1-stooq.json) --------------------------
# stooq answered HTTP 200 "text/html; charset=utf-8" with this JavaScript proof-of-work challenge
# instead of CSV, even with the adapter's own User-Agent. The first 700 chars are verbatim from
# the evidence; only the truncated tail is closed so the body is a complete HTML document.
_STOOQ_CHALLENGE_HTML = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="robots" '
    'content="noindex,nofollow"></head><body><noscript>This site requires JavaScript to verify '
    "your browser. Please enable JavaScript and reload.</noscript><script "
    'nonce="8h39ivcUmW5hdqy6T_wzgw">\n(async()=>{const c="AAAAAGrKhx1hkgByJHmN60yNEcmvd5n5vQQYOUL'
    'FWeo2bJOJVGwa3lMhRK0",d=4,t="0".repeat(d),e=new TextEncoder;let n=0;while(1){const h=await '
    'crypto.subtle.digest("SHA-256",e.encode(c+n)),x=Array.from(new Uint8Array(h)).map(b=>b.toSt'
    'ring(16).padStart(2,"0")).join("");if(x.startsWith(t))break;n++}const r=await fetch("/__veri'
    'fy",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"c="+en'
    'codeURIComponent(c)+"&n="+n});if(r.ok)location.reload()})();</script></body></html>'
)


def _stooq_over_http(status: int, body: str, content_type: str) -> StooqPrices:
    """StooqPrices driven through its REAL httpx fetch path via a mock transport."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "stooq.com"
        return httpx.Response(
            status, headers={"content-type": content_type}, content=body.encode("utf-8")
        )

    return StooqPrices(transport=httpx.MockTransport(handler))


def _stooq_text(body: str) -> StooqPrices:
    async def _fetch(url: str) -> str:
        return body

    return StooqPrices(fetch_csv=_fetch)


async def test_stooq_js_challenge_over_http_200_is_blocked_not_none():
    stooq = _stooq_over_http(200, _STOOQ_CHALLENGE_HTML, "text/html; charset=utf-8")
    with pytest.raises(SourceUnavailable) as exc_info:
        await stooq.get_price_history("SPY", range="1mo", interval="1d")
    assert exc_info.value.reason == "blocked"
    assert exc_info.value.status == "unavailable: blocked"
    assert "JavaScript bot challenge" in str(exc_info.value)


async def test_stooq_csv_over_http_still_parses():
    # The real path must keep working when stooq does serve CSV.
    stooq = _stooq_over_http(200, _CSV, "text/csv")
    history = await stooq.get_price_history("aapl", range="1mo", as_of=date(2024, 6, 11))
    assert history is not None and len(history.bars) == 2


async def test_stooq_no_data_body_is_genuine_absence():
    # stooq's own answer for an unknown symbol: an honest None ("no data"), not a failure.
    assert await _stooq_text("No data").get_price_history("ZZZZ") is None


async def test_stooq_changed_csv_header_is_schema_error():
    with pytest.raises(SourceUnavailable) as exc_info:
        await _stooq_text("Data;Otwarcie;Max\n2024-06-11;1;2\n").get_price_history("AAPL")
    assert exc_info.value.reason == "schema_error"


async def test_stooq_empty_body_is_schema_error():
    with pytest.raises(SourceUnavailable) as exc_info:
        await _stooq_text("   \n").get_price_history("AAPL")
    assert exc_info.value.reason == "schema_error"


async def test_stooq_daily_hits_limit_is_rate_limited():
    with pytest.raises(SourceUnavailable) as exc_info:
        await _stooq_text("Exceeded the daily hits limit").get_price_history("AAPL")
    assert exc_info.value.reason == "rate_limited"


class _SchemaChangedPrimary:
    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        raise ValueError("schema changed")


async def test_generic_primary_error_with_empty_fallback_raises_not_none():
    # The audit's offline repro: primary ValueError + empty fallback used to return None (and
    # so an ok:true envelope with null data). Now it raises with the primary's cause first.
    wrapped = PriceFallbackMarketData(_SchemaChangedPrimary(), _Fallback(result=None))
    with pytest.raises(PriceHistoryUnavailable) as exc_info:
        await wrapped.get_price_history("AAPL")
    error = exc_info.value
    assert isinstance(error, SourceUnavailable)  # same ADR-012 family every caller handles
    assert error.reason == "error"
    assert isinstance(error.primary_error, ValueError)
    assert error.fallback_error is None
    assert "yfinance failed (unavailable: error — ValueError: schema changed)" in str(error)
    assert "stooq fallback returned no bars" in str(error)


async def test_primary_error_with_blocked_stooq_preserves_both_causes():
    stooq = _stooq_over_http(200, _STOOQ_CHALLENGE_HTML, "text/html; charset=utf-8")
    wrapped = PriceFallbackMarketData(_SchemaChangedPrimary(), stooq)
    with pytest.raises(PriceHistoryUnavailable) as exc_info:
        await wrapped.get_price_history("SPY")
    error = exc_info.value
    assert error.reason == "error"  # the PRIMARY's reason leads
    assert error.fallback_error.reason == "blocked"
    message = str(error)
    assert message.index("schema changed") < message.index("unavailable: blocked")


async def test_rate_limited_primary_keeps_its_reason_through_the_wrapper():
    wrapped = PriceFallbackMarketData(_Primary(raises_unavailable=True), _Fallback(raises=True))
    with pytest.raises(SourceUnavailable) as exc_info:
        await wrapped.get_price_history("AAPL")
    assert exc_info.value.reason == "rate_limited"
    assert "stooq fallback failed" in str(exc_info.value)
