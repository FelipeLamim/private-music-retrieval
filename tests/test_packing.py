"""Packing <-> slot-value round trips and decode validation (requirement 1).

Covers: u8 / u16 strategies, block_bytes, boundary words, signed PCM round-trips through real
BFV encryption, odd-length / padding / trimming, invalid (packing, offset, plain-modulus)
combinations, and the noise-overflow representatives that decode must reject.
"""
from __future__ import annotations

import numpy as np
import pytest

from pmr.pir.packing import DecodeError, Packing, split_blocks

T = 65537


# --------------------------------------------------------------------------- construction

def test_block_bytes_and_bytes_per_slot():
    assert Packing("u16", 4096, T).block_bytes == 8192
    assert Packing("u16", 4096, T).bytes_per_slot == 2
    assert Packing("u8", 4096, T).block_bytes == 4096
    assert Packing("u8", 4096, T).bytes_per_slot == 1
    assert Packing("u16", 1024, T).block_bytes == 2048


@pytest.mark.parametrize("name", ["u32", "", "U16", "pcm"])
def test_unknown_packing_rejected(name):
    with pytest.raises(ValueError):
        Packing(name, 4096, T)


@pytest.mark.parametrize("slots", [0, -1])
def test_non_positive_slots_rejected(slots):
    with pytest.raises(ValueError):
        Packing("u16", slots, T)


@pytest.mark.parametrize("offset", [-1, 2, 256])
def test_invalid_offset_rejected(offset):
    with pytest.raises(ValueError):
        Packing("u16", 4096, T, offset)


def test_u16_offset1_needs_t_above_65536():
    # t = 65537 is the smallest prime that admits u16 with offset 1 (max word 65535 -> 65536).
    Packing("u16", 4096, 65537, 1)  # ok
    with pytest.raises(ValueError):
        Packing("u16", 4096, 40961, 1)  # 40961 is prime and 1 mod 2N but < 65537
    with pytest.raises(ValueError):
        Packing("u16", 4096, 65536, 1)


def test_u16_offset0_fits_t_65537():
    Packing("u16", 4096, 65537, 0)  # max word 65535 < 65537
    # u8 needs only t > 255 (offset 0) or > 256 (offset 1)
    Packing("u8", 4096, 257, 1)
    with pytest.raises(ValueError):
        Packing("u8", 4096, 256, 1)


# --------------------------------------------------------------------------- encode/decode

def test_u16_little_endian_word_layout():
    pk = Packing("u16", 4, T, offset=1)
    data = bytes([0x01, 0x02, 0xFF, 0xFF, 0x00, 0x00, 0x34, 0x12])
    slots = pk.encode(data)
    # little-endian: word0 = 0x0201, word1 = 0xFFFF, word2 = 0, word3 = 0x1234; +offset 1
    assert list(slots) == [0x0201 + 1, 0xFFFF + 1, 0 + 1, 0x1234 + 1]
    assert pk.decode(slots) == data


def test_u8_one_byte_per_slot():
    pk = Packing("u8", 5, T, offset=1)
    data = bytes([0, 1, 127, 254, 255])
    slots = pk.encode(data)
    assert list(slots) == [1, 2, 128, 255, 256]
    assert pk.decode(slots) == data


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("word", [0, 1, 255, 256, 32767, 32768, 32769, 65534, 65535])
def test_u16_boundary_words_round_trip(word, offset):
    pk = Packing("u16", 4096, T, offset)
    data = int(word).to_bytes(2, "little") + bytes(8190)
    slots = pk.encode(data)
    assert int(slots[0]) == word + offset
    assert pk.decode(slots) == data


def test_encode_zero_pads_to_block():
    pk = Packing("u16", 4096, T, offset=1)
    slots = pk.encode(b"\x01\x02\x03")  # 3 bytes -> word0 = 0x0201, word1 = 0x0003
    assert slots.shape == (4096,)
    assert int(slots[0]) == 0x0201 + 1 and int(slots[1]) == 0x0003 + 1
    assert all(int(v) == 1 for v in slots[2:])  # padding words are 0 -> 0 + offset


def test_encode_rejects_oversized_block():
    pk = Packing("u16", 4, T)
    with pytest.raises(ValueError):
        pk.encode(bytes(9))  # capacity is 8 bytes


def test_odd_length_data_pads_high_byte_zero():
    pk = Packing("u16", 4, T, offset=1)
    slots = pk.encode(b"\xAB")  # one byte -> word0 = 0x00AB
    assert int(slots[0]) == 0xAB + 1
    assert pk.decode(slots, length=1) == b"\xAB"
    assert pk.decode(slots, length=2) == b"\xAB\x00"


def test_length_trimming_and_bounds():
    pk = Packing("u8", 8, T, offset=1)
    data = bytes(range(8))
    slots = pk.encode(data)
    assert pk.decode(slots, length=0) == b""
    assert pk.decode(slots, length=3) == data[:3]
    assert pk.decode(slots, length=8) == data
    for bad in (-1, 9):
        with pytest.raises(ValueError):
            pk.decode(slots, length=bad)


# --------------------------------------------------------------------------- centered / noise representatives

def test_decode_accepts_centered_negative_representatives():
    """SEAL/TenSEAL centered decoding returns values in (-(t-1)/2, (t-1)/2]; decode must mod them."""
    pk = Packing("u16", 4, T, offset=1)
    # word 65535 is stored as 65536; its centered representative is 65536 - 65537 = -1.
    slots = np.array([-1, -1, -1, -1], dtype=np.int64)
    assert pk.decode(slots) == b"\xff\xff" * 4
    # a mix of representatives: v -> word = (v - offset) mod t
    mixed = np.array([1, -1, 2, -32768], dtype=np.int64)
    expected_words = [0, 65535, 1, 32768]  # (1-1), (-1-1)%t, (2-1), (-32768-1)%t
    expected = b"".join(int(w).to_bytes(2, "little") for w in expected_words)
    assert pk.decode(mixed) == expected


@pytest.mark.parametrize("offset,bad_slot0", [(0, 65536), (1, 0)])
def test_noise_overflow_rejected(offset, bad_slot0):
    """Values a noisy/garbage response could produce must raise DecodeError, not silently wrap."""
    pk = Packing("u16", 4, T, offset)
    slots = pk.encode(b"\x00" * 8)
    slots = slots.copy()
    slots[0] = bad_slot0  # offset 0: word 65536 out of range; offset 1: word (0-1) mod t = 65536
    with pytest.raises(DecodeError):
        pk.decode(slots)


def test_u8_out_of_range_rejected():
    pk = Packing("u8", 4, T, offset=1)
    slots = np.array([1, 1, 300, 1], dtype=np.int64)  # (300 - 1) = 299 >= 256
    with pytest.raises(DecodeError):
        pk.decode(slots)


def test_decode_wrong_shape_rejected():
    pk = Packing("u16", 4, T)
    with pytest.raises(DecodeError):
        pk.decode(np.ones(3, dtype=np.int64))


def test_decode_trims_extra_slots():
    """BatchEncoder returns N values; decode uses the first ``slots`` of a longer vector."""
    pk = Packing("u16", 4, T, offset=1)
    data = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    slots = pk.encode(data)
    padded = np.concatenate([slots, np.zeros(4092, dtype=np.uint64)])
    assert pk.decode(padded) == data


# --------------------------------------------------------------------------- real encryption round-trip

@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("packing", ["u8", "u16"])
def test_round_trip_through_real_bfv(packing, offset):
    """encode -> BatchEncoder -> encrypt -> decrypt -> decode is the identity on real SEAL."""
    from pmr.he.bfv import BFVClientCrypto
    from pmr.he.params import BFVParams
    import tenseal.sealapi as sealapi

    crypto = BFVClientCrypto(BFVParams())
    ctx = crypto.bundle.context
    encoder = sealapi.BatchEncoder(ctx)
    pk = Packing(packing, 4096, T, offset)
    payload = rand_bytes(pk.block_bytes, seed=hash((packing, offset)) & 0xFFFF)
    slots = pk.encode(payload)

    pt = sealapi.Plaintext()
    encoder.encode(slots.tolist(), pt)
    ct = sealapi.Ciphertext()
    crypto._encryptor.encrypt_symmetric(pt, ct)
    out_pt = sealapi.Plaintext()
    crypto._decryptor.decrypt(ct, out_pt)
    recovered = np.asarray(encoder.decode_uint64(out_pt), dtype=np.uint64)
    assert pk.decode(recovered) == payload


def test_signed_pcm_bit_pattern_round_trip():
    """16-bit PCM is transported as its two's-complement bit pattern, exact for every sample."""
    from pmr.he.bfv import BFVClientCrypto
    from pmr.he.params import BFVParams
    import tenseal.sealapi as sealapi

    samples = np.array([-32768, -32767, -1, 0, 1, 32767], dtype=np.int16)
    pcm = samples.tobytes()
    pk = Packing("u16", 4096, T, offset=1)
    slots = pk.encode(pcm)

    crypto = BFVClientCrypto(BFVParams())
    encoder = sealapi.BatchEncoder(crypto.bundle.context)
    pt = sealapi.Plaintext()
    encoder.encode(slots.tolist(), pt)
    ct = sealapi.Ciphertext()
    crypto._encryptor.encrypt_symmetric(pt, ct)
    out_pt = sealapi.Plaintext()
    crypto._decryptor.decrypt(ct, out_pt)
    recovered = np.asarray(encoder.decode_uint64(out_pt), dtype=np.uint64)
    out = pk.decode(recovered, length=len(pcm))
    assert np.frombuffer(out, dtype=np.int16).tolist() == samples.tolist()


# --------------------------------------------------------------------------- split_blocks helper

def test_split_blocks_padding_and_count():
    data = bytes(range(10))
    blocks = split_blocks(data, 4, 3)
    assert blocks == [bytes(range(0, 4)), bytes(range(4, 8)), bytes(range(8, 10))]
    assert len(blocks) == 3


def test_split_blocks_rejects_too_long():
    with pytest.raises(ValueError):
        split_blocks(bytes(13), 4, 3)


def rand_bytes(n, seed=0):
    return np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8).tobytes()
