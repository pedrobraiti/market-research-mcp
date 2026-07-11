"""yfinance implementation of ``MarketDataSource``.

yfinance is synchronous and network-bound, so every blocking call is pushed to a thread via
``anyio.to_thread.run_sync`` (the anyio-native primitive — this server runs under FastMCP's anyio
loop, so we use anyio's thread offload rather than ``asyncio.to_thread``). Beware a separate,
subtle trap: yfinance's ONE-TIME curl_cffi/crumb setup hangs when it first runs inside a FastMCP
worker thread (it hung the first equity/ETF tool call of every fresh MCP session, only ever over
the stdio transport — never in a plain unit test). The cure is a main-thread warm-up at server
startup (``app._warm_up_yfinance``) that completes that setup before any worker-thread call.

The concrete ``yfinance.Ticker`` is injected via ``ticker_factory`` (imported lazily, only
when the default factory is actually used) so the unit tests run fully offline with a fake.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import anyio

from ...analytics import realized_volatility
from ...domain.models import (
    AnalystView,
    CompanySnapshot,
    DividendHistory,
    DividendPayment,
    EarningsEvent,
    EarningsInfo,
    EtfHolding,
    EtfHoldings,
    Fundamentals,
    InsiderTransaction,
    InstitutionalHolder,
    MoverRow,
    MoversList,
    NewsItem,
    NewsList,
    OptionsVolatility,
    Ownership,
    Period,
    PriceBar,
    PriceHistory,
    QualityMetrics,
    ShortInterest,
    StockSplit,
    SymbolMatch,
    SymbolSearch,
)
from ..retry import RATE_LIMITED, SourceUnavailable, classify_transient

# A revenue/gross-profit/etc. line item is looked up by trying several label spellings,
# since yfinance's row labels drift between versions and filings.
_REVENUE = ("Total Revenue", "Operating Revenue", "Revenue")
_GROSS_PROFIT = ("Gross Profit",)
_OPERATING_INCOME = ("Operating Income", "Total Operating Income As Reported")
_NET_INCOME = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income From Continuing Operation Net Minority Interest",
)
_TOTAL_DEBT = ("Total Debt",)
_TOTAL_CASH = (
    "Cash And Cash Equivalents",
    "Cash Cash Equivalents And Short Term Investments",
    "Cash And Cash Equivalents And Short Term Investments",
)
_FREE_CASH_FLOW = ("Free Cash Flow",)
_TOTAL_ASSETS = ("Total Assets",)
_STOCKHOLDERS_EQUITY = (
    "Stockholders Equity",
    "Total Stockholder Equity",
    "Common Stock Equity",
)
_CURRENT_ASSETS = ("Current Assets", "Total Current Assets")
_CURRENT_LIABILITIES = ("Current Liabilities", "Total Current Liabilities")
_RETAINED_EARNINGS = ("Retained Earnings",)
_INTEREST_EXPENSE = ("Interest Expense", "Interest Expense Non Operating")
_DEP_AMORT = (
    "Depreciation And Amortization",
    "Depreciation Amortization Depletion",
    "Reconciled Depreciation",
)


def _default_ticker_factory(symbol: str) -> Any:
    import yfinance  # imported lazily so tests never need the package or the network

    return yfinance.Ticker(symbol)


def _default_search(query: str) -> list:
    import yfinance

    return yfinance.Search(query).quotes


def _default_screen(predefined_key: str) -> dict:
    import yfinance

    return yfinance.screen(predefined_key)


# Friendly category → yfinance predefined screen id.
_MOVER_CATEGORIES = {
    "gainers": "day_gainers",
    "losers": "day_losers",
    "most_active": "most_actives",
}


def _dec(value: Any) -> Decimal | None:
    """Coerce a yfinance/pandas value to ``Decimal``; NaN/None/garbage become ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if result.is_nan() else result


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # pandas Timestamp and most date-likes expose .date(); fall back to parsing the string.
    to_date = getattr(value, "date", None)
    if callable(to_date):
        try:
            return to_date()
        except Exception:  # noqa: BLE001
            pass
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _quantize(value: Decimal | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def _ratio(numerator: Decimal | None, denominator: Decimal | None, places: int) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _quantize(numerator / denominator, places)


def _ratio_pos(
    numerator: Decimal | None, denominator: Decimal | None, places: int
) -> Decimal | None:
    """Ratio only when the denominator is strictly positive. A negative denominator makes the
    ratio meaningless for ranking (e.g. EV/EBIT with negative EBIT, debt/FCF with negative FCF)."""
    if denominator is None or denominator <= 0:
        return None
    return _ratio(numerator, denominator, places)


def _diff(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a - b


def _add(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a + b


def _altman_z_double_prime(
    working_capital: Decimal | None,
    retained_earnings: Decimal | None,
    ebit: Decimal | None,
    equity: Decimal | None,
    total_assets: Decimal | None,
    total_liabilities: Decimal | None,
) -> Decimal | None:
    """Altman Z″ (the book-equity variant for non-manufacturers/all sectors): uses book equity for
    X4 so no market cap is needed — works on a point-in-time read. >2.6 safe, 1.1–2.6 grey, <1.1
    distress. Null unless all inputs are present and the divisors are positive."""
    parts = (working_capital, retained_earnings, ebit, equity, total_assets, total_liabilities)
    if any(p is None for p in parts) or total_assets <= 0 or total_liabilities <= 0:
        return None
    x1 = working_capital / total_assets
    x2 = retained_earnings / total_assets
    x3 = ebit / total_assets
    x4 = equity / total_liabilities
    return Decimal("6.56") * x1 + Decimal("3.26") * x2 + Decimal("6.72") * x3 + Decimal("1.05") * x4


class YFinanceMarketData:
    def __init__(
        self,
        ticker_factory: Callable[[str], Any] | None = None,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.5,
        search_fn: Callable[[str], list] | None = None,
        screen_fn: Callable[[str], dict] | None = None,
    ) -> None:
        self._ticker_factory = ticker_factory or _default_ticker_factory
        self._retry_attempts = max(1, retry_attempts)
        self._retry_base_delay = retry_base_delay
        self._search_fn = search_fn or _default_search
        self._screen_fn = screen_fn or _default_screen

    # ---- public async API (the port) -------------------------------------------------

    async def get_snapshot(
        self, symbol: str, as_of: date | None = None
    ) -> CompanySnapshot | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._snapshot_sync, symbol.strip().upper(), as_of
        )

    async def get_fundamentals(
        self, symbol: str, period: Period = Period.ANNUAL, as_of: date | None = None
    ) -> Fundamentals | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._fundamentals_sync, symbol.strip().upper(), period, as_of
        )

    async def get_dividends(
        self, symbol: str, as_of: date | None = None
    ) -> DividendHistory | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._dividends_sync, symbol.strip().upper(), as_of
        )

    async def get_price_history(
        self,
        symbol: str,
        range: str = "6mo",
        interval: str = "1d",
        as_of: date | None = None,
    ) -> PriceHistory | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._price_history_sync, symbol.strip().upper(), range, interval, as_of
        )

    async def get_news(self, symbol: str, limit: int = 10) -> NewsList | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._news_sync, symbol.strip().upper(), max(1, min(limit, 50))
        )

    async def get_earnings(self, symbol: str, as_of: date | None = None) -> EarningsInfo | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._earnings_sync, symbol.strip().upper(), as_of
        )

    async def get_analyst_view(self, symbol: str) -> AnalystView | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._analyst_sync, symbol.strip().upper()
        )

    async def search_symbols(self, query: str, limit: int = 10) -> SymbolSearch:
        return await anyio.to_thread.run_sync(
            self._retrying, self._search_sync, query.strip(), max(1, min(limit, 25))
        )

    async def get_movers(self, category: str = "gainers", limit: int = 20) -> MoversList:
        return await anyio.to_thread.run_sync(
            self._retrying, self._movers_sync, category.strip().lower(), max(1, min(limit, 50))
        )

    async def get_quality_metrics(
        self, symbol: str, as_of: date | None = None
    ) -> QualityMetrics | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._quality_sync, symbol.strip().upper(), as_of
        )

    async def get_etf_holdings(self, symbol: str) -> EtfHoldings | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._etf_holdings_sync, symbol.strip().upper()
        )

    async def get_ownership(self, symbol: str) -> Ownership | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._ownership_sync, symbol.strip().upper()
        )

    async def get_options_volatility(
        self, symbol: str, expiry: str | None = None
    ) -> OptionsVolatility | None:
        return await anyio.to_thread.run_sync(
            self._retrying, self._options_sync, symbol.strip().upper(), expiry
        )

    def _retrying(self, func: Callable[..., Any], *args: Any) -> Any:
        """Call a blocking fetch, retrying only TRANSIENT failures with exponential backoff.

        yfinance scrapes Yahoo and is rate-limited; a transient error (429/5xx/timeout) should
        NOT look like a missing symbol, so it is retried and, once exhausted, raised as
        ``SourceUnavailable`` (a machine-readable 'couldn't fetch', not 'fetched, empty'). A
        NON-transient error (a 404, a parse/value bug) is re-raised immediately — retrying it
        would be a pointless storm, and it stays an honest error. A genuinely unresolved symbol
        still returns ``None`` without raising (the sync impls handle that).
        """
        last_error: Exception | None = None
        last_reason: str | None = None
        for attempt in range(self._retry_attempts):
            try:
                return func(*args)
            except SourceUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 — classify, then retry-or-reraise
                reason = classify_transient(exc)
                if reason is None:
                    raise
                last_error, last_reason = exc, reason
                if attempt < self._retry_attempts - 1 and self._retry_base_delay > 0:
                    time.sleep(self._retry_base_delay * (2**attempt))
        raise SourceUnavailable(last_reason or RATE_LIMITED) from last_error

    # ---- blocking implementations ----------------------------------------------------

    def _snapshot_sync(self, symbol: str, as_of: date | None) -> CompanySnapshot | None:
        ticker = self._ticker_factory(symbol)
        info = _info(ticker)
        if as_of is not None:
            price, previous_close = self._history_closes(ticker, as_of)
            if price is None and not info:
                return None
            return CompanySnapshot(
                symbol=symbol,
                name=info.get("longName") or info.get("shortName"),
                currency=info.get("currency") or "USD",
                price=price,
                previous_close=previous_close,
                change=_subtract(price, previous_close),
                change_percent=_pct_change(price, previous_close),
                sector=info.get("sector"),
                industry=info.get("industry"),
                recent_splits=_recent_splits(ticker, as_of),
                as_of=as_of,
            )

        if not info:
            return None
        price = _dec(info.get("currentPrice") or info.get("regularMarketPrice"))
        previous_close = _dec(info.get("previousClose"))
        return CompanySnapshot(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            currency=info.get("currency") or "USD",
            price=price,
            previous_close=previous_close,
            change=_subtract(price, previous_close),
            change_percent=_pct_change(price, previous_close),
            market_cap=_dec(info.get("marketCap")),
            pe_ratio=_dec(info.get("trailingPE")),
            forward_pe=_dec(info.get("forwardPE")),
            dividend_yield=_dec(info.get("dividendYield")),
            fifty_two_week_high=_dec(info.get("fiftyTwoWeekHigh")),
            fifty_two_week_low=_dec(info.get("fiftyTwoWeekLow")),
            sector=info.get("sector"),
            industry=info.get("industry"),
            quote_time=_from_unix(info.get("regularMarketTime")),
            market_state=info.get("marketState"),
            recent_splits=_recent_splits(ticker, None),
            as_of=None,
        )

    def _fundamentals_sync(
        self, symbol: str, period: Period, as_of: date | None
    ) -> Fundamentals | None:
        ticker = self._ticker_factory(symbol)
        quarterly = period is Period.QUARTERLY
        income = _statement(ticker, "quarterly_income_stmt" if quarterly else "income_stmt")
        balance = _statement(ticker, "quarterly_balance_sheet" if quarterly else "balance_sheet")
        cash = _statement(ticker, "quarterly_cashflow" if quarterly else "cashflow")

        column = _pick_column(income, as_of)
        if column is None:
            return None

        revenue = _row(income, column, *_REVENUE)
        gross_profit = _row(income, column, *_GROSS_PROFIT)
        operating_income = _row(income, column, *_OPERATING_INCOME)
        net_income = _row(income, column, *_NET_INCOME)
        balance_col = _pick_column(balance, as_of)
        cash_col = _pick_column(cash, as_of)
        total_debt = _row(balance, balance_col, *_TOTAL_DEBT) if balance_col else None
        total_cash = _row(balance, balance_col, *_TOTAL_CASH) if balance_col else None
        total_assets = _row(balance, balance_col, *_TOTAL_ASSETS) if balance_col else None
        equity = _row(balance, balance_col, *_STOCKHOLDERS_EQUITY) if balance_col else None
        current_assets = _row(balance, balance_col, *_CURRENT_ASSETS) if balance_col else None
        current_liab = _row(balance, balance_col, *_CURRENT_LIABILITIES) if balance_col else None
        retained = _row(balance, balance_col, *_RETAINED_EARNINGS) if balance_col else None
        free_cash_flow = _row(cash, cash_col, *_FREE_CASH_FLOW) if cash_col else None
        interest = _row(income, column, *_INTEREST_EXPENSE)
        dep_amort = (_row(cash, cash_col, *_DEP_AMORT) if cash_col else None) or _row(
            income, column, *_DEP_AMORT
        )

        # Market cap only on a live read: the source has no historical cap to pair with a past
        # statement, so cap-based valuation would silently mix "today" with an old fiscal period.
        market_cap = _dec(_info(ticker).get("marketCap")) if as_of is None else None

        net_debt = _diff(total_debt, total_cash)
        enterprise_value = _add(market_cap, net_debt)  # = market_cap + total_debt − total_cash
        invested_capital = _diff(_add(total_debt, equity), total_cash)
        ebitda = _add(operating_income, dep_amort)  # EBIT + D&A
        working_capital = _diff(current_assets, current_liab)
        total_liabilities = _diff(total_assets, equity)  # accounting identity
        interest_abs = abs(interest) if interest is not None else None
        altman = _altman_z_double_prime(
            working_capital, retained, operating_income, equity, total_assets, total_liabilities
        )
        return Fundamentals(
            symbol=symbol,
            period=period,
            fiscal_period_end=_to_date(column),
            revenue=revenue,
            gross_profit=gross_profit,
            operating_income=operating_income,
            net_income=net_income,
            gross_margin=_ratio(gross_profit, revenue, 6),
            operating_margin=_ratio(operating_income, revenue, 6),
            net_margin=_ratio(net_income, revenue, 6),
            total_debt=total_debt,
            total_cash=total_cash,
            total_assets=total_assets,
            stockholders_equity=equity,
            free_cash_flow=free_cash_flow,
            market_cap=market_cap,
            net_debt=_quantize(net_debt, 2),
            net_debt_to_fcf=_ratio_pos(net_debt, free_cash_flow, 2),
            fcf_margin=_ratio(free_cash_flow, revenue, 6),
            fcf_yield=_ratio(free_cash_flow, market_cap, 6),
            earnings_yield=_ratio(net_income, market_cap, 6),
            enterprise_value=_quantize(enterprise_value, 2),
            ev_to_ebit=_ratio_pos(enterprise_value, operating_income, 4),
            ev_to_sales=_ratio_pos(enterprise_value, revenue, 4),
            ebit_to_ev=_ratio_pos(operating_income, enterprise_value, 4),
            gross_profitability=_ratio_pos(gross_profit, total_assets, 6),
            roic_pretax=_ratio_pos(operating_income, invested_capital, 6),
            current_assets=current_assets,
            current_liabilities=current_liab,
            retained_earnings=retained,
            depreciation_amortization=dep_amort,
            ebitda=_quantize(ebitda, 2),
            current_ratio=_ratio_pos(current_assets, current_liab, 4),
            working_capital=_quantize(working_capital, 2),
            debt_to_equity=_ratio_pos(total_debt, equity, 4),
            net_debt_to_ebitda=_ratio_pos(net_debt, ebitda, 2),
            ev_to_ebitda=_ratio_pos(enterprise_value, ebitda, 4),
            interest_coverage=_ratio_pos(operating_income, interest_abs, 2),
            altman_z=_quantize(altman, 2),
            as_of=as_of,
        )

    def _dividends_sync(self, symbol: str, as_of: date | None) -> DividendHistory | None:
        ticker = self._ticker_factory(symbol)
        series = getattr(ticker, "dividends", None)
        if series is None or len(series) == 0:
            # A valid symbol that simply pays no dividend → an empty, honest history.
            info = _info(ticker)
            if not info and not self._history_closes(ticker, as_of or date.today())[0]:
                return None
            return DividendHistory(symbol=symbol, currency=_currency(info), as_of=as_of)

        payments: list[DividendPayment] = []
        for index_value, amount in series.items():
            ex_date = _to_date(index_value)
            value = _dec(amount)
            if ex_date is None or value is None:
                continue
            if as_of is not None and ex_date > as_of:
                continue
            payments.append(DividendPayment(ex_date=ex_date, amount=value))
        payments.sort(key=lambda p: p.ex_date)

        info = _info(ticker)
        reference = as_of or date.today()
        next_ex = _from_unix(info.get("exDividendDate")) if info else None
        trailing_12m = _trailing_sum(payments, reference)
        price = _dec(info.get("currentPrice") or info.get("regularMarketPrice")) if info else None
        annual = _annual_totals(payments)
        return DividendHistory(
            symbol=symbol,
            currency=_currency(info),
            trailing_12m=trailing_12m,
            trailing_yield=_ratio(trailing_12m, price, 6),
            growth_streak_years=_growth_streak(annual, reference.year),
            had_cut=_had_cut(annual, reference.year),
            next_ex_dividend=next_ex.date() if next_ex else None,
            payments=payments,
            as_of=as_of,
        )

    def _price_history_sync(
        self, symbol: str, range_: str, interval: str, as_of: date | None
    ) -> PriceHistory | None:
        ticker = self._ticker_factory(symbol)
        history = getattr(ticker, "history", None)
        if not callable(history):
            return None
        frame = history(period=range_, interval=interval)
        if frame is None or len(frame) == 0 or "Close" not in getattr(frame, "columns", []):
            return None
        bars: list[PriceBar] = []
        for index_value, row in frame.iterrows():
            bar_date = _to_date(index_value)
            if bar_date is None or (as_of is not None and bar_date > as_of):
                continue
            bars.append(
                PriceBar(
                    date=bar_date,
                    open=_dec(row.get("Open")),
                    high=_dec(row.get("High")),
                    low=_dec(row.get("Low")),
                    close=_dec(row.get("Close")),
                    volume=_volume(row.get("Volume")),
                )
            )
        return PriceHistory(symbol=symbol, interval=interval, bars=bars, as_of=as_of)

    def _news_sync(self, symbol: str, limit: int) -> NewsList:
        ticker = self._ticker_factory(symbol)
        raw = getattr(ticker, "news", None) or []
        items: list[NewsItem] = []
        for entry in raw[:limit]:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content") if isinstance(entry.get("content"), dict) else None
            if content:  # newer yfinance schema
                items.append(
                    NewsItem(
                        title=content.get("title"),
                        summary=content.get("summary") or content.get("description"),
                        url=_nested(content, "canonicalUrl", "url")
                        or _nested(content, "clickThroughUrl", "url"),
                        publisher=_nested(content, "provider", "displayName"),
                        published=_parse_datetime(content.get("pubDate")),
                    )
                )
            else:  # older flat schema
                items.append(
                    NewsItem(
                        title=entry.get("title"),
                        summary=entry.get("summary"),
                        url=entry.get("link"),
                        publisher=entry.get("publisher"),
                        published=_from_unix(entry.get("providerPublishTime")),
                    )
                )
        return NewsList(symbol=symbol, items=items)

    def _earnings_sync(self, symbol: str, as_of: date | None) -> EarningsInfo:
        ticker = self._ticker_factory(symbol)
        getter = getattr(ticker, "get_earnings_dates", None)
        frame = getter(limit=16) if callable(getter) else getattr(ticker, "earnings_dates", None)
        if frame is None or getattr(frame, "empty", True):
            return EarningsInfo(symbol=symbol)
        reference = as_of or date.today()
        events: list[EarningsEvent] = []
        next_date: date | None = None
        for index_value, row in frame.iterrows():
            event_date = _to_date(index_value)
            reported = _dec(row.get("Reported EPS"))
            is_future = reported is None and (event_date is None or event_date >= reference)
            events.append(
                EarningsEvent(
                    event_date=event_date,
                    eps_estimate=_dec(row.get("EPS Estimate")),
                    eps_reported=reported,
                    surprise_percent=_dec(row.get("Surprise(%)")),
                    is_future=is_future,
                )
            )
            if is_future and event_date is not None:
                if next_date is None or event_date < next_date:
                    next_date = event_date
        events.sort(key=lambda e: e.event_date or date.min)

        past = [e for e in events if not e.is_future and e.surprise_percent is not None]
        beat_rate = avg_surprise = consistency = None
        if past:
            surprises = [float(e.surprise_percent) for e in past]
            beat_rate = _quantize(_dec(sum(1 for s in surprises if s > 0) / len(surprises)), 4)
            mean_surprise = sum(surprises) / len(surprises)
            avg_surprise = _quantize(_dec(mean_surprise), 4)
            if len(surprises) >= 3:
                variance = sum((s - mean_surprise) ** 2 for s in surprises) / (len(surprises) - 1)
                consistency = _quantize(_dec(variance**0.5), 4)
        streak = 0
        for event in reversed(events):  # newest first (events are oldest→newest)
            if event.is_future:
                continue
            if event.surprise_percent is not None and event.surprise_percent > 0:
                streak += 1
            else:
                break
        return EarningsInfo(
            symbol=symbol,
            next_earnings_date=next_date,
            beat_rate=beat_rate,
            surprise_streak=streak if past else None,
            avg_surprise=avg_surprise,
            surprise_consistency=consistency,
            events=events,
        )

    def _movers_sync(self, category: str, limit: int) -> MoversList:
        predefined = _MOVER_CATEGORIES.get(category)
        if predefined is None:
            raise ValueError(f"category must be one of {sorted(_MOVER_CATEGORIES)}.")
        result = self._screen_fn(predefined)
        quotes = (result or {}).get("quotes") or [] if isinstance(result, dict) else []
        movers: list[MoverRow] = []
        for quote in quotes[:limit]:
            if not isinstance(quote, dict) or not quote.get("symbol"):
                continue
            movers.append(
                MoverRow(
                    symbol=quote["symbol"],
                    name=quote.get("shortName") or quote.get("longName"),
                    price=_dec(quote.get("regularMarketPrice")),
                    change_percent=_quantize(_dec(quote.get("regularMarketChangePercent")), 4),
                    volume=_volume(quote.get("regularMarketVolume")),
                )
            )
        return MoversList(category=category, movers=movers)

    def _search_sync(self, query: str, limit: int) -> SymbolSearch:
        quotes = self._search_fn(query) or []
        matches: list[SymbolMatch] = []
        for quote in quotes[:limit]:
            if not isinstance(quote, dict):
                continue
            symbol = quote.get("symbol")
            if not symbol:
                continue
            matches.append(
                SymbolMatch(
                    symbol=symbol,
                    name=quote.get("shortname") or quote.get("longname"),
                    exchange=quote.get("exchange") or quote.get("exchDisp"),
                    type=quote.get("quoteType") or quote.get("typeDisp"),
                )
            )
        return SymbolSearch(query=query, matches=matches)

    def _options_sync(self, symbol: str, expiry: str | None) -> OptionsVolatility | None:
        ticker = self._ticker_factory(symbol)
        expiries = list(getattr(ticker, "options", None) or [])
        if not expiries:
            return None
        chosen = expiry if (expiry and expiry in expiries) else expiries[0]
        chain = ticker.option_chain(chosen)
        info = _info(ticker)
        price = _dec(info.get("currentPrice") or info.get("regularMarketPrice")) if info else None
        expiry_date = _to_date(chosen)
        days = (expiry_date - date.today()).days if expiry_date else None
        calls = getattr(chain, "calls", None)
        puts = getattr(chain, "puts", None)
        atm_strike, atm_iv = _atm_iv(calls, puts, price)
        move_pct = move_amount = move_low = move_high = None
        if atm_iv is not None and price is not None and days and days > 0:
            move_pct = atm_iv * Decimal(str(math.sqrt(days / 365)))
            move_amount = price * move_pct
            move_low = price - move_amount
            move_high = price + move_amount

        # IV skew: out-of-the-money put IV vs call IV, normalized by ATM (>0 = puts richer = fear).
        otm_put_iv = _otm_iv(puts, price, above=False)
        otm_call_iv = _otm_iv(calls, price, above=True)
        iv_skew = None
        if otm_put_iv is not None and otm_call_iv is not None and atm_iv and atm_iv > 0:
            iv_skew = (otm_put_iv - otm_call_iv) / atm_iv
        # Put/Call ratios over this expiry (sentiment/positioning).
        call_vol, put_vol = _chain_sum(calls, "volume"), _chain_sum(puts, "volume")
        call_oi, put_oi = _chain_sum(calls, "openInterest"), _chain_sum(puts, "openInterest")
        pcr_volume = (
            put_vol / call_vol if put_vol is not None and call_vol and call_vol > 0 else None
        )
        pcr_oi = put_oi / call_oi if put_oi is not None and call_oi and call_oi > 0 else None
        # Volatility risk premium: implied (ATM IV) vs trailing realized vol — stateless IV-rank.
        realized = realized_volatility(_recent_closes(ticker), periods_per_year=252)
        realized_dec = _dec(realized)
        vrp = iv_rv = None
        if atm_iv is not None and realized_dec is not None and realized_dec > 0:
            vrp = atm_iv - realized_dec
            iv_rv = atm_iv / realized_dec
        # IV term structure: one extra (best-effort) chain fetch at a longer expiry. A failure here
        # (e.g. a 429) must not sink the near-expiry read, so it degrades to a null slope.
        far_expiry_str = _pick_far_expiry(expiries, chosen)
        far_atm_iv = far_date = iv_term_slope = term_structure = None
        if far_expiry_str:
            try:
                far_chain = ticker.option_chain(far_expiry_str)
                _, far_atm_iv = _atm_iv(
                    getattr(far_chain, "calls", None), getattr(far_chain, "puts", None), price
                )
                far_date = _to_date(far_expiry_str)
            except Exception:  # noqa: BLE001 — term structure is supplementary
                far_atm_iv = None
        if atm_iv and atm_iv > 0 and far_atm_iv and far_atm_iv > 0:
            iv_term_slope = (far_atm_iv - atm_iv) / atm_iv
            term_structure = "backwardation" if atm_iv > far_atm_iv else "contango"
        return OptionsVolatility(
            symbol=symbol,
            expiry=expiry_date,
            days_to_expiry=days,
            current_price=price,
            atm_strike=atm_strike,
            atm_iv=_quantize(atm_iv, 4),
            expected_move_percent=_quantize(move_pct, 4),
            expected_move_amount=_quantize(move_amount, 2),
            expected_move_low=_quantize(move_low, 2),
            expected_move_high=_quantize(move_high, 2),
            iv_skew=_quantize(iv_skew, 4),
            put_call_ratio_volume=_quantize(pcr_volume, 4),
            put_call_ratio_oi=_quantize(pcr_oi, 4),
            realized_vol=_quantize(realized_dec, 4),
            iv_rv_ratio=_quantize(iv_rv, 4),
            volatility_risk_premium=_quantize(vrp, 4),
            far_expiry=far_date,
            far_atm_iv=_quantize(far_atm_iv, 4),
            iv_term_slope=_quantize(iv_term_slope, 4),
            iv_term_structure=term_structure,
            note=None if atm_iv is not None else "No implied vol available for this expiry.",
        )

    def _ownership_sync(self, symbol: str) -> Ownership | None:
        ticker = self._ticker_factory(symbol)
        insider_pct, institution_pct = _major_holder_pcts(getattr(ticker, "major_holders", None))

        institutions: list[InstitutionalHolder] = []
        inst_frame = getattr(ticker, "institutional_holders", None)
        if inst_frame is not None and not getattr(inst_frame, "empty", True):
            for _, row in list(inst_frame.iterrows())[:10]:
                institutions.append(
                    InstitutionalHolder(
                        holder=row.get("Holder"),
                        shares=_dec(row.get("Shares")),
                        pct_out=_dec(row.get("pctHeld") or row.get("% Out")),
                        value=_dec(row.get("Value")),
                    )
                )

        insiders: list[InsiderTransaction] = []
        insider_frame = getattr(ticker, "insider_transactions", None)
        if insider_frame is not None and not getattr(insider_frame, "empty", True):
            for _, row in list(insider_frame.iterrows())[:15]:
                insiders.append(
                    InsiderTransaction(
                        transaction_date=_to_date(row.get("Start Date") or row.get("Date")),
                        insider=row.get("Insider"),
                        position=row.get("Position"),
                        transaction=row.get("Transaction") or row.get("Text"),
                        shares=_dec(row.get("Shares")),
                        value=_dec(row.get("Value")),
                    )
                )

        if insider_pct is None and institution_pct is None and not institutions and not insiders:
            return None
        return Ownership(
            symbol=symbol,
            insider_percent=insider_pct,
            institution_percent=institution_pct,
            institutional_holders=institutions,
            insider_transactions=insiders,
            short_interest=_short_interest(ticker),
        )

    def _etf_holdings_sync(self, symbol: str) -> EtfHoldings | None:
        try:
            funds = getattr(self._ticker_factory(symbol), "funds_data", None)
        except Exception:  # noqa: BLE001 — yfinance raises for non-funds
            return None
        if funds is None:
            return None
        holdings: list[EtfHolding] = []
        top = getattr(funds, "top_holdings", None)
        if top is not None and not getattr(top, "empty", True):
            for index_value, row in top.iterrows():
                weight = _dec(row.get("Holding Percent"))
                holdings.append(
                    EtfHolding(symbol=str(index_value), name=row.get("Name"), weight=weight)
                )
        sector_weights: dict = {}
        raw_sectors = getattr(funds, "sector_weightings", None)
        if isinstance(raw_sectors, dict):
            for sector, weight in raw_sectors.items():
                value = _dec(weight)
                if value is not None:
                    sector_weights[str(sector)] = value
        if not holdings and not sector_weights:
            return None
        return EtfHoldings(symbol=symbol, top_holdings=holdings, sector_weights=sector_weights)

    def _quality_sync(self, symbol: str, as_of: date | None) -> QualityMetrics | None:
        ticker = self._ticker_factory(symbol)
        info = _info(ticker)
        income = _statement(ticker, "income_stmt")
        revenue_cagr, net_cagr, years = _statement_cagr(income, as_of)
        if not info and revenue_cagr is None:
            return None
        return QualityMetrics(
            symbol=symbol,
            roe=_quantize(_dec(info.get("returnOnEquity")), 6),
            roa=_quantize(_dec(info.get("returnOnAssets")), 6),
            gross_margin=_quantize(_dec(info.get("grossMargins")), 6),
            operating_margin=_quantize(_dec(info.get("operatingMargins")), 6),
            net_margin=_quantize(_dec(info.get("profitMargins")), 6),
            revenue_growth_yoy=_quantize(_dec(info.get("revenueGrowth")), 6),
            earnings_growth_yoy=_quantize(_dec(info.get("earningsGrowth")), 6),
            revenue_cagr=revenue_cagr,
            net_income_cagr=net_cagr,
            cagr_years=years,
            as_of=as_of,
        )

    def _analyst_sync(self, symbol: str) -> AnalystView | None:
        info = _info(self._ticker_factory(symbol))
        if not info:
            return None
        current_price = _dec(info.get("currentPrice") or info.get("regularMarketPrice"))
        target_mean = _dec(info.get("targetMeanPrice"))
        target_high = _dec(info.get("targetHighPrice"))
        target_low = _dec(info.get("targetLowPrice"))
        return AnalystView(
            symbol=symbol,
            recommendation_key=info.get("recommendationKey"),
            recommendation_mean=_dec(info.get("recommendationMean")),
            number_of_analysts=_int(info.get("numberOfAnalystOpinions")),
            current_price=current_price,
            target_mean=target_mean,
            target_median=_dec(info.get("targetMedianPrice")),
            target_high=target_high,
            target_low=target_low,
            upside_pct=_ratio(_diff(target_mean, current_price), current_price, 6),
            target_dispersion=_ratio_pos(_diff(target_high, target_low), target_mean, 6),
        )

    def _history_closes(self, ticker: Any, as_of: date) -> tuple[Decimal | None, Decimal | None]:
        """Return (close at/just before as_of, the close before that), best-effort.

        yfinance's ``end`` is EXCLUSIVE, so it must be ``as_of + 1 day`` for the ``as_of`` bar
        itself to be included. The window reaches ~10 calendar days back — enough to always
        contain ≥ 2 trading sessions (a first-of-the-month window had 0-1 sessions when
        ``as_of`` fell on days 1-3, silently returning no price)."""
        history = getattr(ticker, "history", None)
        if not callable(history):
            return None, None
        try:
            frame = history(
                start=str(as_of - timedelta(days=_AS_OF_CLOSE_LOOKBACK_DAYS)),
                end=str(as_of + timedelta(days=1)),
            )
        except Exception:  # noqa: BLE001
            return None, None
        if frame is None or len(frame) == 0 or "Close" not in getattr(frame, "columns", []):
            return None, None
        closes = [_dec(v) for v in frame["Close"].tolist()]
        closes = [c for c in closes if c is not None]
        if not closes:
            return None, None
        price = closes[-1]
        previous = closes[-2] if len(closes) >= 2 else None
        return price, previous


# ---- module-level helpers (pure, easy to reason about) -------------------------------

# A split older than this (relative to as_of, or today) is company history, not "recent".
_RECENT_SPLIT_WINDOW = timedelta(days=365 * 2)  # ~24 months

# Calendar days of history fetched to locate the close at/just before an as_of date: long
# enough to bridge weekends + multi-day holiday closures, short enough to stay one cheap call.
_AS_OF_CLOSE_LOOKBACK_DAYS = 10


def _info(ticker: Any) -> dict:
    # Deliberately does NOT swallow exceptions: a transient/HTTP error must propagate so the
    # retry wrapper can back off (instead of masquerading as an unresolved symbol). A symbol
    # that simply has no data returns a non-dict/empty payload, which becomes {} → unresolved.
    info = ticker.info
    return info if isinstance(info, dict) else {}


def _recent_splits(ticker: Any, as_of: date | None, limit: int = 3) -> list[StockSplit]:
    """Genuinely *recent* stock splits (newest first), at or before ``as_of``.

    yfinance exposes ``Ticker.splits`` (a date-indexed ratio series), but it spans a company's
    whole history — so a naive "latest few" surfaces decades-old splits and is never empty for
    anything that ever split, defeating the point. Only splits within ``_RECENT_SPLIT_WINDOW``
    of the reference date (``as_of`` when given, else today) qualify; older ones are history,
    not "recent", and yield an empty list. This keeps the field honest: a non-empty list means
    "this stock actually split recently", e.g. NFLX's post-10:1 price is correct-and-adjusted,
    not "wrong" against pre-split memory.

    It is supplementary context — a transient failure or its absence must NEVER fail the
    snapshot — so every error degrades to an empty list.
    """
    try:
        series = getattr(ticker, "splits", None)
        if series is None or len(series) == 0:
            return []
        reference = as_of or date.today()
        cutoff = reference - _RECENT_SPLIT_WINDOW
        splits: list[StockSplit] = []
        for index_value, ratio in series.items():
            split_date = _to_date(index_value)
            value = _dec(ratio)
            if split_date is None or value is None or value <= 0:
                continue
            if split_date > reference or split_date < cutoff:
                continue
            splits.append(StockSplit(date=split_date, ratio=value))
        splits.sort(key=lambda s: s.date, reverse=True)
        return splits[:limit]
    except Exception:  # noqa: BLE001 — splits is best-effort; never break the snapshot over it
        return []


def _currency(info: dict) -> str:
    return info.get("currency") or "USD"


def _volume(value: Any) -> int | None:
    decimal_value = _dec(value)
    return int(decimal_value) if decimal_value is not None else None


def _int(value: Any) -> int | None:
    decimal_value = _dec(value)
    return int(decimal_value) if decimal_value is not None else None


def _nearest_iv(frame: Any, price: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """Strike and implied vol of the option whose strike is closest to ``price``."""
    if frame is None or getattr(frame, "empty", True) or price is None:
        return None, None
    target = float(price)
    best_strike: Decimal | None = None
    best_iv: Decimal | None = None
    best_diff = float("inf")
    for _, row in frame.iterrows():
        strike = _dec(row.get("strike"))
        if strike is None:
            continue
        diff = abs(float(strike) - target)
        if diff < best_diff:
            best_diff, best_strike, best_iv = diff, strike, _dec(row.get("impliedVolatility"))
    return best_strike, best_iv


def _atm_iv(
    calls: Any, puts: Any, price: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    call_strike, call_iv = _nearest_iv(calls, price)
    _, put_iv = _nearest_iv(puts, price)
    ivs = [iv for iv in (call_iv, put_iv) if iv is not None and iv > 0]
    average_iv = sum(ivs) / len(ivs) if ivs else None
    return call_strike, average_iv


def _pick_far_expiry(expiries: list[str], near: str, target_days: int = 45) -> str | None:
    """A longer-dated expiry (after ``near``) closest to ``target_days`` out, for the IV term
    structure. Returns None if there's no expiry beyond the near one."""
    near_date = _to_date(near)
    if near_date is None:
        return None
    today = date.today()
    best: str | None = None
    best_diff = float("inf")
    for expiry in expiries:
        far_date = _to_date(expiry)
        if far_date is None or far_date <= near_date:
            continue
        diff = abs((far_date - today).days - target_days)
        if diff < best_diff:
            best_diff, best = diff, expiry
    return best


def _chain_sum(frame: Any, column: str) -> Decimal | None:
    """Sum a numeric column (volume / openInterest) across an option-chain frame."""
    if frame is None or getattr(frame, "empty", True):
        return None
    if column not in getattr(frame, "columns", []):
        return None
    total = Decimal(0)
    seen = False
    for value in frame[column].tolist():
        dec = _dec(value)
        if dec is not None:
            total += dec
            seen = True
    return total if seen else None


def _otm_iv(frame: Any, price: Decimal | None, *, above: bool) -> Decimal | None:
    """IV of the nearest out-of-the-money strike — above the price for calls, below for puts.
    Skips non-positive IV (illiquid wings often report 0/NaN) so skew isn't built on garbage."""
    if frame is None or getattr(frame, "empty", True) or price is None:
        return None
    best_iv: Decimal | None = None
    best_diff = float("inf")
    target = float(price)
    for _, row in frame.iterrows():
        strike = _dec(row.get("strike"))
        iv = _dec(row.get("impliedVolatility"))
        if strike is None or iv is None or iv <= 0:
            continue
        if above and strike <= price:
            continue
        if not above and strike >= price:
            continue
        diff = abs(float(strike) - target)
        if diff < best_diff:
            best_diff, best_iv = diff, iv
    return best_iv


def _recent_closes(ticker: Any, period: str = "3mo") -> list[float]:
    """Trailing daily closes as floats (best-effort) for a realized-volatility read."""
    history = getattr(ticker, "history", None)
    if not callable(history):
        return []
    try:
        frame = history(period=period)
    except Exception:  # noqa: BLE001 — realized vol is supplementary; never fail the options read
        return []
    if frame is None or len(frame) == 0 or "Close" not in getattr(frame, "columns", []):
        return []
    return [float(c) for c in frame["Close"].tolist() if c is not None]


def _major_holder_pcts(frame: Any) -> tuple[Decimal | None, Decimal | None]:
    """Parse insider/institution percentages from yfinance's ``major_holders`` (modern format)."""
    if frame is None or getattr(frame, "empty", True):
        return None, None
    index = list(getattr(frame, "index", []))
    columns = list(getattr(frame, "columns", []))
    has_pct = "insidersPercentHeld" in index or "institutionsPercentHeld" in index
    if not columns or not has_pct:
        return None, None
    column = columns[0]
    insider = None
    if "insidersPercentHeld" in index:
        insider = _dec(frame.at["insidersPercentHeld", column])
    institution = None
    if "institutionsPercentHeld" in index:
        institution = _dec(frame.at["institutionsPercentHeld", column])
    return insider, institution


def _short_interest(ticker: Any) -> ShortInterest | None:
    """Short-interest block from yfinance's ``.info`` — bi-monthly, point-in-time.

    Supplementary to the holder data: ``.info`` is a separate (slower, sometimes missing) fetch,
    so a failure here must NOT sink the ownership read — it degrades to ``None``. Every figure is
    null when the source doesn't expose it (never fabricated); the block itself is ``None`` when no
    short field is present at all, so an empty block isn't mistaken for "zero short interest".
    """
    try:
        info = ticker.info
    except Exception:  # noqa: BLE001 — short interest is best-effort; never break ownership
        return None
    if not isinstance(info, dict):
        return None
    shares_short = _dec(info.get("sharesShort"))
    prior_month = _dec(info.get("sharesShortPriorMonth"))
    short_ratio = _dec(info.get("shortRatio"))
    pct_float = _dec(info.get("shortPercentOfFloat"))
    pct_out = _dec(info.get("sharesPercentSharesOut"))
    si_date = _from_unix(info.get("dateShortInterest"))
    if all(
        value is None
        for value in (shares_short, prior_month, short_ratio, pct_float, pct_out, si_date)
    ):
        return None
    change_pct = None
    if shares_short is not None and prior_month is not None and prior_month != 0:
        change_pct = _quantize((shares_short - prior_month) / prior_month * Decimal(100), 4)
    return ShortInterest(
        shares_short=shares_short,
        shares_short_prior_month=prior_month,
        short_ratio_days_to_cover=short_ratio,
        short_percent_of_float=pct_float,
        short_percent_of_shares_out=pct_out,
        short_interest_date=si_date.date() if si_date else None,
        short_interest_change_pct=change_pct,
    )


def _nested(data: dict, *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) else None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _from_unix(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC) if value else None
    except (ValueError, TypeError, OSError):
        return None


def _subtract(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return a - b if a is not None and b is not None else None


def _pct_change(price: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if price is None or previous is None or previous == 0:
        return None
    return _quantize((price - previous) / previous * Decimal(100), 4)


def _statement(ticker: Any, attr: str) -> Any:
    return getattr(ticker, attr, None)


def _pick_column(statement: Any, as_of: date | None) -> Any:
    """Pick the statement column (period end) that is latest, or latest at/before as_of."""
    if statement is None or getattr(statement, "empty", True):
        return None
    columns = list(statement.columns)
    if not columns:
        return None
    if as_of is None:
        return max(columns, key=lambda c: _to_date(c) or date.min)
    eligible = [c for c in columns if (_to_date(c) or date.max) <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda c: _to_date(c) or date.min)


def _row(statement: Any, column: Any, *labels: str) -> Decimal | None:
    if statement is None or column is None:
        return None
    index = getattr(statement, "index", [])
    for label in labels:
        if label in index:
            try:
                return _dec(statement.at[label, column])
            except (KeyError, IndexError):
                continue
    return None


def _cagr(points: list[tuple[date | None, Decimal | None]]) -> Decimal | None:
    """CAGR over dated (fiscal-period-end, value) points, annualized by the REAL date span.
    Counting intervals (``len − 1``) instead would over-annualize whenever a fiscal year is
    missing from the statement — 2021→2024 with 2022 absent is 3 years of growth, not 2."""
    clean = [(d, v) for d, v in points if d is not None and v is not None]
    if len(clean) < 2:
        return None
    (first_date, first), (last_date, last) = clean[0], clean[-1]
    if first <= 0 or last <= 0:  # CAGR is undefined across non-positive endpoints
        return None
    years = (last_date - first_date).days / 365.25
    if years <= 0:
        return None
    result = (float(last) / float(first)) ** (1.0 / years) - 1.0
    return _quantize(Decimal(str(result)), 6)


def _statement_cagr(
    statement: Any, as_of: date | None
) -> tuple[Decimal | None, Decimal | None, int | None]:
    if statement is None or getattr(statement, "empty", True):
        return None, None, None
    limit = as_of or date.max
    columns = [c for c in statement.columns if (_to_date(c) or date.max) <= limit]
    columns.sort(key=lambda c: _to_date(c) or date.min)  # oldest → newest
    if len(columns) < 2:
        return None, None, (len(columns) or None)
    revenue = [(_to_date(c), _row(statement, c, *_REVENUE)) for c in columns]
    net_income = [(_to_date(c), _row(statement, c, *_NET_INCOME)) for c in columns]
    first_date, last_date = _to_date(columns[0]), _to_date(columns[-1])
    span_years = (
        round((last_date - first_date).days / 365.25)
        if first_date is not None and last_date is not None
        else None
    )
    return _cagr(revenue), _cagr(net_income), (span_years or None)


def _one_year_before(reference: date) -> date:
    try:
        return reference.replace(year=reference.year - 1)
    except ValueError:  # Feb 29 on a leap year — the prior year has no leap day
        return reference.replace(year=reference.year - 1, day=28)


def _trailing_sum(payments: list[DividendPayment], reference: date) -> Decimal | None:
    window_start = _one_year_before(reference)
    total = sum(
        (p.amount for p in payments if window_start < p.ex_date <= reference), Decimal(0)
    )
    return total if total > 0 else None


def _annual_totals(payments: list[DividendPayment]) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = {}
    for payment in payments:
        totals[payment.ex_date.year] = totals.get(payment.ex_date.year, Decimal(0)) + payment.amount
    return totals


def _had_cut(annual: dict[int, Decimal], current_year: int) -> bool | None:
    """True if any ADJACENT COMPLETE calendar year cut the dividend.

    The incomplete current year is excluded: a year still in progress has fewer ex-dates than
    a full year, so its lower calendar-year total is a payment-timing artifact, not a real cut
    (this is what produced a spurious ``had_cut`` for names like Micron mid-year).

    Only consecutive years are compared — a multi-year gap (a suspension with no data, or a
    company that paid decades ago then resumed) is NOT treated as a cut, because absent data and
    a true cut are indistinguishable here. We report only what we can stand behind.
    """
    complete = {y: total for y, total in annual.items() if y < current_year}
    if len(complete) < 2:
        return None
    years = sorted(complete)
    return any(
        complete[y] < complete[prev]
        for prev, y in zip(years, years[1:], strict=False)
        if y - prev == 1
    )


def _growth_streak(annual: dict[int, Decimal], current_year: int) -> int | None:
    """Consecutive most-recent COMPLETE calendar years of non-decreasing annual dividend.

    Counts only truly adjacent years: a gap in the history ends the streak rather than being
    jumped over (which would inflate the count across a dividend suspension).
    """
    complete = {y: total for y, total in annual.items() if y < current_year}
    if len(complete) < 2:
        return None
    years = sorted(complete, reverse=True)
    streak = 0
    for newer, older in zip(years, years[1:], strict=False):
        if newer - older == 1 and complete[newer] >= complete[older]:
            streak += 1
        else:
            break
    return streak
