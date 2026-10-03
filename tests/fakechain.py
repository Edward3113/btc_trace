"""An in-memory chain that answers the RPC calls the tracer uses.

It models scanblocks with exact matching (as with filter_false_positives=true),
getblock verbosity 3, getblockcount, and, for exposure checks, scantxoutset over a
given list of unspent outputs and validateaddress. All data is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from btc_trace.heuristics import input_addresses, output_address


class FakeChain:
    def __init__(
        self,
        blocks: dict[int, list[dict[str, Any]]],
        false_positives: set[int] | None = None,
        utxos: list[dict[str, Any]] | None = None,
        scripts: dict[str, str] | None = None,
    ) -> None:
        self.blocks = blocks  # height -> transactions
        # Heights the block filter wrongly matches when false positives are not filtered.
        self.false_positives = false_positives or set()
        # Unspent outputs for scantxoutset: {"address", "amount", "height"}.
        self.utxos = utxos or []
        # Output script (hex) per address; others get a made-up P2WPKH script.
        self.scripts = scripts or {}
        self.calls: list[tuple[str, list[Any]]] = []

    def script_of(self, address: str) -> str:
        if address in self.scripts:
            return self.scripts[address]
        return "0014" + hashlib.sha256(address.encode()).hexdigest()[:40]

    @staticmethod
    def block_hash(height: int) -> str:
        return f"{height:064x}"

    @staticmethod
    def block_time(height: int) -> int:
        """2020-01-01 00:00 UTC plus one day per block, so dates are easy to check."""
        return 1_577_836_800 + height * 86_400

    def _touches(self, tx: dict[str, Any], addresses: set[str]) -> bool:
        outputs = {output_address(v) for v in tx.get("vout", [])}
        return bool(addresses & (set(input_addresses(tx)) | outputs))

    def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if method == "getblockcount":
            return max(self.blocks)
        if method == "getblock":
            height = int(params[0], 16)
            return {
                "hash": params[0],
                "height": height,
                "time": self.block_time(height),
                "tx": self.blocks[height],
            }
        if method == "getblockheader":
            height = int(params[0], 16)
            return {"hash": params[0], "height": height, "time": self.block_time(height)}
        if method == "scanblocks":
            _, descriptors, start, stop, _, options = params
            addresses = {re.fullmatch(r"addr\((.+)\)", d).group(1) for d in descriptors}
            keep_fp = not options.get("filter_false_positives", False)
            relevant = [
                self.block_hash(h)
                for h in sorted(self.blocks)
                if start <= h <= stop
                and (
                    any(self._touches(t, addresses) for t in self.blocks[h])
                    or (keep_fp and h in self.false_positives)
                )
            ]
            return {
                "from_height": start,
                "to_height": stop,
                "relevant_blocks": relevant,
                "completed": True,
            }
        if method == "validateaddress":
            address = params[0]
            if address.startswith("INVALID"):
                return {"isvalid": False}
            return {"isvalid": True, "address": address, "scriptPubKey": self.script_of(address)}
        if method == "scantxoutset":
            if params[0] != "start":
                return None if params[0] == "status" else False
            wanted = {re.fullmatch(r"addr\((.+)\)", d).group(1) for d in params[1]}
            tip = max(self.blocks)
            unspents = [
                {
                    "txid": f"{i:064x}",
                    "vout": 0,
                    "scriptPubKey": self.script_of(u["address"]),
                    "desc": f"addr({u['address']})#fakesum0",
                    "amount": u["amount"],
                    "coinbase": False,
                    "height": u["height"],
                    "blockhash": self.block_hash(u["height"]),
                    "confirmations": tip - u["height"] + 1,
                }
                for i, u in enumerate(self.utxos)
                if u["address"] in wanted
            ]
            return {
                "success": True,
                "txouts": len(self.utxos),
                "height": tip,
                "bestblock": self.block_hash(tip),
                "unspents": unspents,
                "total_amount": sum(u["amount"] for u in unspents),
            }
        raise AssertionError(f"unexpected RPC {method}")
