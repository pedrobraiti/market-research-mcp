"""Price-history fallback decorator.

Wraps a primary ``MarketDataSource`` and, for ``get_price_history`` only, falls back to a second
source when the primary fails or returns nothing. Everything else is delegated to the primary
unchanged. This is what makes the scraper-based yfinance resilient: a transient failure or empty
result transparently retries against stooq.

Two honesty rules live here:

- Every returned history is stamped with ``source`` (which upstream actually served the bars),
  so downstream provenance claims (e.g. "yfinance split/div-adjusted closes") stay truthful.
- If the primary raised ``SourceUnavailable`` (429/timeout after retries) and the fallback could
  not deliver bars either, that exception is RE-RAISED — a rate-limited read must never collapse
  into a ``None`` that reads as "this symbol has no data".
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..domain.models import PriceHistory
from .retry import SourceUnavailable


class PriceFallbackMarketData:
    def __init__(
        self,
        primary: Any,
        fallback: Any,
        primary_name: str = "yfinance",
        fallback_name: str = "stooq",
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name

    def __getattr__(self, name: str) -> Any:
        # Everything not defined here (get_snapshot, get_news, ...) goes straight to the primary.
        return getattr(self._primary, name)

    async def get_price_history(
        self, symbol: str, range: str = "6mo", interval: str = "1d", as_of: date | None = None
    ) -> PriceHistory | None:
        primary_result: PriceHistory | None = None
        primary_error: Exception | None = None
        try:
            primary_result = await self._primary.get_price_history(symbol, range, interval, as_of)
        except Exception as exc:  # noqa: BLE001 — try the fallback before deciding anything
            primary_error = exc
        if primary_result is not None and primary_result.bars:
            return self._stamped(primary_result, self._primary_name)

        fallback_result: PriceHistory | None = None
        try:
            fallback_result = await self._fallback.get_price_history(symbol, range, interval, as_of)
        except Exception:  # noqa: BLE001 — the fallback failing must not mask the primary's story
            fallback_result = None
        if fallback_result is not None and fallback_result.bars:
            return self._stamped(fallback_result, self._fallback_name)

        # No bars from either side. If the primary was UNAVAILABLE (rate-limited/timed out),
        # surface that — "couldn't fetch" and "fetched, symbol has no data" must stay distinct.
        if isinstance(primary_error, SourceUnavailable):
            raise primary_error
        if fallback_result is not None:
            return self._stamped(fallback_result, self._fallback_name)
        return self._stamped(primary_result, self._primary_name)

    def _stamped(self, history: PriceHistory | None, source: str) -> PriceHistory | None:
        if history is not None:
            history.source = source
        return history
