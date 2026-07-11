"""Engle-Granger cointegration / pairs analyzer — pure stats over Scout's existing price path.

Adds NO new HTTP client and NO heavy dependency (no scipy/statsmodels): it takes an injected
``fetch_history(symbol) -> PriceHistory | None`` (the yfinance ``MarketDataSource`` in the
composition root) and runs the Engle-Granger two-step in pure Python (``scout.analytics``).

Step 1 — OLS hedge ratio (regress A on B). Step 2 — a Dickey-Fuller test on the residual spread.
We report the DF t-statistic against MacKinnon's asymptotic Engle-Granger critical values rather
than a fake-precise p-value (honesty over filling, ADR-004/ADR-012). The two close series are
aligned by their date intersection and sliced to the requested ``lookback_days`` so a stale or
missing day in one leg never silently mismatches the other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ...analytics import (
    EG_CRITICAL_VALUES,
    dickey_fuller_tstat,
    mean_reversion_half_life,
    ols_with_intercept,
    pct_returns,
    pearson,
    zscore_of_last,
)
from ...domain.models import (
    CointegratedPairs,
    Cointegration,
    PairCandidate,
    PriceHistory,
)
from ..retry import unavailable_status

_MIN_OBS = 30  # below this the pair test is too short to mean anything
_DEFAULT_LOOKBACK = 252
_MAX_UNIVERSE = 40  # cap the basket: every symbol is one yfinance fetch (429 risk)
FetchHistory = Callable[[str], Awaitable[PriceHistory | None]]


def _quantize(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def _closes_by_date(history: PriceHistory | None) -> dict[date, float]:
    if history is None:
        return {}
    out: dict[date, float] = {}
    for bar in history.bars:
        if bar.close is not None:
            close = float(bar.close)
            if close > 0:
                out[bar.date] = close
    return out


def _pair_stats(series_a: list[float], series_b: list[float]) -> dict | None:
    """The Engle-Granger per-pair math shared by the single test and the screen: OLS hedge ratio,
    residual spread, then ADF stat / half-life / latest z-score on the spread. None on a degenerate
    fit (symbol_b has no variance)."""
    fit = ols_with_intercept(series_a, series_b)
    if fit is None:
        return None
    beta, intercept = fit
    spread = [series_a[i] - (intercept + beta * series_b[i]) for i in range(len(series_a))]
    return {
        "beta": beta,
        "spread": spread,
        "adf": dickey_fuller_tstat(spread),
        "half_life": mean_reversion_half_life(spread),
        "zscore": zscore_of_last(spread),
    }


class CointegrationAnalyzer:
    def __init__(self, fetch_history: FetchHistory) -> None:
        self._fetch_history = fetch_history

    async def _leg(self, symbol: str) -> tuple[dict[date, float], str | None, str | None]:
        """Fetch one symbol → (closes by date, unavailability status, price source).
        The source (``PriceHistory.source``, stamped by the fallback wrapper) rides along so the
        result's provenance note can say which upstream actually served each leg."""
        try:
            history = await self._fetch_history(symbol)
        except Exception as exc:  # noqa: BLE001 — a fetch error is honest unavailability, not data
            return {}, unavailable_status(exc), None
        source = history.source if history is not None else None
        return _closes_by_date(history), None, source

    async def get_cointegration(
        self, symbol_a: str, symbol_b: str, lookback_days: int = _DEFAULT_LOOKBACK
    ) -> Cointegration:
        lookback = max(_MIN_OBS, lookback_days)
        closes_a, status_a, source_a = await self._leg(symbol_a)
        closes_b, status_b, source_b = await self._leg(symbol_b)

        statuses = [
            f"{label} {status}"
            for label, status in (("symbol_a", status_a), ("symbol_b", status_b))
            if status
        ]
        source_status = "; ".join(statuses) if statuses else None

        common = sorted(set(closes_a) & set(closes_b))[-lookback:]
        result = Cointegration(
            symbol_a=symbol_a.upper(),
            symbol_b=symbol_b.upper(),
            lookback_days=lookback_days,
            n_obs=len(common),
            adf_crit_1pct=_quantize(EG_CRITICAL_VALUES["1pct"], 2),
            adf_crit_5pct=_quantize(EG_CRITICAL_VALUES["5pct"], 2),
            adf_crit_10pct=_quantize(EG_CRITICAL_VALUES["10pct"], 2),
            as_of=common[-1] if common else None,
            source_status=source_status,
        )

        if len(common) < _MIN_OBS:
            result.note = (
                f"Insufficient overlapping daily closes ({len(common)} < {_MIN_OBS}) to test the "
                "pair — widen the lookback or check the symbols."
            )
            return result

        series_a = [closes_a[day] for day in common]
        series_b = [closes_b[day] for day in common]
        stats = _pair_stats(series_a, series_b)
        if stats is None:
            result.note = "Degenerate hedge-ratio fit (symbol_b has no variance over the window)."
            return result

        adf = stats["adf"]
        result.hedge_ratio_beta = _quantize(stats["beta"], 6)
        result.spread_latest = _quantize(stats["spread"][-1], 6)
        result.spread_zscore = _quantize(stats["zscore"], 4)
        result.adf_stat = _quantize(adf, 4)
        result.is_cointegrated = adf is not None and adf < EG_CRITICAL_VALUES["5pct"]
        result.half_life_days = _quantize(stats["half_life"], 2)
        floor = (
            f"Requested lookback_days={lookback_days} floored to the {_MIN_OBS}-obs minimum "
            f"(used {len(common)} overlapping closes). "
            if lookback_days < _MIN_OBS
            else ""
        )
        # Provenance must reflect what actually happened: the price path falls back to stooq
        # when yfinance is unavailable, and stooq closes are a different adjusted series.
        if "stooq" in {source_a, source_b}:
            source_note = (
                f"Price sources: {result.symbol_a}={source_a or 'yfinance'}, "
                f"{result.symbol_b}={source_b or 'yfinance'} — at least one leg came from the "
                "stooq fallback, not yfinance's split/div-adjusted closes."
            )
        else:
            source_note = "Uses yfinance split/div-adjusted closes."
        result.note = (
            f"{floor}Engle-Granger two-step: OLS hedge ratio then a non-augmented (0-lag) "
            "Dickey-Fuller test on the residual; adf_stat is judged against MacKinnon asymptotic "
            f"critical values (constant, no trend, one regressor). {source_note}"
        )
        return result

    async def find_pairs(
        self,
        symbols: list[str],
        lookback_days: int = _DEFAULT_LOOKBACK,
        min_correlation: float = 0.5,
    ) -> CointegratedPairs:
        lookback = max(_MIN_OBS, lookback_days)
        universe: list[str] = []
        for symbol in symbols:  # strip, dedup (case-insensitive), preserve order, clamp to the cap
            upper = symbol.strip().upper()
            if upper and upper not in universe:
                universe.append(upper)
        clamped = universe[:_MAX_UNIVERSE]

        closes: dict[str, dict[date, float]] = {}
        dropped: list[str] = []
        for symbol in clamped:
            series, status, _source = await self._leg(symbol)
            if status or not series:
                dropped.append(f"{symbol} {status}" if status else f"{symbol} (no data)")
            else:
                closes[symbol] = series

        result = CointegratedPairs(
            symbols=list(closes.keys()),
            lookback_days=lookback_days,
            min_correlation=_quantize(min_correlation, 4),
            adf_crit_5pct=_quantize(EG_CRITICAL_VALUES["5pct"], 2),
            adf_crit_1pct=_quantize(EG_CRITICAL_VALUES["1pct"], 2),
            source_status="; ".join(dropped) if dropped else None,
        )

        fetched = list(closes.keys())
        tested = 0
        candidates: list[tuple[float, PairCandidate]] = []
        latest_day: date | None = None
        for i in range(len(fetched)):
            for j in range(i + 1, len(fetched)):
                sym_a, sym_b = fetched[i], fetched[j]
                common = sorted(set(closes[sym_a]) & set(closes[sym_b]))[-lookback:]
                if len(common) < _MIN_OBS:
                    continue
                series_a = [closes[sym_a][day] for day in common]
                series_b = [closes[sym_b][day] for day in common]
                corr = pearson(pct_returns(series_a), pct_returns(series_b))
                if corr is None or abs(corr) < min_correlation:
                    continue  # correlation pre-filter — skip implausible pairs, cut the test count
                tested += 1
                stats = _pair_stats(series_a, series_b)
                if stats is None or stats["adf"] is None:
                    continue
                if common[-1] is not None and (latest_day is None or common[-1] > latest_day):
                    latest_day = common[-1]
                if stats["adf"] < EG_CRITICAL_VALUES["5pct"]:
                    candidates.append(
                        (
                            stats["adf"],
                            PairCandidate(
                                symbol_a=sym_a,
                                symbol_b=sym_b,
                                correlation=_quantize(corr, 4),
                                hedge_ratio_beta=_quantize(stats["beta"], 6),
                                adf_stat=_quantize(stats["adf"], 4),
                                is_cointegrated=True,
                                spread_zscore=_quantize(stats["zscore"], 4),
                                half_life_days=_quantize(stats["half_life"], 2),
                                n_obs=len(common),
                            ),
                        )
                    )

        candidates.sort(key=lambda pair: pair[0])  # most negative adf_stat first (strongest)
        result.pairs = [candidate for _, candidate in candidates]
        result.pairs_tested = tested
        result.as_of = latest_day

        notes: list[str] = [
            f"Screened {len(fetched)} symbols, {tested} correlated pairs ADF-tested, "
            f"{len(result.pairs)} cointegrated at 5%.",
            "Multiple testing: with that many tests expect ~0.05·pairs_tested false positives at "
            "5% — trust the most negative adf_stat / the 1%-level hits, not marginal ones.",
        ]
        if len(universe) > _MAX_UNIVERSE:
            notes.append(
                f"Universe clamped to the first {_MAX_UNIVERSE} of {len(universe)} symbols "
                "(each is one fetch)."
            )
        result.note = " ".join(notes)
        return result
