"""Packing audio bytes into BFV batching slots, and the exact inverse.

Two strategies are supported:

* ``u8``  — one byte per slot (the original prototype): slot value b + offset, b in 0..255.
* ``u16`` — two bytes per slot: the little-endian byte pair (b0, b1) becomes the word
  w = b0 + 256*b1 in 0..65535, stored as w + offset. With t = 65537 every word fits
  (65535 + 1 = 65536 = t - 1), so 4096 slots carry 8192 bytes.

For 16-bit PCM the word w is the two's-complement bit pattern of the signed sample s
(w = s mod 2^16). We never encode s as a signed residue mod t: SEAL's centered decoding
returns values in (-(t-1)/2, (t-1)/2] and mod t != mod 2^16, so mixing the two would corrupt
negative samples. Transporting the unsigned bit pattern keeps the method valid for any byte
stream (headers, compressed audio) and makes decoding exact: w = (v - offset) mod t, which
must lie in 0..2^(8k) - 1 or the response is rejected.

``offset`` = 1 (used by the optimized backend) guarantees that no record plaintext is the zero
polynomial (all slot values are >= 1), so SEAL never produces a transparent ciphertext and the
server needs no public key. ``offset`` = 0 reproduces the original encoding.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

PACKINGS = ("u8", "u16")


class DecodeError(ValueError):
    """Raised when decrypted slot values are not a valid encoding (noise overflow, bad server)."""


@dataclass(frozen=True)
class Packing:
    name: str  # "u8" | "u16"
    slots: int  # number of batching slots used per block (<= N)
    plain_modulus: int
    offset: int = 1

    def __post_init__(self) -> None:
        if self.name not in PACKINGS:
            raise ValueError(f"unknown packing {self.name!r}; expected one of {PACKINGS}")
        if self.slots < 1:
            raise ValueError("slots must be positive")
        if self.offset not in (0, 1):
            raise ValueError("offset must be 0 or 1")
        max_value = (1 << (8 * self.bytes_per_slot)) - 1 + self.offset
        if max_value >= self.plain_modulus:
            raise ValueError(
                f"packing {self.name} with offset {self.offset} needs plain_modulus > {max_value}"
            )

    @property
    def bytes_per_slot(self) -> int:
        return 1 if self.name == "u8" else 2

    @property
    def block_bytes(self) -> int:
        return self.slots * self.bytes_per_slot

    def encode(self, data: bytes) -> np.ndarray:
        """Bytes (len <= block_bytes, zero-padded) -> uint64 slot values of length ``slots``."""
        if len(data) > self.block_bytes:
            raise ValueError(f"block of {len(data)} bytes exceeds capacity {self.block_bytes}")
        buf = bytes(data) + b"\x00" * (self.block_bytes - len(data))
        if self.name == "u8":
            words = np.frombuffer(buf, dtype=np.uint8)
        else:
            words = np.frombuffer(buf, dtype="<u2")
        return words.astype(np.uint64) + np.uint64(self.offset)

    def decode(self, values, *, length: int | None = None) -> bytes:
        """Slot values (any residue representative, signed or unsigned) -> bytes.

        ``length`` trims the result (padding removal); default returns the whole block.
        """
        v = np.asarray(values, dtype=np.int64)
        if v.shape != (self.slots,) and v.ndim == 1 and v.shape[0] >= self.slots:
            v = v[: self.slots]
        if v.shape != (self.slots,):
            raise DecodeError(f"expected {self.slots} slot values, got shape {v.shape}")
        w = np.mod(v - self.offset, self.plain_modulus)  # normalizes centered representatives too
        limit = 1 << (8 * self.bytes_per_slot)
        bad = np.flatnonzero(w >= limit)
        if bad.size:
            raise DecodeError(
                f"{bad.size} slot values outside the {self.name} range (first at slot {int(bad[0])}); "
                "noise overflow or malformed response"
            )
        dtype = np.uint8 if self.name == "u8" else np.dtype("<u2")
        out = w.astype(dtype).tobytes()
        if length is not None:
            if not 0 <= length <= len(out):
                raise ValueError("invalid length")
            out = out[:length]
        return out


def split_blocks(data: bytes, block_bytes: int, num_blocks: int) -> list[bytes]:
    """Split ``data`` into exactly ``num_blocks`` blocks (last ones zero-padded or empty)."""
    if len(data) > block_bytes * num_blocks:
        raise ValueError("data longer than num_blocks * block_bytes")
    return [data[j * block_bytes:(j + 1) * block_bytes] for j in range(num_blocks)]
