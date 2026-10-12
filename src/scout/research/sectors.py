"""``sector_performance`` — total return of each US sector over a period.

Uses the 11 SPDR sector ETFs as the sector proxy and computes each one's total return,
sorted best to worst — sector rotation as plain price data, no leadership verdict.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from ..domain.models import SectorPerformance, SectorReturn
from ..domain.ports import MarketDataSource
from .notes import unavailable_or_not_found
from .relative_strength import _total_return_pct

# Sector → SPDR ETF (the standard 11 GICS sectors).
_SECTOR_ETFS: tuple[tuple[str, str], ...] = (
    ("Technology", "XLK"),
    ("Financials", "XLF"),
    ("Health Care", "XLV"),
    ("Consumer Discretionary", "XLY"),
    ("Consumer Staples", "XLP"),
    ("Energy", "XLE"),
    ("Industrials", "XLI"),
    ("Materials", "XLB"),
    ("Utilities", "XLU"),
    ("Real Estate", "XLRE"),
    ("Communication Services", "XLC"),
)


async def build_sector_performance(
    source: MarketDataSource, period: str = "3mo", as_of: date | None = None
) -> SectorPerformance:
    histories = await asyncio.gather(
        *(source.get_price_history(etf, period, "1d", as_of) for _, etf in _SECTOR_ETFS),
        return_exceptions=True,
    )
    sectors: list[SectorReturn] = []
    notes: list[str] = []
    for (sector, etf), history in zip(_SECTOR_ETFS, histories, strict=False):
        if isinstance(history, Exception) or history is None:
            notes.append(f"{sector} ({etf}): {unavailable_or_not_found(history)}")
            continue
        sectors.append(
            SectorReturn(sector=sector, etf=etf, return_percent=_total_return_pct(history))
        )
    sectors.sort(
        key=lambda s: s.return_percent if s.return_percent is not None else Decimal("-1e18"),
        reverse=True,
    )
    return SectorPerformance(period=period, sectors=sectors, notes=notes)
