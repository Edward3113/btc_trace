"""Value leaving a seed's cluster, reported as a full-cluster / core-cluster range."""

import json

import pytest

from btc_trace.cli import main
from btc_trace.rpc import NodeClient
from btc_trace.trace import trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout


def deposits(prefix, n=25):
    return [vin(f"{prefix}{i}", 0.1) for i in range(n)]


@pytest.fixture
def chain():
    """
    block 1: S + W spent together, 1.0 to X (outside) and 0.5 to W (inside)
    block 2: S + 25 deposits B* pay 3 recipients   -> batch sweep: B* join S's full cluster
    block 3: S pays 0.7 to B0                     -> inside the full cluster, outside the core
    block 4: S swept with 25 deposits E* into H   -> service sweep, 0.25 traced
    block 5: S pays 0.4 to T, another seed        -> leaves S's cluster for another seed's
    block 6: S joins a CoinJoin with 0.11         -> into CoinJoin
    block 7: T pays 0.3 to Z
    """
    cj_inputs = [vin("S", 0.11)] + [vin(f"cj{i}", 0.11) for i in range(4)]
    return FakeChain(
        {
            1: [tx("t1", [vin("S", 1.0), vin("W", 0.6)], [vout(0, "X", 1.0), vout(1, "W", 0.5)])],
            2: [
                tx(
                    "t2",
                    [vin("S", 0.2), *deposits("B")],
                    [vout(0, "P0", 0.9), vout(1, "P1", 0.9), vout(2, "P2", 0.8)],
                )
            ],
            3: [tx("t3", [vin("S", 0.75)], [vout(0, "B0", 0.7)])],
            4: [tx("t4", [vin("S", 0.25), *deposits("E")], [vout(0, "H", 2.7)])],
            5: [tx("t5", [vin("S", 0.45)], [vout(0, "T", 0.4)])],
            6: [tx("t6", cj_inputs, [vout(i, f"o{i}", 0.1) for i in range(5)])],
            7: [tx("t7", [vin("T", 0.35)], [vout(0, "Z", 0.3)])],
        }
    )


def seed_cluster(result, seed):
    return next(c for c in result.clusters if seed in c.seeds)


def test_outflow_range_for_the_seed_cluster(chain):
    result = trace(NodeClient(chain), ["S", "T"], max_depth=1)
    out = seed_cluster(result, "S").outflow
    # Full cluster {S, W, B*}: X 1.0 + P0-P2 2.6 + T 0.4 leave; W and B0 stay inside.
    assert out.to_outside_addresses == pytest.approx(1.0 + 2.6 + 0.4)
    # Core {S, W}: B0 (joined only through the batch sweep) now counts as leaving too.
    assert out.to_outside_addresses_core == pytest.approx(1.0 + 2.6 + 0.4 + 0.7)
    assert out.into_service_sweeps == pytest.approx(0.25)
    assert out.into_coinjoins == pytest.approx(0.11)
    assert out.to_other_seed_clusters == pytest.approx(0.4)
    assert out.total == pytest.approx(4.0 + 0.25 + 0.11)
    assert out.total_core == pytest.approx(4.7 + 0.25 + 0.11)
    assert out.total <= out.total_core  # full cluster is the lower bound
    assert out.spending_txs == 6


def test_totals_across_seed_clusters(chain):
    report = trace(NodeClient(chain), ["S", "T"], max_depth=1).to_dict()
    t_out = seed_cluster_dict(report, "T")["outflow"]
    assert t_out["to_outside_addresses"] == pytest.approx(0.3)
    totals = report["outflow_total"]
    assert totals["total"] == pytest.approx(4.36 + 0.3)
    assert totals["total_core"] == pytest.approx(5.06 + 0.3)
    assert totals["spending_txs"] == 7


def seed_cluster_dict(report, seed):
    return next(c for c in report["clusters"] if seed in c["seeds"])


def test_internal_sweep_is_not_outflow():
    chain = FakeChain(
        {
            1: [tx("a", [vin("S", 1.0), vin("W", 1.0)], [vout(0, "X", 1.9)])],
            2: [tx("b", [vin("S", 0.5), *deposits("D")], [vout(0, "W", 2.9)])],
        }
    )
    out = trace(NodeClient(chain), ["S"], max_depth=1).clusters[0].outflow
    assert out.into_service_sweeps == 0
    assert out.into_service_sweeps_core == 0  # W is in S's core cluster too
    assert out.to_outside_addresses == pytest.approx(1.9)


def test_show_prints_outflow(chain, tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(trace(NodeClient(chain), ["S", "T"], max_depth=1).to_dict()))
    assert main(["show", str(path), "--hops", "0"]) == 0
    out = capsys.readouterr().out
    assert "Value leaving the seeds' clusters" in out
    assert "4.66000000 to 5.36000000 BTC" in out
    assert "Value leaving the seeds as a whole: 4.26000000 to 4.96000000 BTC" in out


def test_entity_outflow_drops_transfers_between_its_own_clusters(chain):
    info = {"sdn_ref": "111", "sdn_name": "EXAMPLE MARKET"}
    result = trace(NodeClient(chain), ["S", "T"], max_depth=1, seed_info={"S": info, "T": info})
    (entity,) = result.entities
    assert (entity["sdn_ref"], entity["seeds"], entity["clusters"]) == ("111", 2, 2)
    out = entity["outflow"]
    # Per-cluster totals were 4.66 / 5.36; the 0.4 BTC from S to T stays inside the entry.
    assert out["total"] == pytest.approx(4.26)
    assert out["total_core"] == pytest.approx(4.96)
    assert "into_coinjoins_core" not in out
    months = entity["outflow_by_month"]
    assert sum(low for low, _ in months.values()) == pytest.approx(4.26)
    assert sum(high for _, high in months.values()) == pytest.approx(4.96)


def test_seeds_without_sdn_info_form_one_group(chain):
    (entity,) = trace(NodeClient(chain), ["S", "T"], max_depth=1).entities
    assert entity["sdn_ref"] is None
    assert entity["outflow"]["total"] == pytest.approx(4.26)
