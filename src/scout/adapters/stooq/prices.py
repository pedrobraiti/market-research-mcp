"""stooq price history — keyless daily OHLCV via CSV download.

stooq has no real API, but exposes daily OHLCV as a CSV download with no key. It's used as a
**fallback** for price history when yfinance (a scraper) fails or rate-limits — resilience, not a
new signal. Daily only; intraday returns ``None`` so the caller keeps the primary source's result.

Two outcomes must never be confused (ADR-012):

- **"symbol has no data"** — stooq's own ``No data`` body, or a valid CSV with no bars in range →
  ``None``.
- **"couldn't read stooq"** — HTTP 200 carrying an HTML page / JavaScript bot challenge (observed
  live 2026-10-10), the daily-hits-limit notice, an empty body or a CSV whose header changed →
  ``SourceUnavailable`` with a ``blocked`` / ``rate_limited`` / ``schema_error`` reason.

The CSV fetch is injected for offline tests (``transport`` lets a test drive the real httpx path).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from ...domain.models import PriceBar, PriceHistory
from ..retry import BLOCKED, RATE_LIMITED, SCHEMA_ERROR, SourceUnavailable

_CSV_URL = "https://stooq.com/q/d/l/?s={symbol}.us&i=d"

# stooq's documented answer for an unknown symbol — the only body that means "genuinely no data".
_NO_DATA_BODY = "no data"
_HITS_LIMIT_MARKER = "exceeded the daily hits limit"
_CHALLENGE_MARKERS = ("requires javascript", "/__verify", "crypto.subtle")
_BODY_PREFIX_CHARS = 120

# Approximate calendar days per range token.
_RANGE_DAYS = {
    "1mo": 31, "3mo": 93, "6mo": 186, "1y": 366, "2y": 731,
    "5y": 1827, "10y": 3653, "ytd": 366, "max": 100_000,
}


def _to_decimal(value: str):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _body_prefix(text: str) -> str:
    return " ".join(text.split())[:_BODY_PREFIX_CHARS]


def _raise_if_unreadable(text: str) -> None:
    """Raise ``SourceUnavailable`` unless ``text`` is a stooq CSV or stooq's own ``No data``."""
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered.startswith(_NO_DATA_BODY):
        return
    if not stripped:
        raise SourceUnavailable(SCHEMA_ERROR, "stooq returned an empty body instead of CSV")
    if lowered.startswith("<") or "<html" in lowered[:500]:
        kind = (
            "a JavaScript bot challenge"
            if any(marker in lowered for marker in _CHALLENGE_MARKERS)
            else "an HTML page"
        )
        raise SourceUnavailable(BLOCKED, f"stooq returned {kind} instead of CSV")
    if _HITS_LIMIT_MARKER in lowered:
        raise SourceUnavailable(RATE_LIMITED, f"stooq: {_body_prefix(stripped)}")
    if not lowered.startswith("date"):
        raise SourceUnavailable(
            SCHEMA_ERROR, f"stooq CSV header not recognised: {_body_prefix(stripped)!r}"
        )


def _parse(symbol: str, csv_text: str, range_: str, as_of: date | None) -> PriceHistory | None:
    lines = csv_text.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        return None
    end = as_of or date.today()
    start = end - timedelta(days=_RANGE_DAYS.get(range_, 186))
    bars: list[PriceBar] = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            bar_date = datetime.strptime(parts[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if bar_date < start or bar_date > end:
            continue
        volume = parts[5].strip()
        bars.append(
            PriceBar(
                date=bar_date,
                open=_to_decimal(parts[1]),
                high=_to_decimal(parts[2]),
                low=_to_decimal(parts[3]),
                close=_to_decimal(parts[4]),
                volume=int(volume) if volume.isdigit() else None,
            )
        )
    if not bars:
        return None
    return PriceHistory(symbol=symbol, interval="1d", bars=bars, as_of=as_of)


class StooqPrices:
    def __init__(
        self,
        fetch_csv: Callable[[str], Awaitable[str]] | None = None,
        timeout: float = 15.0,
        transport: Any = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._fetch_csv = fetch_csv or self._default_fetch_csv

    async def _default_fetch_csv(self, url: str) -> str:
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=headers,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_price_history(
        self, symbol: str, range: str = "6mo", interval: str = "1d", as_of: date | None = None
    ) -> PriceHistory | None:
        if interval != "1d":  # stooq's keyless CSV is daily only
            return None
        clean = symbol.strip().lower()
        text = await self._fetch_csv(_CSV_URL.format(symbol=clean))
        _raise_if_unreadable(text)
        return _parse(symbol.strip().upper(), text, range, as_of)
