"""An in-memory chain that answers the RPC calls the tracer uses.

It models scanblocks with exact matching (as with filter_false_positives=true),
getblock verbosity 3, and getblockcount. All data is synthetic.
"""

from __future__ import annotations

import re
from typing import Any

from btc_trace.heuristics import input_addresses, output_address


class FakeChain:
    def __init__(self, blocks: dict[int, list[dict[str, Any]]]) -> None:
        self.blocks = blocks  # height -> transactions
        self.calls: list[tuple[str, list[Any]]] = []

    @staticmethod
    def block_hash(height: int) -> str:
        return f"{height:064x}"

    def _touches(self, tx: dict[str, Any], addresses: set[str]) -> bool:
        outputs = {output_address(v) for v in tx.get("vout", [])}
        return bool(addresses & (set(input_addresses(tx)) | outputs))

    def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if method == "getblockcount":
            return max(self.blocks)
        if method == "getblock":
            height = int(params[0], 16)
            return {"hash": params[0], "height": height, "tx": self.blocks[height]}
        if method == "scanblocks":
            _, descriptors, start, stop, _, _ = params
            addresses = {re.fullmatch(r"addr\((.+)\)", d).group(1) for d in descriptors}
            relevant = [
                self.block_hash(h)
                for h in sorted(self.blocks)
                if start <= h <= stop and any(self._touches(t, addresses) for t in self.blocks[h])
            ]
            return {
                "from_height": start,
                "to_height": stop,
                "relevant_blocks": relevant,
                "completed": True,
            }
        raise AssertionError(f"unexpected RPC {method}")
