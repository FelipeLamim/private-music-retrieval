"""Minimal, strict binary container for lists of opaque blobs (ciphertexts, keys).

Layout (little-endian):  magic "PMR1" | kind (u8) | reserved (u8, 0) | count (u32)
                         | count x (length u32 | bytes)
The container carries no identifiers; ``kind`` names the message type only.
"""
from __future__ import annotations

import struct

MAGIC = b"PMR1"
KINDS = {
    "query.selector": 1,   # R selector ciphertexts
    "query.expanded": 2,   # 1 compressed query ciphertext (oblivious expansion)
    "answer.blocks": 3,    # k response ciphertexts for consecutive blocks
    "setup.galois": 4,     # Galois keys (expanded mode)
    "setup.context": 5,    # TenSEAL public context (didactic backend)
}
_KIND_NAMES = {v: k for k, v in KINDS.items()}
_HEADER = struct.Struct("<4sBBI")
_LEN = struct.Struct("<I")


class WireError(ValueError):
    """Malformed container."""


def pack(kind: str, blobs: list[bytes]) -> bytes:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    parts = [_HEADER.pack(MAGIC, KINDS[kind], 0, len(blobs))]
    for b in blobs:
        parts.append(_LEN.pack(len(b)))
        parts.append(bytes(b))
    return b"".join(parts)


def unpack(data: bytes, kind: str, *, max_count: int, max_blob: int, exact_count: int | None = None) -> list[bytes]:
    mv = memoryview(data)
    if len(mv) < _HEADER.size:
        raise WireError("truncated header")
    magic, k, reserved, count = _HEADER.unpack_from(mv, 0)
    if magic != MAGIC or reserved != 0:
        raise WireError("bad magic")
    if _KIND_NAMES.get(k) != kind:
        raise WireError(f"unexpected message kind {_KIND_NAMES.get(k, k)!r}, expected {kind!r}")
    if count > max_count or (exact_count is not None and count != exact_count):
        raise WireError(f"bad blob count {count}")
    off, out = _HEADER.size, []
    for _ in range(count):
        if off + _LEN.size > len(mv):
            raise WireError("truncated length")
        (n,) = _LEN.unpack_from(mv, off)
        off += _LEN.size
        if n > max_blob or off + n > len(mv):
            raise WireError("blob too large or truncated")
        out.append(bytes(mv[off:off + n]))
        off += n
    if off != len(mv):
        raise WireError("trailing bytes")
    return out
