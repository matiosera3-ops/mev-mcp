"""check_confirmed_swaps_on_pool — queries already-mined blocks for Uniswap v3
Swap events on a given pool address.

This is a diagnostic complement to check_pending_swaps_on_pool: where that
tool watches the mempool for unconfirmed transactions, this one queries
confirmed on-chain events to measure the *actual* swap volume on a pool.
Use it to determine whether a low mempool detection rate reflects genuine
swap scarcity or a mempool-watch coverage gap.

Polygon only. Uses standard eth_getLogs — no mempool access required.
"""

import logging
import os
from collections import defaultdict

from web3 import Web3

from .config import get_rpc_url

logger = logging.getLogger(__name__)

# Polygon block time is approximately 2 seconds per block (post-Napoli upgrade,
# verified against average block times on Polygonscan ~2025-2026).
# 600 blocks * ~2 s ≈ 20 min lookback by default.
_POLYGON_APPROX_BLOCK_TIME_S = 2
DEFAULT_LOOKBACK_BLOCKS = 600  # ≈ 20 min at ~2 s/block on Polygon

# Uniswap V3 Pool Swap event canonical ABI signature:
#   event Swap(address indexed sender, address indexed recipient,
#              int256 amount0, int256 amount1, uint160 sqrtPriceX96,
#              uint128 liquidity, int24 tick)
#
# Two common mistakes in the wild:
#   - liquidity typed as int128 → wrong (it's uint128, liquidity is non-negative)
#   - tick typed as uint24     → wrong (it's int24, tick can be negative)
#
# The canonical signature below produces topic0 0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67,
# which is the well-known Uniswap V3 Swap topic0 verifiable on any block explorer.
# We derive it at import time so it's always grounded in the signature string.
_SWAP_EVENT_SIGNATURE = "Swap(address,address,int256,int256,uint160,uint128,int24)"
UNISWAP_V3_SWAP_TOPIC0 = "0x" + Web3.keccak(text=_SWAP_EVENT_SIGNATURE).hex()

# Alchemy free tier hard-limits eth_getLogs to 10 blocks per request (confirmed
# 2026-06 — error code -32600, message: "Under the Free tier plan, you can make
# eth_getLogs requests with up to a 10 block range").
# Paid tiers (PAYG / Growth) support up to 2000 blocks or more.
# Override with MEV_MCP_LOGS_CHUNK_SIZE env var on a paid plan (e.g. "2000").
_MAX_BLOCKS_PER_CALL = int(os.environ.get("MEV_MCP_LOGS_CHUNK_SIZE", "10"))


def check_confirmed_swaps_on_pool(
    pool_address: str,
    chain: str = "polygon",
    lookback_blocks: int = DEFAULT_LOOKBACK_BLOCKS,
) -> dict:
    """
    Queries already-mined (confirmed) blocks for Uniswap v3 Swap events on
    the given pool, and returns how many swaps occurred and which contract
    addresses sent those swap transactions.

    This is NOT a mempool tool — it reads finalized on-chain state via
    eth_getLogs. Use it as a ground-truth complement to
    check_pending_swaps_on_pool: if this reports significant swap volume but
    check_pending_swaps_on_pool sees few or none, the gap is in mempool
    coverage (e.g. private/Flashbots RPC, transactions bypassing the public
    mempool), not genuine swap scarcity on the pool.

    Args:
        pool_address: the Uniswap v3 pool contract address to query
        chain: only "polygon" is currently supported
        lookback_blocks: number of recent confirmed blocks to scan
                         (default 600 ≈ 20 min at ~2 s/block on Polygon)

    Returns:
        {
            "chain": str,
            "pool_address": str,
            "lookback_blocks": int,
            "from_block": int,
            "to_block": int,
            "swap_count": int,
            "by_to_address": {address: count, ...}
        }
        where by_to_address shows which contract addresses sent the swap
        transactions (tx.to — may be a router, aggregator, or the pool itself
        for direct calls).
    """
    if chain != "polygon":
        return {
            "error": (
                f"'{chain}' is not supported by check_confirmed_swaps_on_pool. "
                f"Only 'polygon' is currently supported."
            )
        }

    rpc_url = get_rpc_url(chain)
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        return {"error": f"Could not connect to RPC for chain '{chain}'."}

    checksum_address = Web3.to_checksum_address(pool_address)
    to_block = w3.eth.block_number
    from_block = max(0, to_block - lookback_blocks)

    total_blocks = to_block - from_block + 1
    if total_blocks > _MAX_BLOCKS_PER_CALL:
        logger.warning(
            "Block range %d–%d spans %d blocks, which exceeds the %d-block "
            "per-call RPC limit. Splitting into multiple eth_getLogs requests.",
            from_block, to_block, total_blocks, _MAX_BLOCKS_PER_CALL,
        )

    all_logs = []
    current = from_block
    while current <= to_block:
        chunk_end = min(current + _MAX_BLOCKS_PER_CALL - 1, to_block)
        try:
            chunk_logs = w3.eth.get_logs({
                "address": checksum_address,
                "fromBlock": current,
                "toBlock": chunk_end,
                "topics": [UNISWAP_V3_SWAP_TOPIC0],
            })
        except Exception as exc:
            # web3.py surfaces the Alchemy JSON-RPC error (code + message) in
            # the exception string. Log it verbatim so it's visible server-side,
            # then return it in the result dict so Claude Desktop can display it.
            logger.error(
                "eth_getLogs failed for blocks %d–%d: %s",
                current, chunk_end, exc,
            )
            return {
                "error": str(exc),
                "hint": (
                    f"If the error says 'Free tier' and mentions a block range limit, "
                    f"set MEV_MCP_LOGS_CHUNK_SIZE=10 in your environment (it already "
                    f"is the default) or upgrade your Alchemy plan. "
                    f"Current chunk size: {_MAX_BLOCKS_PER_CALL} block(s)."
                ),
                "chain": chain,
                "pool_address": pool_address,
                "from_block": from_block,
                "to_block": to_block,
                "chunk_that_failed": {"from": current, "to": chunk_end},
            }
        all_logs.extend(chunk_logs)
        current = chunk_end + 1

    by_to_address: dict[str, int] = defaultdict(int)
    for log in all_logs:
        tx_hash = log["transactionHash"]
        try:
            tx = w3.eth.get_transaction(tx_hash)
            to_addr = (tx.get("to") or "").lower()
        except Exception:
            to_addr = "unknown"
        by_to_address[to_addr] += 1

    return {
        "chain": chain,
        "pool_address": pool_address,
        "lookback_blocks": lookback_blocks,
        "from_block": from_block,
        "to_block": to_block,
        "swap_count": len(all_logs),
        "by_to_address": dict(by_to_address),
    }
