"""Domain models — agnostic to the concrete data source.

Monetary and ratio values use ``Decimal`` to avoid floating-point noise (mirrors the
agentic-trading-mcp convention). Every research model carries an optional ``as_of`` date: when
``None`` the data is the latest/real-time read; when set, it is (best-effort) the snapshot
as of that date. ``as_of`` is what lets the calling agent compose two stateless reads into
a "what changed since I bought?" diff — Scout never stores the date, it receives it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

# --- Equities / company / macro / sentiment models ----------------------------------------
# Result models for the equities side (quotes, fundamentals, technicals, macro, ownership,
# options, news, attention). The crypto models start lower down (search "Crypto models").
# When adding a model, put it under the matching section, not at the end of the file.


class Period(StrEnum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class MoverRow(BaseModel):
    symbol: str
    name: str | None = None
    price: Decimal | None = None
    change_percent: Decimal | None = None
    volume: int | None = None


class MoversList(BaseModel):
    """Market-wide top gainers / losers / most-active — discovery without a symbol in hand."""

    category: str
    movers: list[MoverRow] = []


class SymbolMatch(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    type: str | None = None


class SymbolSearch(BaseModel):
    """Tickers matching a free-text query (company name, partial ticker)."""

    query: str
    matches: list[SymbolMatch] = []


class StockSplit(BaseModel):
    """A single executed stock split. ``ratio`` is shares-out per old share (e.g. 10 for 10:1)."""

    date: date
    ratio: Decimal | None = Field(
        default=None, description="New shares per old share — 10 means a 10:1 split, 0.1 a 1:10."
    )


class CompanySnapshot(BaseModel):
    """A light, single-call portrait: price, day move and the key multiples."""

    symbol: str
    name: str | None = None
    currency: str = "USD"
    price: Decimal | None = None
    previous_close: Decimal | None = None
    change: Decimal | None = None
    change_percent: Decimal | None = None
    market_cap: Decimal | None = None
    pe_ratio: Decimal | None = None
    forward_pe: Decimal | None = None
    dividend_yield: Decimal | None = Field(
        default=None, description="As reported by the source (typically a fraction, e.g. 0.0044)."
    )
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    quote_time: datetime | None = Field(
        default=None,
        description=(
            "Source timestamp of the live quote (yfinance regularMarketTime). Lets a robot tell a "
            "true real-time price from a stale weekend/holiday last-session close — both otherwise "
            "look identical on the live (as_of=None) path. None on a historical (as_of) read."
        ),
    )
    market_state: str | None = Field(
        default=None,
        description="Market session for the live quote (e.g. REGULAR, CLOSED, PRE, POST).",
    )
    recent_splits: list[StockSplit] = Field(
        default_factory=list,
        description=(
            "Genuinely recent stock splits (newest first), within ~24 months of as_of (or "
            "today), so a split-adjusted price isn't mistaken for 'wrong' against pre-split "
            "memory. Empty when the stock hasn't split in that window — older splits are "
            "history, not 'recent'. Price and 52-week range are already split-adjusted by the "
            "source."
        ),
    )
    as_of: date | None = None


class Fundamentals(BaseModel):
    """Core income-statement / balance-sheet / cash-flow figures plus derived margins."""

    symbol: str
    period: Period
    fiscal_period_end: date | None = None
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    net_margin: Decimal | None = None
    total_debt: Decimal | None = None
    total_cash: Decimal | None = None
    total_assets: Decimal | None = None
    stockholders_equity: Decimal | None = None
    free_cash_flow: Decimal | None = None
    market_cap: Decimal | None = None  # current market cap; only set on a live read (as_of=None)
    # --- Derived (raw-number measures, no verdict). Cap-based ones are live-read only, because
    # the source has no historical market cap to pair with a past statement. ---
    net_debt: Decimal | None = None  # total_debt − total_cash (negative = net cash position)
    net_debt_to_fcf: Decimal | None = None  # years of FCF to clear net debt (null if FCF ≤ 0)
    fcf_margin: Decimal | None = None  # free_cash_flow / revenue
    fcf_yield: Decimal | None = None  # free_cash_flow / market_cap (live read only)
    earnings_yield: Decimal | None = None  # net_income / market_cap (live read only)
    enterprise_value: Decimal | None = None  # market_cap + total_debt − total_cash (live read only)
    ev_to_ebit: Decimal | None = None  # EV / operating_income (EBIT proxy; null if EBIT ≤ 0)
    ev_to_sales: Decimal | None = None  # EV / revenue
    ebit_to_ev: Decimal | None = None  # operating_income / EV (Greenblatt-style earnings yield)
    gross_profitability: Decimal | None = None  # gross_profit / total_assets (Novy-Marx quality)
    roic_pretax: Decimal | None = None  # operating_income / (total_debt + equity − cash)
    # --- Liquidity / leverage / coverage (statement-only unless noted) ---
    current_assets: Decimal | None = None
    current_liabilities: Decimal | None = None
    retained_earnings: Decimal | None = None
    depreciation_amortization: Decimal | None = None
    ebitda: Decimal | None = None  # operating_income (EBIT) + D&A
    current_ratio: Decimal | None = None  # current_assets / current_liabilities
    working_capital: Decimal | None = None  # current_assets − current_liabilities
    debt_to_equity: Decimal | None = None  # total_debt / stockholders_equity
    net_debt_to_ebitda: Decimal | None = None  # net_debt / EBITDA (leverage; null if EBITDA ≤ 0)
    ev_to_ebitda: Decimal | None = None  # enterprise_value / EBITDA (live read only)
    interest_coverage: Decimal | None = None  # EBIT / interest expense (null if no interest)
    altman_z: Decimal | None = None  # Altman Z″ (book-equity variant; >2.6 safe, <1.1 distress)
    as_of: date | None = None


class DividendPayment(BaseModel):
    ex_date: date
    amount: Decimal


class DividendHistory(BaseModel):
    """The income story: payment history, trailing yield, growth streak and any cut.

    Pure data — no judgment about whether the dividend is "safe". The agent reads the
    streak/cut/yield and concludes.
    """

    symbol: str
    currency: str = "USD"
    trailing_12m: Decimal | None = Field(
        default=None, description="Sum of dividends paid in the trailing 12 months."
    )
    trailing_yield: Decimal | None = Field(
        default=None, description="trailing_12m / price, if a price is available."
    )
    growth_streak_years: int | None = Field(
        default=None, description="Consecutive full calendar years of non-decreasing dividends."
    )
    had_cut: bool | None = Field(
        default=None, description="Whether any year-over-year cut is present in the history."
    )
    next_ex_dividend: date | None = None
    payments: list[DividendPayment] = []
    as_of: date | None = None


class ComparisonRow(BaseModel):
    symbol: str
    name: str | None = None
    price: Decimal | None = None
    market_cap: Decimal | None = None
    pe_ratio: Decimal | None = None
    forward_pe: Decimal | None = None
    dividend_yield: Decimal | None = None
    revenue: Decimal | None = None
    net_margin: Decimal | None = None
    sector: str | None = None
    note: str | None = None


class Comparison(BaseModel):
    """Several symbols side by side — a parallel gather of snapshot + fundamentals per name."""

    symbols: list[str]
    as_of: date | None = None
    rows: list[ComparisonRow] = []


class ClassificationItem(BaseModel):
    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: Decimal | None = None
    note: str | None = None


class Classification(BaseModel):
    """Sector/industry/cap per symbol, in one call — for the agent to aggregate exposure."""

    items: list[ClassificationItem] = []
    as_of: date | None = None


class DigestNewsItem(BaseModel):
    symbol: str
    title: str | None = None
    publisher: str | None = None
    published: datetime | None = None
    url: str | None = None


class NewsDigest(BaseModel):
    """Material headlines across several symbols in one call, newest first."""

    symbols: list[str]
    items: list[DigestNewsItem] = []
    notes: list[str] = []


class CalendarEvent(BaseModel):
    symbol: str
    type: str  # "earnings" | "ex_dividend"
    date: date


class MarketCalendar(BaseModel):
    """Upcoming earnings and ex-dividend dates across several symbols, sorted by date."""

    symbols: list[str]
    events: list[CalendarEvent] = []
    notes: list[str] = []


class CorrelationMatrix(BaseModel):
    """Pairwise return correlation between symbols — diversification you can't see per-symbol."""

    symbols: list[str]
    period: str
    days: int | None = None
    matrix: dict[str, dict[str, float | None]] = {}
    notes: list[str] = []


class Filing(BaseModel):
    """A single SEC filing (10-K, 10-Q, 8-K, ...) with a link to the primary document."""

    form: str
    filing_date: date
    report_date: date | None = None
    accession: str
    primary_document: str | None = None
    description: str | None = None
    url: str | None = None


class FilingsList(BaseModel):
    """Recent SEC EDGAR filings for a company — authoritative, straight from the source."""

    symbol: str
    cik: str | None = None
    name: str | None = None
    filings: list[Filing] = []


class PriceBar(BaseModel):
    date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None


class PriceHistory(BaseModel):
    """OHLCV bars for a symbol over a range/interval."""

    symbol: str
    interval: str
    bars: list[PriceBar] = []
    as_of: date | None = None
    source: str | None = Field(
        default=None,
        description=(
            "Which upstream actually served the bars (e.g. 'yfinance' / 'stooq') — set by the "
            "fallback wrapper so downstream provenance claims stay honest. None = not stamped."
        ),
    )


class Technicals(BaseModel):
    """Computed technical indicators — raw numbers, no trend verdict (that's the agent's call)."""

    symbol: str
    as_of: date | None = None
    last_price: Decimal | None = None
    sma_50: Decimal | None = None
    sma_200: Decimal | None = None
    mayer_multiple: Decimal | None = None  # price / SMA(200) — the Mayer Multiple (cycle gauge)
    ema_20: Decimal | None = None
    rsi_14: Decimal | None = None
    macd: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_histogram: Decimal | None = None
    atr_14: Decimal | None = None
    atr_pct: Decimal | None = None  # ATR(14) / last_price — cross-asset comparable "agitation"
    week52_high: Decimal | None = None
    week52_low: Decimal | None = None
    range_position_52w: Decimal | None = None  # 0 (at 52w low) … 1 (at 52w high)
    volatility_annualized: Decimal | None = None  # close-to-close, annualized
    volatility_ohlc: Decimal | None = None  # OHLC estimator (see volatility_estimator)
    volatility_estimator: str | None = None  # "yang_zhang" (equities) | "rogers_satchell" (crypto)
    sharpe_ratio: Decimal | None = None  # annualized, risk-free=0 (excess-over-zero)
    sortino_ratio: Decimal | None = None  # annualized, downside-deviation based
    max_drawdown: Decimal | None = None  # worst peak-to-trough, as a fraction (≤ 0)
    max_drawdown_bars: int | None = None  # longest underwater stretch, in bars
    calmar_ratio: Decimal | None = None  # annualized return / |max drawdown|
    return_skew: Decimal | None = None  # tail asymmetry of returns
    return_kurtosis: Decimal | None = None  # excess kurtosis (>0 = fat tails)
    momentum_3m: Decimal | None = None
    momentum_6m: Decimal | None = None
    momentum_12m: Decimal | None = None
    momentum_12_1: Decimal | None = None  # 12-month return skipping the last month
    annualization_factor: int | None = None  # 252 equities / 365 crypto — context for the ratios
    bars_used: int | None = None


class CompanyDossier(BaseModel):
    """A consolidated, single-call portrait that fans several reads out in parallel.

    The flagship "research many things about a company at once" tool: snapshot, fundamentals,
    dividends, technicals, earnings, analyst view and news gathered concurrently and returned
    together. ``notes`` carries any partial failure so the dossier degrades gracefully.
    """

    symbol: str
    as_of: date | None = None
    snapshot: CompanySnapshot | None = None
    fundamentals: Fundamentals | None = None
    dividends: DividendHistory | None = None
    technicals: Technicals | None = None
    earnings: EarningsInfo | None = None
    analyst: AnalystView | None = None
    news: NewsList | None = None
    notes: list[str] = []


class MacroIndicator(BaseModel):
    series_id: str
    name: str
    value: Decimal | None = None
    observation_date: date | None = None
    status: str | None = Field(
        default=None,
        description=(
            "Set when the series couldn't be fetched (e.g. 'unavailable: rate_limited' / "
            "'unavailable: timeout') — distinct from a genuine null value. None means fetched OK."
        ),
    )


class MacroDerived(BaseModel):
    """Regime numbers computed from the FRED series the snapshot already fetched — the raw
    levels turned into something interpretable (a CPI index of 322 means nothing alone; its
    YoY does). Measures, not a verdict: it reports a recession probability, never "go to cash".
    Each field is null when its series is missing or too short; `notes` carries any caveat.
    """

    cpi_yoy: Decimal | None = None  # CPI inflation, year-over-year, in %
    cpi_3m_annualized: Decimal | None = None  # 3-month CPI change, annualized, in % (momentum)
    real_fed_funds: Decimal | None = None  # FEDFUNDS − cpi_yoy (ex-post real policy rate, p.p.)
    real_10y: Decimal | None = None  # DGS10 − cpi_yoy (ex-post real 10y yield, p.p.)
    sahm_gap: Decimal | None = None  # Sahm rule gap in p.p.
    sahm_recession_signal: bool | None = None  # True when sahm_gap ≥ 0.50
    vix_zscore: Decimal | None = None  # VIX vs its ~1y window (volatility regime)
    vix_percentile: Decimal | None = None  # VIX percentile within its ~1y window, in %
    yield_curve_inverted: bool | None = None  # 10Y-2Y spread < 0
    yield_curve_days_inverted: int | None = None  # consecutive recent observations inverted
    recession_prob_12m: Decimal | None = None  # NY-Fed probit on the 10y-3m spread, in %
    inflation_expectations_10y: Decimal | None = None  # 10y breakeven (market-expected CPI), %
    real_10y_exante: Decimal | None = None  # 10y TIPS real yield (ex-ante real rate), %
    credit_spread_hy: Decimal | None = None  # US high-yield OAS, % (financial-stress gauge)
    credit_spread_hy_zscore: Decimal | None = None  # HY spread vs its ~1y window (stress regime)
    net_liquidity: Decimal | None = None  # Fed net liquidity: WALCL − TGA − RRP, in $M
    net_liquidity_wow: Decimal | None = None  # week-over-week change in net liquidity, $M
    net_liquidity_zscore: Decimal | None = None  # net liquidity vs its ~1y weekly window
    financial_conditions_tight: bool | None = None  # Chicago Fed NFCI > 0 (tighter than average)
    initial_claims_4wk_yoy: Decimal | None = None  # IC4WSA 4-week-avg claims YoY change, in %
    cfnai_recession_signal: bool | None = None  # CFNAIMA3 < −0.70 (Chicago Fed recession threshold)
    vix_term_structure: Decimal | None = None  # VXV/VIX (3M/1M); < 1 = backwardation (acute stress)
    vix_backwardation: bool | None = None  # VIX term structure < 1.0
    inflation_5y5y: Decimal | None = None  # 5y5y forward inflation expectation (T5YIFR), %
    m2_yoy: Decimal | None = None  # M2 money stock YoY growth, %
    dollar_broad_zscore: Decimal | None = None  # broad nominal USD index vs its ~1y window
    brent_wti_spread: Decimal | None = None  # Brent − WTI crude, $/bbl (global-vs-US crude stress)
    notes: list[str] = []


class MacroSnapshot(BaseModel):
    """A snapshot of key macro indicators (rates, spread, unemployment, CPI, VIX) plus a
    `derived` block of regime metrics computed from the same series."""

    indicators: list[MacroIndicator] = []
    derived: MacroDerived | None = None
    as_of: date | None = None


class SecFinancialLine(BaseModel):
    """One reported financial figure from SEC XBRL, with full provenance."""

    concept: str
    tag: str | None = None
    value: Decimal | None = None
    unit: str | None = None
    period_end: date | None = None
    fiscal_year: int | None = None
    form: str | None = None
    filed: date | None = None


class SecFinancials(BaseModel):
    """Authoritative annual financials from SEC EDGAR XBRL — to cross-check other sources.

    Every line carries which tag/filing/period it came from, so a figure can be audited
    against the primary document. Raw reported numbers, no derived verdict.
    """

    symbol: str
    cik: str | None = None
    fiscal_year: int | None = None
    period_end: date | None = None
    lines: list[SecFinancialLine] = []
    as_of: date | None = None


class NewsItem(BaseModel):
    title: str | None = None
    publisher: str | None = None
    published: datetime | None = None
    url: str | None = None
    summary: str | None = None


class NewsList(BaseModel):
    """Recent headlines for a symbol — links the agent can then `extract` and read in full."""

    symbol: str
    items: list[NewsItem] = []


class EarningsEvent(BaseModel):
    event_date: date | None = None
    eps_estimate: Decimal | None = None
    eps_reported: Decimal | None = None
    surprise_percent: Decimal | None = None
    is_future: bool | None = None


class EarningsInfo(BaseModel):
    """Earnings calendar and history: upcoming dates plus past estimate/actual/surprise."""

    symbol: str
    next_earnings_date: date | None = None
    beat_rate: Decimal | None = None  # fraction of past prints that beat the estimate (0..1)
    surprise_streak: int | None = None  # consecutive most-recent prints that beat
    avg_surprise: Decimal | None = None  # mean surprise_percent across past prints
    surprise_consistency: Decimal | None = None  # stdev of surprise_percent (lower = steadier)
    events: list[EarningsEvent] = []


class AnalystView(BaseModel):
    """What sell-side analysts say — third-party opinion reported as data, NOT Scout's verdict."""

    symbol: str
    recommendation_key: str | None = None
    recommendation_mean: Decimal | None = Field(
        default=None, description="Consensus 1=strong buy … 5=sell (as reported)."
    )
    number_of_analysts: int | None = None
    current_price: Decimal | None = None
    target_mean: Decimal | None = None
    target_median: Decimal | None = None
    target_high: Decimal | None = None
    target_low: Decimal | None = None
    upside_pct: Decimal | None = None  # (target_mean − current_price) / current_price
    target_dispersion: Decimal | None = None  # (high − low) / mean — analyst disagreement


class QualityMetrics(BaseModel):
    """Derived quality/return ratios — ROE/ROA, margins and growth/CAGR. Raw numbers, no rating."""

    symbol: str
    roe: Decimal | None = None
    roa: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    net_margin: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None
    earnings_growth_yoy: Decimal | None = None
    revenue_cagr: Decimal | None = None
    net_income_cagr: Decimal | None = None
    cagr_years: int | None = None
    as_of: date | None = None


class RelativeStrengthRow(BaseModel):
    symbol: str
    return_percent: Decimal | None = None
    excess_vs_benchmark: Decimal | None = None
    window_start: date | None = Field(
        default=None,
        description=(
            "First bar actually used for this symbol's return. A recent IPO has less history "
            "than the requested period — its window starts later than the benchmark's, so its "
            "return is NOT measured over the same span. Compare before ranking."
        ),
    )
    note: str | None = None


class RelativeStrength(BaseModel):
    """Each symbol's total return over a period vs a benchmark — momentum/leadership, as data."""

    benchmark: str
    period: str
    benchmark_return_percent: Decimal | None = None
    benchmark_window_start: date | None = None  # first bar used for the benchmark's return
    rows: list[RelativeStrengthRow] = []
    notes: list[str] = []


class SectorReturn(BaseModel):
    sector: str
    etf: str
    return_percent: Decimal | None = None


class SectorPerformance(BaseModel):
    """Total return of each US sector (via its SPDR ETF) over a period — rotation, as data."""

    period: str
    sectors: list[SectorReturn] = []
    notes: list[str] = []


class EtfHolding(BaseModel):
    symbol: str
    name: str | None = None
    weight: Decimal | None = None


class EtfHoldings(BaseModel):
    """An ETF's declared basket — top holdings and sector weights, straight from the issuer."""

    symbol: str
    top_holdings: list[EtfHolding] = []
    sector_weights: dict[str, Decimal] = {}
    note: str | None = None


class InsiderTransaction(BaseModel):
    transaction_date: date | None = None
    insider: str | None = None
    position: str | None = None
    transaction: str | None = None
    shares: Decimal | None = None
    value: Decimal | None = None


class InstitutionalHolder(BaseModel):
    holder: str | None = None
    shares: Decimal | None = None
    pct_out: Decimal | None = None
    value: Decimal | None = None


class ShortInterest(BaseModel):
    """Short-interest figures for a stock (bi-monthly, exchange-reported via yfinance `.info`).

    Point-in-time as of ``short_interest_date`` (the settlement date the exchange reported), not a
    live read — short interest is published roughly twice a month with a lag. Raw facts (a crowding
    / squeeze-risk signal the agent interprets), no verdict. Every field is null when the source
    doesn't expose it — never fabricated.
    """

    shares_short: Decimal | None = None
    shares_short_prior_month: Decimal | None = None
    short_ratio_days_to_cover: Decimal | None = None  # shortRatio — days of avg volume to cover
    short_percent_of_float: Decimal | None = None
    short_percent_of_shares_out: Decimal | None = None
    short_interest_date: date | None = None  # settlement date of the reported short interest
    short_interest_change_pct: Decimal | None = Field(
        default=None,
        description=(
            "(shares_short − shares_short_prior_month) / shares_short_prior_month × 100 — the "
            "bi-monthly change in short interest. Null when the prior month is missing or zero."
        ),
    )


class Ownership(BaseModel):
    """Who owns the stock: insider/institution percentages, top institutions, insider trades.

    Public-record data (13F / Form 4), reported as facts — a skin-in-the-game signal the agent
    interprets. No verdict.
    """

    symbol: str
    insider_percent: Decimal | None = None
    institution_percent: Decimal | None = None
    institutional_holders: list[InstitutionalHolder] = []
    insider_transactions: list[InsiderTransaction] = []
    short_interest: ShortInterest | None = Field(
        default=None,
        description=(
            "Short-interest block (shares short, days-to-cover, % of float, bi-monthly change). "
            "Null when the source exposes no short data for the symbol."
        ),
    )


class OptionsVolatility(BaseModel):
    """Implied volatility and the options-implied expected move to an expiry. Raw data."""

    symbol: str
    expiry: date | None = None
    days_to_expiry: int | None = None
    current_price: Decimal | None = None
    atm_strike: Decimal | None = None
    atm_iv: Decimal | None = Field(
        default=None, description="ATM implied vol, annualized fraction."
    )
    expected_move_percent: Decimal | None = Field(
        default=None, description="1-sigma move to expiry = IV * sqrt(days/365)."
    )
    expected_move_amount: Decimal | None = None
    expected_move_low: Decimal | None = None
    expected_move_high: Decimal | None = None
    # --- Derived (raw measures, no verdict) ---
    iv_skew: Decimal | None = None  # (OTM put IV − OTM call IV)/ATM IV; >0 = puts richer (fear)
    put_call_ratio_volume: Decimal | None = None  # Σ put volume / Σ call volume (this expiry)
    put_call_ratio_oi: Decimal | None = None  # Σ put OI / Σ call OI (this expiry)
    realized_vol: Decimal | None = None  # trailing ~3mo close-to-close vol, annualized
    iv_rv_ratio: Decimal | None = None  # ATM IV / realized vol (>1 = options look rich)
    volatility_risk_premium: Decimal | None = None  # ATM IV − realized vol (the stateless IV-rank)
    far_expiry: date | None = None  # the longer-dated expiry used for the term-structure read
    far_atm_iv: Decimal | None = None  # ATM IV at the far expiry
    iv_term_slope: Decimal | None = None  # (far IV − near IV)/near IV
    iv_term_structure: str | None = None  # "contango" (far>near, calm) | "backwardation" (stress)
    note: str | None = None


class CotWeek(BaseModel):
    """One weekly Commitments-of-Traders point for a market (speculator side + open interest)."""

    report_date: date
    noncomm_long: Decimal | None = None
    noncomm_short: Decimal | None = None
    noncomm_net: Decimal | None = None  # long − short (large-speculator net positioning)
    open_interest: Decimal | None = None


class CotReport(BaseModel):
    """CFTC Commitments of Traders positioning for a futures market — weekly, point-in-time.

    Speculator (non-commercial) vs commercial (hedger) futures positioning from the CFTC's legacy
    futures-only report. ``net`` = long − short. Positions are as of the Tuesday, published the
    following Friday (~3-day lag), so ``as_of`` is the report's settlement date, NOT today. This is
    POSITIONING, not price — raw facts and derived extremes, no verdict.
    """

    market: str  # the search string the caller passed (e.g. "GOLD")
    market_name: str | None = None  # market_and_exchange_names of the chosen series
    commodity_name: str | None = None
    contract_market_name: str | None = None
    cftc_contract_market_code: str | None = None
    as_of: date | None = None  # latest report_date (the point-in-time settlement date)
    open_interest: Decimal | None = None
    noncomm_long: Decimal | None = None  # large speculators long
    noncomm_short: Decimal | None = None  # large speculators short
    noncomm_net: Decimal | None = None  # noncomm_long − noncomm_short
    comm_long: Decimal | None = None  # commercials / hedgers long
    comm_short: Decimal | None = None  # commercials / hedgers short
    comm_net: Decimal | None = None  # comm_long − comm_short
    noncomm_net_pct_oi: Decimal | None = None  # noncomm_net / open_interest × 100
    noncomm_net_change: Decimal | None = None  # WoW change in noncomm net (CFTC's own deltas)
    noncomm_net_zscore: Decimal | None = None  # noncomm_net vs the returned window (crowding gauge)
    traders_noncomm_long: int | None = None  # count of large-spec long traders
    traders_noncomm_short: int | None = None  # count of large-spec short traders
    history: list[CotWeek] = []  # weekly points, oldest → newest
    matched_markets: list[str] = Field(
        default_factory=list,
        description=(
            "Other distinct markets the search string also matched (the primary series is the "
            "highest-open-interest match) — so an ambiguous query is visible, not silently wrong."
        ),
    )
    note: str | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when the CFTC source couldn't be reached (e.g. 'unavailable: rate_limited') — so "
            "an empty result from a 429/timeout is distinguishable from a genuine 'no market'."
        ),
    )


class WorldBankIndicator(BaseModel):
    code: str
    name: str | None = None
    value: Decimal | None = None
    year: int | None = None
    country: str | None = None


class WorldBankData(BaseModel):
    """Key macro indicators for a country from the World Bank (global, official, keyless)."""

    country: str
    indicators: list[WorldBankIndicator] = []
    partial: bool = Field(
        default=False,
        description="True when one or more requested indicators were dropped (fetch failed) — "
        "the list is thinner than asked for, not the country's full set.",
    )


class TreasuryFigure(BaseModel):
    name: str
    value: Decimal | None = None
    unit: str | None = None
    record_date: date | None = None


class TreasuryData(BaseModel):
    """Official US Treasury fiscal data (debt, average interest rates) — keyless, authoritative."""

    figures: list[TreasuryFigure] = []
    partial: bool = Field(
        default=False,
        description="True when a leg (debt or average-rates) was dropped — the figures are "
        "incomplete, not the full set.",
    )


class PageviewDay(BaseModel):
    day: date
    views: int | None = None


class WikipediaAttention(BaseModel):
    """Daily Wikipedia pageviews for an article — an official, stable attention proxy."""

    article: str
    days: int
    total_views: int | None = None
    items: list[PageviewDay] = []
    note: str | None = None


class RetailBuzzItem(BaseModel):
    symbol: str
    name: str | None = None
    rank: int | None = None
    rank_24h_ago: int | None = None
    mentions: int | None = None
    mentions_24h_ago: int | None = None
    upvotes: int | None = None


class RetailBuzz(BaseModel):
    """Reddit (WSB/stocks) mention buzz — a retail-attention signal, not a sentiment score."""

    symbol: str | None = None
    items: list[RetailBuzzItem] = []
    note: str | None = None


class WebNewsItem(BaseModel):
    title: str | None = None
    domain: str | None = None
    url: str | None = None
    published: datetime | None = None
    language: str | None = None
    country: str | None = None


class WebNewsSearch(BaseModel):
    """Free-text news/event search across global media (GDELT) — by theme or company name."""

    query: str
    items: list[WebNewsItem] = []
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when the source couldn't be reached (e.g. 'unavailable: rate_limited') — so an "
            "empty result from a 429/timeout is distinguishable from a genuine 'no matches'."
        ),
    )


class FilingSearchHit(BaseModel):
    company: str | None = None
    ticker: str | None = None
    cik: str | None = None
    form: str | None = None
    filing_date: date | None = None
    url: str | None = None


class FilingSearch(BaseModel):
    """Companies/filings matching a full-text query across all EDGAR filings (thesis → names)."""

    query: str
    total: int | None = None
    hits: list[FilingSearchHit] = []


class ExtractedPage(BaseModel):
    """A web page fetched and reduced to clean, token-efficient markdown.

    A pure capture sense for the agent's own research: it picks the URL, this returns the main
    content. It does NOT judge or summarize. ``fetched_ok=False`` (with a note) honestly reports
    a paywall/anti-bot block rather than pretending — the kind of gap the benchmark exposed.
    """

    url: str
    fetched_ok: bool
    status_code: int | None = None
    title: str | None = None
    markdown: str | None = None
    char_count: int | None = None
    note: str | None = None


# --- Crypto models (spot, BASE/QUOTE) -----------------------------------------------------
# Mirror the equities models but for crypto spot pairs. Symbols use the CCXT unified format
# ``BASE/QUOTE`` (e.g. ``BTC/USDT``) so a researched asset maps straight to a tradable pair on
# the execution side. Data only — never a verdict (same constitution as the rest of Scout).


class CryptoQuote(BaseModel):
    """A live spot quote for a crypto pair — last/bid/ask and the 24h move."""

    symbol: str  # CCXT unified pair, e.g. "BTC/USDT"
    base: str  # e.g. "BTC"
    quote: str  # e.g. "USDT"
    exchange: str
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    change_percent_24h: Decimal | None = None
    base_volume_24h: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    timestamp: datetime | None = None


class CryptoBar(BaseModel):
    """One OHLCV candle. ``timestamp`` keeps intraday resolution (the date-keyed PriceBar can't)."""

    timestamp: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None  # base-asset volume (fractional in crypto)


class CryptoPriceHistory(BaseModel):
    """OHLCV candles for a crypto pair over a timeframe."""

    symbol: str  # CCXT unified pair, e.g. "BTC/USDT"
    base: str
    quote: str
    exchange: str
    timeframe: str  # CCXT timeframe, e.g. "1d", "1h", "5m"
    bars: list[CryptoBar] = []
    as_of: date | None = None


class CryptoAssetProfile(BaseModel):
    """Supply, market cap, rank and ATH for a crypto asset (the 'fundamentals' of crypto).

    Sourced from an aggregator (Coinpaprika) keyed by the asset, not a single exchange — raw
    facts (circulating/total/max supply, market cap, rank), not a verdict on value.
    """

    base: str  # e.g. "BTC"
    name: str | None = None
    source_id: str | None = None  # the aggregator's id, e.g. "btc-bitcoin"
    rank: int | None = None
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    circulating_supply: Decimal | None = None
    total_supply: Decimal | None = None
    max_supply: Decimal | None = None
    ath_price_usd: Decimal | None = None
    ath_date: date | None = None
    percent_from_ath: Decimal | None = None
    # Derived tokenomics (raw ratios, not a verdict):
    float_ratio: Decimal | None = None  # circulating / total supply (how much is unlocked)
    issuance_progress: Decimal | None = None  # circulating / max supply (% of final supply minted)
    future_dilution: Decimal | None = None  # (max − circulating)/circulating — supply overhang


class FearGreedPoint(BaseModel):
    value: int | None = None
    classification: str | None = None
    observation_date: date | None = None


class CryptoFearGreed(BaseModel):
    """The Crypto Fear & Greed Index (alternative.me) — a market-wide sentiment gauge 0-100.

    Market-level, not per-coin. Raw index + history; the agent reads the level, no buy/sell call.
    """

    value: int | None = None
    classification: str | None = None
    observation_date: date | None = None
    history: list[FearGreedPoint] = []


# --- Crypto Tier 2: on-chain, derivatives, implied volatility -----------------------------


class OnChainMetric(BaseModel):
    name: str
    value: Decimal | None = None
    unit: str | None = None


class CryptoOnChain(BaseModel):
    """Network-health metrics for a chain (BTC fees/hashrate, ETH gas/addresses, …).

    Heterogeneous across chains, so metrics are a labelled list. Raw on-chain facts, not a verdict.
    """

    asset: str
    source: str
    metrics: list[OnChainMetric] = []
    note: str | None = None


class BtcNetworkPoint(BaseModel):
    """One dated sample of the BTC base-layer headline series (hash rate + NVT)."""

    timestamp: datetime | None = None
    hash_rate: Decimal | None = None  # TH/s (Blockchain.com hash-rate chart unit)
    nvt: Decimal | None = None  # market cap / on-chain settlement USD volume that day


class BtcNetwork(BaseModel):
    """BTC base-layer fundamentals (Blockchain.com) + live fee market (mempool.space).

    Composes two keyless sources: hash rate / miner revenue / NVT valuation, and the current
    sat/vB fee tiers and difficulty retarget. NVT here uses ON-CHAIN settlement USD volume (the
    `estimated-transaction-volume-usd` chart), not exchange volume — the real NVT. Raw measures of
    network health and congestion, never a verdict. If one source is throttled the other still
    returns, with the gap recorded in `partial`/`note` (a missing leg reads as null, never a faked
    number — ADR-012).
    """

    # Source A — Blockchain.com fundamentals
    hash_rate: Decimal | None = None  # TH/s
    miners_revenue_usd: Decimal | None = None
    n_transactions: Decimal | None = None
    tx_volume_usd: Decimal | None = None  # on-chain settlement USD volume
    market_cap_usd: Decimal | None = None
    nvt: Decimal | None = None  # market_cap / tx_volume_usd
    nvt_90d: Decimal | None = None  # market_cap / 90-day avg tx_volume_usd (null under 90 points)
    # Source B — mempool.space fee market & difficulty
    fee_fastest: Decimal | None = None  # sat/vB
    fee_half_hour: Decimal | None = None
    fee_hour: Decimal | None = None
    fee_economy: Decimal | None = None
    fee_minimum: Decimal | None = None
    difficulty_change_pct: Decimal | None = None
    difficulty_progress_pct: Decimal | None = None
    estimated_retarget_date: datetime | None = None
    history: list[BtcNetworkPoint] = []
    as_of: datetime | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when a leg failed (e.g. 'unavailable: rate_limited') — so a missing field reads "
            "as 'unavailable', not a real zero. None means both legs completed."
        ),
    )
    partial: bool = False  # one leg returned, the other was throttled/unavailable
    note: str | None = None


class ProtocolFees(BaseModel):
    """One protocol's daily cash flow: fees (what users pay) vs revenue (what it keeps)."""

    name: str | None = None
    category: str | None = None
    chains: list[str] = []
    fees_24h: Decimal | None = None
    fees_7d: Decimal | None = None
    revenue_24h: Decimal | None = None


class DefiFees(BaseModel):
    """DeFi protocol cash-flow fundamentals (DefiLlama) — fees vs revenue.

    Fees = what users pay; revenue = the slice the protocol/token keeps. Real token cash flow, the
    fundamental DeFi lacks elsewhere keyless. Overview (no `protocol`) carries totals + the top
    protocols by 24h fees; a single `protocol` carries just its summary. Perps-DEX volume is out of
    scope (the DefiLlama derivatives overview is Pro-gated / HTTP 402). Raw figures, not a verdict.
    """

    total_fees_24h: Decimal | None = None
    total_fees_7d: Decimal | None = None
    total_revenue_24h: Decimal | None = None
    protocol: str | None = None  # set when scoped to a single protocol
    top_protocols: list[ProtocolFees] = []
    as_of: datetime | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when a fetch failed (e.g. 'unavailable: rate_limited') — so missing figures read "
            "as 'unavailable', not real zeros. None means the reads completed."
        ),
    )
    note: str | None = None


class DerivativesVenue(BaseModel):
    exchange: str
    symbol: str | None = None
    funding_rate: Decimal | None = None
    funding_rate_annualized: Decimal | None = None  # funding × 3/day × 365 (assumes 8h interval)
    next_funding_time: datetime | None = None
    mark_price: Decimal | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None  # OI in USD (already normalized per venue)


class CryptoDerivatives(BaseModel):
    """Perp funding rate and open interest across exchanges — positioning CONTEXT, never executed.

    The execution side is spot-only; this is a sentiment/positioning read (funding sign & size, OI
    trend). Raw numbers per venue; the agent interprets crowding.
    """

    base: str
    venues: list[DerivativesVenue] = []
    # Cross-venue positioning, derived from the venues above (raw measures, not a verdict):
    funding_oi_weighted: Decimal | None = None  # Σ(funding × OI_usd) / Σ(OI_usd) — real consensus
    funding_annualized_oi_weighted: Decimal | None = None  # the same, annualized (8h assumption)
    funding_dispersion: Decimal | None = None  # max − min funding across venues (stress/arb signal)
    total_open_interest_value: Decimal | None = None  # Σ OI in USD across venues
    partial: bool = Field(
        default=False,
        description="True when at least one venue was dropped — the cross-venue aggregates are "
        "computed over fewer exchanges than attempted, so read them as a thinner picture.",
    )
    note: str | None = None


class VolPoint(BaseModel):
    timestamp: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None


class CryptoImpliedVol(BaseModel):
    """The Deribit DVOL index ('crypto VIX') — current value plus recent history.

    Options-implied volatility for BTC/ETH: how much swing the options market is pricing. Raw
    index, useful to size a stop or gauge the vol regime — not a trade call.
    """

    asset: str
    dvol_current: Decimal | None = None
    dvol_zscore: Decimal | None = None  # current DVOL vs its recent window (vol regime)
    dvol_percentile: Decimal | None = None  # current DVOL's percentile within that window, %
    history: list[VolPoint] = []
    note: str | None = None


class PremiumPoint(BaseModel):
    """One day of the US-spot vs offshore premium series."""

    date: date
    premium_pct: Decimal | None = None


class CoinbasePremium(BaseModel):
    """The Coinbase (US-spot, USD) vs Binance (offshore, USDT) price gap — a demand/positioning
    tell, NOT a price. Positive = aggressive US buying (the ETF/institutional bid); negative = US
    selling pressure. Arithmetic on two spot prices Scout can already fetch; paywalled elsewhere.
    """

    symbol: str  # base asset, e.g. "BTC"
    coinbase_price: Decimal | None = None  # US-spot last (<SYM>/USD on Coinbase)
    binance_price: Decimal | None = None  # offshore last (<SYM>/USDT on Binance)
    premium_pct: Decimal | None = None  # (coinbase − binance) / binance × 100
    premium_zscore: Decimal | None = None  # z-score of the latest daily premium vs its window
    history: list[PremiumPoint] = []
    as_of: datetime | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when a venue could not be fetched (e.g. 'unavailable: rate_limited') — the "
            "premium can't be computed honestly from one leg, so the figures read as "
            "'unavailable', not real zeros. None means both venues were fetched OK."
        ),
    )
    note: str | None = None


# --- Crypto Tier 3: DeFi, stablecoins, yields, macro, sectors ------------------------------


class DefiTvlItem(BaseModel):
    name: str
    tvl_usd: Decimal | None = None


class DefiOverview(BaseModel):
    """DeFi total value locked — by chain (no slug) or one protocol's breakdown (with slug)."""

    scope: str  # "chains" or a protocol slug
    total_tvl_usd: Decimal | None = None
    items: list[DefiTvlItem] = []


class StablecoinItem(BaseModel):
    symbol: str | None = None
    name: str | None = None
    peg_type: str | None = None
    peg_mechanism: str | None = None
    price: Decimal | None = None
    peg_deviation: Decimal | None = None  # price - 1.0 for USD-pegged
    circulating_usd: Decimal | None = None


class StablecoinSupply(BaseModel):
    """Stablecoin circulation and peg status (DefiLlama) — liquidity & systemic-risk read."""

    total_circulating_usd: Decimal | None = None
    items: list[StablecoinItem] = []


class StablecoinPegItem(BaseModel):
    """One stablecoin's deviation from its $1 peg on a venue (the PRICE axis)."""

    symbol: str  # e.g. "USDT"
    venue: str  # exchange the price was read from, e.g. "kraken"
    price: Decimal | None = None  # <SYM>/USD last
    deviation_bps: Decimal | None = None  # (price − 1) × 10000; basis points off the $1 peg
    depeg: bool = False  # |deviation_bps| > the depeg threshold (50 bps)


class StablecoinPeg(BaseModel):
    """Major stablecoins' deviation from $1 — the peg-stress / risk-off plumbing read.

    The PRICE axis (a depeg is a market-wide tail trigger), complementing the SUPPLY axis of
    `stablecoin_supply`. Raw basis-point deviation per coin + a depeg flag; not a verdict.
    """

    items: list[StablecoinPegItem] = []
    as_of: datetime | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when a fetch failed (e.g. 'unavailable: rate_limited') — so a missing price reads "
            "as 'unavailable', not a real zero. None means the reads completed."
        ),
    )
    note: str | None = None


class YieldPool(BaseModel):
    project: str | None = None
    chain: str | None = None
    symbol: str | None = None
    tvl_usd: Decimal | None = None
    apy: Decimal | None = None
    apy_base: Decimal | None = None
    apy_reward: Decimal | None = None


class DefiYields(BaseModel):
    """DeFi yield/APY pools (DefiLlama), filterable by chain/project/min TVL — context only."""

    pools: list[YieldPool] = []


class CryptoMacro(BaseModel):
    """Crypto-wide macro snapshot (CoinGecko): total market cap, dominance, DeFi share."""

    total_market_cap_usd: Decimal | None = None
    total_volume_24h_usd: Decimal | None = None
    market_cap_change_percent_24h: Decimal | None = None
    btc_dominance: Decimal | None = None
    eth_dominance: Decimal | None = None
    defi_market_cap_usd: Decimal | None = None
    defi_dominance: Decimal | None = None
    active_cryptocurrencies: int | None = None
    # Cross-source (joined with the DefiLlama stablecoin total) — "dry powder" read:
    stablecoin_supply_ratio: Decimal | None = None  # total mcap / Σ stablecoins (low = much cash)
    stablecoin_dominance: Decimal | None = None  # Σ stablecoins / total mcap (cash share of mkt)
    status: str | None = Field(
        default=None,
        description=(
            "Set when the core CoinGecko leg couldn't be fetched (e.g. 'unavailable: "
            "rate_limited') — so an all-null snapshot from a 429 is distinguishable from real "
            "zeros. None means the headline market-cap/dominance figures were fetched OK."
        ),
    )
    defi_status: str | None = Field(
        default=None,
        description=(
            "Set when ONLY the DeFi leg failed (the headline is fine) — so a null "
            "defi_market_cap_usd/defi_dominance reads as 'unavailable', not a real zero. "
            "Scoped to the DeFi sub-fields; the headline figures above remain valid."
        ),
    )


class CryptoCategory(BaseModel):
    name: str
    market_cap_usd: Decimal | None = None
    market_cap_change_24h: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    top_3_coins: list[str] = []


class CryptoSectors(BaseModel):
    """Per-category (sector) performance for crypto (CoinGecko) — where money rotated."""

    categories: list[CryptoCategory] = []


# --- Crypto parity / discovery -------------------------------------------------------------


class CryptoMover(BaseModel):
    symbol: str
    base: str | None = None
    last: Decimal | None = None
    change_percent_24h: Decimal | None = None
    quote_volume_24h: Decimal | None = None


class CryptoMoversList(BaseModel):
    """Market-wide top gainers / losers / most-active crypto pairs on the configured exchange."""

    category: str
    exchange: str
    quote: str
    movers: list[CryptoMover] = []


class OrderBookLevel(BaseModel):
    price: Decimal | None = None
    amount: Decimal | None = None


class CryptoOrderBook(BaseModel):
    """Top-of-book and aggregated depth for a pair — a pre-trade liquidity/slippage read."""

    symbol: str
    exchange: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread: Decimal | None = None
    spread_percent: Decimal | None = None
    bid_depth_base: Decimal | None = None
    ask_depth_base: Decimal | None = None
    imbalance: Decimal | None = None  # (bid − ask depth)/(bid + ask), −1..1; directional pressure
    microprice: Decimal | None = None  # size-weighted fair price (leans to the thinner side)
    levels: int | None = None
    top_bids: list[OrderBookLevel] = []
    top_asks: list[OrderBookLevel] = []


class CryptoComparisonRow(BaseModel):
    symbol: str
    base: str
    last: Decimal | None = None
    change_percent_24h: Decimal | None = None
    market_cap_usd: Decimal | None = None
    rank: int | None = None
    circulating_supply: Decimal | None = None
    note: str | None = None


class CryptoComparison(BaseModel):
    """Several crypto assets side by side (quote + profile), in one call."""

    items: list[CryptoComparisonRow] = []


class CryptoCorrelationPair(BaseModel):
    a: str
    b: str
    correlation: Decimal | None = None


class CryptoCorrelation(BaseModel):
    """Pairwise return correlation between crypto pairs — real diversification."""

    timeframe: str
    bars_used: int | None = None
    pairs: list[CryptoCorrelationPair] = []
    note: str | None = None


class CryptoRelStrengthRow(BaseModel):
    symbol: str
    return_percent: Decimal | None = None
    excess_vs_benchmark: Decimal | None = None


class CryptoRelativeStrength(BaseModel):
    """Each pair's return over a window vs a benchmark (default BTC) — leadership read."""

    benchmark: str
    timeframe: str
    rows: list[CryptoRelStrengthRow] = []
    notes: list[str] = []


class CryptoSymbolMatch(BaseModel):
    base: str
    name: str | None = None
    source_id: str | None = None
    rank: int | None = None


class CryptoSymbolSearch(BaseModel):
    """Crypto assets matching a free-text query (name / partial symbol)."""

    query: str
    matches: list[CryptoSymbolMatch] = []


class CryptoDossier(BaseModel):
    """Consolidated one-call crypto portrait — fans several reads out in parallel.

    The crypto flagship: quote, asset profile, technicals, market-wide Fear & Greed, derivatives
    positioning and on-chain health gathered concurrently. ``notes`` carries any partial failure.
    """

    symbol: str
    base: str
    quote: str
    quote_data: CryptoQuote | None = None
    profile: CryptoAssetProfile | None = None
    technicals: Technicals | None = None
    fear_greed: CryptoFearGreed | None = None
    derivatives: CryptoDerivatives | None = None
    onchain: CryptoOnChain | None = None
    notes: list[str] = []


# --- Catalysts, commodities & statistical pairs -------------------------------------------
# Cross-domain additions: FDA catalysts (equities), commodity bellwether ratios (macro), and
# the Engle-Granger cointegration / pairs models (multi-symbol StatArb).


class FdaApproval(BaseModel):
    """One drug approval, parsed from a drugsfda submission with status 'AP'.

    Named ``approval_date`` (not ``date``: a field literally named ``date`` would shadow the
    ``date`` type during annotation evaluation). Parallels ``FdaRecall.report_date``.
    """

    approval_date: date | None = None  # submission_status_date (the approval date)
    application_number: str | None = None  # e.g. "NDA021436" / "BLA125514"
    brand_names: list[str] = []  # brand_name(s) from the application's products


class FdaRecall(BaseModel):
    """One drug recall/enforcement record (openFDA enforcement endpoint)."""

    report_date: date | None = None
    product: str | None = None  # product_description
    reason: str | None = None  # reason_for_recall
    classification: str | None = None  # Class I / II / III (severity)
    status: str | None = None  # Ongoing / Completed / Terminated


class FdaEvents(BaseModel):
    """Drug approvals + recalls for a pharma/biotech sponsor (openFDA, keyless).

    Pharma/biotech catalysts: an approval is a binary up-catalyst, a recall a down-catalyst. The
    caller passes the ISSUER/SPONSOR NAME (e.g. from ``company_dossier``); mapping a ticker to the
    exact sponsor-of-record string is fuzzy and out of scope. Two independent openFDA endpoints
    (drugsfda for approvals, enforcement for recalls) are composed with partial handling: a leg that
    can't be fetched (429/timeout/5xx) sets ``source_status`` and leaves its list empty; a genuine
    zero-match (openFDA returns HTTP 404 ``NOT_FOUND``) is an empty list, recorded in ``note`` — the
    two are distinguishable. Raw facts, never a verdict (ADR-004/ADR-012).
    """

    company: str  # the sponsor/issuer name the caller passed
    approvals: list[FdaApproval] = []
    recalls: list[FdaRecall] = []
    as_of: date | None = None  # latest approval/recall date observed
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when a leg couldn't be reached (e.g. 'approvals unavailable: rate_limited') — so "
            "an empty list from a throttle is distinguishable from a genuine zero-match."
        ),
    )
    note: str | None = None




class RatioPoint(BaseModel):
    """One daily aligned point of the commodity bellwether ratios."""

    date: date
    copper_gold: Decimal | None = None
    gold_silver: Decimal | None = None


class CommodityRatios(BaseModel):
    """Macro bellwether commodity ratios — copper/gold and gold/silver (yfinance, keyless).

    ``copper_gold`` (Dr. Copper vs the safe haven) is a risk-appetite tell: copper is an industrial
    growth proxy, gold a fear proxy, so the ratio rising = growth/risk-on. ``gold_silver`` is the
    classic precious-metals ratio (a rough ~50-90 band; high = stress/deflation lean). The LEVEL of
    copper/gold is arbitrary — the trend and the z-score are the signal. These are bellwethers that
    MEASURE the macro backdrop; they are not verdicts (ADR-004). A missing leg leaves that ratio
    null with a ``note``; a fetch failure sets ``source_status`` (ADR-012).
    """

    copper_gold: Decimal | None = None
    copper_gold_zscore: Decimal | None = None  # z-score of the latest copper/gold over ~1y daily
    gold_silver: Decimal | None = None
    gold_silver_zscore: Decimal | None = None
    copper_price: Decimal | None = None  # HG=F, $/lb
    gold_price: Decimal | None = None  # GC=F, $/oz
    silver_price: Decimal | None = None  # SI=F, $/oz
    history: list[RatioPoint] = []  # daily aligned ratios, oldest → newest
    as_of: date | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when one or more legs couldn't be reached (e.g. 'copper unavailable: timeout') "
            "— so a null ratio from a throttle distinguishes from a genuine missing series."
        ),
    )
    note: str | None = None


class Cointegration(BaseModel):
    """Engle-Granger cointegration / pairs read for two symbols (yfinance, keyless).

    The StatArb primitive Scout lacked: is the spread between two price series mean-reverting?
    Step 1 fits a hedge ratio (OLS of A on B); step 2 runs a Dickey-Fuller test on the residual
    spread. ``adf_stat`` is the DF t-statistic — strongly NEGATIVE means stationary/cointegrated;
    it is judged against MacKinnon's asymptotic Engle-Granger critical values (residual-based,
    constant/no-trend, one regressor) carried in ``adf_crit_*``. We expose the statistic vs the
    critical values rather than a fake-precise p-value (honesty over filling, ADR-004/ADR-012).

    ``spread_zscore`` is the live trade signal — how many sigma the latest spread sits from its
    own mean. ``half_life_days`` is the mean-reversion speed (−ln2/γ); null when the spread does
    not revert. This MEASURES a relationship; the decision to trade the pair lives in Vizier.

    Caveats (in ``note``): a non-augmented (0-lag) DF, yfinance split/div-adjusted closes (so the
    series is consistent today but not a historical point-in-time vintage), and the asymptotic
    critical values. A short overlap or a degenerate fit leaves the stats null with a ``note``;
    a fetch failure sets ``source_status``.
    """

    symbol_a: str
    symbol_b: str
    lookback_days: int
    n_obs: int | None = None  # overlapping daily closes actually used
    hedge_ratio_beta: Decimal | None = None  # A ≈ beta·B + intercept (the hedge ratio)
    spread_latest: Decimal | None = None  # most recent residual A − (beta·B + intercept)
    spread_zscore: Decimal | None = None  # latest spread vs its own window (the trade signal)
    adf_stat: Decimal | None = None  # Dickey-Fuller t-stat on the residual (more negative = revert)
    adf_crit_1pct: Decimal | None = None
    adf_crit_5pct: Decimal | None = None
    adf_crit_10pct: Decimal | None = None
    is_cointegrated: bool | None = None  # adf_stat < the 5% critical value (the standard threshold)
    half_life_days: Decimal | None = None  # −ln2/γ; null when the spread doesn't mean-revert
    as_of: date | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when a leg couldn't be reached (e.g. 'symbol_b unavailable: timeout') — so null "
            "stats from a throttle distinguish from a genuine short/degenerate overlap."
        ),
    )
    note: str | None = None


class PairCandidate(BaseModel):
    """One cointegrated pair found by the screen, with the same stats as ``Cointegration``."""

    symbol_a: str
    symbol_b: str
    correlation: Decimal | None = None  # return correlation (the pre-filter that earned the test)
    hedge_ratio_beta: Decimal | None = None
    adf_stat: Decimal | None = None
    is_cointegrated: bool | None = None  # adf_stat < the 5% critical value
    spread_zscore: Decimal | None = None  # how stretched the spread is now (the live signal)
    half_life_days: Decimal | None = None
    n_obs: int | None = None


class CointegratedPairs(BaseModel):
    """Cointegration screen across a basket of symbols (yfinance, keyless).

    Tests every symbol pair that first clears a return-correlation pre-filter (cointegrated series
    are almost always correlated, and the filter cuts the multiple-testing burden), then keeps the
    pairs whose residual ADF stat passes the 5% Engle-Granger level, sorted strongest-first.

    Multiple-testing honesty: with ``pairs_tested`` independent tests at the 5% level you expect
    ~0.05·pairs_tested false positives, so the screen reports ``adf_crit_1pct`` too and the ``note``
    flags that the strongest (most negative ``adf_stat``) / 1%-level hits are the robust ones — a
    fake-precise Bonferroni p-value would over-claim (ADR-004/ADR-012). MEASURES candidates; the
    decision to trade any pair is the caller's (Vizier).
    """

    symbols: list[str] = []  # the (clamped) universe actually screened
    lookback_days: int
    min_correlation: Decimal | None = None
    pairs_tested: int | None = None  # pairs that cleared the correlation filter and were ADF-tested
    adf_crit_5pct: Decimal | None = None
    adf_crit_1pct: Decimal | None = None
    pairs: list[PairCandidate] = []  # cointegrated pairs, most negative adf_stat first
    as_of: date | None = None
    source_status: str | None = Field(
        default=None,
        description=(
            "Set when one or more symbols couldn't be fetched (e.g. 'GLD unavailable: timeout') — "
            "the screen runs on what fetched; the dropped symbols are named here."
        ),
    )
    note: str | None = None
