"""Quantum exposure of the coins a list of addresses holds today.

For each address the report answers two questions:

1. What does it hold now? One ``scantxoutset`` pass over the node's UTXO set finds
   every unspent output paying to the listed addresses.
2. Is its public key already visible on-chain? For P2PK, bare multisig and Taproot
   outputs the key is in the output itself. For hash-based addresses (P2PKH, P2SH,
   P2WPKH, P2WSH) the key or script is revealed by the first spend from the address,
   so the tool looks for one: ``scanblocks`` finds candidate blocks and each is checked,
   oldest first, stopping as soon as every address has been decided.

Coins whose key is visible are open to a "long exposure" attack: someone with a
large enough quantum computer could derive the private key at leisure. Coins behind a
hash are exposed only while a spend waits to confirm. Only addresses that still hold
coins are checked for earlier spends, since exposure matters for coins that can still
be taken.

The result describes keys and coins, not owners. It does not say who controls an
address or whether its funds are frozen or seized.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from btc_trace.heuristics import input_addresses, to_sats
from btc_trace.rpc import NodeClient
from btc_trace.schema import EXPOSURE_REPORT_VERSION
from btc_trace.scripts import LABELS, hash_only, key_in_output, script_type
from btc_trace.trace import (
    DEFAULT_WORKERS,
    LevelCache,
    fetch_blocks,
    summarize_block,
    to_date,
)

Progress = Callable[[str], None]
Bar = Callable[[str, int, int, str], None]

KEY_IN_OUTPUT = "key in output"
SPENT_BEFORE = "spent before"
HASH_ONLY = "hash only"
NO_BALANCE = "no balance"
NOT_APPLICABLE = "not applicable"

REASONS = {
    KEY_IN_OUTPUT: "the output script contains the public key",
    SPENT_BEFORE: "an earlier spend from this address revealed its public key or script",
    HASH_ONLY: "never spent from: only a hash of the key is on-chain",
    NO_BALANCE: "holds nothing today, so there is nothing to expose",
    NOT_APPLICABLE: "not a key-based output type",
}

NOTE = (
    "Exposure describes keys, not owners: it says whether a public key is visible "
    "on-chain, not who controls the address or whether its funds are frozen or seized. "
    "A visible key is a risk only against a quantum computer large enough to run "
    "Shor's algorithm, which does not exist today."
)


@dataclass
class FirstSpend:
    height: int
    date: str | None
    txid: str


@dataclass
class AddressExposure:
    address: str
    script_type: str
    sdn_ref: str | None
    sdn_name: str | None
    balance_btc: float
    utxos: int
    oldest_utxo_height: int | None
    status: str  # one of REASONS
    exposed: bool
    first_spend: FirstSpend | None = None


@dataclass
class Totals:
    addresses: int = 0
    funded_addresses: int = 0
    exposed_addresses: int = 0
    balance_btc: float = 0.0
    exposed_btc: float = 0.0
    hash_only_btc: float = 0.0


@dataclass
class ExposureResult:
    snapshot: dict[str, Any]
    totals: Totals
    by_type: dict[str, Totals]
    by_entry: list[dict[str, Any]]
    addresses: list[AddressExposure]
    candidate_blocks: int = 0
    blocks_checked: int = 0
    note: str = NOTE
    report_kind: str = "exposure"
    report_version: str = EXPOSURE_REPORT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _btc(sats: int) -> float:
    return sats / 100_000_000


def _address_of(unspent: dict[str, Any], by_script: dict[str, str]) -> str | None:
    """The listed address an unspent output pays to, by its script (or its descriptor)."""
    address = by_script.get(unspent["scriptPubKey"])
    if address is None:
        match = re.match(r"addr\(([^)]+)\)", unspent.get("desc", ""))
        address = match.group(1) if match else None
    return address


def first_spends(
    client: NodeClient,
    addresses: list[str],
    stop_height: int,
    workers: int = DEFAULT_WORKERS,
    cache_dir: Path | None = None,
    progress: Progress | None = None,
    bar: Bar | None = None,
) -> tuple[dict[str, FirstSpend], int, int]:
    """The first confirmed spend from each address, up to ``stop_height``.

    Returns (first spends by address, candidate blocks, blocks checked). Candidate
    blocks are checked oldest first and checking stops once every address has spent,
    so busy addresses that spent early cost little. Progress is saved in ``cache_dir``.
    """
    say = progress or (lambda _msg: None)
    draw = bar or (lambda _label, _done, _total, _detail: None)
    if not addresses:
        return {}, 0, 0
    frontier = dict.fromkeys(addresses, 0)
    cache = LevelCache(cache_dir, frontier) if cache_dir else None

    cached_scan = cache.load_scan() if cache else None
    if cached_scan and cached_scan[0] >= stop_height:
        blockhashes = cached_scan[1]
        say(f"  reusing saved block scan to block {cached_scan[0]:,}")
    else:
        low = cached_scan[0] + 1 if cached_scan else 0
        span = stop_height - low + 1

        def chunk_started(lo: int, _hi: int) -> None:
            draw("scanning", lo - low, span, f"block {lo:,} of {stop_height:,}")

        found = client.scan_blocks(sorted(frontier), low, stop_height, progress=chunk_started)
        draw("scanning", span, span, f"block {stop_height:,} of {stop_height:,}")
        blockhashes = (cached_scan[1] if cached_scan else []) + found
        if cache:
            cache.save_scan(stop_height, blockhashes)

    done = cache.load_blocks() if cache else {}
    pending = set(addresses)
    spends: dict[str, FirstSpend] = {}
    todo = [h for h in blockhashes if h not in done]
    fetched = fetch_blocks(client, todo, workers)
    checked = 0
    say(f"  {len(blockhashes):,} candidate block(s) to check, oldest first")
    try:
        for blockhash in blockhashes:
            if not pending:
                break
            record = done.get(blockhash)
            if record is None:
                record = summarize_block(next(fetched), frontier)
                if cache:
                    cache.append_block(record)
            checked += 1
            draw("checking", checked, len(blockhashes), f"{len(pending):,} address(es) left")
            for tx in record["spends"]:
                for address in input_addresses(tx):
                    if address in pending:
                        pending.discard(address)
                        spends[address] = FirstSpend(
                            record["height"], to_date(record.get("time")), tx["txid"]
                        )
    finally:
        fetched.close()
    if checked < len(blockhashes):  # stopped early; otherwise the bar is already full
        draw("checking", len(blockhashes), len(blockhashes), "every address decided")
    return spends, len(blockhashes), checked


def exposure(
    client: NodeClient,
    addresses: list[str],
    *,
    seed_info: dict[str, dict[str, str]] | None = None,
    workers: int = DEFAULT_WORKERS,
    cache_dir: Path | None = None,
    progress: Progress | None = None,
    bar: Bar | None = None,
) -> ExposureResult:
    """Measure how much of what ``addresses`` hold today sits behind a visible key."""
    say = progress or (lambda _msg: None)
    seed_info = seed_info or {}
    addresses = list(dict.fromkeys(addresses))

    say(f"checking {len(addresses)} address(es) with the node")
    by_script = {client.address_script(a): a for a in addresses}
    kinds = {a: script_type(s) for s, a in by_script.items()}

    say("scanning the node's UTXO set for their coins (this takes a few minutes)")
    scan = client.scan_utxos(addresses)
    height = scan["height"]
    header = client.call("getblockheader", scan["bestblock"])
    snapshot = {
        "height": height,
        "bestblock": scan["bestblock"],
        "date": to_date(header.get("time")),
        "utxos_scanned": scan.get("txouts", 0),
    }

    sats: dict[str, int] = dict.fromkeys(addresses, 0)
    counts: dict[str, int] = dict.fromkeys(addresses, 0)
    oldest: dict[str, int] = {}
    for unspent in scan.get("unspents", []):
        address = _address_of(unspent, by_script)
        if address not in sats:
            continue
        sats[address] += to_sats(unspent["amount"])
        counts[address] += 1
        oldest[address] = min(oldest.get(address, unspent["height"]), unspent["height"])

    funded = [a for a in addresses if sats[a] > 0]
    to_check = [a for a in funded if hash_only(kinds[a])]
    say(
        f"  {len(funded)} address(es) hold coins; checking {len(to_check)} hash-based "
        "one(s) for an earlier spend"
    )
    spends, candidates, checked = first_spends(
        client, to_check, height, workers=workers, cache_dir=cache_dir, progress=say, bar=bar
    )

    rows = []
    for address in addresses:
        kind = kinds[address]
        if sats[address] == 0:
            status = NO_BALANCE
        elif key_in_output(kind):
            status = KEY_IN_OUTPUT
        elif hash_only(kind):
            status = SPENT_BEFORE if address in spends else HASH_ONLY
        else:
            status = NOT_APPLICABLE
        info = seed_info.get(address, {})
        rows.append(
            AddressExposure(
                address=address,
                script_type=kind,
                sdn_ref=info.get("sdn_ref"),
                sdn_name=info.get("sdn_name"),
                balance_btc=_btc(sats[address]),
                utxos=counts[address],
                oldest_utxo_height=oldest.get(address),
                status=status,
                exposed=status in (KEY_IN_OUTPUT, SPENT_BEFORE),
                first_spend=spends.get(address),
            )
        )
    rows.sort(key=lambda r: (-r.balance_btc, r.address))
    return ExposureResult(
        snapshot=snapshot,
        totals=_totals(rows),
        by_type={
            LABELS.get(kind, kind): _totals([r for r in rows if r.script_type == kind])
            for kind in sorted({r.script_type for r in rows})
        },
        by_entry=_by_entry(rows),
        addresses=rows,
        candidate_blocks=candidates,
        blocks_checked=checked,
    )


def _totals(rows: list[AddressExposure]) -> Totals:
    t = Totals(addresses=len(rows))
    balance = exposed = hidden = 0
    for r in rows:
        value = to_sats(r.balance_btc)
        balance += value
        if value:
            t.funded_addresses += 1
        if r.exposed:
            t.exposed_addresses += 1
            exposed += value
        elif r.status == HASH_ONLY:
            hidden += value
    t.balance_btc, t.exposed_btc, t.hash_only_btc = _btc(balance), _btc(exposed), _btc(hidden)
    return t


def _by_entry(rows: list[AddressExposure]) -> list[dict[str, Any]]:
    groups: dict[str, list[AddressExposure]] = {}
    names: dict[str, str | None] = {}
    for r in rows:
        if r.sdn_ref:
            groups.setdefault(r.sdn_ref, []).append(r)
            names.setdefault(r.sdn_ref, r.sdn_name)
    entries = [
        {"sdn_ref": ref, "sdn_name": names[ref], **asdict(_totals(group))}
        for ref, group in groups.items()
    ]
    entries.sort(key=lambda e: (-e["balance_btc"], e["sdn_ref"]))
    return entries
