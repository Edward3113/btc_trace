"""Quantum exposure of what listed addresses hold today."""

import copy
import json

import pytest

from btc_trace.cli import main
from btc_trace.exposure import exposure
from btc_trace.rpc import NodeClient, RpcError
from btc_trace.schema import validate_report
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout

TAPROOT = "5120" + "ab" * 32
P2PKH = "76a914" + "cd" * 20 + "88ac"
INFO = {
    "REUSED": {"sdn_ref": "100", "sdn_name": "EXAMPLE MARKET"},
    "FRESH": {"sdn_ref": "100", "sdn_name": "EXAMPLE MARKET"},
    "TAPROOT": {"sdn_ref": "200", "sdn_name": "EXAMPLE EXCHANGE"},
    "EMPTY": {"sdn_ref": "200", "sdn_name": "EXAMPLE EXCHANGE"},
}


def chain():
    """REUSED spent at block 5 and holds coins again; FRESH and TAPROOT never spent."""
    blocks = {h: [tx(f"pay{h}", [vin(f"F{h}", 2.0)], [vout(0, "REUSED", 1.0)])] for h in (1, 2)}
    blocks[3] = [tx("fund", [vin("F3", 3.0)], [vout(0, "FRESH", 2.5), vout(1, "EMPTY", 0.4)])]
    blocks[5] = [tx("spend1", [vin("REUSED", 1.0)], [vout(0, "OUT", 0.9)])]
    blocks[6] = [tx("spend2", [vin("EMPTY", 0.4)], [vout(0, "OUT", 0.39)])]
    for h in range(7, 20):  # later activity: never needs to be read
        blocks[h] = [tx(f"later{h}", [vin("REUSED", 0.1)], [vout(0, "REUSED", 0.09)])]
    blocks[20] = [tx("tr", [vin("F20", 5.0)], [vout(0, "TAPROOT", 4.0)])]
    utxos = [
        {"address": "REUSED", "amount": 1.0, "height": 2},
        {"address": "REUSED", "amount": 0.09, "height": 19},
        {"address": "FRESH", "amount": 2.5, "height": 3},
        {"address": "TAPROOT", "amount": 4.0, "height": 20},
    ]
    return FakeChain(blocks, utxos=utxos, scripts={"TAPROOT": TAPROOT, "FRESH": P2PKH})


def run(fake=None, **kwargs):
    fake = fake or chain()
    result = exposure(NodeClient(fake), list(INFO), seed_info=INFO, workers=1, **kwargs)
    return result.to_dict(), fake


def rows(report):
    return {r["address"]: r for r in report["addresses"]}


def test_each_address_gets_the_right_status():
    report, _ = run()
    by = rows(report)
    assert by["TAPROOT"]["status"] == "key in output"
    assert by["TAPROOT"]["first_spend"] is None  # never needed checking
    assert by["REUSED"]["status"] == "spent before"
    assert by["REUSED"]["first_spend"] == {"height": 5, "date": "2020-01-06", "txid": "spend1"}
    assert by["REUSED"]["utxos"] == 2 and by["REUSED"]["oldest_utxo_height"] == 2
    assert by["FRESH"]["status"] == "hash only"
    assert by["FRESH"]["script_type"] == "pubkeyhash"
    assert by["EMPTY"]["status"] == "no balance"
    assert [r["exposed"] for r in report["addresses"]] == [True, False, True, False]


def test_totals_by_type_and_entry():
    report, _ = run()
    t = report["totals"]
    assert t["balance_btc"] == pytest.approx(7.59)
    assert t["exposed_btc"] == pytest.approx(5.09)  # Taproot 4.0 + reused 1.09
    assert t["hash_only_btc"] == pytest.approx(2.5)
    assert (t["funded_addresses"], t["exposed_addresses"]) == (3, 2)
    assert report["by_type"]["P2TR (Taproot)"]["exposed_btc"] == pytest.approx(4.0)
    assert report["by_type"]["P2PKH"]["hash_only_btc"] == pytest.approx(2.5)
    entries = {e["sdn_ref"]: e for e in report["by_entry"]}
    assert entries["100"]["balance_btc"] == pytest.approx(3.59)
    assert entries["100"]["exposed_btc"] == pytest.approx(1.09)
    assert entries["200"]["sdn_name"] == "EXAMPLE EXCHANGE"
    assert report["snapshot"] == {
        "height": 20,
        "bestblock": f"{20:064x}",
        "date": "2020-01-21",
        "utxos_scanned": 4,
    }


def test_checking_stops_once_every_address_has_spent():
    report, fake = run()
    reads = [p[0] for m, p in fake.calls if m == "getblock"]
    # Candidates for REUSED and FRESH: blocks 1, 2, 3, 5 and 7-19. Block 5 settles
    # REUSED; FRESH never spends, so every candidate is read, but no Taproot or empty
    # address is ever scanned for.
    scanned = next(p[1] for m, p in fake.calls if m == "scanblocks")
    assert scanned == ["addr(FRESH)", "addr(REUSED)"]
    assert report["candidate_blocks"] == 17 and len(reads) == 17

    fake2 = chain()
    fake2.utxos = [u for u in fake2.utxos if u["address"] != "FRESH"]
    report2, _ = run(fake2)
    reads2 = [int(p[0], 16) for m, p in fake2.calls if m == "getblock"]
    assert reads2 == [1, 2, 5]  # stops at the first spend
    assert report2["blocks_checked"] == 3


def test_saved_progress_is_reused(tmp_path):
    first, _ = run(cache_dir=tmp_path)
    again, fake = run(cache_dir=tmp_path)
    assert again == first
    assert [m for m, _ in fake.calls if m in ("getblock", "scanblocks")] == []


def test_invalid_address_is_reported():
    with pytest.raises(ValueError, match="INVALID_X"):
        exposure(NodeClient(chain()), ["INVALID_X"])


def test_report_matches_schema_and_mistakes_are_caught():
    report, _ = run()
    assert validate_report(report) == []
    assert validate_report(json.loads(json.dumps(report))) == []

    wrong = copy.deepcopy(report)
    wrong["addresses"][0]["exposed"] = False  # Taproot with a visible key
    assert validate_report(wrong)
    wrong = copy.deepcopy(report)
    wrong["addresses"][0]["status"] = "probably fine"
    assert any("status" in p for p in validate_report(wrong))
    wrong = copy.deepcopy(report)
    wrong["report_version"] = "9.9"
    assert "exposure reports of version 1.0" in validate_report(wrong)[0]


def test_utxo_scan_is_stopped_when_interrupted():
    class Interrupting:
        def __init__(self):
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            if params[:1] == ["start"]:
                raise KeyboardInterrupt
            return True

    transport = Interrupting()
    with pytest.raises(KeyboardInterrupt):
        NodeClient(transport).scan_utxos(["A"])
    assert transport.calls[-1] == ("scantxoutset", ["abort"])


def test_utxo_scan_in_progress_gives_clear_message():
    class Busy:
        def call(self, method, params):
            raise RpcError("scantxoutset: Scan already in progress, use action abort")

    with pytest.raises(RpcError, match="scan-abort"):
        NodeClient(Busy()).scan_utxos(["A"])


def test_exposure_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    fake = chain()
    monkeypatch.setattr(NodeClient, "from_env", classmethod(lambda cls, *a: cls(fake)))
    seeds = tmp_path / "seeds.json"
    seeds.write_text(
        json.dumps({"addresses": [{"address": a, **info} for a, info in INFO.items()]})
    )
    out = tmp_path / "reports" / "exposure.json"
    assert main(["exposure", "--seeds", str(seeds), "--sdn", "100", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "key visible" in printed and "spent before (first spend 2020-01-06)" in printed
    report = json.loads(out.read_text())
    assert sorted(rows(report)) == ["FRESH", "REUSED"]

    assert main(["validate", str(out)]) == 0
    assert "valid exposure report (version 1.0)" in capsys.readouterr().out
    assert main(["show", str(out)]) == 0
    assert "By SDN entry" in capsys.readouterr().out
    assert main(["report", str(out), "--out", str(tmp_path / "page.html")]) == 1
    assert "renders trace and utxo-set reports" in capsys.readouterr().err


def test_names_print_after_the_numbers(capsys):
    """Right-to-left names must not reorder the number columns."""
    from btc_trace.cli import _print_exposure

    report, _ = run()
    report["by_entry"][0]["sdn_name"] = "Gaza Now غزة الآن"
    _print_exposure(report)
    line = next(x for x in capsys.readouterr().out.splitlines() if "Gaza Now" in x)
    numbers_end = line.index("\u2068")
    assert "." in line[:numbers_end]  # both amounts come before the isolated name
