import pytest

from scout.adapters.retry import SourceUnavailable, classify_transient, with_retry


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        self.response = _Resp(status_code)
        super().__init__(f"HTTP {status_code}")


class _ReadTimeout(Exception):
    """Name contains 'timeout' so the classifier treats it as transient (like httpx.ReadTimeout)."""


def test_classify_detects_429_and_5xx_as_rate_limited():
    assert classify_transient(_HttpError(429)) == "rate_limited"
    assert classify_transient(_HttpError(503)) == "rate_limited"


def test_classify_detects_timeout_by_name():
    assert classify_transient(_ReadTimeout()) == "timeout"


def test_classify_detects_rate_limit_by_name_without_response():
    # yfinance raises YFRateLimitError — its own throttle type with NO httpx ``response``.
    # It must still be classified as rate_limited (retry + honest label), not slip through
    # as a genuine error on the primary equity source.
    class YFRateLimitError(Exception):
        pass

    err = YFRateLimitError("Too Many Requests. Rate limited.")
    assert classify_transient(err) == "rate_limited"


def test_classify_ignores_non_transient():
    assert classify_transient(_HttpError(404)) is None
    assert classify_transient(ValueError("bad payload")) is None


async def test_with_retry_returns_value_without_retrying():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        return "ok"

    assert await with_retry(op, attempts=3, base_delay=0) == "ok"
    assert calls["n"] == 1


async def test_with_retry_recovers_after_transient_failures():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HttpError(429)
        return "recovered"

    assert await with_retry(op, attempts=3, base_delay=0) == "recovered"
    assert calls["n"] == 3


async def test_with_retry_raises_source_unavailable_after_exhaustion():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _ReadTimeout()

    with pytest.raises(SourceUnavailable) as exc_info:
        await with_retry(op, attempts=2, base_delay=0)
    assert calls["n"] == 2
    assert exc_info.value.reason == "timeout"
    assert exc_info.value.status == "unavailable: timeout"


async def test_with_retry_reraises_non_transient_immediately():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise ValueError("genuine error")

    with pytest.raises(ValueError, match="genuine error"):
        await with_retry(op, attempts=5, base_delay=0)
    assert calls["n"] == 1  # not retried


# --- Retry-After (audit 2026-10-10: GDELT asks for >=5s/call; 0.5s retries only earn 429s) ---

_URL = "https://api.gdeltproject.org/api/v2/doc/doc?query=bitcoin"


def _real_429(headers: dict | None = None) -> Exception:
    """A real ``httpx.HTTPStatusError`` built from a real 429 ``httpx.Response``."""
    import httpx

    response = httpx.Response(
        429, headers=headers or {}, text="Please limit requests", request=httpx.Request("GET", _URL)
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status did not raise on a 429")


def test_retry_after_seconds_parses_delta_seconds_and_http_date():
    from datetime import UTC, datetime

    from scout.adapters.retry import retry_after_seconds

    assert retry_after_seconds(_real_429({"Retry-After": "7"})) == 7.0
    now = datetime(2026, 10, 10, 18, 40, 6, tzinfo=UTC)
    dated = _real_429({"Retry-After": "Sat, 10 Oct 2026 18:40:18 GMT"})
    assert retry_after_seconds(dated, now=now) == 12.0
    assert retry_after_seconds(_real_429()) is None  # absent header
    assert retry_after_seconds(_real_429({"Retry-After": "soon"})) is None  # garbled
    assert retry_after_seconds(ValueError("no response")) is None


async def test_with_retry_honours_retry_after_as_minimum_wait():
    calls = {"n": 0}
    slept: list[float] = []

    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _real_429({"Retry-After": "3"})
        return "ok"

    async def fake_sleep(seconds):
        slept.append(seconds)

    assert await with_retry(op, attempts=2, base_delay=0.5, sleep=fake_sleep) == "ok"
    assert slept == [3.0]  # the server's 3s, not the 0.5s backoff


async def test_with_retry_fails_fast_when_retry_after_exceeds_cap():
    calls = {"n": 0}
    slept: list[float] = []

    async def op():
        calls["n"] += 1
        raise _real_429({"Retry-After": "120"})

    async def fake_sleep(seconds):
        slept.append(seconds)

    with pytest.raises(SourceUnavailable) as exc_info:
        await with_retry(op, attempts=3, base_delay=0.5, max_retry_after=10, sleep=fake_sleep)
    assert calls["n"] == 1  # no pointless retry the server already refused
    assert slept == []  # the tool call never parks for two minutes
    assert exc_info.value.reason == "rate_limited"
    assert "120s" in str(exc_info.value)


async def test_exhausted_retry_keeps_the_cause_and_leads_with_status():
    async def op():
        raise _real_429()

    with pytest.raises(SourceUnavailable) as exc_info:
        await with_retry(op, attempts=2, base_delay=0)
    message = str(exc_info.value)
    assert message.startswith("unavailable: rate_limited")  # machine label first
    assert "429" in message  # the underlying cause is preserved, not swallowed
