"""Command-line entry point: ``btc-trace``."""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from btc_trace import __version__
from btc_trace.heuristics import detect_change, input_addresses, looks_like_coinjoin
from btc_trace.ofac import extract_addresses
from btc_trace.progress import ProgressDisplay
from btc_trace.report import render
from btc_trace.rpc import NodeClient, RpcError
from btc_trace.schema import validate_report
from btc_trace.trace import DEFAULT_WORKERS, trace


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


def _cmd_trace(args: argparse.Namespace) -> int:
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

    seed_info: dict[str, dict[str, str]] = {}
    for e in entries:
        if e.get("sdn_ref"):
            seed_info.setdefault(e["address"], {"sdn_ref": e["sdn_ref"]})
            if e.get("sdn_name"):
                seed_info[e["address"]]["sdn_name"] = e["sdn_name"]

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
    print(f"{args.report}: valid trace report (version {report['report_version']})")
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
    status = NodeClient.from_env().scan_status()
    if not status:
        print("no block scan is running")
    else:
        print(
            f"scan running: {status.get('progress')}% done, at block {status.get('current_height')}"
        )
    return 0


def _cmd_scan_abort(args: argparse.Namespace) -> int:
    stopped = NodeClient.from_env().abort_scan()
    print("stopped the running block scan" if stopped else "no block scan was running")
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

    sh = sub.add_parser("show", help="print a saved trace report as readable hops")
    sh.add_argument("report", type=Path, help="JSON report written by trace --out")
    sh.add_argument("--seeds", type=int, default=15, help="seed rows to print (default: 15)")
    sh.add_argument("--clusters", type=int, default=10, help="clusters to print (default: 10)")
    sh.add_argument(
        "--hops", type=int, help="hops to print (default: all if 50 or fewer, else none)"
    )
    sh.set_defaults(func=_cmd_show)

    va = sub.add_parser("validate", help="check a saved trace report against the JSON Schema")
    va.add_argument("report", type=Path, help="JSON report written by trace --out")
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

    st = sub.add_parser("scan-status", help="show progress of a block scan on the node")
    st.set_defaults(func=_cmd_scan_status)
    ab = sub.add_parser("scan-abort", help="stop a block scan running on the node")
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
