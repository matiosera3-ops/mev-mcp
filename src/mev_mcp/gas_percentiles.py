"""get_gas_price_percentiles — EIP-1559 fee history, works on any chain that
supports eth_feeHistory (which both Polygon and Arbitrum do). No mempool
access needed — this reads already-mined block data.
"""

from web3 import Web3

from .config import get_rpc_url


def get_gas_price_percentiles(
    chain: str,
    block_count: int = 20,
    percentiles: list[float] | None = None,
) -> dict:
    """
    Returns gas price distribution over the last `block_count` blocks.

    Args:
        chain: "polygon" or "arbitrum"
        block_count: number of recent blocks to sample (max 1024, default 20)
        percentiles: priority fee percentiles to compute (default [10, 50, 90])

    Returns:
        dict with oldest_block, base_fee_gwei (latest), gas_used_ratio (avg),
        and priority_fee_percentiles_gwei (per requested percentile, averaged
        across the sampled blocks).
    """
    if percentiles is None:
        percentiles = [10, 50, 90]

    rpc_url = get_rpc_url(chain)
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        return {"error": f"Could not connect to RPC for chain '{chain}'."}

    block_count = min(max(block_count, 1), 1024)

    history = w3.eth.fee_history(block_count, "latest", percentiles)

    # history.reward is a list of lists: one inner list per block, one value
    # per requested percentile. Average each percentile column across blocks.
    reward_columns = list(zip(*history["reward"])) if history["reward"] else []
    avg_rewards_wei = [
        sum(col) / len(col) if col else 0 for col in reward_columns
    ]

    base_fee_latest_wei = history["baseFeePerGas"][-1] if history["baseFeePerGas"] else 0
    avg_gas_used_ratio = (
        sum(history["gasUsedRatio"]) / len(history["gasUsedRatio"])
        if history["gasUsedRatio"]
        else 0.0
    )

    return {
        "chain": chain,
        "blocks_sampled": block_count,
        "oldest_block": history["oldestBlock"],
        "base_fee_gwei": base_fee_latest_wei / 1e9,
        "avg_gas_used_ratio": round(avg_gas_used_ratio, 4),
        "priority_fee_percentiles_gwei": {
            str(p): round(avg_rewards_wei[i] / 1e9, 4)
            for i, p in enumerate(percentiles)
        },
    }
