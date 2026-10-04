"""Finding addresses whose key or script an earlier spend revealed."""

import hashlib
import json
import threading
from argparse import Namespace
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from btc_trace.cli import _cmd_reveal_scan, main
from btc_trace.rawscan import KeepAliveClient, RpcBlockSource, scan_chain
from btc_trace.reveal import (
    BlockCounts,
    block_inputs,
    block_reveal_prefixes,
    hash160,
    input_reveals,
    match,
    prefix,
)
from btc_trace.schema import validate_report
from tests import snapshot as s
from tests.rawtx import (
    KEY1,
    KEY1_FULL,
    KEY2,
    SCHNORR,
    SIG,
    RawSource,
    block,
    multisig,
    push,
    tx,
)

# Known addresses of private key 1 (public test vectors).
KEY1_HASH = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")  # 1BgGZ9tc… / bc1qw508…
KEY1_FULL_HASH = bytes.fromhex("91b24bf9f5288532960ac687abb035127b1d28a5")  # 1EHNa6Q4…
KEY1_WRAPPED = bytes.fromhex("bcfeb728b584253d5f3f70bcb780e9ef218a68f4")  # 3JvL6Ymt…


def reveals(script_sig=b"", witness=()):
    out = []
    input_reveals(script_sig, list(witness), out)
    return out


def test_known_address_hashes():
    assert hash160(KEY1) == KEY1_HASH
    assert hash160(KEY1_FULL) == KEY1_FULL_HASH
    assert hash160(b"\x00\x14" + KEY1_HASH) == KEY1_WRAPPED


def test_p2pkh_spend_reveals_the_keys_addresses():
    assert reveals(push(SIG) + push(KEY1)) == [KEY1_HASH, KEY1_WRAPPED]
    # An uncompressed key also exposes the compressed form's addresses: same key.
    assert reveals(push(SIG) + push(KEY1_FULL)) == [KEY1_FULL_HASH, KEY1_HASH, KEY1_WRAPPED]


def test_p2wpkh_and_wrapped_spends():
    assert reveals(witness=[SIG, KEY1]) == [KEY1_HASH, KEY1_WRAPPED]
    assert reveals(push(b"\x00\x14" + KEY1_HASH), [SIG, KEY1]) == [KEY1_HASH, KEY1_WRAPPED]


def test_p2sh_multisig_reveals_script_and_keys():
    redeem = multisig(KEY1, KEY2)
    got = reveals(b"\x00" + push(SIG) + push(redeem))
    assert got[0] == hash160(redeem)
    assert KEY1_HASH in got and hash160(KEY2) in got


def test_p2wsh_and_wrapped_p2wsh():
    script = multisig(KEY1, KEY2)
    program = hashlib.sha256(script).digest()
    got = reveals(witness=[b"", SIG, script])
    assert got[0] == program and KEY1_HASH in got
    wrapped = reveals(push(b"\x00\x20" + program), [b"", SIG, script])
    assert wrapped[:2] == [program, hash160(b"\x00\x20" + program)]


def test_spends_that_reveal_nothing_new():
    assert reveals(push(SIG)) == []  # P2PK: the key is already in the output
    assert reveals(witness=[SCHNORR]) == []  # Taproot key path
    control = b"\xc0" + b"\x44" * 32
    assert reveals(witness=[SCHNORR, b"\x20" + b"\x44" * 32 + b"\xac", control]) == []
    assert reveals() == []


def test_block_parsing_skips_the_coinbase_and_handles_mixed_inputs():
    legacy = tx([(push(SIG) + push(KEY1), [])])
    mixed = tx([(push(SIG) + push(KEY1_FULL), []), (b"", [SIG, KEY2])], n_outputs=3)
    raw = block(legacy, mixed)
    inputs = list(block_inputs(raw))
    assert [w for _, w in inputs] == [[], [], [SIG, KEY2]]
    found = array("Q")
    counts = BlockCounts()
    block_reveal_prefixes(raw, found, counts)
    assert counts.inputs == 3
    assert prefix(KEY1_HASH) in found and prefix(hash160(KEY2)) in found
    with pytest.raises(ValueError, match="parse ended"):
        list(block_inputs(raw + b"\x00"))


def test_match_keeps_only_targets():
    targets = np.array(sorted([5, 9, 2**64 - 1]), dtype=np.uint64)
    assert match(array("Q", [9, 9, 4, 2**64 - 1]), targets).tolist() == [9, 2**64 - 1]
    assert match(array("Q"), targets).size == 0


# --- whole pipeline: snapshot targets, chain scan, revealed tallies ---------------


def snapshot_coins():
    t = [bytes([n]) * 32 for n in range(1, 8)]
    redeem = multisig(KEY1, KEY2)
    return [
        s.Coin(t[0], 0, 10, False, 10**8, s.p2pkh(0)[:3] + KEY1_HASH + b"\x88\xac"),  # reused
        s.Coin(t[1], 0, 11, False, 2 * 10**8, b"\x00\x14" + KEY1_HASH),  # same key, P2WPKH
        s.Coin(t[2], 0, 12, False, 3 * 10**8, s.p2wpkh(9)),  # never spent from
        s.Coin(t[3], 0, 13, False, 4 * 10**8, b"\xa9\x14" + hash160(redeem) + b"\x87"),
        s.Coin(t[4], 0, 14, False, 5 * 10**8, s.p2tr(3)),
        s.Coin(t[5], 0, 15, False, 6 * 10**8, s.p2wsh(4)),  # never revealed
        s.Coin(t[6], 0, 2_500, False, 7 * 10**8, b"\x00\x14" + hash160(KEY2)),  # via multisig
    ]


def chain():
    return RawSource(
        {
            3: block(tx([(push(SIG) + push(KEY1), [])])),
            7: block(tx([(b"\x00" + push(SIG) + push(multisig(KEY1, KEY2)), [])])),
            9: block(tx([(b"", [SCHNORR])])),
        }
    )


@pytest.fixture
def snap(tmp_path):
    path = tmp_path / "utxo-2600.dat"
    base = bytes.fromhex(RawSource.hash_of(2_600))[::-1]
    path.write_bytes(s.write_snapshot(snapshot_coins(), base_hash=base))
    return path


def run_scan(snap, tmp_path, source=None, workers=0):
    from btc_trace.utxoset import hash_targets

    targets = tmp_path / "targets.npy"
    np.save(targets, hash_targets(snap))
    return scan_chain(
        2_600, targets, tmp_path / "cache", chunk=1000, workers=workers, source=source
    )


def test_targets_are_the_hash_based_coins(snap):
    from btc_trace.utxoset import hash_targets

    targets = hash_targets(snap)
    assert targets.size == 5  # KEY1's hash appears twice; Taproot is not a target
    assert prefix(KEY1_HASH) in targets.tolist()


def test_scan_and_revealed_tallies(snap, tmp_path):
    from btc_trace.utxoset import build_report, read_snapshot

    source = chain()
    matched, totals = run_scan(snap, tmp_path, source)
    assert totals.blocks == 2_601 and totals.inputs == 3
    assert matched.tolist() == sorted(
        [prefix(KEY1_HASH), prefix(hash160(multisig(KEY1, KEY2))), prefix(hash160(KEY2))]
    )
    stats = read_snapshot(snap, revealed=matched)
    got = {k: (t.coins, t.sats) for k, t in stats.revealed.items()}
    assert got == {
        "pubkeyhash": (1, 10**8),
        "scripthash": (1, 4 * 10**8),
        "witness_v0_keyhash": (2, 9 * 10**8),  # 2 BTC via KEY1's P2PKH spend, 7 via multisig
        "witness_v0_scripthash": (0, 0),
    }
    report = build_report(stats, base_height=2_600, reuse=None)
    t = report["totals"]
    assert t["revealed_btc"] == pytest.approx(14.0)
    assert t["exposed_btc"] == pytest.approx(19.0)  # plus the Taproot coin
    assert validate_report(report) == []

    again = RawSource(source.blocks)
    run_scan(snap, tmp_path, again)
    assert again.fetched == []  # every range was saved


def test_reveal_scan_and_utxo_stats_commands(snap, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BTC_URL", raising=False)
    monkeypatch.delenv("BTC_FIXTURES", raising=False)
    args = Namespace(
        snapshot=snap, workers=1, chunk=1000, cache_dir=tmp_path / "cache", out=None, source=chain
    )
    assert _cmd_reveal_scan(args) == 0
    revealed = snap.with_name("utxo-2600.revealed.npy")
    summary = json.loads(revealed.with_suffix(".json").read_text())
    assert summary["matched_addresses"] == 3 and summary["scan_height"] == 2_600
    assert snap.with_name("utxo-2600.targets.npy").exists()
    capsys.readouterr()

    out = tmp_path / "report.json"
    assert main(["utxo-stats", str(snap), "--revealed", str(revealed), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "revealed by an earlier spend" in printed and "key visible in total" in printed
    report = json.loads(out.read_text())
    assert report["reuse"]["matched_addresses"] == 3
    assert report["by_type"]["P2WPKH"]["revealed_btc"] == pytest.approx(9.0)
    assert validate_report(report) == []

    other = tmp_path / "other.dat"
    other.write_bytes(s.write_snapshot(snapshot_coins(), base_hash=b"\x22" * 32))
    assert main(["utxo-stats", str(other), "--revealed", str(revealed)]) == 1
    assert "different snapshot" in capsys.readouterr().err


# --- the live path: a local JSON-RPC server, kept-alive connections, worker processes


class FakeNode(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like Bitcoin Core
    wbufsize = 1 << 16  # send headers and body together, as Bitcoin Core does
    source = chain()
    drop_next = {"count": 0}

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.drop_next["count"]:
            self.drop_next["count"] -= 1
            self.close_connection = True
            self.connection.shutdown(2)
            return
        replies = [self.answer(r) for r in body] if isinstance(body, list) else self.answer(body)
        data = json.dumps(replies).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def answer(self, request):
        method, params = request["method"], request["params"]
        if method == "getblockhash":
            result = RawSource.hash_of(params[0])
        elif method == "getblock":
            result = self.source.raw_block(params[0]).hex()
        elif method == "getblockheader":
            result = {"height": int(params[0], 16)}
        else:
            return {"id": request["id"], "result": None, "error": {"message": "no"}}
        return {"id": request["id"], "result": result, "error": None}


@pytest.fixture
def node(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeNode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BTC_URL", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setenv("BTC_USER", "u")
    monkeypatch.setenv("BTC_PASS", "p")
    yield server
    server.shutdown()


def test_keepalive_client_batches_and_reconnects(node):
    client = KeepAliveClient.from_env()
    source = RpcBlockSource(client)
    assert source.block_hashes(0, 2_500)[2_500] == RawSource.hash_of(2_500)
    first = client.conn
    assert source.raw_block(RawSource.hash_of(3)) == chain().raw_block(RawSource.hash_of(3))
    assert client.conn is first  # the same connection was reused
    FakeNode.drop_next["count"] = 1
    client.retries, client.retry_delay = 2, 0.01
    assert source.raw_block(RawSource.hash_of(7)) == chain().raw_block(RawSource.hash_of(7))


def test_worker_processes_read_from_the_node(node, snap, tmp_path):
    matched, totals = run_scan(snap, tmp_path, source=None, workers=2)
    assert totals.blocks == 2_601
    assert prefix(KEY1_HASH) in matched.tolist() and matched.size == 3
