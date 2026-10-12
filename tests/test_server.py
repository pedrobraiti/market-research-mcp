from datetime import date
from decimal import Decimal

import pytest

from scout.config import Settings, get_settings
from scout.domain.models import (
    AnalystView,
    CompanySnapshot,
    CryptoMacro,
    DividendHistory,
    EarningsInfo,
    Filing,
    FilingsList,
    Fundamentals,
    NewsList,
    Period,
    PriceBar,
    PriceHistory,
    SecFinancialLine,
    SecFinancials,
    SymbolMatch,
    SymbolSearch,
)
from scout.server import app as app_module
from scout.server.services import Services


class FakeSource:
    async def get_snapshot(self, symbol, as_of=None):
        return CompanySnapshot(symbol=symbol.upper(), price=Decimal("100"), as_of=as_of)

    async def get_fundamentals(self, symbol, period=Period.ANNUAL, as_of=None):
        return Fundamentals(symbol=symbol.upper(), period=period, revenue=Decimal("1000"))

    async def get_dividends(self, symbol, as_of=None):
        return DividendHistory(symbol=symbol.upper(), growth_streak_years=10)

    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        def bar(day, close, high, low):
            return PriceBar(
                date=date(2024, 1, day),
                close=Decimal(close),
                high=Decimal(high),
                low=Decimal(low),
            )

        bars = [bar(1, "100", "101", "99"), bar(2, "102", "103", "101")]
        return PriceHistory(symbol=symbol.upper(), interval=interval, bars=bars, as_of=as_of)

    async def get_news(self, symbol, limit=10):
        return NewsList(symbol=symbol.upper())

    async def get_earnings(self, symbol, as_of=None):
        return EarningsInfo(symbol=symbol.upper())

    async def get_analyst_view(self, symbol):
        return AnalystView(symbol=symbol.upper(), recommendation_key="hold")

    async def search_symbols(self, query, limit=10):
        return SymbolSearch(query=query, matches=[SymbolMatch(symbol="AAPL", name="Apple Inc.")])


class BrokenSource(FakeSource):
    async def get_snapshot(self, symbol, as_of=None):
        raise RuntimeError("data source down")


class FakeFilings:
    async def get_filings(self, symbol, form_type=None, limit=20, as_of=None):
        return FilingsList(
            symbol=symbol.upper(),
            cik="0000320193",
            filings=[Filing(form="10-K", filing_date=date(2024, 11, 1), accession="x")],
        )


class FakeFinancials:
    async def get_financials(self, symbol, as_of=None):
        return SecFinancials(
            symbol=symbol.upper(),
            cik="0000320193",
            fiscal_year=2024,
            period_end=date(2024, 9, 28),
            lines=[
                SecFinancialLine(concept="revenue", value=Decimal("391035000000"), tag="Revenues")
            ],
        )


@pytest.fixture
def use_source():
    previous = app_module._services

    def _install(source, filings=None, settings=None, financials=None):
        app_module._services = Services(
            settings=settings or get_settings(),
            market_data=source,
            filings=filings,
            financials=financials,
        )

    yield _install
    app_module._services = previous


async def test_company_snapshot_envelope(use_source):
    use_source(FakeSource())
    result = await app_module.company_snapshot("aapl")
    assert result["ok"] is True
    assert result["data"]["symbol"] == "AAPL"
    assert result["data"]["price"] == "100"


async def test_company_snapshot_parses_as_of(use_source):
    use_source(FakeSource())
    result = await app_module.company_snapshot("aapl", as_of="2024-01-15")
    assert result["ok"] is True
    assert result["data"]["as_of"] == "2024-01-15"


async def test_fundamentals_rejects_bad_period(use_source):
    use_source(FakeSource())
    result = await app_module.fundamentals("aapl", period="weekly")
    assert result["ok"] is False
    assert "period" in result["error"]


async def test_dividends_envelope(use_source):
    use_source(FakeSource())
    result = await app_module.dividends("KO")
    assert result["ok"] is True
    assert result["data"]["growth_streak_years"] == 10


async def test_company_dossier_envelope(use_source):
    use_source(FakeSource())
    result = await app_module.company_dossier("aapl")
    assert result["ok"] is True
    assert result["data"]["symbol"] == "AAPL"
    assert result["data"]["snapshot"]["price"] == "100"
    assert result["data"]["fundamentals"] is not None
    assert result["data"]["notes"] == []


async def test_company_dossier_rejects_bad_depth(use_source):
    use_source(FakeSource())
    result = await app_module.company_dossier("aapl", depth="everything")
    assert result["ok"] is False
    assert "depth" in result["error"]


async def test_tool_surfaces_error_as_envelope(use_source):
    use_source(BrokenSource())
    result = await app_module.company_snapshot("aapl")
    assert result["ok"] is False
    assert "data source down" in result["error"]


async def test_search_symbols_tool(use_source):
    use_source(FakeSource())
    result = await app_module.search_symbols("apple")
    assert result["ok"] is True
    assert result["data"]["matches"][0]["symbol"] == "AAPL"


async def test_price_history_tool(use_source):
    use_source(FakeSource())
    result = await app_module.price_history("aapl")
    assert result["ok"] is True
    assert len(result["data"]["bars"]) == 2
    assert result["data"]["bars"][1]["close"] == "102"


async def test_technicals_tool(use_source):
    use_source(FakeSource())
    result = await app_module.technicals("aapl")
    assert result["ok"] is True
    assert result["data"]["last_price"] == "102.0000"
    assert result["data"]["bars_used"] == 2
    assert result["data"]["sma_50"] is None  # only 2 bars


async def test_filings_requires_user_agent(use_source):
    use_source(FakeSource(), filings=FakeFilings())  # default settings → blank UA
    result = await app_module.filings("AAPL")
    assert result["ok"] is False
    assert "SCOUT_SEC_USER_AGENT" in result["error"]


async def test_filings_returns_data_when_configured(use_source):
    use_source(
        FakeSource(),
        filings=FakeFilings(),
        settings=Settings(sec_user_agent="Scout Test test@example.com"),
    )
    result = await app_module.filings("aapl", form_type="10-K")
    assert result["ok"] is True
    assert result["data"]["cik"] == "0000320193"
    assert result["data"]["filings"][0]["form"] == "10-K"


async def test_sec_financials_requires_user_agent(use_source):
    use_source(FakeSource(), financials=FakeFinancials())  # blank UA by default
    result = await app_module.sec_financials("AAPL")
    assert result["ok"] is False
    assert "SCOUT_SEC_USER_AGENT" in result["error"]
    assert result["error"].startswith("unavailable: misconfigured")


async def test_sec_financials_returns_data_when_configured(use_source):
    use_source(
        FakeSource(),
        financials=FakeFinancials(),
        settings=Settings(sec_user_agent="Scout Test test@example.com"),
    )
    result = await app_module.sec_financials("aapl")
    assert result["ok"] is True
    assert result["data"]["fiscal_year"] == 2024
    assert result["data"]["lines"][0]["value"] == "391035000000"


def test_parse_as_of_handles_blank_and_value():
    assert app_module._parse_as_of(None) is None
    assert app_module._parse_as_of("  ") is None
    assert app_module._parse_as_of("2024-03-02") == date(2024, 3, 2)


def test_attach_stablecoin_ratios_computes_ssr():
    macro = CryptoMacro(total_market_cap_usd=Decimal("3000000000000"))
    app_module._attach_stablecoin_ratios(macro, Decimal("250000000000"))
    assert macro.stablecoin_supply_ratio == Decimal("12.00")  # 3T / 250B
    assert macro.stablecoin_dominance == Decimal("0.0833")  # 250B / 3T

    # A missing/zero stablecoin total leaves the ratios null (the join is supplementary).
    untouched = CryptoMacro(total_market_cap_usd=Decimal("3000000000000"))
    app_module._attach_stablecoin_ratios(untouched, None)
    assert untouched.stablecoin_supply_ratio is None
    assert untouched.stablecoin_dominance is None


def test_warm_up_yfinance_swallows_errors(monkeypatch):
    """The startup warm-up must NEVER block the server from starting — a failure is swallowed."""
    import yfinance

    from scout.server import app

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(yfinance, "Ticker", boom)
    app._warm_up_yfinance()  # must return, not raise


def test_warm_up_ccxt_swallows_errors(monkeypatch):
    import ccxt.async_support as ccxt

    from scout.server import app

    def boom(*a, **k):
        raise RuntimeError("no exchange")

    monkeypatch.setattr(ccxt, "binance", boom)
    app._warm_up_ccxt()  # must return, not raise


# --- Source failures must reach the envelope, never as ok:true + null (audit 2026-10-10) -----


class _SchemaChangedSource(FakeSource):
    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        raise ValueError("schema changed")


class _BlockedStooq:
    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        from scout.adapters.retry import BLOCKED, SourceUnavailable

        raise SourceUnavailable(BLOCKED, "stooq returned a JavaScript bot challenge instead of CSV")


async def test_price_history_tool_surfaces_both_source_failures(use_source):
    from scout.adapters.price_fallback import PriceFallbackMarketData

    use_source(PriceFallbackMarketData(_SchemaChangedSource(), _BlockedStooq()))
    result = await app_module.price_history("spy")
    assert result["ok"] is False  # never ok:true with data:null
    assert result["error"].startswith("unavailable: error")
    assert "yfinance failed" in result["error"] and "schema changed" in result["error"]
    assert "stooq fallback failed (unavailable: blocked" in result["error"]


async def test_technicals_tool_surfaces_source_failure(use_source):
    from scout.adapters.price_fallback import PriceFallbackMarketData

    use_source(PriceFallbackMarketData(_SchemaChangedSource(), _BlockedStooq()))
    result = await app_module.technicals("spy")
    assert result["ok"] is False
    assert "unavailable: blocked" in result["error"]


async def test_news_search_tool_reports_gdelt_429_as_source_status():
    import httpx

    from scout.adapters.gdelt import GdeltNewsSearch

    def handler(request):
        return httpx.Response(429, content=b"Please limit requests to one every 5 seconds")

    async def no_sleep(seconds):
        return None

    previous = app_module._services
    app_module._services = Services(
        settings=get_settings(),
        market_data=FakeSource(),
        news_search=GdeltNewsSearch(transport=httpx.MockTransport(handler), sleep=no_sleep),
    )
    try:
        result = await app_module.news_search("bitcoin")
    finally:
        app_module._services = previous
    assert result["ok"] is True
    assert result["data"]["items"] == []
    assert result["data"]["source_status"] == "unavailable: rate_limited"
