from datetime import date
from decimal import Decimal

import httpx
import pytest

from scout.adapters.retry import SourceUnavailable
from scout.adapters.sec import SecEdgar

_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

_SUBMISSIONS = {
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["10-K", "8-K", "10-Q", "4"],
            "filingDate": ["2024-11-01", "2024-10-15", "2024-08-02", "2024-07-20"],
            "reportDate": ["2024-09-28", "", "2024-06-29", ""],
            "accessionNumber": [
                "0000320193-24-000123",
                "0000320193-24-000110",
                "0000320193-24-000081",
                "0000320193-24-000070",
            ],
            "primaryDocument": ["aapl-20240928.htm", "ex.htm", "aapl-20240629.htm", "form4.xml"],
            "primaryDocDescription": ["10-K", "8-K", "10-Q", "FORM 4"],
        }
    },
}


def _concept(observations):
    return {"units": {"USD": observations}}


def _annual(fy, end, val, filed):
    return {"fy": fy, "fp": "FY", "form": "10-K", "end": end, "val": val, "filed": filed}


# Apple does NOT use the primary revenue tag — get_financials must fall back to "Revenues".
_CONCEPTS = {
    "Revenues": _concept(
        [
            _annual(2024, "2024-09-28", 391035000000, "2024-11-01"),
            _annual(2023, "2023-09-30", 383285000000, "2023-11-03"),
            {"fy": 2024, "fp": "Q1", "form": "10-Q", "end": "2023-12-30", "val": 1, "filed": "x"},
        ]
    ),
    "GrossProfit": _concept([_annual(2024, "2024-09-28", 180683000000, "2024-11-01")]),
    "OperatingIncomeLoss": _concept([_annual(2024, "2024-09-28", 123216000000, "2024-11-01")]),
    "NetIncomeLoss": _concept(
        [
            _annual(2024, "2024-09-28", 93736000000, "2024-11-01"),
            _annual(2023, "2023-09-30", 96995000000, "2023-11-03"),
        ]
    ),
    "Assets": _concept([_annual(2024, "2024-09-28", 364980000000, "2024-11-01")]),
    "StockholdersEquity": _concept([_annual(2024, "2024-09-28", 56950000000, "2024-11-01")]),
}


_FTS = {
    "hits": {
        "total": {"value": 42},
        "hits": [
            {
                "_id": "0000320193-24-000123:aapl-20240928.htm",
                "_source": {
                    "ciks": ["0000320193"],
                    "display_names": ["Apple Inc. (AAPL) (CIK 0000320193)"],
                    "form_type": "10-K",
                    "file_date": "2024-11-01",
                },
            }
        ],
    }
}


def _http_error(status: int, url: str, body: bytes) -> httpx.HTTPStatusError:
    """The exact exception the default httpx fetcher raises (``raise_for_status``)."""
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request, content=body)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# What SEC really serves an undeclared User-Agent (probed 2026-10-12): HTTP 403 + an HTML page.
_UNDECLARED_AGENT_HTML = (
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN">\n<html><head>'
    b"<title>SEC.gov | Your Request Originates from an Undeclared Automated Tool</title>"
)


def _fetch_factory():
    async def _fetch(url: str) -> dict:
        if "company_tickers" in url:
            return _TICKERS
        if "efts.sec.gov" in url:
            return _FTS
        if "/companyconcept/" in url:
            tag = url.rsplit("/", 1)[-1].removesuffix(".json")
            if tag in _CONCEPTS:
                return _CONCEPTS[tag]
            raise _http_error(404, url, b"<Error><Code>NoSuchKey</Code></Error>")
        return _SUBMISSIONS

    return _fetch


def _source():
    return SecEdgar("Test Agent test@example.com", fetch_json=_fetch_factory())


# ---- filings -------------------------------------------------------------------------


async def test_resolves_cik_and_returns_filings():
    result = await _source().get_filings("aapl")
    assert result is not None
    assert result.cik == "0000320193"
    assert result.name == "Apple Inc."
    assert len(result.filings) == 4
    first = result.filings[0]
    assert first.form == "10-K"
    assert first.filing_date == date(2024, 11, 1)
    assert first.report_date == date(2024, 9, 28)
    assert first.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
    )


async def test_filters_by_form_type():
    result = await _source().get_filings("AAPL", form_type="10-q")
    assert result is not None
    assert len(result.filings) == 1
    assert result.filings[0].form == "10-Q"


async def test_filters_by_as_of():
    result = await _source().get_filings("AAPL", as_of=date(2024, 9, 1))
    assert result is not None
    assert [f.form for f in result.filings] == ["10-Q", "4"]


async def test_limit_caps_results():
    result = await _source().get_filings("AAPL", limit=2)
    assert result is not None
    assert len(result.filings) == 2


async def test_unresolved_symbol_returns_none():
    assert await _source().get_filings("NOPE") is None


# ---- financials (XBRL cross-check) ---------------------------------------------------


async def test_financials_returns_reported_annual_with_provenance():
    financials = await _source().get_financials("aapl")
    assert financials is not None
    assert financials.cik == "0000320193"
    by_concept = {line.concept: line for line in financials.lines}
    revenue = by_concept["revenue"]
    assert revenue.value == Decimal("391035000000")
    assert revenue.tag == "Revenues"  # fell back from the unused primary tag
    assert revenue.period_end == date(2024, 9, 28)
    assert revenue.form == "10-K"
    assert revenue.filed == date(2024, 11, 1)
    assert by_concept["net_income"].value == Decimal("93736000000")
    assert by_concept["stockholders_equity"].value == Decimal("56950000000")
    assert financials.fiscal_year == 2024
    assert financials.period_end == date(2024, 9, 28)


async def test_financials_as_of_picks_prior_year():
    financials = await _source().get_financials("AAPL", as_of=date(2024, 1, 1))
    assert financials is not None
    by_concept = {line.concept: line for line in financials.lines}
    assert by_concept["revenue"].value == Decimal("383285000000")
    assert by_concept["revenue"].period_end == date(2023, 9, 30)
    # gross_profit only has a FY2024 observation, which is after as_of → no value.
    assert by_concept["gross_profit"].value is None


async def test_financials_unresolved_symbol_returns_none():
    assert await _source().get_financials("NOPE") is None


# ---- full-text search ----------------------------------------------------------------


async def test_search_filings_parses_hits():
    result = await _source().search_filings("small modular reactor", forms="10-K", limit=5)
    assert result.query == "small modular reactor"
    assert result.total == 42
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.ticker == "AAPL"
    assert "Apple Inc." in hit.company
    assert hit.cik == "0000320193"
    assert hit.form == "10-K"
    assert hit.filing_date == date(2024, 11, 1)
    assert hit.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
    )


# ---- explicit failure: unset identity / unreadable SEC (audit item 6) ------------------


async def test_unset_user_agent_sends_no_request(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("an anonymous request reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    with pytest.raises(SourceUnavailable) as excinfo:
        await SecEdgar("   ").get_financials("AAPL")
    assert excinfo.value.status == "unavailable: misconfigured"
    assert "SCOUT_SEC_USER_AGENT" in str(excinfo.value)


def _concepts_fail_with(status: int, body: bytes = b""):
    async def _fetch(url: str) -> dict:
        if "company_tickers" in url:
            return _TICKERS
        raise _http_error(status, url, body)

    return _fetch


async def test_unreadable_concepts_carry_status_not_six_silent_nulls():
    # Ticker map cached, but every XBRL concept answers 403 — previously six null lines with no
    # status, indistinguishable from "the company reports nothing".
    source = SecEdgar("Test Agent test@example.com", fetch_json=_concepts_fail_with(
        403, _UNDECLARED_AGENT_HTML
    ))
    financials = await source.get_financials("AAPL")
    assert financials is not None
    assert len(financials.lines) == 6
    assert all(line.value is None for line in financials.lines)
    assert all(line.status.startswith("unavailable: blocked") for line in financials.lines)
    assert financials.status.startswith("unavailable: blocked")
    assert "SCOUT_SEC_USER_AGENT" in financials.status


async def test_rate_limited_concepts_are_unavailable():
    source = SecEdgar("Test Agent test@example.com", fetch_json=_concepts_fail_with(429))
    financials = await source.get_financials("AAPL")
    assert financials.status.startswith("unavailable: rate_limited")


async def test_not_reported_is_distinct_from_unavailable():
    base = _fetch_factory()

    async def _fetch(url: str) -> dict:
        if url.endswith("/GrossProfit.json"):
            raise _http_error(404, url, b"<Error><Code>NoSuchKey</Code></Error>")
        if url.endswith("/OperatingIncomeLoss.json"):
            raise _http_error(429, url, b"")
        return await base(url)

    financials = await SecEdgar("Test Agent test@example.com", fetch_json=_fetch).get_financials(
        "AAPL"
    )
    by_concept = {line.concept: line for line in financials.lines}
    assert by_concept["gross_profit"].status == "not_reported"
    assert by_concept["operating_income"].status.startswith("unavailable: rate_limited")
    assert by_concept["revenue"].status is None  # found (via the fallback tag)
    assert financials.status == "partial: operating_income unavailable"
    assert financials.fiscal_year == 2024  # the readable lines are still delivered


async def test_fully_read_financials_have_no_status():
    financials = await _source().get_financials("AAPL")
    assert financials.status is None
    assert all(line.status is None for line in financials.lines)
