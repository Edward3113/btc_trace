"""Service-consolidation stops, clusters and SDN attribution.

The synthetic chain mirrors the shape of a real trace: a seed spent with co-owned
inputs, the payment swept into a service with many outside deposits, and the change
spent again with more co-owned inputs.
"""

import json

import pytest

from btc_trace.cli import main
from btc_trace.rpc import NodeClient
from btc_trace.trace import trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout

PKH, SH, WPKH = "pubkeyhash", "scripthash", "witness_v0_keyhash"


@pytest.fixture
def chain():
    deposits = [vin(f"O{i}", 1.0, SH) for i in range(25)]
    return FakeChain(
        {
            100: [
                tx(
                    "t1",
                    [vin("S", 100.0, PKH), vin("A1", 40.0, PKH), vin("A2", 19.0, PKH)],
                    [vout(0, "C1", 18.85793354, PKH), vout(1, "X1", 140.1419, SH)],
                )
            ],
            110: [tx("t2", [vin("X1", 140.1419, SH), *deposits], [vout(0, "H", 165.1, WPKH)])],
            120: [
                tx(
                    "t3",
                    [vin("C1", 18.85793354, PKH), vin("B1", 112.0, PKH)],
                    [vout(0, "C2", 6.44541077, PKH), vout(1, "X2", 124.3123, SH)],
                )
            ],
        }
    )


def test_service_consolidation_is_an_endpoint(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=3)
    (stop,) = result.service_stops
    assert stop.txid == "t2"
    assert stop.from_addresses == ["X1"]
    assert stop.outside_inputs == 25
    assert stop.traced_value_btc == pytest.approx(140.1419)
    assert stop.outputs == [{"address": "H", "value_btc": 165.1}]
    assert "H" not in {h.to_address for h in result.hops}


def test_service_check_can_be_disabled(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=2, service_min_inputs=0)
    assert result.service_stops == []
    assert "H" in {h.to_address for h in result.hops}


def test_seed_cluster_grows_through_change(chain):
    result = trace(NodeClient(chain), ["S"], max_depth=3)
    seed_cluster = result.clusters[0]
    assert seed_cluster.seeds == ["S"]
    assert seed_cluster.addresses == sorted(["S", "A1", "A2", "C1", "B1", "C2"])
    assert {e.kind for e in seed_cluster.evidence} == {"common input", "change"}
    everything_clustered = {a for c in result.clusters for a in c.addresses}
    # Payments and the service's deposit addresses stay out of the seed's cluster.
    assert not everything_clustered & {"X1", "X2", "H", "O0"}


def test_lone_seed_still_listed(chain):
    result = trace(NodeClient(chain), ["NEVER_USED"], max_depth=1)
    assert [c.addresses for c in result.clusters] == [["NEVER_USED"]]


def test_sdn_filter_and_attribution(chain, tmp_path, monkeypatch, capsys):
    seeds = tmp_path / "seeds.json"
    seeds.write_text(
        json.dumps(
            {
                "addresses": [
                    {"address": "S", "sdn_ref": "111", "sdn_name": "EXAMPLE MARKET"},
                    {"address": "Z", "sdn_ref": "222", "sdn_name": "OTHER ENTITY"},
                ]
            }
        )
    )
    monkeypatch.setattr(NodeClient, "from_env", classmethod(lambda cls, *a: cls(chain)))
    monkeypatch.chdir(tmp_path)  # keep the default progress cache out of the project
    out = tmp_path / "report.json"
    assert main(["trace", "--seeds", str(seeds), "--sdn", "111", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["seeds"] == ["S"]
    assert report["seed_info"] == {"S": {"sdn_ref": "111", "sdn_name": "EXAMPLE MARKET"}}
    assert "1 sweep stops (0 internal)" in capsys.readouterr().err

    assert main(["show", str(out)]) == 0
    shown = capsys.readouterr().out
    assert "SDN entry 111: EXAMPLE MARKET" in shown
    assert "service consolidation (heuristic): 1 sweep(s)" in shown
    assert "#1: 6 address(es), 1 seed(s)" in shown
    assert "in 2 transaction(s)" in shown


def test_sdn_filter_needs_seeds_file(capsys):
    assert main(["trace", "--sdn", "111"]) == 1
    assert "--sdn needs --seeds" in capsys.readouterr().err
