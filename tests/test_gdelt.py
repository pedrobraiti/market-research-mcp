from datetime import datetime

from scout.adapters.gdelt import GdeltNewsSearch

_RESPONSE = {
    "articles": [
        {
            "title": "AI data centers strain the power grid",
            "domain": "reuters.com",
            "url": "https://reuters.com/ai-power",
            "seendate": "20260620T143000Z",
            "language": "English",
            "sourcecountry": "United States",
        },
        {"title": "second", "domain": "ft.com", "url": "https://ft.com/x", "seendate": "bad"},
    ]
}


def _source(captured=None):
    async def _fetch(url: str) -> dict:
        if captured is not None:
            captured["url"] = url
        return _RESPONSE

    return GdeltNewsSearch(fetch_json=_fetch)


async def test_search_news_parses_articles():
    captured = {}

    async def _fetch(url: str) -> dict:
        captured["url"] = url
        return _RESPONSE

    source = GdeltNewsSearch(fetch_json=_fetch)
    result = await source.search_news("AI data center power", limit=10, days=7)
    assert result.query == "AI data center power"
    assert "AI%20data%20center%20power" in captured["url"]  # url-encoded query
    assert "timespan=7d" in captured["url"]
    assert len(result.items) == 2
    assert result.items[0].domain == "reuters.com"
    assert result.items[0].published == datetime(2026, 6, 20, 14, 30, 0)
    assert result.items[1].published is None  # bad seendate parsed to None


async def test_search_news_empty_response():
    async def _fetch(url: str) -> dict:
        return {}

    result = await GdeltNewsSearch(fetch_json=_fetch).search_news("nothing")
    assert result.items == []
    assert result.source_status is None  # genuine "no matches", not a fetch failure


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHttpError(Exception):
    """Mimics httpx.HTTPStatusError for the retry classifier (carries .response.status_code)."""

    def __init__(self, status_code: int) -> None:
        self.response = _Resp(status_code)
        super().__init__(f"HTTP {status_code}")


async def test_search_news_rate_limited_marks_unavailable():
    calls = {"n": 0}

    async def _fetch(url: str) -> dict:
        calls["n"] += 1
        raise _FakeHttpError(429)

    source = GdeltNewsSearch(fetch_json=_fetch, retry_attempts=3, retry_base_delay=0)
    result = await source.search_news("AI data center power")
    assert calls["n"] == 3  # retried the full budget before giving up
    assert result.items == []
    # A 429 is "couldn't fetch", distinguishable from an empty-but-successful response.
    assert result.source_status == "unavailable: rate_limited"


# --- Real GDELT throttle payloads (audit 2026-10-10, evidencia-1-diagnostico.jsonl) ---------
# GDELT answered HTTP 429, no content-type, with this plain-text body (verbatim prefix).
_GDELT_429_BODY = (
    "Please limit requests to one every 5 seconds or contact kalev.leetaru5@gmail.com for larger "
    "queries. All high-traffic users should switch to our ngrams dataset: "
    "https://blog.gdeltproject.org/using-the-new-web-ngrams-dataset-to-find-relevant-coverage/. "
    "For trend analysis, please see our daily newsletter briefings: "
    "https://blog.gdeltproject.org/announcing-our-new-daily-todays-trends-on-capitol-hill-"
    "todays-media-trends-newsletter-briefings/.\n\n"
)


def _http_source(status: int, body: str, headers: dict | None = None):
    """GdeltNewsSearch driven through its REAL httpx fetch path via a mock transport."""
    import httpx

    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, headers=headers or {}, content=body.encode())

    async def fake_sleep(seconds):
        slept.append(seconds)

    source = GdeltNewsSearch(transport=httpx.MockTransport(handler), sleep=fake_sleep)
    return source, calls, slept


async def test_real_429_text_body_is_unavailable_not_zero_news():
    source, calls, slept = _http_source(429, _GDELT_429_BODY)
    result = await source.search_news("bitcoin", limit=3, days=1)
    assert result.items == []
    assert result.source_status == "unavailable: rate_limited"  # "couldn't fetch", not "no news"
    assert calls["n"] == 2  # one retry only — a tool call can't hammer a 5s/call source
    assert slept == [5.0]  # the retry waits GDELT's own interval, not 0.5s


async def test_429_retry_after_header_is_honoured():
    source, calls, slept = _http_source(429, _GDELT_429_BODY, {"Retry-After": "8"})
    result = await source.search_news("bitcoin")
    assert result.source_status == "unavailable: rate_limited"
    assert slept == [8.0]  # Retry-After (8s) beats the 5s floor


async def test_throttle_notice_on_http_200_is_rate_limited():
    # Same notice, but on a 200 — must not raise a JSON error nor read as an empty result.
    source, calls, _ = _http_source(200, _GDELT_429_BODY, {"content-type": "text/html"})
    result = await source.search_news("bitcoin")
    assert result.items == []
    assert result.source_status == "unavailable: rate_limited"
    assert calls["n"] == 2


async def test_non_json_body_is_schema_error_not_empty():
    source, calls, _ = _http_source(200, "<html><body>maintenance</body></html>")
    result = await source.search_news("bitcoin")
    assert result.items == []
    assert result.source_status == "unavailable: schema_error"
    assert calls["n"] == 1  # a schema break isn't transient — no retry
