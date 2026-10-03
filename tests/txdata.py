"""Synthetic transactions in Bitcoin Core's getrawtransaction verbosity-2 shape.

Addresses and txids are placeholders, not real on-chain data.
"""

from __future__ import annotations

from typing import Any


def vin(address: str, value: float, script_type: str = "witness_v0_keyhash") -> dict[str, Any]:
    return {
        "txid": "00" * 32,
        "vout": 0,
        "prevout": {
            "value": value,
            "scriptPubKey": {"address": address, "type": script_type},
        },
    }


def vout(n: int, address: str, value: float, script_type: str = "witness_v0_keyhash"):
    return {"n": n, "value": value, "scriptPubKey": {"address": address, "type": script_type}}


def tx(txid: str, inputs: list[dict], outputs: list[dict]) -> dict[str, Any]:
    return {"txid": txid, "vin": inputs, "vout": outputs}


COINBASE = {
    "txid": "cb" * 32,
    "vin": [{"coinbase": "03abcdef"}],
    "vout": [vout(0, "addr_miner", 3.125)],
}
