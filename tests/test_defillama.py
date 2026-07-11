from decimal import Decimal

from scout.adapters.defillama import DefiLlamaDefi

_CHAINS = [
    {"name": "Ethereum", "tvl": 60000000000},
    {"name": "Solana", "tvl": 8000000000},
    {"name": "Tron", "tvl": 5000000000},
]

# Faithful to the real /protocol/{slug} payload: ``currentChainTvls`` mixes the chain keys with
# cross-chain aggregate buckets ("borrowed"/"staking"/"pool2") AND their per-chain twins
# ("Ethereum-staking" repeats the same dollars as "staking"). Only the chain keys are TVL.
_PROTOCOL = {
    "name": "Aave",
    "category": "Lending",
    "currentChainTvls": {
        "Ethereum": 10_000_000_000,
        "Polygon": 500_000_000,
        "borrowed": 10_320_000_000,
        "Ethereum-borrowed": 9_900_000_000,
        "Polygon-borrowed": 420_000_000,
        "staking": 224_000_000,
        "Ethereum-staking": 224_000_000,
        "pool2": 1_000_000,
        "Ethereum-pool2": 1_000_000,
    },
}

_STABLES = {
    "peggedAssets": [
        {
            "symbol": "USDT",
            "name": "Tether",
            "pegType": "peggedUSD",
            "pegMechanism": "fiat-backed",
            "price": 1.001,
            "circulating": {"peggedUSD": 120000000000},
        },
        {
            "symbol": "DAI",
            "name": "Dai",
            "pegType": "peggedUSD",
            "pegMechanism": "crypto-backed",
            "price": 0.998,
            "circulating": {"peggedUSD": 5000000000},
        },
    ]
}

_YIELDS = {
    "data": [
        {"project": "aave-v3", "chain": "Ethereum", "symbol": "USDC", "tvlUsd": 2e9, "apy": 4.5,
         "apyBase": 4.0, "apyReward": 0.5},
        {"project": "curve", "chain": "Ethereum", "symbol": "3POOL", "tvlUsd": 5e8, "apy": 9.0,
         "apyBase": 9.0, "apyReward": None},
        {"project": "dust", "chain": "Solana", "symbol": "X", "tvlUsd": 1000, "apy": 999.0},
    ]
}


async def test_tvl_chains():
    async def _fetch(url: str):
        return _CHAINS

    result = await DefiLlamaDefi(fetch_json=_fetch).get_tvl()
    assert result.scope == "chains"
    assert result.items[0].name == "Ethereum"
    assert result.total_tvl_usd == Decimal("73000000000")


async def test_tvl_protocol_excludes_borrowed():
    async def _fetch(url: str):
        assert "protocol/aave" in url
        return _PROTOCOL

    result = await DefiLlamaDefi(fetch_json=_fetch).get_tvl("aave")
    names = [i.name for i in result.items]
    # Chains only — no aggregates, no suffixed buckets (summing them ~doubled the total).
    assert names == ["Ethereum", "Polygon"]
    assert result.items[0].name == "Ethereum"
    assert result.total_tvl_usd == Decimal("10500000000")


async def test_stablecoins_peg_deviation():
    async def _fetch(url: str):
        return _STABLES

    result = await DefiLlamaDefi(fetch_json=_fetch).get_stablecoins()
    assert result.total_circulating_usd == Decimal("125000000000")
    usdt = result.items[0]
    assert usdt.symbol == "USDT"
    assert usdt.peg_deviation == Decimal("0.001")


async def test_yields_filters_and_sorts():
    async def _fetch(url: str):
        return _YIELDS

    result = await DefiLlamaDefi(fetch_json=_fetch).get_yields(min_tvl=1_000_000)
    # dust pool (1000 < 1M) excluded; sorted by APY desc → curve(9) before aave(4.5)
    symbols = [p.symbol for p in result.pools]
    assert symbols == ["3POOL", "USDC"]


async def test_yields_chain_filter():
    async def _fetch(url: str):
        return _YIELDS

    result = await DefiLlamaDefi(fetch_json=_fetch).get_yields(chain="solana", min_tvl=100)
    assert [p.project for p in result.pools] == ["dust"]


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        self.response = _Resp(status_code)
        super().__init__(f"HTTP {status_code}")


_FEES_OVERVIEW = {
    "total24h": 1000,
    "total7d": 7000,
    "protocols": [
        {"name": "Uniswap", "category": "Dexes", "chains": ["Ethereum"],
         "total24h": 500, "total7d": 3500},
        {"name": "Aave", "category": "Lending", "chains": ["Ethereum"],
         "total24h": 800, "total7d": 5600},
    ],
}

_REVENUE_OVERVIEW = {
    "total24h": 300,
    "protocols": [
        {"name": "Uniswap", "total24h": 50},
        {"name": "Aave", "total24h": 200},
    ],
}


async def test_fees_overview_sorted_with_revenue_joined():
    async def _fetch(url: str):
        if "dataType=dailyRevenue" in url:
            return _REVENUE_OVERVIEW
        return _FEES_OVERVIEW

    result = await DefiLlamaDefi(fetch_json=_fetch).get_fees()
    assert result.total_fees_24h == Decimal("1000")
    assert result.total_fees_7d == Decimal("7000")
    assert result.total_revenue_24h == Decimal("300")
    # Sorted by 24h fees desc → Aave (800) before Uniswap (500), revenue joined by name.
    assert [p.name for p in result.top_protocols] == ["Aave", "Uniswap"]
    assert result.top_protocols[0].revenue_24h == Decimal("200")
    assert result.top_protocols[1].revenue_24h == Decimal("50")
    assert result.top_protocols[0].chains == ["Ethereum"]
    assert result.source_status is None


async def test_fees_overview_revenue_leg_failure_keeps_fees():
    async def _fetch(url: str):
        if "dataType=dailyRevenue" in url:
            raise _HttpError(429)
        return _FEES_OVERVIEW

    result = await DefiLlamaDefi(fetch_json=_fetch, retry_base_delay=0).get_fees()
    assert result.total_fees_24h == Decimal("1000")
    assert result.total_revenue_24h is None
    assert result.top_protocols[0].revenue_24h is None
    assert result.note is not None and "rate_limited" in result.note


async def test_fees_per_protocol():
    async def _fetch(url: str):
        assert "summary/fees/uniswap" in url
        if "dataType=dailyRevenue" in url:
            return {"name": "Uniswap", "total24h": 50}
        return {"name": "Uniswap", "total24h": 1670000, "total7d": 8700000}

    result = await DefiLlamaDefi(fetch_json=_fetch).get_fees("uniswap")
    assert result.protocol == "uniswap"
    assert result.total_fees_24h == Decimal("1670000")
    assert result.total_fees_7d == Decimal("8700000")
    assert result.total_revenue_24h == Decimal("50")
    assert result.top_protocols == []


async def test_fees_unknown_protocol_empty_with_note():
    async def _fetch(url: str):
        raise _HttpError(404)  # DefiLlama 404 for an unknown slug

    result = await DefiLlamaDefi(fetch_json=_fetch, retry_base_delay=0).get_fees("nope")
    assert result.protocol == "nope"
    assert result.total_fees_24h is None
    assert result.note is not None and "nope" in result.note
    assert result.source_status is None


async def test_fees_overview_rate_limited_unavailable():
    async def _fetch(url: str):
        raise _HttpError(429)

    result = await DefiLlamaDefi(fetch_json=_fetch, retry_base_delay=0).get_fees()
    assert result.source_status == "unavailable: rate_limited"
    assert result.total_fees_24h is None
    assert result.top_protocols == []
