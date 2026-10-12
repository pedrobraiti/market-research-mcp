"""Price-history fallback decorator.

Wraps a primary ``MarketDataSource`` and, for ``get_price_history`` only, falls back to a second
source when the primary fails or returns nothing. Everything else is delegated to the primary
unchanged. This is what makes the scraper-based yfinance resilient: a transient failure or empty
result transparently retries against stooq.

Two honesty rules live here:

- Every returned history is stamped with ``source`` (which upstream actually served the bars),
  so downstream provenance claims (e.g. "yfinance split/div-adjusted closes") stay truthful.
- If the primary FAILED (any exception — a 429 after retries, a schema change, a crash) and the
  fallback could not deliver bars either, a ``PriceHistoryUnavailable`` is raised carrying BOTH
  causes — the primary's first, since it is the real story. A failed read must never collapse into
  a ``None`` that reads as "this symbol has no data" (and so into an ``ok: true`` envelope).
  Only a primary that genuinely ANSWERED "no bars" may end in ``None``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..domain.models import PriceHistory
from .retry import ERROR, SourceUnavailable, classify_transient


def _describe(exc: Exception) -> str:
    if isinstance(exc, SourceUnavailable):
        return str(exc)
    return f"unavailable: {classify_transient(exc) or ERROR} — {type(exc).__name__}: {exc}"


class PriceHistoryUnavailable(SourceUnavailable):
    """Neither the primary nor the fallback produced bars, and the primary FAILED (not empty).

    ``reason`` is the primary's (so a throttled primary stays ``rate_limited``); the message names
    each source and its cause, so the tool envelope shows the whole chain, not a bare null.
    """

    def __init__(
        self,
        primary_name: str,
        primary_error: Exception,
        fallback_name: str,
        fallback_error: Exception | None,
    ) -> None:
        self.primary_error = primary_error
        self.fallback_error = fallback_error
        reason = (
            primary_error.reason
            if isinstance(primary_error, SourceUnavailable)
            else classify_transient(primary_error) or ERROR
        )
        fallback_outcome = (
            f"failed ({_describe(fallback_error)})"
            if fallback_error is not None
            else "returned no bars"
        )
        super().__init__(
            reason,
            f"{primary_name} failed ({_describe(primary_error)}); "
            f"{fallback_name} fallback {fallback_outcome}",
        )


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
        fallback_error: Exception | None = None
        try:
            fallback_result = await self._fallback.get_price_history(symbol, range, interval, as_of)
        except Exception as exc:  # noqa: BLE001 — kept as context, never masks the primary's story
            fallback_error = exc
        if fallback_result is not None and fallback_result.bars:
            return self._stamped(fallback_result, self._fallback_name)

        # No bars from either side. If the primary FAILED, surface it (with the fallback's cause) —
        # "couldn't fetch" and "fetched, symbol has no data" must stay distinct.
        if primary_error is not None:
            raise PriceHistoryUnavailable(
                self._primary_name, primary_error, self._fallback_name, fallback_error
            ) from primary_error
        if fallback_result is not None:
            return self._stamped(fallback_result, self._fallback_name)
        return self._stamped(primary_result, self._primary_name)

    def _stamped(self, history: PriceHistory | None, source: str) -> PriceHistory | None:
        if history is not None:
            history.source = source
        return history
