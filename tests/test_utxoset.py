"""Reading UTXO snapshots (dumptxoutset files) and measuring them by type and age."""

import json
import random

import pytest

from btc_trace.cli import main
from btc_trace.rpc import HttpTransport, NodeClient, _one_shot
from btc_trace.schema import validate_report
from btc_trace.trace import to_date
from btc_trace.utxoset import SnapshotError, build_report, read_snapshot
from tests import snapshot as s
from tests.fakechain import FakeChain

BASE_HEIGHT = 300_000


def coins():
    """One coin of every interesting kind, at known heights and values."""
    t = [bytes([n]) * 32 for n in range(1, 12)]
    return [
        s.Coin(t[0], 0, 1, True, 50 * 10**8, s.p2pk_uncompressed()),  # early mining
        s.Coin(t[1], 0, 2, True, 50 * 10**8, s.p2pk_compressed()),
        s.Coin(t[2], 1, 1_500, False, 12_345, s.p2pkh(1)),
        s.Coin(t[2], 7, 1_500, False, 10**8, s.p2pkh(2)),  # two coins from one tx
        s.Coin(t[3], 300, 150_000, False, 2 * 10**8, s.p2sh(3)),  # vout needs 3 bytes
        s.Coin(t[4], 0, 250_000, False, 3 * 10**8, s.p2wpkh(4)),
        s.Coin(t[5], 0, 260_000, False, 4 * 10**8, s.p2wsh(5)),
        s.Coin(t[6], 0, 299_000, False, 5 * 10**8, s.p2tr(6)),
        s.Coin(t[7], 0, 299_500, False, 330, b"\x51\x02\x4e\x73"),  # anchor
        s.Coin(t[8], 0, 100, False, 1_000, s.bare_multisig()),
        s.Coin(t[9], 0, 299_900, False, 777, b"\x52\x20" + b"\x09" * 32),  # witness v2
        s.Coin(t[10], 0, BASE_HEIGHT, True, 3_125_000_000, b"\xde\xad\xbe\xef"),
    ]


@pytest.fixture
def snap(tmp_path):
    path = tmp_path / "utxo-300000.dat"
    base = bytes.fromhex(FakeChain.block_hash(BASE_HEIGHT))[::-1]
    path.write_bytes(s.write_snapshot(coins(), base_hash=base))
    return path


def test_round_trip_of_the_encoders():
    for n in (0, 1, 9, 10, 330, 12_345, 50 * 10**8, 21_000_000 * 10**8):
        from btc_trace.utxoset import decompress_amount

        assert decompress_amount(s.compress_amount(n)) == n


def test_tallies_by_type_and_the_utxo_set_hash(snap):
    stats = read_snapshot(snap)
    assert stats.header.network == "main"
    assert stats.header.coins == 12
    assert stats.header.base_hash == FakeChain.block_hash(BASE_HEIGHT)
    got = {k: (t.coins, t.sats) for k, t in stats.tallies.items()}
    assert got == {
        "pubkey": (2, 100 * 10**8),
        "pubkeyhash": (2, 10**8 + 12_345),
        "scripthash": (1, 2 * 10**8),
        "witness_v0_keyhash": (1, 3 * 10**8),
        "witness_v0_scripthash": (1, 4 * 10**8),
        "witness_v1_taproot": (1, 5 * 10**8),
        "anchor": (1, 330),
        "multisig": (1, 1_000),
        "witness_unknown": (1, 777),
        "nonstandard": (1, 3_125_000_000),
    }
    assert (stats.coinbase_p2pk.coins, stats.coinbase_p2pk.sats) == (2, 100 * 10**8)
    assert stats.max_height == BASE_HEIGHT
    assert stats.computed_hash == s.expected_hash(coins())  # includes the uncompressed key


def test_hash_matches_on_many_random_coins(tmp_path):
    rng = random.Random(7)  # noqa: S311 - repeatable test data, not cryptography
    makers = [s.p2pkh, s.p2sh, s.p2wpkh, s.p2wsh, s.p2tr]
    many = []
    for i in range(2_000):
        txid = rng.randbytes(32)
        for vout in rng.sample(range(400), rng.randint(1, 3)):
            many.append(
                s.Coin(
                    txid,
                    vout,
                    rng.randint(0, 900_000),
                    rng.random() < 0.1,
                    rng.randint(0, 21 * 10**14),
                    rng.choice(makers)(i % 256),
                )
            )
    path = tmp_path / "many.dat"
    path.write_bytes(s.write_snapshot(many))
    stats = read_snapshot(path)
    assert stats.computed_hash == s.expected_hash(many)
    assert sum(t.coins for t in stats.tallies.values()) == len(many)
    assert read_snapshot(path, verify=False).computed_hash is None


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda b: b"nope" + b[4:], "not a UTXO snapshot"),
        (lambda b: b[:5] + (3).to_bytes(2, "little") + b[7:], "version 3"),
        (lambda b: b[:-3], "ends"),
        (lambda b: b + b"\x00", "unexpected byte"),
    ],
)
def test_damaged_files_are_refused(tmp_path, snap, damage, message):
    bad = tmp_path / "bad.dat"
    bad.write_bytes(damage(snap.read_bytes()))
    with pytest.raises(SnapshotError, match=message):
        read_snapshot(bad)


def test_report_totals_dormancy_and_checks(snap):
    stats = read_snapshot(snap)
    info = {
        "coins_written": 12,
        "base_hash": FakeChain.block_hash(BASE_HEIGHT),
        "base_height": BASE_HEIGHT,
        "txoutset_hash": s.expected_hash(coins()),
    }
    report = build_report(stats, base_height=BASE_HEIGHT, expected=info)
    t = report["totals"]
    assert t["key_in_output_btc"] == pytest.approx(105.00001)  # P2PK, multisig, Taproot
    assert t["hash_only_btc"] == pytest.approx(10.00012345)
    # Created before block 300,000 - 262,800 = 37,200: the P2PK coins, the P2PKH
    # pair at 1,500 and the multisig at 100.
    assert report["dormancy"]["created_before_height"] == 37_200
    assert t["dormant_btc"] == pytest.approx(101.00013345)
    assert t["dormant_key_in_output_btc"] == pytest.approx(100.00001)
    assert report["by_type"]["P2PK"]["share_of_supply"] == pytest.approx(100 / 146.25012452)
    assert report["checks"] == {
        "coins_match_header": True,
        "computed_txoutset_hash": s.expected_hash(coins()),
        "expected_txoutset_hash": s.expected_hash(coins()),
        "hash_matches": True,
        "base_hash_matches": True,
        "coins_match_node": True,
    }
    assert validate_report(report) == []
    assert validate_report(json.loads(json.dumps(report))) == []

    wrong = dict(info, txoutset_hash="00" * 32, coins_written=13)
    checks = build_report(stats, expected=wrong)["checks"]
    assert checks["hash_matches"] is False and checks["coins_match_node"] is False


def test_utxo_stats_command_with_and_without_a_node(snap, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BTC_URL", raising=False)
    monkeypatch.delenv("BTC_FIXTURES", raising=False)
    out = tmp_path / "utxo.json"
    assert main(["utxo-stats", str(snap), "--out", str(out)]) == 0
    printed = capsys.readouterr()
    assert "no node configured" in printed.err
    assert "12 coins" in printed.out and "not checked" in printed.out
    report = json.loads(out.read_text())
    assert report["snapshot"]["date"] is None
    assert all(b["start_date"] is None for b in report["age_bins"]["bins"])

    # With the node's dump reply next to the file, and a node to date the blocks.
    snap.with_suffix(".json").write_text(
        json.dumps(
            {
                "coins_written": 12,
                "base_hash": FakeChain.block_hash(BASE_HEIGHT),
                "base_height": BASE_HEIGHT,
                "txoutset_hash": s.expected_hash(coins()),
            }
        )
    )
    fake = FakeChain({BASE_HEIGHT: []})
    monkeypatch.setenv("BTC_FIXTURES", "unused")
    monkeypatch.setattr(NodeClient, "from_env", classmethod(lambda cls, *a: cls(fake)))
    assert main(["utxo-stats", str(snap), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "UTXO set hash matches the node's:     ok" in printed
    report = json.loads(out.read_text())
    assert report["snapshot"]["date"] == to_date(FakeChain.block_time(BASE_HEIGHT))
    first = report["age_bins"]["bins"][0]
    assert first == {
        "start_height": 0,
        "start_date": "2020-01-01",
        "btc_by_type": {"multisig": 0.00001, "pubkey": 100.0},
    }
    assert main(["validate", str(out)]) == 0
    assert "valid utxo-set report" in capsys.readouterr().out
    assert main(["show", str(out)]) == 0

    # A copy that does not match the node's own hash fails loudly.
    snap.with_suffix(".json").write_text(json.dumps({"txoutset_hash": "00" * 32}))
    assert main(["utxo-stats", str(snap), "--no-dates", "--out", str(out)]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_dump_utxos_saves_the_nodes_reply(tmp_path, monkeypatch, capsys):
    reply = {
        "coins_written": 12,
        "base_hash": "ab" * 32,
        "base_height": 969_741,
        "path": "/data/utxo-969741.dat",
        "txoutset_hash": "cd" * 32,
        "nchaintx": 1,
    }

    class Node:
        def __init__(self):
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return 969_741 if method == "getblockcount" else reply

    node = Node()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(NodeClient, "from_env", classmethod(lambda cls, *a: cls(node)))
    assert main(["dump-utxos"]) == 0
    assert node.calls[-1] == ("dumptxoutset", ["utxo-969741.dat", "latest"])
    assert json.loads((tmp_path / "data" / "utxo-969741.json").read_text()) == reply
    assert "btc-trace utxo-stats data/utxo-969741.dat" in capsys.readouterr().out


def test_dump_is_never_retried_and_waits_long():
    live = HttpTransport("https://node.invalid/", "u", "p", timeout=900)
    once = _one_shot(live, 7200)
    assert (once.retries, once.timeout) == (1, 7200)
    assert live.retries == 4  # the normal connection is unchanged
