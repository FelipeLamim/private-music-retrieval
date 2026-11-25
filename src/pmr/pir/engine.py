"""PIR engines: server-side database + answering, client-side query + decoding.

These classes contain no networking; ``pmr.server`` and ``pmr.client`` wrap them in separate
processes. The server engine never receives the selected index: its inputs are public
parameters, the database, query ciphertexts and public block indices.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict, replace
from typing import Any, Sequence

import numpy as np

from ..he.bfv import BFVClientCrypto, BFVServerCrypto, CryptoError, DB_FORMS, QUERY_MODES
from ..he.didactic import DidacticClientCrypto, DidacticServerCrypto
from ..he.params import BFVParams
from .layout import Layout
from .packing import Packing

BACKENDS = ("optimized", "didactic")


@dataclass(frozen=True)
class BackendConfig:
    """Everything that defines the PIR protocol variant (all public)."""

    backend: str = "optimized"      # "optimized" (sealapi) | "didactic" (TenSEAL BFVVector)
    packing: str = "u16"            # "u8" | "u16"
    query_mode: str = "selector"    # "selector" | "expanded" (optimized only)
    db_form: str = "ntt"            # "ntt" | "plain" | "raw" (optimized only)
    mod_switch: bool = True         # switch responses to the last modulus (optimized only)
    seeded: bool = True             # seeded symmetric query ciphertexts (optimized only)
    slots: int = 4096               # batching slots used per block (<= N)
    params: BFVParams = BFVParams()

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}")
        if self.query_mode not in QUERY_MODES:
            raise ValueError(f"query_mode must be one of {QUERY_MODES}")
        if self.db_form not in DB_FORMS:
            raise ValueError(f"db_form must be one of {DB_FORMS}")
        if self.backend == "didactic" and (
            self.query_mode != "selector" or self.db_form != "raw" or self.mod_switch or self.seeded
        ):
            raise ValueError("didactic backend: selector queries, raw records, no mod switch, no seeds")
        if not 1 <= self.slots <= self.params.poly_modulus_degree:
            raise ValueError("slots must be in 1..N")

    @staticmethod
    def didactic(packing: str = "u8", slots: int = 4096, params: BFVParams = BFVParams()) -> "BackendConfig":
        return BackendConfig("didactic", packing, "selector", "raw", False, False, slots, params)

    @property
    def offset(self) -> int:
        return 0 if self.backend == "didactic" else 1

    def packing_obj(self) -> Packing:
        return Packing(self.packing, self.slots, self.params.plain_modulus, self.offset)

    def label(self) -> str:
        if self.backend == "didactic":
            return f"didactic-{self.packing}"
        flags = [self.packing, self.query_mode, self.db_form]
        flags.append("ms" if self.mod_switch else "noms")
        flags.append("seed" if self.seeded else "noseed")
        return "opt-" + "-".join(flags)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["params"] = self.params.resolved().to_public_dict()
        d["label"] = self.label()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "BackendConfig":
        return BackendConfig(
            backend=d["backend"], packing=d["packing"], query_mode=d["query_mode"], db_form=d["db_form"],
            mod_switch=bool(d["mod_switch"]), seeded=bool(d["seeded"]), slots=int(d["slots"]),
            params=BFVParams.from_public_dict(d["params"]),
        )


# --------------------------------------------------------------------------- server

class PirServerEngine:
    """Holds the (public) database and answers block requests for prepared queries."""

    def __init__(self, records: Sequence[bytes], config: BackendConfig, *, block_indices: Sequence[int] | None = None):
        self.config = replace(config, params=config.params.resolved())
        self.packing = self.config.packing_obj()
        self.layout = Layout.for_records([len(r) for r in records], self.packing.block_bytes,
                                         self.config.packing, self.config.slots)
        # A shard worker may hold only some block indices (process-level parallelism).
        self.block_indices = list(range(self.layout.num_blocks)) if block_indices is None else list(block_indices)
        self._crypto_opt: BFVServerCrypto | None = None
        if self.config.backend == "optimized":
            self._crypto_opt = BFVServerCrypto(self.config.params, db_form=self.config.db_form,
                                               mod_switch=self.config.mod_switch)
        t0 = time.perf_counter()
        bb = self.packing.block_bytes
        self._blocks: dict[int, list[Any]] = {}
        for j in self.block_indices:
            column = []
            for rec in records:
                vals = self.packing.encode(rec[j * bb:(j + 1) * bb])
                if self._crypto_opt is not None:
                    column.append(self._crypto_opt.encode_record_block(vals))
                else:
                    column.append(DidacticServerCrypto.encode_record_block(vals))
            self._blocks[j] = column
        self.preprocess_s = time.perf_counter() - t0

    # -- sessions / queries
    def new_session_state(self, setup_blob: bytes | None) -> Any:
        """Validate per-session public material (Galois keys or didactic public context)."""
        if self.config.backend == "didactic":
            if not setup_blob:
                raise CryptoError("didactic backend requires the client's public context")
            return DidacticServerCrypto(setup_blob, self.config.slots)
        if self.config.query_mode == "expanded":
            if not setup_blob:
                raise CryptoError("expanded mode requires Galois keys")
            return self._crypto_opt.load_galois_keys(setup_blob)
        if setup_blob:
            raise CryptoError("unexpected setup material for selector mode")
        return None

    def prepare_query(self, session_state: Any, blobs: Sequence[bytes]) -> Any:
        r = self.layout.num_records
        if self.config.backend == "didactic":
            return session_state.prepare_query(blobs, r)
        return self._crypto_opt.prepare_query(self.config.query_mode, blobs, r, session_state)

    def answer_block(self, session_state: Any, prepared: Any, j: int) -> bytes:
        column = self._blocks.get(j)
        if column is None:
            raise IndexError(f"block {j} not held by this engine")
        if self.config.backend == "didactic":
            return session_state.answer_block(prepared, column)
        return self._crypto_opt.answer_block(prepared, column)

    def memory_estimate_bytes(self) -> int:
        """Approximate bytes held for the database representation."""
        n_items = len(self._blocks) * self.layout.num_records
        n, k = self.config.params.poly_modulus_degree, len(self.config.params.coeff_modulus) - 1
        if self.config.backend == "didactic":
            return n_items * self.config.slots * 28  # Python ints in lists (CPython small-int objects shared)
        if self.config.db_form == "ntt":
            return n_items * n * k * 8
        if self.config.db_form == "plain":
            return n_items * n * 8
        return n_items * self.config.slots * 8


# --------------------------------------------------------------------------- client

class PirClientEngine:
    """Trusted client: owns the secret key; turns a track index into a query and decodes blocks."""

    def __init__(self, config: BackendConfig, layout: Layout):
        self.config = replace(config, params=config.params.resolved())
        self.layout = layout
        self.packing = self.config.packing_obj()
        if layout.block_bytes != self.packing.block_bytes or layout.slots != self.config.slots:
            raise ValueError("layout does not match the backend configuration")
        if self.config.backend == "optimized":
            self.crypto: Any = BFVClientCrypto(self.config.params, seeded=self.config.seeded)
        else:
            self.crypto = DidacticClientCrypto(self.config.params)

    def session_setup_blob(self) -> bytes | None:
        """Public material uploaded once per session (never the secret key)."""
        if self.config.backend == "didactic":
            return self.crypto.public_context()
        if self.config.query_mode == "expanded":
            return self.crypto.galois_keys(self.layout.num_records)
        return None

    def make_query(self, index: int) -> list[bytes]:
        """Fresh query for one retrieval (new randomness every call; never reuse across retrievals)."""
        r = self.layout.num_records
        if self.config.backend == "didactic":
            return self.crypto.selector_query(index, r, self.config.slots)
        if self.config.query_mode == "expanded":
            return [self.crypto.expanded_query(index, r)]
        return self.crypto.selector_query(index, r)

    def decode_block(self, index: int, j: int, blob: bytes) -> bytes:
        """Decrypt the response for block j and strip padding beyond the track's length."""
        if self.config.backend == "didactic":
            slots = self.crypto.decrypt_slots(blob, self.config.slots)
        else:
            slots = self.crypto.decrypt_slots(blob)
        bb = self.layout.block_bytes
        remaining = self.layout.record_lengths[index] - j * bb
        length = max(0, min(bb, remaining))
        return self.packing.decode(slots, length=length)


def retrieve_in_process(server: PirServerEngine, client: PirClientEngine, index: int,
                        *, stop_at_track_end: bool = False) -> bytes:
    """Convenience loop used by tests: one query, reused for every block of the retrieval."""
    state = server.new_session_state(client.session_setup_blob())
    prepared = server.prepare_query(state, client.make_query(index))
    blocks = client.layout.blocks_for_record(index) if stop_at_track_end else client.layout.num_blocks
    out = bytearray()
    for j in range(blocks):
        out += client.decode_block(index, j, server.answer_block(state, prepared, j))
    return bytes(out)
