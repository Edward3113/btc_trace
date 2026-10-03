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

Two kinds of transaction end a branch instead of being followed:

* Likely CoinJoins: their outputs cannot be linked to the inputs with any confidence.
* Likely service consolidations: many inputs from outside the trace swept into one or
  two outputs, the pattern of an exchange or other custodial service gathering its
  deposit addresses. Past that point the trace would follow the service's pooled
  funds, so it is reported as an exposure endpoint instead.

A sweep is marked *internal* when its destination falls inside the spender's own
cluster: then the funds stayed with the same owner rather than entering a service.

Clusters group addresses that the heuristics suggest share an owner, with the
transactions that link them. Large custodial wallets sweep hundreds of deposit
addresses while paying out several recipients at once ("batch sweeps"). Those are
kept in clustering, since they are usually the wallet's own deposits, but each cluster
also reports its size without them so a reader can see how much depends on them.

All results are heuristic estimates, not proof of ownership or identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from btc_trace.heuristics import (
    detect_change,
    input_addresses,
    looks_like_coinjoin,
    output_address,
    to_sats,
)
from btc_trace.rpc import NodeClient

DEFAULT_WORKERS = 4  # parallel getblock requests; Bitcoin Core serves several at once
SAMPLE = 10  # outside input addresses kept per transaction in the report

DISCLAIMER = (
    "Heuristic estimates only. Common-input ownership and change detection can be wrong, "
    "and an address appearing here is not proof of ownership, identity, or wrongdoing."
)


def to_date(timestamp: int | None) -> str | None:
    """Block time (Unix seconds) as a UTC calendar date, e.g. "2020-02-05"."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


@dataclass
class TxInfo:
    """One examined transaction, stored once and referenced by txid from hops and stops."""

    height: int
    date: str | None
    input_addresses: int  # distinct input addresses
    outside_inputs: int  # inputs from addresses outside the trace at that point
    outside_inputs_sample: list[str]  # the first few, for reference
    outputs: int
    batch_sweep: bool  # many outside inputs and more than two outputs


@dataclass
class Hop:
    depth: int
    height: int
    date: str | None
    txid: str
    from_addresses: list[str]
    vout: int
    to_address: str
    value_btc: float
    label: str  # "change (heuristic)", "payment (heuristic)" or "undetermined"
    reasons: list[str]  # the evidence behind the label
    followed: bool


CHANGE = "change (heuristic)"
PAYMENT = "payment (heuristic)"
UNDETERMINED = "undetermined"


def label_outputs(tx: dict[str, Any]) -> dict[int, tuple[str, list[str]]]:
    """Label and reasons for every output of a (non-CoinJoin) transaction."""
    outputs = tx.get("vout", [])
    change = detect_change(tx)
    if change is not None:
        labels = {}
        for out in outputs:
            if out["n"] == change.vout:
                labels[out["n"]] = (CHANGE, list(change.reasons))
            else:
                labels[out["n"]] = (PAYMENT, [f"output {change.vout} was identified as change"])
        return labels

    if len(outputs) == 1:
        why = "single output: every coin moved to one address (a payment or a self-transfer)"
    elif len(outputs) == 2:
        why = "no change signal, or the signals disagree"
    else:
        why = f"{len(outputs)} outputs; change detection only handles two-output transactions"
    return {out["n"]: (UNDETERMINED, [why]) for out in outputs}


@dataclass
class CoinJoinStop:
    depth: int
    height: int
    date: str | None
    txid: str
    from_addresses: list[str]
    traced_value_btc: float = 0.0  # value the traced addresses put into the CoinJoin


INTERNAL_SWEEP = "internal consolidation (heuristic)"
SERVICE_SWEEP = "service consolidation (heuristic)"


@dataclass
class ServiceStop:
    """A sweep of many outside inputs into one or two outputs; not followed further.

    ``classification`` is INTERNAL_SWEEP when the destination is in the spender's own
    cluster (the funds stayed put), otherwise SERVICE_SWEEP (likely entered a service).
    """

    depth: int
    height: int
    date: str | None
    txid: str
    from_addresses: list[str]
    input_addresses: int  # distinct input addresses in the transaction
    outside_inputs: int  # of those, addresses not in the trace
    outputs: list[dict[str, Any]]  # [{"address", "value_btc"}]
    traced_value_btc: float  # value the traced addresses put into the transaction
    classification: str = SERVICE_SWEEP


@dataclass
class Evidence:
    kind: str  # "common input" or "change"
    txid: str
    height: int
    batch_sweep: bool = False


@dataclass
class Cluster:
    addresses: list[str]
    seeds: list[str]  # seed addresses inside this cluster
    evidence: list[Evidence]
    batch_links: int  # evidence entries that come from batch sweeps
    size_without_batch_sweeps: int  # largest seed-holding part if batch sweeps are ignored
    outflow: Outflow | None = None  # filled for clusters whose addresses spent in the trace


@dataclass
class Outflow:
    """Value that left a cluster, in BTC, as a range.

    The *full* figures treat the whole cluster as one owner (batch-sweep links
    included), so less counts as leaving: a lower bound. The *core* figures use only
    the part held together without batch sweeps, so more counts as leaving: an upper
    bound. Outputs whose owner the heuristics could not place (e.g. undetected change
    to a new address) count as leaving in both, so both lean high in that respect.
    """

    to_outside_addresses: float = 0.0  # spend outputs to addresses outside the cluster
    to_outside_addresses_core: float = 0.0
    into_service_sweeps: float = 0.0  # traced value swept into a destination outside
    into_service_sweeps_core: float = 0.0
    into_coinjoins: float = 0.0  # traced value entering likely CoinJoins
    to_other_seed_clusters: float = 0.0  # part of to_outside that reached another seed's cluster
    spending_txs: int = 0

    @property
    def total(self) -> float:
        return self.to_outside_addresses + self.into_service_sweeps + self.into_coinjoins

    @property
    def total_core(self) -> float:
        return self.to_outside_addresses_core + self.into_service_sweeps_core + self.into_coinjoins

    def to_dict(self) -> dict[str, float | int]:
        data = asdict(self)
        data["total"] = round(self.total, 8)
        data["total_core"] = round(self.total_core, 8)
        return data


@dataclass
class SeedSummary:
    address: str
    sdn_ref: str | None
    sdn_name: str | None
    cluster: int | None  # 1-based index into clusters
    cluster_size: int
    spending_txs: int
    spent_btc: float  # gross: value of this address's coins spent in those transactions
    returned_btc: float  # outputs of those same transactions paid back to this address
    net_out_btc: float  # spent minus returned: what actually left the address
    first_spend: str | None  # date
    last_spend: str | None
    first_spend_height: int | None
    last_spend_height: int | None


@dataclass
class LevelStats:
    depth: int
    addresses: int  # frontier size at this level
    scanned_from_height: int
    candidate_blocks: int  # blocks the filter index matched (may include false positives)
    blocks_touching: int  # blocks where a frontier address really received or spent
    spending_txs: int  # transactions that spent from a frontier address


@dataclass
class TraceResult:
    seeds: list[str]
    parameters: dict[str, Any]
    scanned_to_height: int
    seed_info: dict[str, dict[str, str]] = field(default_factory=dict)  # address -> SDN entry
    seed_summaries: list[SeedSummary] = field(default_factory=list)
    outflow_total: dict[str, float | int] = field(default_factory=dict)  # sum over seed clusters
    # Per SDN entry: all its seeds' clusters treated as one owner. See _measure_entities.
    entities: list[dict[str, Any]] = field(default_factory=list)
    levels: list[LevelStats] = field(default_factory=list)
    transactions: dict[str, TxInfo] = field(default_factory=dict)
    hops: list[Hop] = field(default_factory=list)
    coinjoin_stops: list[CoinJoinStop] = field(default_factory=list)
    service_stops: list[ServiceStop] = field(default_factory=list)
    clusters: list[Cluster] = field(default_factory=list)
    unexplored: list[str] = field(default_factory=list)
    truncated: bool = False
    note: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for cluster, raw in zip(self.clusters, data["clusters"], strict=True):
            raw["outflow"] = cluster.outflow.to_dict() if cluster.outflow else None
        return data


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, a: str) -> str:
        self.parent.setdefault(a, a)
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:  # path compression
            self.parent[a], a = root, self.parent[a]
        return root

    def union(self, addresses: list[str]) -> None:
        if len(addresses) < 2:
            return
        first = self.find(addresses[0])
        for other in addresses[1:]:
            root = self.find(other)
            if root != first:
                self.parent[root] = first

    def same(self, a: str, b: str) -> bool:
        if a == b:
            return True
        if a not in self.parent or b not in self.parent:
            return False
        return self.find(a) == self.find(b)

    def group_sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for a in self.parent:
            root = self.find(a)
            sizes[root] = sizes.get(root, 0) + 1
        return sizes


class _Clusterer:
    """Clusters with evidence, plus a second view that ignores batch-sweep links."""

    def __init__(self) -> None:
        self.all = _UnionFind()
        self.core = _UnionFind()  # same links minus batch sweeps
        self.links: list[tuple[str, Evidence]] = []  # (an address in the group, evidence)

    def link(self, addresses: list[str], evidence: Evidence) -> None:
        if len(addresses) < 2:
            return
        self.all.union(addresses)
        if not evidence.batch_sweep:
            self.core.union(addresses)
        self.links.append((addresses[0], evidence))

    def clusters(self, seeds: list[str]) -> list[Cluster]:
        for seed in seeds:
            self.all.find(seed)
        groups: dict[str, list[str]] = {}
        for a in list(self.all.parent):
            groups.setdefault(self.all.find(a), []).append(a)
        evidence_by_root: dict[str, list[Evidence]] = {}
        for a, e in self.links:
            evidence_by_root.setdefault(self.all.find(a), []).append(e)
        core_sizes = self.core.group_sizes()

        def core_size(address: str) -> int:
            if address not in self.core.parent:
                return 1
            return core_sizes[self.core.find(address)]

        seed_set = set(seeds)
        out = []
        for root, members in groups.items():
            in_cluster = sorted(set(members) & seed_set)
            if len(members) < 2 and not in_cluster:
                continue
            evidence = evidence_by_root.get(root, [])
            anchors = in_cluster or members
            out.append(
                Cluster(
                    addresses=sorted(members),
                    seeds=in_cluster,
                    evidence=evidence,
                    batch_links=sum(e.batch_sweep for e in evidence),
                    size_without_batch_sweeps=max(core_size(a) for a in anchors),
                )
            )
        # Clusters holding seeds first, then the largest.
        out.sort(key=lambda c: (not c.seeds, -len(c.addresses), c.addresses))
        return out


def looks_like_service_consolidation(
    tx: dict[str, Any], traced: set[str], min_outside_inputs: int
) -> bool:
    """Many inputs from outside the trace swept into one or two outputs."""
    if min_outside_inputs <= 0 or len(tx.get("vout", [])) > 2:
        return False
    outside = {a for a in input_addresses(tx) if a not in traced}
    return len(outside) >= min_outside_inputs


Progress = Callable[[str], None]
# bar(label, done, total, detail): called as work advances so a caller can draw a bar.
Bar = Callable[[str, int, int, str], None]


def fetch_blocks(client: NodeClient, hashes: list[str], workers: int) -> Iterator[dict[str, Any]]:
    """Fetch blocks in order, up to ``workers`` requests at a time."""
    if workers <= 1:
        for blockhash in hashes:
            yield client.get_block(blockhash)
        return
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        # Keep a bounded window in flight so memory stays flat on long scans.
        window = workers * 2
        pending = [pool.submit(client.get_block, h) for h in hashes[:window]]
        for i in range(len(hashes)):
            block = pending[i].result()
            if i + window < len(hashes):
                pending.append(pool.submit(client.get_block, hashes[i + window]))
            pending[i] = None  # release the finished block
            yield block
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _touches(tx: dict[str, Any], addresses: dict[str, int]) -> bool:
    if any(a in addresses for a in input_addresses(tx)):
        return True
    return any(output_address(v) in addresses for v in tx.get("vout", []))


class LevelCache:
    """Saves one level's scan and per-block results so an interrupted trace can resume.

    Files are keyed by the level's addresses and starting heights, so a rerun of the
    same trace (or a deeper one that shares its first levels) reuses them. Each block
    is recorded as soon as it is processed, and only the transactions that spend from
    the level's addresses are kept, which keeps the files small. Everything stored is
    public blockchain data.
    """

    def __init__(self, directory: Path, frontier: dict[str, int]) -> None:
        key = hashlib.sha256(json.dumps(sorted(frontier.items())).encode()).hexdigest()[:16]
        directory.mkdir(parents=True, exist_ok=True)
        self.scan_path = directory / f"{key}.scan.json"
        self.blocks_path = directory / f"{key}.blocks.jsonl"

    def load_scan(self) -> tuple[int, list[str]] | None:
        try:
            data = json.loads(self.scan_path.read_text())
            return int(data["stop_height"]), list(data["blocks"])
        except (OSError, ValueError, KeyError):
            return None

    def save_scan(self, stop_height: int, blocks: list[str]) -> None:
        tmp = self.scan_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stop_height": stop_height, "blocks": blocks}))
        os.replace(tmp, self.scan_path)

    def load_blocks(self) -> dict[str, dict[str, Any]]:
        done: dict[str, dict[str, Any]] = {}
        try:
            with self.blocks_path.open() as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        done[record["hash"]] = record
                    except (ValueError, KeyError):
                        continue  # a line cut off by an interruption
        except OSError:
            pass
        return done

    def append_block(self, record: dict[str, Any]) -> None:
        with self.blocks_path.open("a") as f:
            f.write(json.dumps(record) + "\n")


def summarize_block(block: dict[str, Any], frontier: dict[str, int]) -> dict[str, Any]:
    """The parts of a block the tracer needs: does it touch the frontier, and its spends."""
    touches = False
    spends = []
    for tx in block["tx"]:
        if not _touches(tx, frontier):
            continue
        touches = True
        if any(a in frontier for a in input_addresses(tx)):
            spends.append(tx)
    return {
        "hash": block["hash"],
        "height": block["height"],
        "time": block.get("time"),
        "touches": touches,
        "spends": spends,
    }


def _fill_missing_times(
    client: NodeClient,
    records: list[dict[str, Any]],
    cache: LevelCache | None,
    workers: int,
    draw: Bar,
) -> None:
    """Add block times to saved records from older runs, using cheap header lookups."""
    if not records:
        return
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(client.call, "getblockheader", r["hash"]) for r in records]
        for i, (record, future) in enumerate(zip(records, futures, strict=True), 1):
            record["time"] = future.result()["time"]
            if cache:
                cache.append_block(record)  # the newer line replaces the old one on load
            draw("dating", i, len(records), f"{i:,} of {len(records):,} blocks")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def spends_from(
    client: NodeClient,
    frontier: dict[str, int],
    stop_height: int,
    progress: Progress | None = None,
    workers: int = DEFAULT_WORKERS,
    cache_dir: Path | None = None,
    bar: Bar | None = None,
) -> tuple[list[tuple[int, int | None, dict[str, Any]]], int, int]:
    """Transactions spending from any frontier address, oldest first.

    Returns ((height, block time, tx) triples, candidate blocks from the filter index,
    blocks that really touch the frontier).
    """
    if not frontier:
        return [], 0, 0
    say = progress or (lambda _msg: None)
    draw = bar or (lambda _label, _done, _total, _detail: None)
    start = min(frontier.values())
    cache = LevelCache(cache_dir, frontier) if cache_dir else None

    def scan(low: int) -> list[str]:
        span = stop_height - low + 1

        def chunk_started(lo: int, _hi: int) -> None:
            draw("scanning", lo - low, span, f"block {lo:,} of {stop_height:,}")

        hashes = client.scan_blocks(sorted(frontier), low, stop_height, progress=chunk_started)
        draw("scanning", span, span, f"block {stop_height:,} of {stop_height:,}")
        return hashes

    cached_scan = cache.load_scan() if cache else None
    if cached_scan and cached_scan[0] >= stop_height:
        blockhashes = cached_scan[1]
        say(f"  reusing saved scan to block {cached_scan[0]}")
    elif cached_scan:
        say(f"  reusing saved scan to block {cached_scan[0]}; scanning newer blocks")
        blockhashes = cached_scan[1] + scan(cached_scan[0] + 1)
    else:
        blockhashes = scan(start)
    if cache:
        cache.save_scan(max(stop_height, cached_scan[0] if cached_scan else 0), blockhashes)

    done = cache.load_blocks() if cache else {}
    undated = [r for h in blockhashes if (r := done.get(h)) and r["spends"] and not r.get("time")]
    if undated:
        say(f"  adding dates to {len(undated)} saved block(s)")
        _fill_missing_times(client, undated, cache, workers, draw)

    todo = [h for h in blockhashes if h not in done]
    if done:
        say(
            f"  {len(blockhashes)} candidate block(s); {len(blockhashes) - len(todo)} already "
            f"done, fetching {len(todo)} ({workers} at a time)"
        )
    else:
        say(f"  {len(blockhashes)} candidate block(s); fetching them ({workers} at a time)")

    fetched = fetch_blocks(client, todo, workers)
    found = []
    touching = 0
    count = 0
    if todo:
        draw("fetching", 0, len(todo), f"0 of {len(todo):,} blocks")
    for blockhash in blockhashes:
        record = done.get(blockhash)
        if record is None:
            record = summarize_block(next(fetched), frontier)
            if cache:
                cache.append_block(record)
            count += 1
            draw("fetching", count, len(todo), f"{count:,} of {len(todo):,} blocks")
        touching += record["touches"]
        height = record["height"]
        for tx in record["spends"]:
            spenders = {a for a in input_addresses(tx) if a in frontier}
            # Ignore spends from before the address entered the trace.
            if spenders and height >= min(frontier[a] for a in spenders):
                found.append((height, record.get("time"), tx))
    found.sort(key=lambda item: item[0])
    return found, len(blockhashes), touching


def _input_value_by_address(tx: dict[str, Any]) -> dict[str, int]:
    """Satoshis each input address put into the transaction."""
    totals: dict[str, int] = {}
    for vin in tx.get("vin", []):
        prevout = vin.get("prevout", {})
        address = prevout.get("scriptPubKey", {}).get("address")
        if address:
            totals[address] = totals.get(address, 0) + to_sats(prevout.get("value", 0))
    return totals


def trace(
    client: NodeClient,
    seeds: list[str],
    *,
    max_depth: int = 2,
    max_addresses: int = 100,
    min_value_btc: float = 0.0,
    start_height: int = 0,
    service_min_inputs: int = 20,
    seed_info: dict[str, dict[str, str]] | None = None,
    progress: Progress | None = None,
    workers: int = DEFAULT_WORKERS,
    cache_dir: Path | None = None,
    bar: Bar | None = None,
) -> TraceResult:
    """Follow funds forward from the seeds.

    ``service_min_inputs``: a transaction with at least this many input addresses from
    outside the trace is a sweep. With at most two outputs it ends the branch (a service
    stop); with more outputs it is a batch sweep, followed but flagged in clustering.
    0 disables both checks.
    ``seed_info``: optional SDN attribution per seed, copied into the report.
    """
    stop_height = client.tip_height()
    result = TraceResult(
        seeds=sorted(set(seeds)),
        parameters={
            "max_depth": max_depth,
            "max_addresses": max_addresses,
            "min_value_btc": min_value_btc,
            "start_height": start_height,
            "service_min_inputs": service_min_inputs,
            "workers": workers,
        },
        scanned_to_height=stop_height,
    )
    seeds_set = set(result.seeds)
    result.seed_info = {a: dict(v) for a, v in (seed_info or {}).items() if a in seeds_set}
    clusterer = _Clusterer()
    min_sats = to_sats(min_value_btc)
    visited: set[str] = set(result.seeds)
    frontier: dict[str, int] = dict.fromkeys(result.seeds, start_height)
    seen_txids: set[str] = set()
    # per-seed: [spending txs, sats spent, sats returned, first (height, time), last]
    seed_activity: dict[str, list[Any]] = {a: [0, 0, 0, None, None] for a in result.seeds}

    for depth in range(1, max_depth + 1):
        next_frontier: dict[str, int] = {}
        if progress:
            progress(f"depth {depth}: {len(frontier)} address(es)")
        spends, candidates, blocks_touching = spends_from(
            client, frontier, stop_height, progress, workers, cache_dir, bar
        )
        result.levels.append(
            LevelStats(
                depth=depth,
                addresses=len(frontier),
                scanned_from_height=min(frontier.values()),
                candidate_blocks=candidates,
                blocks_touching=blocks_touching,
                spending_txs=len(spends),
            )
        )
        for height, block_time, tx in spends:
            txid = tx["txid"]
            if txid in seen_txids:
                continue
            seen_txids.add(txid)
            date = to_date(block_time)
            spenders = sorted({a for a in input_addresses(tx) if a in frontier})
            all_inputs = list(dict.fromkeys(input_addresses(tx)))
            outside = [a for a in all_inputs if a not in visited]
            n_outputs = len(tx.get("vout", []))
            is_sweep = service_min_inputs > 0 and len(outside) >= service_min_inputs
            result.transactions[txid] = TxInfo(
                height=height,
                date=date,
                input_addresses=len(all_inputs),
                outside_inputs=len(outside),
                outside_inputs_sample=outside[:SAMPLE],
                outputs=n_outputs,
                batch_sweep=is_sweep and n_outputs > 2,
            )
            values = _input_value_by_address(tx)
            for seed in spenders:
                if seed in seed_activity:
                    activity = seed_activity[seed]
                    activity[0] += 1
                    activity[1] += values.get(seed, 0)
                    # Change sent back to the same address is not money leaving it.
                    activity[2] += sum(
                        to_sats(o["value"]) for o in tx.get("vout", []) if output_address(o) == seed
                    )
                    activity[3] = activity[3] or (height, block_time)
                    activity[4] = (height, block_time)

            if looks_like_coinjoin(tx):
                traced = sum(values.get(a, 0) for a in spenders) / 100_000_000
                result.coinjoin_stops.append(
                    CoinJoinStop(depth, height, date, txid, spenders, traced)
                )
                continue

            if is_sweep and n_outputs <= 2:
                traced_sats = sum(values.get(a, 0) for a in spenders)
                result.service_stops.append(
                    ServiceStop(
                        depth=depth,
                        height=height,
                        date=date,
                        txid=txid,
                        from_addresses=spenders,
                        input_addresses=len(all_inputs),
                        outside_inputs=len(outside),
                        outputs=[
                            {"address": output_address(o), "value_btc": o["value"]}
                            for o in tx.get("vout", [])
                        ],
                        traced_value_btc=traced_sats / 100_000_000,
                    )
                )
                continue

            labels = label_outputs(tx)
            batch = result.transactions[txid].batch_sweep
            clusterer.link(all_inputs, Evidence("common input", txid, height, batch))
            for n, (label, _) in labels.items():
                change_address = output_address(tx["vout"][n]) if label == CHANGE else None
                if change_address and spenders:
                    clusterer.link(
                        [spenders[0], change_address], Evidence("change", txid, height, batch)
                    )
            for out in tx.get("vout", []):
                address = output_address(out)
                if address is None:  # OP_RETURN or non-standard output
                    continue
                label, reasons = labels[out["n"]]
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
                        date=date,
                        txid=txid,
                        from_addresses=spenders,
                        vout=out["n"],
                        to_address=address,
                        value_btc=out["value"],
                        label=label,
                        reasons=reasons,
                        followed=follow,
                    )
                )
        frontier = next_frontier
        if not frontier:
            break

    # Addresses reached at the last level whose own spends were not examined.
    result.unexplored = sorted(frontier)
    result.clusters = clusterer.clusters(result.seeds)

    # A sweep into the spender's own cluster kept the funds with the same owner.
    for stop in result.service_stops:
        if any(
            clusterer.all.same(src, out["address"])
            for src in stop.from_addresses
            for out in stop.outputs
            if out["address"]
        ):
            stop.classification = INTERNAL_SWEEP

    _measure_outflow(result, clusterer)
    _measure_entities(result, clusterer)

    cluster_of = {a: i for i, c in enumerate(result.clusters, 1) for a in c.seeds}
    for seed in result.seeds:
        count, sats, returned, first, last = seed_activity[seed]
        info = result.seed_info.get(seed, {})
        index = cluster_of.get(seed)
        result.seed_summaries.append(
            SeedSummary(
                address=seed,
                sdn_ref=info.get("sdn_ref"),
                sdn_name=info.get("sdn_name"),
                cluster=index,
                cluster_size=len(result.clusters[index - 1].addresses) if index else 1,
                spending_txs=count,
                spent_btc=sats / 100_000_000,
                returned_btc=returned / 100_000_000,
                net_out_btc=(sats - returned) / 100_000_000,
                first_spend=to_date(first[1]) if first else None,
                last_spend=to_date(last[1]) if last else None,
                first_spend_height=first[0] if first else None,
                last_spend_height=last[0] if last else None,
            )
        )
    result.seed_summaries.sort(key=lambda s: -s.net_out_btc)
    return result


def _measure_outflow(result: TraceResult, clusterer: _Clusterer) -> None:
    """Fill ``Cluster.outflow`` and ``TraceResult.outflow_total``.

    Every spend recorded in the trace is credited to the cluster of the address that
    spent it; outputs staying inside that cluster are not outflow.
    """
    full, core = clusterer.all, clusterer.core
    index_of_root = {full.find(c.addresses[0]): i for i, c in enumerate(result.clusters)}
    seed_roots = {full.find(c.addresses[0]) for c in result.clusters if c.seeds}
    sats: list[dict[str, int]] = [{} for _ in result.clusters]
    txs: list[set[str]] = [set() for _ in result.clusters]

    def cluster_index(address: str) -> int | None:
        if address not in full.parent:
            return None
        return index_of_root.get(full.find(address))

    def add(i: int, key: str, amount: int) -> None:
        sats[i][key] = sats[i].get(key, 0) + amount

    for hop in result.hops:
        source = hop.from_addresses[0] if hop.from_addresses else None
        i = cluster_index(source) if source else None
        if i is None:
            continue
        txs[i].add(hop.txid)
        amount = to_sats(hop.value_btc)
        if not full.same(source, hop.to_address):
            add(i, "to_outside_addresses", amount)
            if hop.to_address in full.parent and full.find(hop.to_address) in seed_roots:
                add(i, "to_other_seed_clusters", amount)
        if not core.same(source, hop.to_address):
            add(i, "to_outside_addresses_core", amount)

    for stop in result.service_stops:
        i = cluster_index(stop.from_addresses[0]) if stop.from_addresses else None
        if i is None:
            continue
        txs[i].add(stop.txid)
        amount = to_sats(stop.traced_value_btc)
        destinations = [o["address"] for o in stop.outputs if o["address"]]
        if stop.classification != INTERNAL_SWEEP:
            add(i, "into_service_sweeps", amount)
        if not any(core.same(src, d) for src in stop.from_addresses for d in destinations):
            add(i, "into_service_sweeps_core", amount)

    for stop in result.coinjoin_stops:
        i = cluster_index(stop.from_addresses[0]) if stop.from_addresses else None
        if i is None:
            continue
        txs[i].add(stop.txid)
        add(i, "into_coinjoins", to_sats(stop.traced_value_btc))

    total: dict[str, float | int] = {}
    for i, cluster in enumerate(result.clusters):
        if not txs[i]:
            continue
        cluster.outflow = Outflow(
            **{key: value / 100_000_000 for key, value in sats[i].items()},
            spending_txs=len(txs[i]),
        )
        if cluster.seeds:
            for key, value in cluster.outflow.to_dict().items():
                total[key] = round(total.get(key, 0) + value, 8)
    result.outflow_total = total


def _measure_entities(result: TraceResult, clusterer: _Clusterer) -> None:
    """Value leaving all of one SDN entry's clusters together.

    Summing per-cluster outflow double-counts money that moves from one of an entry's
    clusters to another. Here every cluster holding one of the entry's seeds counts as
    "inside", so those transfers drop out. Seeds without SDN information form one group.
    As with clusters, the low figure uses whole clusters and the high figure only the
    parts held together without batch sweeps (plus the seeds' own core parts).
    """
    groups: dict[str | None, list[str]] = {}
    for seed in result.seeds:
        groups.setdefault(result.seed_info.get(seed, {}).get("sdn_ref"), []).append(seed)

    for sdn_ref, seeds in groups.items():
        result.entities.append(_entity_outflow(result, clusterer, sdn_ref, seeds))


def _entity_outflow(
    result: TraceResult, clusterer: _Clusterer, sdn_ref: str | None, seeds: list[str]
) -> dict[str, Any]:
    """Outflow for one SDN entry's seeds; see _measure_entities."""
    full, core = clusterer.all, clusterer.core
    seed_set = set(seeds)
    full_roots = {full.find(s) for s in seeds}
    core_roots = {core.find(s) for s in seeds if s in core.parent}

    def in_entity(address: str | None) -> bool:
        return bool(address) and address in full.parent and full.find(address) in full_roots

    def in_core(source: str, address: str | None) -> bool:
        if not address:
            return False
        if address in seed_set or core.same(source, address):
            return True
        return address in core.parent and core.find(address) in core_roots

    sats: dict[str, int] = {}
    months: dict[str, list[int]] = {}
    txs: set[str] = set()

    def add(key: str, amount: int, date: str | None, low: bool, high: bool) -> None:
        month = (date or "unknown")[:7]
        bucket = months.setdefault(month, [0, 0])
        if low:
            sats[key] = sats.get(key, 0) + amount
            bucket[0] += amount
        if high:
            sats[key + "_core"] = sats.get(key + "_core", 0) + amount
            bucket[1] += amount

    for hop in result.hops:
        source = hop.from_addresses[0] if hop.from_addresses else None
        if not in_entity(source):
            continue
        txs.add(hop.txid)
        add(
            "to_outside_addresses",
            to_sats(hop.value_btc),
            hop.date,
            low=not in_entity(hop.to_address),
            high=not in_core(source, hop.to_address),
        )
    for stop in result.service_stops:
        source = stop.from_addresses[0] if stop.from_addresses else None
        if not in_entity(source):
            continue
        txs.add(stop.txid)
        destinations = [o["address"] for o in stop.outputs]
        add(
            "into_service_sweeps",
            to_sats(stop.traced_value_btc),
            stop.date,
            low=not any(in_entity(d) for d in destinations),
            high=not any(in_core(source, d) for d in destinations),
        )
    for stop in result.coinjoin_stops:
        source = stop.from_addresses[0] if stop.from_addresses else None
        if not in_entity(source):
            continue
        txs.add(stop.txid)
        # CoinJoin value counts at both ends of the range (one total, not a low/high pair).
        add("into_coinjoins", to_sats(stop.traced_value_btc), stop.date, True, True)

    outflow = Outflow(
        **{key: value / 100_000_000 for key, value in sats.items() if key != "into_coinjoins_core"},
        spending_txs=len(txs),
    )
    info = result.seed_info.get(seeds[0], {}) if sdn_ref else {}
    clusters = [c for c in result.clusters if seed_set & set(c.seeds)]
    return {
        "sdn_ref": sdn_ref,
        "sdn_name": info.get("sdn_name"),
        "seeds": len(seeds),
        "clusters": len(clusters),
        "cluster_addresses": sum(len(c.addresses) for c in clusters),
        "outflow": outflow.to_dict(),
        # month -> [low, high] BTC, for charts of when value left
        "outflow_by_month": {
            month: [round(low / 100_000_000, 8), round(high / 100_000_000, 8)]
            for month, (low, high) in sorted(months.items())
        },
    }
