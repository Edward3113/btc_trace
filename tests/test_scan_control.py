import pytest

from btc_trace.cli import main
from btc_trace.rpc import NodeClient, RpcError


class StubTransport:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return self.behavior(method, params)


def test_scan_in_progress_gives_clear_message():
    def busy(method, params):
        raise RpcError(
            "scanblocks: {'code': -8, 'message': 'Scan already in progress, use action "
            '"abort" or "status"\'}'
        )

    with pytest.raises(RpcError, match="scan-abort"):
        NodeClient(StubTransport(busy)).scan_blocks(["A"], 0, 10)


def test_interrupt_aborts_scan_on_node():
    def interrupted(method, params):
        if params[0] == "start":
            raise KeyboardInterrupt
        return True

    transport = StubTransport(interrupted)
    with pytest.raises(KeyboardInterrupt):
        NodeClient(transport).scan_blocks(["A"], 0, 10)
    assert transport.calls[-1] == ("scanblocks", ["abort"])


def test_status_and_abort_helpers():
    replies = {"status": {"progress": 42, "current_height": 400000}, "abort": True}
    client = NodeClient(StubTransport(lambda m, p: replies[p[0]]))
    assert client.scan_status()["progress"] == 42
    assert client.abort_scan() is True


def test_scan_commands(monkeypatch, capsys):
    replies = {"status": None, "abort": False}
    stub = StubTransport(lambda m, p: replies[p[0]])
    monkeypatch.setattr(NodeClient, "from_env", classmethod(lambda cls, *a: cls(stub)))
    assert main(["scan-status"]) == 0
    assert main(["scan-abort"]) == 0
    out = capsys.readouterr().out
    assert "no block scan is running" in out
    assert "no block scan was running" in out


def test_scan_is_split_into_chunks():
    from tests.fakechain import FakeChain
    from tests.txdata import tx, vin, vout

    chain = FakeChain(
        {h: [tx(f"t{h}", [vin("X", 1.0)], [vout(0, "A", 0.9)])] for h in (5, 12, 25, 26)}
    )
    ranges = []
    client = NodeClient(chain, scan_chunk=10)
    blocks = client.scan_blocks(["A"], 0, 26, progress=lambda lo, hi: ranges.append((lo, hi)))
    assert ranges == [(0, 9), (10, 19), (20, 26)]
    assert [int(b, 16) for b in blocks] == [5, 12, 25, 26]


def test_timeout_stops_scan_on_node():
    def slow(method, params):
        if params[0] == "start":
            raise RpcError("scanblocks timed out after 900s; raise BTC_TIMEOUT or narrow the scan")
        return True

    transport = StubTransport(slow)
    with pytest.raises(RpcError, match="BTC_SCAN_CHUNK"):
        NodeClient(transport).scan_blocks(["A"], 0, 10)
    assert transport.calls[-1] == ("scanblocks", ["abort"])


def test_scan_chunk_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_FIXTURES", str(tmp_path))
    monkeypatch.setenv("BTC_SCAN_CHUNK", "5000")
    assert NodeClient.from_env().scan_chunk == 5000
