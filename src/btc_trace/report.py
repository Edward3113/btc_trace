"""Render a trace report (JSON) as one self-contained HTML page.

The page has no external scripts, fonts or images, so it can be opened from disk or
published with GitHub Pages as-is. Factual sections are generated from the report;
interpretation comes from an optional Markdown file the analyst writes, so the
judgement calls stay human-authored and reviewable.
"""

from __future__ import annotations

import html
import json
import math
import re
from datetime import date
from typing import Any

from btc_trace import __version__

# ---------------------------------------------------------------------------
# Small helpers


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def btc(value: float, places: int = 2) -> str:
    return f"{value:,.{places}f}"


def compact(value: float) -> str:
    """1,284 / 12.9K / 4.2M for stat tiles."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def nice_ticks(maximum: float, count: int = 5) -> list[float]:
    """Round axis ticks from 0 up to at least ``maximum``."""
    if maximum <= 0:
        return [0.0, 1.0]
    raw = maximum / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    top = math.ceil(maximum / step) * step
    return [round(i * step, 10) for i in range(int(round(top / step)) + 1)]


def _range(low: float, high: float) -> str:
    """'47,417 to 48,235', or one number when both ends round the same."""
    a, b = btc(low, 0), btc(high, 0)
    return a if a == b else f"{a} to {b}"


def _tip_range(low: float, high: float) -> str:
    return "" if btc(low) == btc(high) else f"range {btc(low)} to {btc(high)} BTC"


def short(address: str, keep: int = 10) -> str:
    return address if len(address) <= keep + 3 else address[:keep] + "…"


# ---------------------------------------------------------------------------
# Minimal, safe Markdown for the analyst's findings file


_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*]+)\*(?![*\w])"), r"<em>\1</em>"),
    (
        re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"),
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
    ),
]


def _inline(text: str) -> str:
    out = esc(text)
    for pattern, replacement in _INLINE:
        out = pattern.sub(replacement, out)
    return out


def markdown(source: str) -> str:
    """Headings, paragraphs, lists, bold, italics, code and http(s) links.

    Everything is HTML-escaped first, so raw HTML in the file is shown as text.
    """
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []
    ordered = False

    def flush() -> None:
        nonlocal para, items
        if para:
            blocks.append(f"<p>{_inline(' '.join(para))}</p>")
            para = []
        if items:
            tag = "ol" if ordered else "ul"
            lis = "".join(f"<li>{_inline(i)}</li>" for i in items)
            blocks.append(f"<{tag}>{lis}</{tag}>")
            items = []

    for raw in source.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        number = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if not line.strip():
            flush()
        elif heading:
            flush()
            level = len(heading.group(1)) + 1  # the page title is the only h1
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet or number:
            if para:
                flush()
            if items and ordered != bool(number):
                flush()
            ordered = bool(number)
            items.append((bullet or number).group(1))
        elif items and raw.startswith(("  ", "\t")):
            items[-1] += " " + line.strip()  # continuation of a list item
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Charts (inline SVG; hover handled by the page script via data-* attributes)


def _month_range(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:7])
    end = (int(last[:4]), int(last[5:7]))
    months = []
    while (y, m) <= end:
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _column_path(x: float, y: float, width: float, height: float) -> str:
    """A column with a 4px rounded top and a square base."""
    r = min(4.0, width / 2, height)
    return (
        f"M{x:.2f},{y + height:.2f} L{x:.2f},{y + r:.2f} Q{x:.2f},{y:.2f} {x + r:.2f},{y:.2f} "
        f"L{x + width - r:.2f},{y:.2f} Q{x + width:.2f},{y:.2f} {x + width:.2f},{y + r:.2f} "
        f"L{x + width:.2f},{y + height:.2f} Z"
    )


def monthly_chart(by_month: dict[str, list[float]]) -> str:
    months = sorted(m for m in by_month if m != "unknown")
    if not months:
        return '<p class="muted">No dated outflow to chart.</p>'
    months = _month_range(months[0], months[-1])
    values = [by_month.get(m, [0.0, 0.0]) for m in months]
    width, height = 960, 320
    left, right, top, bottom = 64, 16, 16, 36
    plot_w, plot_h = width - left - right, height - top - bottom
    ticks = nice_ticks(max(low for low, _ in values))
    y_max = ticks[-1]
    band = plot_w / len(months)
    bar = max(2.0, min(24.0, band - 2))  # 2px surface gap between neighbours

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="monthly-title" preserveAspectRatio="xMidYMid meet">'
    ]
    for tick in ticks:
        y = top + plot_h - tick / y_max * plot_h
        parts.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{tick:,.0f}</text>"
        )
    parts.append(
        f'<line class="axis" x1="{left}" x2="{width - right}" '
        f'y1="{top + plot_h}" y2="{top + plot_h}"/>'
    )
    for i, (month, (low, high)) in enumerate(zip(months, values, strict=True)):
        x = left + i * band + (band - bar) / 2
        h = low / y_max * plot_h if y_max else 0
        if h > 0:
            parts.append(f'<path class="mark" d="{_column_path(x, top + plot_h - h, bar, h)}"/>')
        parts.append(
            f'<rect class="hit" x="{left + i * band:.2f}" y="{top}" width="{band:.2f}" '
            f'height="{plot_h}" tabindex="0" data-tip-title="{esc(month)}" '
            f'data-tip-value="{btc(low)} BTC" '
            f'data-tip-note="{_tip_range(low, high)}"/>'
        )
        if month.endswith("-01") or i == 0:
            parts.append(
                f'<text class="tick" x="{left + i * band:.1f}" y="{height - 12}">{month[:4]}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _days(day: str) -> int:
    return date.fromisoformat(day).toordinal()


def timeline_chart(seeds: list[dict[str, Any]], marks: list[tuple[str, str]]) -> str:
    rows = [s for s in seeds if s.get("first_spend") and s.get("last_spend")]
    if not rows:
        return '<p class="muted">No dated seed activity to chart.</p>'
    starts = [_days(s["first_spend"]) for s in rows] + [_days(d) for d, _ in marks]
    ends = [_days(s["last_spend"]) for s in rows] + [_days(d) for d, _ in marks]
    lo, hi = min(starts), max(ends)
    lo -= 30
    hi += 30
    row_h, bar_h = 26, 12
    width = 960
    left, right, top = 132, 16, 28
    plot_w = width - left - right
    height = top + row_h * len(rows) + 30

    def x_of(day: int) -> float:
        return left + (day - lo) / (hi - lo) * plot_w

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="timeline-title" preserveAspectRatio="xMidYMid meet">'
    ]
    first_year = date.fromordinal(lo).year + 1
    last_year = date.fromordinal(hi).year
    for year in range(first_year, last_year + 1):
        x = x_of(date(year, 1, 1).toordinal())
        parts.append(
            f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" '
            f'y2="{height - 26}"/><text class="tick" x="{x:.1f}" y="{height - 10}" '
            f'text-anchor="middle">{year}</text>'
        )
    for i, s in enumerate(rows):
        y = top + i * row_h + (row_h - bar_h) / 2
        x1, x2 = x_of(_days(s["first_spend"])), x_of(_days(s["last_spend"]))
        w = max(4.0, x2 - x1)
        parts.append(
            f'<text class="label" x="{left - 8}" y="{y + bar_h - 2:.1f}" '
            f'text-anchor="end">{esc(short(s["address"]))}</text>'
            f'<rect class="mark" x="{x1:.2f}" y="{y:.2f}" width="{w:.2f}" height="{bar_h}" '
            f'rx="4"/>'
            f'<rect class="hit" x="{left}" y="{top + i * row_h}" width="{plot_w}" '
            f'height="{row_h}" tabindex="0" data-tip-title="{esc(s["address"])}" '
            f'data-tip-value="{btc(s.get("net_out_btc", s["spent_btc"]))} BTC out" '
            f'data-tip-note="{esc(s["first_spend"])} to {esc(s["last_spend"])}, '
            f'{s["spending_txs"]:,} transactions"/>'
        )
    for day, label in marks:
        x = x_of(_days(day))
        parts.append(
            f'<line class="ref" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 10}" '
            f'y2="{height - 26}"/><text class="ref-label" x="{x - 4:.1f}" y="{top - 14}" '
            f'text-anchor="end">{esc(label)} ({esc(day)})</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Page


CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #6f6d68; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series: #2a78d6; --ref: #52514e; --note-bg: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #a3a29b; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series: #3987e5; --ref: #c3c2b7; --note-bg: #242422;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --muted: #a3a29b; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series: #3987e5; --ref: #c3c2b7; --note-bg: #242422;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 30px; line-height: 1.2; margin: 0 0 6px; }
h2 { font-size: 21px; margin: 40px 0 10px; }
h3 { font-size: 17px; margin: 22px 0 6px; }
p, li { color: var(--ink); max-width: 72ch; }
.sub, .muted { color: var(--ink-2); }
a { color: var(--series); }
code { font: 0.9em ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.note { background: var(--note-bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; margin: 20px 0; color: var(--ink-2); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin: 24px 0; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; }
.tile .label { color: var(--ink-2); font-size: 14px; }
.tile .value { font-size: 26px; font-weight: 600; line-height: 1.25; }
.tile.hero { grid-column: 1 / -1; }
.tile.hero .value { font-size: 48px; }
.tile .detail { color: var(--ink-2); font-size: 14px; }
figure { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  margin: 16px 0; padding: 16px; position: relative; }
figcaption { font-weight: 600; margin-bottom: 2px; }
figure .sub { font-size: 14px; margin: 0 0 10px; }
.chart-scroll { overflow-x: auto; }
.chart { width: 100%; min-width: 640px; height: auto; display: block; }
.chart .grid { stroke: var(--grid); stroke-width: 1; }
.chart .axis { stroke: var(--axis); stroke-width: 1; }
.chart .mark { fill: var(--series); }
.chart .hit { fill: transparent; cursor: default; outline: none; }
.chart .hit:hover, .chart .hit:focus { fill: var(--ink); fill-opacity: 0.05; }
.chart .tick, .chart .label { fill: var(--muted); font-size: 12px;
  font-variant-numeric: tabular-nums; }
.chart .label { fill: var(--ink-2); font-family: ui-monospace, Menlo, monospace; }
.chart .ref { stroke: var(--ref); stroke-width: 1; }
.chart .ref-label { fill: var(--ink-2); font-size: 12px; }
.tip { position: absolute; pointer-events: none; background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 13px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); max-width: 300px; display: none; }
.tip strong { display: block; font-size: 15px; }
.tip .t { color: var(--ink-2); overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; margin: 12px 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; background: var(--surface); }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid);
  white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
details { margin: 8px 0 16px; }
summary { cursor: pointer; color: var(--ink-2); }
footer { margin-top: 48px; color: var(--ink-2); font-size: 14px; }
"""

SCRIPT = """
document.querySelectorAll('figure').forEach(function (fig) {
  var tip = fig.querySelector('.tip');
  if (!tip) return;
  function show(el, x, y) {
    tip.textContent = '';
    var v = document.createElement('strong');
    v.textContent = el.getAttribute('data-tip-value');
    var t = document.createElement('div');
    t.className = 't';
    t.textContent = el.getAttribute('data-tip-title');
    var n = document.createElement('div');
    n.className = 't';
    n.textContent = el.getAttribute('data-tip-note') || '';
    tip.append(v, t, n);
    tip.style.display = 'block';
    var box = fig.getBoundingClientRect();
    var left = Math.min(x - box.left + 12, box.width - tip.offsetWidth - 8);
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top = Math.max(8, y - box.top - tip.offsetHeight - 12) + 'px';
  }
  fig.querySelectorAll('.hit').forEach(function (el) {
    el.addEventListener('pointermove', function (e) { show(el, e.clientX, e.clientY); });
    el.addEventListener('focus', function () {
      var r = el.getBoundingClientRect();
      show(el, r.left + r.width / 2, r.top + 20);
    });
    el.addEventListener('pointerleave', function () { tip.style.display = 'none'; });
    el.addEventListener('blur', function () { tip.style.display = 'none'; });
  });
});
"""


def _table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    head = "".join(
        f'<th class="num">{esc(h)}</th>' if n else f"<th>{esc(h)}</th>" for h, n in headers
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{cell}</td>' if headers[i][1] else f"<td>{cell}</td>"
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def render(
    report: dict[str, Any],
    *,
    title: str | None = None,
    findings: str | None = None,
    marks: list[tuple[str, str]] | None = None,
    timeline_rows: int = 20,
) -> str:
    marks = marks or []
    entities = report.get("entities", [])
    entity = entities[0] if len(entities) == 1 else None
    name = entity.get("sdn_name") if entity else None
    title = title or (f"Where {name}'s Bitcoin went" if name else "Bitcoin trace report")
    seeds = report.get("seed_summaries", [])
    active = [s for s in seeds if s.get("spending_txs")]
    dated = [s for s in active if s.get("first_spend")]
    first = min((s["first_spend"] for s in dated), default=None)
    last = max((s["last_spend"] for s in dated), default=None)
    clusters = report.get("clusters", [])
    seed_clusters = [c for c in clusters if c.get("seeds")]

    out: list[str] = []
    out.append(f"<h1>{esc(title)}</h1>")
    refs = sorted({e["sdn_ref"] for e in entities if e.get("sdn_ref")})
    who = ", ".join(f"OFAC SDN entry {esc(r)}" for r in refs) or "the seed addresses"
    out.append(
        f'<p class="sub">A heuristic trace of {len(report["seeds"])} sanctioned Bitcoin '
        f"address(es) from {who}, using public blockchain data up to block "
        f"{report['scanned_to_height']:,}.</p>"
    )
    out.append(
        f'<div class="note"><strong>Read this first.</strong> {esc(report["note"])} '
        "Sanctions designations change; this page is research, not compliance "
        "screening.</div>"
    )

    # Headline figures
    tiles = []
    if entity:
        o = entity["outflow"]
        tiles.append(
            '<div class="tile hero"><div class="label">Value that left the entity</div>'
            f'<div class="value">{_range(o["total"], o["total_core"])} BTC</div>'
            '<div class="detail">Gross flow out of all its clusters over the whole period, '
            "transfers between its own clusters excluded. Not a balance or profit, and "
            "not converted to dollars.</div></div>"
        )
    tiles.append(
        f'<div class="tile"><div class="label">Sanctioned addresses traced</div>'
        f'<div class="value">{len(report["seeds"]):,}</div>'
        f'<div class="detail">{len(active):,} ever spent</div></div>'
    )
    tiles.append(
        f'<div class="tile"><div class="label">Separate clusters</div>'
        f'<div class="value">{len(seed_clusters):,}</div>'
        '<div class="detail">holding those addresses</div></div>'
    )
    if report.get("levels"):
        tiles.append(
            f'<div class="tile"><div class="label">Spending transactions</div>'
            f'<div class="value">{compact(report["levels"][0]["spending_txs"])}</div>'
            '<div class="detail">from the sanctioned addresses</div></div>'
        )
    if first and last:
        tiles.append(
            f'<div class="tile"><div class="label">Active period</div>'
            f'<div class="value">{esc(first[:4])} to {esc(last[:4])}</div>'
            f'<div class="detail">{esc(first)} to {esc(last)}</div></div>'
        )
    out.append(f'<div class="tiles">{"".join(tiles)}</div>')

    if findings:
        out.append('<section id="findings"><h2>Findings</h2>')
        out.append(markdown(findings))
        out.append("</section>")

    # Charts
    if entity and entity.get("outflow_by_month"):
        by_month = entity["outflow_by_month"]
        out.append(
            '<figure><figcaption id="monthly-title">Value leaving the entity, by month (BTC)'
            '</figcaption><p class="sub">Low end of the range shown; hover a month for both '
            "ends.</p>"
            + f'<div class="chart-scroll">{monthly_chart(by_month)}</div>'
            + '<div class="tip" role="status"></div></figure>'
        )
        rows = [
            [esc(m), btc(v[0], 8), btc(v[1], 8)]
            for m, v in sorted(by_month.items())
            if v[0] or v[1]
        ]
        out.append(
            "<details><summary>Table: value leaving by month</summary>"
            + _table([("Month", False), ("Low (BTC)", True), ("High (BTC)", True)], rows)
            + "</details>"
        )
    if dated:
        top = sorted(dated, key=lambda s: -s.get("net_out_btc", s["spent_btc"]))[:timeline_rows]
        top.sort(key=lambda s: s["first_spend"])
        out.append(
            '<figure><figcaption id="timeline-title">When the busiest addresses were active'
            f'</figcaption><p class="sub">First to last spend for the {len(top)} addresses '
            "with the most value out, oldest first.</p>"
            + f'<div class="chart-scroll">{timeline_chart(top, marks)}</div>'
            + '<div class="tip" role="status"></div></figure>'
        )

    # Tables
    out.append("<h2>Sanctioned addresses</h2>")
    out.append(
        '<p class="muted">Net out is value spent minus change returned to the same '
        "address. It is still an upper bound: change sent to the owner's other addresses "
        "is included. Adding rows double-counts money moved between them.</p>"
    )
    seed_rows = [
        [
            f"<code>{esc(s['address'])}</code>",
            btc(s.get("net_out_btc", s["spent_btc"]), 8),
            f"{s['spending_txs']:,}",
            f"#{s['cluster']}" if s.get("cluster") else "",
            f"{s['cluster_size']:,}",
            esc(s.get("first_spend") or ""),
            esc(s.get("last_spend") or ""),
        ]
        for s in seeds
    ]
    out.append(
        _table(
            [
                ("Address", False),
                ("Net out (BTC)", True),
                ("Spends", True),
                ("Cluster", False),
                ("Cluster size", True),
                ("First spend", False),
                ("Last spend", False),
            ],
            seed_rows,
        )
    )

    out.append("<h2>Clusters</h2>")
    out.append(
        '<p class="muted">Addresses the heuristics suggest share an owner. Where batch '
        "sweeps hold a cluster together, its size is shown both with and without them; "
        "value out is a low to high range for the same reason.</p>"
    )
    cluster_rows = []
    for c in seed_clusters[:25]:
        o = c.get("outflow") or {}
        cluster_rows.append(
            [
                f"#{clusters.index(c) + 1}",
                f"{len(c['addresses']):,}",
                f"{c.get('size_without_batch_sweeps', len(c['addresses'])):,}",
                f"{len(c['seeds'])}",
                f"{btc(o.get('total', 0))} to {btc(o.get('total_core', 0))}" if o else "",
            ]
        )
    out.append(
        _table(
            [
                ("Cluster", False),
                ("Addresses", True),
                ("Without batch sweeps", True),
                ("Seeds", True),
                ("Value out (BTC)", True),
            ],
            cluster_rows,
        )
    )
    if len(seed_clusters) > 25:
        out.append(f'<p class="muted">… {len(seed_clusters) - 25} more in the JSON report.</p>')

    stops = report.get("service_stops", [])
    if stops:
        internal = [s for s in stops if s.get("classification", "").startswith("internal")]
        external = [s for s in stops if s not in internal]
        out.append("<h2>Sweeps</h2>")
        out.append(
            f"<p>{len(stops):,} sweeps gathered many outside addresses into one or two "
            f"outputs. {len(internal):,} went into the spender's own cluster "
            f"({btc(sum(s['traced_value_btc'] for s in internal))} BTC), and "
            f"{len(external):,} went elsewhere "
            f"({btc(sum(s['traced_value_btc'] for s in external))} BTC), usually a sign "
            "of funds entering an exchange or other custodial service.</p>"
        )

    out.append("<h2>Method and limits</h2>")
    out.append(
        markdown(
            """
- **Data.** Starting addresses come from OFAC's SDN list. Every transaction comes from a
  Bitcoin Core full node; no third-party blockchain API is used.
- **Common-input ownership.** Addresses spent together in one transaction are assumed to
  share an owner. CoinJoins break this, so likely CoinJoins end the trace.
- **Change detection.** A two-output transaction's change is guessed from address reuse,
  matching script types and round payment amounts. Many outputs stay *undetermined*.
- **Ranges, not points.** Large custodial wallets sweep hundreds of deposit addresses at
  once. Those batch sweeps can join unrelated wallets, so sizes and values are given with
  and without them.
- **What this cannot show.** Identities, intent, or wrongdoing. An address appearing here
  is not evidence that its owner did anything wrong.
"""
        )
    )
    out.append(
        f"<footer>Generated by btc_trace {esc(__version__)} from public data. "
        "Heuristic estimates only.</footer>"
    )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{''.join(out)}</main><script>{SCRIPT}</script></body></html>\n"
    )


def load(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)
