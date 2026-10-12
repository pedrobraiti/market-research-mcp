"""GDELT DOC 2.0 implementation of ``NewsSearchSource`` — keyless, no API key.

GDELT indexes global news in near-real-time and exposes a keyless DOC API. Unlike the
symbol-based ``news`` tool (yfinance), this searches by FREE TEXT — a theme ("AI data center
power"), an event, or a company name — across worldwide media. Capture only: it returns
articles (title, source, date, link) for the agent to read via ``extract`` and judge.

GDELT throttles hard: one call per ~5 s, answered with HTTP 429 and a plain-text notice (observed
live 2026-10-10), sometimes with that notice on a 200. Either way it is ``unavailable:
rate_limited`` on ``source_status`` — never an empty list that reads as "no news". A non-JSON body
that is not the throttle notice is ``unavailable: schema_error``. Retries wait at least the
source's own interval (a 0.5 s retry would only earn another 429), honouring ``Retry-After``.

The JSON fetch is injected for offline tests (``transport`` lets a test drive the real httpx path).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ...domain.models import WebNewsItem, WebNewsSearch
from ..retry import SCHEMA_ERROR, SourceUnavailable, with_retry

_DOC_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc?query={query}&mode=artlist&format=json"
    "&maxrecords={limit}&timespan={days}d&sort=datedesc"
)

# GDELT's published courtesy interval ("one every 5 seconds") — the floor for a retry wait.
_MIN_INTERVAL_SECONDS = 5.0
# One retry only: with a 5 s floor, a second retry would push a tool call past ~15 s.
_RETRY_ATTEMPTS = 2
_THROTTLE_MARKER = "limit requests"
_BODY_PREFIX_CHARS = 120


class GdeltRateLimitError(Exception):
    """GDELT's throttle notice delivered on a non-429 response. The class name contains
    ``RateLimit`` so ``classify_transient`` treats it as ``rate_limited`` (retry + honest label)."""


def _parse_seendate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None


class GdeltNewsSearch:
    def __init__(
        self,
        fetch_json: Callable[[str], Awaitable[dict]] | None = None,
        timeout: float = 15.0,
        retry_attempts: int = _RETRY_ATTEMPTS,
        retry_base_delay: float = _MIN_INTERVAL_SECONDS,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._transport = transport
        self._sleep = sleep or asyncio.sleep
        self._fetch_json = fetch_json or self._default_fetch_json

    async def _default_fetch_json(self, url: str) -> dict:
        import httpx

        headers = {"User-Agent": "scout-mcp/0.1 (research; +https://github.com/pedrobraiti)"}
        async with httpx.AsyncClient(
            timeout=self._timeout, headers=headers, transport=self._transport
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            text = response.text.strip()
            if not text:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                prefix = " ".join(text.split())[:_BODY_PREFIX_CHARS]
                if _THROTTLE_MARKER in text.lower():
                    raise GdeltRateLimitError(prefix) from exc
                raise SourceUnavailable(
                    SCHEMA_ERROR, f"GDELT returned a non-JSON body: {prefix!r}"
                ) from exc

    async def search_news(self, query: str, limit: int = 20, days: int = 7) -> WebNewsSearch:
        clean = query.strip()
        capped = max(1, min(limit, 75))
        days_window = max(1, min(days, 90))
        url = _DOC_URL.format(query=quote(clean), limit=capped, days=days_window)
        try:
            data = await with_retry(
                lambda: self._fetch_json(url),
                attempts=self._retry_attempts,
                base_delay=self._retry_base_delay,
                sleep=self._sleep,
            )
        except SourceUnavailable as exc:
            # A 429/timeout is "couldn't fetch", NOT "no news" — surface it so a downstream
            # gate retries/abstains instead of treating an empty list as a real absence.
            return WebNewsSearch(query=clean, items=[], source_status=exc.status)
        articles = (data or {}).get("articles") or []
        items: list[WebNewsItem] = []
        for article in articles[:capped]:
            if not isinstance(article, dict):
                continue
            items.append(
                WebNewsItem(
                    title=article.get("title"),
                    domain=article.get("domain"),
                    url=article.get("url"),
                    published=_parse_seendate(article.get("seendate")),
                    language=article.get("language"),
                    country=article.get("sourcecountry"),
                )
            )
        return WebNewsSearch(query=clean, items=items)
