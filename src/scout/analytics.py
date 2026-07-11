"""Technical indicators — pure, source-agnostic math over a price series.

Kept separate from any adapter so the formulas are unit-testable in isolation and reusable on
any ``PriceHistory`` (yfinance today, anything tomorrow). Output is raw numbers: no "uptrend"
or "overbought" verdict — the agent reads RSI/price-vs-SMA and concludes (see DECISIONS ADR-004).
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from .domain.models import PriceHistory, Technicals


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def ema_series(values: list[float], window: int) -> list[float]:
    """EMA aligned so index 0 corresponds to input index ``window-1`` (seeded with the SMA)."""
    if len(values) < window:
        return []
    k = 2 / (window + 1)
    current = sum(values[:window]) / window
    out = [current]
    for value in values[window:]:
        current = value * k + current * (1 - k)
        out.append(current)
    return out


def ema_last(values: list[float], window: int) -> float | None:
    series = ema_series(values, window)
    return series[-1] if series else None


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    if len(values) < slow + signal:
        return None, None, None
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    offset = slow - fast  # align the longer fast-EMA onto the slow-EMA timeline
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow, strict=False)]
    signal_series = ema_series(macd_line, signal)
    if not signal_series:
        return macd_line[-1], None, None
    macd_value = macd_line[-1]
    signal_value = signal_series[-1]
    return macd_value, signal_value, macd_value - signal_value


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    true_ranges = []
    for i in range(1, n):
        true_ranges.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    current = sum(true_ranges[:period]) / period
    for i in range(period, len(true_ranges)):
        current = (current * (period - 1) + true_ranges[i]) / period
    return current


def pct_returns(closes: list[float]) -> list[float]:
    """Period-over-period simple returns from a close series."""
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] != 0]


def pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of two equal-length series; ``None`` if undefined (zero variance)."""
    n = len(a)
    if n < 2 or n != len(b):
        return None
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / math.sqrt(var_a * var_b)


# --- Risk / volatility / momentum -------------------------------------------------
# These turn OHLCV the Scout already fetches into derived numbers (realized vol,
# risk-adjusted return, drawdown, momentum) instead of leaving them raw. Still pure,
# still verdict-free: the agent reads the number and concludes.

_TRADING_DAYS_PER_YEAR = 252  # equities; crypto trades 24/7 → pass 365
_MIN_BARS_FOR_VOL = 20  # below this a vol estimate is too noisy to report
_MIN_BARS_FOR_RATIO = 30  # Sharpe/Sortino need a minimal sample to mean anything
_MIN_BARS_FOR_SHAPE = 60  # skew/kurtosis are high-moment → demand more data
def _momentum_lookbacks(periods_per_year: int) -> dict[str, int]:
    """Calendar-aware momentum lookbacks. A field labelled ``momentum_6m`` must mean 6 calendar
    months, so the bar count scales with ``periods_per_year`` — 252 trading days for equities,
    365 for 24/7 crypto. Hardcoding 63/126/252 would silently make crypto's '6m' measure ~4
    months (it has ~365 bars/yr, not 252), reporting a confidently mislabelled number."""
    return {
        "momentum_3m": round(periods_per_year / 4),
        "momentum_6m": round(periods_per_year / 2),
        "momentum_12m": periods_per_year,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: list[float], *, sample: bool = True) -> float | None:
    """Standard deviation; ``sample`` uses Bessel's (N-1) correction, else population (N)."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    divisor = (n - 1) if sample else n
    return math.sqrt(sum((x - mean) ** 2 for x in values) / divisor)


def log_returns(closes: list[float]) -> list[float]:
    """Continuously-compounded returns; skips non-positive prices that break ``ln``."""
    out = []
    for i in range(1, len(closes)):
        prev, curr = closes[i - 1], closes[i]
        if prev > 0 and curr > 0:
            out.append(math.log(curr / prev))
    return out


def realized_volatility(
    closes: list[float], periods_per_year: int = _TRADING_DAYS_PER_YEAR
) -> float | None:
    """Annualized close-to-close volatility (stdev of log returns × √periods)."""
    rets = log_returns(closes)
    if len(rets) < _MIN_BARS_FOR_VOL:
        return None
    sd = _stdev(rets, sample=True)
    if sd is None:
        return None
    return sd * math.sqrt(periods_per_year)


def rogers_satchell_volatility(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Drift-independent OHLC volatility (Rogers-Satchell, 1991). Ignores overnight gaps,
    so it is the right estimator for 24/7 markets (crypto) where there is no gap."""
    n = len(closes)
    if not (len(opens) == len(highs) == len(lows) == n):
        return None
    total = 0.0
    valid = 0
    for o, h, low, c in zip(opens, highs, lows, closes, strict=True):
        if o <= 0 or h <= 0 or low <= 0 or c <= 0:
            continue  # skip a degenerate bar rather than nuke the whole estimate
        u = math.log(h / o)
        d = math.log(low / o)
        cl = math.log(c / o)
        total += u * (u - cl) + d * (d - cl)
        valid += 1
    if valid < _MIN_BARS_FOR_VOL:
        return None
    var = total / valid
    return math.sqrt(var * periods_per_year) if var >= 0 else None


def yang_zhang_volatility(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Yang-Zhang (2000) OHLC volatility: overnight gap + open-to-close drift + intraday
    range (Rogers-Satchell). Most efficient estimator when overnight gaps exist (equities)."""
    n = len(closes)
    if not (len(opens) == len(highs) == len(lows) == n):
        return None
    overnight, open_to_close, rs_terms = [], [], []
    for i in range(1, n):
        o, h, low, c, prev_c = opens[i], highs[i], lows[i], closes[i], closes[i - 1]
        if min(o, h, low, c, prev_c) <= 0:
            continue  # skip a degenerate bar rather than nuke the whole estimate
        overnight.append(math.log(o / prev_c))
        open_to_close.append(math.log(c / o))
        u, d, cl = math.log(h / o), math.log(low / o), math.log(c / o)
        rs_terms.append(u * (u - cl) + d * (d - cl))
    bars = len(overnight)
    if bars < _MIN_BARS_FOR_VOL:
        return None
    var_overnight = _stdev(overnight, sample=True) ** 2  # type: ignore[operator]
    var_open_close = _stdev(open_to_close, sample=True) ** 2  # type: ignore[operator]
    var_rs = sum(rs_terms) / bars
    k = 0.34 / (1.34 + (bars + 1) / (bars - 1))
    var = var_overnight + k * var_open_close + (1 - k) * var_rs
    return math.sqrt(var * periods_per_year) if var >= 0 else None


def sharpe_ratio(
    returns: list[float],
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> float | None:
    """Annualized Sharpe over simple returns. ``risk_free_rate`` is an annual rate (default 0,
    i.e. excess-over-zero); the per-period rate is derived by dividing by ``periods_per_year``."""
    if len(returns) < _MIN_BARS_FOR_RATIO:
        return None
    rf = risk_free_rate / periods_per_year
    excess = [r - rf for r in returns]
    sd = _stdev(excess, sample=True)
    if not sd:
        return None
    return (_mean(excess) / sd) * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: list[float],
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
    target: float = 0.0,
) -> float | None:
    """Annualized Sortino: excess return over downside deviation. Downside deviation divides
    the squared shortfalls by the full N (standard convention), not just the negative count."""
    n = len(returns)
    if n < _MIN_BARS_FOR_RATIO:
        return None
    mar = target / periods_per_year
    downside = sum(min(0.0, r - mar) ** 2 for r in returns) / n
    if downside <= 0:
        return None
    excess_mean = _mean([r - mar for r in returns])
    return (excess_mean / math.sqrt(downside)) * math.sqrt(periods_per_year)


def max_drawdown(closes: list[float]) -> float | None:
    """Largest peak-to-trough decline as a fraction (≤ 0). Uses price as the equity proxy."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for price in closes:
        if price > peak:
            peak = price
        if peak > 0:
            worst = min(worst, price / peak - 1)
    return worst


def max_drawdown_duration(closes: list[float]) -> int | None:
    """Longest stretch (in bars) spent below a prior peak — the 'underwater' duration."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    current = longest = 0
    for price in closes:
        if price >= peak:
            peak = price
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def calmar_ratio(
    closes: list[float], periods_per_year: int = _TRADING_DAYS_PER_YEAR
) -> float | None:
    """Annualized return divided by the absolute max drawdown."""
    n = len(closes)
    if n < _MIN_BARS_FOR_RATIO or closes[0] <= 0:
        return None
    mdd = max_drawdown(closes)
    if not mdd:  # None or 0 (no drawdown → ratio undefined)
        return None
    years = n / periods_per_year
    if years <= 0:
        return None
    annualized = (closes[-1] / closes[0]) ** (1 / years) - 1
    return annualized / abs(mdd)


def skewness(returns: list[float]) -> float | None:
    """Population skewness of returns (tail asymmetry; <0 = fatter left/down tail)."""
    n = len(returns)
    if n < _MIN_BARS_FOR_SHAPE:
        return None
    sd = _stdev(returns, sample=False)
    if not sd:
        return None
    mean = _mean(returns)
    return sum(((r - mean) / sd) ** 3 for r in returns) / n


def excess_kurtosis(returns: list[float]) -> float | None:
    """Excess kurtosis of returns (>0 = fatter tails than normal; tail/crash risk)."""
    n = len(returns)
    if n < _MIN_BARS_FOR_SHAPE:
        return None
    sd = _stdev(returns, sample=False)
    if not sd:
        return None
    mean = _mean(returns)
    return sum(((r - mean) / sd) ** 4 for r in returns) / n - 3


def momentum(closes: list[float], lookback: int) -> float | None:
    """Total return over ``lookback`` bars: close[-1] / close[-1-lookback] - 1."""
    if len(closes) <= lookback:
        return None
    base = closes[-1 - lookback]
    if base <= 0:
        return None
    return closes[-1] / base - 1


def momentum_12_1(
    closes: list[float], periods_per_year: int = _TRADING_DAYS_PER_YEAR
) -> float | None:
    """12-month return skipping the most recent month (classic momentum factor), removing
    the short-term reversal that contaminates raw 12-month momentum. The 12-month and 1-month
    windows are calendar-aware (scale with ``periods_per_year``) so crypto's 24/7 bars map to
    true months, not ~8 months mislabelled as 12."""
    year = periods_per_year
    skip = round(periods_per_year / 12)  # ~one month
    if len(closes) <= year:
        return None
    recent, far = closes[-1 - skip], closes[-1 - year]
    if far <= 0:
        return None
    return recent / far - 1


# --- Macro transforms ------------------------------------------------------------
# Generic statistics reused by the FRED-derived macro layer. Pure (numbers in, number
# out); the date alignment that builds these series lives in the macro adapter. They
# turn raw FRED series (a CPI index level, a VIX print) into regime numbers the agent
# can act on — still measures, never a "risk-on" verdict (ADR-004).


def normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function (stdlib ``math.erf`` — no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def zscore_of_last(values: list[float]) -> float | None:
    """Z-score of the most recent value against its window (sample stdev)."""
    if len(values) < 2:
        return None
    sd = _stdev(values, sample=True)
    if not sd:
        return None
    return (values[-1] - _mean(values)) / sd


def percentile_of_last(values: list[float]) -> float | None:
    """Fraction of the window at or below the most recent value, 0..1."""
    n = len(values)
    if n < 2:
        return None
    last = values[-1]
    return sum(1 for v in values if v <= last) / n


def trailing_negative_run(values: list[float]) -> int:
    """Count of consecutive most-recent values strictly below zero (e.g. days inverted)."""
    run = 0
    for value in reversed(values):
        if value < 0:
            run += 1
        else:
            break
    return run


def sahm_gap(monthly_unemployment: list[float]) -> float | None:
    """Sahm recession gap: current 3-month average unemployment minus the minimum of that
    same 3-month average over the PREVIOUS 12 months — excluding the current month, per the
    official definition (FRED SAHMREALTIME). Including the current month would clamp the gap
    at ≥ 0 forever: falling unemployment could never print negative. ≥ 0.50 p.p. has
    historically marked the start of a US recession. Needs at least 4 monthly points
    (oldest→newest)."""
    if len(monthly_unemployment) < 4:
        return None
    ma3 = [
        sum(monthly_unemployment[i - 2 : i + 1]) / 3 for i in range(2, len(monthly_unemployment))
    ]
    if len(ma3) < 2:
        return None
    return ma3[-1] - min(ma3[-13:-1])


def recession_probit(spread_10y_3m: float) -> float:
    """12-month-ahead US recession probability from the Treasury term spread (Estrella-Mishkin
    probit used by the NY Fed): P = Φ(-0.5333 - 0.6330 × spread), spread in percentage points."""
    return normal_cdf(-0.5333 - 0.6330 * spread_10y_3m)


def _q(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def _range_position(price: float, low: float | None, high: float | None) -> float | None:
    """Where ``price`` sits in the [low, high] band, 0 (at low) to 1 (at high)."""
    if low is None or high is None or high <= low:
        return None
    return (price - low) / (high - low)


def compute_technicals(
    history: PriceHistory,
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
    overnight_gaps: bool = True,
) -> Technicals:
    """Compute the indicator set from a ``PriceHistory`` (expects daily bars, oldest→newest).

    ``periods_per_year`` annualizes the risk/volatility numbers — 252 for equities (trading
    days), 365 for crypto (24/7). ``overnight_gaps`` picks the OHLC volatility estimator:
    Yang-Zhang when gaps exist (equities), Rogers-Satchell for gapless 24/7 markets (crypto).
    """
    closes = [float(b.close) for b in history.bars if b.close is not None]
    # ATR pairs high/low/close PER BAR, so the three series must come from the same bars.
    # Filtering each independently can produce equal-length but row-shifted series (one bar
    # missing only its high, another only its low) → a confidently wrong ATR.
    hlc = [
        (float(b.high), float(b.low), float(b.close))
        for b in history.bars
        if None not in (b.high, b.low, b.close)
    ]
    aligned = [
        (float(b.open), float(b.high), float(b.low), float(b.close))
        for b in history.bars
        if None not in (b.open, b.high, b.low, b.close)
    ]
    opens_a = [b[0] for b in aligned]
    highs_a = [b[1] for b in aligned]
    lows_a = [b[2] for b in aligned]
    closes_a = [b[3] for b in aligned]

    macd_value, signal_value, histogram = macd(closes)
    # 52 weeks = one YEAR of bars, and a year is ``periods_per_year`` bars — 252 trading days
    # for equities, 365 for 24/7 crypto. A hardcoded 252 would silently make crypto's "52w"
    # range a ~36-week one (same mislabel _momentum_lookbacks guards against).
    recent = history.bars[-periods_per_year:]
    high52 = max((float(b.high) for b in recent if b.high is not None), default=None)
    low52 = min((float(b.low) for b in recent if b.low is not None), default=None)

    returns = pct_returns(closes)
    last_price = closes[-1] if closes else None
    sma_200 = sma(closes, 200)
    atr_14 = atr([b[0] for b in hlc], [b[1] for b in hlc], [b[2] for b in hlc], 14)
    mayer = last_price / sma_200 if last_price and sma_200 and sma_200 > 0 else None
    if overnight_gaps:
        ohlc_vol = yang_zhang_volatility(opens_a, highs_a, lows_a, closes_a, periods_per_year)
        estimator = "yang_zhang"
    else:
        ohlc_vol = rogers_satchell_volatility(opens_a, highs_a, lows_a, closes_a, periods_per_year)
        estimator = "rogers_satchell"
    _mom_lookbacks = _momentum_lookbacks(periods_per_year)

    return Technicals(
        symbol=history.symbol,
        as_of=history.as_of,
        last_price=_q(last_price, 4),
        sma_50=_q(sma(closes, 50), 4),
        sma_200=_q(sma_200, 4),
        mayer_multiple=_q(mayer, 4),
        ema_20=_q(ema_last(closes, 20), 4),
        rsi_14=_q(rsi(closes, 14), 2),
        macd=_q(macd_value, 4),
        macd_signal=_q(signal_value, 4),
        macd_histogram=_q(histogram, 4),
        atr_14=_q(atr_14, 4),
        atr_pct=_q(atr_14 / last_price if atr_14 and last_price else None, 4),
        week52_high=_q(high52, 4),
        week52_low=_q(low52, 4),
        range_position_52w=_q(
            _range_position(last_price, low52, high52) if last_price is not None else None, 4
        ),
        volatility_annualized=_q(realized_volatility(closes, periods_per_year), 4),
        volatility_ohlc=_q(ohlc_vol, 4),
        volatility_estimator=estimator if ohlc_vol is not None else None,
        sharpe_ratio=_q(sharpe_ratio(returns, periods_per_year), 4),
        sortino_ratio=_q(sortino_ratio(returns, periods_per_year), 4),
        max_drawdown=_q(max_drawdown(closes), 4),
        max_drawdown_bars=max_drawdown_duration(closes),
        calmar_ratio=_q(calmar_ratio(closes, periods_per_year), 4),
        return_skew=_q(skewness(returns), 4),
        return_kurtosis=_q(excess_kurtosis(returns), 4),
        momentum_3m=_q(momentum(closes, _mom_lookbacks["momentum_3m"]), 4),
        momentum_6m=_q(momentum(closes, _mom_lookbacks["momentum_6m"]), 4),
        momentum_12m=_q(momentum(closes, _mom_lookbacks["momentum_12m"]), 4),
        momentum_12_1=_q(momentum_12_1(closes, periods_per_year), 4),
        annualization_factor=periods_per_year,
        bars_used=len(closes),
    )


# --- Cointegration / pairs (Engle-Granger two-step) ------------------------------
# StatArb primitive: is the spread between two price series mean-reverting (cointegrated)?
# Step 1 — OLS hedge ratio (regress A on B). Step 2 — a Dickey-Fuller test on the residual spread.
# We report the DF t-statistic against MacKinnon's asymptotic Engle-Granger critical values (the
# RESIDUAL-based ones — more negative than a plain ADF because the spread is itself estimated)
# rather than fabricating a precise p-value: that needs the full response surface, and a
# fake-precise p-value is the "confidently mislabelled number" honesty-over-filling warns against.
# Pure-python, stdlib only — same spirit as the rest of this module (no scipy/statsmodels).

_MIN_OBS_FOR_COINTEGRATION = 30  # below this the DF regression is too short to mean anything
# MacKinnon (1991) asymptotic critical values: constant, NO trend, ONE regressor (a single pair).
EG_CRITICAL_VALUES: dict[str, float] = {"1pct": -3.90, "5pct": -3.34, "10pct": -3.04}


def ols_with_intercept(y: list[float], x: list[float]) -> tuple[float, float] | None:
    """Simple OLS of y on x with an intercept → (slope, intercept). None if x has zero variance."""
    n = len(y)
    if n < 2 or n != len(x):
        return None
    mean_x, mean_y = sum(x) / n, sum(y) / n
    sxx = sum((xi - mean_x) ** 2 for xi in x)
    if sxx == 0:
        return None
    sxy = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


def _df_regression(series: list[float]) -> tuple[float, float] | None:
    """The Dickey-Fuller regression Δs_t = a + γ·s_{t-1} + ε (0 lags). Returns (gamma, t_stat).
    A strongly NEGATIVE t_stat ⇒ the residual is stationary (the pair mean-reverts)."""
    if len(series) < _MIN_OBS_FOR_COINTEGRATION:
        return None
    lagged = series[:-1]
    delta = [series[i] - series[i - 1] for i in range(1, len(series))]
    fit = ols_with_intercept(delta, lagged)
    if fit is None:
        return None
    gamma, intercept = fit
    n = len(delta)
    mean_lag = sum(lagged) / n
    sxx = sum((xi - mean_lag) ** 2 for xi in lagged)
    dof = n - 2
    if sxx <= 0 or dof <= 0:
        return None
    sse = sum((delta[i] - (intercept + gamma * lagged[i])) ** 2 for i in range(n))
    variance = sse / dof
    if variance <= 0:
        return None
    se_gamma = math.sqrt(variance / sxx)
    if se_gamma == 0:
        return None
    return gamma, gamma / se_gamma


def dickey_fuller_tstat(series: list[float]) -> float | None:
    """Dickey-Fuller t-statistic for a residual spread (see ``_df_regression``)."""
    result = _df_regression(series)
    return result[1] if result else None


def mean_reversion_half_life(series: list[float]) -> float | None:
    """Half-life of mean reversion in observations: −ln2/γ from Δs_t = a + γ·s_{t-1} + ε.
    None when the series doesn't revert (γ ≥ 0) or is too short — a non-reverting spread has no
    meaningful half-life, so we return null rather than a negative/absurd number."""
    result = _df_regression(series)
    if result is None:
        return None
    gamma = result[0]
    if gamma >= 0:
        return None
    return -math.log(2) / gamma
