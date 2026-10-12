from datetime import date, datetime, timedelta
from decimal import Decimal

from scout.domain.models import (
    CompanySnapshot,
    DividendHistory,
    EarningsInfo,
    Fundamentals,
    NewsItem,
    NewsList,
    Period,
    PriceBar,
    PriceHistory,
)
from scout.research import (
    build_calendar,
    build_classification,
    build_comparison,
    build_correlation,
    build_news_digest,
    build_relative_strength,
    build_sector_performance,
)


class FakeSource:
    """A market-data source backed by canned per-symbol data."""

    def __init__(
        self,
        snapshots=None,
        fundamentals=None,
        closes=None,
        news=None,
        earnings=None,
        dividends=None,
    ):
        self._snapshots = snapshots or {}
        self._fundamentals = fundamentals or {}
        self._closes = closes or {}
        self._news = news or {}
        self._earnings = earnings or {}
        self._dividends = dividends or {}

    async def get_snapshot(self, symbol, as_of=None):
        return self._snapshots.get(symbol)

    async def get_fundamentals(self, symbol, period=Period.ANNUAL, as_of=None):
        return self._fundamentals.get(symbol)

    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        closes = self._closes.get(symbol)
        if closes is None:
            return None
        base = date(2024, 1, 1)
        bars = [
            PriceBar(date=base + timedelta(days=i), close=Decimal(str(c)))
            for i, c in enumerate(closes)
        ]
        return PriceHistory(symbol=symbol, interval=interval, bars=bars, as_of=as_of)

    async def get_news(self, symbol, limit=10):
        return self._news.get(symbol, NewsList(symbol=symbol))

    async def get_earnings(self, symbol, as_of=None):
        return self._earnings.get(symbol, EarningsInfo(symbol=symbol))

    async def get_dividends(self, symbol, as_of=None):
        return self._dividends.get(symbol, DividendHistory(symbol=symbol))


async def test_comparison_builds_rows_in_parallel():
    source = FakeSource(
        snapshots={
            "AAPL": CompanySnapshot(
                symbol="AAPL", name="Apple", price=Decimal("230"), sector="Tech"
            ),
            "MSFT": CompanySnapshot(symbol="MSFT", name="Microsoft", price=Decimal("350")),
        },
        fundamentals={
            "AAPL": Fundamentals(symbol="AAPL", period=Period.ANNUAL, revenue=Decimal("391")),
        },
    )
    result = await build_comparison(source, ["aapl", "msft"])
    assert result.symbols == ["AAPL", "MSFT"]
    by_symbol = {r.symbol: r for r in result.rows}
    assert by_symbol["AAPL"].price == Decimal("230")
    assert by_symbol["AAPL"].revenue == Decimal("391")
    assert by_symbol["MSFT"].revenue is None  # no fundamentals provided


async def test_comparison_notes_missing_symbol():
    result = await build_comparison(FakeSource(), ["nope"])
    assert result.rows[0].note == "not_found"  # genuine None, not a transient failure


async def test_correlation_matrix_perfect_and_diagonal():
    source = FakeSource(
        closes={
            "AAA": [10, 11, 12, 13, 14],
            "BBB": [20, 22, 24, 26, 28],  # same returns as AAA → correlation 1.0
        }
    )
    result = await build_correlation(source, ["AAA", "BBB"])
    assert result.days == 5
    assert result.matrix["AAA"]["AAA"] == 1.0
    assert result.matrix["AAA"]["BBB"] == 1.0
    assert result.matrix["BBB"]["AAA"] == 1.0


async def test_correlation_needs_two_symbols():
    result = await build_correlation(FakeSource(closes={"AAA": [1, 2, 3]}), ["AAA", "ZZZ"])
    assert result.matrix == {}
    assert any("at least two" in note.lower() for note in result.notes)


async def test_relative_strength_vs_benchmark_sorted():
    source = FakeSource(
        closes={
            "SPY": [100, 105],  # benchmark +5%
            "AAA": [100, 120],  # +20% → excess +15
            "BBB": [100, 102],  # +2%  → excess -3
        }
    )
    result = await build_relative_strength(source, ["AAA", "BBB"], benchmark="SPY", period="3mo")
    assert result.benchmark_return_percent == Decimal("5.00")
    # Sorted strongest-excess first.
    assert [r.symbol for r in result.rows] == ["AAA", "BBB"]
    assert result.rows[0].return_percent == Decimal("20.00")
    assert result.rows[0].excess_vs_benchmark == Decimal("15.00")
    assert result.rows[1].excess_vs_benchmark == Decimal("-3.00")


async def test_relative_strength_annotates_effective_window_start():
    # BBB has less history than the requested period (a recent IPO): its series ENDS with the
    # others but STARTS later, so its return covers a shorter window. The effective start date
    # per symbol (and for the benchmark) is annotated so the mismatch is visible instead of the
    # rows silently posing as period-comparable.
    base = date(2024, 1, 1)

    class IpoAwareSource(FakeSource):
        async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
            closes = self._closes.get(symbol)
            if closes is None:
                return None
            offset = 5 - len(closes)  # shorter series start later, all end on the same day
            bars = [
                PriceBar(date=base + timedelta(days=offset + i), close=Decimal(str(c)))
                for i, c in enumerate(closes)
            ]
            return PriceHistory(symbol=symbol, interval=interval, bars=bars, as_of=as_of)

    source = IpoAwareSource(
        closes={
            "SPY": [100, 101, 102, 103, 105],
            "AAA": [100, 104, 108, 112, 120],
            "BBB": [100, 102],  # 2 bars only — the "IPO"
        }
    )
    result = await build_relative_strength(source, ["AAA", "BBB"], benchmark="SPY")
    assert result.benchmark_window_start == base
    by_symbol = {r.symbol: r for r in result.rows}
    assert by_symbol["AAA"].window_start == base
    assert by_symbol["BBB"].window_start == base + timedelta(days=3)  # starts later than SPY


async def test_sector_performance_sorts_and_notes_missing():
    source = FakeSource(closes={"XLK": [100, 110], "XLE": [100, 95]})
    result = await build_sector_performance(source, period="3mo")
    # Only XLK and XLE have data; the rest land in notes.
    ranked = [(s.sector, s.return_percent) for s in result.sectors]
    assert ranked[0] == ("Technology", Decimal("10.00"))
    assert ("Energy", Decimal("-5.00")) in ranked
    assert any("XLV" in n for n in result.notes)


async def test_classification_in_batch():
    source = FakeSource(
        snapshots={
            "AAPL": CompanySnapshot(symbol="AAPL", sector="Tech", market_cap=Decimal("3e12")),
        }
    )
    result = await build_classification(source, ["aapl", "zzz"])
    by_symbol = {i.symbol: i for i in result.items}
    assert by_symbol["AAPL"].sector == "Tech"
    assert by_symbol["ZZZ"].note == "not_found"  # genuine None → not a fetch failure


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _RateLimited(Exception):
    def __init__(self):
        self.response = _Resp(429)
        super().__init__("HTTP 429")


class _ThrottledSource(FakeSource):
    async def get_snapshot(self, symbol, as_of=None):
        raise _RateLimited()


async def test_classification_distinguishes_rate_limit_from_not_found():
    """A 429 must read as 'unavailable: rate_limited' (retry/abstain), NOT as not_found."""
    result = await build_classification(_ThrottledSource(), ["aaa"])
    assert result.items[0].note == "unavailable: rate_limited"


async def test_news_digest_merges_and_sorts():
    source = FakeSource(
        news={
            "AAPL": NewsList(
                symbol="AAPL",
                items=[NewsItem(title="older", published=datetime(2026, 6, 1))],
            ),
            "MSFT": NewsList(
                symbol="MSFT",
                items=[NewsItem(title="newer", published=datetime(2026, 6, 20))],
            ),
        }
    )
    result = await build_news_digest(source, ["AAPL", "MSFT"])
    assert [i.title for i in result.items] == ["newer", "older"]  # newest first
    assert result.items[0].symbol == "MSFT"


async def test_calendar_collects_future_events_sorted():
    source = FakeSource(
        earnings={"AAPL": EarningsInfo(symbol="AAPL", next_earnings_date=date(2026, 8, 1))},
        dividends={"KO": DividendHistory(symbol="KO", next_ex_dividend=date(2026, 7, 10))},
    )
    result = await build_calendar(source, ["AAPL", "KO"], as_of=date(2026, 6, 25))
    assert [(e.symbol, e.type, e.date) for e in result.events] == [
        ("KO", "ex_dividend", date(2026, 7, 10)),
        ("AAPL", "earnings", date(2026, 8, 1)),
    ]


async def test_calendar_drops_past_events():
    source = FakeSource(
        earnings={"AAPL": EarningsInfo(symbol="AAPL", next_earnings_date=date(2026, 5, 1))},
    )
    result = await build_calendar(source, ["AAPL"], as_of=date(2026, 6, 25))
    assert result.events == []


class _BlockedHistorySource(FakeSource):
    """Price history for listed symbols raises the fallback's 'blocked' unavailability."""

    def __init__(self, blocked, **kwargs):
        super().__init__(**kwargs)
        self._blocked = set(blocked)

    async def get_price_history(self, symbol, range="6mo", interval="1d", as_of=None):
        if symbol in self._blocked:
            from scout.adapters.retry import BLOCKED, SourceUnavailable

            raise SourceUnavailable(BLOCKED, "stooq returned a JavaScript bot challenge")
        return await super().get_price_history(symbol, range, interval, as_of)


async def test_correlation_notes_tag_unavailable_vs_not_found():
    source = _BlockedHistorySource(
        blocked={"BBB"}, closes={"AAA": [1, 2, 3, 4], "CCC": [2, 3, 4, 5]}
    )
    result = await build_correlation(source, ["AAA", "BBB", "CCC", "ZZZ"])
    assert "BBB: price history unavailable: blocked" in result.notes  # couldn't fetch
    assert "ZZZ: price history not_found" in result.notes  # genuinely no data


async def test_sector_performance_notes_tag_the_reason():
    source = _BlockedHistorySource(blocked={"XLK"}, closes={"XLE": [100, 95]})
    result = await build_sector_performance(source, period="3mo")
    assert "Technology (XLK): unavailable: blocked" in result.notes
    assert "Health Care (XLV): not_found" in result.notes


async def test_relative_strength_rows_and_benchmark_tag_the_reason():
    source = _BlockedHistorySource(blocked={"SPY", "BBB"}, closes={"AAA": [100, 110]})
    result = await build_relative_strength(source, ["AAA", "BBB", "ZZZ"], benchmark="SPY")
    assert any("SPY price history unavailable: blocked" in n for n in result.notes)
    by_symbol = {r.symbol: r for r in result.rows}
    assert by_symbol["BBB"].note == "price history unavailable: blocked"
    assert by_symbol["ZZZ"].note == "price history not_found"
