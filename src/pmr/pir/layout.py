"""Public layout of the PIR database.

The database is a matrix of R records (tracks) x B blocks. Block j of every track is one
record of the j-th PIR sub-database. Every track is padded to B = ceil(max_len / block_bytes)
blocks, where max_len is the longest track, so a client that requests all B blocks produces
the same request sequence for every track. Track lengths themselves are public catalog
metadata (they are shown in the UI), which is acceptable: the server must not learn which
track is being retrieved, not how long the catalog entries are.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class Layout:
    num_records: int
    num_blocks: int
    block_bytes: int
    packing: str
    slots: int
    record_lengths: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.num_records < 1 or len(self.record_lengths) != self.num_records:
            raise ValueError("record_lengths must have num_records entries (>= 1)")
        if self.num_blocks < 1 or self.block_bytes < 1:
            raise ValueError("num_blocks and block_bytes must be positive")
        if max(self.record_lengths) > self.num_blocks * self.block_bytes:
            raise ValueError("a record does not fit in num_blocks blocks")

    @staticmethod
    def for_records(lengths: list[int] | tuple[int, ...], block_bytes: int, packing: str, slots: int) -> "Layout":
        if not lengths:
            raise ValueError("empty catalog")
        if min(lengths) < 1:
            raise ValueError("empty record")
        blocks = math.ceil(max(lengths) / block_bytes)
        return Layout(len(lengths), blocks, block_bytes, packing, slots, tuple(int(x) for x in lengths))

    def blocks_for_record(self, index: int) -> int:
        """Blocks that carry real data for record ``index`` (the rest is padding)."""
        return math.ceil(self.record_lengths[index] / self.block_bytes)

    @property
    def padded_record_bytes(self) -> int:
        return self.num_blocks * self.block_bytes

    @property
    def database_bytes(self) -> int:
        return sum(self.record_lengths)

    @property
    def padded_database_bytes(self) -> int:
        return self.num_records * self.padded_record_bytes

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["record_lengths"] = list(self.record_lengths)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Layout":
        return Layout(
            int(d["num_records"]), int(d["num_blocks"]), int(d["block_bytes"]), str(d["packing"]),
            int(d["slots"]), tuple(int(x) for x in d["record_lengths"]),
        )
