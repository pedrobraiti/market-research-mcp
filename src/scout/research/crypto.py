"""Crypto research aggregators — meta-tools composing several crypto port reads into one result.

Mirror the equities aggregators (compare / correlation / relative_strength / dossier) for crypto.
Stateless: pure aggregation, no judgment, no persistence. They orchestrate the crypto ports and
never reach a concrete adapter.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from ..analytics import compute_technicals, pct_returns, pearson
from ..domain.models import (
    CryptoComparison,
    CryptoComparisonRow,
    CryptoCorrelation,
    CryptoCorrelationPair,
    CryptoDossier,
    CryptoRelativeStrength,
    CryptoRelStrengthRow,
    PriceBar,
    PriceHistory,
)
from ..domain.ports import (
    CryptoAssetSource,
    CryptoDerivativesSource,
    CryptoMarketDataSource,
    CryptoOnChainSource,
    CryptoSentimentSource,
)
from .notes import unavailable_or_not_found


def _q(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, places)))


async def build_crypto_comparison(
    market: CryptoMarketDataSource,
    assets: CryptoAssetSource,
    symbols: list[str],
) -> CryptoComparison:
    clean = [s.strip() for s in symbols if s.strip()]

    async def _one(symbol: str) -> CryptoComparisonRow:
        base = symbol.replace("-", "/").replace("_", "/").split("/")[0].upper()
        quote, profile = await asyncio.gather(
            market.get_quote(symbol), assets.get_profile(base), return_exceptions=True
        )
        quote_val = quote if not isinstance(quote, Exception) else None
        profile_val = profile if not isinstance(profile, Exception) else None
        if quote_val is None and profile_val is None:
            failure = next((r for r in (quote, profile) if isinstance(r, Exception)), None)
            return CryptoComparisonRow(
                symbol=symbol, base=base, note=unavailable_or_not_found(failure)
            )
        quote, profile = quote_val, profile_val
        return CryptoComparisonRow(
            symbol=quote.symbol if quote else symbol,
            base=base,
            last=quote.last if quote else None,
            change_percent_24h=quote.change_percent_24h if quote else None,
            market_cap_usd=profile.market_cap_usd if profile else None,
            rank=profile.rank if profile else None,
            circulating_supply=profile.circulating_supply if profile else None,
        )

    rows = await asyncio.gather(*(_one(s) for s in clean))
    return CryptoComparison(items=list(rows))


def _closes_by_day(bars: list) -> dict:
    out: dict = {}
    for bar in bars:
        if bar.close is None or not isinstance(bar.timestamp, datetime):
            continue
        out[bar.timestamp.date()] = float(bar.close)
    return out


async def build_crypto_correlation(
    market: CryptoMarketDataSource,
    symbols: list[str],
    timeframe: str = "1d",
    limit: int = 120,
) -> CryptoCorrelation:
    clean = [s.strip().upper() for s in symbols if s.strip()]
    histories = await asyncio.gather(
        *(market.get_price_history(s, timeframe, limit) for s in clean),
        return_exceptions=True,
    )
    closes: dict[str, dict] = {}
    notes: list[str] = []
    for symbol, history in zip(clean, histories, strict=False):
        if isinstance(history, Exception) or history is None or not history.bars:
            notes.append(f"{symbol}: price history unavailable")
            continue
        closes[symbol] = _closes_by_day(history.bars)

    valid = [s for s in clean if s in closes]
    if len(valid) < 2:
        return CryptoCorrelation(
            timeframe=timeframe, note="Need at least two pairs with price data to correlate."
        )
    common = sorted(set.intersection(*(set(closes[s]) for s in valid)))
    if len(common) < 3:
        return CryptoCorrelation(
            timeframe=timeframe, bars_used=len(common), note="Too few overlapping bars."
        )
    returns = {s: pct_returns([closes[s][d] for d in common]) for s in valid}
    pairs: list[CryptoCorrelationPair] = []
    for i, a in enumerate(valid):
        for b in valid[i + 1 :]:
            pairs.append(
                CryptoCorrelationPair(a=a, b=b, correlation=_q(pearson(returns[a], returns[b]), 3))
            )
    return CryptoCorrelation(
        timeframe=timeframe,
        bars_used=len(common),
        pairs=pairs,
        note="; ".join(notes) or None,
    )


async def build_crypto_relative_strength(
    market: CryptoMarketDataSource,
    symbols: list[str],
    benchmark: str = "BTC",
    timeframe: str = "1d",
    limit: int = 90,
) -> CryptoRelativeStrength:
    clean = [s.strip().upper() for s in symbols if s.strip()]
    all_symbols = [benchmark.strip().upper(), *clean]
    histories = await asyncio.gather(
        *(market.get_price_history(s, timeframe, limit) for s in all_symbols),
        return_exceptions=True,
    )

    def _ret(history) -> float | None:
        if isinstance(history, Exception) or history is None or not history.bars:
            return None
        closes = [float(b.close) for b in history.bars if b.close is not None]
        if len(closes) < 2 or closes[0] == 0:
            return None
        return (closes[-1] / closes[0] - 1) * 100

    returns = dict(zip(all_symbols, (_ret(h) for h in histories), strict=False))
    bench = benchmark.strip().upper()
    bench_ret = returns.get(bench)
    notes: list[str] = []
    if bench_ret is None:
        notes.append(f"{bench}: benchmark price history unavailable — excess not computed")
    rows = []
    for s in clean:
        if returns.get(s) is None:
            notes.append(f"{s}: price history unavailable")
            continue
        rows.append(
            CryptoRelStrengthRow(
                symbol=s,
                return_percent=_q(returns[s], 2),
                excess_vs_benchmark=(
                    _q(returns[s] - bench_ret, 2) if bench_ret is not None else None
                ),
            )
        )
    rows.sort(key=lambda r: r.return_percent or Decimal(0), reverse=True)
    return CryptoRelativeStrength(benchmark=bench, timeframe=timeframe, rows=rows, notes=notes)


async def build_crypto_dossier(
    market: CryptoMarketDataSource,
    assets: CryptoAssetSource,
    sentiment: CryptoSentimentSource,
    derivatives: CryptoDerivativesSource,
    onchain: CryptoOnChainSource,
    symbol: str,
    depth: str = "full",
) -> CryptoDossier:
    base = symbol.replace("-", "/").replace("_", "/").split("/")[0].upper()
    notes: list[str] = []

    quote_t = market.get_quote(symbol)
    profile_t = assets.get_profile(base)
    if depth == "summary":
        quote, profile = await asyncio.gather(quote_t, profile_t, return_exceptions=True)
        history = fear = deriv = chain = None
    else:
        results = await asyncio.gather(
            quote_t,
            profile_t,
            # 400 daily candles (> 365 = one 24/7 year): momentum_12m/12-1 and the true 52-week
            # range need a full crypto year of bars — 300 left them structurally null.
            market.get_price_history(symbol, "1d", 400),
            sentiment.get_fear_greed(14),
            derivatives.get_derivatives(base),
            onchain.get_onchain(base),
            return_exceptions=True,
        )
        quote, profile, history, fear, deriv, chain = results

    def _val(x, label):
        if isinstance(x, Exception):
            notes.append(f"{label}: {x}")
            return None
        return x

    quote = _val(quote, "quote")
    profile = _val(profile, "profile")
    history = _val(history, "price_history")
    fear = _val(fear, "fear_greed")
    deriv = _val(deriv, "derivatives")
    chain = _val(chain, "onchain")

    technicals = None
    if history is not None and history.bars:
        bars = [
            PriceBar(date=b.timestamp.date(), open=b.open, high=b.high, low=b.low, close=b.close)
            for b in history.bars
        ]
        technicals = compute_technicals(
            PriceHistory(symbol=history.symbol, interval=history.timeframe, bars=bars),
            periods_per_year=365,
            overnight_gaps=False,
        )

    quote_ccy = quote.quote if quote else ""
    return CryptoDossier(
        symbol=quote.symbol if quote else symbol,
        base=base,
        quote=quote_ccy,
        quote_data=quote,
        profile=profile,
        technicals=technicals,
        fear_greed=fear,
        derivatives=deriv,
        onchain=chain,
        notes=notes,
    )
