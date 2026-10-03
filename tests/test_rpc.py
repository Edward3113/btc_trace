import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from btc_trace.rpc import (
    FixtureTransport,
    HttpTransport,
    NodeClient,
    RecordingTransport,
    RpcError,
    fixture_name,
)

USER, PASSWORD = "tester", "s3cret"


class _FakeNode(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server naming
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["method"] == "getblockcount":
            body = {"result": 968920, "error": None, "id": request["id"]}
            status = 200
        else:
            body = {"result": None, "error": {"code": -32601, "message": "Method not found"}}
            status = 404
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_node_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeNode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def test_http_transport_success(fake_node_url):
    assert HttpTransport(fake_node_url, USER, PASSWORD).call("getblockcount", []) == 968920


def test_http_transport_bad_credentials(fake_node_url):
    with pytest.raises(RpcError, match="401"):
        HttpTransport(fake_node_url, USER, "wrong").call("getblockcount", [])


def test_http_transport_rpc_error(fake_node_url):
    with pytest.raises(RpcError, match="Method not found"):
        HttpTransport(fake_node_url, USER, PASSWORD).call("nosuchmethod", [])


def test_http_transport_unreachable():
    with pytest.raises(RpcError, match="could not reach node"):
        HttpTransport("http://127.0.0.1:1/", USER, PASSWORD, timeout=2).call("getblockcount", [])


def test_password_not_in_repr():
    assert PASSWORD not in repr(HttpTransport("http://x/", USER, PASSWORD))


def test_fixture_name():
    assert fixture_name("getblockcount", []) == "getblockcount.json"
    assert fixture_name("getrawtransaction", ["ab" * 32, 2]) == (
        "getrawtransaction__" + "ab" * 32 + "_2.json"
    )


def test_record_then_replay(tmp_path, fake_node_url):
    live = HttpTransport(fake_node_url, USER, PASSWORD)
    assert RecordingTransport(live, tmp_path).call("getblockcount", []) == 968920
    assert FixtureTransport(tmp_path).call("getblockcount", []) == 968920


def test_missing_fixture(tmp_path):
    with pytest.raises(RpcError, match="no fixture"):
        FixtureTransport(tmp_path).call("getblockcount", [])


def test_from_env_prefers_fixtures(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_FIXTURES", str(tmp_path))
    assert isinstance(NodeClient.from_env().transport, FixtureTransport)


def test_from_env_reports_missing_settings(monkeypatch):
    for var in ("BTC_FIXTURES", "BTC_URL", "BTC_USER", "BTC_PASS"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RpcError, match="BTC_URL"):
        NodeClient.from_env()


class _FlakyNode(BaseHTTPRequestHandler):
    """Cuts the first replies short, like a connection dropping mid-transfer."""

    failures_left = 0

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        data = json.dumps({"result": 7, "error": None, "id": "x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if _FlakyNode.failures_left > 0:
            _FlakyNode.failures_left -= 1
            self.wfile.write(data[:5])  # promise more bytes than we send, then hang up
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def flaky_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyNode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def test_retries_after_dropped_connection(flaky_url):
    _FlakyNode.failures_left = 2
    transport = HttpTransport(flaky_url, USER, PASSWORD, retries=4, retry_delay=0.01)
    assert transport.call("getblockcount", []) == 7


def test_gives_up_after_repeated_drops(flaky_url):
    _FlakyNode.failures_left = 10
    transport = HttpTransport(flaky_url, USER, PASSWORD, retries=3, retry_delay=0.01)
    with pytest.raises(RpcError, match="kept dropping"):
        transport.call("getblockcount", [])
