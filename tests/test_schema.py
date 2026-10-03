"""Every report the tracer produces matches the published schema, and bad ones fail."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from btc_trace.cli import main
from btc_trace.rpc import NodeClient
from btc_trace.schema import REPORT_VERSION, load_schema, validate_report
from btc_trace.trace import trace
from tests import test_outflow, test_report_details, test_service_and_clusters, test_trace
from tests.fakechain import FakeChain
from tests.txdata import tx, vin, vout

INFO = {"sdn_ref": "111", "sdn_name": "EXAMPLE MARKET"}


class UndatedChain(FakeChain):
    """Blocks without times, as when a node reply lacks them: dates become null."""

    def call(self, method, params):
        result = super().call(method, params)
        if method in ("getblock", "getblockheader"):
            result = {k: v for k, v in result.items() if k != "time"}
        return result


def scenarios():
    yield "coinjoin and dust", test_trace.chain.__wrapped__(), ["S"], 3, {}
    yield "sweeps and clusters", test_service_and_clusters.chain.__wrapped__(), ["S"], 3, {}
    yield (
        "internal sweeps and batch",
        test_report_details.chain.__wrapped__(),
        ["S", "T"],
        1,
        {"S": INFO},
    )
    yield "outflow", test_outflow.chain.__wrapped__(), ["S", "T"], 1, {"S": INFO, "T": INFO}
    yield "never used", FakeChain({1: []}), ["NEVER_USED"], 2, {}
    undated = UndatedChain({5: [tx("a", [vin("S", 1.0)], [vout(0, "X", 0.9)])]})
    yield "no block times", undated, ["S"], 1, {}


@pytest.fixture(params=list(scenarios()), ids=lambda s: s[0])
def report(request):
    _, chain, seeds, depth, info = request.param
    return trace(NodeClient(chain), seeds, max_depth=depth, seed_info=info).to_dict()


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(load_schema())


def test_every_scenario_produces_a_valid_report(report):
    assert report["report_version"] == REPORT_VERSION
    assert validate_report(report) == []


def test_reports_round_trip_through_json(report):
    assert validate_report(json.loads(json.dumps(report))) == []


@pytest.fixture
def valid():
    _, chain, seeds, depth, info = next(s for s in scenarios() if s[0] == "outflow")
    return trace(NodeClient(chain), seeds, max_depth=depth, seed_info=info).to_dict()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda r: r["hops"][0].update(label="maybe change"), "report.hops[0].label"),
        (lambda r: r["hops"][0].pop("txid"), "'txid' is a required property"),
        (lambda r: r.update(surprise=1), "Additional properties are not allowed"),
        (lambda r: r["hops"][0].update(date="05/04/2022"), "report.hops[0].date"),
        (lambda r: r["hops"][0].update(value_btc=-1), "report.hops[0].value_btc"),
        (lambda r: r["levels"][0].update(depth=0), "report.levels[0].depth"),
        (lambda r: r["clusters"][0]["evidence"][0].update(kind="vibes"), "evidence[0].kind"),
    ],
)
def test_mistakes_are_reported_with_their_location(valid, change, expected):
    broken = copy.deepcopy(valid)
    change(broken)
    problems = validate_report(broken)
    assert problems and any(expected in p for p in problems), problems


def test_old_reports_get_one_clear_message(valid):
    old = {k: v for k, v in valid.items() if k != "report_version"}
    assert validate_report(old) == [
        "report has no report_version: it was made by an older btc_trace. "
        "Run `btc-trace trace` again to produce a current report."
    ]
    newer = dict(valid, report_version="9.9")
    assert "is not supported" in validate_report(newer)[0]


def test_validate_command(valid, tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(valid))
    assert main(["validate", str(good)]) == 0
    assert "valid trace report (version 1.0)" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    broken = copy.deepcopy(valid)
    broken["hops"][0]["label"] = "nope"
    bad.write_text(json.dumps(broken))
    assert main(["validate", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "INVALID" in err and "report.hops[0].label" in err


def test_report_command_refuses_invalid_input(valid, tmp_path, capsys):
    old = tmp_path / "old.json"
    old.write_text(json.dumps({k: v for k, v in valid.items() if k != "report_version"}))
    out = tmp_path / "page.html"
    assert main(["report", str(old), "--out", str(out)]) == 1
    assert "older btc_trace" in capsys.readouterr().err
    assert not out.exists()


def test_report_command_can_skip_validation(valid, tmp_path):
    src = tmp_path / "r.json"
    src.write_text(json.dumps(valid))
    out = tmp_path / "page.html"
    assert main(["report", str(src), "--out", str(out), "--no-validate"]) == 0
    assert out.exists()
