from datetime import date, timedelta
from decimal import Decimal

from scout.adapters.fred import FredMacro

# Per-series canned CSVs; the dot marks a missing observation (FRED's convention).
_CSV = {
    "FEDFUNDS": "DATE,FEDFUNDS\n2026-04-01,3.62\n2026-05-01,3.63\n",
    "DGS10": "DATE,DGS10\n2026-06-23,4.45\n2026-06-24,.\n2026-06-25,4.47\n",
    "DGS2": "DATE,DGS2\n2026-06-25,4.10\n",
    "DGS3MO": "DATE,DGS3MO\n2026-06-25,4.30\n",
    "T10Y2Y": "DATE,T10Y2Y\n2026-06-25,0.37\n",
    "UNRATE": "DATE,UNRATE\n2026-05-01,4.3\n",
    "CPIAUCSL": "DATE,CPIAUCSL\n2026-05-01,322.1\n",
    "VIXCLS": "DATE,VIXCLS\n2026-06-25,18.4\n",
    "T10YIE": "DATE,T10YIE\n2026-06-25,2.35\n",
    "DFII10": "DATE,DFII10\n2026-06-25,2.05\n",
    "BAMLH0A0HYM2": "DATE,BAMLH0A0HYM2\n2026-06-25,3.10\n",
    # Macro source expansion (ADR-013): one point each, enough to wire the series count.
    "WALCL": "DATE,WALCL\n2026-06-18,6000000\n",
    "WTREGEN": "DATE,WTREGEN\n2026-06-18,900000\n",
    "RRPONTSYD": "DATE,RRPONTSYD\n2026-06-25,25.0\n",
    "NFCI": "DATE,NFCI\n2026-06-20,-0.20\n",
    "STLFSI4": "DATE,STLFSI4\n2026-06-20,-0.50\n",
    "ICSA": "DATE,ICSA\n2026-06-21,230000\n",
    "IC4WSA": "DATE,IC4WSA\n2026-06-21,228000\n",
    "CCSA": "DATE,CCSA\n2026-06-14,1900000\n",
    "GDPNOW": "DATE,GDPNOW\n2026-06-25,2.4\n",
    "CFNAI": "DATE,CFNAI\n2026-05-01,0.10\n",
    "CFNAIMA3": "DATE,CFNAIMA3\n2026-05-01,0.05\n",
    "WEI": "DATE,WEI\n2026-06-21,2.1\n",
    "VXVCLS": "DATE,VXVCLS\n2026-06-25,20.5\n",
    "T5YIFR": "DATE,T5YIFR\n2026-06-25,2.40\n",
    "M2SL": "DATE,M2SL\n2026-05-01,21000\n",
    "SOFR": "DATE,SOFR\n2026-06-25,4.32\n",
    "DTWEXBGS": "DATE,DTWEXBGS\n2026-06-25,120.5\n",
    "DCOILBRENTEU": "DATE,DCOILBRENTEU\n2026-06-25,80.0\n",
    "DCOILWTICO": "DATE,DCOILWTICO\n2026-06-25,76.0\n",
    "DHHNGSP": "DATE,DHHNGSP\n2026-06-25,3.10\n",
    "PCOPPUSDM": "DATE,PCOPPUSDM\n2026-05-01,9500\n",
    "OVXCLS": "DATE,OVXCLS\n2026-06-25,35.0\n",
    "GVZCLS": "DATE,GVZCLS\n2026-06-25,16.0\n",
}


def _fetch_factory(store=_CSV):
    async def _fetch(url: str) -> str:
        series = url.split("id=")[-1]
        return store[series]

    return _fetch


async def test_returns_all_series_latest():
    snapshot = await FredMacro(fetch_csv=_fetch_factory()).get_macro_context()
    by_id = {i.series_id: i for i in snapshot.indicators}
    assert len(snapshot.indicators) == 34
    assert by_id["FEDFUNDS"].value == Decimal("3.63")
    # The latest DGS10 row is valid; the dot row before it is skipped.
    assert by_id["DGS10"].value == Decimal("4.47")
    assert by_id["DGS10"].observation_date == date(2026, 6, 25)


async def test_as_of_picks_earlier_observation():
    snapshot = await FredMacro(fetch_csv=_fetch_factory()).get_macro_context(
        as_of=date(2026, 6, 23)
    )
    by_id = {i.series_id: i for i in snapshot.indicators}
    assert by_id["DGS10"].value == Decimal("4.45")
    assert by_id["DGS10"].observation_date == date(2026, 6, 23)


async def test_non_transient_failure_is_flagged_not_dropped():
    # A genuine (non-transient) failure — a 404, a parse bug — must surface as a null value
    # WITH an error status, exactly like the rate-limited case. A silently missing indicator
    # reads as "no such series", which is a lie.
    async def flaky_fetch(url: str) -> str:
        if "VIXCLS" in url:
            raise RuntimeError("series fetch failed")
        return _CSV[url.split("id=")[-1]]

    snapshot = await FredMacro(fetch_csv=flaky_fetch).get_macro_context()
    by_id = {i.series_id: i for i in snapshot.indicators}
    assert len(snapshot.indicators) == 34  # nothing dropped
    assert by_id["VIXCLS"].value is None
    assert by_id["VIXCLS"].status == "unavailable: error"
    assert by_id["FEDFUNDS"].value == Decimal("3.63")
    assert by_id["FEDFUNDS"].status is None


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHttpError(Exception):
    """Mimics httpx.HTTPStatusError enough for the retry classifier (has .response.status_code)."""

    def __init__(self, status_code: int) -> None:
        self.response = _Resp(status_code)
        super().__init__(f"HTTP {status_code}")


async def test_rate_limited_series_is_flagged_not_dropped():
    calls = {"n": 0}

    async def rate_limited_fetch(url: str) -> str:
        if "VIXCLS" in url:
            calls["n"] += 1
            raise _FakeHttpError(429)
        return _CSV[url.split("id=")[-1]]

    snapshot = await FredMacro(
        fetch_csv=rate_limited_fetch, retry_attempts=3, retry_base_delay=0
    ).get_macro_context()
    by_id = {i.series_id: i for i in snapshot.indicators}
    # The series is RETRIED the full budget, then kept WITH a status — not silently dropped.
    assert calls["n"] == 3
    assert "VIXCLS" in by_id
    assert by_id["VIXCLS"].value is None
    assert by_id["VIXCLS"].status == "unavailable: rate_limited"
    # The healthy series are unaffected.
    assert by_id["FEDFUNDS"].value == Decimal("3.63")
    assert by_id["FEDFUNDS"].status is None


def _monthly_csv(series: str, start_year: int, start_month: int, values: list) -> str:
    lines = [f"DATE,{series}"]
    year, month = start_year, start_month
    for value in values:
        lines.append(f"{year:04d}-{month:02d}-01,{value}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return "\n".join(lines) + "\n"


def _daily_csv(series: str, end: date, values: list) -> str:
    lines = [f"DATE,{series}"]
    n = len(values)
    for i, value in enumerate(values):
        observation = end - timedelta(days=(n - 1 - i))
        lines.append(f"{observation.isoformat()},{value}")
    return "\n".join(lines) + "\n"


# A store with real history so the derived layer has something to chew on.
_END = date(2026, 6, 25)
_RICH_CSV = {
    # 13 monthly points: 300.0 (2025-05) → 309.0 (2026-05) = exactly +3.0% YoY.
    "CPIAUCSL": _monthly_csv("CPIAUCSL", 2025, 5, [round(300 + 0.75 * i, 2) for i in range(13)]),
    # Unemployment rising off a ~3.5% trough → Sahm gap clears 0.5.
    "UNRATE": _monthly_csv(
        "UNRATE", 2025, 5,
        [3.5, 3.5, 3.5, 3.6, 3.6, 3.7, 3.8, 3.9, 4.0, 4.1, 4.2, 4.3, 4.35, 4.4],
    ),
    "FEDFUNDS": _monthly_csv("FEDFUNDS", 2026, 4, [4.2, 4.3]),
    "DGS10": _daily_csv("DGS10", _END, [4.4, 4.5]),
    "DGS3MO": _daily_csv("DGS3MO", _END, [4.6]),
    "DGS2": _daily_csv("DGS2", _END, [4.0]),
    # Curve newly inverted: last three observations below zero.
    "T10Y2Y": _daily_csv("T10Y2Y", _END, [0.3, 0.2, 0.1, -0.05, -0.1, -0.2]),
    # VIX flat then a spike on the latest print → high z-score, top of its window.
    "VIXCLS": _daily_csv("VIXCLS", _END, [15.0] * 39 + [30.0]),
    "T10YIE": _daily_csv("T10YIE", _END, [2.30, 2.35]),
    "DFII10": _daily_csv("DFII10", _END, [2.00, 2.05]),
    # Credit spread flat then a blowout on the latest print → high z-score (stress).
    "BAMLH0A0HYM2": _daily_csv("BAMLH0A0HYM2", _END, [3.0] * 39 + [6.0]),
}


async def test_derived_regime_metrics():
    snapshot = await FredMacro(fetch_csv=_fetch_factory(_RICH_CSV)).get_macro_context()
    d = snapshot.derived
    assert d is not None
    # Inflation YoY and the ex-post real rates built on it.
    assert d.cpi_yoy == Decimal("3.0")
    assert d.cpi_3m_annualized is not None
    assert d.real_fed_funds == Decimal("1.3")  # 4.3 − 3.0
    assert d.real_10y == Decimal("1.5")  # 4.5 − 3.0
    # Sahm: rising unemployment trips the recession signal.
    assert d.sahm_gap is not None and d.sahm_gap > 0
    assert d.sahm_recession_signal is True
    # VIX regime: latest spike sits at the top of its window.
    assert d.vix_zscore is not None and d.vix_zscore > 0
    assert d.vix_percentile == Decimal("100.0")
    # Yield curve freshly inverted for three observations.
    assert d.yield_curve_inverted is True
    assert d.yield_curve_days_inverted == 3
    # Recession probit on the −0.1 (10y−3m) spread ≈ 32%.
    assert d.recession_prob_12m is not None
    assert Decimal("31.0") <= d.recession_prob_12m <= Decimal("33.0")
    assert any("recession_prob_12m" in note for note in d.notes)
    # New FRED series: breakeven, ex-ante real yield, and the credit-spread stress regime.
    assert d.inflation_expectations_10y == Decimal("2.35")
    assert d.real_10y_exante == Decimal("2.05")
    assert d.credit_spread_hy == Decimal("6.0")  # latest (blowout) print
    assert d.credit_spread_hy_zscore is not None and d.credit_spread_hy_zscore > 2


async def test_cpi_3m_annualized_uses_exactly_three_monthly_prints():
    # Monthly prints dated on the 1st: Jan=90, Feb=100, Mar/Apr/May → 103. The 3m window is
    # Feb→May = (103/100)^4 − 1 = 12.55%. The old calendar lookup (latest − 92 days =
    # Jan 29) landed on the JANUARY print → (103/90)^4 − 1 ≈ 71% — a 4-month window raised
    # to the 4th power, confidently mislabelled as "3m annualized".
    store = {"CPIAUCSL": _monthly_csv("CPIAUCSL", 2026, 1, [90, 100, 101, 102, 103])}
    d = (await FredMacro(fetch_csv=_fetch_factory(store)).get_macro_context()).derived
    assert d is not None
    assert d.cpi_3m_annualized == Decimal("12.55")


async def test_derived_is_empty_when_series_too_short():
    # The minimal canned store has 1-2 points per series → YoY/Sahm/z-score can't compute.
    snapshot = await FredMacro(fetch_csv=_fetch_factory()).get_macro_context()
    d = snapshot.derived
    assert d is not None
    assert d.cpi_yoy is None  # only one CPI point
    assert d.sahm_gap is None  # only one unemployment point
    assert d.vix_zscore is None  # only one VIX point


def _weekly_csv(series: str, end: date, values: list) -> str:
    lines = [f"DATE,{series}"]
    n = len(values)
    for i, value in enumerate(values):
        observation = end - timedelta(days=7 * (n - 1 - i))
        lines.append(f"{observation.isoformat()},{value}")
    return "\n".join(lines) + "\n"


# --- Macro source expansion (ADR-013) focused math --------------------------------


async def test_net_liquidity_unit_alignment():
    # WALCL/WTREGEN in $M, RRPONTSYD in $B → RRP must be scaled ×1000. Two weekly points so
    # WoW is defined: net rises from 4,920,000 to 5,075,000.
    store = {
        "WALCL": _daily_csv("WALCL", _END, [5_900_000, 6_000_000]),
        "WTREGEN": _daily_csv("WTREGEN", _END, [950_000, 900_000]),
        "RRPONTSYD": _daily_csv("RRPONTSYD", _END, [30.0, 25.0]),
    }
    d = (await FredMacro(fetch_csv=_fetch_factory(store)).get_macro_context()).derived
    assert d is not None
    # 6,000,000 − 900,000 − 25.0×1000 = 5,075,000 (proves the ×1000 unit alignment).
    assert d.net_liquidity == Decimal("5075000")
    # Rose from 4,920,000 (= 5,900,000 − 950,000 − 30.0×1000) → +155,000.
    assert d.net_liquidity_wow == Decimal("155000")
    assert any("net_liquidity" in note for note in d.notes)


async def test_net_liquidity_null_when_leg_missing():
    store = {
        "WALCL": _daily_csv("WALCL", _END, [6_000_000]),
        "WTREGEN": _daily_csv("WTREGEN", _END, [900_000]),
        # RRPONTSYD absent → the whole net-liquidity block must stay null, not guess.
    }
    d = (await FredMacro(fetch_csv=_fetch_factory(store)).get_macro_context()).derived
    assert d is not None
    assert d.net_liquidity is None
    assert d.net_liquidity_wow is None


async def test_vix_term_structure_contango_and_backwardation():
    contango = {
        "VXVCLS": _daily_csv("VXVCLS", _END, [22]),
        "VIXCLS": _daily_csv("VIXCLS", _END, [20]),
    }
    d = (await FredMacro(fetch_csv=_fetch_factory(contango)).get_macro_context()).derived
    assert d.vix_term_structure == Decimal("1.1")  # 22 / 20
    assert d.vix_backwardation is False
    backwardation = {
        "VXVCLS": _daily_csv("VXVCLS", _END, [18]),
        "VIXCLS": _daily_csv("VIXCLS", _END, [20]),
    }
    d = (await FredMacro(fetch_csv=_fetch_factory(backwardation)).get_macro_context()).derived
    assert d.vix_term_structure == Decimal("0.9")  # 18 / 20
    assert d.vix_backwardation is True


async def test_m2_yoy():
    # 13 monthly points: 100 a year ago (2025-05) → 110 latest (2026-05) = +10.0% YoY.
    store = {"M2SL": _monthly_csv("M2SL", 2025, 5, [100] * 12 + [110])}
    d = (await FredMacro(fetch_csv=_fetch_factory(store)).get_macro_context()).derived
    assert d.m2_yoy == Decimal("10.0")


async def test_initial_claims_4wk_yoy():
    # 54 weekly points so a year-ago (≈ −365d) observation exists: 200 then → 240 now = +20.0%.
    store = {"IC4WSA": _weekly_csv("IC4WSA", _END, [200] + [210] * 52 + [240])}
    d = (await FredMacro(fetch_csv=_fetch_factory(store)).get_macro_context()).derived
    assert d.initial_claims_4wk_yoy == Decimal("20.0")


async def test_brent_wti_spread():
    store = {
        "DCOILBRENTEU": _daily_csv("DCOILBRENTEU", _END, [80]),
        "DCOILWTICO": _daily_csv("DCOILWTICO", _END, [76]),
    }
    d = (await FredMacro(fetch_csv=_fetch_factory(store)).get_macro_context()).derived
    assert d.brent_wti_spread == Decimal("4.0")


async def test_financial_conditions_tight_threshold():
    tight = {"NFCI": _daily_csv("NFCI", _END, [0.25])}
    d = (await FredMacro(fetch_csv=_fetch_factory(tight)).get_macro_context()).derived
    assert d.financial_conditions_tight is True
    loose = {"NFCI": _daily_csv("NFCI", _END, [-0.30])}
    d = (await FredMacro(fetch_csv=_fetch_factory(loose)).get_macro_context()).derived
    assert d.financial_conditions_tight is False


async def test_cfnai_recession_signal_threshold():
    recession = {"CFNAIMA3": _daily_csv("CFNAIMA3", _END, [-0.85])}
    d = (await FredMacro(fetch_csv=_fetch_factory(recession)).get_macro_context()).derived
    assert d.cfnai_recession_signal is True  # −0.85 < −0.70
    expansion = {"CFNAIMA3": _daily_csv("CFNAIMA3", _END, [-0.50])}
    d = (await FredMacro(fetch_csv=_fetch_factory(expansion)).get_macro_context()).derived
    assert d.cfnai_recession_signal is False  # −0.50 not below −0.70
