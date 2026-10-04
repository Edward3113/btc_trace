"""The quantum exposure page built from a UTXO set report."""

import json

import pytest

from btc_trace.cli import main
from btc_trace.quantum_page import age_chart, composition_chart, render_quantum, type_chart
from btc_trace.utxoset import build_report, read_snapshot
from tests.test_exposure import run as run_exposure
from tests.test_reveal import (
    KEY1_HASH,
    chain,
    run_scan,
    snap,  # noqa: F401 - a pytest fixture used below
    snapshot_coins,
)


@pytest.fixture
def report(snap, tmp_path):  # noqa: F811
    matched, totals = run_scan(snap, tmp_path, chain())
    stats = read_snapshot(snap, revealed=matched)
    reuse = {
        "scan_height": 2_600,
        "blocks": totals.blocks,
        "inputs": totals.inputs,
        "revealed_hashes": totals.reveals,
        "matched_addresses": int(matched.size),
        "method": "test",
    }
    dates = {h: f"{2009 + h // 1000}-01-01" for h in range(0, 3000, 1000)}
    return build_report(
        stats, base_height=2_600, base_date="2026-10-03", reuse=reuse, bin_dates=dates
    )


def test_page_shows_totals_and_no_addresses(report):
    page = render_quantum(report, findings="## Mine\n\n<script>alert(1)</script> **ok**")
    assert "19 BTC" in page  # 5 BTC Taproot + 14 BTC revealed by reuse
    assert "Key in the output" in page and "Revealed by address reuse" in page
    assert "&lt;script&gt;" in page and "<script>alert" not in page
    assert KEY1_HASH.hex() not in page  # no address hashes anywhere
    for coin in snapshot_coins():
        assert coin.script.hex() not in page
    assert "Compared with published estimates" in page
    assert page.count("<figure>") == 3 and page.count("<details>") == 2


def test_charts_handle_empty_and_partial_data(report):
    assert "<svg" in composition_chart(report)
    assert "<svg" in type_chart(report)
    assert "<svg" in age_chart(report)
    empty = dict(report, age_bins={"bin_size": 1000, "bins": []})
    assert "No age data" in age_chart(empty)


def test_page_without_a_reuse_scan(snap):  # noqa: F811
    report = build_report(read_snapshot(snap), base_height=2_600)
    page = render_quantum(report)
    assert "reuse not measured" in page
    assert "Revealed by address reuse" not in page


def test_report_command_writes_the_quantum_page(report, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "utxo.json"
    src.write_text(json.dumps(report))
    exposure_report, _ = run_exposure()
    exp = tmp_path / "exposure.json"
    exp.write_text(json.dumps(exposure_report))
    findings = tmp_path / "f.md"
    findings.write_text("## Findings here")
    assert main(["report", str(src), "--exposure", str(exp), "--findings", str(findings)]) == 0
    page = (tmp_path / "docs" / "quantum" / "index.html").read_text()
    assert "Sanctioned addresses" in page and "EXAMPLE MARKET" in page
    assert "Findings here" in page

    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"report_version": "1.0"}))
    assert main(["report", str(trace), "--no-validate", "--exposure", str(exp)]) == 1
    assert "--exposure goes with a utxo-set report" in capsys.readouterr().err


def test_quantum_page_links_back_by_default(report):
    assert '<a href="../">← Hydra Market trace</a>' in render_quantum(report)
    assert "<nav" not in render_quantum(report, links=[])
