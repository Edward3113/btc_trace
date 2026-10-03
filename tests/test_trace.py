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
    assert hops["P1"].label == "payment (heuristic)"
    assert hops["P1"].reasons == ["output 1 was identified as change"]
    assert hops["C1"].label == "change (heuristic)"
    assert len(hops["C1"].reasons) == 2
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
    # The tracer checks every transaction itself, so the node skips that second read.
    assert scans[0][5] == {"filter_false_positives": False}


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


def test_level_stats_distinguish_receive_only(chain):
    # F2 only ever receives (block 50); S receives at 50 and spends at 100.
    receive_only = trace(NodeClient(chain), ["F2"], max_depth=1)
    assert receive_only.hops == []
    assert (receive_only.levels[0].blocks_touching, receive_only.levels[0].spending_txs) == (1, 0)

    spent = trace(NodeClient(chain), ["S"], max_depth=1)
    assert (spent.levels[0].blocks_touching, spent.levels[0].spending_txs) == (2, 1)


def test_level_stats_absent_address(chain):
    result = trace(NodeClient(chain), ["NEVER_USED"], max_depth=2)
    assert len(result.levels) == 1
    assert result.levels[0].blocks_touching == 0


def test_single_output_is_undetermined(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=2)
    q = hops_by_address(result)["Q"]
    assert q.label == "undetermined"
    assert "single output" in q.reasons[0]


def test_other_inputs_record_cospent_addresses():
    chain = FakeChain(
        {
            10: [tx("t1", [vin("S", 1.0), vin("W", 2.0), vin("W", 0.5)], [vout(0, "Z", 3.4)])],
        }
    )
    result = trace(NodeClient(chain), ["S"], max_depth=1)
    (hop,) = result.hops
    assert hop.from_addresses == ["S"]
    # Co-spent inputs are stored once per transaction, not repeated on every hop.
    info = result.transactions["t1"]
    assert (info.input_addresses, info.outside_inputs, info.outside_inputs_sample) == (2, 1, ["W"])


def test_show_command(chain, tmp_path, capsys):
    report = tmp_path / "r.json"
    report.write_text(json.dumps(trace(NodeClient(chain), ["S"], max_depth=2).to_dict()))
    assert main(["show", str(report)]) == 0
    out = capsys.readouterr().out
    assert "label: change (heuristic)" in out
    assert "single output" in out
    assert "not proof" in out


def test_false_positive_blocks_are_counted_but_ignored():
    chain = FakeChain(
        {
            10: [tx("t1", [vin("S", 1.0)], [vout(0, "P", 0.9)])],
            20: [tx("noise", [vin("X", 1.0)], [vout(0, "Y", 0.9)])],
        },
        false_positives={20},
    )
    result = trace(NodeClient(chain), ["S"], max_depth=1)
    level = result.levels[0]
    assert (level.candidate_blocks, level.blocks_touching, level.spending_txs) == (2, 1, 1)
    assert {h.txid for h in result.hops} == {"t1"}


@pytest.mark.parametrize("workers", [1, 3, 8])
def test_parallel_fetch_matches_sequential(chain, workers):
    expected = trace(NodeClient(chain), ["S"], max_depth=3, workers=1).to_dict()
    got = trace(NodeClient(chain), ["S"], max_depth=3, workers=workers).to_dict()
    expected["parameters"].pop("workers")
    got["parameters"].pop("workers")
    assert got == expected


def test_fetch_blocks_keeps_order():
    from btc_trace.trace import fetch_blocks

    chain = FakeChain({h: [] for h in range(1, 41)})
    hashes = [FakeChain.block_hash(h) for h in range(1, 41)]
    heights = [b["height"] for b in fetch_blocks(NodeClient(chain), hashes, workers=4)]
    assert heights == list(range(1, 41))
