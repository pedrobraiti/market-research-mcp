from datetime import date, timedelta
from decimal import Decimal

from scout.analytics import (
    atr,
    calmar_ratio,
    compute_technicals,
    ema_last,
    excess_kurtosis,
    log_returns,
    macd,
    max_drawdown,
    max_drawdown_duration,
    momentum,
    momentum_12_1,
    normal_cdf,
    pct_returns,
    pearson,
    percentile_of_last,
    realized_volatility,
    recession_probit,
    rogers_satchell_volatility,
    rsi,
    sahm_gap,
    sharpe_ratio,
    skewness,
    sma,
    sortino_ratio,
    trailing_negative_run,
    yang_zhang_volatility,
    zscore_of_last,
)
from scout.domain.models import PriceBar, PriceHistory


def test_sma_exact():
    assert sma([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5) == 8.0
    assert sma([1, 2, 3], 5) is None


def test_rsi_extremes():
    rising = list(range(1, 30))
    falling = list(range(30, 1, -1))
    assert rsi(rising, 14) == 100.0  # only gains
    assert rsi(falling, 14) == 0.0  # only losses
    assert rsi([1, 2, 3], 14) is None  # not enough data


def test_ema_last_tracks_recent_values():
    value = ema_last([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
    assert value is not None
    assert 7.0 < value < 10.0  # weighted toward the recent higher values


def test_macd_needs_enough_data():
    assert macd([1, 2, 3]) == (None, None, None)
    line, signal, hist = macd(list(range(1, 60)))
    assert line is not None and signal is not None and hist is not None


def test_atr_constant_range():
    highs = [i + 1 for i in range(1, 30)]
    lows = [i - 1 for i in range(1, 30)]
    closes = list(range(1, 30))
    assert atr(highs, lows, closes, 14) == 2.0
    assert atr([1], [1], [1], 14) is None


def test_pct_returns():
    result = pct_returns([10.0, 11.0, 12.0])
    assert abs(result[0] - 0.1) < 1e-9
    assert abs(result[1] - 0.090909) < 1e-5


def test_pearson_correlation():
    assert pearson([1, 2, 3], [2, 4, 6]) == 1.0  # perfectly positively correlated
    assert pearson([1, 2, 3], [6, 4, 2]) == -1.0  # perfectly negatively correlated
    assert pearson([1, 2, 3], [1, 1, 1]) is None  # zero variance → undefined
    assert pearson([1, 2], [1]) is None  # mismatched lengths


def test_log_returns_skips_nonpositive():
    rets = log_returns([10.0, 11.0, 0.0, 12.0])
    assert len(rets) == 1  # only the 10→11 step is valid (0 breaks ln on both sides)
    assert abs(rets[0] - 0.0953) < 1e-3


def test_realized_volatility_minimum_and_positivity():
    assert realized_volatility([1.0, 2.0, 3.0]) is None  # not enough returns
    closes = [100.0 * (1.01 ** i if i % 2 == 0 else 1.005 ** i) for i in range(60)]
    vol = realized_volatility(closes, periods_per_year=252)
    assert vol is not None and vol > 0


def test_ohlc_volatility_estimators_positive_on_ramp():
    opens = [float(i) for i in range(1, 40)]
    highs = [i + 1.0 for i in range(1, 40)]
    lows = [i - 1.0 for i in range(1, 40)]
    closes = [float(i) for i in range(1, 40)]
    rs = rogers_satchell_volatility(opens, highs, lows, closes)
    yz = yang_zhang_volatility(opens, highs, lows, closes)
    assert rs is not None and rs > 0
    assert yz is not None and yz > 0
    assert rogers_satchell_volatility([1.0], [1.0], [1.0], [1.0]) is None  # too short


def test_sharpe_and_sortino_need_sample_and_reward_consistency():
    assert sharpe_ratio([0.01, 0.01]) is None  # below minimum sample
    steady = [0.01, -0.002] * 30  # positive drift, modest noise
    volatile = [0.05, -0.045] * 30  # same-ish drift, far more noise
    s_steady = sharpe_ratio(steady)
    s_volatile = sharpe_ratio(volatile)
    assert s_steady is not None and s_volatile is not None
    assert s_steady > s_volatile  # less noise → higher Sharpe
    assert sortino_ratio(steady) is not None


def test_max_drawdown_and_duration():
    closes = [100.0, 120.0, 90.0, 130.0]
    assert abs(max_drawdown(closes) - (-0.25)) < 1e-9  # 120 → 90 is the worst
    assert max_drawdown_duration(closes) == 1  # one bar spent below the prior peak
    assert max_drawdown([100.0, 110.0, 120.0]) == 0.0  # monotonic up → no drawdown


def test_calmar_ratio_rewards_return_over_pain():
    rising = [100.0 + i for i in range(40)]  # steady climb, tiny drawdowns
    calmar = calmar_ratio(rising, periods_per_year=252)
    assert calmar is None or calmar > 0  # no real drawdown may yield None or a positive ratio


def test_skewness_symmetric_is_near_zero():
    symmetric = [-2.0, -1.0, 0.0, 1.0, 2.0] * 12  # 60 points, symmetric
    skew = skewness(symmetric)
    assert skew is not None and abs(skew) < 1e-6
    assert excess_kurtosis(symmetric) is not None
    assert skewness([1.0, 2.0, 3.0]) is None  # below minimum sample


def test_momentum_horizons():
    closes = [float(i) for i in range(1, 11)]  # 1..10
    assert abs(momentum(closes, 2) - (10.0 / 8.0 - 1)) < 1e-9
    assert momentum(closes, 20) is None  # lookback exceeds series
    assert momentum_12_1([1.0, 2.0, 3.0]) is None  # needs > 252 bars


def test_momentum_lookbacks_are_calendar_aware():
    # A 'momentum_6m' field must mean 6 calendar months. Equities use 252 trading days/yr,
    # crypto 365 (24/7) — so the bar counts must scale, or crypto's '6m' silently measures ~4.
    from scout.analytics import _momentum_lookbacks

    assert _momentum_lookbacks(252) == {"momentum_3m": 63, "momentum_6m": 126, "momentum_12m": 252}
    assert _momentum_lookbacks(365) == {"momentum_3m": 91, "momentum_6m": 182, "momentum_12m": 365}
    # crypto 12-1 needs a full 365-bar year, not 252
    flat = [100.0] * 360
    assert momentum_12_1(flat, periods_per_year=365) is None  # 360 < 365 → insufficient
    assert momentum_12_1([100.0] * 370, periods_per_year=365) == 0.0


def test_normal_cdf_known_points():
    assert abs(normal_cdf(0.0) - 0.5) < 1e-12
    assert abs(normal_cdf(1.96) - 0.975) < 1e-3
    assert abs(normal_cdf(-1.96) - 0.025) < 1e-3


def test_zscore_and_percentile_of_last():
    values = [10.0] * 9 + [20.0]  # last value is an outlier high
    z = zscore_of_last(values)
    assert z is not None and z > 2
    assert percentile_of_last(values) == 1.0  # last is >= everything
    assert zscore_of_last([5.0]) is None  # too short


def test_trailing_negative_run():
    assert trailing_negative_run([0.3, 0.1, -0.05, -0.1, -0.2]) == 3
    assert trailing_negative_run([0.3, 0.2, 0.1]) == 0
    assert trailing_negative_run([-0.1, -0.2]) == 2


def test_sahm_gap_rising_unemployment():
    rising = [3.5, 3.5, 3.5, 3.6, 3.7, 3.8, 3.9, 4.0, 4.1, 4.2, 4.3, 4.4]
    gap = sahm_gap(rising)
    assert gap is not None and gap > 0.5  # clears the recession threshold
    assert sahm_gap([4.0, 4.0, 4.0]) is None  # needs at least 4 months


def test_sahm_gap_falling_unemployment_is_negative():
    # Official definition (SAHMREALTIME): current 3m MA minus the min of the PREVIOUS 12
    # months' 3m MA. Including the current month in the min clamps the gap at ≥ 0 — a falling
    # labour market could never print an improving (negative) gap.
    falling = [4.6, 4.5, 4.4, 4.3, 4.2, 4.1, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5]
    gap = sahm_gap(falling)
    assert gap is not None and gap < 0


def test_recession_probit_monotonic_and_bounded():
    inverted = recession_probit(-1.0)  # inverted curve → higher recession odds
    steep = recession_probit(2.5)  # steep curve → low odds
    assert 0.0 < steep < inverted < 1.0
    assert recession_probit(0.0) == normal_cdf(-0.5333)


def _ramp_history(n: int) -> PriceHistory:
    base = date(2024, 1, 1)
    bars = [
        PriceBar(
            date=base + timedelta(days=i),
            open=Decimal(i),
            high=Decimal(i + 1),
            low=Decimal(i - 1),
            close=Decimal(i),
            volume=1000,
        )
        for i in range(1, n + 1)
    ]
    return PriceHistory(symbol="TEST", interval="1d", bars=bars, as_of=None)


def test_compute_technicals_on_ramp():
    tech = compute_technicals(_ramp_history(250))
    assert tech.last_price == Decimal("250")
    assert tech.sma_50 == Decimal("225.5")
    assert tech.sma_200 == Decimal("150.5")
    assert tech.rsi_14 == Decimal("100")
    assert tech.atr_14 == Decimal("2")
    assert tech.week52_high == Decimal("251")
    assert tech.week52_low == Decimal("0")
    assert tech.bars_used == 250
    assert tech.macd is not None


def test_compute_technicals_short_series_partial():
    tech = compute_technicals(_ramp_history(10))
    assert tech.last_price == Decimal("10")
    assert tech.sma_50 is None  # not enough bars
    assert tech.volatility_annualized is None  # below the vol minimum too
    assert tech.bars_used == 10


def test_compute_technicals_atr_aligns_high_low_close_per_bar():
    # Three trailing bars each missing a DIFFERENT field: filtering highs/lows/closes
    # independently yields equal-length but ROW-SHIFTED series → a silently wrong ATR on this
    # constant-range-2 ramp. ATR must pair high/low/close from the SAME bar, dropping any
    # partial bar — the remaining bars are gap-free, so the true ATR is exactly 2.
    history = _ramp_history(60)
    history.bars[-3].high = None
    history.bars[-2].low = None
    history.bars[-1].close = None
    tech = compute_technicals(history)
    assert tech.atr_14 == Decimal("2")


def test_compute_technicals_populates_derived_layer():
    tech = compute_technicals(_ramp_history(260))
    assert tech.volatility_annualized is not None
    assert tech.volatility_ohlc is not None
    assert tech.volatility_estimator == "yang_zhang"  # equities default → gaps modeled
    assert tech.mayer_multiple is not None  # 260 / SMA200(160.5) ≈ 1.62
    assert Decimal("1.5") < tech.mayer_multiple < Decimal("1.7")
    assert tech.atr_pct is not None and tech.atr_pct > 0
    assert tech.range_position_52w is not None
    assert tech.sharpe_ratio is not None
    assert tech.momentum_12m is not None
    assert tech.momentum_12_1 is not None
    assert tech.max_drawdown is not None
    assert tech.annualization_factor == 252


def test_compute_technicals_crypto_calibration():
    tech = compute_technicals(_ramp_history(260), periods_per_year=365, overnight_gaps=False)
    assert tech.annualization_factor == 365
    assert tech.volatility_estimator == "rogers_satchell"  # no overnight gap in 24/7 markets


def test_compute_technicals_crypto_week52_and_momentum_use_365_bars():
    # 380 daily crypto bars (ramp 1..380). "52 weeks" in a 24/7 market is 365 BARS, not 252:
    # a hardcoded 252 makes week52_low the low of bar 129 (= 128) — a ~36-week range
    # confidently labelled 52w. And a full crypto year present → momentum_12m must populate.
    tech = compute_technicals(_ramp_history(380), periods_per_year=365, overnight_gaps=False)
    assert tech.week52_high == Decimal("381")  # high of the last bar
    assert tech.week52_low == Decimal("15")  # low of bar 16, the first of the last 365
    assert tech.momentum_12m is not None  # closes[-1]/closes[-366] − 1
    assert tech.momentum_12_1 is not None
