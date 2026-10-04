"""Render a UTXO set report (with its reuse scan) as one self-contained HTML page.

The page answers one question: how much bitcoin sits behind a public key that is
already visible on-chain? It shows totals only, by script type and by when coins last
moved, never a list of exposed addresses: a ranked list of other people's exposed
balances would be a target list.

Like the trace page, it loads nothing from the internet, works in light and dark mode
and on phones, and every chart has a table behind it.
"""

from __future__ import annotations

from typing import Any

from btc_trace import __version__
from btc_trace.report import (
    CSS,
    SCRIPT,
    _column_path,
    _table,
    btc,
    compact,
    esc,
    markdown,
    nav,
    nice_ticks,
)

KEY_KINDS = ("pubkey", "multisig", "witness_v1_taproot")
HASH_KINDS = ("pubkeyhash", "scripthash", "witness_v0_keyhash", "witness_v0_scripthash")

# Published measurements the page compares against. Sources are linked on the page.
PUBLISHED = [
    (
        "BIP-361 (draft)",
        "1 Mar 2026",
        "over 34% of all bitcoin",
        "https://github.com/bitcoin/bips/blob/master/bip-0361.mediawiki",
    ),
    (
        "Google Quantum AI",
        "Mar 2026",
        "about 6.9 million BTC (about 2.3 million dormant)",
        "https://quantumai.google/static/site-assets/downloads/cryptocurrency-whitepaper.pdf",
    ),
    (
        "Project Eleven, cited by Chaincode Labs",
        "Jan 2025",
        "about 6.26 million BTC",
        "https://chaincode.com/bitcoin-post-quantum.pdf",
    ),
    (
        "Chaincode Labs (output type only)",
        "May 2025",
        "P2PK about 1,720,747 BTC; P2TR about 146,715 BTC",
        "https://chaincode.com/bitcoin-post-quantum.pdf",
    ),
]

DEFAULT_LINKS = [("← Hydra Market trace", "../")]

PAGE_CSS = """
:root { --key: #2a78d6; --reused: #eb6834; --hidden: #d6d5ce; }
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    --key: #3987e5; --reused: #d95926; --hidden: #3a3a37;
  }
}
:root[data-theme="dark"] { --key: #3987e5; --reused: #d95926; --hidden: #3a3a37; }
.chart .key { fill: var(--key); }
.chart .reused { fill: var(--reused); }
.chart .hidden { fill: var(--hidden); }
.chart .seg-label { fill: var(--ink); font-size: 13px; font-variant-numeric: tabular-nums; }
.chart .row-label { fill: var(--ink-2); font-size: 13px; }
.chart .row-value { fill: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 4px 0 10px;
  font-size: 14px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.legend .key { background: var(--key); }
.legend .reused { background: var(--reused); }
.legend .hidden { background: var(--hidden); }
.tile.hero .value { font-size: clamp(30px, 8.5vw, 48px); }
"""

LEGEND = (
    '<div class="legend">'
    '<span><i class="key"></i>Key in the output (P2PK, multisig, Taproot)</span>'
    '<span><i class="reused"></i>Key revealed by an earlier spend (address reuse)</span>'
    '<span><i class="hidden"></i>Only a hash on-chain</span>'
    "</div>"
)


def _split(report: dict[str, Any]) -> tuple[float, float, float, float]:
    """(key in output, revealed, hash only and not revealed, other) in BTC."""
    t = report["totals"]
    revealed = t.get("revealed_btc") or 0.0
    return (
        t["key_in_output_btc"],
        revealed,
        t["hash_only_btc"] - revealed,
        t["other_btc"],
    )


def composition_chart(report: dict[str, Any]) -> str:
    """One bar for all bitcoin, split by whether its key is visible."""
    key, revealed, hidden, other = _split(report)
    total = key + revealed + hidden + other
    width, height, top, bar_h = 960, 112, 8, 44
    gap = 2.0
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="composition-title" preserveAspectRatio="xMidYMid meet">'
    ]
    x = 0.0
    segments = [
        ("key", key, "Key in the output"),
        ("reused", revealed, "Revealed by address reuse"),
        ("hidden", hidden + other, "Only a hash on-chain (or no key at all)"),
    ]
    for i, (cls, value, label) in enumerate(segments):
        w = value / total * width if total else 0
        if w <= 0:
            continue
        draw_w = max(1.0, w - gap)
        parts.append(
            f'<rect class="{cls}" x="{x:.2f}" y="{top}" width="{draw_w:.2f}" height="{bar_h}" '
            'rx="4"/>'
        )
        share = value / total if total else 0
        if w > 40:
            # Alternate label rows so a narrow segment's label cannot run into the next.
            y = top + bar_h + (22 if i % 2 == 0 else 44)
            parts.append(
                f'<text class="seg-label" x="{x + 2:.2f}" y="{y}">'
                f"{compact(value)} BTC · {share:.1%}</text>"
            )
        parts.append(
            f'<rect class="hit" x="{x:.2f}" y="{top}" width="{w:.2f}" height="{bar_h}" '
            f'tabindex="0" data-tip-title="{esc(label)}" data-tip-value="{btc(value, 0)} BTC" '
            f'data-tip-note="{share:.2%} of all bitcoin"/>'
        )
        x += w
    parts.append("</svg>")
    return "".join(parts)


def _type_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for label, v in report["by_type"].items():
        if v["btc"] < 1:
            continue
        if v["key_in_output"]:
            key, revealed, hidden = v["btc"], 0.0, 0.0
        elif v["hash_only"]:
            revealed = v.get("revealed_btc") or 0.0
            key, hidden = 0.0, v["btc"] - revealed
        else:
            key, revealed, hidden = 0.0, 0.0, v["btc"]
        rows.append({"label": label, "key": key, "revealed": revealed, "hidden": hidden, **v})
    return rows


def type_chart(report: dict[str, Any]) -> str:
    """One horizontal bar per script type, split by exposure."""
    rows = _type_rows(report)
    if not rows:
        return '<p class="muted">No script types to chart.</p>'
    width, left, right, top, row_h, bar_h = 960, 132, 200, 8, 34, 20
    plot_w = width - left - right
    height = top + row_h * len(rows) + 8
    scale = max(r["btc"] for r in rows)
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="types-title" preserveAspectRatio="xMidYMid meet">'
    ]
    for i, r in enumerate(rows):
        y = top + i * row_h + (row_h - bar_h) / 2
        parts.append(
            f'<text class="row-label" x="{left - 10}" y="{y + bar_h - 5:.1f}" '
            f'text-anchor="end">{esc(r["label"])}</text>'
        )
        x = float(left)
        for cls in ("key", "reused", "hidden"):
            value = r[cls if cls != "reused" else "revealed"]
            w = value / scale * plot_w
            if w <= 0:
                continue
            parts.append(
                f'<rect class="{cls}" x="{x:.2f}" y="{y:.2f}" width="{max(1.0, w - 2):.2f}" '
                f'height="{bar_h}" rx="4"/>'
            )
            x += w
        exposed = r["key"] + r["revealed"]
        share = exposed / r["btc"] if r["btc"] else 0
        parts.append(
            f'<text class="row-value" x="{left + r["btc"] / scale * plot_w + 8:.1f}" '
            f'y="{y + bar_h - 5:.1f}">{compact(r["btc"])} BTC · {share:.0%} exposed</text>'
        )
        note = (
            "key in the output"
            if r["key"]
            else f"{btc(r['revealed'], 0)} BTC revealed by reuse"
            if r["hash_only"]
            else "no key-based spending condition"
        )
        parts.append(
            f'<rect class="hit" x="0" y="{top + i * row_h}" width="{width}" height="{row_h}" '
            f'tabindex="0" data-tip-title="{esc(r["label"])}: {btc(r["btc"], 0)} BTC held" '
            f'data-tip-value="{btc(exposed, 0)} BTC exposed ({share:.1%})" '
            f'data-tip-note="{esc(note)}; {r["coins"]:,} coins"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _years(report: dict[str, Any]) -> list[tuple[str, float, float, float]]:
    """(year, key in output, revealed, hash only) for the coins created in each year."""
    years: dict[str, list[float]] = {}
    for b in report["age_bins"]["bins"]:
        label = (b["start_date"] or "")[:4] or f"block {b['start_height']:,}"
        row = years.setdefault(label, [0.0, 0.0, 0.0])
        by_type = b["btc_by_type"]
        revealed = sum(b.get("revealed_btc_by_type", {}).values())
        row[0] += sum(by_type.get(k, 0.0) for k in KEY_KINDS)
        row[1] += revealed
        row[2] += sum(by_type.get(k, 0.0) for k in HASH_KINDS) - revealed
    return [(y, *v) for y, v in years.items()]


def age_chart(report: dict[str, Any]) -> str:
    """Stacked columns: the BTC created (last moved) in each year, by exposure."""
    years = _years(report)
    if not years:
        return '<p class="muted">No age data to chart.</p>'
    width, height = 960, 340
    left, right, top, bottom = 64, 16, 16, 36
    plot_w, plot_h = width - left - right, height - top - bottom
    ticks = nice_ticks(max(k + r + h for _, k, r, h in years))
    y_max = ticks[-1]
    band = plot_w / len(years)
    bar = max(4.0, min(36.0, band - 6))
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="age-title" preserveAspectRatio="xMidYMid meet">'
    ]
    for tick in ticks:
        y = top + plot_h - tick / y_max * plot_h
        parts.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{compact(tick)}</text>"
        )
    parts.append(
        f'<line class="axis" x1="{left}" x2="{width - right}" '
        f'y1="{top + plot_h}" y2="{top + plot_h}"/>'
    )
    every = max(1, len(years) // 12)
    for i, (year, key, revealed, hidden) in enumerate(years):
        x = left + i * band + (band - bar) / 2
        base = top + plot_h
        stack = [("key", key), ("reused", revealed), ("hidden", hidden)]
        drawn = [(c, v / y_max * plot_h) for c, v in stack if v > 0]
        for j, (cls, h) in enumerate(drawn):
            last = j == len(drawn) - 1
            seg = h if last else max(0.0, h - 2)  # 2px gap between stacked segments
            y = base - h
            if last:
                parts.append(f'<path class="{cls}" d="{_column_path(x, y, bar, h)}"/>')
            elif seg > 0:
                parts.append(
                    f'<rect class="{cls}" x="{x:.2f}" y="{y + 2:.2f}" width="{bar:.2f}" '
                    f'height="{seg:.2f}"/>'
                )
            base -= h
        total = key + revealed + hidden
        exposed = key + revealed
        parts.append(
            f'<rect class="hit" x="{left + i * band:.2f}" y="{top}" width="{band:.2f}" '
            f'height="{plot_h}" tabindex="0" data-tip-title="Coins last moved in {esc(year)}" '
            f'data-tip-value="{btc(exposed, 0)} of {btc(total, 0)} BTC exposed" '
            f'data-tip-note="key in output {btc(key, 0)} · reuse {btc(revealed, 0)} · '
            f'hash only {btc(hidden, 0)}"/>'
        )
        if i % every == 0:
            parts.append(
                f'<text class="tick" x="{left + i * band + band / 2:.1f}" y="{height - 12}" '
                f'text-anchor="middle">{esc(year)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _figure(title_id: str, title: str, sub: str, chart: str, legend: bool = True) -> str:
    return (
        f'<figure><figcaption id="{title_id}">{esc(title)}</figcaption>'
        f'<p class="sub">{sub}</p>{LEGEND if legend else ""}'
        f'<div class="chart-scroll">{chart}</div><div class="tip" role="status"></div></figure>'
    )


def render_quantum(
    report: dict[str, Any],
    *,
    exposure: dict[str, Any] | None = None,
    findings: str | None = None,
    title: str | None = None,
    links: list[tuple[str, str]] | None = None,
) -> str:
    snap, t = report["snapshot"], report["totals"]
    measured = t.get("exposed_btc") is not None
    title = title or "How much bitcoin is exposed to a quantum computer?"
    key, revealed, hidden, other = _split(report)
    out: list[str] = []
    out.append(nav(DEFAULT_LINKS if links is None else links))
    out.append(f"<h1>{esc(title)}</h1>")
    when = f" ({esc(snap['date'])})" if snap.get("date") else ""
    out.append(
        f'<p class="sub">Every unspent output in the Bitcoin UTXO set at block '
        f"{snap['base_height']:,}{when}, measured on a self-hosted Bitcoin Core node: "
        f"{t['coins']:,} coins holding {btc(t['btc'])} BTC.</p>"
    )
    out.append(
        '<div class="note"><strong>Read this first.</strong> A public key on-chain is a '
        "risk only against a quantum computer large enough to run Shor's algorithm, which "
        "does not exist today. Exposure describes keys, not owners: it says nothing about "
        "who holds a coin. This page shows totals only and lists no addresses.</div>"
    )

    tiles = []
    if measured:
        tiles.append(
            '<div class="tile hero"><div class="label">Bitcoin behind a public key that is '
            "already on-chain</div>"
            f'<div class="value">{btc(t["exposed_btc"], 0)} BTC</div>'
            f'<div class="detail">{t["exposed_share"]:.1%} of all bitcoin. Address reuse '
            f"accounts for {revealed / t['exposed_btc']:.0%} of it.</div></div>"
        )
    tiles.append(
        '<div class="tile"><div class="label">Key in the output</div>'
        f'<div class="value">{compact(key)}</div>'
        '<div class="detail">BTC in P2PK, bare multisig and Taproot outputs</div></div>'
    )
    if measured:
        tiles.append(
            '<div class="tile"><div class="label">Revealed by address reuse</div>'
            f'<div class="value">{compact(revealed)}</div>'
            f'<div class="detail">BTC at {report["reuse"]["matched_addresses"]:,} hash-based '
            "addresses that have spent before</div></div>"
        )
        tiles.append(
            '<div class="tile"><div class="label">Exposed and unmoved 5+ years</div>'
            f'<div class="value">{compact(t["dormant_exposed_btc"])}</div>'
            '<div class="detail">BTC whose owners may never move it</div></div>'
        )
    tiles.append(
        '<div class="tile"><div class="label">Early-mining P2PK</div>'
        f'<div class="value">{compact(t["coinbase_p2pk_btc"])}</div>'
        f'<div class="detail">BTC in {t["coinbase_p2pk_coins"]:,} coinbase outputs</div></div>'
    )
    out.append(f'<div class="tiles">{"".join(tiles)}</div>')

    if findings:
        out.append('<section id="findings"><h2>Findings</h2>')
        out.append(markdown(findings))
        out.append("</section>")

    out.append(
        _figure(
            "composition-title",
            "All bitcoin, by whether its public key is visible",
            "Hover a segment for the exact amount.",
            composition_chart(report),
        )
    )
    out.append(
        _figure(
            "types-title",
            "Exposure by output type (BTC)",
            "P2PK, multisig and Taproot outputs show a key by design; hash-based types "
            "are exposed only where the address has spent before.",
            type_chart(report),
        )
    )
    type_rows = [
        [
            esc(r["label"]),
            f"{r['coins']:,}",
            btc(r["btc"]),
            btc(r["key"] + r["revealed"]),
            f"{(r['key'] + r['revealed']) / r['btc']:.1%}" if r["btc"] else "",
        ]
        for r in _type_rows(report)
    ]
    out.append(
        "<details><summary>Table: exposure by output type</summary>"
        + _table(
            [
                ("Type", False),
                ("Coins", True),
                ("BTC held", True),
                ("BTC exposed", True),
                ("Share exposed", True),
            ],
            type_rows,
        )
        + "</details>"
    )
    out.append(
        _figure(
            "age-title",
            "When the coins last moved (BTC, by year)",
            "Each column is the bitcoin whose current output was created that year. "
            "Exposed coins from early years are unlikely ever to move to a safer address.",
            age_chart(report),
        )
    )
    year_rows = [[esc(y), btc(k), btc(r), btc(h), btc(k + r)] for y, k, r, h in _years(report)]
    out.append(
        "<details><summary>Table: coins by the year they last moved</summary>"
        + _table(
            [
                ("Year", False),
                ("Key in output", True),
                ("Revealed by reuse", True),
                ("Hash only", True),
                ("Exposed", True),
            ],
            year_rows,
        )
        + "</details>"
    )

    out.append("<h2>Compared with published estimates</h2>")
    ours = (
        f"{btc(t['exposed_btc'], 0)} BTC, {t['exposed_share']:.1%} of all bitcoin"
        if measured
        else f"{btc(key, 0)} BTC with the key in the output (reuse not measured)"
    )
    rows = [["<strong>This page</strong>", esc(snap.get("date") or ""), esc(ours)]]
    rows += [
        [f'<a href="{esc(url)}" rel="noopener noreferrer">{esc(name)}</a>', esc(d), esc(v)]
        for name, d, v, url in PUBLISHED
    ]
    out.append(_table([("Source", False), ("As of", False), ("Exposed", False)], rows))

    if exposure:
        out.append(_sanctions_section(exposure))

    out.append("<h2>What could change this</h2>")
    out.append(
        markdown(
            """
- **[BIP-360](https://github.com/bitcoin/bips/blob/master/bip-0360.mediawiki)**
  (draft) proposes Pay-to-Merkle-Root (P2MR), a `bc1z` output type without Taproot's
  key path, so coins sent to it would show no key until spent.
- **[BIP-361](https://github.com/bitcoin/bips/blob/master/bip-0361.mediawiki)** (draft)
  proposes a migration: first stop sending coins to quantum-vulnerable types, later
  restrict how coins with exposed keys can be spent. The coins it is most about are
  the exposed ones that have not moved in years, shown in the age chart above.
- **Owners can act today.** Coins at a reused address stop being exposed once they
  move to a fresh hash-based address. Coins in P2PK outputs need their owner, who may
  have lost the keys long ago.
"""
        )
    )

    out.append("<h2>Method and limits</h2>")
    reuse = report.get("reuse") or {}
    scan = (
        f"{reuse.get('blocks', 0):,} blocks and {reuse.get('inputs', 0):,} inputs"
        if reuse
        else "no reveal scan"
    )
    checks = report["checks"]
    verified = (
        "The snapshot was verified against the node's own UTXO set hash "
        f"(`{checks['computed_txoutset_hash']}`)."
        if checks.get("hash_matches")
        else "The snapshot's hash was not compared with the node's."
    )
    out.append(
        markdown(
            f"""
- **Data.** Bitcoin Core's `dumptxoutset` wrote the UTXO set at block
  {snap["base_height"]:,}. {verified}
- **Key in the output.** P2PK, bare multisig and Taproot outputs show a public key in
  the output script itself.
- **Address reuse.** A spend from a P2PKH, P2WPKH, P2SH or P2WSH address puts its key
  or script on-chain. Every input in the chain was read ({scan}) and what it revealed
  was hashed back to the addresses it unlocks, including the same key's other address
  types and keys inside revealed scripts, then matched to coins by the first 8 bytes of
  the hash (expected false matches: well under one).
- **Not counted.** Keys shared off-chain (such as extended public keys given to
  services), and the uncompressed P2PKH address of a key revealed only in compressed
  form. A revealed script with no key in it still counts as revealed.
- **Dormant** means the coin's output was created at least five years (262,800 blocks)
  before the snapshot.
- **Coins are not owners.** One wallet can hold many outputs, and exchanges hold
  coins for many customers.
"""
        )
    )
    out.append(
        f"<footer>Generated by btc_trace {esc(__version__)} from public data on a "
        "self-hosted node.</footer>"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}{PAGE_CSS}</style></head>"
        f"<body><main>{''.join(out)}</main><script>{SCRIPT}</script></body></html>\n"
    )


def _sanctions_section(exposure: dict[str, Any]) -> str:
    t = exposure["totals"]
    snap = exposure["snapshot"]
    entries = [e for e in exposure["by_entry"] if e["balance_btc"] > 0][:10]
    rows = [
        [
            esc(e["sdn_ref"]),
            esc(e["sdn_name"] or ""),
            btc(e["balance_btc"], 8),
            btc(e["exposed_btc"], 8),
        ]
        for e in entries
    ]
    return (
        "<h2>Sanctioned addresses</h2>"
        f"<p>Of the {t['addresses']:,} Bitcoin addresses on the OFAC sanctions list, "
        f"{t['funded_addresses']:,} still held coins at block {snap['height']:,}: "
        f"{btc(t['balance_btc'])} BTC, of which <strong>{btc(t['exposed_btc'])} BTC "
        f"({t['exposed_btc'] / t['balance_btc'] if t['balance_btc'] else 0:.1%})</strong> "
        "had a visible key. "
        "A balance on a sanctioned address does not show who controls it: seized coins "
        "can sit unmoved while held by a government.</p>"
        + _table(
            [
                ("SDN entry", False),
                ("Name", False),
                ("BTC held", True),
                ("BTC with key visible", True),
            ],
            rows,
        )
    )
