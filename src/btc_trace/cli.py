"""Command-line entry point: ``btc-trace``."""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np

from btc_trace import __version__
from btc_trace.exposure import exposure
from btc_trace.heuristics import detect_change, input_addresses, looks_like_coinjoin
from btc_trace.ofac import extract_addresses
from btc_trace.progress import ProgressDisplay
from btc_trace.report import render
from btc_trace.rpc import NodeClient, RpcError
from btc_trace.schema import report_kind, validate_report
from btc_trace.trace import DEFAULT_WORKERS, to_date, trace
from btc_trace.utxoset import (
    BIN_SIZE,
    SnapshotError,
    build_report,
    hash_targets,
    read_header,
    read_snapshot,
)


def _cmd_node(args: argparse.Namespace) -> int:
    client = NodeClient.from_env(args.record)
    print(json.dumps(client.summary(), indent=2))
    return 0


def _cmd_ofac(args: argparse.Namespace) -> int:
    result = extract_addresses(args.xml, args.ticker)
    if args.json:
        print(
            json.dumps(
                {
                    "ticker": result.ticker,
                    "addresses": [
                        {"address": a.address, "sdn_ref": a.sdn_ref, "sdn_name": a.sdn_name}
                        for a in result.addresses
                    ],
                    "rejected": result.rejected,
                },
                indent=2,
            )
        )
    else:
        # One address can appear under several SDN entries; --json keeps each pairing.
        for address in sorted({entry.address for entry in result.addresses}):
            print(address)
    unique = len({entry.address for entry in result.addresses})
    print(
        f"{unique} unique {result.ticker} addresses "
        f"({len(result.addresses)} SDN pairings), {len(result.rejected)} rejected",
        file=sys.stderr,
    )
    return 0


def _cmd_tx(args: argparse.Namespace) -> int:
    client = NodeClient.from_env(args.record)
    tx = client.get_transaction(args.txid)
    change = detect_change(tx)
    report = {
        "txid": tx.get("txid"),
        "input_count": len(tx.get("vin", [])),
        # One entry per address with how many inputs it funded, in first-seen order.
        "input_addresses": [
            {"address": address, "inputs": count}
            for address, count in Counter(input_addresses(tx)).items()
        ],
        "outputs": [
            {
                "n": v["n"],
                "value_btc": v["value"],
                "address": v["scriptPubKey"].get("address"),
                "type": v["scriptPubKey"].get("type"),
            }
            for v in tx.get("vout", [])
        ],
        "likely_coinjoin": looks_like_coinjoin(tx),
        "change_guess": None
        if change is None
        else {"vout": change.vout, "address": change.address, "reasons": change.reasons},
        "note": "Heuristic estimates only; not proof of ownership or identity.",
    }
    print(json.dumps(report, indent=2))
    return 0


def load_seed_entries(path: Path) -> list[dict[str, str]]:
    """Seed entries from `btc-trace ofac --json` output, or one address per line."""
    text = path.read_text()
    if text.lstrip().startswith("{"):
        return list(json.loads(text)["addresses"])
    return [
        {"address": line.strip()}
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def load_seeds(path: Path) -> list[str]:
    return [entry["address"] for entry in load_seed_entries(path)]


def _select_entries(args: argparse.Namespace) -> list[dict[str, str]]:
    """Addresses from the command line plus --seeds, optionally narrowed by --sdn."""
    entries = [{"address": a} for a in args.addresses]
    if args.seeds:
        from_file = load_seed_entries(args.seeds)
        if args.sdn:
            from_file = [e for e in from_file if e.get("sdn_ref") == args.sdn]
            if not from_file:
                raise ValueError(f"no addresses with sdn_ref {args.sdn} in {args.seeds}")
        entries += from_file
    elif args.sdn:
        raise ValueError("--sdn needs --seeds pointing at `btc-trace ofac --json` output")
    if not entries:
        raise ValueError("give at least one address, or --seeds FILE")
    return entries


def _seed_info(entries: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    seed_info: dict[str, dict[str, str]] = {}
    for e in entries:
        if e.get("sdn_ref"):
            seed_info.setdefault(e["address"], {"sdn_ref": e["sdn_ref"]})
            if e.get("sdn_name"):
                seed_info[e["address"]]["sdn_name"] = e["sdn_name"]
    return seed_info


def _cmd_trace(args: argparse.Namespace) -> int:
    entries = _select_entries(args)
    seed_info = _seed_info(entries)
    client = NodeClient.from_env(args.record)
    display = ProgressDisplay()
    try:
        result = _run_trace(args, client, entries, seed_info, display)
    finally:
        display.finish()
    return _report_trace(args, result)


def _run_trace(args, client, entries, seed_info, display):
    return trace(
        client,
        [e["address"] for e in entries],
        max_depth=args.depth,
        max_addresses=args.max_addresses,
        min_value_btc=args.min_btc,
        start_height=args.start_height,
        service_min_inputs=args.service_min_inputs,
        seed_info=seed_info,
        progress=display.message,
        bar=display.bar,
        workers=args.workers,
        cache_dir=None if args.no_cache else args.cache_dir,
    )


def _report_trace(args: argparse.Namespace, result) -> int:
    report = json.dumps(result.to_dict(), indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n")
    else:
        print(report)
    for level in result.levels:
        print(
            f"depth {level.depth}: {level.addresses} address(es) scanned from block "
            f"{level.scanned_from_height}; {level.blocks_touching} block(s) touch them "
            f"(of {level.candidate_blocks} candidates), "
            f"{level.spending_txs} spending transaction(s)",
            file=sys.stderr,
        )
    biggest = max((len(c.addresses) for c in result.clusters if c.seeds), default=0)
    internal = sum(st.classification.startswith("internal") for st in result.service_stops)
    print(
        f"{len(result.hops)} hops, {len(result.coinjoin_stops)} CoinJoin stops, "
        f"{len(result.service_stops)} sweep stops ({internal} internal), "
        f"{len(result.unexplored)} unexplored; largest seed cluster {biggest} address(es)"
        + (" (address limit reached)" if result.truncated else ""),
        file=sys.stderr,
    )
    return 0


def _cmd_exposure(args: argparse.Namespace) -> int:
    entries = _select_entries(args)
    client = NodeClient.from_env(args.record)
    display = ProgressDisplay()
    try:
        result = exposure(
            client,
            [e["address"] for e in entries],
            seed_info=_seed_info(entries),
            workers=args.workers,
            cache_dir=None if args.no_cache else args.cache_dir,
            progress=display.message,
            bar=display.bar,
        )
    finally:
        display.finish()
    report = result.to_dict()
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        _print_exposure(report, rows=args.rows)
    else:
        print(text)
    return 0


def _print_exposure(report: dict, rows: int = 15) -> None:
    """Readable summary of an exposure report."""
    snap = report["snapshot"]
    t = report["totals"]
    print(
        f"UTXO set at block {snap['height']:,} ({snap['date'] or 'date unknown'}): "
        f"{t['addresses']} address(es), {t['funded_addresses']} holding coins"
    )
    print(
        f"  holding        {t['balance_btc']:>16,.8f} BTC\n"
        f"  key visible    {t['exposed_btc']:>16,.8f} BTC  ({t['exposed_addresses']} address(es))\n"
        f"  behind a hash  {t['hash_only_btc']:>16,.8f} BTC"
    )
    funded_types = {k: v for k, v in report["by_type"].items() if v["balance_btc"]}
    if funded_types:
        print("\nBy script type:")
        for label, v in sorted(funded_types.items(), key=lambda kv: -kv[1]["balance_btc"]):
            print(
                f"  {label:<16} {v['balance_btc']:>16,.8f} BTC held, "
                f"{v['exposed_btc']:>16,.8f} BTC with key visible"
            )
    funded_entries = [e for e in report["by_entry"] if e["balance_btc"]]
    if funded_entries:
        print("\nBy SDN entry (held, key visible):")
        for e in funded_entries[:rows]:
            # The name goes last: right-to-left scripts in a name would otherwise
            # reorder the number columns on screen.
            print(
                f"  {e['sdn_ref']:>6}  {e['balance_btc']:>16,.8f}  {e['exposed_btc']:>16,.8f}  "
                f"{_isolate(e['sdn_name'] or '')}"
            )
    funded = [a for a in report["addresses"] if a["balance_btc"]]
    if funded:
        print(
            f"\nAddresses holding coins (largest {min(rows, len(funded))} of {len(funded)}; "
            "address, SDN entry, balance, status):"
        )
        width = max(len(a["address"]) for a in funded[:rows])
        for a in funded[:rows]:
            spend = a["first_spend"]
            why = a["status"]
            if spend:
                why += f" (first spend {spend['date'] or spend['height']})"
            ref = a["sdn_ref"] or "-"
            print(f"  {a['address']:<{width}}  {ref:>6}  {a['balance_btc']:>16,.8f} BTC  {why}")
    print(
        f"\n{report['blocks_checked']:,} of {report['candidate_blocks']:,} candidate block(s) "
        "read to find earlier spends"
    )


def _cmd_dump_utxos(args: argparse.Namespace) -> int:
    client = NodeClient.from_env(args.record)
    tip = client.tip_height()
    name = args.name or f"utxo-{tip}.dat"
    print(
        f"asking the node to write its UTXO set (block {tip:,}) to {name} in its data "
        "directory. This takes several minutes and about 10 GB on the node; Ctrl+C stops "
        "waiting but not the node, which finishes the file anyway.",
        file=sys.stderr,
        flush=True,
    )
    info = client.dump_utxo_set(name)
    info_path = args.info or Path("data") / f"{Path(name).stem}.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps(info, indent=2) + "\n")
    print(
        f"wrote {info['coins_written']:,} coins at block {info['base_height']:,}\n"
        f"  on the node:    {info['path']}\n"
        f"  UTXO set hash:  {info['txoutset_hash']}\n"
        f"  details saved:  {info_path}\n"
        f"Copy the file to data/{Path(name).name} over SSH, then run:\n"
        f"  btc-trace utxo-stats data/{Path(name).name}"
    )
    return 0


def _block_dates(client: NodeClient, heights: list[int], workers: int) -> dict[int, str | None]:
    """UTC dates of the blocks at the given heights."""

    def date_of(height: int) -> str | None:
        return to_date(client.call("getblockheader", client.call("getblockhash", height))["time"])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(zip(heights, pool.map(date_of, heights), strict=True))


REVEAL_METHOD = (
    "Every input in blocks 0 to the snapshot height was read; the public keys, redeem "
    "scripts and witness scripts it revealed were hashed back to the addresses they "
    "unlock (including the same key's other single-key address types), and matched to "
    "hash-based coins by the first 8 bytes of the hash."
)


def _snapshot_info(snapshot: Path) -> tuple[dict | None, str]:
    """The dump-utxos reply saved next to a snapshot, and the snapshot's base hash."""
    info_path = snapshot.with_suffix(".json")
    info = json.loads(info_path.read_text()) if info_path.exists() else None
    with snapshot.open("rb") as f:
        try:
            header = read_header(f.read(51))
        except SnapshotError as exc:
            raise ValueError(f"{snapshot}: {exc}") from exc
    return info, header.base_hash


def _cmd_reveal_scan(args: argparse.Namespace) -> int:
    from btc_trace.rawscan import KeepAliveClient, scan_chain
    from btc_trace.reveal import ripemd160_available

    if not ripemd160_available():
        raise ValueError(
            "this Python's OpenSSL has no RIPEMD-160, which address hashes need; "
            "use the Python that uv installs (`uv python install 3.12`)"
        )
    info, base_hash = _snapshot_info(args.snapshot)
    source = args.source() if getattr(args, "source", None) else None
    client = None if source else KeepAliveClient.from_env()
    if source is not None:
        height = source.height_of(base_hash)
    else:
        height = client.call("getblockheader", [base_hash])["height"]
        client.close()
    if info and info.get("base_height") not in (None, height):
        raise ValueError("the snapshot's saved details disagree with the node about its height")
    stem = args.snapshot.with_suffix("")
    targets_path = Path(f"{stem}.targets.npy")
    out = args.out or Path(f"{stem}.revealed.npy")
    display = ProgressDisplay()

    if not targets_path.exists():
        display.message(f"collecting the address hashes of hash-based coins in {args.snapshot}")

        def reading(done: int, total: int) -> None:
            display.bar("reading", done, total, f"{done:,} of {total:,} coins")

        targets = hash_targets(args.snapshot, progress=reading)
        display.finish()
        tmp = Path(f"{stem}.targets.tmp.npy")
        np.save(tmp, targets)
        os.replace(tmp, targets_path)
        display.message(f"  {targets.size:,} distinct addresses saved to {targets_path}")
    else:
        display.message(f"reusing the address hashes in {targets_path}")

    display.message(
        f"reading blocks 0 to {height:,} from the node ({args.workers} at a time). This "
        "reads the whole chain and can take many hours; progress is saved, so an "
        "interrupted scan picks up where it stopped."
    )

    def scanning(done: int, total: int, size: int, seconds: float) -> None:
        rate = size / seconds / 1e6 if seconds > 0 else 0.0
        gigabytes = size / 1e9
        display.bar(
            "blocks", done, total, f"{done:,} of {total:,}  {gigabytes:,.1f} GB  {rate:,.0f} MB/s"
        )

    try:
        matched, totals = scan_chain(
            height,
            targets_path,
            args.cache_dir / f"reveal-{height}",
            chunk=args.chunk,
            workers=args.workers,
            source=source,
            progress=scanning,
        )
    finally:
        display.finish()
    tmp = Path(f"{out.with_suffix('')}.tmp.npy")
    np.save(tmp, matched)
    os.replace(tmp, out)
    summary = {
        "scan_height": height,
        "base_hash": base_hash,
        "blocks": totals.blocks,
        "inputs": totals.inputs,
        "revealed_hashes": totals.reveals,
        "matched_addresses": int(matched.size),
        "bytes_read": totals.bytes,
    }
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"read {totals.blocks:,} blocks ({totals.bytes / 1e9:,.1f} GB) and {totals.inputs:,} "
        f"inputs: {matched.size:,} hash-based addresses in the snapshot have revealed "
        f"their key or script.\nSaved to {out}. Next:\n"
        f"  btc-trace utxo-stats {args.snapshot} --revealed {out} "
        f"--out reports/{stem.name}.json"
    )
    return 0


def _load_revealed(path: Path, base_hash: str) -> tuple[np.ndarray, dict]:
    summary_path = path.with_suffix(".json")
    if not summary_path.exists():
        raise ValueError(f"{summary_path} is missing; reveal-scan writes it next to {path}")
    summary = json.loads(summary_path.read_text())
    if summary.get("base_hash") != base_hash:
        raise ValueError(f"{path} was made for a different snapshot")
    reuse = {
        k: summary[k]
        for k in ("scan_height", "blocks", "inputs", "revealed_hashes", "matched_addresses")
    }
    reuse["method"] = REVEAL_METHOD
    return np.load(path), reuse


def _cmd_utxo_stats(args: argparse.Namespace) -> int:
    info_path = args.dump_info or args.snapshot.with_suffix(".json")
    expected = json.loads(info_path.read_text()) if info_path.exists() else None
    revealed = reuse = None
    if args.revealed:
        _, base_hash = _snapshot_info(args.snapshot)
        revealed, reuse = _load_revealed(args.revealed, base_hash)
    display = ProgressDisplay()
    verb = "reading and verifying" if not args.no_verify else "reading"
    display.message(f"{verb} {args.snapshot}")

    def progress(done: int, total: int) -> None:
        display.bar("reading", done, total, f"{done:,} of {total:,} coins")

    try:
        stats = read_snapshot(
            args.snapshot, verify=not args.no_verify, progress=progress, revealed=revealed
        )
    except SnapshotError as exc:
        raise ValueError(f"{args.snapshot}: {exc}") from exc
    finally:
        display.finish()

    base_height = expected.get("base_height") if expected else None
    base_date = None
    bin_dates = None
    if not args.no_dates and (os.environ.get("BTC_URL") or os.environ.get("BTC_FIXTURES")):
        client = NodeClient.from_env(args.record)
        header = client.call("getblockheader", stats.header.base_hash)
        base_height, base_date = header["height"], to_date(header["time"])
        starts = sorted({b * BIN_SIZE for bins in stats.bins.values() for b in bins})
        display.message(f"looking up dates for {len(starts)} age bins on the node")
        bin_dates = _block_dates(client, starts, args.workers)
    elif not args.no_dates:
        display.message("no node configured (BTC_URL); age bins will have heights but no dates")

    report = build_report(
        stats,
        base_height=base_height,
        base_date=base_date,
        expected=expected,
        bin_dates=bin_dates,
        reuse=reuse,
    )
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        _print_utxo_report(report)
    else:
        print(text)
    return 0 if _checks_pass(report["checks"]) else 1


def _checks_pass(checks: dict) -> bool:
    return checks["coins_match_header"] and all(
        checks[k] is not False for k in ("hash_matches", "base_hash_matches", "coins_match_node")
    )


def _print_utxo_report(report: dict) -> None:
    snap, t = report["snapshot"], report["totals"]
    reuse = report.get("reuse")
    print(
        f"UTXO set at block {snap['base_height']:,} ({snap['date'] or 'date unknown'}, "
        f"{snap['network']}): {t['coins']:,} coins holding {t['btc']:,.8f} BTC"
    )
    print(f"\n  {'type':<24}{'coins':>14}{'BTC':>22}{'share':>8}  key visible?")
    for label, v in report["by_type"].items():
        if v["key_in_output"]:
            visible = "yes, in output"
        elif v.get("revealed_btc") is not None:
            visible = f"revealed for {v['revealed_btc']:,.8f} BTC"
        elif v["hash_only"]:
            visible = "only if reused"
        else:
            visible = "-"
        print(
            f"  {label:<24}{v['coins']:>14,}{v['btc']:>22,.8f}{v['share_of_supply']:>8.2%}"
            f"  {visible}"
        )
    print(
        f"\n  key in output (P2PK, multisig, Taproot): {t['key_in_output_btc']:>18,.8f} BTC\n"
        f"  behind a hash until spent:               {t['hash_only_btc']:>18,.8f} BTC"
    )
    if t.get("exposed_btc") is not None:
        print(
            f"    of which revealed by an earlier spend: {t['revealed_btc']:>18,.8f} BTC\n"
            f"  key visible in total:                    {t['exposed_btc']:>18,.8f} BTC "
            f"({t['exposed_share']:.2%} of all BTC)"
        )
    print(
        f"  P2PK from coinbase (early mining):       {t['coinbase_p2pk_btc']:>18,.8f} BTC "
        f"in {t['coinbase_p2pk_coins']:,} coins"
    )
    d = report["dormancy"]
    print(
        f"  unmoved {d['years']}+ years (created before block {d['created_before_height']:,}): "
        f"{t['dormant_btc']:,.8f} BTC, of which {t['dormant_key_in_output_btc']:,.8f} BTC "
        "with the key in the output"
    )
    if t.get("dormant_exposed_btc") is not None:
        print(
            f"  unmoved {d['years']}+ years with a visible key (either way): "
            f"{t['dormant_exposed_btc']:,.8f} BTC"
        )
    if reuse:
        print(
            f"  reveal scan: {reuse['blocks']:,} blocks, {reuse['inputs']:,} inputs, "
            f"{reuse['matched_addresses']:,} reused hash-based addresses in this snapshot"
        )
    c = report["checks"]
    marks = {True: "ok", False: "MISMATCH", None: "not checked"}
    print(
        "\nChecks:\n"
        f"  coin count matches the file header:   {marks[c['coins_match_header']]}\n"
        f"  coin count matches the node's dump:   {marks[c['coins_match_node']]}\n"
        f"  base block matches the node's dump:   {marks[c['base_hash_matches']]}\n"
        f"  UTXO set hash matches the node's:     {marks[c['hash_matches']]}"
    )
    if c["computed_txoutset_hash"]:
        print(f"  computed UTXO set hash: {c['computed_txoutset_hash']}")


def _isolate(text: str) -> str:
    """Wrap text in Unicode directional isolates so mixed scripts print in order."""
    return f"\u2068{text}\u2069" if text else text


def _short(addresses: list[str], limit: int = 3) -> str:
    extra = len(addresses) - limit
    return ", ".join(addresses[:limit]) + (f" (+{extra} more)" if extra > 0 else "")


def _when(item: dict) -> str:
    return f"block {item['height']}" + (f" ({item['date']})" if item.get("date") else "")


def _print_hop(hop: dict, transactions: dict) -> None:
    print()
    print(f"[depth {hop['depth']}] {_when(hop)}  tx {hop['txid'][:16]}...  output {hop['vout']}")
    print(f"  {hop['value_btc']:.8f} BTC -> {hop['to_address']}")
    print(f"  from: {', '.join(hop['from_addresses'])}")
    tx = transactions.get(hop["txid"], {})
    if tx.get("outside_inputs"):
        sample = tx["outside_inputs_sample"]
        extra = tx["outside_inputs"] - len(sample[:3])
        shown = ", ".join(sample[:3]) + (f" (+{extra} more)" if extra > 0 else "")
        print(f"  co-spent with: {shown}" + ("  [batch sweep]" if tx.get("batch_sweep") else ""))
    print(f"  label: {hop['label']}" + ("" if hop["followed"] else "  (not followed)"))
    for reason in hop.get("reasons", []):
        print(f"    - {reason}")


def _cmd_show(args: argparse.Namespace) -> int:
    """Print a saved trace report: summaries first, individual hops on request."""
    report = json.loads(args.report.read_text())
    if report_kind(report) == "exposure":
        _print_exposure(report, rows=args.seeds)
        return 0
    if report_kind(report) == "utxo-set":
        _print_utxo_report(report)
        return 0
    transactions = report.get("transactions", {})
    names = {
        (info.get("sdn_ref"), info.get("sdn_name", ""))
        for info in report.get("seed_info", {}).values()
    }
    for ref, name in sorted(names):
        print(f"seeds from SDN entry {ref}: {name or '(name not recorded)'}")
    print(
        f"{len(report['seeds'])} seed address(es), scanned to block {report['scanned_to_height']}"
    )
    for level in report.get("levels", []):
        print(
            f"depth {level['depth']}: {level['addresses']} address(es), "
            f"{level['blocks_touching']} block(s) touching, "
            f"{level['spending_txs']} spending transaction(s)"
        )

    summaries = report.get("seed_summaries", [])
    if summaries:
        active = [s for s in summaries if s["spending_txs"]]
        print(
            f"\nSeeds ({len(active)} of {len(summaries)} ever spent), by net BTC out "
            "(spent minus change returned to the same address):"
        )
        for s in active[: args.seeds]:
            where = f"cluster #{s['cluster']} ({s['cluster_size']:,})" if s["cluster"] else "alone"
            span = f"{s['first_spend'] or s['first_spend_height']} to "
            span += f"{s['last_spend'] or s['last_spend_height']}"
            net = s.get("net_out_btc", s["spent_btc"])
            print(
                f"  {s['address']}  {net:>14,.8f} BTC out in {s['spending_txs']:>5} tx  "
                f"{where:<22} {span}"
            )
        if len(active) > args.seeds:
            print(f"  ... {len(active) - args.seeds} more (use --seeds N)")

    hops = report["hops"]
    labels = collections.Counter(h["label"] for h in hops)
    print(f"\nHops: {len(hops):,}  " + ", ".join(f"{k} {v:,}" for k, v in labels.most_common()))

    total = report.get("outflow_total")
    if total:
        print(
            f"\nValue leaving the seeds' clusters: {total['total']:,.8f} to "
            f"{total['total_core']:,.8f} BTC"
        )
        print("  (low: each cluster counted whole; high: only its core without batch sweeps)")
        print(
            f"  to outside addresses   {total.get('to_outside_addresses', 0):>16,.8f} to "
            f"{total.get('to_outside_addresses_core', 0):,.8f}"
        )
        print(
            f"  into service sweeps    {total.get('into_service_sweeps', 0):>16,.8f} to "
            f"{total.get('into_service_sweeps_core', 0):,.8f}"
        )
        print(f"  into CoinJoins         {total.get('into_coinjoins', 0):>16,.8f}")
        print(
            f"  of which reached another seed's cluster "
            f"{total.get('to_other_seed_clusters', 0):,.8f} (counted again there if it moves on)"
        )

    for entity in report.get("entities", []):
        out = entity["outflow"]
        who = (
            f"SDN entry {entity['sdn_ref']} ({entity['sdn_name'] or 'name not recorded'})"
            if entity["sdn_ref"]
            else "the seeds"
        )
        print(
            f"\nValue leaving {who} as a whole: {out['total']:,.8f} to {out['total_core']:,.8f} BTC"
        )
        print(
            f"  {entity['seeds']} seed(s) in {entity['clusters']} cluster(s); transfers between "
            "those clusters are not counted"
        )

    stops = report.get("service_stops", [])
    if stops:
        print(f"\nSweeps that ended a branch: {len(stops):,}")
        for kind in ("internal consolidation (heuristic)", "service consolidation (heuristic)"):
            group = [s for s in stops if s.get("classification", kind) == kind]
            if not group:
                continue
            btc = sum(s["traced_value_btc"] for s in group)
            print(f"  {kind}: {len(group):,} sweep(s), {btc:,.8f} BTC from traced addresses")
        external = collections.Counter()
        for stop in stops:
            if stop.get("classification") == "internal consolidation (heuristic)":
                continue
            share = stop["traced_value_btc"] / max(len(stop["outputs"]), 1)
            for out in stop["outputs"]:
                external[out["address"]] += share
        if external:
            print("  top destinations outside the spender's cluster (approx. BTC):")
            for address, btc in external.most_common(5):
                print(f"    {address}  {btc:,.8f}")
    coinjoins = report.get("coinjoin_stops", [])
    if coinjoins:
        print(f"\nCoinJoin stops: {len(coinjoins):,}")

    clusters = report.get("clusters", [])
    if clusters:
        print("\nClusters (addresses the heuristics suggest share an owner):")
    for i, cluster in enumerate(clusters[: args.clusters], 1):
        kinds = sorted({e["kind"] for e in cluster["evidence"]})
        seeds = f", {len(cluster['seeds'])} seed(s)" if cluster["seeds"] else ""
        linked = (
            f"linked by {' and '.join(kinds)} in "
            f"{len({e['txid'] for e in cluster['evidence']}):,} transaction(s)"
            if kinds
            else "no links (the seed alone)"
        )
        print(f"  #{i}: {len(cluster['addresses']):,} address(es){seeds}; {linked}")
        if cluster.get("outflow"):
            out = cluster["outflow"]
            print(
                f"      value out: {out['total']:,.8f} to {out['total_core']:,.8f} BTC "
                f"from {out['spending_txs']:,} spending transaction(s)"
            )
        if cluster.get("batch_links"):
            print(
                f"      {cluster['batch_links']:,} link(s) from batch sweeps; "
                f"without them the seed's part is {cluster['size_without_batch_sweeps']:,} "
                "address(es)"
            )
        print(f"      {_short(cluster['addresses'], 4)}")
    if len(clusters) > args.clusters:
        print(f"  ... {len(clusters) - args.clusters} more (use --clusters N)")

    shown = len(hops) if args.hops is None and len(hops) <= 50 else (args.hops or 0)
    for hop in hops[:shown]:
        _print_hop(hop, transactions)
    if shown < len(hops):
        print(f"\n{len(hops) - shown:,} hop(s) not printed (use --hops N)")
    if report.get("unexplored"):
        print(f"\nunexplored: {len(report['unexplored']):,} address(es)")
    print(f"\n{report['note']}")
    return 0


def _parse_mark(text: str) -> tuple[str, str]:
    day, _, label = text.partition("=")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD=label, got {text!r}") from exc
    return day, label or day


def _load_valid_report(path: Path, validate: bool = True) -> dict:
    """Read a trace report, refusing one that does not match the schema."""
    report = json.loads(path.read_text())
    if report_kind(report) != "trace":
        raise ValueError(
            f"{path} is an {report_kind(report)} report; `btc-trace report` renders trace "
            "reports only (use `btc-trace show` for a summary)"
        )
    if not validate:
        return report
    print(f"checking {path} against the report schema...", file=sys.stderr, flush=True)
    problems = validate_report(report)
    if problems:
        raise ValueError(f"{path} is not a valid trace report:\n  " + "\n  ".join(problems))
    return report


def _cmd_validate(args: argparse.Namespace) -> int:
    report = json.loads(args.report.read_text())
    problems = validate_report(report, limit=args.limit)
    if problems:
        print(f"{args.report}: INVALID", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    kind = report_kind(report)
    print(f"{args.report}: valid {kind} report (version {report['report_version']})")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    report = _load_valid_report(args.report, validate=not args.no_validate)
    findings = args.findings.read_text() if args.findings else None
    page = render(report, title=args.title, findings=findings, marks=args.mark)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(f"wrote {args.out} ({len(page) / 1024:,.0f} KB)", file=sys.stderr)
    return 0


def _cmd_scan_status(args: argparse.Namespace) -> int:
    client = NodeClient.from_env()
    blocks = client.scan_status()
    utxos = client.utxo_scan_status()
    if blocks:
        print(
            f"block scan running: {blocks.get('progress')}% done, "
            f"at block {blocks.get('current_height')}"
        )
    if utxos:
        print(f"UTXO set scan running: {utxos.get('progress')}% done")
    if not blocks and not utxos:
        print("no scan is running")
    return 0


def _cmd_scan_abort(args: argparse.Namespace) -> int:
    stopped = NodeClient.from_env().abort_scan()
    print("stopped the running scan" if stopped else "no scan was running")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc-trace",
        description="Heuristic Bitcoin fund tracing from OFAC-sanctioned addresses.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--record",
        type=Path,
        metavar="DIR",
        help="save each node reply as a fixture in DIR (public chain data only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    node = sub.add_parser("node", help="show node version, chain state and indexes")
    node.set_defaults(func=_cmd_node)

    ofac = sub.add_parser("ofac", help="extract sanctioned addresses from SDN_ADVANCED.XML")
    ofac.add_argument("xml", type=Path, help="path to SDN_ADVANCED.XML")
    ofac.add_argument("--ticker", default="XBT", help="currency ticker (default: XBT)")
    ofac.add_argument("--json", action="store_true", help="output JSON with SDN references")
    ofac.set_defaults(func=_cmd_ofac)

    tx = sub.add_parser("tx", help="decode a transaction and apply the heuristics")
    tx.add_argument("txid")
    tx.set_defaults(func=_cmd_tx)

    tr = sub.add_parser("trace", help="follow funds forward from seed addresses")
    tr.add_argument("addresses", nargs="*", help="seed addresses")
    tr.add_argument("--seeds", type=Path, help="seed file: ofac --json output or one per line")
    tr.add_argument("--depth", type=int, default=2, help="hops to follow (default: 2)")
    tr.add_argument(
        "--max-addresses", type=int, default=100, help="stop adding addresses after this many"
    )
    tr.add_argument(
        "--min-btc", type=float, default=0.0, help="do not follow outputs smaller than this"
    )
    tr.add_argument(
        "--start-height",
        type=int,
        default=0,
        help="first block to scan; a later start makes the first scan much faster",
    )
    tr.add_argument(
        "--sdn", metavar="REF", help="with --seeds: only addresses from this SDN entry (sdn_ref)"
    )
    tr.add_argument(
        "--service-min-inputs",
        type=int,
        default=20,
        help="stop at likely service consolidations with this many outside inputs "
        "(default: 20; 0 disables)",
    )
    tr.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("BTC_WORKERS", DEFAULT_WORKERS)),
        help=f"blocks to fetch in parallel (default: {DEFAULT_WORKERS}, or BTC_WORKERS)",
    )
    tr.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".btc_trace_cache"),
        help="where progress is saved so an interrupted trace resumes (default: .btc_trace_cache)",
    )
    tr.add_argument("--no-cache", action="store_true", help="do not save or reuse progress")
    tr.add_argument("--out", type=Path, help="write the JSON report here instead of stdout")
    tr.set_defaults(func=_cmd_trace)

    ex = sub.add_parser(
        "exposure", help="how much of what addresses hold today has a visible public key"
    )
    ex.add_argument("addresses", nargs="*", help="addresses to check")
    ex.add_argument("--seeds", type=Path, help="address file: ofac --json output or one per line")
    ex.add_argument(
        "--sdn", metavar="REF", help="with --seeds: only addresses from this SDN entry (sdn_ref)"
    )
    ex.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("BTC_WORKERS", DEFAULT_WORKERS)),
        help=f"blocks to fetch in parallel (default: {DEFAULT_WORKERS}, or BTC_WORKERS)",
    )
    ex.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".btc_trace_cache"),
        help="where progress is saved so an interrupted run resumes (default: .btc_trace_cache)",
    )
    ex.add_argument("--no-cache", action="store_true", help="do not save or reuse progress")
    ex.add_argument("--rows", type=int, default=15, help="rows to print per table (default: 15)")
    ex.add_argument("--out", type=Path, help="write the JSON report here instead of stdout")
    ex.set_defaults(func=_cmd_exposure)

    sh = sub.add_parser("show", help="print a saved trace report as readable hops")
    sh.add_argument("report", type=Path, help="JSON report written by trace or exposure --out")
    sh.add_argument("--seeds", type=int, default=15, help="seed rows to print (default: 15)")
    sh.add_argument("--clusters", type=int, default=10, help="clusters to print (default: 10)")
    sh.add_argument(
        "--hops", type=int, help="hops to print (default: all if 50 or fewer, else none)"
    )
    sh.set_defaults(func=_cmd_show)

    va = sub.add_parser("validate", help="check a saved report against its JSON Schema")
    va.add_argument("report", type=Path, help="JSON report written by trace or exposure --out")
    va.add_argument("--limit", type=int, default=20, help="problems to list (default: 20)")
    va.set_defaults(func=_cmd_validate)

    rp = sub.add_parser("report", help="render a saved trace report as one HTML page")
    rp.add_argument("report", type=Path, help="JSON report written by trace --out")
    rp.add_argument("--out", type=Path, default=Path("docs/index.html"), help="HTML file to write")
    rp.add_argument("--title", help="page title (default: from the SDN entry)")
    rp.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the schema check (for a report already checked with `validate`)",
    )
    rp.add_argument("--findings", type=Path, help="Markdown file with your findings")
    rp.add_argument(
        "--mark",
        action="append",
        type=_parse_mark,
        default=[],
        metavar="YYYY-MM-DD=LABEL",
        help="a dated reference line on the timeline (repeatable)",
    )
    rp.set_defaults(func=_cmd_report)

    du = sub.add_parser(
        "dump-utxos", help="have the node write its UTXO set to a file (about 10 GB)"
    )
    du.add_argument(
        "--name", help="file name in the node's data directory (default: utxo-HEIGHT.dat)"
    )
    du.add_argument(
        "--info", type=Path, help="where to save the node's reply (default: data/NAME.json)"
    )
    du.set_defaults(func=_cmd_dump_utxos)

    us = sub.add_parser("utxo-stats", help="measure a UTXO snapshot by script type and age")
    us.add_argument("snapshot", type=Path, help="file written by dump-utxos (dumptxoutset)")
    us.add_argument(
        "--dump-info",
        type=Path,
        help="the node's reply saved by dump-utxos (default: SNAPSHOT with .json)",
    )
    us.add_argument("--no-verify", action="store_true", help="skip recomputing the UTXO set hash")
    us.add_argument("--revealed", type=Path, help="result of reveal-scan, to measure address reuse")
    us.add_argument(
        "--no-dates", action="store_true", help="do not look up block dates on the node"
    )
    us.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("BTC_WORKERS", DEFAULT_WORKERS)),
        help=f"parallel date lookups (default: {DEFAULT_WORKERS}, or BTC_WORKERS)",
    )
    us.add_argument("--out", type=Path, help="write the JSON report here instead of stdout")
    us.set_defaults(func=_cmd_utxo_stats)

    rv = sub.add_parser(
        "reveal-scan",
        help="read every block to find addresses whose key or script has been revealed",
    )
    rv.add_argument("snapshot", type=Path, help="file written by dump-utxos (dumptxoutset)")
    rv.add_argument(
        "--workers", type=int, default=4, help="processes reading blocks at once (default: 4)"
    )
    rv.add_argument(
        "--chunk", type=int, default=1000, help="blocks per saved range (default: 1000)"
    )
    rv.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".btc_trace_cache"),
        help="where finished ranges are saved (default: .btc_trace_cache)",
    )
    rv.add_argument("--out", type=Path, help="result file (default: SNAPSHOT.revealed.npy)")
    rv.set_defaults(func=_cmd_reveal_scan)

    st = sub.add_parser("scan-status", help="show progress of a block or UTXO scan on the node")
    st.set_defaults(func=_cmd_scan_status)
    ab = sub.add_parser("scan-abort", help="stop a block or UTXO scan running on the node")
    ab.set_defaults(func=_cmd_scan_abort)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RpcError, ValueError, FileNotFoundError) as exc:
        print(f"btc-trace: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("btc-trace: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
