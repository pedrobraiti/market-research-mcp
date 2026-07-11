<p align="center">
  <strong>Scout</strong> — an MCP server that gathers and structures financial data for AI agents.
</p>

<p align="center">
  <em>Valet trades, Scout researches.</em> The <strong>senses</strong> layer of an agentic-trading
  stack — it gives an AI agent (like Claude Code) the data to research any US stock, ETF or crypto
  asset, in depth, from <strong>free</strong> sources. Pairs with
  <a href="https://github.com/pedrobraiti/agentic-trading-mcp">agentic-trading-mcp</a> (execution).
</p>

<p align="center">
  <a href="https://github.com/pedrobraiti/market-research-mcp/actions/workflows/ci.yml"><img src="https://github.com/pedrobraiti/market-research-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
  <img src="https://img.shields.io/badge/tools-62-orange" alt="62 tools">
  <a href="src/scout/healthcheck.py"><img src="https://img.shields.io/badge/status-live--validated-success" alt="Status: live-validated (verify anytime: python -m scout.healthcheck AAPL)"></a>
</p>

> **Not financial advice.** A research and data-gathering tool. It does **not** place orders and
> does **not** decide what to buy — that is the agent's and `agentic-trading-mcp`'s job. Use at your own risk.

## What this is

**Scout** is an **MCP server** exposing **62 purpose-built tools** an AI agent calls to research a
company or market — quotes, fundamentals, technicals, options-implied volatility, SEC filings & XBRL
financials, macro, news, sentiment and attention, plus a full **crypto spot** layer (quotes, OHLCV,
on-chain network health, derivatives funding/OI, DVOL, DeFi TVL/stablecoins/yields, crypto macro and
sentiment) — gathered from **free, keyless** data sources, in parallel, returned as typed, structured data.

It is the **senses** layer of a three-part split:

- **Brain** — [**Vizier**](https://github.com/pedrobraiti/vizier-trading-skill): the Claude Code skill that researches, decides *what/when*, and orchestrates this repo + the execution layer.
- **Senses** — **Scout** (this repo): data & info gathering. **Stateless, data-only.**
- **Hands** — [`agentic-trading-mcp`](https://github.com/pedrobraiti/agentic-trading-mcp): execution on Interactive Brokers (stocks/ETFs) and crypto exchanges (spot, via CCXT).

Scope: **US equities & ETFs** plus **crypto spot** (what the execution side can trade). Every tool
returns an `{"ok": ..., "data": ...}` envelope and most accept an optional `as_of` (point-in-time) date.

> **Scout returns data, not reports.** It gives the agent the numbers and sources; the agent does the
> analysis and produces the output (e.g. a written report, a chart, a PDF). Ask your agent to
> *"research MU and write me a report/PDF"* and it will call Scout's tools, then compose the result itself.

## Quickstart

Scout is a **standard MCP server** — register it with any MCP-capable agent/client. The example
below uses Claude Code (the most common one); the steps are the same elsewhere, only the register
command differs.

```bash
# 1. Install
git clone https://github.com/pedrobraiti/market-research-mcp.git
cd market-research-mcp
python -m venv .venv
# Windows (PowerShell): & ".venv\Scripts\Activate.ps1"   (on a policy error: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass)
# Linux/macOS:          source .venv/bin/activate
pip install -e .

# 2. (recommended) Configure — SEC EDGAR needs an identifiable User-Agent
cp .env.example .env      # then set SCOUT_SEC_USER_AGENT="Your Name your@email.com"

# 3. Register the MCP with Claude Code (use the venv's python — absolute path)
#   Windows:      claude mcp add scout -- "C:\path\to\market-research-mcp\.venv\Scripts\python.exe" -m scout.server.app
#   Linux/macOS:  claude mcp add scout -- /path/to/market-research-mcp/.venv/bin/python -m scout.server.app
```

The tools then appear to the agent (in Claude Code, a **new** session). Verify the data layer works
at any time, independent of any agent:

```bash
python -m scout.healthcheck AAPL   # live smoke test (fetches a real quote)
```

Every tool answers with the same `{"ok": ..., "data": ...}` envelope. `company_snapshot("AAPL")`,
for example, returns:

```json
{
  "ok": true,
  "data": {
    "symbol": "AAPL",
    "name": "Apple Inc.",
    "currency": "USD",
    "price": "230.0",
    "previous_close": "228.0",
    "change": "2.0",
    "change_percent": "0.877193",
    "market_cap": "3500000000000",
    "pe_ratio": "35.5",
    "forward_pe": "30.1",
    "dividend_yield": "0.0044",
    "fifty_two_week_high": "260.1",
    "fifty_two_week_low": "164.0",
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "market_state": "REGULAR",
    "recent_splits": []
  }
}
```

Then just ask the agent in natural language — e.g. *"Use the scout tools to do the most complete
analysis you can of a stock (say MU), then write it up as a PDF with charts."* The agent fans out
across the tools below, then composes the result itself.

> **First-call note.** On start the MCP server warms up its data libraries (yfinance + ccxt) on the
> main thread — this adds **~5s to startup** but is deliberate: those libraries' one-time setup
> otherwise hangs when it first runs inside the async server, which would freeze the very first tool
> call of a session. With the warm-up, the first real call is instant.

**No API keys or logins required.** The only optional setting is `SCOUT_SEC_USER_AGENT` (SEC policy:
an identifiable User-Agent) — without it the SEC tools (`filings`, `sec_financials`, `filing_search`)
return a clear "please set it" message; everything else works keyless.

## Tools (62)

**Discovery — find names, not just look them up**
- `search_symbols(query, limit?)` — company name / partial ticker → symbols (the entry point).
- `market_movers(category?, limit?)` — market-wide top gainers / losers / most-active today.
- `sector_performance(period?, as_of?)` — total return of each US sector (SPDR ETFs) — rotation.
- `etf_holdings(symbol)` — an ETF's declared top holdings & sector weights (open a theme).
- `filing_search(query, forms?, limit?)` — full-text search across **all** SEC filings → companies by what they disclose (thesis → names).
- `news_search(query, limit?, days?)` — free-text news/event search across global media (**GDELT**). Not exhaustive — it can miss capital-structure events (raises, dilution, buybacks, M&A); cross-check `filings(form_type="8-K")` / `filing_search` for those. On a 429/timeout it returns `source_status` (e.g. `unavailable: rate_limited`) so a "couldn't fetch" is distinguishable from "no matches".

**Company deep-dive**
- `company_dossier(symbol, depth?, as_of?)` — **flagship:** snapshot + fundamentals + dividends + technicals + earnings + analyst view + news, gathered **in parallel** (degrades gracefully).
- `company_snapshot(symbol, as_of?)` — price, day move, key multiples, sector/industry.
- `fundamentals(symbol, period?, as_of?)` — income/balance/cash-flow figures + derived margins.
- `quality_metrics(symbol, as_of?)` — ROE/ROA, margins, revenue & earnings growth and CAGR.
- `dividends(symbol, as_of?)` — payment history, trailing yield, growth streak, cut flag.
- `sec_financials(symbol, as_of?)` — **authoritative** annual financials from SEC XBRL (cross-check `fundamentals`).
- `filings(symbol, form_type?, limit?, as_of?)` — recent SEC EDGAR filings (10-K/10-Q/8-K …) with links.
- `earnings(symbol, as_of?)` — next earnings date + history (estimate / actual / surprise).
- `ownership(symbol)` — insider & institution %, top institutions, recent insider trades, **short interest** (days-to-cover, % of float, % change).
- `fda_events(company, limit?)` — FDA drug approvals + recalls for a sponsor (**openFDA**) — pharma/biotech catalysts.

**Price & technicals**
- `price_history(symbol, period?, interval?, as_of?)` — OHLCV bars.
- `technicals(symbol, as_of?)` — SMA(50/200), EMA(20), RSI(14), MACD, ATR(14), 52-week range (raw numbers, no verdict).
- `relative_strength(symbols[], benchmark?, period?, as_of?)` — each name's return vs a benchmark.
- `options_volatility(symbol, expiry?)` — ATM implied vol + options-implied expected move.

**Sentiment & attention**
- `news(symbol, limit?)` — recent headlines (pairs with `extract`).
- `analyst_view(symbol)` — sell-side consensus & price targets (third-party opinion, as data).
- `retail_buzz(symbol?, limit?)` — Reddit (WSB/stocks) mention buzz (**ApeWisdom**) — retail attention.
- `wikipedia_attention(article, days?)` — daily Wikipedia pageviews — an attention proxy.

**Macro**
- `macro_context(as_of?)` — key US macro (**FRED**) + a rich `derived` block: rates/curve/CPI/VIX, plus **Fed net liquidity** (WALCL−TGA−RRP), financial-conditions (NFCI/STLFSI4), jobless claims, growth nowcasts (GDPNow/CFNAI), VIX term structure, 5y5y forward, M2 YoY, SOFR, broad dollar, Brent-WTI, Sahm gap & recession probit.
- `cot_positioning(market, weeks?)` — CFTC **Commitments of Traders**: speculator vs commercial net positioning, % of OI, week-over-week change, crowding z-score.
- `commodity_ratios()` — macro bellwethers **copper/gold** (Dr. Copper, risk appetite) and **gold/silver**, with z-scores (yfinance).
- `world_macro(country?, codes?)` — country-level macro (**World Bank**): GDP, inflation, unemployment…
- `treasury_data()` — official **US Treasury** figures: total public debt, average interest rates.

**Multi-symbol / portfolio (batch, stateless)**
- `compare(symbols[], as_of?)` — several names side by side (price, multiples, margins, sector).
- `correlation_matrix(symbols[], period?, as_of?)` — pairwise return correlation (real diversification).
- `cointegration_test(symbol_a, symbol_b, lookback_days?)` — Engle-Granger **pairs/StatArb**: hedge ratio, residual ADF stat vs critical values, spread z-score, mean-reversion half-life.
- `find_cointegrated_pairs(symbols[], lookback_days?, min_correlation?)` — **pairs discovery**: screen a basket (correlation pre-filter → ADF test), cointegrated pairs ranked strongest-first.
- `classify(symbols[], as_of?)` — sector/industry/market-cap per symbol (to aggregate exposure).
- `news_digest(symbols[], limit_per_symbol?)` — headlines across a watchlist, newest first.
- `calendar(symbols[], as_of?)` — upcoming earnings & ex-dividend dates across symbols, sorted.

**Crypto — price, technicals & discovery** (symbols use the CCXT `BASE/QUOTE` format, e.g. `BTC/USDT` or `BTC`)
- `crypto_quote(symbol)` — live spot quote: last/bid/ask + 24h move (the crypto `company_snapshot`).
- `crypto_price_history(symbol, timeframe?, limit?, as_of?)` — OHLCV candles (CCXT, ~100 exchanges).
- `crypto_technicals(symbol, as_of?)` — SMA/EMA/RSI/MACD/ATR for a pair (same math as `technicals`).
- `crypto_search(query, limit?)` — name / partial symbol → assets (the entry point when you lack the ticker).
- `crypto_movers(category?, limit?)` — top gainers / losers / most-active pairs (no symbol needed, no 429).
- `crypto_order_book(symbol, limit?)` — best bid/ask, spread and depth — a pre-trade liquidity/slippage read.
- `crypto_compare(symbols[])` — several assets side by side (price, market cap, rank, supply).
- `crypto_correlation_matrix(symbols[], timeframe?, limit?)` — pairwise return correlation (diversification).
- `crypto_relative_strength(symbols[], benchmark?, timeframe?, limit?)` — return vs a benchmark (default BTC).

**Crypto — fundamentals, derivatives & sentiment**
- `crypto_dossier(symbol, depth?)` — **flagship:** quote + profile + technicals + Fear & Greed + derivatives + on-chain, **in parallel**.
- `crypto_asset_profile(symbol)` — supply (circ/total/max), market cap, rank, ATH (**Coinpaprika**).
- `crypto_onchain(asset?)` — network health: BTC fees/hashrate (**mempool.space**), ETH gas/addresses (**Blockscout**).
- `crypto_derivatives(symbol)` — perp funding rate & open interest across **Binance/Bybit/OKX** (positioning context).
- `crypto_implied_vol(asset?)` — the **Deribit** DVOL index ("crypto VIX") for BTC/ETH + history.
- `coinbase_premium(symbol?, days?)` — US-spot (Coinbase) vs offshore (Binance) premium — institutional/US demand tell (CCXT).
- `btc_network()` — BTC base-layer: hash rate, miner revenue, **NVT** (real, on-chain settlement) + live fee market & difficulty (**Blockchain.com** + **mempool.space**).
- `crypto_fear_greed(days?)` — the Crypto Fear & Greed Index (0-100) + history (**alternative.me**).
- `crypto_buzz(symbol?, limit?)` — Reddit crypto mention buzz (**ApeWisdom** `all-crypto`).

**Crypto — DeFi & macro**
- `crypto_macro()` — total market cap, BTC/ETH dominance, DeFi share (**CoinGecko**).
- `crypto_sectors()` — per-category (L1/DeFi/AI/memecoin…) performance (**CoinGecko**).
- `defi_overview(slug?)` — DeFi TVL by chain, or one protocol's breakdown (**DefiLlama**).
- `stablecoin_supply()` — stablecoin circulation & peg status (**DefiLlama**) — liquidity/systemic-risk read.
- `stablecoin_peg(symbols?, venue?)` — USDT/USDC/DAI deviation from $1 in basis points + depeg flag (**Kraken/CCXT**) — the price/peg axis.
- `defi_fees(protocol?)` — protocol **fees & revenue** (cash-flow fundamentals): total + top-N, or one protocol (**DefiLlama**).
- `defi_yields(chain?, project?, min_tvl?)` — yield/APY pools, filterable (**DefiLlama**).

**Web**
- `extract(url)` — fetch a page → clean, token-efficient markdown (honestly reports paywalls/blocks).

## Architecture

Hexagonal (ports & adapters), mirroring `agentic-trading-mcp`: the agent talks only to the MCP tools; each
data source is a swappable adapter behind a port, so a free source can be replaced or complemented
without touching the domain. Design principles (stateless, data-not-verdict, point-in-time) in
[DECISIONS.md](DECISIONS.md).

```
domain/      models + ports (the contracts)
adapters/    equities: yfinance, SEC EDGAR, FRED, World Bank, US Treasury, GDELT, ApeWisdom, Wikimedia, web, stooq
             crypto:   CCXT, Coinpaprika, alternative.me, mempool.space/Blockscout, Binance/Bybit/OKX, Deribit,
                       DefiLlama, CoinGecko
research/    meta-tools that fan several ports out in parallel (dossier, compare, correlation, sectors…)
analytics.py source-agnostic indicator math (SMA/EMA/RSI/MACD/ATR, correlation) — reused for crypto
server/      MCP server (FastMCP) + dependency composition
```

## Data sources (all free)

| Source | Provides | Key/login |
|---|---|---|
| **yfinance** | quotes, fundamentals, dividends, prices, options, news, analysts, ownership | none |
| **SEC EDGAR** | filings, XBRL financials, full-text search | User-Agent only |
| **FRED** | US macro (rates, CPI, unemployment, VIX) | none (keyless CSV) |
| **World Bank** | global country-level macro | none |
| **US Treasury** | public debt, average interest rates | none |
| **GDELT** | global news/event search | none |
| **ApeWisdom** | Reddit mention buzz (stocks + crypto) | none |
| **Wikimedia** | Wikipedia pageviews (attention) | none |
| **stooq** | daily prices — transparent fallback for yfinance | none |
| **CCXT** | crypto spot quotes, OHLCV, movers, order book (~100 exchanges) | none (public market data) |
| **Coinpaprika** | crypto supply, market cap, rank, ATH, search | none |
| **alternative.me** | Crypto Fear & Greed Index | none |
| **mempool.space / Blockscout** | on-chain network health (BTC fees/hashrate, ETH gas/addresses) | none |
| **Binance / Bybit / OKX** | perp funding rate & open interest (derivatives context) | none (public) |
| **Deribit** | DVOL implied-volatility index ("crypto VIX") | none |
| **DefiLlama** | DeFi TVL, stablecoin supply/peg, yield pools | none |
| **CoinGecko** | crypto macro (dominance, total mcap) & sector performance | none (rate-limited) |

Paid free-tiers (Finnhub/FMP) are pluggable behind the same ports if ever wanted.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 267 offline tests (fixtures faithful to the real source payloads)
ruff check .       # lint
```

The suite includes a smoke test of the **real MCP stdio transport** (it spawns
`python -m scout.server.app` and drives it as an MCP client) — a class of bug that only reproduces
over the actual transport, never through the service layer, is caught there.

## License

MIT.
