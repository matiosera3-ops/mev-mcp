# mev-mcp

An MCP server for inspecting MEV-relevant activity on Polygon — pending mempool traffic, confirmed swap history, and gas pricing — exposed as tools an AI agent (or you, via Claude Desktop) can call directly.

This is a diagnostic and research tool, not a production MEV detector. It's built to be honest about what it sees and what it misses, rather than to look more capable than it is.

## What it does

| Tool | What it tells you | Source |
|---|---|---|
| `check_pending_swaps_on_pool` | Pending transactions touching a given Uniswap v3 pool, broken down by route type, plus a self-check against confirmed history | Public mempool (`eth_subscribe`) |
| `check_confirmed_swaps_on_pool` | Ground-truth swap count and originating contracts for a pool over a recent block range | Finalized chain state (`eth_getLogs`) |
| `get_gas_price_percentiles` | Gas price distribution over recent blocks | `eth_feeHistory` |
| `hello` | Connectivity check | — |

Polygon only. Arbitrum uses a centralized sequencer with no public mempool, so pending-transaction tools aren't meaningful there.

## The honest part: mempool coverage is partial, and the tool tells you so

Swaps on a given pool can be routed through many different contracts — Uniswap's own `SwapRouter`, `SwapRouter02`, `UniversalRouter`, or any of a long list of aggregators (1inch, Odos, Paraswap, and others). `check_pending_swaps_on_pool` currently decodes:

- Direct calls to the pool contract
- `SwapRouter` / `SwapRouter02` single-hop swaps (`exactInputSingle`, `exactOutputSingle`) and multi-hop swaps (`exactInput`, `exactOutput`, via packed-path decoding)
- `UniversalRouter` `V3_SWAP_EXACT_IN` / `V3_SWAP_EXACT_OUT` commands
- `1inch AggregationRouter v6` swaps

That's a meaningful slice of mempool traffic, but it is **not exhaustive**. In testing on a moderately active Polygon pool, well over half of confirmed swap volume routed through contracts outside this list — other aggregators, smart-contract wallets, and routing contracts we haven't decoded yet.

Rather than let that show up as a silent `count: 0` — indistinguishable from "this pool is just quiet" — `check_pending_swaps_on_pool` runs a quick confirmed-swap check over a comparable window and returns a `coverage_estimate` field alongside the raw count:

```jsonc
{
  "count": 0,
  "routes": { "router_swap": 0, "universal_router_swap": 0, "aggregator_1inch": 0, "direct_pool_call": 0 },
  "confirmed_check": { "swap_count": 4, "by_to_address": { "...": 1 } },
  "coverage_estimate": "low — confirmed swaps exist on this pool but none were caught pending; likely routed through unrecognized contracts"
}
```

Possible `coverage_estimate` values:

- `no_recent_activity — no confirmed swaps in the last N blocks (~M min); this pool appears genuinely inactive` — confirmed window large enough (≥300 blocks) to trust a zero result
- `inconclusive — … window too short to draw conclusions` — confirmed window smaller than the minimum reliable threshold
- `low — confirmed swaps exist on this pool but none were caught pending; likely routed through unrecognized contracts` — pool is active but none of the pending transactions matched a known router
- `low / medium / high (ratio R: N pending caught vs M confirmed)` — at least some pending matches found; ratio against confirmed volume gives a coverage signal
- `unknown — confirmed check failed: ExceptionType: message` — the self-check itself threw an exception; raw `count` still stands
- `unknown — confirmed check returned error: …` — RPC or other error from the confirmed check endpoint

If you need a quick, reliable read on whether a pool is active at all — independent of mempool coverage — use `check_confirmed_swaps_on_pool` directly. It reads finalized blocks, so it has no router-coverage blind spot, only the same long-tail-of-aggregators caveat in its `by_to_address` breakdown (you'll see contract addresses it doesn't attempt to label).

## Requirements

- Python 3.10+
- An RPC provider for Polygon with mempool (`eth_subscribe`) support — most free-tier providers (including Alchemy's free tier) support this for `newPendingTransactions`
- For `check_confirmed_swaps_on_pool`: an `eth_getLogs`-capable endpoint. Free-tier plans often cap the block range per call (Alchemy's free tier: 10 blocks); the tool chunks requests automatically, configurable via `MEV_MCP_LOGS_CHUNK_SIZE` (default `10`). On a paid plan, raising this (e.g. to `500`) significantly speeds up confirmed-swap lookups.

## Setup

```bash
git clone https://github.com/matiosera3-ops/mev-mcp
cd mev-mcp
pip install -e .
```

Add to your Claude Desktop config (`claude_desktop_config.json`):

```jsonc
{
  "mcpServers": {
    "mev-mcp": {
      "command": "python",
      "args": ["-m", "mev_mcp.server"],
      "env": {
        "POLYGON_RPC_URL": "https://your-polygon-rpc-url",
        "MEV_MCP_LOGS_CHUNK_SIZE": "10"
      }
    }
  }
}
```

Not yet published to PyPI — install from source for now.

## Known limitations

- **Router coverage is partial.** See above. Contributions adding decoders for additional aggregators (Odos, Paraswap, 0x) are welcome — the existing `SwapRouter`/`UniversalRouter`/`1inch` decoders in `pending_swaps.py` are a template for the calldata-matching pattern used.
- **Mempool visibility depends on your RPC provider.** Different providers see different shares of the public mempool. `hashes_seen` / `hashes_resolved` in the raw tool output give you a sense of how much traffic your provider surfaces.
- **Free-tier `eth_getLogs` limits make `check_confirmed_swaps_on_pool` slow** on a free Alchemy plan (chunking in 10-block calls). Fine for occasional diagnostic use; not built for high-frequency polling.
- **No Arbitrum mempool support**, structurally — there isn't a public one to watch.

## License

MIT
