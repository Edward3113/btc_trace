from btc_trace.heuristics import (
    common_input_clusters,
    detect_change,
    looks_like_coinjoin,
    to_sats,
)
from tests.txdata import COINBASE, tx, vin, vout


def test_to_sats_handles_float_noise():
    assert to_sats(0.1 + 0.2) == 30_000_000


def test_common_input_ownership_merges_transitively():
    txs = [
        tx("t1", [vin("A", 1.0), vin("B", 1.0)], [vout(0, "X", 1.9)]),
        tx("t2", [vin("B", 0.5), vin("C", 0.5)], [vout(0, "Y", 0.9)]),
        tx("t3", [vin("D", 2.0)], [vout(0, "Z", 1.9)]),
    ]
    result = common_input_clusters(txs)
    assert result.clusters == [{"A", "B", "C"}, {"D"}]
    assert result.skipped_coinjoin == []


def test_coinbase_is_ignored():
    assert common_input_clusters([COINBASE]).clusters == []


def coinjoin():
    inputs = [vin(f"in{i}", 0.11) for i in range(5)]
    outputs = [vout(i, f"out{i}", 0.1) for i in range(5)]
    return tx("cj", inputs, outputs)


def test_coinjoin_detected_and_skipped():
    assert looks_like_coinjoin(coinjoin())
    result = common_input_clusters([coinjoin()])
    assert result.clusters == []
    assert result.skipped_coinjoin == ["cj"]


def test_ordinary_batch_payment_is_not_coinjoin():
    batch = tx("b", [vin("A", 5.0)], [vout(i, f"o{i}", 1.0) for i in range(4)])
    assert not looks_like_coinjoin(batch)


def test_change_by_address_reuse():
    t = tx("t", [vin("A", 1.0)], [vout(0, "P", 0.5), vout(1, "A", 0.49)])
    guess = detect_change(t)
    assert guess is not None
    assert (guess.vout, guess.address) == (1, "A")


def test_change_by_script_type_and_round_payment():
    t = tx(
        "t",
        [vin("A", 1.0, "witness_v0_keyhash")],
        [vout(0, "P", 0.25, "pubkeyhash"), vout(1, "C", 0.74981234, "witness_v0_keyhash")],
    )
    guess = detect_change(t)
    assert guess is not None
    assert guess.vout == 1
    assert len(guess.reasons) == 2


def test_conflicting_signals_give_no_guess():
    # Script type points at output 0; the round amount points at output 1.
    t = tx(
        "t",
        [vin("A", 1.0, "witness_v0_keyhash")],
        [vout(0, "P", 0.3, "witness_v0_keyhash"), vout(1, "C", 0.6999, "pubkeyhash")],
    )
    assert detect_change(t) is None


def test_no_guess_without_two_outputs():
    t = tx("t", [vin("A", 1.0)], [vout(0, "P", 0.9)])
    assert detect_change(t) is None
