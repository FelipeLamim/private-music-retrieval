"""Client for the conventional baseline (single-track download and whole-database download).

Measured with the same transport and byte counting as the PIR client. Reads the response
incrementally so time to first playable audio can be compared with progressive PIR playback.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..client.pirclient import BlockEvent, CountingHTTPConnection, ByteCounter


@dataclass
class ConventionalResult:
    pcm: bytes
    index: int
    mode: str
    timings: dict[str, float]
    bytes: dict[str, int]
    events: list[BlockEvent]


class ConventionalClient:
    def __init__(self, base_url: str, chunk_bytes: int = 8192):
        hostport = base_url[len("http://"):].rstrip("/")
        host, _, port = hostport.partition(":")
        self.host, self.port = host, int(port)
        self.counter = ByteCounter()
        self.chunk = chunk_bytes
        self._conn = CountingHTTPConnection(host, self.port, self.counter)
        self._conn.request("GET", "/v1/catalog", headers={"Host": f"{host}:{self.port}"})
        import json
        self.catalog = json.loads(self._conn.getresponse().read())
        self.lengths = [t["pcm_bytes"] for t in self.catalog["tracks"]]

    def _get_stream(self, path: str, t_start: float, keep: tuple[int, int] | None):
        self._conn.request("GET", path, headers={"Host": f"{self.host}:{self.port}"})
        resp = self._conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"GET {path}: HTTP {resp.status}")
        total = int(resp.getheader("Content-Length"))
        out, events, pos = bytearray(), [], 0
        lo, hi = keep if keep else (0, total)
        while pos < total:
            t_req = time.perf_counter() - t_start
            data = resp.read(min(self.chunk, total - pos))
            if not data:
                raise RuntimeError("connection closed early")
            t_rcv = time.perf_counter() - t_start
            a, b = max(lo, pos), min(hi, pos + len(data))
            useful = data[a - pos:b - pos] if b > a else b""
            out += useful
            events.append(BlockEvent(len(events), t_req, t_rcv, t_rcv, len(data), len(useful)))
            pos += len(data)
        return bytes(out), events

    def retrieve_track(self, index: int) -> ConventionalResult:
        tx0, rx0 = self.counter.snapshot()
        t0 = time.perf_counter()
        pcm, events = self._get_stream(f"/v1/tracks/{index}/pcm", t0, None)
        return self._result(pcm, index, "single_track", t0, tx0, rx0, events)

    def retrieve_via_database(self, index: int) -> ConventionalResult:
        tx0, rx0 = self.counter.snapshot()
        t0 = time.perf_counter()
        start = sum(self.lengths[:index])
        pcm, events = self._get_stream("/v1/database", t0, (start, start + self.lengths[index]))
        return self._result(pcm, index, "whole_database", t0, tx0, rx0, events)

    def _result(self, pcm, index, mode, t0, tx0, rx0, events) -> ConventionalResult:
        t_end = time.perf_counter() - t0
        tx1, rx1 = self.counter.snapshot()
        first = next((e for e in events if e.pcm_bytes > 0), None)
        return ConventionalResult(pcm, index, mode,
                                  {"first_block_ms": first.decoded * 1e3 if first else float("nan"),
                                   "total_ms": t_end * 1e3},
                                  {"wire_tx": tx1 - tx0, "wire_rx": rx1 - rx0, "pcm": len(pcm)}, events)

    def close(self) -> None:
        self._conn.close()
