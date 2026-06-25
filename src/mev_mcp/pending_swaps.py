"""check_pending_swaps_on_pool — watches the public mempool for pending
transactions involving a given Uniswap v3 pool, then self-verifies by running
a confirmed-swap check over a comparable block window.

Polygon only. Arbitrum has no public mempool (centralized sequencer) — see
config.py and the README for why this isn't a missing feature, it's a
structural property of how Arbitrum works.

This is intentionally a single bounded call, not a persistent subscription:
an AI agent needs a response that terminates, not an open-ended stream. The
free/public version of this tool caps duration at 60 seconds. Persistent,
unbounded watching is a defi-mcp-cloud feature (see README).

DETECTION APPROACH: four route types are checked (tagged in match_type):

  1. "direct_pool_call" — tx.to == pool_address directly (rare, used by some
     contracts/aggregators that skip the router entirely).

  2. "router_swap" — SwapRouter (v1) or SwapRouter02, checked by tx.to:
     - Single-hop (exactInputSingle / exactOutputSingle): tokenIn/tokenOut at
       fixed calldata offsets, decoded directly.
     - Multi-hop (exactInput / exactOutput): path encoded as packed bytes
       (tokenA + 3-byte fee + tokenB + ...), ABI-decoded via eth_abi.

  3. "universal_router_swap" — UniversalRouter execute(): each command byte
     is inspected; V3_SWAP_EXACT_IN (0x00) and V3_SWAP_EXACT_OUT (0x01)
     carry an ABI-encoded input with a packed bytes path, decoded to check
     for both token addresses.

  4. "aggregator_1inch" — 1inch AggregationRouter v6 swap(): the calldata
     struct carries srcToken and dstToken, decoded and matched against the
     pool's token pair.

SELF-VERIFICATION: after the observation window closes, the tool automatically
calls check_confirmed_swaps_on_pool internally over a block window sized to
match the observed duration. The result appears in confirmed_check (same
format as check_confirmed_swaps_on_pool) and coverage_estimate (a string
categorising how much of on-chain volume was caught pending).

Selectors verified against the deployed Polygon contracts (Polygonscan /
codeslaw.app) and the Uniswap v3 periphery source.
"""

import asyncio
import logging
import time

from eth_abi import decode as abi_decode
from web3 import AsyncWeb3, WebSocketProvider

from .config import get_rpc_url, to_ws_url, chain_has_mempool

logger = logging.getLogger(__name__)
from .confirmed_swaps import (
    check_confirmed_swaps_on_pool as _run_confirmed_check,
    _POLYGON_APPROX_BLOCK_TIME_S,
)

MAX_DURATION_SECONDS = 60
MAX_HASHES_COLLECTED = 500

# Minimum lookback for the auto-confirmed check, regardless of duration_seconds.
# A single 15 s pending watch produces only ~8 blocks — far too thin a window to
# draw conclusions about swap activity on most pools. This floor ensures the
# confirmed check always covers a statistically meaningful period (~10 min at
# Polygon's ~2 s/block). It is intentionally wider than any pending watch window.
MIN_CONFIRMED_LOOKBACK_BLOCKS = 300

# SwapRouter (v1) and SwapRouter02 on Polygon.
KNOWN_ROUTERS_POLYGON = {
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # SwapRouter (v1)
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # SwapRouter02
}

# UniversalRouter on Polygon.
UNIVERSAL_ROUTER_POLYGON = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"

# 1inch AggregationRouter v6 — same address on all EVM chains.
# Source: https://docs.1inch.io/docs/aggregation-protocol/introduction
# Verified against the deployed contract on Polygonscan.
ONEINCH_AGGREGATION_ROUTER_V6 = "0x111111125421ca6dc452d289314280a0f8842a65"

# swap(address,(address,address,address,address,uint256,uint256,uint256),bytes,bytes)
# Selector verified against the deployed source on Polygonscan.
ONEINCH_SWAP_SELECTOR = "0x07ed2379"

# Routers tracked by known_router_hits diagnostic (addresses from
# developers.uniswap.org/contracts/v3/reference/deployments/polygon-deployments,
# verified 2026-06). UniversalRouter here differs from UNIVERSAL_ROUTER_POLYGON
# above — the docs page now lists 0x1095692a... as the canonical address.
KNOWN_ROUTERS_FOR_HITS = {
    "SwapRouter": "0xe592427a0aece92de3edee1f18e0157c05861564",
    "SwapRouter02": "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
    "UniversalRouter": "0x1095692a6237d83c6a72f3f5efedb9a670c49223",
}

# Single-hop: tokenIn at calldata[4:36], tokenOut at [36:68] (right-padded
# 32-byte slots, address in last 20 bytes).
SINGLE_HOP_SELECTORS = {
    "0x414bf389",  # SwapRouter.exactInputSingle
    "0xdb3e2198",  # SwapRouter.exactOutputSingle
    "0x04e45aaf",  # SwapRouter02.exactInputSingle
    "0x5023b4df",  # SwapRouter02.exactOutputSingle
}

# Multi-hop: path is a packed `bytes` field ABI-decoded from a struct.
# SwapRouter (v1) structs include a `deadline` field; SwapRouter02 does not.
MULTI_HOP_SELECTORS_V1 = {
    "0xc04b8d59",  # SwapRouter.exactInput  (bytes,address,uint256,uint256,uint256)
    "0xf28c0498",  # SwapRouter.exactOutput (bytes,address,uint256,uint256,uint256)
}
MULTI_HOP_SELECTORS_V2 = {
    "0xb858183f",  # SwapRouter02.exactInput  (bytes,address,uint256,uint256)
    "0x09b81346",  # SwapRouter02.exactOutput (bytes,address,uint256,uint256)
}

# UniversalRouter execute() selectors.
UNIVERSAL_ROUTER_SELECTORS = {
    "0x3593564c",  # execute(bytes commands, bytes[] inputs, uint256 deadline)
    "0x24856bc3",  # execute(bytes commands, bytes[] inputs)
}

V3_SWAP_EXACT_IN = 0x00
V3_SWAP_EXACT_OUT = 0x01


def _both_tokens_in_path(path: bytes, token0: str, token1: str) -> bool:
    """Token addresses are packed as 20-byte sequences in the V3 path bytes."""
    path_hex = path.hex()
    t0 = token0.lower().lstrip("0x")
    t1 = token1.lower().lstrip("0x")
    return t0 in path_hex and t1 in path_hex


def _calldata_references_both_tokens(input_data: str, token0: str, token1: str) -> bool:
    """
    Single-hop: tokenIn at calldata bytes [4:36], tokenOut at [36:68], both
    right-padded 32-byte slots (address in last 20 bytes).
    """
    if not input_data or len(input_data) < 2 + 8 + 128:  # "0x" + selector + 2 slots
        return False

    data = input_data[2:]  # strip "0x"
    selector = "0x" + data[:8]
    if selector not in SINGLE_HOP_SELECTORS:
        return False

    token_in_slot = data[8:72]
    token_out_slot = data[72:136]
    token_in = "0x" + token_in_slot[-40:]
    token_out = "0x" + token_out_slot[-40:]

    found = {token_in.lower(), token_out.lower()}
    expected = {token0.lower(), token1.lower()}
    return found == expected


def _multihop_references_both_tokens(input_data: str, token0: str, token1: str) -> bool:
    """
    Multi-hop: ABI-decode the struct parameter to extract the packed
    `bytes path`, then check if both token addresses appear in it.
    """
    if not input_data or len(input_data) < 10:
        return False

    raw = bytes.fromhex(input_data[2:])
    selector = "0x" + raw[:4].hex()

    try:
        if selector in MULTI_HOP_SELECTORS_V1:
            # struct: (bytes path, address recipient, uint256 deadline, uint256 amountIn/Out, uint256 limit)
            path = abi_decode(["(bytes,address,uint256,uint256,uint256)"], raw[4:])[0][0]
        elif selector in MULTI_HOP_SELECTORS_V2:
            # struct: (bytes path, address recipient, uint256 amountIn/Out, uint256 limit)
            path = abi_decode(["(bytes,address,uint256,uint256)"], raw[4:])[0][0]
        else:
            return False
        return _both_tokens_in_path(path, token0, token1)
    except Exception:
        return False


def _universal_router_references_both_tokens(input_data: str, token0: str, token1: str) -> bool:
    """
    UniversalRouter execute(): decode commands + inputs arrays, then for each
    V3_SWAP_EXACT_IN/OUT command decode its input tuple and check the path.
    """
    if not input_data or len(input_data) < 10:
        return False

    raw = bytes.fromhex(input_data[2:])
    selector = "0x" + raw[:4].hex()
    if selector not in UNIVERSAL_ROUTER_SELECTORS:
        return False

    try:
        if selector == "0x3593564c":
            commands, inputs, _ = abi_decode(["bytes", "bytes[]", "uint256"], raw[4:])
        else:
            commands, inputs = abi_decode(["bytes", "bytes[]"], raw[4:])
    except Exception:
        return False

    for i, cmd in enumerate(commands):
        if cmd not in (V3_SWAP_EXACT_IN, V3_SWAP_EXACT_OUT):
            continue
        if i >= len(inputs):
            continue
        try:
            # abi.encode(address recipient, uint256 amountIn/Out, uint256 limit, bytes path, bool payerIsUser)
            _, _, _, path, _ = abi_decode(
                ["address", "uint256", "uint256", "bytes", "bool"], inputs[i]
            )
        except Exception:
            continue
        if _both_tokens_in_path(path, token0, token1):
            return True
    return False


def _oneinch_references_both_tokens(input_data: str, token0: str, token1: str) -> bool:
    """
    1inch AggregationRouter v6 swap(): ABI-decode the SwapDescription struct
    to extract srcToken and dstToken, then check if both match the pool's pair.

    Function signature:
      swap(address executor,
           (address srcToken, address dstToken, address srcReceiver,
            address dstReceiver, uint256 amount, uint256 minReturnAmount,
            uint256 flags) desc,
           bytes permit,
           bytes data)
    """
    if not input_data or len(input_data) < 10:
        return False

    raw = bytes.fromhex(input_data[2:])
    selector = "0x" + raw[:4].hex()
    if selector != ONEINCH_SWAP_SELECTOR:
        return False

    try:
        decoded = abi_decode(
            [
                "address",
                "(address,address,address,address,uint256,uint256,uint256)",
                "bytes",
                "bytes",
            ],
            raw[4:],
        )
        desc = decoded[1]
        src_token = desc[0].lower()
        dst_token = desc[1].lower()
        found = {src_token, dst_token}
        expected = {token0.lower(), token1.lower()}
        return found == expected
    except Exception:
        return False


async def _watch_pool(
    ws_url: str,
    pool_address: str,
    token0: str,
    token1: str,
    duration_seconds: int,
) -> dict:
    pool_address = pool_address.lower()

    collected_hashes = []

    async with AsyncWeb3(WebSocketProvider(ws_url)) as w3:
        subscription_id = await w3.eth.subscribe("newPendingTransactions")
        start = time.monotonic()

        try:
            async for response in w3.socket.process_subscriptions():
                tx_hash = response.get("result")
                if tx_hash:
                    collected_hashes.append(tx_hash)

                if (
                    time.monotonic() - start > duration_seconds
                    or len(collected_hashes) >= MAX_HASHES_COLLECTED
                ):
                    break
        finally:
            await w3.eth.unsubscribe(subscription_id)

        hashes_seen = len(collected_hashes)

        # Resolve hashes to full transactions *after* the subscription window
        # closes, on the same connection but outside the subscription loop —
        # avoids contention between one-to-many (subscription) and one-to-one
        # (get_transaction) traffic on the same persistent WebSocket.
        async def _resolve(tx_hash):
            try:
                return tx_hash, await w3.eth.get_transaction(tx_hash)
            except Exception:
                # Transaction may have already been dropped/mined between
                # notification and lookup — normal for a fraction of hashes.
                return tx_hash, None

        results = await asyncio.gather(*(_resolve(h) for h in collected_hashes))

        hashes_resolved = sum(1 for _, tx in results if tx is not None)

        observed = []
        raw_tx_sample = []
        selector_counts: dict[str, int] = {}
        known_router_hits: dict[str, int] = {name: 0 for name in KNOWN_ROUTERS_FOR_HITS}

        for tx_hash, tx in results:
            if tx is None:
                continue

            tx_to = (tx.get("to") or "").lower()
            input_data = tx.get("input", "")
            if isinstance(input_data, bytes):
                input_data = "0x" + input_data.hex()

            selector = input_data[2:10] if len(input_data) >= 10 else ""

            raw_tx_sample.append({
                "to": tx_to,
                "selector": selector,
                "calldata_len": len(input_data),
            })

            if selector:
                selector_counts[selector] = selector_counts.get(selector, 0) + 1

            for _name, _addr in KNOWN_ROUTERS_FOR_HITS.items():
                if tx_to == _addr:
                    known_router_hits[_name] += 1

            match_type = None

            if tx_to == pool_address:
                match_type = "direct_pool_call"
            elif tx_to in KNOWN_ROUTERS_POLYGON:
                if _calldata_references_both_tokens(input_data, token0, token1):
                    match_type = "router_swap"
                elif _multihop_references_both_tokens(input_data, token0, token1):
                    match_type = "router_swap"
            elif tx_to == UNIVERSAL_ROUTER_POLYGON:
                if _universal_router_references_both_tokens(input_data, token0, token1):
                    match_type = "universal_router_swap"
            elif tx_to == ONEINCH_AGGREGATION_ROUTER_V6:
                if _oneinch_references_both_tokens(input_data, token0, token1):
                    match_type = "aggregator_1inch"

            if match_type:
                observed.append({
                    "tx_hash": tx_hash if isinstance(tx_hash, str) else tx_hash.hex(),
                    "match_type": match_type,
                    "to": tx.get("to"),
                    "from": tx.get("from"),
                    "gas_price_gwei": (tx.get("gasPrice") or 0) / 1e9,
                    "value_wei": tx.get("value", 0),
                })

    return {
        "hashes_seen": hashes_seen,
        "hashes_resolved": hashes_resolved,
        "observed": observed,
        "raw_tx_sample": raw_tx_sample,
        "selector_counts": selector_counts,
        "known_router_hits": known_router_hits,
    }


async def check_pending_swaps_on_pool(
    pool_address: str,
    token0: str,
    token1: str,
    chain: str = "polygon",
    duration_seconds: int = 15,
) -> dict:
    """
    Watches the public mempool for `duration_seconds`, then automatically
    self-verifies by querying confirmed on-chain swaps for a comparable block
    window. Returns both the pending observations and a coverage estimate.

    Detection covers four route types (tagged in match_type):
      - "direct_pool_call": tx.to is the pool address directly
      - "router_swap": SwapRouter / SwapRouter02, single-hop or multi-hop
      - "universal_router_swap": UniversalRouter V3_SWAP_EXACT_IN/OUT commands
      - "aggregator_1inch": 1inch AggregationRouter v6 swap()

    After the observation window the tool calls check_confirmed_swaps_on_pool
    internally over a block window sized to match `duration_seconds` (using
    the ~2 s/block Polygon block time). Results appear in:
      - confirmed_check: the full check_confirmed_swaps_on_pool result dict,
        or null if that call fails
      - coverage_estimate: a string categorising how much of the confirmed
        on-chain volume was caught in the mempool watch

    Args:
        pool_address: the pool contract address (used to catch direct calls)
        token0: address of one of the pool's two tokens
        token1: address of the pool's other token
        chain: only "polygon" is supported (see module docstring for why)
        duration_seconds: how long to watch, capped at 60 seconds

    Returns:
        dict with pending_transactions_observed (list), count, routes
        (per-match_type breakdown with all four keys always present),
        hashes_seen, hashes_resolved, confirmed_check, and coverage_estimate.
    """
    if not chain_has_mempool(chain):
        return {
            "error": (
                f"'{chain}' has no public mempool to watch (centralized "
                f"sequencer architecture — see README for details). "
                f"This tool currently only supports 'polygon'."
            )
        }

    duration_seconds = min(max(duration_seconds, 1), MAX_DURATION_SECONDS)

    rpc_url = get_rpc_url(chain)
    ws_url = to_ws_url(rpc_url)

    raw = await _watch_pool(ws_url, pool_address, token0, token1, duration_seconds)
    observed = raw["observed"]

    routes: dict[str, int] = {
        "direct_pool_call": 0,
        "router_swap": 0,
        "universal_router_swap": 0,
        "aggregator_1inch": 0,
    }
    for tx in observed:
        routes[tx["match_type"]] = routes.get(tx["match_type"], 0) + 1

    count = len(observed)

    # Self-verification: query confirmed swaps over a block window sized to give
    # a reliable signal. The window is intentionally wider than duration_seconds:
    # a 15 s pending watch maps to only ~8 blocks, which is too thin to draw any
    # conclusion about swap activity. MIN_CONFIRMED_LOOKBACK_BLOCKS (≈10 min)
    # is the floor so the confirmed check always covers a meaningful period.
    lookback_blocks = max(
        MIN_CONFIRMED_LOOKBACK_BLOCKS,
        round(duration_seconds / _POLYGON_APPROX_BLOCK_TIME_S),
    )
    confirmed_check = None
    coverage_estimate = "unknown — confirmed check failed"

    try:
        confirmed_result = await asyncio.to_thread(
            _run_confirmed_check, pool_address, chain, lookback_blocks
        )
        if "error" in confirmed_result:
            confirmed_check = None
            coverage_estimate = (
                f"unknown — confirmed check returned error: {confirmed_result['error']}"
            )
        else:
            confirmed_check = confirmed_result
            confirmed_count = confirmed_check["swap_count"]
            if confirmed_count == 0:
                if lookback_blocks >= MIN_CONFIRMED_LOOKBACK_BLOCKS:
                    approx_min = (lookback_blocks * _POLYGON_APPROX_BLOCK_TIME_S) // 60
                    coverage_estimate = (
                        f"no_recent_activity — no confirmed swaps in the last "
                        f"{lookback_blocks} blocks (~{approx_min} min); "
                        f"this pool appears genuinely inactive"
                    )
                else:
                    coverage_estimate = (
                        f"inconclusive — no confirmed swaps found but the "
                        f"{lookback_blocks}-block confirmed window is too short "
                        f"to draw conclusions"
                    )
            elif count == 0:
                coverage_estimate = (
                    "low — confirmed swaps exist on this pool but none were caught "
                    "pending; likely routed through unrecognized contracts"
                )
            else:
                ratio = count / confirmed_count
                if ratio > 0.5:
                    level = "high"
                elif ratio >= 0.2:
                    level = "medium"
                else:
                    level = "low"
                coverage_estimate = (
                    f"{level} (ratio {ratio:.2f}: {count} pending caught vs "
                    f"{confirmed_count} confirmed)"
                )
    except Exception as exc:
        logger.error(
            "confirmed check failed internally (pool=%s chain=%s lookback=%d): %s: %s",
            pool_address, chain, lookback_blocks,
            type(exc).__name__, exc,
            exc_info=True,
        )
        confirmed_check = None
        coverage_estimate = (
            f"unknown — confirmed check failed: {type(exc).__name__}: {exc}"
        )

    return {
        "chain": chain,
        "pool_address": pool_address,
        "token0": token0,
        "token1": token1,
        "duration_seconds": duration_seconds,
        "hashes_seen": raw["hashes_seen"],
        "hashes_resolved": raw["hashes_resolved"],
        "pending_transactions_observed": observed,
        "count": count,
        "routes": routes,
        "raw_tx_sample": raw["raw_tx_sample"],
        "selector_counts": raw["selector_counts"],
        "known_router_hits": raw["known_router_hits"],
        "confirmed_check": confirmed_check,
        "coverage_estimate": coverage_estimate,
    }
