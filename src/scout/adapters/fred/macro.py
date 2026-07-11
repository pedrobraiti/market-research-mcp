"""FRED implementation of ``MacroSource``.

Uses FRED's keyless ``fredgraph.csv`` download endpoint, so no API key is required — it stays in
the "free sources first" spirit. Each series is fetched concurrently; a series that fails stays
in the snapshot with a null value and an ``unavailable: …`` status (never silently dropped —
a missing indicator would read as "no such series").

The CSV fetch is injected (``fetch_csv``) so the unit tests run fully offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ...analytics import (
    percentile_of_last,
    recession_probit,
    sahm_gap,
    trailing_negative_run,
    zscore_of_last,
)
from ...domain.models import MacroDerived, MacroIndicator, MacroSnapshot
from ..retry import SourceUnavailable, unavailable_status, with_retry

_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# (series id, human label). Chosen for an equity macro read: policy rate, the curve,
# the labour market, inflation level and volatility. DGS3MO feeds the recession probit.
_SERIES: tuple[tuple[str, str], ...] = (
    ("FEDFUNDS", "Federal Funds Rate (%)"),
    ("DGS10", "10-Year Treasury Yield (%)"),
    ("DGS2", "2-Year Treasury Yield (%)"),
    ("DGS3MO", "3-Month Treasury Yield (%)"),
    ("T10Y2Y", "10Y-2Y Treasury Spread (%)"),
    ("UNRATE", "Unemployment Rate (%)"),
    ("CPIAUCSL", "CPI (index, SA)"),
    ("VIXCLS", "VIX (volatility index)"),
    ("T10YIE", "10-Year Breakeven Inflation (%)"),
    ("DFII10", "10-Year TIPS Real Yield (%)"),
    ("BAMLH0A0HYM2", "US High-Yield Credit Spread (OAS, %)"),
    # --- Macro source expansion (ADR-013): liquidity, financial conditions, labour,
    # nowcasts, the VIX term structure, the dollar and energy. All keyless fredgraph CSV.
    ("WALCL", "Fed Total Assets ($M, weekly)"),
    ("WTREGEN", "Treasury General Account ($M, weekly)"),
    ("RRPONTSYD", "Overnight Reverse Repo ($B, daily)"),
    ("NFCI", "Chicago Fed Financial Conditions Index"),
    ("STLFSI4", "St. Louis Fed Financial Stress Index"),
    ("ICSA", "Initial Jobless Claims (SA)"),
    ("IC4WSA", "Initial Jobless Claims 4-Week Avg (SA)"),
    ("CCSA", "Continued Jobless Claims (SA)"),
    ("GDPNOW", "Atlanta Fed GDPNow (% nowcast)"),
    ("CFNAI", "Chicago Fed National Activity Index"),
    ("CFNAIMA3", "CFNAI 3-Month Average"),
    ("WEI", "Weekly Economic Index (Lewis-Mertens-Stock)"),
    ("VXVCLS", "VIX 3-Month (VXV)"),
    ("T5YIFR", "5y5y Forward Inflation Expectation (%)"),
    ("M2SL", "M2 Money Stock ($B, monthly)"),
    ("SOFR", "Secured Overnight Financing Rate (%)"),
    ("DTWEXBGS", "Nominal Broad USD Index"),
    ("DCOILBRENTEU", "Brent Crude ($/bbl)"),
    ("DCOILWTICO", "WTI Crude ($/bbl)"),
    ("DHHNGSP", "Henry Hub Natural Gas ($/MMBtu)"),
    ("PCOPPUSDM", "Global Copper ($/tonne, monthly)"),
    ("OVXCLS", "Crude Oil Volatility (OVX)"),
    ("GVZCLS", "Gold Volatility (GVZ)"),
)

_VIX_WINDOW = 252  # ~1 trading year for the VIX regime z-score/percentile
_CREDIT_WINDOW = 252  # ~1 trading year for the credit-spread regime z-score
_CPI_YOY_MIN_MONTHS = 13  # need a year-ago observation to compute YoY
_NET_LIQ_WINDOW = 52  # ~1 year of weekly net-liquidity points for the z-score
_NET_LIQ_MIN_ZSCORE_WEEKS = 8  # min weekly points before a net-liquidity z-score is meaningful
_DOLLAR_WINDOW = 252  # ~1 trading year for the broad-USD z-score
_DOLLAR_MIN_POINTS = 30  # min daily points before a dollar z-score is meaningful
_M2_YOY_MIN_MONTHS = 13  # need a year-ago monthly observation for M2 YoY
_CLAIMS_YOY_MIN_WEEKS = 53  # need a year-ago weekly observation for jobless-claims YoY
_CFNAI_RECESSION_THRESHOLD = -0.70  # Chicago Fed CFNAI-MA3 recession-signal level


def _parse_value(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw or raw == ".":  # FRED marks missing observations with a dot
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _observations(csv_text: str, as_of: date | None) -> list[tuple[date, Decimal]]:
    """Parse a fredgraph CSV into (date, value) pairs at or before ``as_of``, oldest→newest.
    Missing observations (FRED's dot) are skipped. The whole series is kept — not just the last
    point — so the derived layer can compute YoY, z-scores, the Sahm gap, etc."""
    rows: list[tuple[date, Decimal]] = []
    for line in csv_text.splitlines()[1:]:  # skip the header row
        parts = line.split(",")
        if len(parts) < 2:
            continue
        observation_date = _parse_date(parts[0])
        value = _parse_value(parts[1])
        if observation_date is None or value is None:
            continue
        if as_of is not None and observation_date > as_of:
            continue
        rows.append((observation_date, value))
    rows.sort(key=lambda r: r[0])
    return rows


def _value_on_or_before(obs: list[tuple[date, Decimal]], target: date) -> Decimal | None:
    """Most recent value at or before ``target`` (obs assumed oldest→newest)."""
    result: Decimal | None = None
    for observation_date, value in obs:
        if observation_date <= target:
            result = value
        else:
            break
    return result


def _round(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, places)))


class FredMacro:
    def __init__(
        self,
        fetch_csv: Callable[[str], Awaitable[str]] | None = None,
        timeout: float = 15.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.5,
    ) -> None:
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._fetch_csv = fetch_csv or self._default_fetch_csv

    async def _default_fetch_csv(self, url: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_macro_context(self, as_of: date | None = None) -> MacroSnapshot:
        results = await asyncio.gather(
            *(self._one(series_id, name, as_of) for series_id, name in _SERIES),
            return_exceptions=True,
        )
        indicators: list[MacroIndicator] = []
        series: dict[str, list[tuple[date, Decimal]]] = {}
        for result in results:
            if not isinstance(result, tuple):
                continue  # defensive: ``_one`` converts every failure into a flagged indicator
            indicator, observations = result
            indicators.append(indicator)
            if observations:
                series[indicator.series_id] = observations
        return MacroSnapshot(
            indicators=indicators, derived=_derive(series), as_of=as_of
        )

    async def _one(
        self, series_id: str, name: str, as_of: date | None
    ) -> tuple[MacroIndicator, list[tuple[date, Decimal]] | None]:
        url = _CSV_URL.format(series=series_id)
        try:
            csv_text = await with_retry(
                lambda: self._fetch_csv(url),
                attempts=self._retry_attempts,
                base_delay=self._retry_base_delay,
            )
            observations = _observations(csv_text, as_of)
        except SourceUnavailable as exc:
            # Keep the series in the snapshot but flag it: a rate-limited/timed-out series must
            # not vanish (which would read as "no such indicator") — value is null WITH a status.
            indicator = MacroIndicator(
                series_id=series_id, name=name, value=None, status=exc.status
            )
            return indicator, None
        except Exception as exc:  # noqa: BLE001 — a non-transient failure (404, parse error…)
            # …must ALSO stay visible: null value with an ``unavailable: error`` status, exactly
            # like the transient case, instead of the series silently vanishing from the list.
            indicator = MacroIndicator(
                series_id=series_id, name=name, value=None, status=unavailable_status(exc)
            )
            return indicator, None
        latest_date, latest_value = observations[-1] if observations else (None, None)
        indicator = MacroIndicator(
            series_id=series_id, name=name, value=latest_value, observation_date=latest_date
        )
        return indicator, observations


def _derive(series: dict[str, list[tuple[date, Decimal]]]) -> MacroDerived:
    """Turn the raw FRED series into regime metrics. Every block is independently guarded so a
    missing or short series simply leaves its fields null (and, where useful, leaves a note)."""
    derived = MacroDerived()
    notes: list[str] = []

    cpi = series.get("CPIAUCSL")
    cpi_yoy: float | None = None
    if cpi and len(cpi) >= _CPI_YOY_MIN_MONTHS:
        latest_date, latest_value = cpi[-1]
        year_ago = _value_on_or_before(cpi, latest_date - timedelta(days=365))
        if year_ago and year_ago != 0:
            cpi_yoy = (float(latest_value) / float(year_ago) - 1) * 100
            derived.cpi_yoy = _round(cpi_yoy, 2)
    if cpi and len(cpi) >= 4:
        # CPI is a monthly series dated on the 1st: index by POSITION — cpi[-4] is exactly
        # 3 prints before cpi[-1]. A calendar lookup (latest − 92d) lands on the print FOUR
        # months back in most months, and the error is then raised to the 4th power.
        latest_value = cpi[-1][1]
        three_ago = cpi[-4][1]
        if three_ago and three_ago != 0:
            derived.cpi_3m_annualized = _round(
                ((float(latest_value) / float(three_ago)) ** 4 - 1) * 100, 2
            )

    if cpi_yoy is not None:
        fedfunds = series.get("FEDFUNDS")
        if fedfunds:
            derived.real_fed_funds = _round(float(fedfunds[-1][1]) - cpi_yoy, 2)
        dgs10 = series.get("DGS10")
        if dgs10:
            derived.real_10y = _round(float(dgs10[-1][1]) - cpi_yoy, 2)

    unrate = series.get("UNRATE")
    if unrate:
        gap = sahm_gap([float(v) for _, v in unrate])
        if gap is not None:
            derived.sahm_gap = _round(gap, 2)
            derived.sahm_recession_signal = gap >= 0.50

    vix = series.get("VIXCLS")
    if vix and len(vix) >= 30:
        window = [float(v) for _, v in vix][-_VIX_WINDOW:]
        z = zscore_of_last(window)
        pct = percentile_of_last(window)
        derived.vix_zscore = _round(z, 2)
        derived.vix_percentile = _round(pct * 100, 1) if pct is not None else None

    t10y2y = series.get("T10Y2Y")
    if t10y2y:
        spread_values = [float(v) for _, v in t10y2y]
        derived.yield_curve_inverted = spread_values[-1] < 0
        derived.yield_curve_days_inverted = trailing_negative_run(spread_values)

    dgs10 = series.get("DGS10")
    dgs3mo = series.get("DGS3MO")
    if dgs10 and dgs3mo:
        spread = float(dgs10[-1][1]) - float(dgs3mo[-1][1])
        derived.recession_prob_12m = _round(recession_probit(spread) * 100, 1)
        notes.append(
            "recession_prob_12m: NY-Fed probit on the latest daily 10y-3m spread as a proxy for "
            "the model's monthly-average input."
        )

    breakeven = series.get("T10YIE")
    if breakeven:
        derived.inflation_expectations_10y = _round(float(breakeven[-1][1]), 2)
    real_yield = series.get("DFII10")
    if real_yield:
        derived.real_10y_exante = _round(float(real_yield[-1][1]), 2)
    credit = series.get("BAMLH0A0HYM2")
    if credit:
        derived.credit_spread_hy = _round(float(credit[-1][1]), 2)
        if len(credit) >= 30:
            window = [float(v) for _, v in credit][-_CREDIT_WINDOW:]
            z = zscore_of_last(window)
            derived.credit_spread_hy_zscore = _round(z, 2)

    # Fed net liquidity = WALCL − TGA − RRP. UNIT GOTCHA: WALCL and WTREGEN are in $ MILLIONS,
    # but RRPONTSYD is in $ BILLIONS — multiply RRP by 1000 to bring it to $M before subtracting,
    # or the result is off by three orders of magnitude. The net series is built on WALCL's
    # (weekly) observation dates, carrying the most recent TGA/RRP value forward to each date.
    walcl = series.get("WALCL")
    treasury_account = series.get("WTREGEN")
    reverse_repo = series.get("RRPONTSYD")
    if walcl and treasury_account and reverse_repo:
        net_series: list[float] = []
        for observation_date, total_assets in walcl:
            tga = _value_on_or_before(treasury_account, observation_date)
            rrp = _value_on_or_before(reverse_repo, observation_date)
            if tga is None or rrp is None:
                continue
            net_series.append(float(total_assets) - float(tga) - float(rrp) * 1000)
        if net_series:
            derived.net_liquidity = _round(net_series[-1], 0)
            notes.append(
                "net_liquidity: WALCL − TGA − RRP, mixing the weekly balance-sheet/TGA prints with "
                "the daily RRP carried forward to each WALCL date; RRP (in $B) is scaled ×1000 to "
                "the $M unit of WALCL/TGA."
            )
        if len(net_series) >= 2:
            derived.net_liquidity_wow = _round(net_series[-1] - net_series[-2], 0)
        if len(net_series) >= _NET_LIQ_MIN_ZSCORE_WEEKS:
            z = zscore_of_last(net_series[-_NET_LIQ_WINDOW:])
            derived.net_liquidity_zscore = _round(z, 2)

    nfci = series.get("NFCI")
    if nfci:
        # NFCI is already standardized (mean 0); positive = tighter-than-average conditions.
        derived.financial_conditions_tight = float(nfci[-1][1]) > 0

    claims_4wk = series.get("IC4WSA")
    if claims_4wk and len(claims_4wk) >= _CLAIMS_YOY_MIN_WEEKS:
        latest_date, latest_value = claims_4wk[-1]
        year_ago = _value_on_or_before(claims_4wk, latest_date - timedelta(days=365))
        if year_ago and year_ago != 0:
            derived.initial_claims_4wk_yoy = _round(
                (float(latest_value) / float(year_ago) - 1) * 100, 2
            )

    cfnai_ma3 = series.get("CFNAIMA3")
    if cfnai_ma3:
        derived.cfnai_recession_signal = float(cfnai_ma3[-1][1]) < _CFNAI_RECESSION_THRESHOLD

    vxv = series.get("VXVCLS")
    if vxv and vix:
        vix_latest = float(vix[-1][1])
        if vix_latest > 0:
            ratio = float(vxv[-1][1]) / vix_latest
            derived.vix_term_structure = _round(ratio, 2)
            derived.vix_backwardation = ratio < 1.0

    forward_inflation = series.get("T5YIFR")
    if forward_inflation:
        derived.inflation_5y5y = _round(float(forward_inflation[-1][1]), 2)

    m2 = series.get("M2SL")
    if m2 and len(m2) >= _M2_YOY_MIN_MONTHS:
        latest_date, latest_value = m2[-1]
        year_ago = _value_on_or_before(m2, latest_date - timedelta(days=365))
        if year_ago and year_ago != 0:
            derived.m2_yoy = _round((float(latest_value) / float(year_ago) - 1) * 100, 2)

    dollar = series.get("DTWEXBGS")
    if dollar and len(dollar) >= _DOLLAR_MIN_POINTS:
        window = [float(v) for _, v in dollar][-_DOLLAR_WINDOW:]
        derived.dollar_broad_zscore = _round(zscore_of_last(window), 2)

    brent = series.get("DCOILBRENTEU")
    wti = series.get("DCOILWTICO")
    if brent and wti:
        derived.brent_wti_spread = _round(float(brent[-1][1]) - float(wti[-1][1]), 2)

    derived.notes = notes
    return derived
