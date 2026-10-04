"""Read a Bitcoin Core UTXO snapshot and measure quantum exposure by script type.

``bitcoin-cli dumptxoutset <file> latest`` writes every unspent output the node knows
about to one compact file. This module streams that file once and adds up, for each
output script type, how many coins exist and how much BTC they hold, both overall and
by the block height each coin was created at (which is when it last moved).

While reading, it can also recompute the node's own UTXO set hash (the
``txoutset_hash`` that ``dumptxoutset`` returns, Bitcoin Core's ``hash_serialized_3``):
a SHA256d over every coin in order. A match proves the file was copied and parsed
exactly, coin for coin.

File format (Bitcoin Core ``WriteUTXOSnapshot``, version 2): a header with magic
bytes, version, network magic, base block hash and coin count, then coins grouped by
transaction: txid, number of coins, and for each coin its output index, height and
coinbase flag, compressed amount and compressed script. The decoding follows
``contrib/utxo-tools/utxo_to_sqlite.py`` from Bitcoin Core (MIT licence).

Only the output type matters here: P2PK, bare multisig and Taproot outputs show a
public key; the rest show a hash until spent. Whether a hash-based address has
already revealed its key by spending (address reuse) needs the block history and is
a separate step.
"""

from __future__ import annotations

import hashlib
import mmap
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from btc_trace.schema import UTXO_REPORT_VERSION
from btc_trace.scripts import (
    ANCHOR,
    LABELS,
    NULLDATA,
    P2PK,
    P2PKH,
    P2SH,
    P2TR,
    P2WPKH,
    P2WSH,
    hash_only,
    key_in_output,
    script_type,
)

MAGIC = b"utxo\xff"
SUPPORTED_VERSION = 2
NETWORKS = {
    b"\xf9\xbe\xb4\xd9": "main",
    b"\x0a\x03\xcf\x40": "signet",
    b"\x0b\x11\x09\x07": "test",
    b"\x1c\x16\x3f\x28": "testnet4",
    b"\xfa\xbf\xb5\xda": "regtest",
}
HEADER_SIZE = 5 + 2 + 4 + 32 + 8
BIN_SIZE = 1000  # blocks per age bin, about one week
BLOCKS_PER_YEAR = 52_560  # at the 10-minute target spacing
DORMANT_YEARS = 5

NOTE = (
    "Counts every unspent output in the node's UTXO set by script type. 'Key in "
    "output' covers types whose output script shows a public key (P2PK, bare "
    "multisig, Taproot). 'Revealed' covers hash-based outputs at addresses whose key "
    "or script an earlier spend has put on-chain (address reuse); it is null when no "
    "reveal scan was given. Coins are not owners: one wallet can hold many outputs, "
    "and an output's age is when it last moved."
)

Progress = Callable[[int, int], None]

_SECP_P = 2**256 - 2**32 - 977


class SnapshotError(ValueError):
    """The file is not a UTXO snapshot this tool can read."""


@dataclass
class SnapshotHeader:
    network: str
    base_hash: str  # display order, as RPCs show it
    coins: int


def read_header(data: bytes | mmap.mmap) -> SnapshotHeader:
    if len(data) < HEADER_SIZE or data[:5] != MAGIC:
        raise SnapshotError("not a UTXO snapshot (made with `dumptxoutset`)")
    version = int.from_bytes(data[5:7], "little")
    if version != SUPPORTED_VERSION:
        raise SnapshotError(
            f"snapshot format version {version}; this tool reads version {SUPPORTED_VERSION}"
        )
    network = NETWORKS.get(bytes(data[7:11]), f"unknown ({bytes(data[7:11]).hex()})")
    base_hash = bytes(data[11:43])[::-1].hex()
    coins = int.from_bytes(data[43:51], "little")
    return SnapshotHeader(network, base_hash, coins)


def decompress_amount(x: int) -> int:
    """Bitcoin Core's ``DecompressAmount``: satoshis from the compact encoding."""
    if x == 0:
        return 0
    x -= 1
    e = x % 10
    x //= 10
    if e < 9:
        d = (x % 9) + 1
        x //= 9
        n = x * 10 + d
    else:
        n = x + 1
    return n * 10**e


def _uncompressed_key(prefix: int, x_bytes: bytes) -> bytes:
    """The 65-byte public key for an x coordinate and parity (y^2 = x^3 + 7)."""
    x = int.from_bytes(x_bytes, "big")
    rhs = (pow(x, 3, _SECP_P) + 7) % _SECP_P
    y = pow(rhs, (_SECP_P + 1) // 4, _SECP_P)
    if pow(y, 2, _SECP_P) != rhs:
        raise SnapshotError(f"public key not on the curve: {x_bytes.hex()}")
    if (y & 1) != (prefix & 1):
        y = _SECP_P - y
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _classify(script: bytes) -> str:
    """Fast type checks for the common shapes; anything else goes to script_type."""
    n = len(script)
    if n == 22 and script[0] == 0 and script[1] == 20:
        return P2WPKH
    if n == 34 and script[1] == 32:
        if script[0] == 0x51:
            return P2TR
        if script[0] == 0:
            return P2WSH
    if script == b"\x51\x02\x4e\x73":
        return ANCHOR
    if n and script[0] == 0x6A:
        return NULLDATA
    return script_type(script.hex())


@dataclass
class TypeTally:
    coins: int = 0
    sats: int = 0


HASH_KINDS = (P2PKH, P2SH, P2WPKH, P2WSH)
FLUSH_EVERY = 1 << 20


@dataclass
class SnapshotStats:
    header: SnapshotHeader
    tallies: dict[str, TypeTally] = field(default_factory=dict)
    bins: dict[str, dict[int, TypeTally]] = field(default_factory=dict)  # kind -> bin
    coinbase_p2pk: TypeTally = field(default_factory=TypeTally)
    max_height: int = 0
    computed_hash: str | None = None
    # Filled when a set of revealed address hashes is given: hash-based coins whose
    # address has revealed its key or script.
    revealed: dict[str, TypeTally] | None = None
    revealed_bins: dict[str, dict[int, int]] | None = None  # kind -> bin -> sats


def read_snapshot(
    path: Path,
    *,
    verify: bool = True,
    progress: Progress | None = None,
    bin_size: int = BIN_SIZE,
    revealed: np.ndarray | None = None,
) -> SnapshotStats:
    """Stream a snapshot once and tally coins by type and age.

    With ``verify`` the UTXO set hash is recomputed along the way (about half again
    as long). ``progress(done, total)`` is called every 2^20 coins. ``revealed`` is a
    sorted array of address-hash prefixes from ``btc-trace reveal-scan``; when given,
    hash-based coins at those addresses are tallied as revealed.
    """
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        header = read_header(data)
        stats = SnapshotStats(header)
        _read_coins(data, header, stats, verify, progress, bin_size, revealed, None)
    return stats


def hash_targets(path: Path, progress: Progress | None = None) -> np.ndarray:
    """Sorted, distinct 8-byte prefixes of every hash-based coin's address hash.

    These are what ``reveal-scan`` looks for: the 20-byte hash in P2PKH, P2SH and
    P2WPKH outputs and the 32-byte hash in P2WSH outputs.
    """
    out = array("Q")
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        header = read_header(data)
        _read_coins(data, header, SnapshotStats(header), False, progress, BIN_SIZE, None, out)
    return np.unique(np.frombuffer(out, dtype=np.uint64))


class _RevealedTally:
    """Collects hash-based coins in batches and checks them against revealed hashes."""

    def __init__(self, revealed: np.ndarray, bin_size: int) -> None:
        self.revealed = revealed
        self.bin_size = bin_size
        self.prefixes = array("Q")
        self.sats = array("Q")
        self.kinds = array("B")
        self.heights = array("I")
        self.tallies = {k: [0, 0] for k in HASH_KINDS}
        self.bins: dict[str, dict[int, int]] = {k: {} for k in HASH_KINDS}

    def add(self, key: int, sats: int, kind_index: int, height: int) -> None:
        self.prefixes.append(key)
        self.sats.append(sats)
        self.kinds.append(kind_index)
        self.heights.append(height)
        if len(self.prefixes) >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        if not self.prefixes:
            return
        keys = np.frombuffer(self.prefixes, dtype=np.uint64)
        sats = np.frombuffer(self.sats, dtype=np.uint64)
        kinds = np.frombuffer(self.kinds, dtype=np.uint8)
        bins = np.frombuffer(self.heights, dtype=np.uint32) // self.bin_size
        if self.revealed.size:
            idx = np.searchsorted(self.revealed, keys)
            idx[idx == self.revealed.size] = self.revealed.size - 1
            hit = self.revealed[idx] == keys
        else:
            hit = np.zeros(keys.size, dtype=bool)
        for i, kind in enumerate(HASH_KINDS):
            sel = hit & (kinds == i)
            if not sel.any():
                continue
            chosen = sats[sel]
            self.tallies[kind][0] += int(sel.sum())
            self.tallies[kind][1] += int(chosen.sum())
            kind_bins = self.bins[kind]
            values, inverse = np.unique(bins[sel], return_inverse=True)
            sums = np.zeros(values.size, dtype=np.uint64)
            np.add.at(sums, inverse, chosen)
            for b, total in zip(values.tolist(), sums.tolist(), strict=True):
                kind_bins[b] = kind_bins.get(b, 0) + total
        self.prefixes = array("Q")
        self.sats = array("Q")
        self.kinds = array("B")
        self.heights = array("I")


def _read_coins(  # noqa: C901
    data, header, stats, verify, progress, bin_size, revealed, targets_out
) -> None:
    pos = HEADER_SIZE
    total = header.coins
    end = len(data)
    hasher = hashlib.sha256() if verify else None
    tallies: dict[str, list[int]] = {}
    bins: dict[str, dict[int, list[int]]] = {}
    coinbase_p2pk = [0, 0]
    max_height = 0
    left_in_tx = 0
    txid = b""
    report_every = 1 << 20
    tracker = _RevealedTally(revealed, bin_size) if revealed is not None else None
    track = tracker is not None or targets_out is not None
    key = b""

    def varint() -> int:
        nonlocal pos
        n = 0
        while True:
            byte = data[pos]
            pos += 1
            n = (n << 7) | (byte & 0x7F)
            if byte & 0x80:
                n += 1
            else:
                return n

    def compactsize() -> int:
        nonlocal pos
        n = data[pos]
        pos += 1
        if n < 253:
            return n
        width = {253: 2, 254: 4, 255: 8}[n]
        n = int.from_bytes(data[pos : pos + width], "little")
        pos += width
        return n

    try:
        for i in range(1, total + 1):
            if left_in_tx == 0:
                txid = data[pos : pos + 32]
                pos += 32
                left_in_tx = compactsize()
            vout = compactsize()
            code = varint()
            height = code >> 1
            sats = decompress_amount(varint())
            size = varint()
            if size == 0:
                kind = P2PKH
                key = data[pos : pos + 8]
                script = b"\x76\xa9\x14" + data[pos : pos + 20] + b"\x88\xac" if verify else b""
                pos += 20
            elif size == 1:
                kind = P2SH
                key = data[pos : pos + 8]
                script = b"\xa9\x14" + data[pos : pos + 20] + b"\x87" if verify else b""
                pos += 20
            elif size < 6:
                kind = P2PK
                x = data[pos : pos + 32]
                pos += 32
                if not verify:
                    script = b""
                elif size < 4:
                    script = bytes((33, size)) + x + b"\xac"
                else:
                    script = b"\x41" + _uncompressed_key(size - 2, x) + b"\xac"
                if code & 1:
                    coinbase_p2pk[0] += 1
                    coinbase_p2pk[1] += sats
            else:
                size -= 6
                if size > 10_000:
                    raise SnapshotError(f"coin {i}: script of {size} bytes is too long")
                script = data[pos : pos + size]
                pos += size
                kind = _classify(script)
                if kind in (P2WPKH, P2WSH, P2SH):
                    key = script[2:10]
                elif kind == P2PKH:
                    key = script[3:11]
            if pos > end:
                raise SnapshotError(f"file ends in the middle of coin {i} of {total}")

            tally = tallies.get(kind)
            if tally is None:
                tally = tallies[kind] = [0, 0]
                bins[kind] = {}
            tally[0] += 1
            tally[1] += sats
            b = bins[kind].get(height // bin_size)
            if b is None:
                b = bins[kind][height // bin_size] = [0, 0]
            b[0] += 1
            b[1] += sats
            if height > max_height:
                max_height = height
            if track and kind in HASH_KINDS:
                prefix = int.from_bytes(key, "big")
                if targets_out is not None:
                    targets_out.append(prefix)
                if tracker is not None:
                    tracker.add(prefix, sats, HASH_KINDS.index(kind), height)

            if hasher is not None:
                hasher.update(
                    txid
                    + vout.to_bytes(4, "little")
                    + code.to_bytes(4, "little")
                    + sats.to_bytes(8, "little")
                    + _compactsize_bytes(len(script))
                    + script
                )
            left_in_tx -= 1
            if progress and i % report_every == 0:
                progress(i, total)
    except IndexError:
        raise SnapshotError("file ends before all coins were read") from None

    if tracker is not None:
        tracker.flush()
        stats.revealed = {k: TypeTally(*v) for k, v in tracker.tallies.items()}
        stats.revealed_bins = tracker.bins
    if pos != end:
        raise SnapshotError(f"{end - pos} unexpected byte(s) after the last coin")
    if progress:
        progress(total, total)
    stats.tallies = {k: TypeTally(*v) for k, v in tallies.items()}
    stats.bins = {k: {h: TypeTally(*v) for h, v in b.items()} for k, b in bins.items()}
    stats.coinbase_p2pk = TypeTally(*coinbase_p2pk)
    stats.max_height = max_height
    if hasher is not None:
        stats.computed_hash = hashlib.sha256(hasher.digest()).digest()[::-1].hex()


def _compactsize_bytes(n: int) -> bytes:
    if n < 253:
        return bytes((n,))
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    return b"\xfe" + n.to_bytes(4, "little")


def _btc(sats: int) -> float:
    return sats / 100_000_000


def build_report(
    stats: SnapshotStats,
    *,
    base_height: int | None = None,
    base_date: str | None = None,
    expected: dict[str, Any] | None = None,
    bin_dates: dict[int, str | None] | None = None,
    bin_size: int = BIN_SIZE,
    reuse: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The report for a read snapshot.

    ``expected`` is the ``dumptxoutset`` reply saved by ``btc-trace dump-utxos``; when
    given, coin count, base block and hash are checked against it. ``reuse`` describes
    the reveal scan whose results were passed to ``read_snapshot`` as ``revealed``.
    """
    height = base_height if base_height is not None else stats.max_height
    total_coins = sum(t.coins for t in stats.tallies.values())
    total_sats = sum(t.sats for t in stats.tallies.values())
    dormant_before = height - DORMANT_YEARS * BLOCKS_PER_YEAR
    measured = stats.revealed is not None
    revealed = stats.revealed or {}
    revealed_bins = stats.revealed_bins or {}

    def is_old(b: int) -> bool:
        return (b + 1) * bin_size <= dormant_before

    by_type = {}
    exposed = hidden = dormant_exposed = dormant_all = 0
    revealed_sats = dormant_revealed = 0
    for kind in sorted(stats.tallies, key=lambda k: -stats.tallies[k].sats):
        tally = stats.tallies[kind]
        old = sum(t.sats for b, t in stats.bins[kind].items() if is_old(b))
        entry = {
            "script_type": kind,
            "coins": tally.coins,
            "btc": _btc(tally.sats),
            "share_of_supply": tally.sats / total_sats if total_sats else 0.0,
            "key_in_output": key_in_output(kind),
            "hash_only": hash_only(kind),
            "dormant_btc": _btc(old),
            "revealed_coins": None,
            "revealed_btc": None,
        }
        dormant_all += old
        if key_in_output(kind):
            exposed += tally.sats
            dormant_exposed += old
        elif hash_only(kind):
            hidden += tally.sats
            if measured:
                r = revealed.get(kind, TypeTally())
                entry["revealed_coins"] = r.coins
                entry["revealed_btc"] = _btc(r.sats)
                revealed_sats += r.sats
                dormant_revealed += sum(
                    v for b, v in revealed_bins.get(kind, {}).items() if is_old(b)
                )
        by_type[LABELS.get(kind, kind)] = entry

    checks = _checks(stats, total_coins, expected)
    bin_ids = sorted({b for kinds in stats.bins.values() for b in kinds})
    age = [
        {
            "start_height": b * bin_size,
            "start_date": (bin_dates or {}).get(b * bin_size),
            "btc_by_type": {
                kind: _btc(stats.bins[kind][b].sats)
                for kind in sorted(stats.bins)
                if b in stats.bins[kind]
            },
            "revealed_btc_by_type": {
                kind: _btc(revealed_bins[kind][b])
                for kind in sorted(revealed_bins)
                if b in revealed_bins[kind]
            },
        }
        for b in bin_ids
    ]
    exposed_total = exposed + revealed_sats
    return {
        "report_kind": "utxo-set",
        "report_version": UTXO_REPORT_VERSION,
        "snapshot": {
            "network": stats.header.network,
            "base_hash": stats.header.base_hash,
            "base_height": height,
            "date": base_date,
            "coins": stats.header.coins,
        },
        "totals": {
            "coins": total_coins,
            "btc": _btc(total_sats),
            "key_in_output_btc": _btc(exposed),
            "hash_only_btc": _btc(hidden),
            "other_btc": _btc(total_sats - exposed - hidden),
            "revealed_btc": _btc(revealed_sats) if measured else None,
            "exposed_btc": _btc(exposed_total) if measured else None,
            "exposed_share": (exposed_total / total_sats if total_sats else 0.0)
            if measured
            else None,
            "dormant_btc": _btc(dormant_all),
            "dormant_key_in_output_btc": _btc(dormant_exposed),
            "dormant_exposed_btc": _btc(dormant_exposed + dormant_revealed) if measured else None,
            "coinbase_p2pk_coins": stats.coinbase_p2pk.coins,
            "coinbase_p2pk_btc": _btc(stats.coinbase_p2pk.sats),
        },
        "dormancy": {
            "years": DORMANT_YEARS,
            "created_before_height": max(dormant_before, 0),
            "rule": (
                f"coins created at least {DORMANT_YEARS * BLOCKS_PER_YEAR:,} blocks "
                f"({DORMANT_YEARS} years at 10 minutes a block) before the snapshot, "
                f"counted in whole {bin_size:,}-block bins"
            ),
        },
        "reuse": reuse if measured else None,
        "by_type": by_type,
        "age_bins": {"bin_size": bin_size, "bins": age},
        "checks": checks,
        "note": NOTE,
    }


def _checks(
    stats: SnapshotStats, total_coins: int, expected: dict[str, Any] | None
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "coins_match_header": total_coins == stats.header.coins,
        "computed_txoutset_hash": stats.computed_hash,
        "expected_txoutset_hash": None,
        "hash_matches": None,
        "base_hash_matches": None,
        "coins_match_node": None,
    }
    if expected:
        checks["expected_txoutset_hash"] = expected.get("txoutset_hash")
        checks["base_hash_matches"] = expected.get("base_hash") == stats.header.base_hash
        checks["coins_match_node"] = expected.get("coins_written") == total_coins
        if stats.computed_hash and expected.get("txoutset_hash"):
            checks["hash_matches"] = stats.computed_hash == expected["txoutset_hash"]
    return checks
