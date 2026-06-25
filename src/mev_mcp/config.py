"""Chain configuration for mev-mcp.

Mirrors the CHAINS pattern used in defi-mcp for consistency, but with an
explicit capability flag per chain: mempool-dependent tools only work where
a public mempool actually exists. Arbitrum is a rollup with a centralized
sequencer — there's no public mempool to watch, so check_pending_swaps_on_pool
is structurally unavailable there, not just "not yet implemented".
"""

import os

CHAINS = {
    "polygon": {
        "rpc_env_var": "POLYGON_RPC_URL",
        "chain_id": 137,
        "has_public_mempool": True,
    },
    "arbitrum": {
        "rpc_env_var": "ARBITRUM_RPC_URL",
        "chain_id": 42161,
        "has_public_mempool": False,  # centralized sequencer, no public mempool
    },
}


def get_rpc_url(chain: str) -> str:
    """Returns the configured RPC URL for a chain, or raises with a clear message."""
    if chain not in CHAINS:
        raise ValueError(
            f"Unknown chain '{chain}'. Supported chains: {list(CHAINS.keys())}"
        )
    env_var = CHAINS[chain]["rpc_env_var"]
    url = os.environ.get(env_var)
    if not url:
        raise ValueError(
            f"{env_var} is not set. Add it to your environment (see .env.example)."
        )
    return url


def to_ws_url(rpc_url: str) -> str:
    """
    Converts an HTTPS RPC URL to its WSS equivalent for providers that follow
    the common convention (Alchemy, and most major providers): same host,
    https:// -> wss://. If the URL is already wss://, returns it unchanged.

    This is a convenience for providers using the standard pattern. If your
    provider uses a genuinely different URL for WebSocket vs HTTP, set the
    *_RPC_URL environment variable directly to the wss:// URL instead —
    that's also supported, since this function is a no-op on already-wss:// URLs.
    """
    if rpc_url.startswith("wss://"):
        return rpc_url
    if rpc_url.startswith("https://"):
        return "wss://" + rpc_url[len("https://"):]
    if rpc_url.startswith("http://"):
        return "ws://" + rpc_url[len("http://"):]
    raise ValueError(f"Unrecognized RPC URL scheme: {rpc_url}")


def chain_has_mempool(chain: str) -> bool:
    if chain not in CHAINS:
        raise ValueError(
            f"Unknown chain '{chain}'. Supported chains: {list(CHAINS.keys())}"
        )
    return CHAINS[chain]["has_public_mempool"]
