"""Read every block from the node as raw bytes and collect revealed address hashes.

This is the heaviest job in the tool: the whole chain, roughly 700 GB of blocks, is
read once. To keep it practical:

* Blocks are fetched raw (``getblock <hash> 0``), about a fifth of the size of the
  decoded JSON the tracer uses, over kept-alive connections, and block hashes are
  looked up a thousand at a time with JSON-RPC batches.
* Several worker processes each fetch and parse their own ranges of blocks, so
  parsing uses several CPU cores while the node serves blocks.
* Each worker keeps only the revealed hashes that match a hash-based coin in the UTXO
  snapshot (``targets``), so results stay small.
* Every finished range of blocks is saved, so an interrupted scan resumes where it
  stopped.

All of this only reads from the node.
"""

from __future__ import annotations

import base64
import http.client
import json
import multiprocessing
import os
import ssl
import time
import urllib.parse
from array import array
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from btc_trace.reveal import BlockCounts, block_reveal_prefixes, match
from btc_trace.rpc import RpcError, _ssl_context

HASH_BATCH = 1000


class BlockSource(Protocol):
    def block_hashes(self, start: int, end: int) -> list[str]: ...
    def raw_block(self, blockhash: str) -> bytes: ...


class KeepAliveClient:
    """JSON-RPC over one persistent HTTP(S) connection, with batches and reconnects."""

    def __init__(
        self, url: str, user: str, password: str, timeout: float = 900.0, retries: int = 4
    ) -> None:
        parts = urllib.parse.urlsplit(url)
        self.https = parts.scheme == "https"
        self.host = parts.hostname or "localhost"
        self.port = parts.port or (443 if self.https else 80)
        self.path = parts.path or "/"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Content-Type": "application/json", "Authorization": f"Basic {token}"}
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = 2.0
        self.conn: http.client.HTTPConnection | None = None
        self.context: ssl.SSLContext | None = _ssl_context() if self.https else None

    @classmethod
    def from_env(cls) -> KeepAliveClient:
        missing = [v for v in ("BTC_URL", "BTC_USER", "BTC_PASS") if not os.environ.get(v)]
        if missing:
            raise RpcError("set " + ", ".join(missing) + " to read blocks from your node")
        return cls(
            os.environ["BTC_URL"],
            os.environ["BTC_USER"],
            os.environ["BTC_PASS"],
            timeout=float(os.environ.get("BTC_TIMEOUT", "900")),
        )

    def _connect(self) -> http.client.HTTPConnection:
        if self.conn is None:
            if self.https:
                self.conn = http.client.HTTPSConnection(
                    self.host, self.port, timeout=self.timeout, context=self.context
                )
            else:
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        return self.conn

    def _post(self, body: Any) -> Any:
        data = json.dumps(body).encode()
        delay = self.retry_delay
        for attempt in range(1, self.retries + 1):
            conn = self._connect()
            try:
                conn.request("POST", self.path, body=data, headers=self.headers)
                response = conn.getresponse()
                payload = response.read()
            except ConnectionRefusedError as exc:
                raise RpcError(f"could not reach node: {exc}") from exc
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
                self.close()
                if attempt == self.retries:
                    raise RpcError(
                        f"connection to the node kept dropping ({exc!r}); "
                        f"gave up after {attempt} attempts"
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            if response.status == 401:
                raise RpcError("node rejected the RPC credentials (HTTP 401)")
            try:
                return json.loads(payload)
            except ValueError as exc:
                raise RpcError(f"HTTP {response.status} from node: {payload[:200]!r}") from exc
        raise AssertionError("unreachable")

    def call(self, method: str, params: list[Any]) -> Any:
        reply = self._post({"jsonrpc": "1.0", "id": 0, "method": method, "params": params})
        if reply.get("error"):
            raise RpcError(f"{method}: {reply['error']}")
        return reply["result"]

    def batch(self, method: str, param_lists: list[list[Any]]) -> list[Any]:
        """Call one method with several parameter lists in a single request."""
        body = [
            {"jsonrpc": "1.0", "id": i, "method": method, "params": p}
            for i, p in enumerate(param_lists)
        ]
        replies = self._post(body)
        if isinstance(replies, dict):  # a whole-batch error
            raise RpcError(f"{method} batch: {replies.get('error')}")
        results: list[Any] = [None] * len(param_lists)
        for reply in replies:
            if reply.get("error"):
                raise RpcError(f"{method}: {reply['error']}")
            results[reply["id"]] = reply["result"]
        return results

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


class RpcBlockSource:
    """Blocks from a live node."""

    def __init__(self, client: KeepAliveClient) -> None:
        self.client = client

    def block_hashes(self, start: int, end: int) -> list[str]:
        hashes: list[str] = []
        for low in range(start, end + 1, HASH_BATCH):
            high = min(low + HASH_BATCH - 1, end)
            hashes += self.client.batch("getblockhash", [[h] for h in range(low, high + 1)])
        return hashes

    def raw_block(self, blockhash: str) -> bytes:
        return bytes.fromhex(self.client.call("getblock", [blockhash, 0]))


@dataclass
class RangeResult:
    start: int
    end: int
    blocks: int
    inputs: int
    reveals: int
    bytes: int
    matched: int


def scan_range(
    source: BlockSource, start: int, end: int, targets: np.ndarray
) -> tuple[RangeResult, np.ndarray]:
    """Revealed hashes in blocks start..end that match a target, and counts."""
    found = array("Q")
    counts = BlockCounts()
    size = 0
    hashes = source.block_hashes(start, end)
    for blockhash in hashes:
        raw = source.raw_block(blockhash)
        size += len(raw)
        block_reveal_prefixes(raw, found, counts)
    matched = match(found, targets)
    result = RangeResult(start, end, len(hashes), counts.inputs, counts.reveals, size, matched.size)
    return result, matched


# Worker processes: each opens its own connection and maps the targets file.
_worker: dict[str, Any] = {}


def _init_worker(targets_path: str) -> None:
    _worker["source"] = RpcBlockSource(KeepAliveClient.from_env())
    _worker["targets"] = np.load(targets_path, mmap_mode="r")


def _scan_in_worker(start: int, end: int) -> tuple[RangeResult, bytes]:
    result, matched = scan_range(_worker["source"], start, end, _worker["targets"])
    return result, matched.tobytes()


Progress = Callable[[int, int, int, float], None]  # blocks done, total, bytes, seconds


class ScanCache:
    """One file of matches per finished range, plus a log of their counts."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.log = directory / "ranges.jsonl"

    def done(self) -> dict[tuple[int, int], RangeResult]:
        results = {}
        try:
            for line in self.log.read_text().splitlines():
                try:
                    r = RangeResult(**json.loads(line))
                except (ValueError, TypeError):
                    continue  # a line cut off by an interruption
                if self._path(r.start, r.end).exists():
                    results[(r.start, r.end)] = r
        except OSError:
            pass
        return results

    def _path(self, start: int, end: int) -> Path:
        return self.directory / f"{start:07d}-{end:07d}.npy"

    def save(self, result: RangeResult, matched: np.ndarray) -> None:
        path = self._path(result.start, result.end)
        tmp = path.with_suffix(".tmp.npy")
        np.save(tmp, matched)
        os.replace(tmp, path)
        with self.log.open("a") as f:
            f.write(json.dumps(asdict(result)) + "\n")

    def load(self, start: int, end: int) -> np.ndarray:
        return np.load(self._path(start, end))


def scan_chain(
    stop_height: int,
    targets_path: Path,
    cache_dir: Path,
    *,
    chunk: int = 1000,
    workers: int = 4,
    source: BlockSource | None = None,
    progress: Progress | None = None,
) -> tuple[np.ndarray, RangeResult]:
    """Scan blocks 0..stop_height; return all matched prefixes and the summed counts.

    With ``source`` given the scan runs in this process (used by tests); otherwise
    ``workers`` processes each read from the node configured in the environment.
    """
    cache = ScanCache(cache_dir)
    ranges = [(s, min(s + chunk - 1, stop_height)) for s in range(0, stop_height + 1, chunk)]
    done = cache.done()
    todo = [r for r in ranges if r not in done]
    total_blocks = stop_height + 1
    blocks_done = sum(r.blocks for r in done.values())
    bytes_done = 0
    started = time.monotonic()

    def report(result: RangeResult) -> None:
        nonlocal blocks_done, bytes_done
        blocks_done += result.blocks
        bytes_done += result.bytes
        if progress:
            progress(blocks_done, total_blocks, bytes_done, time.monotonic() - started)

    if progress:
        progress(blocks_done, total_blocks, 0, 0.0)
    if source is not None:
        targets = np.load(targets_path, mmap_mode="r")
        for start, end in todo:
            result, matched = scan_range(source, start, end, targets)
            cache.save(result, matched)
            report(result)
    elif todo:
        _run_workers(todo, targets_path, cache, workers, report)

    finished = cache.done()
    missing = [r for r in ranges if r not in finished]
    if missing:
        raise RpcError(f"{len(missing)} block range(s) were not scanned; run the scan again")
    parts = [cache.load(s, e) for s, e in ranges]
    matched = np.unique(np.concatenate(parts)) if parts else np.zeros(0, dtype=np.uint64)
    totals = RangeResult(
        0,
        stop_height,
        sum(finished[r].blocks for r in ranges),
        sum(finished[r].inputs for r in ranges),
        sum(finished[r].reveals for r in ranges),
        sum(finished[r].bytes for r in ranges),
        int(matched.size),
    )
    return matched, totals


def _run_workers(todo, targets_path, cache, workers, report) -> None:
    pool = ProcessPoolExecutor(
        max_workers=max(1, workers),
        mp_context=multiprocessing.get_context("spawn"),  # the same on macOS and Linux
        initializer=_init_worker,
        initargs=(str(targets_path),),
    )
    pending: set[Future] = set()
    queue = list(todo)
    try:
        while queue or pending:
            while queue and len(pending) < workers * 2:
                start, end = queue.pop(0)
                pending.add(pool.submit(_scan_in_worker, start, end))
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                result, matched = future.result()
                cache.save(result, np.frombuffer(matched, dtype=np.uint64))
                report(result)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
