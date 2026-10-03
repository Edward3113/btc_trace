"""Multi-hop forward tracing from seed addresses.

Bitcoin Core has no index of which transaction spent a given output, so tracing works
level by level:

1. ``scanblocks`` (BIP158 block filters) finds every block touching the frontier
   addresses at or after the height they were reached.
2. Each block is fetched with ``getblock <hash> 3`` and the transactions that *spend*
   from a frontier address are kept.
3. Every output of those transactions becomes a hop. Outputs above the value floor
   join the next frontier, until the depth or address limit is reached.

Each hop is labelled with the change heuristic, so an analyst can tell funds that
probably stayed with the same owner from funds that probably moved to someone else.
Likely CoinJoins stop the trace on that branch: their outputs cannot be linked to the
inputs with any confidence. All results are heuristic estimates, not proof of
ownership or identity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from btc_trace.heuristics import (
    detect_change,
    input_addresses,
    looks_like_coinjoin,
    output_address,
    to_sats,
)
from btc_trace.rpc import NodeClient

DISCLAIMER = (
    "Heuristic estimates only. Common-input ownership and change detection can be wrong, "
    "and an address appearing here is not proof of ownership, identity, or wrongdoing."
)


@dataclass
class Hop:
    depth: int
    height: int
    txid: str
    from_addresses: list[str]
    vout: int
    to_address: str
    value_btc: float
    label: str  # "change (heuristic)" or "payment"
    followed: bool


@dataclass
class CoinJoinStop:
    depth: int
    height: int
    txid: str
    from_addresses: list[str]


@dataclass
class TraceResult:
    seeds: list[str]
    parameters: dict[str, Any]
    scanned_to_height: int
    hops: list[Hop] = field(default_factory=list)
    coinjoin_stops: list[CoinJoinStop] = field(default_factory=list)
    unexplored: list[str] = field(default_factory=list)
    truncated: bool = False
    note: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def spends_from(
    client: NodeClient, frontier: dict[str, int], stop_height: int
) -> list[tuple[int, dict[str, Any]]]:
    """(height, tx) for transactions spending from any frontier address, oldest first."""
    if not frontier:
        return []
    start = min(frontier.values())
    found = []
    for blockhash in client.scan_blocks(sorted(frontier), start, stop_height):
        block = client.get_block(blockhash)
        height = block["height"]
        for tx in block["tx"]:
            spenders = {a for a in input_addresses(tx) if a in frontier}
            # Ignore spends from before the address entered the trace.
            if spenders and height >= min(frontier[a] for a in spenders):
                found.append((height, tx))
    found.sort(key=lambda item: item[0])
    return found


def trace(
    client: NodeClient,
    seeds: list[str],
    *,
    max_depth: int = 2,
    max_addresses: int = 100,
    min_value_btc: float = 0.0,
    start_height: int = 0,
) -> TraceResult:
    stop_height = client.tip_height()
    result = TraceResult(
        seeds=sorted(set(seeds)),
        parameters={
            "max_depth": max_depth,
            "max_addresses": max_addresses,
            "min_value_btc": min_value_btc,
            "start_height": start_height,
        },
        scanned_to_height=stop_height,
    )
    min_sats = to_sats(min_value_btc)
    visited: set[str] = set(result.seeds)
    frontier: dict[str, int] = dict.fromkeys(result.seeds, start_height)
    seen_txids: set[str] = set()

    for depth in range(1, max_depth + 1):
        next_frontier: dict[str, int] = {}
        for height, tx in spends_from(client, frontier, stop_height):
            txid = tx["txid"]
            if txid in seen_txids:
                continue
            seen_txids.add(txid)
            spenders = sorted({a for a in input_addresses(tx) if a in frontier})

            if looks_like_coinjoin(tx):
                result.coinjoin_stops.append(CoinJoinStop(depth, height, txid, spenders))
                continue

            change = detect_change(tx)
            for out in tx.get("vout", []):
                address = output_address(out)
                if address is None:  # OP_RETURN or non-standard output
                    continue
                label = "change (heuristic)" if change and change.vout == out["n"] else "payment"
                follow = to_sats(out["value"]) >= min_sats and address not in visited
                if follow and len(visited) >= max_addresses:
                    follow = False
                    result.truncated = True
                if follow:
                    visited.add(address)
                    next_frontier[address] = height
                result.hops.append(
                    Hop(
                        depth=depth,
                        height=height,
                        txid=txid,
                        from_addresses=spenders,
                        vout=out["n"],
                        to_address=address,
                        value_btc=out["value"],
                        label=label,
                        followed=follow,
                    )
                )
        frontier = next_frontier
        if not frontier:
            break

    # Addresses reached at the last level whose own spends were not examined.
    result.unexplored = sorted(frontier)
    return result
