"""``correlation_matrix`` — pairwise return correlation across symbols.

Fetches each symbol's price history in parallel, aligns them on their common trading days,
turns prices into returns and computes Pearson correlation for every pair. This is the kind
of cross-symbol view a per-symbol loop can't reconstruct — it only exists in aggregate.
"""

from __future__ import annotations

import asyncio
from datetime import date

from ..analytics import pct_returns, pearson
from ..domain.models import CorrelationMatrix
from ..domain.ports import MarketDataSource
from .notes import unavailable_or_not_found


async def build_correlation(
    source: MarketDataSource,
    symbols: list[str],
    period: str = "6mo",
    as_of: date | None = None,
) -> CorrelationMatrix:
    clean = [s.strip().upper() for s in symbols if s.strip()]
    histories = await asyncio.gather(
        *(source.get_price_history(s, period, "1d", as_of) for s in clean),
        return_exceptions=True,
    )

    notes: list[str] = []
    closes_by_date: dict[str, dict[date, float]] = {}
    for symbol, history in zip(clean, histories, strict=False):
        if isinstance(history, Exception) or history is None or not history.bars:
            notes.append(f"{symbol}: price history {unavailable_or_not_found(history)}")
            continue
        closes_by_date[symbol] = {
            b.date: float(b.close) for b in history.bars if b.close is not None
        }

    valid = [s for s in clean if s in closes_by_date]
    if len(valid) < 2:
        notes.append("Need at least two symbols with price data to correlate.")
        return CorrelationMatrix(symbols=clean, period=period, notes=notes)

    common_dates = sorted(set.intersection(*(set(closes_by_date[s]) for s in valid)))
    if len(common_dates) < 3:
        notes.append("Too few overlapping trading days to compute a stable correlation.")
        return CorrelationMatrix(symbols=clean, period=period, days=len(common_dates), notes=notes)

    returns = {s: pct_returns([closes_by_date[s][d] for d in common_dates]) for s in valid}
    matrix: dict[str, dict[str, float | None]] = {}
    for row_symbol in valid:
        matrix[row_symbol] = {}
        for col_symbol in valid:
            if row_symbol == col_symbol:
                matrix[row_symbol][col_symbol] = 1.0
                continue
            value = pearson(returns[row_symbol], returns[col_symbol])
            matrix[row_symbol][col_symbol] = round(value, 3) if value is not None else None

    return CorrelationMatrix(
        symbols=clean, period=period, days=len(common_dates), matrix=matrix, notes=notes
    )
