"""mev-mcp server entrypoint."""

from mcp.server.fastmcp import FastMCP

from .confirmed_swaps import check_confirmed_swaps_on_pool as _check_confirmed_swaps_on_pool
from .gas_percentiles import get_gas_price_percentiles as _get_gas_price_percentiles
from .pending_swaps import check_pending_swaps_on_pool as _check_pending_swaps_on_pool

mcp = FastMCP("mev-mcp")


@mcp.tool()
def hello() -> str:
    """Simple connectivity check — confirms the MCP server is reachable."""
    return "mev-mcp is running."


@mcp.tool()
def get_gas_price_percentiles(
    chain: str,
    block_count: int = 20,
) -> dict:
    """
    Returns the gas price distribution (base fee + priority fee percentiles)
    over the last N blocks. Works on any supported chain — does not require
    mempool access, just standard eth_feeHistory.

    Args:
        chain: "polygon" or "arbitrum"
        block_count: number of recent blocks to sample (default 20, max 1024)
    """
    return _get_gas_price_percentiles(chain, block_count)


@mcp.tool()
async def check_pending_swaps_on_pool(
    pool_address: str,
    token0: str,
    token1: str,
    chain: str = "polygon",
    duration_seconds: int = 15,
) -> dict:
    """
    Watches the public mempool for pending transactions touching a given
    Uniswap v3 pool — either direct calls to the pool, or single-hop swaps
    routed through a known Uniswap v3 router (SwapRouter or SwapRouter02)
    whose calldata references both of the pool's tokens.

    Polygon only — Arbitrum uses a centralized sequencer with no public
    mempool, so this isn't available there. See the README for details.

    Note: only catches single-hop router swaps (exactInputSingle /
    exactOutputSingle). Multi-hop swaps (exactInput / exactOutput) encode
    the route as a packed path that isn't decoded yet — see roadmap.

    Args:
        pool_address: the pool contract address (catches direct calls)
        token0: address of one of the pool's two tokens
        token1: address of the pool's other token
        chain: only "polygon" is currently supported
        duration_seconds: how long to watch, capped at 60 seconds (default 15)
    """
    return await _check_pending_swaps_on_pool(
        pool_address, token0, token1, chain, duration_seconds
    )


@mcp.tool()
def check_confirmed_swaps_on_pool(
    pool_address: str,
    chain: str = "polygon",
    lookback_blocks: int = 600,
) -> dict:
    """
    Queries already-mined (confirmed) blocks for Uniswap v3 Swap events on
    the given pool. Returns the total swap count and a breakdown by the `to`
    address of each swap transaction (router, aggregator, or direct caller).

    This is NOT a mempool tool — it reads finalized on-chain state via
    eth_getLogs. Use it to establish ground-truth swap volume and compare
    against check_pending_swaps_on_pool: a large gap between confirmed volume
    and pending detections points to a mempool coverage issue, not swap
    scarcity. Polygon only.

    Args:
        pool_address: the Uniswap v3 pool contract address to query
        chain: only "polygon" is currently supported
        lookback_blocks: number of recent confirmed blocks to scan
                         (default 600 ≈ 20 min at ~2 s/block on Polygon)
    """
    return _check_confirmed_swaps_on_pool(pool_address, chain, lookback_blocks)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
