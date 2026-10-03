"""The HTML report: self-contained, escaped, and faithful to the trace."""

import json
import re

import pytest

from btc_trace.cli import main
from btc_trace.report import markdown, nice_ticks, render
from btc_trace.rpc import NodeClient
from btc_trace.trace import trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout


@pytest.fixture
def report():
    blocks = {
        h: [tx(f"t{h}", [vin("S", 2.0), vin(f"C{h}", 1.0)], [vout(0, f"P{h}", 2.5)])]
        for h in range(1, 60, 3)
    }
    blocks[70] = [tx("t70", [vin("T", 1.0)], [vout(0, "Q", 0.9)])]
    info = {"sdn_ref": "111", "sdn_name": "EXAMPLE MARKET"}
    result = trace(
        NodeClient(FakeChain(blocks)), ["S", "T"], max_depth=1, seed_info={"S": info, "T": info}
    )
    return result.to_dict()


def test_page_is_self_contained(report):
    page = render(report)
    assert page.startswith("<!doctype html>")
    assert not re.search(r"<(script|link|img)[^>]+(src|href)=", page)  # nothing loaded
    assert page.count("<script>") == 1  # only the page's own tooltip script


def test_page_states_findings_and_limits(report):
    page = render(report, findings="## Mine\n\n- **Bold** claim with `code`")
    assert "Where EXAMPLE MARKET&#x27;s Bitcoin went" in page
    assert "<h3>Mine</h3>" in page
    assert "<li><strong>Bold</strong> claim with <code>code</code></li>" in page
    assert "Heuristic estimates only" in page
    assert "Method and limits" in page
    assert "OFAC SDN entry 111" in page


def test_findings_cannot_inject_html():
    out = markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://a.example)")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert 'href="javascript' not in out
    assert '<a href="https://a.example" rel="noopener noreferrer">ok</a>' in out


def test_hero_shows_one_number_when_range_collapses(report):
    total = report["entities"][0]["outflow"]["total"]
    assert report["entities"][0]["outflow"]["total_core"] == pytest.approx(total)
    assert f"{total:,.0f} BTC</div>" in render(report)
    assert " to " not in render(report).split('class="value">')[1].split("</div>")[0]


def test_timeline_marks_and_tables(report):
    page = render(report, marks=[("2020-02-01", "Example event")])
    assert "Example event (2020-02-01)" in page
    assert page.count("<tr>") >= 2 + 2  # header + seed rows at least
    assert "Table: value leaving by month" in page


def test_nice_ticks():
    assert nice_ticks(523) == [0, 200, 400, 600]
    assert nice_ticks(0) == [0.0, 1.0]


def test_report_command(report, tmp_path, capsys):
    src = tmp_path / "r.json"
    src.write_text(json.dumps(report))
    out = tmp_path / "site" / "index.html"
    assert main(["report", str(src), "--out", str(out), "--mark", "2020-02-01=Event"]) == 0
    assert out.exists() and "Event (2020-02-01)" in out.read_text()


def test_bad_mark_is_rejected(report, tmp_path):
    src = tmp_path / "r.json"
    src.write_text(json.dumps(report))
    with pytest.raises(SystemExit):
        main(["report", str(src), "--out", str(tmp_path / "x.html"), "--mark", "soon=Later"])
