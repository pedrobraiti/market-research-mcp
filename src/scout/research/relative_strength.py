"""``relative_strength`` — each symbol's total return over a period vs a benchmark.

Pure aggregation: fetch the symbols and the benchmark in parallel, turn each into a total
return, and report the excess vs the benchmark. Leadership/laggard as a number — the agent
decides what "strong" means.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ..domain.models import RelativeStrength, RelativeStrengthRow
from ..domain.ports import MarketDataSource


def _total_return_pct(history) -> Decimal | None:
    if history is None or not history.bars:
        return None
    closes = [b.close for b in history.bars if b.close is not None]
    if len(closes) < 2 or closes[0] == 0:
        return None
    pct = (closes[-1] - closes[0]) / closes[0] * Decimal(100)
    return pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _window_start(history) -> date | None:
    """First priced bar actually used — the EFFECTIVE window start. A short-history symbol
    (recent IPO) covers less than the requested period; annotating the start date keeps its
    return from silently posing as period-comparable to the benchmark's."""
    if history is None:
        return None
    for bar in history.bars:
        if bar.close is not None:
            return bar.date
    return None


async def build_relative_strength(
    source: MarketDataSource,
    symbols: list[str],
    benchmark: str = "SPY",
    period: str = "3mo",
    as_of: date | None = None,
) -> RelativeStrength:
    clean = [s.strip().upper() for s in symbols if s.strip()]
    benchmark = benchmark.strip().upper()
    histories = await asyncio.gather(
        source.get_price_history(benchmark, period, "1d", as_of),
        *(source.get_price_history(s, period, "1d", as_of) for s in clean),
        return_exceptions=True,
    )
    benchmark_history = histories[0] if not isinstance(histories[0], Exception) else None
    benchmark_return = _total_return_pct(benchmark_history)

    rows: list[RelativeStrengthRow] = []
    notes: list[str] = []
    if benchmark_return is None:
        notes.append(f"Benchmark {benchmark} return unavailable — excess can't be computed.")
    for symbol, history in zip(clean, histories[1:], strict=False):
        if isinstance(history, Exception) or history is None:
            rows.append(RelativeStrengthRow(symbol=symbol, note="price history unavailable"))
            continue
        symbol_return = _total_return_pct(history)
        excess = (
            symbol_return - benchmark_return
            if symbol_return is not None and benchmark_return is not None
            else None
        )
        rows.append(
            RelativeStrengthRow(
                symbol=symbol,
                return_percent=symbol_return,
                excess_vs_benchmark=excess,
                window_start=_window_start(history),
            )
        )
    rows.sort(key=lambda r: r.excess_vs_benchmark or Decimal("-1e18"), reverse=True)
    return RelativeStrength(
        benchmark=benchmark, period=period, benchmark_return_percent=benchmark_return,
        benchmark_window_start=_window_start(benchmark_history),
        rows=rows, notes=notes,
    )
