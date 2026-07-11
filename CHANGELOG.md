# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project predates semantic versioning, so
dates anchor the entries.

## [Unreleased]

## [0.6.0] — 2026-07-11

### Fixed
- **`defi_overview(slug=...)` no longer inflates protocol TVL ~2×.** DefiLlama's
  `currentChainTvls` carries, besides the per-chain figures, the cross-chain aggregates
  `borrowed`/`staking`/`pool2` AND their per-chain twins (`Ethereum-staking` duplicates the same
  dollars as `staking`); the old filter only excluded the `-borrowed` suffix, so aave's TVL came out
  ~$24.4B against the ~$13.7B headline. Protocol TVL now sums CHAIN keys only, and the test fixture
  replicates the real payload shape (the permissive fixture is what hid this).
- **`cpi_3m_annualized` measured a 4-month window and annualized it as a quarter.** The lookup
  walked back `latest − 92 days` over a monthly series dated on the 1st, which lands on the print
  from four months ago in most months, then raised to the 4th power (~+1pp systematic error at 3%
  inflation). It now indexes the monthly series by position (`cpi[-4]`).
- **The price fallback no longer swallows `SourceUnavailable`.** A yfinance 429 on a symbol stooq
  cannot serve (futures like `GC=F` in `commodity_ratios`/`cointegration_test`) used to come back
  as `None` — indistinguishable from "no such symbol", defeating the whole "unavailable ≠ no data"
  machinery. If the primary raised `SourceUnavailable` and the fallback produced no bars, the
  original exception now propagates.
- **Crypto `week52_high/low` was really a ~36-week range, and `momentum_12m`/`momentum_12_1` could
  never populate.** The 52-week window was fixed at 252 bars (the EQUITY year) and crypto tools
  fetched only 300 daily candles (a crypto year is 365). The window now scales with
  `periods_per_year` and `crypto_technicals`/`crypto_dossier` fetch 400 candles.
- **`company_snapshot(as_of=...)` never returned the close of `as_of` itself** (yfinance's `end` is
  exclusive) **and returned `price=None` in the first days of any month** (the window started on the
  1st). The window is now ~10 calendar days ending at `as_of + 1 day`, and the fake ticker in tests
  now reproduces yfinance's end-exclusive semantics.
- **The Sahm gap could never go negative.** The 12-month minimum included the current month, so the
  reported gap floored at 0 and suppressed the informative "labor market improving" negative reading
  (the official SAHMREALTIME rule excludes the current month). Now `min(ma3[-13:-1])`; the ≥0.50
  trigger was never affected.
- The expiry in the options-volatility test fixture is now relative to today — the hardcoded
  `2026-07-10` expired on 2026-07-11 and turned CI red.
- `dividends` no longer raises on Feb 29 (`date.replace(year=...)` guard); `_cagr` annualizes over
  the real year span instead of counting non-null values; ATR aligns closes/highs/lows per-bar
  (the same technique the OHLC volatility block already used); a non-transient FRED series error
  now surfaces as an indicator with an error status instead of silently vanishing from the list.

### Added
- **Price provenance.** `PriceHistory` carries a `source` field ("yfinance"/"stooq") filled by the
  fallback wrapper; the cointegration note now names the real source per leg instead of
  unconditionally claiming yfinance-adjusted closes (adjustment policies differ between sources —
  a pair with legs from different sources is now flagged, honesty over silence).
- `relative_strength` reports each symbol's effective `window_start`, so a short-history name (a
  recent IPO) can no longer masquerade as comparable to a full-window benchmark in silence.
- README: dynamic CI badge, real test count, the `company_snapshot` example envelope, and a note on
  the MCP-transport smoke test.

## [0.5.4] — 2026-07-01

### Fixed
- **The startup warm-up is now time-bounded (~10s per request).** `try/except` protected the
  warm-up against *errors* but not against *hangs*: an unbounded network stall before `mcp.run()`
  would delay the MCP handshake past the client's startup timeout and kill the whole server —
  worse than starting cold (curl_cffi's session default is 30s and yfinance's crumb setup can
  chain several requests, so the worst case reached minutes). The yfinance warm-up now runs on a
  dedicated curl_cffi session with a 10s per-request timeout (restored to the default afterwards,
  so runtime calls keep today's behavior); the ccxt warm-up passes `timeout: 10000` to the
  exchange. Startup is now bounded even in the worst network pathology, ~5s typical.

### Added
- **MCP-transport smoke test (`tests/test_mcp_transport.py`) — the v0.5.3 lesson, encoded.** The
  first-call hang class only reproduces over the REAL MCP stdio transport (spawn the server, speak
  MCP), never through the service layer or plain unit tests — which is why nothing caught it for
  four incidents. The new test spawns `python -m scout.server.app`, completes the handshake,
  asserts the full tool listing (62 — a drift tripwire), and makes the FIRST call down each
  lazy-setup path (yfinance `price_history`, ccxt `crypto_quote`) under a hard timeout. It asserts
  "responds in bounded time", not "returns data" — an honest error envelope passes — so it is
  CI-safe and passes offline. Runs in ~8s.

## [0.5.3] — 2026-06-30

### Fixed
- **The first equity/ETF and first crypto tool call of every fresh MCP session no longer hangs.**
  This was THE recurring "a new `/vizier` chat freezes on its first Scout call, works after a
  retry" bug. Root cause: yfinance's one-time curl_cffi/crumb setup, and ccxt's first
  `load_markets`, **hang when they first run under the FastMCP async server** (yfinance in a worker
  thread, ccxt's aiohttp init) — but complete fine on the main thread / in a plain unit test, which
  is why it never showed up until it was exercised over the real MCP stdio transport. The fix warms
  both libraries at server startup on the main thread (`_warm_up_yfinance` + `_warm_up_ccxt`, both
  best-effort so a network failure never blocks startup); the server now takes ~5s to start but the
  first real call is instant instead of hanging. Verified over the real MCP transport: first
  `search_symbols`/`price_history` ~0.3s, first `crypto_quote`/`crypto_derivatives` ~1-4s (all were
  hanging &gt;40s before).
- The yfinance and web adapters now offload blocking work with `anyio.to_thread.run_sync` (the
  anyio-native primitive) instead of `asyncio.to_thread`, matching the FastMCP anyio runtime.

## [0.5.2] — 2026-06-30

### Fixed
- **`btc_network` history NVT is no longer always null.** `_build_history` joined the market-cap
  series to the hash-rate grid by exact unix timestamp, but Blockchain.com's market-cap chart uses
  intraday timestamps while hash-rate/tx-volume are midnight-aligned — so every `history[].nvt` came
  back null even when the headline NVT computed. It now joins by calendar date. (Found by user-sim.)
- **`cointegration_test` discloses the lookback floor.** A `lookback_days` below the 30-observation
  minimum was silently raised to 30 while the response still echoed the requested value; the `note`
  now states when the floor was applied and how many observations were actually used.

## [0.5.1] — 2026-06-30

### Added
- **`find_cointegrated_pairs`** (ADR-024): pairs *discovery* on top of `cointegration_test` — screen
  a basket of symbols (return-correlation pre-filter → Engle-Granger ADF test) and return the
  cointegrated pairs ranked strongest-first, each with hedge ratio, spread z-score and half-life.
  Multiple-testing honest (reports `pairs_tested` + the 1% level, steers to the strongest hits);
  the universe is capped (40) and unfetchable symbols are named in `source_status` (rate-limit aware).

## [0.5.0] — 2026-06-30

Eight new keyless tools (53 → 61) plus a major macro expansion, all free/keyless and following the
source-honesty convention (a throttle surfaces as `unavailable: …`, never a fabricated reading).

### Added
- **Macro expansion in `macro_context`** (ADR-013): 23 new keyless FRED series (11 → 34) and 12
  derived transforms — **Fed net liquidity** (WALCL − TGA − RRP), financial-conditions (NFCI,
  STLFSI4), jobless claims, growth nowcasts (GDPNow, CFNAI), VIX term structure, 5y5y forward,
  M2 YoY, SOFR, broad dollar, Brent-WTI spread.
- **Short interest** folded into `ownership` (ADR-014): shares short, days-to-cover, % of float,
  % of shares outstanding, and the month-over-month change (yfinance).
- **`cot_positioning`** (ADR-015): CFTC Commitments of Traders — speculator/commercial net
  positioning, % of open interest, week-over-week change and a crowding z-score (keyless Socrata).
- **`coinbase_premium`** (ADR-016): US-spot (Coinbase) vs offshore (Binance) premium + z-score (CCXT).
- **`stablecoin_peg`** (ADR-017): USDT/USDC/DAI deviation from $1 in bps + depeg flag (Kraken/CCXT).
- **`btc_network`** (ADR-018): BTC base-layer fundamentals — hash rate, miner revenue, real NVT
  (on-chain settlement volume) — plus the live fee market and difficulty (Blockchain.com +
  mempool.space), composed with per-source `partial` honesty.
- **`defi_fees`** (ADR-019): protocol fees & revenue (cash-flow fundamentals), total + top-N or a
  single protocol (DefiLlama).
- **`fda_events`** (ADR-020): FDA drug approvals + recalls for a sponsor (openFDA) — pharma/biotech
  catalysts.
- **`commodity_ratios`** (ADR-022): copper/gold (Dr. Copper) and gold/silver bellwethers with
  z-scores (yfinance).
- **`cointegration_test`** (ADR-023): Engle-Granger pairs/StatArb — hedge ratio, residual ADF stat
  vs MacKinnon critical values, spread z-score and mean-reversion half-life (pure-Python, no
  scipy/statsmodels).

### Notes
- `clinical_trials` (ClinicalTrials.gov v2) was built then withdrawn: the host blocks by TLS
  client fingerprint (curl 200 / httpx 403), so a clean keyless integration isn't possible without
  browser-impersonation. Recorded in ADR-021 so it isn't re-attempted blind.

## [0.4.1] — 2026-06-28

### Fixed
- **yfinance's own throttle now counts as rate-limited.** `classify_transient` only recognized a
  429 via an httpx `response`; yfinance raises `YFRateLimitError` (no `.response`), so the primary
  equity source's most common throttle slipped through as a genuine `error` — no retry, wrong bucket.
  It is now matched by class name (`*RateLimit*`) → backed off and labelled `unavailable: rate_limited`.
- **Crypto momentum fields are now calendar-aware.** Lookbacks were fixed bar counts (63/126/252), so
  a crypto `momentum_6m` (24/7, ~365 bars/yr) silently measured ~4 months. They now scale with
  `periods_per_year` (equities 63/126/252, crypto 91/182/365; `momentum_12_1` likewise) so a labelled
  horizon means that many calendar months on both asset classes.
- **A DeFi-only `crypto_macro` failure is now flagged.** When only the DeFi leg failed (headline fine),
  the null `defi_market_cap_usd`/`defi_dominance` read as real zeros. A new scoped `defi_status` flags
  it without touching the headline `status` (which would wrongly imply the whole reading is unavailable).

## [0.4.0] — 2026-06-28

### Added
- **Full crypto spot layer — 21 tools**, mirroring the equities side, all free & keyless. Symbols
  use the CCXT `BASE/QUOTE` format (e.g. `BTC/USDT`) so a researched asset maps to a tradable pair.
  Read-only & external — Scout never reads an exchange account (that is the execution side's job).
  New config `SCOUT_CRYPTO_EXCHANGE` / `SCOUT_CRYPTO_QUOTE_CCY`; new dependency `ccxt`.
  - **Price/technicals/discovery:** `crypto_quote`, `crypto_price_history`, `crypto_technicals`
    (reuses `analytics.compute_technicals`), `crypto_search`, `crypto_movers`, `crypto_order_book`,
    `crypto_compare`, `crypto_correlation_matrix`, `crypto_relative_strength` (CCXT, Coinpaprika).
  - **Fundamentals/derivatives/sentiment:** `crypto_dossier` (flagship parallel fan-out),
    `crypto_asset_profile` (Coinpaprika), `crypto_onchain` (mempool.space BTC / Blockscout ETH),
    `crypto_derivatives` (Binance/Bybit/OKX funding & OI — context only), `crypto_implied_vol`
    (Deribit DVOL), `crypto_fear_greed` (alternative.me), `crypto_buzz` (ApeWisdom `all-crypto`).
  - **DeFi/macro:** `crypto_macro` & `crypto_sectors` (CoinGecko), `defi_overview`,
    `stablecoin_supply`, `defi_yields` (DefiLlama).
- **53 MCP tools** across 17 free, keyless data sources (yfinance, SEC EDGAR, FRED, World Bank,
  US Treasury, GDELT, ApeWisdom, Wikimedia, plus stooq as a price fallback; and for crypto: CCXT,
  Coinpaprika, alternative.me, mempool.space, Blockscout, Binance/Bybit/OKX, Deribit, DefiLlama,
  CoinGecko):
  - **Discovery:** `search_symbols`, `market_movers`, `sector_performance`, `etf_holdings`,
    `filing_search` (SEC full-text), `news_search` (GDELT).
  - **Company deep-dive:** `company_dossier`, `company_snapshot`, `fundamentals`, `quality_metrics`,
    `dividends`, `sec_financials` (XBRL), `filings`, `earnings`, `ownership`.
  - **Price & technicals:** `price_history`, `technicals`, `relative_strength`, `options_volatility`.
  - **Sentiment & attention:** `news`, `analyst_view`, `retail_buzz`, `wikipedia_attention`.
  - **Macro:** `macro_context` (FRED), `world_macro` (World Bank), `treasury_data` (US Treasury).
  - **Multi-symbol / portfolio:** `compare`, `correlation_matrix`, `classify`, `news_digest`, `calendar`.
  - **Web capture:** `extract` (URL → clean markdown).
- Stateless design with an optional point-in-time `as_of` on every research read.
- Hexagonal architecture (ports & adapters); each source is injectable for offline tests.
- `company_dossier` fans snapshot, fundamentals, dividends, technicals, earnings, analyst view
  and news out in parallel, degrading gracefully on partial failure.
- Indicator math (`analytics.py`): SMA/EMA/RSI/MACD/ATR, 52-week range, return correlation.
- **Derived risk/volatility/momentum layer** on `technicals` & `crypto_technicals` (same pure
  `analytics.py`, no new deps, no new tools — additive fields, each null until its own minimum
  sample is met): annualized realized volatility (close-to-close) and an OHLC estimator
  (**Yang-Zhang** for equities, **Rogers-Satchell** for gapless 24/7 crypto), `atr_pct`,
  52-week range position, **Sharpe / Sortino / Calmar**, max drawdown (+ underwater bars),
  return skew & excess kurtosis, and momentum (3m/6m/12m and the 12-1 factor). Asset-class
  aware: ratios annualize at 252 (equities) / 365 (crypto), surfaced via `annualization_factor`.
  Turns OHLCV the Scout already fetched — but previously reduced to the close — into decision-grade
  risk numbers. See ADR-006.
- **Derived macro regime layer** on `macro_context`: the FRED adapter already downloaded each full
  series and then kept only the last point — it now also returns a `derived` block computed from
  those same series (no new sources beyond adding `DGS3MO`): **CPI inflation YoY** and 3-month
  annualized (a raw CPI index level is meaningless alone), **real Fed-funds & real 10y** (ex-post),
  the **Sahm recession gap** (+ signal), **VIX z-score & percentile** vs its ~1y window, **yield-curve
  inversion** (+ consecutive observations inverted), and a **12-month recession probability** (NY-Fed
  Estrella-Mishkin term-spread probit, via stdlib `math.erf`). Stays measures-not-verdicts. See ADR-007.
- **Macro layer extended** with three more FRED series and their reads: 10y **breakeven inflation**
  (`T10YIE` → market-expected CPI), the 10y **TIPS real yield** (`DFII10` → ex-ante real rate), and
  the US **high-yield credit spread** (`BAMLH0A0HYM2`) with a **z-score** vs its ~1y window — a
  financial-stress gauge that often widens before equities fall. See ADR-007.
- **Derived fundamentals layer** on `fundamentals`, `analyst_view` and `earnings`, from figures
  already fetched (the balance sheet was loaded but total assets / equity went unread; the option
  chain analogue here is the raw statement). `fundamentals` now also surfaces total assets,
  stockholders' equity and (on a live read) market cap, plus: **net_debt**, **net_debt_to_fcf**,
  **fcf_margin**, **gross_profitability** (Novy-Marx) and **pretax ROIC** (statement-only), and —
  paired with current market cap on a live read — **FCF & earnings yield**, **enterprise value**
  and **EV/EBIT, EV/Sales, EBIT/EV** (Greenblatt). `analyst_view` adds **upside_pct** and
  **target_dispersion** (consensus tightness). `earnings` adds **beat_rate**, **surprise_streak**,
  **avg_surprise** and **surprise_consistency**. Cap-based ratios are null on a point-in-time
  (`as_of`) read — the source has no historical market cap to pair with a past statement — and any
  ratio is null when its denominator isn't positive. See ADR-008.
- **Fundamentals extended** with liquidity/leverage/coverage from more of the same statements:
  current assets/liabilities, retained earnings, D&A and interest expense are now read, giving
  **current_ratio**, **working_capital**, **debt_to_equity**, **EBITDA** (EBIT + D&A) → **EV/EBITDA**
  and **net_debt_to_ebitda**, **interest_coverage**, and the **Altman Z″** distress score (book-equity
  variant — no market cap needed, so it works on a point-in-time read). Null where a line is missing
  or a divisor isn't positive. See ADR-008.
- **Derived crypto microstructure/derivatives layer** on `crypto_derivatives` and
  `crypto_order_book`, from data already fetched and discarded. `crypto_derivatives` now adds a
  per-venue **annualized funding rate** and cross-venue aggregates: **OI-weighted funding
  consensus** (and annualized), **funding dispersion** across venues (a stress/arbitrage signal)
  and **total open interest in USD** (using the per-venue USD OI the adapter already computes, so
  no unit-mismatch). `crypto_order_book` adds **imbalance** ((bid−ask depth)/(bid+ask)) and
  **microprice** (size-weighted fair price). Annualized funding assumes the venues' 8h interval
  (a single next-funding snapshot can't reveal the true interval) and flags it in `note`. See
  ADR-009.
- **DVOL regime read** on `crypto_implied_vol`: the current DVOL's **z-score and percentile** vs a
  trailing ~6-month window (extended from ~1 month for a meaningful regime) — the crypto analogue of
  the VIX z-score, self-contained from DVOL's own history. See ADR-009.
- **Crypto cycle/tokenomics measures:** the **Mayer Multiple** (price / SMA-200) on `technicals`
  and `crypto_technicals` — both inputs were already computed — and tokenomics ratios on
  `crypto_asset_profile`: **float_ratio** (circulating/total), **issuance_progress**
  (circulating/max) and **future_dilution** ((max−circulating)/circulating). Max-based ratios are
  null for uncapped assets (no max supply). The circulating-based ratios populate only when the
  source reports circulating supply (the free Coinpaprika tier currently returns it null for some
  assets — they degrade to null rather than erroring). See ADR-010.
- **Stablecoin Supply Ratio & dominance** on `crypto_macro`: joined with the DefiLlama stablecoin
  total — SSR (total mcap / Σ stablecoins; low = lots of sidelined "dry powder") and stablecoin
  dominance (cash share of the market). The join is supplementary: a stablecoin-source failure leaves
  the ratios null and never sinks the macro read. See ADR-010.
- **Derived options layer** on `options_volatility`, from the chain already fetched (only ATM IV
  was used before): **iv_skew** (OTM put vs call IV over ATM — >0 = puts richer/fear), **put/call
  ratios** (volume & open interest), and the **volatility risk premium** — ATM IV minus trailing
  realized vol — with **iv_rv_ratio** (>1 = options look rich). The VRP is the stateless stand-in
  for IV rank; true IV-rank/percentile need stored IV history a stateless server can't keep, so
  they're omitted. Illiquid wings (IV ≤ 0) are skipped so skew isn't built on garbage. See ADR-011.
- **IV term structure** on `options_volatility`: one extra best-effort chain fetch at a longer expiry
  gives **iv_term_slope** and **iv_term_structure** — "contango" (normal) vs "backwardation" (near
  IV > far IV, i.e. near-term stress/event priced in). The extra fetch is supplementary: a failure
  (e.g. a 429) leaves the slope null and never sinks the near-expiry read. See ADR-011.
- Resilience: retry/backoff on yfinance, and a transparent **stooq fallback** for price history so
  a yfinance failure doesn't lose data.
- **Freshness stamp on the live equity snapshot** (`company_snapshot`): `quote_time`
  (`regularMarketTime`) and `market_state`, so a stale weekend/holiday last-session close is no
  longer indistinguishable from a live tick (crypto already carried `CryptoQuote.timestamp`).
- 191 offline tests; live-validated against every source.

### Fixed
- **Data-honesty hardening — a failed source must never look like valid data.** A consistent,
  machine-readable `unavailable: <reason>` (`rate_limited` / `timeout` / `error`) is now emitted on
  the unavailable path so a caller can branch (retry vs abstain) on a code, not a guess:
  - `crypto_macro` no longer returns an **all-null snapshot on a 429** (which read as real zeros): a
    rate-limit/timeout that drops both CoinGecko legs now raises an honest error envelope (matching
    its own docstring), and a single failed headline leg is flagged via a new `status` field.
    See ADR-012.
  - yfinance retry now **only retries transient failures** (429/5xx/timeout via `classify_transient`)
    and re-raises a genuine error (404/parse bug) immediately — stopping a retry storm on permanent
    errors — raising `SourceUnavailable` with a reason once exhausted.
  - The **CCXT crypto layer holds one shared, rate-limited exchange instance** per process (markets
    loaded once) instead of building a new one per call, so `enableRateLimit` throttles across a
    parallel manager-mode fan-out instead of drawing mass-429s; fetches are wrapped in transient
    retry emitting `SourceUnavailable`.
  - `classify` / `compare` now distinguish a transient failure (`unavailable: rate_limited`) from a
    genuinely unknown symbol (`not_found`), instead of collapsing both into `"unavailable"`.
  - `crypto_derivatives`, `treasury_data` and `world_macro` carry a `partial` flag when a sub-leg is
    dropped, so a complete-but-thin result isn't mistaken for the full picture.
- `dividends`: `had_cut` no longer flags a spurious cut from the **incomplete current calendar
  year** (a partial year has fewer ex-dates, so its lower calendar-year total is a timing
  artifact, not a real cut). Surfaced while validating a real MU equity-research report.

### Changed
- `quality_metrics`: docstring now states ROE/ROA/margins are the provider's **trailing-twelve-month**
  figures and will not reconcile with a single fiscal-year statement (only CAGR is statement-derived).

### Notes
- **Keyless by default.** No source requires a login or API key; SEC EDGAR only needs an
  identifiable `SCOUT_SEC_USER_AGENT` (its policy), not an account.
- Alpha Vantage (25 req/day) and Finnhub (free but needs an API key) were evaluated and **deferred**
  to keep the keyless, login-free experience. They remain pluggable behind the existing ports.
