"""End-to-end CLI runs against offline fixtures (no node needed)."""

import json
from pathlib import Path

from btc_trace.cli import main
from btc_trace.rpc import fixture_name
from tests.txdata import tx, vin, vout

FIXTURES = Path(__file__).parent / "fixtures"


def test_ofac_command(capsys):
    assert main(["ofac", str(FIXTURES / "sdn_advanced_sample.xml")]) == 0
    out = capsys.readouterr()
    assert "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4" in out.out
    assert "rejected" in out.err


def test_tx_command_offline(tmp_path, monkeypatch, capsys):
    txid = "ab" * 32
    sample = tx(txid, [vin("A", 1.0)], [vout(0, "P", 0.5), vout(1, "A", 0.49)])
    (tmp_path / fixture_name("getrawtransaction", [txid, 2])).write_text(json.dumps(sample))
    monkeypatch.setenv("BTC_FIXTURES", str(tmp_path))

    assert main(["tx", txid]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["change_guess"]["vout"] == 1
    assert "Heuristic" in report["note"]


def test_missing_config_exits_cleanly(monkeypatch, capsys):
    for var in ("BTC_FIXTURES", "BTC_URL", "BTC_USER", "BTC_PASS"):
        monkeypatch.delenv(var, raising=False)
    assert main(["node"]) == 1
    assert "btc-trace:" in capsys.readouterr().err
