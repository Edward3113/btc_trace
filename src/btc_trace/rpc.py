"""Minimal Bitcoin Core JSON-RPC client.

Two transports share one interface:

* ``HttpTransport`` talks to a live node. Connection details come from the
  environment (``BTC_URL``, ``BTC_USER``, ``BTC_PASS``) and are never written to disk.
* ``FixtureTransport`` replays saved JSON replies from a directory, so tests, CI and
  anyone cloning the repo can run everything without a node.

``RecordingTransport`` wraps a live transport and saves each reply as a fixture.
Fixtures hold only public blockchain data, which is identical on every node.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import truststore


class RpcError(RuntimeError):
    """Raised when the node cannot be reached or returns an error."""


class _DroppedConnection(Exception):
    """The connection failed mid-request; safe to retry a read-only call."""


class Transport(Protocol):
    def call(self, method: str, params: list[Any]) -> Any: ...


def _ssl_context() -> ssl.SSLContext:
    """Trust a CA file from BTC_CA_CERT if given, otherwise the OS trust store.

    StartOS serves LAN interfaces with certificates signed by its own root CA.
    Once that CA is trusted in macOS Keychain, the OS trust store covers it.
    """
    cafile = os.environ.get("BTC_CA_CERT")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


@dataclass
class HttpTransport:
    url: str
    user: str
    password: str = field(repr=False)
    timeout: float = 60.0
    retries: int = 4  # attempts per call when the connection drops
    retry_delay: float = 2.0  # seconds, doubled after each failed attempt

    def call(self, method: str, params: list[Any]) -> Any:
        """Call the node, retrying when the connection drops mid-request.

        Every call this tool makes only reads from the node, so repeating one is safe.
        """
        delay = self.retry_delay
        for attempt in range(1, self.retries + 1):
            try:
                return self._call_once(method, params)
            except _DroppedConnection as exc:
                if attempt == self.retries:
                    raise RpcError(
                        f"{method}: connection to the node kept dropping "
                        f"({exc.__cause__!r}); gave up after {attempt} attempts"
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise AssertionError("unreachable")

    def _call_once(self, method: str, params: list[Any]) -> Any:
        body = json.dumps(
            {"jsonrpc": "1.0", "id": "btc_trace", "method": method, "params": params}
        ).encode()
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        request = urllib.request.Request(  # noqa: S310 - URL is the user's own node
            self.url,
            data=body,
            headers={"Content-Type": "text/plain", "Authorization": f"Basic {token}"},
        )
        context = _ssl_context() if self.url.startswith("https") else None
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout, context=context
            ) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            # Bitcoin Core sends JSON error bodies with 4xx/5xx codes; 401 has no body.
            if exc.code == 401:
                raise RpcError("node rejected the RPC credentials (HTTP 401)") from exc
            try:
                payload = json.load(exc)
            except (json.JSONDecodeError, ValueError):
                raise RpcError(f"HTTP {exc.code} from node for {method}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ConnectionError) and not isinstance(
                exc.reason, ConnectionRefusedError
            ):
                raise _DroppedConnection from exc
            message = f"could not reach node: {exc.reason}"
            if "broken key size" in str(exc.reason):
                # macOS's trust store rejects some certificate key types and curves
                # that OpenSSL accepts. Verifying against the CA file uses OpenSSL.
                message += (
                    "\nmacOS rejected the certificate's key type or curve. Set BTC_CA_CERT to your "
                    "StartOS root CA file so Python verifies it with OpenSSL instead."
                )
            raise RpcError(message) from exc
        except TimeoutError as exc:
            raise RpcError(
                f"{method} timed out after {self.timeout:.0f}s; "
                "raise BTC_TIMEOUT or narrow the scan"
            ) from exc
        except ConnectionRefusedError as exc:
            raise RpcError(f"could not reach node: {exc}") from exc
        except (http.client.HTTPException, ConnectionError) as exc:
            # e.g. IncompleteRead or a reset partway through a large getblock reply
            raise _DroppedConnection from exc

        if payload.get("error"):
            raise RpcError(f"{method}: {payload['error']}")
        return payload["result"]


def fixture_name(method: str, params: list[Any]) -> str:
    """Stable file name for a call, e.g. getrawtransaction__<txid>_2.json.

    Calls with long parameters (such as scanblocks with many descriptors) use a hash
    of the parameters instead, to stay within file name limits.
    """
    name = method
    if params:
        name += "__" + "_".join(str(p) for p in params)
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    if len(name) > 150:
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        name = f"{method}__{digest}"
    return name + ".json"


@dataclass
class FixtureTransport:
    directory: Path

    def call(self, method: str, params: list[Any]) -> Any:
        path = Path(self.directory) / fixture_name(method, params)
        if not path.exists():
            raise RpcError(f"no fixture for {method} {params} (expected {path.name})")
        return json.loads(path.read_text())


@dataclass
class RecordingTransport:
    inner: Transport
    directory: Path

    def call(self, method: str, params: list[Any]) -> Any:
        result = self.inner.call(method, params)
        out = Path(self.directory)
        out.mkdir(parents=True, exist_ok=True)
        (out / fixture_name(method, params)).write_text(json.dumps(result, indent=2) + "\n")
        return result


DEFAULT_SCAN_CHUNK = 25_000  # blocks per scanblocks call


class NodeClient:
    """Typed helpers over a transport."""

    def __init__(self, transport: Transport, scan_chunk: int = DEFAULT_SCAN_CHUNK) -> None:
        self.transport = transport
        self.scan_chunk = max(1, scan_chunk)

    @classmethod
    def from_env(cls, record_dir: Path | None = None) -> NodeClient:
        """Build a client from the environment.

        BTC_FIXTURES=<dir> selects offline replay; otherwise BTC_URL, BTC_USER and
        BTC_PASS select a live node.
        """
        fixtures = os.environ.get("BTC_FIXTURES")
        scan_chunk = int(os.environ.get("BTC_SCAN_CHUNK", DEFAULT_SCAN_CHUNK))
        if fixtures:
            return cls(FixtureTransport(Path(fixtures)), scan_chunk)

        missing = [v for v in ("BTC_URL", "BTC_USER", "BTC_PASS") if not os.environ.get(v)]
        if missing:
            raise RpcError(
                "set BTC_FIXTURES for offline mode, or " + ", ".join(missing) + " for a live node"
            )
        transport: Transport = HttpTransport(
            os.environ["BTC_URL"],
            os.environ["BTC_USER"],
            os.environ["BTC_PASS"],
            timeout=float(os.environ.get("BTC_TIMEOUT", "900")),
        )
        if record_dir is not None:
            transport = RecordingTransport(transport, record_dir)
        return cls(transport, scan_chunk)

    def call(self, method: str, *params: Any) -> Any:
        return self.transport.call(method, list(params))

    def get_transaction(self, txid: str) -> dict[str, Any]:
        """Decoded transaction with each input's previous output (verbosity 2).

        Confirmed transactions need txindex on the node.
        """
        return self.call("getrawtransaction", txid, 2)

    def tip_height(self) -> int:
        return self.call("getblockcount")

    def get_block(self, blockhash: str) -> dict[str, Any]:
        """Block with fully decoded transactions, including each input's prevout."""
        return self.call("getblock", blockhash, 3)

    def scan_blocks(
        self,
        addresses: list[str],
        start_height: int,
        stop_height: int,
        progress: Callable[[int, int], None] | None = None,
        filter_false_positives: bool = False,
    ) -> list[str]:
        """Hashes of blocks that touch any of the addresses, as paying to or spending from.

        Uses the node's BIP158 block filter index (blockfilterindex=1). False positives
        are left in by default (about one block in several thousand per address): the
        caller checks every transaction anyway, and filtering them on the node would make
        it read each matching block from disk twice. Set ``filter_false_positives`` to
        have the node remove them.
        The range is scanned in chunks of ``scan_chunk`` blocks so each call stays short
        and progress can be reported; ``progress(low, high)`` is called before each chunk.
        If anything interrupts a chunk, the scan is stopped on the node as well.
        """
        descriptors = [f"addr({a})" for a in addresses]
        found: list[str] = []
        low = start_height
        while low <= stop_height:
            high = min(low + self.scan_chunk - 1, stop_height)
            if progress:
                progress(low, high)
            found.extend(self._scan_chunk(descriptors, low, high, filter_false_positives))
            low = high + 1
        return found

    def _scan_chunk(
        self, descriptors: list[str], low: int, high: int, filter_false_positives: bool
    ) -> list[str]:
        try:
            result = self.call(
                "scanblocks",
                "start",
                descriptors,
                low,
                high,
                "basic",
                {"filter_false_positives": filter_false_positives},
            )
        except KeyboardInterrupt:
            # The node keeps scanning after the client stops waiting; stop it too.
            self.abort_scan()
            raise
        except RpcError as exc:
            if "already in progress" in str(exc):
                raise RpcError(
                    "the node is already running a block scan, and it runs only one at a "
                    "time. Check it with `btc-trace scan-status`, or stop it with "
                    "`btc-trace scan-abort`."
                ) from exc
            if "timed out" in str(exc):
                self.abort_scan()
                raise RpcError(
                    f"scanning blocks {low}-{high} timed out, so the scan on the node was "
                    "stopped. Set a smaller BTC_SCAN_CHUNK (blocks per call) or a larger "
                    "BTC_TIMEOUT."
                ) from exc
            raise
        if not result.get("completed", True):
            raise RpcError(f"scanblocks {low}-{high} did not complete (was it aborted?)")
        return result["relevant_blocks"]

    def scan_status(self) -> dict[str, Any] | None:
        """Progress of a running scanblocks, or None when no scan is running."""
        return self.call("scanblocks", "status")

    def abort_scan(self) -> bool:
        """Stop a running scanblocks. True if a scan was stopped."""
        try:
            return bool(self.call("scanblocks", "abort"))
        except RpcError:
            return False

    def summary(self) -> dict[str, Any]:
        """Node software, chain state and index status."""
        network = self.call("getnetworkinfo")
        chain = self.call("getblockchaininfo")
        indexes = self.call("getindexinfo")
        return {
            "subversion": network.get("subversion"),
            "chain": chain.get("chain"),
            "blocks": chain.get("blocks"),
            "pruned": chain.get("pruned"),
            "initialblockdownload": chain.get("initialblockdownload"),
            "indexes": indexes,
        }
