import io

from btc_trace.progress import ProgressDisplay
from btc_trace.rpc import NodeClient
from btc_trace.trace import trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


def test_terminal_bar_redraws_in_place():
    term = FakeTerminal()
    display = ProgressDisplay(term, width=10)
    display.bar("fetching", 0, 4, "0 of 4 blocks")
    display.bar("fetching", 4, 4, "4 of 4 blocks")
    out = term.getvalue()
    assert out.count("\r") == 2  # each update returns to the start of the same line
    assert "[░░░░░░░░░░]   0%  0 of 4 blocks" in out
    assert "[██████████] 100%  4 of 4 blocks" in out
    assert out.endswith("\n")  # a finished bar ends its line


def test_messages_start_on_a_fresh_line():
    term = FakeTerminal()
    display = ProgressDisplay(term, width=4)
    display.bar("scanning", 1, 2, "block 1 of 2")
    display.message("depth 2: 3 address(es)")
    assert "block 1 of 2\033[K\ndepth 2" in term.getvalue()


def test_redirected_output_prints_one_line_per_ten_percent():
    log = io.StringIO()
    display = ProgressDisplay(log)
    for i in range(1, 1001):
        display.bar("fetching", i, 1000, f"{i} of 1000 blocks")
    lines = log.getvalue().splitlines()
    assert len(lines) == 11  # 0%..90% steps plus the final 100%
    assert lines[-1].endswith("1000 of 1000 blocks")


def test_trace_reports_bar_progress():
    blocks = {h: [tx(f"in{h}", [vin(f"F{h}", 1.0)], [vout(0, "S", 0.5)])] for h in range(1, 6)}
    blocks[9] = [tx("spend", [vin("S", 0.5)], [vout(0, "P", 0.49)])]
    events = []
    trace(
        NodeClient(FakeChain(blocks)),
        ["S"],
        max_depth=1,
        bar=lambda label, done, total, detail: events.append((label, done, total, detail)),
    )
    assert events[-1] == ("fetching", 6, 6, "6 of 6 blocks")
    assert events[0] == ("scanning", 0, 10, "block 0 of 9")  # blocks 0..9
    assert ("scanning", 10, 10, "block 9 of 9") in events
