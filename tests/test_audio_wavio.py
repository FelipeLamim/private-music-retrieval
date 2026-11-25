"""Tests for pmr.audio.wavio (RIFF parsing, conversion, canonical header, atomic writes)."""
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from pmr.audio.wavio import (
    PcmAudio,
    array_to_pcm16,
    float_to_pcm16,
    pcm16_to_array,
    read_wav,
    read_wav_float,
    read_wav_format,
    read_wav_pcm16_mono,
    wav_header,
    write_wav,
)

GUID_TAIL = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


def chunk(cid: bytes, body: bytes, pad: bool = True) -> bytes:
    out = cid + struct.pack("<I", len(body)) + body
    return out + (b"\x00" if pad and len(body) % 2 else b"")


def fmt_body(tag=1, channels=1, rate=16000, bits=16, block_align=None, ext_sub=1, valid_bits=None):
    block_align = channels * bits // 8 if block_align is None else block_align
    body = struct.pack("<HHIIHH", tag, channels, rate, rate * block_align, block_align, bits)
    if tag == 0xFFFE:
        sub = struct.pack("<H", ext_sub) + GUID_TAIL
        body += struct.pack("<HHI", 22, valid_bits or bits, 0x4) + sub
    return body


def riff(*chunks: bytes, riff_size=None) -> bytes:
    payload = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(payload) if riff_size is None else riff_size) + payload


def put(tmp_path, name, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ----------------------------------------------------------------------------- round trip


def test_round_trip_mono_and_stereo(tmp_path):
    rng = np.random.default_rng(1)
    mono = rng.integers(-32768, 32768, size=1000, dtype=np.int64).astype(np.int16)
    write_wav(tmp_path / "m.wav", array_to_pcm16(mono), 16000)
    a = read_wav(tmp_path / "m.wav")
    assert a == PcmAudio(array_to_pcm16(mono), 16000, 1, 2)
    assert np.array_equal(pcm16_to_array(a.pcm), mono)
    assert (tmp_path / "m.wav").stat().st_size == 44 + 2000
    stereo = rng.integers(-32768, 32768, size=2 * 500, dtype=np.int64).astype(np.int16)
    write_wav(tmp_path / "s.wav", array_to_pcm16(stereo), 44100, channels=2)
    b = read_wav(tmp_path / "s.wav")
    assert (b.sample_rate, b.channels, b.sample_width, b.num_frames) == (44100, 2, 2, 500)
    assert b.pcm == array_to_pcm16(stereo)
    with wave.open(str(tmp_path / "s.wav"), "rb") as w:  # independent reader agrees
        assert w.getnchannels() == 2 and w.getframerate() == 44100
        assert w.readframes(w.getnframes()) == b.pcm


def test_canonical_header_fields():
    h = wav_header(32000, 16000)
    assert len(h) == 44
    fields = struct.unpack("<4sI4s4sIHHIIHH4sI", h)
    assert fields == (b"RIFF", 36 + 32000, b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16, b"data", 32000)
    assert struct.unpack_from("<I", wav_header(0, 8000), 40)[0] == 0
    assert struct.unpack_from("<I", wav_header(3, 8000, 1, 1), 4)[0] == 36 + 3 + 1  # pad byte counted
    for bad in ((-1, 16000), (3, 16000), (100, 0), (100, 16000, 0), (100, 16000, 1, 5)):
        with pytest.raises(ValueError):
            wav_header(*bad)


def test_write_is_atomic_and_replaces(tmp_path):
    p = tmp_path / "x.wav"
    write_wav(p, b"\x01\x00" * 10, 16000)
    write_wav(p, b"\x02\x00" * 20, 16000)
    assert read_wav(p).pcm == b"\x02\x00" * 20
    assert [q.name for q in tmp_path.iterdir()] == ["x.wav"]  # no temp files left behind
    with pytest.raises(ValueError):
        write_wav(tmp_path / "odd.wav", b"\x00" * 3, 16000)  # not a whole 16-bit sample
    assert not (tmp_path / "odd.wav").exists()
    write_wav(tmp_path / "u8.wav", b"\x80\x81\x82", 8000, 1, 1)  # odd data gets a pad byte
    assert (tmp_path / "u8.wav").stat().st_size == 44 + 3 + 1
    assert read_wav_format(tmp_path / "u8.wav").data_bytes == 3


def test_array_helpers():
    a = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)
    pcm = array_to_pcm16(a)
    assert pcm == struct.pack("<5h", -32768, -1, 0, 1, 32767)
    back = pcm16_to_array(pcm)
    assert back.dtype == np.int16 and np.array_equal(back, a)
    back[0] = 5  # writable copy
    assert array_to_pcm16(np.array([1, 2], dtype=np.int64)) == struct.pack("<2h", 1, 2)
    with pytest.raises(ValueError):
        array_to_pcm16(np.array([40000]))
    with pytest.raises(ValueError):
        array_to_pcm16(np.array([0.5]))
    with pytest.raises(ValueError):
        pcm16_to_array(b"\x00\x01\x02")
    q, clipped = float_to_pcm16(np.array([0.0, 0.5, -1.0, 1.0, 2.0, -3.0, 1.5 / 32768]))
    assert q.tolist() == [0, 16384, -32768, 32767, 32767, -32768, 2] and clipped == 3


# ----------------------------------------------------------------------------- chunk handling


def test_pcm16_samples_with_list_chunk(tmp_path):
    """Read FFmpeg-style LIST/INFO metadata before five seconds of PCM audio."""
    info = chunk(b"LIST", b"INFO" + chunk(b"ISFT", b"Lavf60.16.100\x00"))
    time = np.arange(80000) / 16000
    for frequency in (220, 330, 440, 523, 660, 880):
        pcm = np.rint(4096 * np.sin(2 * np.pi * frequency * time)).astype("<i2").tobytes()
        data = riff(chunk(b"fmt ", fmt_body()), info, chunk(b"data", pcm))
        p = put(tmp_path, f"tone_{frequency}.wav", data)
        a = read_wav_pcm16_mono(p, expected_rate=16000)
        assert (a.sample_rate, a.channels, a.sample_width, len(a.pcm)) == (16000, 1, 2, 160000)
        assert a.pcm == pcm
        assert read_wav(p) == a
        with wave.open(str(p), "rb") as w:
            assert w.readframes(w.getnframes()) == a.pcm


def test_skips_unknown_chunks_and_odd_padding(tmp_path):
    pcm = struct.pack("<4h", 1, -2, 3, -4)
    info = chunk(b"LIST", b"INFOISFT" + struct.pack("<I", 5) + b"tool\x00")  # 17 bytes, odd
    data = riff(chunk(b"fmt ", fmt_body()), info, chunk(b"junk", b"abc"), chunk(b"data", pcm), chunk(b"id3 ", b"x"))
    p = put(tmp_path, "odd.wav", data)
    assert read_wav(p).pcm == pcm
    # a writer that forgot the pad byte after an odd-sized chunk is tolerated
    nopad = riff(chunk(b"fmt ", fmt_body()), chunk(b"junk", b"abc", pad=False), chunk(b"data", pcm))
    assert read_wav(put(tmp_path, "nopad.wav", nopad)).pcm == pcm
    # fmt after data, and a wrong RIFF size field, are accepted
    late = riff(chunk(b"data", pcm), chunk(b"fmt ", fmt_body()), riff_size=0)
    assert read_wav(put(tmp_path, "late.wav", late)).pcm == pcm
    # streaming placeholder size 0xFFFFFFFF means "data runs to end of file"
    stream = riff(chunk(b"fmt ", fmt_body())) + b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm
    assert read_wav(put(tmp_path, "stream.wav", stream)).pcm == pcm


@pytest.mark.parametrize(
    "name,data,message",
    [
        ("empty", b"", "too short"),
        ("notriff", b"OggS" + b"\x00" * 40, "not a RIFF/WAVE"),
        ("notwave", b"RIFF\x10\x00\x00\x00AVI " + b"\x00" * 8, "not a RIFF/WAVE"),
        ("nofmt", riff(chunk(b"data", b"\x00\x00")), "no fmt chunk"),
        ("nodata", riff(chunk(b"fmt ", fmt_body())), "no data chunk"),
        ("shortfmt", riff(chunk(b"fmt ", b"\x01\x00\x01\x00"), chunk(b"data", b"\x00\x00")), "fmt chunk too short"),
        ("truncdata", riff(chunk(b"fmt ", fmt_body())) + b"data" + struct.pack("<I", 1000) + b"\x00" * 10, "truncated"),
        ("truncchunk", riff(chunk(b"fmt ", fmt_body())) + b"LIST" + struct.pack("<I", 500) + b"\x00" * 4, "truncated"),
        ("badalign", riff(chunk(b"fmt ", fmt_body(block_align=4)), chunk(b"data", b"\x00" * 8)), "block align"),
        ("partialframe", riff(chunk(b"fmt ", fmt_body(channels=2)), chunk(b"data", b"\x00" * 6)), "not a multiple"),
        ("adpcm", riff(chunk(b"fmt ", fmt_body(tag=2, bits=4, block_align=1)), chunk(b"data", b"\x00" * 8)), "unsupported WAV encoding"),
        ("mp3", riff(chunk(b"fmt ", fmt_body(tag=0x55)), chunk(b"data", b"\x00" * 8)), "MP3"),
        ("bits12", riff(chunk(b"fmt ", fmt_body(bits=12, block_align=2)), chunk(b"data", b"\x00" * 8)), "unsupported integer PCM"),
        ("badguid", riff(chunk(b"fmt ", fmt_body(tag=0xFFFE)[:-14] + b"\x11" * 14), chunk(b"data", b"\x00" * 8)), "GUID"),
        ("garbagehdr", riff(chunk(b"fmt ", fmt_body())) + b"\x00\x01\x02\x03\x04\x00\x00\x00", "malformed chunk header"),
        ("zerochannels", riff(chunk(b"fmt ", fmt_body(channels=0, block_align=0)), chunk(b"data", b"")), "channel"),
    ],
)
def test_malformed_files_raise_value_error(tmp_path, name, data, message):
    p = put(tmp_path, f"{name}.wav", data)
    with pytest.raises(ValueError, match=message):
        read_wav(p)


def test_nan_float_rejected(tmp_path):
    body = np.array([0.0, np.nan], dtype="<f4").tobytes()
    p = put(tmp_path, "nan.wav", riff(chunk(b"fmt ", fmt_body(tag=3, bits=32)), chunk(b"data", body)))
    with pytest.raises(ValueError, match="NaN"):
        read_wav(p)


# ----------------------------------------------------------------------------- format conversion


def test_24_bit_conversion(tmp_path):
    vals = [0, 1, 127, 128, 129, 383, -128, -129, 8388607, -8388608, 256 * 1000, -256 * 1000 - 128]
    raw = b"".join(struct.pack("<i", v)[:3] for v in vals)
    p = put(tmp_path, "s24.wav", riff(chunk(b"fmt ", fmt_body(bits=24)), chunk(b"data", raw)))
    fmt, x = read_wav_float(p)
    assert fmt.encoding == "s24" and x.shape == (len(vals), 1)
    assert np.array_equal(x[:, 0] * 8388608.0, np.array(vals, dtype=np.float64))  # exact decode
    got = pcm16_to_array(read_wav(p).pcm).tolist()
    expected = [int(np.clip(np.rint(v / 256.0), -32768, 32767)) for v in vals]
    assert got == expected
    assert got[:6] == [0, 0, 0, 0, 1, 1]  # 128/256 = 0.5 rounds half to even
    with pytest.raises(ValueError, match="16-bit mono"):
        read_wav_pcm16_mono(p)


def test_float_8bit_32bit_conversion(tmp_path):
    f = np.array([0.0, 0.25, -0.5, 1.0, -1.0, 1.7, -2.0, 1e-6], dtype="<f4")
    p = put(tmp_path, "f32.wav", riff(chunk(b"fmt ", fmt_body(tag=3, bits=32)), chunk(b"fact", b"\x08\x00\x00\x00"), chunk(b"data", f.tobytes())))
    assert pcm16_to_array(read_wav(p).pcm).tolist() == [0, 8192, -16384, 32767, -32768, 32767, -32768, 0]
    assert read_wav_format(p).encoding == "f32"
    u8 = bytes([0, 64, 128, 192, 255])
    p8 = put(tmp_path, "u8.wav", riff(chunk(b"fmt ", fmt_body(bits=8)), chunk(b"data", u8, pad=True)))
    assert pcm16_to_array(read_wav(p8).pcm).tolist() == [-32768, -16384, 0, 16384, 32512]
    s32 = np.array([0, 2**31 - 1, -(2**31), 65536 * 100 + 32768], dtype="<i4")
    p32 = put(tmp_path, "s32.wav", riff(chunk(b"fmt ", fmt_body(bits=32)), chunk(b"data", s32.tobytes())))
    assert pcm16_to_array(read_wav(p32).pcm).tolist() == [0, 32767, -32768, 100]  # 100.5 -> 100 (half to even)


def test_extensible_formats(tmp_path):
    pcm = struct.pack("<4h", 100, -200, 300, -400)
    p = put(tmp_path, "ext16.wav", riff(chunk(b"fmt ", fmt_body(tag=0xFFFE, channels=2, ext_sub=1)), chunk(b"data", pcm)))
    a = read_wav(p)
    assert (a.channels, a.pcm) == (2, pcm)
    fmt = read_wav_format(p)
    assert (fmt.container_tag, fmt.format_tag, fmt.channel_mask) == (0xFFFE, 1, 0x4)
    mono = put(tmp_path, "ext16m.wav", riff(chunk(b"fmt ", fmt_body(tag=0xFFFE, ext_sub=1)), chunk(b"data", pcm)))
    assert read_wav_pcm16_mono(mono, expected_rate=16000).pcm == pcm
    # 24 valid bits in a 32-bit container (left-justified), as written by many DAWs
    vals = np.array([1000 << 8, -1000 << 8, (2**23 - 1) << 8], dtype="<i4")
    p24 = put(tmp_path, "ext24in32.wav", riff(chunk(b"fmt ", fmt_body(tag=0xFFFE, bits=32, ext_sub=1, valid_bits=24)), chunk(b"data", vals.tobytes())))
    assert pcm16_to_array(read_wav(p24).pcm).tolist() == [4, -4, 32767]
    assert read_wav_format(p24).valid_bits == 24
    fl = np.array([0.5, -0.25], dtype="<f4")
    pf = put(tmp_path, "extf.wav", riff(chunk(b"fmt ", fmt_body(tag=0xFFFE, bits=32, ext_sub=3)), chunk(b"data", fl.tobytes())))
    assert pcm16_to_array(read_wav(pf).pcm).tolist() == [16384, -8192]


def test_pcm16_mono_requirements(tmp_path):
    write_wav(tmp_path / "st.wav", b"\x00\x00" * 4, 16000, channels=2)
    with pytest.raises(ValueError, match="16-bit mono"):
        read_wav_pcm16_mono(tmp_path / "st.wav")
    write_wav(tmp_path / "m.wav", b"\x00\x00" * 4, 22050)
    assert read_wav_pcm16_mono(tmp_path / "m.wav").sample_rate == 22050
    with pytest.raises(ValueError, match="16000"):
        read_wav_pcm16_mono(tmp_path / "m.wav", expected_rate=16000)
