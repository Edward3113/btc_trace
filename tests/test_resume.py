"""Interrupted traces resume from saved progress instead of starting over."""

import pytest

from btc_trace.rpc import NodeClient, RpcError
from btc_trace.trace import trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout


class CrashAfter:
    """Wraps a chain and fails the Nth getblock, like a dropped connection."""

    def __init__(self, chain, fail_on):
        self.chain = chain
        self.fail_on = fail_on
        self.getblocks = 0

    def call(self, method, params):
        if method == "getblock":
            self.getblocks += 1
            if self.getblocks == self.fail_on:
                raise RpcError("getblock: connection to the node kept dropping")
        return self.chain.call(method, params)


def busy_chain():
    """Seed S receives in 20 blocks, then spends once."""
    blocks = {h: [tx(f"in{h}", [vin(f"F{h}", 1.0)], [vout(0, "S", 0.5)])] for h in range(1, 21)}
    blocks[30] = [tx("spend", [vin("S", 0.5)], [vout(0, "P", 0.3), vout(1, "C", 0.1999)])]
    return FakeChain(blocks)


def test_resume_fetches_only_remaining_blocks(tmp_path):
    expected = trace(NodeClient(busy_chain()), ["S"], max_depth=1, workers=1).to_dict()

    crashing = CrashAfter(busy_chain(), fail_on=12)
    with pytest.raises(RpcError):
        trace(NodeClient(crashing), ["S"], max_depth=1, workers=1, cache_dir=tmp_path)

    resumed_chain = CrashAfter(busy_chain(), fail_on=0)
    resumed = trace(
        NodeClient(resumed_chain), ["S"], max_depth=1, workers=1, cache_dir=tmp_path
    ).to_dict()
    assert resumed == expected
    assert resumed_chain.getblocks == 21 - 11  # only the blocks not saved before the crash
    scans = [m for m, _ in resumed_chain.chain.calls if m == "scanblocks"]
    assert scans == []  # the saved scan was reused


def test_complete_rerun_needs_no_block_fetches(tmp_path):
    first = trace(NodeClient(busy_chain()), ["S"], max_depth=1, cache_dir=tmp_path).to_dict()
    again_chain = busy_chain()
    again = trace(NodeClient(again_chain), ["S"], max_depth=1, cache_dir=tmp_path).to_dict()
    assert again == first
    assert [m for m, _ in again_chain.calls if m in ("getblock", "scanblocks")] == []


def test_saved_scan_is_extended_to_new_blocks(tmp_path):
    chain = busy_chain()
    trace(NodeClient(chain), ["S"], max_depth=1, cache_dir=tmp_path)
    chain.blocks[40] = [tx("later", [vin("S", 0.5)], [vout(0, "Q", 0.49)])]
    result = trace(NodeClient(chain), ["S"], max_depth=1, cache_dir=tmp_path)
    assert {h.txid for h in result.hops} == {"spend", "later"}
    last_scan = [p for m, p in chain.calls if m == "scanblocks"][-1]
    assert last_scan[2] == 31  # only the blocks after the saved scan


def test_truncated_line_is_ignored(tmp_path):
    trace(NodeClient(busy_chain()), ["S"], max_depth=1, cache_dir=tmp_path)
    (jsonl,) = tmp_path.glob("*.blocks.jsonl")
    with jsonl.open("a") as f:
        f.write('{"hash": "cut off mid-wri')
    result = trace(NodeClient(busy_chain()), ["S"], max_depth=1, cache_dir=tmp_path)
    assert {h.txid for h in result.hops} == {"spend"}
