"""Command-line entry point: ``btc-trace``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from btc_trace import __version__
from btc_trace.heuristics import detect_change, input_addresses, looks_like_coinjoin
from btc_trace.ofac import extract_addresses
from btc_trace.rpc import NodeClient, RpcError
from btc_trace.trace import trace


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
                        {"address": a.address, "sdn_ref": a.sdn_ref} for a in result.addresses
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
        "input_addresses": input_addresses(tx),
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


def load_seeds(path: Path) -> list[str]:
    """Seeds from `btc-trace ofac --json` output, or a text file with one address per line."""
    text = path.read_text()
    if text.lstrip().startswith("{"):
        return [entry["address"] for entry in json.loads(text)["addresses"]]
    return [line.strip() for line in text.splitlines() if line.strip() and line[0] != "#"]


def _cmd_trace(args: argparse.Namespace) -> int:
    seeds = list(args.addresses)
    if args.seeds:
        seeds += load_seeds(args.seeds)
    if not seeds:
        raise ValueError("give at least one address, or --seeds FILE")
    client = NodeClient.from_env(args.record)
    result = trace(
        client,
        seeds,
        max_depth=args.depth,
        max_addresses=args.max_addresses,
        min_value_btc=args.min_btc,
        start_height=args.start_height,
    )
    report = json.dumps(result.to_dict(), indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n")
    else:
        print(report)
    print(
        f"{len(result.hops)} hops, {len(result.coinjoin_stops)} CoinJoin stops, "
        f"{len(result.unexplored)} unexplored"
        + (" (address limit reached)" if result.truncated else ""),
        file=sys.stderr,
    )
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
    tr.add_argument("--out", type=Path, help="write the JSON report here instead of stdout")
    tr.set_defaults(func=_cmd_trace)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RpcError, ValueError, FileNotFoundError) as exc:
        print(f"btc-trace: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
