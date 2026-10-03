"""Clustering heuristics over decoded transactions.

Transactions use Bitcoin Core's ``getrawtransaction <txid> 2`` shape, where each
input carries its previous output under ``prevout``.

Every result here is a heuristic estimate, not proof of common ownership or identity.
CoinJoin and other collaborative transactions deliberately break these assumptions,
so likely CoinJoins are excluded rather than clustered.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

Tx = dict[str, Any]

SATS_PER_BTC = 100_000_000
ROUND_UNIT_SATS = 100_000  # 0.001 BTC; payments are often round, change rarely is


def to_sats(value_btc: float) -> int:
    return round(value_btc * SATS_PER_BTC)


def is_coinbase(tx: Tx) -> bool:
    return any("coinbase" in vin for vin in tx.get("vin", []))


def input_addresses(tx: Tx) -> list[str]:
    addresses = []
    for vin in tx.get("vin", []):
        address = vin.get("prevout", {}).get("scriptPubKey", {}).get("address")
        if address:
            addresses.append(address)
    return addresses


def output_address(vout: dict[str, Any]) -> str | None:
    return vout.get("scriptPubKey", {}).get("address")


def looks_like_coinjoin(tx: Tx, min_equal_outputs: int = 3) -> bool:
    """Several inputs and several outputs of one identical value suggest a CoinJoin."""
    values = Counter(to_sats(v["value"]) for v in tx.get("vout", []))
    most_common = values.most_common(1)
    if not most_common:
        return False
    _, count = most_common[0]
    return count >= min_equal_outputs and len(tx.get("vin", [])) >= min_equal_outputs


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


@dataclass
class ClusterResult:
    clusters: list[set[str]]
    skipped_coinjoin: list[str] = field(default_factory=list)


def common_input_clusters(txs: list[Tx]) -> ClusterResult:
    """Common-input-ownership heuristic: addresses spent together share an owner.

    Coinbase transactions have no spent addresses and are ignored. Likely CoinJoins
    are skipped and reported so an analyst can review them.
    """
    uf = _UnionFind()
    skipped: list[str] = []
    for tx in txs:
        if is_coinbase(tx):
            continue
        if looks_like_coinjoin(tx):
            skipped.append(tx.get("txid", "?"))
            continue
        addresses = input_addresses(tx)
        for address in addresses:
            uf.find(address)
        for other in addresses[1:]:
            uf.union(addresses[0], other)

    groups: dict[str, set[str]] = {}
    for address in uf.parent:
        groups.setdefault(uf.find(address), set()).add(address)
    clusters = sorted(groups.values(), key=lambda c: (-len(c), sorted(c)))
    return ClusterResult(clusters=clusters, skipped_coinjoin=skipped)


@dataclass
class ChangeGuess:
    vout: int
    address: str | None
    reasons: list[str]


def detect_change(tx: Tx) -> ChangeGuess | None:
    """Conservative change detection for two-output transactions.

    Returns a guess only when the signals point at exactly one output:

    * address reuse: an output pays back to one of the input addresses
    * script type: only one output matches the script type every input uses
    * round amount: the other output is a round payment and this one is not
    """
    if is_coinbase(tx) or looks_like_coinjoin(tx):
        return None
    outputs = tx.get("vout", [])
    if len(outputs) != 2:
        return None

    inputs = set(input_addresses(tx))
    input_types = {
        vin.get("prevout", {}).get("scriptPubKey", {}).get("type") for vin in tx.get("vin", [])
    }

    # Address reuse is the strongest signal; it settles the guess on its own.
    reused = [v for v in outputs if output_address(v) in inputs]
    if len(reused) == 1:
        v = reused[0]
        return ChangeGuess(v["n"], output_address(v), ["pays back to an input address"])

    reasons: dict[int, list[str]] = {v["n"]: [] for v in outputs}

    if len(input_types) == 1:
        (input_type,) = input_types
        matching = [v for v in outputs if v["scriptPubKey"].get("type") == input_type]
        if len(matching) == 1:
            reasons[matching[0]["n"]].append(f"only output matching input script type {input_type}")

    rounds = [v for v in outputs if to_sats(v["value"]) % ROUND_UNIT_SATS == 0]
    if len(rounds) == 1:
        other = next(v for v in outputs if v is not rounds[0])
        reasons[other["n"]].append("other output is a round amount")

    flagged = [n for n, r in reasons.items() if r]
    if len(flagged) != 1:
        return None  # no signal, or signals disagree
    n = flagged[0]
    vout = next(v for v in outputs if v["n"] == n)
    return ChangeGuess(n, output_address(vout), reasons[n])
