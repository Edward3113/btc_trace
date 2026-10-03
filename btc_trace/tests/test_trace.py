import json

import pytest

from btc_trace.cli import load_seeds, main
from btc_trace.rpc import NodeClient
from btc_trace.trace import trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout


def coinjoin(txid, spender):
    inputs = [vin(spender, 0.11)] + [vin(f"cj_in{i}", 0.11) for i in range(4)]
    return tx(txid, inputs, [vout(i, f"cj_out{i}", 0.1) for i in range(5)])


@pytest.fixture
def chain():
    """
    height 50:  F -> S                          (funds S; not a spend, ignored)
    height 100: S -> P1 0.5 (payment), C1 (change: same script type, non-round)
    height 105: P1 -> Q 0.4999
    height 106: C1 enters a CoinJoin            (trace stops on that branch)
    height 110: Q -> R 0.4998, dust D 0.00001
    height 120: unrelated transaction
    """
    return FakeChain(
        {
            50: [tx("t0", [vin("F", 2.0)], [vout(0, "S", 1.0), vout(1, "F2", 0.99)])],
            100: [
                tx(
                    "t1",
                    [vin("S", 1.0)],
                    [vout(0, "P1", 0.5, "pubkeyhash"), vout(1, "C1", 0.49871234)],
                )
            ],
            105: [tx("t2", [vin("P1", 0.5, "pubkeyhash")], [vout(0, "Q", 0.4999)])],
            106: [coinjoin("cj", "C1")],
            110: [tx("t3", [vin("Q", 0.4999)], [vout(0, "R", 0.4998), vout(1, "D", 0.00001)])],
            120: [tx("t4", [vin("X", 1.0)], [vout(0, "Y", 0.9)])],
        }
    )


def hops_by_address(result):
    return {h.to_address: h for h in result.hops}


def test_first_hop_labels_change_and_payment(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=1)
    hops = hops_by_address(result)
    assert set(hops) == {"P1", "C1"}
    assert hops["P1"].label == "payment"
    assert hops["C1"].label == "change (heuristic)"
    assert hops["P1"].from_addresses == ["S"]
    assert result.unexplored == ["C1", "P1"]


def test_multi_hop_and_coinjoin_stop(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=3)
    assert set(hops_by_address(result)) == {"P1", "C1", "Q", "R", "D"}
    assert [(s.txid, s.from_addresses) for s in result.coinjoin_stops] == [("cj", ["C1"])]
    assert not any(h.to_address.startswith("cj_out") for h in result.hops)
    assert hops_by_address(result)["R"].depth == 3


def test_funding_transaction_is_not_a_spend(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=1)
    assert "t0" not in {h.txid for h in result.hops}


def test_min_value_does_not_follow_dust(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=3, min_value_btc=0.001)
    assert hops_by_address(result)["D"].followed is False
    assert hops_by_address(result)["R"].followed is True


def test_address_limit_truncates(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=3, max_addresses=2)
    assert result.truncated
    assert sum(h.followed for h in result.hops) == 1


def test_scan_starts_where_address_was_reached(chain):
    trace(NodeClient(chain), ["S"], max_depth=2, start_height=40)
    scans = [params for method, params in chain.calls if method == "scanblocks"]
    assert scans[0][2] == 40  # seeds from --start-height
    assert scans[1][2] == 100  # second level starts at the block that paid P1 and C1
    assert scans[0][5] == {"filter_false_positives": True}


def test_report_is_json_with_disclaimer(chain):
    report = json.loads(json.dumps(trace(NodeClient(chain), ["S"]).to_dict()))
    assert "not proof" in report["note"]
    assert report["scanned_to_height"] == 120


def test_load_seeds_json_and_text(tmp_path):
    js = tmp_path / "seeds.json"
    js.write_text(json.dumps({"addresses": [{"address": "A", "sdn_ref": "1"}]}))
    txt = tmp_path / "seeds.txt"
    txt.write_text("# comment\nB\n\nC\n")
    assert load_seeds(js) == ["A"]
    assert load_seeds(txt) == ["B", "C"]


def test_trace_command_requires_seeds(monkeypatch, capsys):
    monkeypatch.setenv("BTC_FIXTURES", "/nonexistent")
    assert main(["trace"]) == 1
    assert "at least one address" in capsys.readouterr().err
