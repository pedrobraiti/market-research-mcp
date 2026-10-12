"""Shared HTTP retry/backoff for the keyless web adapters (GDELT, FRED, …).

The free data sources rate-limit (HTTP 429) and occasionally time out. A null field coming
from a rate-limit must NOT look like "this data does not exist" — that would mislead a
downstream data-sufficiency gate. So this helper retries transient failures (429 and connect/
read timeouts) with exponential backoff and, once exhausted, raises ``SourceUnavailable`` with a
machine-readable ``reason`` the adapter surfaces as a status (e.g. ``unavailable: rate_limited``)
instead of a silent ``None``.

Non-transient errors (a 404, a malformed payload, …) propagate immediately — they are genuine,
not worth retrying, and keep the existing error-envelope behaviour.

A server-sent ``Retry-After`` (seconds or HTTP-date) is honoured as the minimum wait before the next
attempt — but only up to ``MAX_RETRY_AFTER_SECONDS``. A source asking for longer than that fails
fast as ``unavailable: rate_limited`` instead of parking a tool call for minutes; retrying sooner
than it asked would only earn another 429.

httpx is intentionally NOT imported here: failures are classified structurally (a ``response``
with ``status_code``) and by exception-class name, so offline tests can simulate a 429/timeout
with a tiny fake and never need the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
ERROR = "error"  # a genuine, non-transient failure (bad payload, 404…) on an unavailable path
# The source answered, but not with the data format we read: a bot/JS challenge or HTML page
# served with HTTP 200 (BLOCKED), or a body whose shape no longer matches the parser (SCHEMA_ERROR).
# Both are "couldn't read", never "the symbol has no data".
BLOCKED = "blocked"
SCHEMA_ERROR = "schema_error"

# Upper bound on how long a single wait honouring ``Retry-After`` may last. A tool call is
# interactive; a source asking for longer is reported as unavailable rather than waited out.
MAX_RETRY_AFTER_SECONDS = 10.0

# 429 plus the transient-overload 5xx family — all worth backing off and retrying.
_RETRYABLE_STATUS = {429, 502, 503, 504}


class SourceUnavailable(Exception):
    """A source could not be reached after retries — 'couldn't fetch', not 'fetched, empty'.

    ``reason`` is machine-readable (``rate_limited`` / ``timeout`` / ``blocked`` / …); ``status``
    is the ready-made label adapters attach to a model so the caller can tell the two apart.
    ``detail`` is the human-readable cause; ``str(exc)`` always leads with ``status`` so an error
    envelope keeps the machine-readable label even when a detail is attached.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        self.status = f"unavailable: {reason}"
        self.detail = message
        super().__init__(f"{self.status} — {message}" if message else self.status)


def classify_transient(exc: Exception) -> str | None:
    """Return a retry reason for a transient HTTP failure, else ``None`` (do not retry)."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    name = type(exc).__name__.lower()
    if status_code in _RETRYABLE_STATUS:
        return RATE_LIMITED
    # Some clients raise their own throttle type with no httpx ``response`` (e.g. yfinance's
    # YFRateLimitError) — the most common equity-source throttle. Match it by class name so it
    # is backed off + labelled ``rate_limited`` instead of slipping through as a genuine error.
    if "ratelimit" in name:
        return RATE_LIMITED
    if "timeout" in name:
        return TIMEOUT
    return None


def unavailable_status(exc: Exception) -> str:
    """A ready ``unavailable: <reason>`` label for any exception, so a caller that catches a
    failure can attach a machine-readable reason instead of a bare 'unavailable'. Uses the
    transient reason when it can be classified (``rate_limited`` / ``timeout``); a
    ``SourceUnavailable`` already carries its own; anything else is a genuine ``error``."""
    if isinstance(exc, SourceUnavailable):
        return exc.status
    return f"unavailable: {classify_transient(exc) or ERROR}"


def retry_after_seconds(exc: Exception, now: datetime | None = None) -> float | None:
    """Seconds the server asked us to wait (``Retry-After`` header), or ``None`` if absent/garbled.

    Accepts both RFC 9110 forms: delta-seconds (``"5"``) and an HTTP-date. A date already in the
    past means "now" (0). Read structurally from ``exc.response.headers`` so fakes need no httpx.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> T:
    """Run an async fetch, retrying 429/timeout with exponential backoff.

    Succeeds → returns the value. Transient failure that persists past ``attempts`` →
    ``SourceUnavailable`` (carrying the last cause as ``detail``). A ``Retry-After`` longer than
    ``max_retry_after`` → ``SourceUnavailable`` at once. Any other exception is re-raised
    untouched on first sight.
    """
    bounded_attempts = max(1, attempts)
    last_reason: str | None = None
    last_error: Exception | None = None
    for attempt in range(bounded_attempts):
        try:
            return await operation()
        except SourceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — classify, then retry-or-reraise
            reason = classify_transient(exc)
            if reason is None:
                raise
            last_reason, last_error = reason, exc
            if attempt == bounded_attempts - 1:
                break
            requested = retry_after_seconds(exc)
            if requested is not None and requested > max_retry_after:
                raise SourceUnavailable(
                    reason,
                    f"server asked to retry after {requested:.0f}s "
                    f"(cap {max_retry_after:.0f}s); {_describe(exc)}",
                ) from exc
            delay = max(base_delay * (2**attempt), requested or 0.0)
            if delay > 0:
                await sleep(delay)
    detail = _describe(last_error) if last_error is not None else None
    raise SourceUnavailable(last_reason or RATE_LIMITED, detail) from last_error
