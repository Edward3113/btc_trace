"""Dates, sweep classification, batch-sweep robustness and per-seed summaries."""

import json

import pytest

from btc_trace.cli import main
from btc_trace.rpc import NodeClient
from btc_trace.trace import INTERNAL_SWEEP, SERVICE_SWEEP, trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout


def deposits(prefix, n=25):
    return [vin(f"{prefix}{i}", 0.1) for i in range(n)]


@pytest.fixture
def chain():
    """
    block 10: S and W spent together            -> cluster {S, W}
    block 11: S swept with 25 deposits into W   -> internal sweep (W is in S's cluster)
    block 12: S swept with 25 deposits into H   -> service sweep (H is outside)
    block 13: S + 25 deposits pay 3 recipients  -> batch sweep, clustered but flagged
    block 14: T (a second seed) spends alone
    """
    return FakeChain(
        {
            10: [tx("t1", [vin("S", 1.0), vin("W", 2.0)], [vout(0, "C", 2.9)])],
            11: [tx("t2", [vin("S", 0.5), *deposits("D")], [vout(0, "W", 2.9)])],
            12: [tx("t3", [vin("S", 0.25), *deposits("E")], [vout(0, "H", 2.7)])],
            13: [
                tx(
                    "t4",
                    [vin("S", 0.125), *deposits("B")],
                    [vout(0, "P0", 1.0), vout(1, "P1", 1.0), vout(2, "P2", 0.6)],
                )
            ],
            14: [tx("t5", [vin("T", 3.0)], [vout(0, "Q", 2.99)])],
        }
    )


def run(chain, **kwargs):
    return trace(NodeClient(chain), ["S", "T"], max_depth=1, **kwargs)


def test_hops_and_stops_carry_dates(chain):
    result = run(chain)
    hop = next(h for h in result.hops if h.txid == "t1")
    assert (hop.height, hop.date) == (10, "2020-01-11")  # fake chain: one day per block
    assert {s.date for s in result.service_stops} == {"2020-01-12", "2020-01-13"}
    assert result.transactions["t1"].date == "2020-01-11"


def test_sweeps_are_classified_internal_or_service(chain):
    stops = {s.txid: s.classification for s in run(chain).service_stops}
    assert stops == {"t2": INTERNAL_SWEEP, "t3": SERVICE_SWEEP}


def test_batch_sweeps_are_clustered_but_measured(chain):
    result = run(chain)
    assert result.transactions["t4"].batch_sweep
    assert not result.transactions["t1"].batch_sweep
    cluster = result.clusters[0]
    assert cluster.seeds == ["S"]
    assert len(cluster.addresses) == 2 + 25  # S, W and the batch sweep's 25 deposits
    assert cluster.batch_links == 1
    assert cluster.size_without_batch_sweeps == 2  # just S and W


def test_seed_summaries(chain):
    summaries = {s.address: s for s in run(chain).seed_summaries}
    s = summaries["S"]
    assert s.spending_txs == 4
    assert s.spent_btc == pytest.approx(1.0 + 0.5 + 0.25 + 0.125)
    assert s.returned_btc == 0
    assert s.net_out_btc == pytest.approx(s.spent_btc)
    assert (s.first_spend, s.last_spend) == ("2020-01-11", "2020-01-14")
    assert s.cluster == 1 and s.cluster_size == 27
    t = summaries["T"]
    assert (t.spending_txs, t.spent_btc, t.cluster_size) == (1, 3.0, 1)


def test_report_stores_cospent_inputs_once(chain):
    report = run(chain).to_dict()
    assert all("other_inputs" not in h for h in report["hops"])
    t4 = report["transactions"]["t4"]
    assert (t4["outside_inputs"], len(t4["outside_inputs_sample"])) == (25, 10)


def test_saved_progress_without_dates_gets_dated(chain, tmp_path):
    run(chain, cache_dir=tmp_path)
    (jsonl,) = tmp_path.glob("*.blocks.jsonl")
    old_style = []
    for line in jsonl.read_text().splitlines():
        record = json.loads(line)
        record.pop("time")
        old_style.append(json.dumps(record))
    jsonl.write_text("\n".join(old_style) + "\n")

    chain.calls.clear()
    result = run(chain, cache_dir=tmp_path)
    methods = [m for m, _ in chain.calls]
    assert "getblock" not in methods  # blocks are not fetched again
    assert methods.count("getblockheader") == 5  # one cheap lookup per block with spends
    assert all(h.date for h in result.hops)


def test_show_leads_with_summaries(chain, tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(run(chain).to_dict()))
    assert main(["show", str(path), "--hops", "0"]) == 0
    out = capsys.readouterr().out
    assert "Seeds (2 of 2 ever spent), by net BTC out" in out
    assert "internal consolidation (heuristic): 1 sweep(s)" in out
    assert "without them the seed's part is 2 address(es)" in out
    assert "hop(s) not printed (use --hops N)" in out


def test_change_back_to_the_same_address_is_not_counted_as_outflow():
    """A reused hot-wallet address: 10 BTC in, 9 BTC change back, spent again."""
    chain = FakeChain(
        {
            1: [tx("a", [vin("R", 10.0)], [vout(0, "X", 1.0), vout(1, "R", 9.0)])],
            2: [tx("b", [vin("R", 9.0)], [vout(0, "Y", 2.0), vout(1, "R", 7.0)])],
        }
    )
    (s,) = trace(NodeClient(chain), ["R"], max_depth=1).seed_summaries
    assert s.spent_btc == pytest.approx(19.0)  # gross double-counts the recycled 9 BTC
    assert s.returned_btc == pytest.approx(16.0)
    assert s.net_out_btc == pytest.approx(3.0)  # what actually left: 1 + 2
