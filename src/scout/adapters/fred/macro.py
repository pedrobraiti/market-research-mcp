"""FRED implementation of ``MacroSource``.

Uses FRED's keyless ``fredgraph.csv`` download endpoint, so no API key is required — it stays in
the "free sources first" spirit. Each series is fetched concurrently; a series that fails stays
in the snapshot with a null value and an ``unavailable: …`` status (never silently dropped —
a missing indicator would read as "no such series"). A body that is not a fredgraph CSV for the
requested series (an HTML page served with HTTP 200, a changed header) is ``unavailable: blocked``
/ ``unavailable: schema_error``, never a silent null.

Temporal honesty: the series run on different clocks (CPI monthly with a ~6-week lag, WALCL
weekly, RRP daily). Each indicator reports its frequency, age and a ``stale`` flag against a
per-frequency limit, and every derived figure records the observation date of each input it used
(``inputs_as_of``) plus any stale one (``stale_inputs``) — a composite is annotated, never refused,
and never silently mixes an old print. ``as_of`` filters by OBSERVATION date, not publication
date (no vintages), so a point-in-time read can still contain a print published after ``as_of``.

The CSV fetch is injected (``fetch_csv``) so the unit tests run fully offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ...analytics import (
    percentile_of_last,
    recession_probit,
    sahm_gap,
    trailing_negative_run,
    zscore_of_last,
)
from ...domain.models import MacroDerived, MacroIndicator, MacroSnapshot
from ..retry import BLOCKED, SCHEMA_ERROR, SourceUnavailable, unavailable_status, with_retry

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

DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"
QUARTERLY = "quarterly"

# How old a series' latest observation may be before it reads as stale. Observations are dated
# at the START of their period (a monthly print is dated the 1st) and published with a lag, so
# each limit is "one period + the normal publication lag + a few days of slack", checked against
# the real FRED data on 2026-10-12: CPI's previous print is ~75 days old the day before the next
# release, a weekly H.4.1/claims print ≤ ~12 days, a daily rate ≤ ~4 days across a long weekend.
STALE_AFTER_DAYS_DAILY = 7
STALE_AFTER_DAYS_WEEKLY = 14
STALE_AFTER_DAYS_MONTHLY = 80
STALE_AFTER_DAYS_QUARTERLY = 130  # GDPNow's quarter is dated its first day and runs ~4 months
_STALE_AFTER_DAYS = {
    DAILY: STALE_AFTER_DAYS_DAILY,
    WEEKLY: STALE_AFTER_DAYS_WEEKLY,
    MONTHLY: STALE_AFTER_DAYS_MONTHLY,
    QUARTERLY: STALE_AFTER_DAYS_QUARTERLY,
}

# Observation cadence per series (FRED's own frequency).
_FREQUENCY: dict[str, str] = {
    "FEDFUNDS": MONTHLY,
    "DGS10": DAILY,
    "DGS2": DAILY,
    "DGS3MO": DAILY,
    "T10Y2Y": DAILY,
    "UNRATE": MONTHLY,
    "CPIAUCSL": MONTHLY,
    "VIXCLS": DAILY,
    "T10YIE": DAILY,
    "DFII10": DAILY,
    "BAMLH0A0HYM2": DAILY,
    "WALCL": WEEKLY,
    "WTREGEN": WEEKLY,
    "RRPONTSYD": DAILY,
    "NFCI": WEEKLY,
    "STLFSI4": WEEKLY,
    "ICSA": WEEKLY,
    "IC4WSA": WEEKLY,
    "CCSA": WEEKLY,
    "GDPNOW": QUARTERLY,
    "CFNAI": MONTHLY,
    "CFNAIMA3": MONTHLY,
    "WEI": WEEKLY,
    "VXVCLS": DAILY,
    "T5YIFR": DAILY,
    "M2SL": MONTHLY,
    "SOFR": DAILY,
    "DTWEXBGS": DAILY,
    "DCOILBRENTEU": DAILY,
    "DCOILWTICO": DAILY,
    "DHHNGSP": DAILY,
    "PCOPPUSDM": MONTHLY,
    "OVXCLS": DAILY,
    "GVZCLS": DAILY,
}

# Series published on a slower clock than they are observed on get that release's tolerance:
# daily series released once a week (Fed H.10 dollar, EIA spot energy), continued claims (one
# week behind initial claims), and monthly prints released ~4 weeks after month-end (M2, CFNAI —
# ~85 days old just before their next print).
_PUBLICATION_LAG_STALE_AFTER_DAYS: dict[str, int] = {
    "DTWEXBGS": STALE_AFTER_DAYS_WEEKLY,
    "DCOILBRENTEU": STALE_AFTER_DAYS_WEEKLY,
    "DCOILWTICO": STALE_AFTER_DAYS_WEEKLY,
    "DHHNGSP": STALE_AFTER_DAYS_WEEKLY,
    "CCSA": STALE_AFTER_DAYS_WEEKLY + 7,
    "M2SL": STALE_AFTER_DAYS_MONTHLY + 15,
    "CFNAI": STALE_AFTER_DAYS_MONTHLY + 15,
    "CFNAIMA3": STALE_AFTER_DAYS_MONTHLY + 15,
}

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

Observations = list[tuple[date, Decimal]]


def stale_after_days(series_id: str) -> int | None:
    """Staleness limit in days for a series, or ``None`` when its frequency is unknown."""
    if series_id in _PUBLICATION_LAG_STALE_AFTER_DAYS:
        return _PUBLICATION_LAG_STALE_AFTER_DAYS[series_id]
    frequency = _FREQUENCY.get(series_id)
    return _STALE_AFTER_DAYS.get(frequency) if frequency else None


def _is_stale(series_id: str, observation_date: date, reference: date) -> bool | None:
    limit = stale_after_days(series_id)
    if limit is None:
        return None
    return (reference - observation_date).days > limit


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


def _check_header(csv_text: str, series_id: str) -> None:
    """The first line of a fredgraph CSV is ``observation_date,<SERIES>`` (``DATE,…`` on older
    exports). Anything else is not the series asked for — an HTML page (bot wall, error page
    served with 200) is ``blocked``, any other shape a ``schema_error``; never an empty series."""
    lines = csv_text.lstrip().splitlines()
    header = lines[0].strip() if lines else ""
    if header.startswith("<"):
        raise SourceUnavailable(BLOCKED, f"{series_id}: got HTML instead of a CSV")
    columns = [column.strip() for column in header.split(",")]
    if len(columns) < 2 or columns[1].upper() != series_id.upper():
        raise SourceUnavailable(
            SCHEMA_ERROR, f"{series_id}: unexpected CSV header {header[:60]!r}"
        )


def _observations(csv_text: str, as_of: date | None) -> Observations:
    """Parse a fredgraph CSV into (date, value) pairs at or before ``as_of``, oldest→newest.
    Missing observations (FRED's dot) are skipped. The whole series is kept — not just the last
    point — so the derived layer can compute YoY, z-scores, the Sahm gap, etc."""
    rows: Observations = []
    for line in csv_text.lstrip().splitlines()[1:]:  # skip the header row
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


def _point_on_or_before(obs: Observations, target: date) -> tuple[date, Decimal] | None:
    """Most recent (date, value) at or before ``target`` (obs assumed oldest→newest)."""
    result: tuple[date, Decimal] | None = None
    for point in obs:
        if point[0] <= target:
            result = point
        else:
            break
    return result


def _value_on_or_before(obs: Observations, target: date) -> Decimal | None:
    point = _point_on_or_before(obs, target)
    return point[1] if point else None


def _latest_common(
    first: Observations, second: Observations
) -> tuple[date, Decimal, Decimal] | None:
    """The latest date BOTH same-cadence series observed, with each value on it — so a spread or
    ratio never combines one series' Tuesday print with the other's Friday print."""
    second_by_date = dict(second)
    for observation_date, value in reversed(first):
        if observation_date in second_by_date:
            return observation_date, value, second_by_date[observation_date]
    return None


def _round(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, places)))


class _Provenance:
    """Records, per derived field, the observation date of each input and flags stale ones.

    ``reference`` is the read's point in time (as_of, else today). An input carried forward onto
    an anchor's date (TGA/RRP onto the WALCL date) is judged against that anchor instead."""

    def __init__(self, reference: date | None) -> None:
        self._reference = reference
        self.inputs_as_of: dict[str, dict[str, date]] = {}
        self.stale_inputs: dict[str, list[str]] = {}
        self._stale_detail: dict[str, tuple[date, int, int]] = {}

    def record(
        self,
        fields: tuple[str, ...],
        series_id: str,
        observation_date: date,
        anchor: date | None = None,
    ) -> None:
        for field in fields:
            self.inputs_as_of.setdefault(field, {})[series_id] = observation_date
        reference = anchor or self._reference
        limit = stale_after_days(series_id)
        if reference is None or limit is None:
            return
        age = (reference - observation_date).days
        if age <= limit:
            return
        for field in fields:
            stale = self.stale_inputs.setdefault(field, [])
            if series_id not in stale:
                stale.append(series_id)
        self._stale_detail[series_id] = (observation_date, age, limit)

    def notes(self) -> list[str]:
        notes = []
        for series_id, (observation_date, age, limit) in self._stale_detail.items():
            fields = sorted(f for f, ids in self.stale_inputs.items() if series_id in ids)
            notes.append(
                f"stale input: {series_id} last observed {observation_date.isoformat()} "
                f"({age}d old, limit {limit}d) feeds {', '.join(fields)}."
            )
        return notes


class FredMacro:
    def __init__(
        self,
        fetch_csv: Callable[[str], Awaitable[str]] | None = None,
        timeout: float = 15.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.5,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._fetch_csv = fetch_csv or self._default_fetch_csv
        self._today = today or (lambda: datetime.now(UTC).date())

    async def _default_fetch_csv(self, url: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_macro_context(self, as_of: date | None = None) -> MacroSnapshot:
        reference = as_of or self._today()
        results = await asyncio.gather(
            *(self._one(series_id, name, as_of, reference) for series_id, name in _SERIES),
            return_exceptions=True,
        )
        indicators: list[MacroIndicator] = []
        series: dict[str, Observations] = {}
        for result in results:
            if not isinstance(result, tuple):
                continue  # defensive: ``_one`` converts every failure into a flagged indicator
            indicator, observations = result
            indicators.append(indicator)
            if observations:
                series[indicator.series_id] = observations
        derived = _derive(series, reference)
        if as_of is not None:
            derived.notes.append(
                "as_of filters FRED by observation date, not publication date (no vintages): a "
                "print observed before as_of but published after it can still appear."
            )
        return MacroSnapshot(indicators=indicators, derived=derived, as_of=as_of)

    async def _one(
        self, series_id: str, name: str, as_of: date | None, reference: date
    ) -> tuple[MacroIndicator, Observations | None]:
        url = _CSV_URL.format(series=series_id)
        frequency = _FREQUENCY.get(series_id)
        try:
            csv_text = await with_retry(
                lambda: self._fetch_csv(url),
                attempts=self._retry_attempts,
                base_delay=self._retry_base_delay,
            )
            _check_header(csv_text, series_id)
            observations = _observations(csv_text, as_of)
        except SourceUnavailable as exc:
            # Keep the series in the snapshot but flag it: a rate-limited/timed-out series must
            # not vanish (which would read as "no such indicator") — value is null WITH a status.
            indicator = MacroIndicator(
                series_id=series_id,
                name=name,
                value=None,
                status=exc.status,
                frequency=frequency,
            )
            return indicator, None
        except Exception as exc:  # noqa: BLE001 — a non-transient failure (404, parse error…)
            # …must ALSO stay visible: null value with an ``unavailable: error`` status, exactly
            # like the transient case, instead of the series silently vanishing from the list.
            indicator = MacroIndicator(
                series_id=series_id,
                name=name,
                value=None,
                status=unavailable_status(exc),
                frequency=frequency,
            )
            return indicator, None
        if not observations:
            return MacroIndicator(series_id=series_id, name=name, frequency=frequency), None
        latest_date, latest_value = observations[-1]
        indicator = MacroIndicator(
            series_id=series_id,
            name=name,
            value=latest_value,
            observation_date=latest_date,
            frequency=frequency,
            age_days=(reference - latest_date).days,
            stale=_is_stale(series_id, latest_date, reference),
        )
        return indicator, observations


def _derive(series: dict[str, Observations], reference: date | None = None) -> MacroDerived:
    """Turn the raw FRED series into regime metrics. Every block is independently guarded so a
    missing or short series simply leaves its fields null (and, where useful, leaves a note).
    Each computed field records the observation date of every input it used, flagging stale
    ones against ``reference``."""
    derived = MacroDerived()
    notes: list[str] = []
    provenance = _Provenance(reference)

    cpi = series.get("CPIAUCSL")
    cpi_yoy: float | None = None
    if cpi and len(cpi) >= _CPI_YOY_MIN_MONTHS:
        latest_date, latest_value = cpi[-1]
        year_ago = _value_on_or_before(cpi, latest_date - timedelta(days=365))
        if year_ago and year_ago != 0:
            cpi_yoy = (float(latest_value) / float(year_ago) - 1) * 100
            derived.cpi_yoy = _round(cpi_yoy, 2)
            provenance.record(("cpi_yoy",), "CPIAUCSL", latest_date)
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
            provenance.record(("cpi_3m_annualized",), "CPIAUCSL", cpi[-1][0])

    if cpi_yoy is not None and cpi:
        fedfunds = series.get("FEDFUNDS")
        if fedfunds:
            derived.real_fed_funds = _round(float(fedfunds[-1][1]) - cpi_yoy, 2)
            provenance.record(("real_fed_funds",), "FEDFUNDS", fedfunds[-1][0])
            provenance.record(("real_fed_funds",), "CPIAUCSL", cpi[-1][0])
        dgs10 = series.get("DGS10")
        if dgs10:
            derived.real_10y = _round(float(dgs10[-1][1]) - cpi_yoy, 2)
            provenance.record(("real_10y",), "DGS10", dgs10[-1][0])
            provenance.record(("real_10y",), "CPIAUCSL", cpi[-1][0])

    unrate = series.get("UNRATE")
    if unrate:
        gap = sahm_gap([float(v) for _, v in unrate])
        if gap is not None:
            derived.sahm_gap = _round(gap, 2)
            derived.sahm_recession_signal = gap >= 0.50
            provenance.record(("sahm_gap", "sahm_recession_signal"), "UNRATE", unrate[-1][0])

    vix = series.get("VIXCLS")
    if vix and len(vix) >= 30:
        window = [float(v) for _, v in vix][-_VIX_WINDOW:]
        z = zscore_of_last(window)
        pct = percentile_of_last(window)
        derived.vix_zscore = _round(z, 2)
        derived.vix_percentile = _round(pct * 100, 1) if pct is not None else None
        provenance.record(("vix_zscore", "vix_percentile"), "VIXCLS", vix[-1][0])

    t10y2y = series.get("T10Y2Y")
    if t10y2y:
        spread_values = [float(v) for _, v in t10y2y]
        derived.yield_curve_inverted = spread_values[-1] < 0
        derived.yield_curve_days_inverted = trailing_negative_run(spread_values)
        provenance.record(
            ("yield_curve_inverted", "yield_curve_days_inverted"), "T10Y2Y", t10y2y[-1][0]
        )

    dgs10 = series.get("DGS10")
    dgs3mo = series.get("DGS3MO")
    curve_point = _latest_common(dgs10, dgs3mo) if dgs10 and dgs3mo else None
    if curve_point:
        curve_date, ten_year, three_month = curve_point
        spread = float(ten_year) - float(three_month)
        derived.recession_prob_12m = _round(recession_probit(spread) * 100, 1)
        provenance.record(("recession_prob_12m",), "DGS10", curve_date)
        provenance.record(("recession_prob_12m",), "DGS3MO", curve_date)
        notes.append(
            "recession_prob_12m: NY-Fed probit on the latest daily 10y-3m spread as a proxy for "
            "the model's monthly-average input."
        )

    breakeven = series.get("T10YIE")
    if breakeven:
        derived.inflation_expectations_10y = _round(float(breakeven[-1][1]), 2)
        provenance.record(("inflation_expectations_10y",), "T10YIE", breakeven[-1][0])
    real_yield = series.get("DFII10")
    if real_yield:
        derived.real_10y_exante = _round(float(real_yield[-1][1]), 2)
        provenance.record(("real_10y_exante",), "DFII10", real_yield[-1][0])
    credit = series.get("BAMLH0A0HYM2")
    if credit:
        derived.credit_spread_hy = _round(float(credit[-1][1]), 2)
        credit_fields: tuple[str, ...] = ("credit_spread_hy",)
        if len(credit) >= 30:
            window = [float(v) for _, v in credit][-_CREDIT_WINDOW:]
            z = zscore_of_last(window)
            derived.credit_spread_hy_zscore = _round(z, 2)
            credit_fields += ("credit_spread_hy_zscore",)
        provenance.record(credit_fields, "BAMLH0A0HYM2", credit[-1][0])

    _derive_net_liquidity(series, derived, notes, provenance)

    nfci = series.get("NFCI")
    if nfci:
        # NFCI is already standardized (mean 0); positive = tighter-than-average conditions.
        derived.financial_conditions_tight = float(nfci[-1][1]) > 0
        provenance.record(("financial_conditions_tight",), "NFCI", nfci[-1][0])

    claims_4wk = series.get("IC4WSA")
    if claims_4wk and len(claims_4wk) >= _CLAIMS_YOY_MIN_WEEKS:
        latest_date, latest_value = claims_4wk[-1]
        year_ago = _value_on_or_before(claims_4wk, latest_date - timedelta(days=365))
        if year_ago and year_ago != 0:
            derived.initial_claims_4wk_yoy = _round(
                (float(latest_value) / float(year_ago) - 1) * 100, 2
            )
            provenance.record(("initial_claims_4wk_yoy",), "IC4WSA", latest_date)

    cfnai_ma3 = series.get("CFNAIMA3")
    if cfnai_ma3:
        derived.cfnai_recession_signal = float(cfnai_ma3[-1][1]) < _CFNAI_RECESSION_THRESHOLD
        provenance.record(("cfnai_recession_signal",), "CFNAIMA3", cfnai_ma3[-1][0])

    vxv = series.get("VXVCLS")
    vix_point = _latest_common(vxv, vix) if vxv and vix else None
    if vix_point:
        vix_date, three_month_vix, vix_latest = vix_point
        if vix_latest > 0:
            ratio = float(three_month_vix) / float(vix_latest)
            derived.vix_term_structure = _round(ratio, 2)
            derived.vix_backwardation = ratio < 1.0
            for series_id in ("VXVCLS", "VIXCLS"):
                provenance.record(("vix_term_structure", "vix_backwardation"), series_id, vix_date)

    forward_inflation = series.get("T5YIFR")
    if forward_inflation:
        derived.inflation_5y5y = _round(float(forward_inflation[-1][1]), 2)
        provenance.record(("inflation_5y5y",), "T5YIFR", forward_inflation[-1][0])

    m2 = series.get("M2SL")
    if m2 and len(m2) >= _M2_YOY_MIN_MONTHS:
        latest_date, latest_value = m2[-1]
        year_ago = _value_on_or_before(m2, latest_date - timedelta(days=365))
        if year_ago and year_ago != 0:
            derived.m2_yoy = _round((float(latest_value) / float(year_ago) - 1) * 100, 2)
            provenance.record(("m2_yoy",), "M2SL", latest_date)

    dollar = series.get("DTWEXBGS")
    if dollar and len(dollar) >= _DOLLAR_MIN_POINTS:
        window = [float(v) for _, v in dollar][-_DOLLAR_WINDOW:]
        derived.dollar_broad_zscore = _round(zscore_of_last(window), 2)
        provenance.record(("dollar_broad_zscore",), "DTWEXBGS", dollar[-1][0])

    brent = series.get("DCOILBRENTEU")
    wti = series.get("DCOILWTICO")
    crude_point = _latest_common(brent, wti) if brent and wti else None
    if crude_point:
        crude_date, brent_price, wti_price = crude_point
        derived.brent_wti_spread = _round(float(brent_price) - float(wti_price), 2)
        for series_id in ("DCOILBRENTEU", "DCOILWTICO"):
            provenance.record(("brent_wti_spread",), series_id, crude_date)

    derived.inputs_as_of = provenance.inputs_as_of
    derived.stale_inputs = provenance.stale_inputs
    derived.notes = notes + provenance.notes()
    return derived


def _derive_net_liquidity(
    series: dict[str, Observations],
    derived: MacroDerived,
    notes: list[str],
    provenance: _Provenance,
) -> None:
    """Fed net liquidity = WALCL − TGA − RRP.

    UNIT GOTCHA: WALCL and WTREGEN are in $ MILLIONS, but RRPONTSYD is in $ BILLIONS — multiply
    RRP by 1000 to bring it to $M before subtracting, or the result is off by three orders of
    magnitude. The net series is built on WALCL's (weekly) observation dates, carrying the most
    recent TGA/RRP value forward to each date; the carried values' dates are recorded and judged
    against that WALCL date, so an RRP print weeks older than the balance sheet is flagged."""
    walcl = series.get("WALCL")
    treasury_account = series.get("WTREGEN")
    reverse_repo = series.get("RRPONTSYD")
    if not (walcl and treasury_account and reverse_repo):
        return
    net_series: list[float] = []
    last_dates: tuple[date, date, date] | None = None
    for observation_date, total_assets in walcl:
        tga = _point_on_or_before(treasury_account, observation_date)
        rrp = _point_on_or_before(reverse_repo, observation_date)
        if tga is None or rrp is None:
            continue
        net_series.append(float(total_assets) - float(tga[1]) - float(rrp[1]) * 1000)
        last_dates = (observation_date, tga[0], rrp[0])
    if not net_series or last_dates is None:
        return
    fields: tuple[str, ...] = ("net_liquidity",)
    derived.net_liquidity = _round(net_series[-1], 0)
    notes.append(
        "net_liquidity: WALCL − TGA − RRP, mixing the weekly balance-sheet/TGA prints with "
        "the daily RRP carried forward to each WALCL date; RRP (in $B) is scaled ×1000 to "
        "the $M unit of WALCL/TGA."
    )
    if len(net_series) >= 2:
        derived.net_liquidity_wow = _round(net_series[-1] - net_series[-2], 0)
        fields += ("net_liquidity_wow",)
    if len(net_series) >= _NET_LIQ_MIN_ZSCORE_WEEKS:
        z = zscore_of_last(net_series[-_NET_LIQ_WINDOW:])
        derived.net_liquidity_zscore = _round(z, 2)
        fields += ("net_liquidity_zscore",)
    anchor, tga_date, rrp_date = last_dates
    provenance.record(fields, "WALCL", anchor)
    provenance.record(fields, "WTREGEN", tga_date, anchor=anchor)
    provenance.record(fields, "RRPONTSYD", rrp_date, anchor=anchor)
