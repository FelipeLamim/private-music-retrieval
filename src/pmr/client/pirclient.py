"""Client side of the PIR protocol over HTTP (used by the trusted client app and experiments).

Privacy-relevant behaviour:
  * the track index never leaves this process: requests carry only ciphertexts, random
    server-chosen tokens and public block indices;
  * every retrieval uses a fresh query (new encryption randomness); a query is reused only for
    the blocks of the same retrieval;
  * by default all B blocks of the padded catalog are requested, in order, for every track
    (``policy="pad"``), so the request sequence does not depend on the track's length;
    ``policy="stop"`` stops at the track's last block and leaks its length (experiments only).
"""
from __future__ import annotations

import http.client
import io
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..pir import wire
from ..pir.engine import BackendConfig, PirClientEngine
from ..pir.layout import Layout


class ProtocolError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class ByteCounter:
    tx: int = 0
    rx: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, tx: int = 0, rx: int = 0) -> None:
        with self.lock:
            self.tx += tx
            self.rx += rx

    def snapshot(self) -> tuple[int, int]:
        with self.lock:
            return self.tx, self.rx


class _CountingRaw(io.RawIOBase):
    def __init__(self, sock, counter: ByteCounter):
        self._sock, self._counter = sock, counter

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = self._sock.recv_into(b)
        self._counter.add(rx=n)
        return n


class _CountingSocket:
    """Wraps a socket so every byte sent/received (HTTP headers + bodies) is counted."""

    def __init__(self, sock, counter: ByteCounter):
        self._sock, self._counter = sock, counter

    def sendall(self, data, *args) -> None:
        self._counter.add(tx=len(data))
        self._sock.sendall(data, *args)

    def send(self, data, *args) -> int:
        n = self._sock.send(data, *args)
        self._counter.add(tx=n)
        return n

    def makefile(self, mode="rb", buffering=-1, **_kw):
        if "b" not in mode or "r" not in mode:
            raise ValueError("only binary read files are supported")
        return io.BufferedReader(_CountingRaw(self._sock, self._counter), buffer_size=256 * 1024)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class CountingHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, counter: ByteCounter, timeout: float = 120.0):
        super().__init__(host, port, timeout=timeout)
        self._pmr_counter = counter

    def connect(self) -> None:
        super().connect()
        self.sock = _CountingSocket(self.sock, self._pmr_counter)


@dataclass
class WireLogEntry:
    method: str
    path: str
    request_body: bytes
    status: int
    response_body_len: int


class HttpTransport:
    """One keep-alive connection per thread; counts wire bytes; can record every message."""

    def __init__(self, base_url: str, *, record: bool = False, timeout: float = 120.0):
        if not base_url.startswith("http://"):
            raise ValueError("only plain http:// on loopback is supported")
        hostport = base_url[len("http://"):].rstrip("/")
        host, _, port = hostport.partition(":")
        self.host, self.port = host, int(port or 80)
        self.counter = ByteCounter()
        self.timeout = timeout
        self.record = record
        self.log: list[WireLogEntry] = []
        self._local = threading.local()

    def _conn(self) -> CountingHTTPConnection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = CountingHTTPConnection(self.host, self.port, self.counter, timeout=self.timeout)
            self._local.conn = c
        return c

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        hdrs = {"Host": f"{self.host}:{self.port}"}
        if body is not None:
            hdrs["Content-Type"] = "application/octet-stream"
        hdrs.update(headers or {})
        for attempt in (1, 2):
            conn = self._conn()
            try:
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conn.close()
                self._local.conn = None
                if attempt == 2:
                    raise
        if self.record:
            self.log.append(WireLogEntry(method, path, body or b"", resp.status, len(data)))
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data

    def json(self, method: str, path: str, body: bytes | None = None) -> Any:
        status, _, data = self.request(method, path, body)
        try:
            doc = json.loads(data.decode("utf-8")) if data else {}
        except ValueError:
            raise ProtocolError(f"{method} {path}: non-JSON response (HTTP {status})") from None
        if status >= 400:
            raise ProtocolError(f"{method} {path}: HTTP {status}: {doc.get('error', '')}")
        return doc

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None


@dataclass
class BlockEvent:
    j: int
    requested: float  # seconds since retrieval start
    received: float
    decoded: float
    response_bytes: int
    pcm_bytes: int


@dataclass
class RetrievalResult:
    pcm: bytes
    index: int
    blocks_fetched: int
    complete: bool
    timings: dict[str, float]
    bytes: dict[str, int]
    events: list[BlockEvent]
    server_compute_ms: float
    server_total_ms: float
    requests: list[dict[str, float]] = field(default_factory=list)


class PirHttpClient:
    """Talks to ``pmr.server``. Holds the secret key through :class:`PirClientEngine`."""

    def __init__(self, server_url: str, *, record_wire: bool = False):
        self.transport = HttpTransport(server_url, record=record_wire)
        t0 = time.perf_counter()
        self.catalog = self.transport.json("GET", "/v1/catalog")
        self.params = self.transport.json("GET", "/v1/pir/params")
        self.config = BackendConfig.from_dict(self.params["backend"])
        self.layout = Layout.from_dict(self.params["layout"])
        self.max_batch = int(self.params["limits"]["max_blocks_per_request"])
        t1 = time.perf_counter()
        self.engine = PirClientEngine(self.config, self.layout)  # validates parameters, generates keys
        t2 = time.perf_counter()
        self.session: str | None = None
        self.setup_timings = {"fetch_params_ms": (t1 - t0) * 1e3, "keygen_ms": (t2 - t1) * 1e3}
        self.setup_bytes = 0

    # -- session
    def open_session(self) -> str:
        t0 = time.perf_counter()
        blob = self.engine.session_setup_blob()
        t1 = time.perf_counter()
        body = b""
        if blob is not None:
            kind = "setup.context" if self.config.backend == "didactic" else "setup.galois"
            body = wire.pack(kind, [blob])
        doc = self.transport.json("POST", "/v1/pir/sessions", body)
        t2 = time.perf_counter()
        self.session = doc["session"]
        self.setup_bytes = len(body)
        self.setup_timings.update({"setup_material_ms": (t1 - t0) * 1e3, "session_upload_ms": (t2 - t1) * 1e3})
        return self.session

    def close_session(self) -> None:
        if self.session:
            try:
                self.transport.json("DELETE", f"/v1/pir/sessions/{self.session}")
            finally:
                self.session = None

    # -- retrieval
    def retrieve(self, index: int, *, policy: str = "pad", first_batch: int = 1, batch: int = 8,
                 prefetch: int = 2, on_block: Callable[[int, bytes], None] | None = None,
                 cancel: threading.Event | None = None) -> RetrievalResult:
        if policy not in ("pad", "stop"):
            raise ValueError("policy must be 'pad' or 'stop'")
        if not 0 <= index < self.layout.num_records:
            raise ValueError("track index out of range")
        if self.session is None:
            self.open_session()
        batch = max(1, min(batch, self.max_batch))
        first_batch = max(1, min(first_batch, self.max_batch))
        total_blocks = self.layout.num_blocks if policy == "pad" else self.layout.blocks_for_record(index)
        tx0, rx0 = self.transport.counter.snapshot()
        t_start = time.perf_counter()
        rel = lambda: time.perf_counter() - t_start  # noqa: E731

        blobs = self.engine.make_query(index)  # fresh randomness for this retrieval
        kind = "query.expanded" if (self.config.backend == "optimized" and self.config.query_mode == "expanded") \
            else "query.selector"
        body = wire.pack(kind, blobs)
        t_query = rel()
        doc = self.transport.json("POST", f"/v1/pir/sessions/{self.session}/queries", body)
        qid = doc["query"]
        t_query_up = rel()

        schedule: list[tuple[int, int]] = []
        j = 0
        while j < total_blocks:
            k = min(first_batch if j == 0 else batch, total_blocks - j)
            schedule.append((j, k))
            j += k

        fetched: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, prefetch))
        stop = threading.Event()
        stats = {"compute_ms": 0.0, "server_ms": 0.0, "resp_bytes": 0}
        requests: list[dict[str, float]] = []

        def fetcher() -> None:
            try:
                for start, count in schedule:
                    if stop.is_set() or (cancel is not None and cancel.is_set()):
                        break
                    t_req = rel()
                    status, headers, data = self.transport.request(
                        "GET", f"/v1/pir/queries/{qid}/blocks?start={start}&count={count}")
                    t_rcv = rel()
                    if status != 200:
                        raise ProtocolError(f"block request failed: HTTP {status}: {data[:200]!r}")
                    parts = wire.unpack(data, "answer.blocks", max_count=count, max_blob=1 << 20, exact_count=count)
                    c_ms = float(headers.get("x-pmr-compute-ms", "0"))
                    s_ms = float(headers.get("x-pmr-server-ms", "0"))
                    stats["compute_ms"] += c_ms
                    stats["server_ms"] += s_ms
                    requests.append({"start": start, "count": count, "compute_ms": c_ms, "server_ms": s_ms,
                                     "t_req": t_req, "t_rcv": t_rcv, "bytes": len(data)})
                    stats["resp_bytes"] += len(data)
                    fetched.put((start, parts, t_req, t_rcv, len(data)))
                fetched.put(None)
            except BaseException as exc:  # propagate to the consumer
                fetched.put(exc)

        th = threading.Thread(target=fetcher, name="pir-fetch", daemon=True)
        th.start()
        pcm = bytearray()
        events: list[BlockEvent] = []
        decode_s = 0.0
        complete = False
        try:
            while True:
                item = fetched.get()
                if item is None:
                    complete = not (cancel is not None and cancel.is_set())
                    break
                if isinstance(item, BaseException):
                    raise item
                start, parts, t_req, t_rcv, nbytes = item
                for off, blob in enumerate(parts):
                    jj = start + off
                    d0 = time.perf_counter()
                    chunk = self.engine.decode_block(index, jj, blob)
                    decode_s += time.perf_counter() - d0
                    pcm += chunk
                    events.append(BlockEvent(jj, t_req, t_rcv, rel(), len(blob), len(chunk)))
                    if on_block is not None:
                        on_block(jj, chunk)
                if cancel is not None and cancel.is_set():
                    break
        finally:
            stop.set()
            while th.is_alive():
                try:
                    fetched.get(timeout=0.05)
                except queue.Empty:
                    pass
            th.join(timeout=5)
            try:
                self.transport.json("DELETE", f"/v1/pir/queries/{qid}")
            except Exception:
                pass
        t_end = rel()
        tx1, rx1 = self.transport.counter.snapshot()
        expected = self.layout.record_lengths[index]
        if complete and len(pcm) != expected:
            raise ProtocolError(f"recovered {len(pcm)} bytes, expected {expected}")
        first = next((e for e in events if e.pcm_bytes > 0), None)
        timings = {
            "query_gen_ms": t_query * 1e3,
            "query_upload_ms": (t_query_up - t_query) * 1e3,
            "first_block_ms": (first.decoded * 1e3) if first else float("nan"),
            "total_ms": t_end * 1e3,
            "client_decode_ms": decode_s * 1e3,
            "server_prepare_ms": float(doc.get("prepare_ms", 0.0)),
        }
        sizes = {"query_body": len(body), "response_bodies": stats["resp_bytes"],
                 "wire_tx": tx1 - tx0, "wire_rx": rx1 - rx0, "blocks": len(events), "pcm": len(pcm),
                 "session_setup_body": self.setup_bytes}
        return RetrievalResult(bytes(pcm), index, len(events), complete, timings, sizes, events,
                               stats["compute_ms"], stats["server_ms"], requests)

    def close(self) -> None:
        try:
            self.close_session()
        except Exception:
            pass
        self.transport.close()
