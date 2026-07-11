"""DefiLlama implementation of ``DefiSource`` — keyless DeFi TVL, stablecoins and yields.

DefiLlama's open hosts (api.llama.fi / stablecoins.llama.fi / yields.llama.fi) need no key and have
generous limits. Some responses are multi-MB (all protocols / all pools), so results are filtered
and capped here. Raw ecosystem facts (TVL, peg, APY), never a verdict.

The JSON fetch is injected so the unit tests run fully offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...domain.models import (
    DefiFees,
    DefiOverview,
    DefiTvlItem,
    DefiYields,
    ProtocolFees,
    StablecoinItem,
    StablecoinSupply,
    YieldPool,
)
from ..retry import SourceUnavailable, unavailable_status, with_retry

_CHAINS_URL = "https://api.llama.fi/v2/chains"
_PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"
_STABLES_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
_YIELDS_URL = "https://yields.llama.fi/pools"
# Fees/revenue overview: keep the response small (no per-day chart breakdowns, multi-MB otherwise).
_FEES_OVERVIEW_URL = (
    "https://api.llama.fi/overview/fees"
    "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
)
_REVENUE_OVERVIEW_URL = _FEES_OVERVIEW_URL + "&dataType=dailyRevenue"
_PROTOCOL_FEES_URL = "https://api.llama.fi/summary/fees/{slug}?dataType=dailyFees"
_PROTOCOL_REVENUE_URL = "https://api.llama.fi/summary/fees/{slug}?dataType=dailyRevenue"
_TOP_PROTOCOLS = 15

# ``currentChainTvls`` mixes per-chain TVL ("Ethereum", "Polygon"…) with NON-chain buckets:
# the cross-chain aggregates "borrowed"/"staking"/"pool2" AND their per-chain twins
# ("Ethereum-staking" duplicates the same dollars as "staking"). Summing any of them
# double-counts (borrowed alone inflated aave ~2×) — protocol TVL is the CHAIN keys only.
_NON_CHAIN_TVL_KEYS = {"borrowed", "staking", "pool2"}
_NON_CHAIN_TVL_SUFFIXES = ("-borrowed", "-staking", "-pool2")


def _is_chain_tvl(key: Any) -> bool:
    name = str(key)
    return name not in _NON_CHAIN_TVL_KEYS and not name.endswith(_NON_CHAIN_TVL_SUFFIXES)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class DefiLlamaDefi:
    def __init__(
        self,
        fetch_json: Callable[[str], Awaitable[Any]] | None = None,
        timeout: float = 20.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.5,
    ) -> None:
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._fetch_json = fetch_json or self._default_fetch_json

    async def _default_fetch_json(self, url: str) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def get_tvl(self, slug: str | None = None) -> DefiOverview:
        if slug:
            data = await self._fetch_json(_PROTOCOL_URL.format(slug=slug.strip().lower()))
            chain_tvls = (data or {}).get("currentChainTvls") or {}
            items = [
                DefiTvlItem(name=chain, tvl_usd=_dec(tvl))
                for chain, tvl in chain_tvls.items()
                if _is_chain_tvl(chain)
            ]
            items.sort(key=lambda i: i.tvl_usd or Decimal(0), reverse=True)
            total = sum((i.tvl_usd or Decimal(0)) for i in items) or None
            return DefiOverview(scope=slug, total_tvl_usd=total, items=items[:30])

        chains = await self._fetch_json(_CHAINS_URL)
        rows = [c for c in (chains or []) if isinstance(c, dict)]
        items = [DefiTvlItem(name=c.get("name"), tvl_usd=_dec(c.get("tvl"))) for c in rows]
        items.sort(key=lambda i: i.tvl_usd or Decimal(0), reverse=True)
        total = sum((i.tvl_usd or Decimal(0)) for i in items) or None
        return DefiOverview(scope="chains", total_tvl_usd=total, items=items[:30])

    async def get_stablecoins(self) -> StablecoinSupply:
        data = await self._fetch_json(_STABLES_URL)
        assets = [a for a in ((data or {}).get("peggedAssets") or []) if isinstance(a, dict)]
        items: list[StablecoinItem] = []
        total = Decimal(0)
        for asset in assets:
            circ = _dec((asset.get("circulating") or {}).get("peggedUSD"))
            price = _dec(asset.get("price"))
            if circ is not None:
                total += circ
            items.append(
                StablecoinItem(
                    symbol=asset.get("symbol"),
                    name=asset.get("name"),
                    peg_type=asset.get("pegType"),
                    peg_mechanism=asset.get("pegMechanism"),
                    price=price,
                    peg_deviation=(price - Decimal(1)) if price is not None else None,
                    circulating_usd=circ,
                )
            )
        items.sort(key=lambda i: i.circulating_usd or Decimal(0), reverse=True)
        return StablecoinSupply(total_circulating_usd=total or None, items=items[:30])

    async def get_yields(
        self, chain: str | None = None, project: str | None = None, min_tvl: float = 1_000_000
    ) -> DefiYields:
        data = await self._fetch_json(_YIELDS_URL)
        pools = [p for p in ((data or {}).get("data") or []) if isinstance(p, dict)]
        chain_f = chain.strip().lower() if chain else None
        project_f = project.strip().lower() if project else None
        floor = _dec(min_tvl) or Decimal(0)
        out: list[YieldPool] = []
        for pool in pools:
            tvl = _dec(pool.get("tvlUsd"))
            if tvl is None or tvl < floor:
                continue
            if chain_f and str(pool.get("chain", "")).lower() != chain_f:
                continue
            if project_f and str(pool.get("project", "")).lower() != project_f:
                continue
            out.append(
                YieldPool(
                    project=pool.get("project"),
                    chain=pool.get("chain"),
                    symbol=pool.get("symbol"),
                    tvl_usd=tvl,
                    apy=_dec(pool.get("apy")),
                    apy_base=_dec(pool.get("apyBase")),
                    apy_reward=_dec(pool.get("apyReward")),
                )
            )
        out.sort(key=lambda p: p.apy or Decimal(0), reverse=True)
        return DefiYields(pools=out[:25])

    async def _fetch_retry(self, url: str) -> Any:
        return await with_retry(
            lambda: self._fetch_json(url),
            attempts=self._retry_attempts,
            base_delay=self._retry_base_delay,
        )

    async def get_fees(self, protocol: str | None = None) -> DefiFees:
        if protocol and protocol.strip():
            return await self._protocol_fees(protocol.strip())
        return await self._fees_overview()

    async def _fees_overview(self) -> DefiFees:
        fees, revenue = await asyncio.gather(
            self._fetch_retry(_FEES_OVERVIEW_URL),
            self._fetch_retry(_REVENUE_OVERVIEW_URL),
            return_exceptions=True,
        )
        # The fees overview is the headline. If it can't be fetched, the whole read is unavailable —
        # surface that (ADR-012), never an all-null DefiFees that reads as "no fees exist".
        if isinstance(fees, Exception):
            return DefiFees(source_status=unavailable_status(fees))

        revenue_by_name: dict[str, Decimal | None] = {}
        note: str | None = None
        if isinstance(revenue, dict):
            for proto in revenue.get("protocols") or []:
                if isinstance(proto, dict) and proto.get("name"):
                    revenue_by_name[str(proto["name"])] = _dec(proto.get("total24h"))
            total_revenue = _dec(revenue.get("total24h"))
        else:
            # Fees came back but the revenue leg failed: keep fees, flag the gap, leave revenue null
            # (a null revenue must read as "unavailable", not "zero revenue").
            total_revenue = None
            note = f"Revenue leg {unavailable_status(revenue)}; revenue figures omitted."

        protocols = [p for p in (fees.get("protocols") or []) if isinstance(p, dict)]
        protocols.sort(key=lambda p: _dec(p.get("total24h")) or Decimal(0), reverse=True)
        top = [
            ProtocolFees(
                name=p.get("name"),
                category=p.get("category"),
                chains=[str(c) for c in (p.get("chains") or [])],
                fees_24h=_dec(p.get("total24h")),
                fees_7d=_dec(p.get("total7d")),
                revenue_24h=revenue_by_name.get(str(p.get("name"))),
            )
            for p in protocols[:_TOP_PROTOCOLS]
        ]
        return DefiFees(
            total_fees_24h=_dec(fees.get("total24h")),
            total_fees_7d=_dec(fees.get("total7d")),
            total_revenue_24h=total_revenue,
            top_protocols=top,
            as_of=datetime.now(UTC),
            note=note,
        )

    async def _protocol_fees(self, slug: str) -> DefiFees:
        key = slug.lower()
        try:
            fees = await self._fetch_retry(_PROTOCOL_FEES_URL.format(slug=key))
        except SourceUnavailable as exc:
            return DefiFees(protocol=slug, source_status=exc.status)
        except Exception:  # noqa: BLE001 — a non-transient failure (404) = unknown protocol
            return DefiFees(
                protocol=slug,
                note=(
                    f"No DefiLlama fees data for protocol '{slug}'. "
                    "Check the slug (e.g. 'uniswap')."
                ),
            )
        if not isinstance(fees, dict):
            return DefiFees(
                protocol=slug, note=f"No DefiLlama fees data for protocol '{slug}'."
            )
        # Revenue is supplementary; many protocols report none. A failure here must not sink the
        # fees read, so revenue stays null on any error.
        revenue_24h: Decimal | None = None
        try:
            revenue = await self._fetch_retry(_PROTOCOL_REVENUE_URL.format(slug=key))
            if isinstance(revenue, dict):
                revenue_24h = _dec(revenue.get("total24h"))
        except Exception:  # noqa: BLE001 — revenue is best-effort
            revenue_24h = None
        return DefiFees(
            protocol=slug,
            total_fees_24h=_dec(fees.get("total24h")),
            total_fees_7d=_dec(fees.get("total7d")),
            total_revenue_24h=revenue_24h,
            as_of=datetime.now(UTC),
        )
