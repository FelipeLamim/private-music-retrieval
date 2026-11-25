"""Database layout (requirement 2) and the wire container (requirement 3)."""
from __future__ import annotations

import struct

import pytest

from pmr.pir.layout import Layout
from pmr.pir import wire


# =========================================================================== layout

def test_padding_to_max_blocks():
    bb = 8192
    layout = Layout.for_records([10, bb, bb + 1, 3 * bb], bb, "u16", 4096)
    assert layout.num_records == 4
    # longest record is 3*bb -> every track padded to 3 blocks
    assert layout.num_blocks == 3
    assert layout.padded_record_bytes == 3 * bb


def test_blocks_for_record_exact_one_longer_one_shorter():
    bb = 8192
    # exact multiple, one byte longer, one byte shorter than a block boundary
    layout = Layout.for_records([bb, bb + 1, bb - 1, 2 * bb, 2 * bb + 1], bb, "u16", 4096)
    assert layout.blocks_for_record(0) == 1       # exactly one block
    assert layout.blocks_for_record(1) == 2       # one byte over -> spills into a second block
    assert layout.blocks_for_record(2) == 1       # one byte short -> still one block
    assert layout.blocks_for_record(3) == 2       # exact two blocks
    assert layout.blocks_for_record(4) == 3
    assert layout.num_blocks == 3                 # max is 2*bb+1 -> 3 blocks


def test_record_lengths_preserved_and_public_sizes():
    bb = 4096
    lengths = [1, 100, bb, 5 * bb]
    layout = Layout.for_records(lengths, bb, "u8", 4096)
    assert layout.record_lengths == tuple(lengths)
    assert layout.database_bytes == sum(lengths)
    assert layout.padded_database_bytes == layout.num_records * layout.num_blocks * bb


def test_empty_catalog_rejected():
    with pytest.raises(ValueError):
        Layout.for_records([], 8192, "u16", 4096)


def test_empty_record_rejected():
    with pytest.raises(ValueError):
        Layout.for_records([8192, 0, 100], 8192, "u16", 4096)


def test_direct_construction_validations():
    with pytest.raises(ValueError):  # record_lengths length mismatch
        Layout(2, 1, 8192, "u16", 4096, (8192,))
    with pytest.raises(ValueError):  # record does not fit in num_blocks
        Layout(1, 1, 8192, "u16", 4096, (8193,))
    with pytest.raises(ValueError):  # non-positive num_blocks
        Layout(1, 0, 8192, "u16", 4096, (10,))


def test_layout_dict_round_trip():
    layout = Layout.for_records([10, 8192, 20000], 8192, "u16", 4096)
    assert Layout.from_dict(layout.to_dict()) == layout


# =========================================================================== wire container

KIND = "answer.blocks"


def test_round_trip_multiple_blobs():
    blobs = [b"", b"\x00", b"abc", bytes(range(256))]
    packed = wire.pack(KIND, blobs)
    out = wire.unpack(packed, KIND, max_count=10, max_blob=4096)
    assert out == blobs


def test_round_trip_zero_blobs():
    packed = wire.pack(KIND, [])
    assert wire.unpack(packed, KIND, max_count=10, max_blob=4096) == []


def test_header_layout_is_as_documented():
    packed = wire.pack("query.selector", [b"xy"])
    magic, kind, reserved, count = struct.unpack_from("<4sBBI", packed, 0)
    assert magic == b"PMR1" and kind == wire.KINDS["query.selector"] and reserved == 0 and count == 1


def test_pack_unknown_kind_rejected():
    with pytest.raises(ValueError):
        wire.pack("query.bogus", [b"x"])


def test_wrong_kind_rejected():
    packed = wire.pack("query.selector", [b"x"])
    with pytest.raises(wire.WireError):
        wire.unpack(packed, "answer.blocks", max_count=10, max_blob=4096)


def test_bad_magic_rejected():
    packed = bytearray(wire.pack(KIND, [b"x"]))
    packed[0] = ord("X")
    with pytest.raises(wire.WireError):
        wire.unpack(bytes(packed), KIND, max_count=10, max_blob=4096)


def test_nonzero_reserved_rejected():
    packed = bytearray(wire.pack(KIND, [b"x"]))
    packed[5] = 1  # reserved byte
    with pytest.raises(wire.WireError):
        wire.unpack(bytes(packed), KIND, max_count=10, max_blob=4096)


def test_truncated_header_rejected():
    with pytest.raises(wire.WireError):
        wire.unpack(b"PMR", KIND, max_count=10, max_blob=4096)


def test_truncated_length_field_rejected():
    packed = wire.pack(KIND, [b"abcd"])
    with pytest.raises(wire.WireError):
        wire.unpack(packed[:-6], KIND, max_count=10, max_blob=4096)


def test_truncated_blob_rejected():
    packed = wire.pack(KIND, [b"abcdefgh"])
    with pytest.raises(wire.WireError):
        wire.unpack(packed[:-2], KIND, max_count=10, max_blob=4096)


def test_trailing_bytes_rejected():
    packed = wire.pack(KIND, [b"abc"])
    with pytest.raises(wire.WireError):
        wire.unpack(packed + b"\x00", KIND, max_count=10, max_blob=4096)


def test_count_over_max_rejected():
    packed = wire.pack(KIND, [b"a", b"b", b"c"])
    with pytest.raises(wire.WireError):
        wire.unpack(packed, KIND, max_count=2, max_blob=4096)


def test_exact_count_enforced():
    packed = wire.pack(KIND, [b"a", b"b"])
    wire.unpack(packed, KIND, max_count=5, max_blob=4096, exact_count=2)  # ok
    with pytest.raises(wire.WireError):
        wire.unpack(packed, KIND, max_count=5, max_blob=4096, exact_count=3)


def test_blob_over_max_rejected():
    packed = wire.pack(KIND, [b"x" * 100])
    with pytest.raises(wire.WireError):
        wire.unpack(packed, KIND, max_count=10, max_blob=50)


def test_declared_length_overflow_rejected():
    """A length field larger than the remaining buffer must be rejected (no over-read)."""
    header = struct.pack("<4sBBI", b"PMR1", wire.KINDS[KIND], 0, 1)
    packed = header + struct.pack("<I", 0xFFFFFFF) + b"short"
    with pytest.raises(wire.WireError):
        wire.unpack(packed, KIND, max_count=10, max_blob=1 << 30)
