from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from scout.adapters.yfinance import YFinanceMarketData
from scout.adapters.yfinance.market_data import _had_cut, _trailing_sum
from scout.domain.models import DividendPayment

_INFO = {
    "longName": "Apple Inc.",
    "shortName": "Apple",
    "currency": "USD",
    "currentPrice": 230.0,
    "previousClose": 228.0,
    "marketCap": 3_500_000_000_000,
    "trailingPE": 35.5,
    "forwardPE": 30.1,
    "dividendYield": 0.0044,
    "fiftyTwoWeekHigh": 260.1,
    "fiftyTwoWeekLow": 164.0,
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "regularMarketTime": 1_718_000_000,  # unix; live-quote freshness stamp
    "marketState": "REGULAR",
    "recommendationKey": "buy",
    "recommendationMean": 2.1,
    "numberOfAnalystOpinions": 35,
    "targetMeanPrice": 290.0,
    "targetMedianPrice": 295.0,
    "targetHighPrice": 340.0,
    "targetLowPrice": 200.0,
}

_NEWS = [
    {
        "content": {
            "title": "Apple unveils new chip",
            "summary": "The company announced a faster processor.",
            "canonicalUrl": {"url": "https://news.example/aapl-chip"},
            "provider": {"displayName": "Reuters"},
            "pubDate": "2026-06-20T14:30:00Z",
        }
    }
]

_INCOME = pd.DataFrame(
    {
        pd.Timestamp("2024-09-30"): {
            "Total Revenue": 391_035_000_000.0,
            "Gross Profit": 180_683_000_000.0,
            "Operating Income": 123_216_000_000.0,
            "Net Income": 93_736_000_000.0,
            "Interest Expense": 3_933_000_000.0,
        },
        pd.Timestamp("2023-09-30"): {
            "Total Revenue": 383_285_000_000.0,
            "Gross Profit": 169_148_000_000.0,
            "Operating Income": 114_301_000_000.0,
            "Net Income": 96_995_000_000.0,
            "Interest Expense": 3_933_000_000.0,
        },
    }
)

_BALANCE = pd.DataFrame(
    {
        pd.Timestamp("2024-09-30"): {
            "Total Debt": 106_629_000_000.0,
            "Cash And Cash Equivalents": 29_943_000_000.0,
            "Total Assets": 364_980_000_000.0,
            "Stockholders Equity": 56_950_000_000.0,
            "Current Assets": 152_987_000_000.0,
            "Current Liabilities": 176_392_000_000.0,
            "Retained Earnings": 50_000_000_000.0,
        }
    }
)

_CASHFLOW = pd.DataFrame(
    {
        pd.Timestamp("2024-09-30"): {
            "Free Cash Flow": 108_807_000_000.0,
            "Depreciation And Amortization": 11_445_000_000.0,
        }
    }
)

_DIVIDENDS = pd.Series(
    {
        pd.Timestamp("2021-08-06"): 0.205,
        pd.Timestamp("2022-08-05"): 0.23,
        pd.Timestamp("2023-08-11"): 0.24,
        pd.Timestamp("2024-08-12"): 0.25,
    }
)


_SPLITS = pd.Series(
    {
        pd.Timestamp("2014-06-09"): 7.0,
        pd.Timestamp("2020-08-31"): 4.0,
    }
)


class FakeTicker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.info = _INFO
        self.dividends = _DIVIDENDS
        self.splits = _SPLITS
        self.income_stmt = _INCOME
        self.quarterly_income_stmt = _INCOME
        self.balance_sheet = _BALANCE
        self.quarterly_balance_sheet = _BALANCE
        self.cashflow = _CASHFLOW
        self.quarterly_cashflow = _CASHFLOW
        self.news = _NEWS

    def get_earnings_dates(self, limit=12):
        return pd.DataFrame(
            {
                "EPS Estimate": [1.50, 1.40],
                "Reported EPS": [float("nan"), 1.46],
                "Surprise(%)": [float("nan"), 4.3],
            },
            index=[pd.Timestamp("2026-08-01"), pd.Timestamp("2026-05-01")],
        )

    def history(self, period=None, interval=None, start=None, end=None, **kwargs):
        frame = pd.DataFrame(
            {
                "Open": [220.0, 221.5],
                "High": [222.0, 224.0],
                "Low": [219.0, 221.0],
                "Close": [221.0, 223.5],
                "Volume": [1_000_000, 1_100_000],
            },
            index=[pd.Timestamp("2024-06-10"), pd.Timestamp("2024-06-11")],
        )
        # Faithful to the real yfinance contract: ``start`` inclusive, ``end`` EXCLUSIVE.
        # (An earlier fake ignored start/end entirely, which hid an off-by-one for 9 sessions:
        # code calling history(end=as_of) never gets the as_of bar from the real library.)
        if start is not None:
            frame = frame[frame.index >= pd.Timestamp(start)]
        if end is not None:
            frame = frame[frame.index < pd.Timestamp(end)]
        return frame


class EmptyTicker:
    def __init__(self, symbol: str) -> None:
        self.info = {}
        self.dividends = pd.Series(dtype=float)

    def history(self, start=None, end=None):
        return pd.DataFrame()


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _RateLimited(Exception):
    """A 429-shaped error (carries .response.status_code) — classified as a transient failure."""

    def __init__(self) -> None:
        self.response = _Resp(429)
        super().__init__("HTTP 429")


class FlakyTicker:
    """Raises on `.info` the first ``fail_times`` accesses, then succeeds — to test retry."""

    fail_times = 2
    accesses = {"n": 0}

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.dividends = _DIVIDENDS

    @property
    def info(self):
        FlakyTicker.accesses["n"] += 1
        if FlakyTicker.accesses["n"] <= FlakyTicker.fail_times:
            raise _RateLimited()
        return _INFO


class BrokenTicker:
    """Always raises a NON-transient error on `.info` — must NOT be retried."""

    accesses = {"n": 0}

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def info(self):
        BrokenTicker.accesses["n"] += 1
        raise ValueError("genuine parse bug")


def _source():
    return YFinanceMarketData(ticker_factory=FakeTicker)


async def test_snapshot_parses_price_and_multiples():
    snapshot = await _source().get_snapshot("aapl")
    assert snapshot is not None
    assert snapshot.symbol == "AAPL"
    assert snapshot.name == "Apple Inc."
    assert snapshot.price == Decimal("230.0")
    assert snapshot.change == Decimal("2.0")
    assert snapshot.change_percent is not None
    assert Decimal("0.8") < snapshot.change_percent < Decimal("0.9")
    assert snapshot.market_cap == Decimal("3500000000000")
    assert snapshot.sector == "Technology"
    assert snapshot.as_of is None
    # Live path carries a freshness stamp so a stale weekend close ≠ a live tick.
    assert snapshot.market_state == "REGULAR"
    assert snapshot.quote_time is not None
    assert snapshot.quote_time.year == 2024


async def test_snapshot_with_as_of_uses_history_close():
    # The fake honours yfinance's EXCLUSIVE ``end`` — so this only passes if the adapter asks
    # for end = as_of + 1 day. The old end=as_of call could never return the as_of close.
    snapshot = await _source().get_snapshot("AAPL", as_of=date(2024, 6, 11))
    assert snapshot is not None
    assert snapshot.price == Decimal("223.5")
    assert snapshot.previous_close == Decimal("221.0")
    assert snapshot.as_of == date(2024, 6, 11)
    # Historical reads have no live freshness stamp.
    assert snapshot.quote_time is None
    assert snapshot.market_state is None


async def test_snapshot_as_of_first_days_of_month_still_finds_price():
    # as_of on day 1 of a month: a window opened at day 1 contains 0-1 sessions → price None.
    # The lookback window must reach into the previous month for the last sessions before as_of.
    class LateJuneTicker(FakeTicker):
        def history(self, period=None, interval=None, start=None, end=None, **kwargs):
            frame = pd.DataFrame(
                {"Close": [500.0, 502.5]},
                index=[pd.Timestamp("2024-06-27"), pd.Timestamp("2024-06-28")],
            )
            if start is not None:
                frame = frame[frame.index >= pd.Timestamp(start)]
            if end is not None:
                frame = frame[frame.index < pd.Timestamp(end)]  # yfinance end is EXCLUSIVE
            return frame

    source = YFinanceMarketData(ticker_factory=LateJuneTicker)
    snapshot = await source.get_snapshot("AAPL", as_of=date(2024, 7, 1))
    assert snapshot is not None
    assert snapshot.price == Decimal("502.5")  # last close at/just before as_of
    assert snapshot.previous_close == Decimal("500.0")


async def test_snapshot_surfaces_only_recent_splits_newest_first():
    today = date.today()
    recent = today - timedelta(days=120)
    older_but_recent = today - timedelta(days=600)
    ancient = today - timedelta(days=365 * 10)

    class RecentAndAncientSplitTicker(FakeTicker):
        def __init__(self, symbol):
            super().__init__(symbol)
            self.splits = pd.Series(
                {
                    pd.Timestamp(ancient): 7.0,
                    pd.Timestamp(older_but_recent): 3.0,
                    pd.Timestamp(recent): 10.0,
                }
            )

    source = YFinanceMarketData(ticker_factory=RecentAndAncientSplitTicker)
    snapshot = await source.get_snapshot("NFLX")
    assert snapshot is not None
    assert [s.date for s in snapshot.recent_splits] == [recent, older_but_recent]
    assert snapshot.recent_splits[0].ratio == Decimal("10.0")


async def test_snapshot_excludes_decades_old_splits():
    # The default fake ticker only ever split in 2014/2020 — ancient as of today.
    snapshot = await _source().get_snapshot("AAPL")
    assert snapshot is not None
    assert snapshot.recent_splits == []


async def test_snapshot_as_of_windows_splits_around_as_of():
    as_of = date(2024, 1, 1)
    in_window = date(2023, 6, 1)  # within ~24 months before as_of
    after_as_of = date(2024, 6, 1)  # later than as_of -> excluded
    ancient = date(2014, 6, 9)  # decades before as_of -> excluded

    class AsOfSplitTicker(FakeTicker):
        def __init__(self, symbol):
            super().__init__(symbol)
            self.splits = pd.Series(
                {
                    pd.Timestamp(ancient): 7.0,
                    pd.Timestamp(in_window): 4.0,
                    pd.Timestamp(after_as_of): 2.0,
                }
            )

    source = YFinanceMarketData(ticker_factory=AsOfSplitTicker)
    snapshot = await source.get_snapshot("X", as_of=as_of)
    assert snapshot is not None
    assert [s.date for s in snapshot.recent_splits] == [in_window]


async def test_snapshot_splits_empty_when_none():
    class NoSplitsTicker(FakeTicker):
        def __init__(self, symbol):
            super().__init__(symbol)
            self.splits = pd.Series(dtype=float)

    snapshot = await YFinanceMarketData(ticker_factory=NoSplitsTicker).get_snapshot("AAPL")
    assert snapshot is not None
    assert snapshot.recent_splits == []


async def test_snapshot_splits_failure_does_not_break_snapshot():
    class BrokenSplitsTicker(FakeTicker):
        @property
        def splits(self):
            raise RuntimeError("splits fetch blew up")

        @splits.setter
        def splits(self, value):
            pass

    snapshot = await YFinanceMarketData(ticker_factory=BrokenSplitsTicker).get_snapshot("AAPL")
    assert snapshot is not None
    assert snapshot.price == Decimal("230.0")
    assert snapshot.recent_splits == []


async def test_snapshot_unresolved_symbol_returns_none():
    source = YFinanceMarketData(ticker_factory=EmptyTicker)
    assert await source.get_snapshot("NOPE") is None


async def test_fundamentals_picks_latest_period_and_derives_margins():
    fundamentals = await _source().get_fundamentals("AAPL")
    assert fundamentals is not None
    assert fundamentals.fiscal_period_end == date(2024, 9, 30)
    assert fundamentals.revenue == Decimal("391035000000")
    assert fundamentals.total_debt == Decimal("106629000000")
    assert fundamentals.free_cash_flow == Decimal("108807000000")
    assert fundamentals.gross_margin is not None
    assert Decimal("0.46") < fundamentals.gross_margin < Decimal("0.47")


async def test_fundamentals_derived_valuation_solvency_quality():
    f = await _source().get_fundamentals("AAPL")  # live read → market-cap metrics populate
    assert f is not None
    # Solvency (statement-only, no market cap needed).
    assert f.net_debt == Decimal("76686000000")  # 106.629B debt − 29.943B cash
    assert f.net_debt_to_fcf == Decimal("0.70")
    assert f.gross_profitability is not None  # 180.683B / 364.980B ≈ 0.495
    assert Decimal("0.49") < f.gross_profitability < Decimal("0.50")
    assert f.roic_pretax is not None and f.roic_pretax > Decimal("0.9")
    # Valuation (needs the current market cap from the live read).
    assert f.market_cap == Decimal("3500000000000")
    assert f.enterprise_value == Decimal("3576686000000")  # cap + net_debt
    assert f.ev_to_ebit is not None and Decimal("29") < f.ev_to_ebit < Decimal("30")
    assert f.fcf_yield is not None and Decimal("0.030") < f.fcf_yield < Decimal("0.032")


async def test_fundamentals_solvency_coverage_altman():
    f = await _source().get_fundamentals("AAPL")  # live read
    assert f is not None
    # Liquidity / leverage from the newly-read balance-sheet lines.
    assert Decimal("0.86") < f.current_ratio < Decimal("0.87")  # 152.987B / 176.392B
    assert f.working_capital == Decimal("-23405000000.00")  # current assets − current liabilities
    assert Decimal("1.8") < f.debt_to_equity < Decimal("1.9")  # 106.629B / 56.950B
    # EBITDA = EBIT + D&A, and the leverage/valuation built on it.
    assert f.ebitda == Decimal("134661000000.00")  # 123.216B + 11.445B
    assert f.net_debt_to_ebitda == Decimal("0.57")  # 76.686B / 134.661B
    assert f.ev_to_ebitda is not None and Decimal("26") < f.ev_to_ebitda < Decimal("27")
    assert f.interest_coverage == Decimal("31.33")  # 123.216B / 3.933B
    # Altman Z″ in the grey zone for these inputs (book-equity variant).
    assert f.altman_z is not None and Decimal("2.0") < f.altman_z < Decimal("3.0")


async def test_fundamentals_as_of_selects_older_period():
    fundamentals = await _source().get_fundamentals("AAPL", as_of=date(2023, 12, 31))
    assert fundamentals is not None
    assert fundamentals.fiscal_period_end == date(2023, 9, 30)
    assert fundamentals.revenue == Decimal("383285000000")
    # A point-in-time read pairs no current market cap with the old statement → cap metrics null.
    assert fundamentals.market_cap is None
    assert fundamentals.enterprise_value is None
    assert fundamentals.fcf_yield is None
    assert fundamentals.ev_to_ebit is None


async def test_dividends_streak_and_no_cut():
    history = await _source().get_dividends("AAPL")
    assert history is not None
    assert len(history.payments) == 4
    assert history.growth_streak_years == 3
    assert history.had_cut is False


def test_had_cut_ignores_incomplete_current_year():
    # Rising every complete year; the current (partial) year sums lower only because it has
    # fewer ex-dates so far — must NOT be flagged as a cut. (The Micron mid-year false positive.)
    annual = {
        2021: Decimal("0.40"),
        2022: Decimal("0.46"),
        2023: Decimal("0.46"),
        2024: Decimal("0.49"),
        2025: Decimal("0.49"),
        2026: Decimal("0.15"),  # partial year-to-date
    }
    assert _had_cut(annual, current_year=2026) is False


def test_had_cut_detects_real_adjacent_cut_between_complete_years():
    annual = {2022: Decimal("0.46"), 2023: Decimal("0.30"), 2024: Decimal("0.30")}
    assert _had_cut(annual, current_year=2026) is True


def test_had_cut_ignores_multi_year_gap():
    # Paid in the 1990s, suspended, resumed decades later — a gap is not a cut.
    annual = {1995: Decimal("0.20"), 2021: Decimal("0.10"), 2022: Decimal("0.12")}
    assert _had_cut(annual, current_year=2026) is False


def test_trailing_sum_survives_leap_day_reference():
    # reference = Feb 29: naive replace(year−1) raises ValueError (2027 has no Feb 29).
    payments = [
        DividendPayment(ex_date=date(2027, 3, 15), amount=Decimal("0.25")),
        DividendPayment(ex_date=date(2027, 11, 15), amount=Decimal("0.25")),
        DividendPayment(ex_date=date(2028, 2, 10), amount=Decimal("0.26")),
    ]
    total = _trailing_sum(payments, reference=date(2028, 2, 29))
    # Window (2027-02-28, 2028-02-29]: all three payments fall inside.
    assert total == Decimal("0.76")


async def test_dividends_as_of_filters_and_recomputes():
    history = await _source().get_dividends("AAPL", as_of=date(2022, 12, 31))
    assert history is not None
    assert len(history.payments) == 2
    assert history.trailing_12m == Decimal("0.23")
    # Only one complete prior year (2021) remains, so a streak can't be established.
    assert history.growth_streak_years is None


async def test_price_history_parses_ohlcv_bars():
    history = await _source().get_price_history("AAPL", "1mo", "1d")
    assert history is not None
    assert history.interval == "1d"
    assert len(history.bars) == 2
    last = history.bars[1]
    assert last.date == date(2024, 6, 11)
    assert last.close == Decimal("223.5")
    assert last.high == Decimal("224.0")
    assert last.volume == 1_100_000


async def test_price_history_as_of_truncates():
    history = await _source().get_price_history("AAPL", "1mo", "1d", as_of=date(2024, 6, 10))
    assert history is not None
    assert len(history.bars) == 1
    assert history.bars[0].date == date(2024, 6, 10)


async def test_news_parses_items():
    news = await _source().get_news("AAPL")
    assert news is not None
    assert len(news.items) == 1
    item = news.items[0]
    assert item.title == "Apple unveils new chip"
    assert item.publisher == "Reuters"
    assert "news.example/aapl-chip" in item.url
    assert item.published is not None and item.published.year == 2026


async def test_earnings_splits_future_and_past():
    info = await _source().get_earnings("AAPL", as_of=date(2026, 6, 25))
    assert info.next_earnings_date == date(2026, 8, 1)
    future = [e for e in info.events if e.is_future]
    past = [e for e in info.events if not e.is_future]
    assert any(e.event_date == date(2026, 8, 1) for e in future)
    assert any(e.eps_reported == Decimal("1.46") for e in past)
    # The one past print beat (surprise 4.3%) → full beat rate and a one-print streak.
    assert info.beat_rate == Decimal("1.0000")
    assert info.surprise_streak == 1
    assert info.avg_surprise == Decimal("4.3000")
    assert info.surprise_consistency is None  # needs ≥3 prints for a stdev


async def test_analyst_view_reports_consensus():
    view = await _source().get_analyst_view("AAPL")
    assert view is not None
    assert view.recommendation_key == "buy"
    assert view.number_of_analysts == 35
    assert view.target_mean == Decimal("290.0")
    assert view.target_high == Decimal("340.0")
    # Derived: (290 − 230)/230 ≈ 0.261 upside; (340 − 200)/290 ≈ 0.483 dispersion.
    assert view.upside_pct is not None and Decimal("0.26") < view.upside_pct < Decimal("0.27")
    assert (
        view.target_dispersion is not None
        and Decimal("0.48") < view.target_dispersion < Decimal("0.49")
    )


async def test_options_volatility_expected_move():
    calls = pd.DataFrame(
        {
            "strike": [240.0, 250.0, 260.0],
            "impliedVolatility": [0.40, 0.30, 0.42],
            "volume": [100, 200, 150],
            "openInterest": [1000, 2000, 1500],
        }
    )
    puts = pd.DataFrame(
        {
            "strike": [240.0, 250.0, 260.0],
            "impliedVolatility": [0.41, 0.32, 0.43],
            "volume": [300, 250, 200],
            "openInterest": [3000, 2500, 2000],
        }
    )

    far_calls = pd.DataFrame(
        {"strike": [240.0, 250.0, 260.0], "impliedVolatility": [0.44, 0.35, 0.46]}
    )
    far_puts = pd.DataFrame(
        {"strike": [240.0, 250.0, 260.0], "impliedVolatility": [0.45, 0.36, 0.47]}
    )

    class _Chain:
        def __init__(self, c, p):
            self.calls = c
            self.puts = p

    # The near expiry must lie in the FUTURE relative to the test run (a hardcoded date rots:
    # once past, days_to_expiry < 0 and expected_move silently degrades to None).
    near_expiry = date.today() + timedelta(days=7)

    class OptionsTicker:
        def __init__(self, symbol):
            self.info = {"currentPrice": 250.0}
            self.options = [near_expiry.isoformat(), "2099-08-21"]

        def option_chain(self, expiry):
            if expiry == "2099-08-21":
                return _Chain(far_calls, far_puts)
            return _Chain(calls, puts)

        def history(self, period=None, **kwargs):
            return pd.DataFrame({"Close": [250 + (i % 5 - 2) * 3 for i in range(40)]})

    source = YFinanceMarketData(ticker_factory=OptionsTicker)
    vol = await source.get_options_volatility("AAPL")
    assert vol is not None
    assert vol.expiry == near_expiry  # nearest expiry chosen
    assert vol.days_to_expiry == 7
    assert vol.atm_strike == Decimal("250.0")  # closest strike to 250 price
    assert vol.atm_iv == Decimal("0.31")  # avg of ATM call 0.30 and put 0.32
    assert vol.current_price == Decimal("250.0")
    assert vol.expected_move_percent is not None and vol.expected_move_percent > Decimal("0")
    assert vol.expected_move_low < Decimal("250") < vol.expected_move_high
    # Derived: OTM put (240, IV .41) vs OTM call (260, IV .42), over ATM .31.
    assert vol.iv_skew == Decimal("-0.0323")
    assert vol.put_call_ratio_volume == Decimal("1.6667")  # 750 / 450
    assert vol.put_call_ratio_oi == Decimal("1.6667")  # 7500 / 4500
    # VRP: ATM IV vs trailing realized vol from the 40-close history.
    assert vol.realized_vol is not None and vol.realized_vol > Decimal("0")
    assert vol.iv_rv_ratio is not None
    assert vol.volatility_risk_premium is not None
    # Term structure: far-expiry ATM IV (0.355) above the near ATM (0.31) → contango.
    assert vol.far_atm_iv == Decimal("0.3550")
    assert vol.iv_term_slope == Decimal("0.1452")  # (0.355 − 0.31) / 0.31
    assert vol.iv_term_structure == "contango"


async def test_options_volatility_none_without_chain():
    source = YFinanceMarketData(ticker_factory=FakeTicker)  # no options attribute
    assert await source.get_options_volatility("AAPL") is None


async def test_ownership_parses_holders_and_insiders():
    class OwnershipTicker:
        def __init__(self, symbol):
            self.major_holders = pd.DataFrame(
                {"Value": [0.0007, 0.62]},
                index=["insidersPercentHeld", "institutionsPercentHeld"],
            )
            self.institutional_holders = pd.DataFrame(
                {
                    "Holder": ["Vanguard", "BlackRock"],
                    "Shares": [1_000_000_000, 900_000_000],
                    "Value": [3.0e11, 2.7e11],
                    "pctHeld": [0.08, 0.07],
                }
            )
            self.insider_transactions = pd.DataFrame(
                {
                    "Insider": ["Tim Cook"],
                    "Position": ["CEO"],
                    "Transaction": ["Sale"],
                    "Shares": [50_000],
                    "Value": [1.2e7],
                    "Start Date": [pd.Timestamp("2026-05-10")],
                }
            )

    source = YFinanceMarketData(ticker_factory=OwnershipTicker)
    owner = await source.get_ownership("AAPL")
    assert owner is not None
    assert owner.insider_percent == Decimal("0.0007")
    assert owner.institution_percent == Decimal("0.62")
    assert owner.institutional_holders[0].holder == "Vanguard"
    assert owner.institutional_holders[0].pct_out == Decimal("0.08")
    assert owner.insider_transactions[0].insider == "Tim Cook"
    assert owner.insider_transactions[0].transaction_date == date(2026, 5, 10)


async def test_ownership_none_when_unavailable():
    assert await YFinanceMarketData(ticker_factory=FakeTicker).get_ownership("AAPL") is None


async def test_ownership_parses_short_interest_and_change():
    class ShortTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.major_holders = pd.DataFrame(
                {"Value": [0.0007, 0.62]},
                index=["insidersPercentHeld", "institutionsPercentHeld"],
            )
            self.info = {
                "sharesShort": 12_000_000,
                "sharesShortPriorMonth": 10_000_000,
                "shortRatio": 3.4,
                "shortPercentOfFloat": 0.085,
                "sharesPercentSharesOut": 0.08,
                "dateShortInterest": 1_718_000_000,  # epoch seconds -> a date
            }

    owner = await YFinanceMarketData(ticker_factory=ShortTicker).get_ownership("GME")
    assert owner is not None
    short = owner.short_interest
    assert short is not None
    assert short.shares_short == Decimal("12000000")
    assert short.shares_short_prior_month == Decimal("10000000")
    assert short.short_ratio_days_to_cover == Decimal("3.4")
    assert short.short_percent_of_float == Decimal("0.085")
    # (12M - 10M) / 10M * 100 = +20.0
    assert short.short_interest_change_pct == Decimal("20.0000")
    assert short.short_interest_date == date(2024, 6, 10)


async def test_ownership_short_interest_change_null_without_prior():
    class NoPriorShortTicker:
        def __init__(self, symbol):
            self.major_holders = pd.DataFrame(
                {"Value": [0.0007, 0.62]},
                index=["insidersPercentHeld", "institutionsPercentHeld"],
            )
            self.info = {"sharesShort": 12_000_000, "shortRatio": 3.4}

    owner = await YFinanceMarketData(ticker_factory=NoPriorShortTicker).get_ownership("GME")
    assert owner is not None
    assert owner.short_interest is not None
    assert owner.short_interest.shares_short == Decimal("12000000")
    assert owner.short_interest.short_interest_change_pct is None  # no prior month -> null


async def test_ownership_short_interest_none_when_info_absent():
    # The holder-only OwnershipTicker has no `.info` — short_interest degrades to None, the
    # ownership read still succeeds.
    class HolderOnlyTicker:
        def __init__(self, symbol):
            self.major_holders = pd.DataFrame(
                {"Value": [0.0007, 0.62]},
                index=["insidersPercentHeld", "institutionsPercentHeld"],
            )

    owner = await YFinanceMarketData(ticker_factory=HolderOnlyTicker).get_ownership("AAPL")
    assert owner is not None
    assert owner.short_interest is None


async def test_etf_holdings_parses_funds_data():
    class _FundsData:
        top_holdings = pd.DataFrame(
            {"Name": ["Apple", "Microsoft"], "Holding Percent": [0.071, 0.063]},
            index=["AAPL", "MSFT"],
        )
        sector_weightings = {"technology": 0.30, "financial_services": 0.13}

    class EtfTicker:
        def __init__(self, symbol):
            self.funds_data = _FundsData()

    source = YFinanceMarketData(ticker_factory=EtfTicker)
    holdings = await source.get_etf_holdings("XLK")
    assert holdings is not None
    assert holdings.top_holdings[0].symbol == "AAPL"
    assert holdings.top_holdings[0].weight == Decimal("0.071")
    assert holdings.sector_weights["technology"] == Decimal("0.3")


async def test_etf_holdings_none_for_non_fund():
    source = YFinanceMarketData(ticker_factory=FakeTicker)  # no funds_data attribute
    assert await source.get_etf_holdings("AAPL") is None


async def test_quality_metrics_ratios_and_cagr():
    # _INFO lacks the ratio keys, so add them via a custom ticker.
    class QualityTicker(FakeTicker):
        def __init__(self, symbol):
            super().__init__(symbol)
            self.info = {
                **_INFO,
                "returnOnEquity": 1.5,
                "returnOnAssets": 0.3,
                "grossMargins": 0.46,
                "profitMargins": 0.24,
                "revenueGrowth": 0.08,
            }

    source = YFinanceMarketData(ticker_factory=QualityTicker)
    metrics = await source.get_quality_metrics("AAPL")
    assert metrics is not None
    assert metrics.roe == Decimal("1.5")
    assert metrics.gross_margin == Decimal("0.46")
    assert metrics.revenue_growth_yoy == Decimal("0.08")
    # _INCOME has FY2024 revenue 391035 and FY2023 383285 → 1-period CAGR ~2.02%.
    assert metrics.cagr_years == 1
    assert metrics.revenue_cagr is not None
    assert Decimal("0.01") < metrics.revenue_cagr < Decimal("0.03")


async def test_cagr_uses_real_year_span_when_a_fiscal_year_is_missing():
    # FY2021 → FY2024 with FY2022/FY2023 absent: 100 → 121 over THREE years is ~6.6%/yr.
    # Counting intervals (len−1 = 1) would report 21%/yr — over-annualized 3×.
    class GappyIncomeTicker(FakeTicker):
        def __init__(self, symbol):
            super().__init__(symbol)
            self.income_stmt = pd.DataFrame(
                {
                    pd.Timestamp("2024-09-30"): {"Total Revenue": 121_000_000_000.0},
                    pd.Timestamp("2021-09-30"): {"Total Revenue": 100_000_000_000.0},
                }
            )

    source = YFinanceMarketData(ticker_factory=GappyIncomeTicker)
    metrics = await source.get_quality_metrics("AAPL")
    assert metrics is not None
    assert metrics.cagr_years == 3  # real span between the endpoints, not column count − 1
    assert metrics.revenue_cagr is not None
    assert Decimal("0.06") < metrics.revenue_cagr < Decimal("0.07")


async def test_market_movers_parses_screen():
    screen_result = {
        "quotes": [
            {
                "symbol": "ABCD",
                "shortName": "Abcd Inc",
                "regularMarketPrice": 12.5,
                "regularMarketChangePercent": 18.4,
                "regularMarketVolume": 5_000_000,
            },
            {"no_symbol": True},
        ]
    }
    source = YFinanceMarketData(screen_fn=lambda key: screen_result)
    result = await source.get_movers("gainers", 10)
    assert result.category == "gainers"
    assert len(result.movers) == 1
    assert result.movers[0].symbol == "ABCD"
    assert result.movers[0].change_percent == Decimal("18.4")
    assert result.movers[0].volume == 5_000_000

    with pytest.raises(ValueError):
        await source.get_movers("nonsense")


async def test_search_symbols_parses_quotes():
    quotes = [
        {"symbol": "AAPL", "shortname": "Apple Inc.", "exchange": "NMS", "quoteType": "EQUITY"},
        {"symbol": "APLE", "longname": "Apple Hospitality REIT", "quoteType": "EQUITY"},
        {"no_symbol": True},  # malformed entry should be skipped
    ]
    source = YFinanceMarketData(search_fn=lambda q: quotes)
    result = await source.search_symbols("apple", limit=10)
    assert result.query == "apple"
    assert len(result.matches) == 2
    assert result.matches[0].symbol == "AAPL"
    assert result.matches[0].name == "Apple Inc."
    assert result.matches[1].name == "Apple Hospitality REIT"


async def test_retry_recovers_from_transient_failure():
    FlakyTicker.accesses["n"] = 0
    FlakyTicker.fail_times = 2
    source = YFinanceMarketData(ticker_factory=FlakyTicker, retry_attempts=3, retry_base_delay=0)
    snapshot = await source.get_snapshot("AAPL")
    assert snapshot is not None
    assert snapshot.price == Decimal("230.0")
    assert FlakyTicker.accesses["n"] == 3  # failed twice, succeeded on the third


async def test_retry_gives_up_and_raises_source_unavailable_after_exhaustion():
    from scout.adapters.retry import SourceUnavailable

    FlakyTicker.accesses["n"] = 0
    FlakyTicker.fail_times = 99
    source = YFinanceMarketData(ticker_factory=FlakyTicker, retry_attempts=2, retry_base_delay=0)
    with pytest.raises(SourceUnavailable) as exc_info:
        await source.get_snapshot("AAPL")
    assert exc_info.value.reason == "rate_limited"
    assert FlakyTicker.accesses["n"] == 2  # exactly retry_attempts, no more


async def test_retry_reraises_non_transient_immediately():
    """A genuine (non-429/timeout) error must not trigger a retry storm — re-raised at once."""
    BrokenTicker.accesses["n"] = 0
    source = YFinanceMarketData(ticker_factory=BrokenTicker, retry_attempts=3, retry_base_delay=0)
    with pytest.raises(ValueError, match="genuine parse bug"):
        await source.get_snapshot("AAPL")
    assert BrokenTicker.accesses["n"] == 1  # not retried
