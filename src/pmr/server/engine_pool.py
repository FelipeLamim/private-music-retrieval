"""Bounded process-level parallelism for the PIR server.

SEAL calls made through ``tenseal.sealapi`` hold the Python GIL, so threads do not speed up
homomorphic evaluation. With ``workers > 1`` the database is sharded by block index
(block j lives in worker j mod W). Each worker process loads only its shard, so the
preprocessed database is not duplicated. Session material (Galois keys / public context) and
prepared queries are replicated to every worker.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
import traceback
from typing import Any, Sequence

from ..pir.engine import BackendConfig, PirServerEngine


def _rss_peak_kib() -> int:
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    import resource
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


class LocalEngine:
    """Single-process engine with the same interface as the pool."""

    def __init__(self, records: Sequence[bytes], config: BackendConfig):
        self.engine = PirServerEngine(records, config)
        self.layout = self.engine.layout
        self.config = self.engine.config
        self.preprocess_s = self.engine.preprocess_s
        self.workers = 1
        self._sessions: dict[str, Any] = {}
        self._queries: dict[str, tuple[str, Any]] = {}
        self._lock = threading.Lock()  # SEAL objects are not shared across concurrent calls

    def open_session(self, sid: str, setup: bytes | None) -> None:
        with self._lock:
            self._sessions[sid] = self.engine.new_session_state(setup)

    def close_session(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
            for q in [q for q, (s, _) in self._queries.items() if s == sid]:
                self._queries.pop(q, None)

    def open_query(self, sid: str, qid: str, blobs: Sequence[bytes]) -> float:
        with self._lock:
            t0 = time.perf_counter()
            prepared = self.engine.prepare_query(self._sessions[sid], blobs)
            self._queries[qid] = (sid, prepared)
            return (time.perf_counter() - t0) * 1e3

    def close_query(self, qid: str) -> None:
        with self._lock:
            self._queries.pop(qid, None)

    def answer(self, qid: str, blocks: Sequence[int]) -> tuple[list[bytes], float]:
        with self._lock:
            sid, prepared = self._queries[qid]
            state = self._sessions[sid]
            t0 = time.perf_counter()
            out = [self.engine.answer_block(state, prepared, j) for j in blocks]
            return out, (time.perf_counter() - t0) * 1e3

    def stats(self) -> list[dict[str, Any]]:
        return [{"pid": os.getpid(), "peak_rss_kib": _rss_peak_kib(), "cpu_s": time.process_time(),
                 "db_bytes_est": self.engine.memory_estimate_bytes()}]

    def shutdown(self) -> None:
        pass


def _worker_main(conn, catalog_dir: str, max_tracks: int | None, config_dict: dict, shard: int, workers: int) -> None:
    from ..catalog import load_catalog  # imported in the child

    try:
        _, records = load_catalog(catalog_dir, max_tracks=max_tracks)
        config = BackendConfig.from_dict(config_dict)
        from ..pir.layout import Layout
        packing = config.packing_obj()
        layout = Layout.for_records([len(r) for r in records], packing.block_bytes, config.packing, config.slots)
        mine = [j for j in range(layout.num_blocks) if j % workers == shard]
        engine = PirServerEngine(records, config, block_indices=mine)
        del records
        conn.send(("ready", engine.preprocess_s, engine.memory_estimate_bytes()))
    except Exception:
        conn.send(("error", traceback.format_exc()))
        return
    sessions: dict[str, Any] = {}
    queries: dict[str, tuple[str, Any]] = {}
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        op = msg[0]
        try:
            if op == "stop":
                conn.send(("ok",))
                return
            if op == "open_session":
                sessions[msg[1]] = engine.new_session_state(msg[2])
                conn.send(("ok",))
            elif op == "close_session":
                sessions.pop(msg[1], None)
                for q in [q for q, (s, _) in queries.items() if s == msg[1]]:
                    queries.pop(q, None)
                conn.send(("ok",))
            elif op == "open_query":
                t0 = time.perf_counter()
                queries[msg[2]] = (msg[1], engine.prepare_query(sessions[msg[1]], msg[3]))
                conn.send(("ok", (time.perf_counter() - t0) * 1e3))
            elif op == "close_query":
                queries.pop(msg[1], None)
                conn.send(("ok",))
            elif op == "answer":
                sid, prepared = queries[msg[1]]
                t0 = time.perf_counter()
                out = [engine.answer_block(sessions[sid], prepared, j) for j in msg[2]]
                conn.send(("ok", out, (time.perf_counter() - t0) * 1e3))
            elif op == "stats":
                conn.send(("ok", {"pid": os.getpid(), "peak_rss_kib": _rss_peak_kib(),
                                  "cpu_s": time.process_time(), "db_bytes_est": engine.memory_estimate_bytes()}))
            else:
                conn.send(("error", f"unknown op {op}"))
        except Exception as exc:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class EnginePool:
    """W worker processes, each holding the blocks j with j mod W == shard."""

    def __init__(self, catalog_dir: str, config: BackendConfig, workers: int, record_lengths: Sequence[int], *,
                 max_tracks: int | None = None):
        if workers < 2:
            raise ValueError("use LocalEngine for a single worker")
        self.workers = workers
        from dataclasses import replace
        self.config = replace(config, params=config.params.resolved())
        config = self.config
        ctx = mp.get_context("spawn")
        self._conns, self._procs, self._locks = [], [], []
        t0 = time.perf_counter()
        for w in range(workers):
            parent, child = ctx.Pipe()
            p = ctx.Process(target=_worker_main, args=(child, catalog_dir, max_tracks, config.to_dict(), w, workers),
                            name=f"pmr-pir-worker-{w}", daemon=True)
            p.start()
            self._conns.append(parent)
            self._procs.append(p)
            self._locks.append(threading.Lock())
        self.preprocess_s = 0.0
        for conn in self._conns:
            msg = conn.recv()
            if msg[0] != "ready":
                self.shutdown()
                raise RuntimeError(f"worker failed to start:\n{msg[1]}")
            self.preprocess_s = max(self.preprocess_s, msg[1])
        self.startup_s = time.perf_counter() - t0
        from ..pir.layout import Layout
        packing = config.packing_obj()
        self.layout = Layout.for_records(list(record_lengths), packing.block_bytes, config.packing, config.slots)

    def _call(self, w: int, *msg) -> tuple:
        with self._locks[w]:
            self._conns[w].send(msg)
            reply = self._conns[w].recv()
        if reply[0] != "ok":
            raise RuntimeError(reply[1])
        return reply

    def _broadcast(self, *msg) -> list[tuple]:
        results: list[Any] = [None] * self.workers
        errors: list[BaseException] = []

        def run(w: int) -> None:
            try:
                results[w] = self._call(w, *msg)
            except BaseException as exc:  # collected below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(w,)) for w in range(self.workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise errors[0]
        return results

    def open_session(self, sid: str, setup: bytes | None) -> None:
        self._broadcast("open_session", sid, setup)

    def close_session(self, sid: str) -> None:
        self._broadcast("close_session", sid)

    def open_query(self, sid: str, qid: str, blobs: Sequence[bytes]) -> float:
        return max(r[1] for r in self._broadcast("open_query", sid, qid, list(blobs)))

    def close_query(self, qid: str) -> None:
        self._broadcast("close_query", qid)

    def answer(self, qid: str, blocks: Sequence[int]) -> tuple[list[bytes], float]:
        by_worker: dict[int, list[int]] = {}
        for j in blocks:
            by_worker.setdefault(j % self.workers, []).append(j)
        results: dict[int, tuple] = {}
        errors: list[BaseException] = []

        def run(w: int, js: list[int]) -> None:
            try:
                results[w] = self._call(w, "answer", qid, js)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(w, js)) for w, js in by_worker.items()]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise errors[0]
        blobs: dict[int, bytes] = {}
        for w, js in by_worker.items():
            for j, b in zip(js, results[w][1]):
                blobs[j] = b
        compute_ms = max(results[w][2] for w in by_worker)
        return [blobs[j] for j in blocks], compute_ms

    def stats(self) -> list[dict[str, Any]]:
        return [r[1] for r in self._broadcast("stats")]

    def shutdown(self) -> None:
        for w, conn in enumerate(self._conns):
            try:
                with self._locks[w]:
                    conn.send(("stop",))
                    conn.recv()
            except (OSError, EOFError):
                pass
        for p in self._procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
