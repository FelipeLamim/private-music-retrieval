"""WAV (RIFF/WAVE) reading and writing with the standard library and NumPy only.

The reader is a small, strict RIFF parser:

* unknown chunks (``LIST``, ``fact``, ``cue ``, ``bext``, ...) are skipped;
* chunks with an odd size are followed by one pad byte (RIFF rule); a missing pad
  byte written by some broken tools is tolerated when the next chunk id is only
  readable without it;
* ``WAVE_FORMAT_PCM`` (1), ``WAVE_FORMAT_IEEE_FLOAT`` (3) and
  ``WAVE_FORMAT_EXTENSIBLE`` (0xFFFE) with a PCM or IEEE-float sub-format are
  accepted, with 8/16/24/32-bit integer and 32/64-bit float samples;
* anything malformed, truncated or unsupported raises :class:`ValueError` with a
  message that names the file and the problem.

:func:`read_wav` returns signed 16-bit little-endian PCM (the format used by the
whole project); other sample formats are converted with documented rounding.
:func:`read_wav_float` exposes the decoded samples as float64 so that the import
tool can downmix and resample before quantizing only once.
"""
from __future__ import annotations

import os
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Union

import numpy as np

PathLike = Union[str, "os.PathLike[str]"]

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# KSDATAFORMAT_SUBTYPE_* GUIDs share this 14-byte tail after the 2-byte format code.
_GUID_TAIL = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"

_KNOWN_TAGS = {
    0x0002: "Microsoft ADPCM",
    0x0006: "A-law",
    0x0007: "mu-law",
    0x0011: "IMA ADPCM",
    0x0031: "GSM 6.10",
    0x0050: "MPEG audio",
    0x0055: "MPEG Layer 3 (MP3)",
    0x00FF: "AAC",
    0x2000: "AC-3",
    0x674F: "Ogg Vorbis",
    0xF1AC: "FLAC",
}

MAX_CHUNK_SCAN = 4096  # upper bound on the number of chunks inspected


@dataclass(frozen=True)
class PcmAudio:
    """Uncompressed interleaved PCM audio.

    ``pcm`` holds little-endian signed 16-bit interleaved samples when
    ``sample_width == 2`` (always the case for objects returned by :func:`read_wav`).
    """

    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int

    @property
    def num_frames(self) -> int:
        return len(self.pcm) // (self.channels * self.sample_width)

    @property
    def duration_s(self) -> float:
        return self.num_frames / self.sample_rate


@dataclass(frozen=True)
class WavFormat:
    """Format description of a parsed WAV file (as stored in the file)."""

    format_tag: int  # effective sample format: WAVE_FORMAT_PCM or WAVE_FORMAT_IEEE_FLOAT
    container_tag: int  # tag found in the fmt chunk (1, 3 or 0xFFFE)
    channels: int
    sample_rate: int
    bits_per_sample: int  # container size in bits (8, 16, 24, 32 or 64)
    valid_bits: int  # significant bits (== bits_per_sample unless EXTENSIBLE says otherwise)
    block_align: int
    channel_mask: Union[int, None]
    data_offset: int
    data_bytes: int

    @property
    def sample_width(self) -> int:
        return self.bits_per_sample // 8

    @property
    def num_frames(self) -> int:
        return self.data_bytes // self.block_align

    @property
    def encoding(self) -> str:
        """Short name: ``u8``, ``s16``, ``s24``, ``s32``, ``f32`` or ``f64``."""
        if self.format_tag == WAVE_FORMAT_IEEE_FLOAT:
            return f"f{self.bits_per_sample}"
        if self.bits_per_sample == 8:
            return "u8"
        return f"s{self.bits_per_sample}"

    def describe(self) -> str:
        names = {
            "u8": "8-bit unsigned integer PCM",
            "s16": "16-bit signed integer PCM",
            "s24": "24-bit signed integer PCM",
            "s32": "32-bit signed integer PCM",
            "f32": "32-bit IEEE float",
            "f64": "64-bit IEEE float",
        }
        text = names[self.encoding]
        if self.valid_bits != self.bits_per_sample:
            text += f" ({self.valid_bits} valid bits)"
        return text


# --------------------------------------------------------------------------- sniffing


def sniff_audio_container(head: bytes) -> str:
    """Best-effort name of the container format from the first bytes of a file."""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "WAV"
    if head[:4] == b"RIFX":
        return "big-endian RIFX WAV"
    if head[:4] in (b"RF64", b"BW64"):
        return "RF64/BW64 (64-bit WAV)"
    if head[:4] == b"fLaC":
        return "FLAC"
    if head[:4] == b"OggS":
        return "Ogg (Vorbis/Opus/FLAC)"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "MP3/MPEG audio"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "MP4/M4A/AAC"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "AIFF"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "Matroska/WebM"
    if head[:4] == b"RIFF":
        return "RIFF (not WAVE)"
    return "unknown"


# --------------------------------------------------------------------------- parsing


def _is_fourcc(raw: bytes) -> bool:
    return len(raw) == 4 and all(32 <= b < 127 for b in raw)


def _parse_fmt(name: str, body: bytes) -> dict:
    if len(body) < 16:
        raise ValueError(f"{name}: fmt chunk too short ({len(body)} bytes, need >= 16)")
    tag, channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", body, 0)
    container_tag = tag
    valid_bits = bits
    channel_mask = None
    if tag == WAVE_FORMAT_EXTENSIBLE:
        if len(body) < 40:
            raise ValueError(
                f"{name}: WAVE_FORMAT_EXTENSIBLE fmt chunk too short ({len(body)} bytes, need 40)"
            )
        cb_size, valid_bits, channel_mask = struct.unpack_from("<HHI", body, 16)
        if cb_size < 22:
            raise ValueError(f"{name}: WAVE_FORMAT_EXTENSIBLE cbSize {cb_size} < 22")
        guid = body[24:40]
        if guid[2:] != _GUID_TAIL:
            raise ValueError(f"{name}: unsupported WAVE_FORMAT_EXTENSIBLE sub-format GUID {guid.hex()}")
        tag = struct.unpack_from("<H", guid, 0)[0]
        if valid_bits == 0:
            valid_bits = bits
    if tag not in (WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT):
        label = _KNOWN_TAGS.get(tag, "unknown codec")
        raise ValueError(
            f"{name}: unsupported WAV encoding (format tag 0x{tag:04X}, {label}); "
            "only uncompressed integer PCM or IEEE float WAV is supported"
        )
    if channels < 1:
        raise ValueError(f"{name}: invalid channel count {channels}")
    if rate < 1:
        raise ValueError(f"{name}: invalid sample rate {rate}")
    if tag == WAVE_FORMAT_PCM and bits not in (8, 16, 24, 32):
        raise ValueError(f"{name}: unsupported integer PCM sample size {bits} bits (need 8/16/24/32)")
    if tag == WAVE_FORMAT_IEEE_FLOAT and bits not in (32, 64):
        raise ValueError(f"{name}: unsupported float sample size {bits} bits (need 32 or 64)")
    if valid_bits > bits:
        raise ValueError(f"{name}: valid bits per sample {valid_bits} > container size {bits}")
    expected_align = channels * (bits // 8)
    if block_align != expected_align:
        raise ValueError(
            f"{name}: block align {block_align} does not match {channels} channel(s) x {bits} bits "
            f"(expected {expected_align})"
        )
    return {
        "format_tag": tag,
        "container_tag": container_tag,
        "channels": channels,
        "sample_rate": rate,
        "bits_per_sample": bits,
        "valid_bits": valid_bits,
        "block_align": block_align,
        "channel_mask": channel_mask,
    }


def _parse(fh: BinaryIO, name: str) -> WavFormat:
    fh.seek(0, os.SEEK_END)
    file_size = fh.tell()
    fh.seek(0)
    head = fh.read(12)
    if len(head) < 12:
        raise ValueError(f"{name}: file too short to be a WAV file ({file_size} bytes)")
    kind = sniff_audio_container(head)
    if kind != "WAV":
        raise ValueError(f"{name}: not a RIFF/WAVE file (detected: {kind})")
    pos = 12
    fmt = None
    data = None  # (offset, size)
    scanned = 0
    while pos + 8 <= file_size and (fmt is None or data is None):
        scanned += 1
        if scanned > MAX_CHUNK_SCAN:
            raise ValueError(f"{name}: too many chunks (> {MAX_CHUNK_SCAN})")
        fh.seek(pos)
        header = fh.read(8)
        cid, size = header[:4], struct.unpack("<I", header[4:])[0]
        if not _is_fourcc(cid):
            raise ValueError(f"{name}: malformed chunk header at byte {pos} (id {cid!r})")
        body_start = pos + 8
        remaining = file_size - body_start
        if cid == b"data":
            if size == 0xFFFFFFFF:  # streaming placeholder: data runs to end of file
                size = remaining
            if size > remaining:
                raise ValueError(
                    f"{name}: truncated file: data chunk declares {size} bytes "
                    f"but only {remaining} bytes follow"
                )
            data = (body_start, size)
        elif size > remaining:
            raise ValueError(
                f"{name}: truncated file: chunk {cid.decode('ascii')!r} declares {size} bytes "
                f"but only {remaining} bytes follow"
            )
        elif cid == b"fmt ":
            if fmt is not None:
                raise ValueError(f"{name}: duplicate fmt chunk")
            fmt = _parse_fmt(name, fh.read(size))
        nxt = body_start + size
        if size & 1:
            nxt += 1
            # Tolerate writers that omit the pad byte after an odd-sized chunk.
            if nxt + 4 <= file_size:
                fh.seek(nxt)
                padded_id = fh.read(4)
                fh.seek(nxt - 1)
                unpadded_id = fh.read(4)
                if not _is_fourcc(padded_id) and _is_fourcc(unpadded_id):
                    nxt -= 1
        pos = nxt
    if fmt is None:
        raise ValueError(f"{name}: no fmt chunk found")
    if data is None:
        raise ValueError(f"{name}: no data chunk found")
    offset, size = data
    if size % fmt["block_align"]:
        raise ValueError(
            f"{name}: data chunk size {size} is not a multiple of the block align "
            f"{fmt['block_align']} (truncated or malformed)"
        )
    return WavFormat(data_offset=offset, data_bytes=size, **fmt)


def read_wav_format(path: PathLike) -> WavFormat:
    """Parse the chunk structure of a WAV file without decoding its samples."""
    name = os.fspath(path)
    with open(path, "rb") as fh:
        return _parse(fh, name)


def _read_payload(path: PathLike) -> tuple[WavFormat, bytes]:
    name = os.fspath(path)
    with open(path, "rb") as fh:
        fmt = _parse(fh, name)
        fh.seek(fmt.data_offset)
        raw = fh.read(fmt.data_bytes)
    if len(raw) != fmt.data_bytes:
        raise ValueError(f"{name}: truncated data chunk")
    return fmt, raw


def _decode_float(fmt: WavFormat, raw: bytes, name: str) -> np.ndarray:
    """Samples as float64 in [-1, 1) (integer formats are scaled exactly by 2**(bits-1))."""
    enc = fmt.encoding
    if enc == "u8":
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif enc == "s16":
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif enc == "s24":
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = (v ^ 0x800000) - 0x800000  # sign-extend 24 -> 32 bits
        x = v.astype(np.float64) / 8388608.0
    elif enc == "s32":
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    elif enc in ("f32", "f64"):
        x = np.frombuffer(raw, dtype="<f4" if enc == "f32" else "<f8").astype(np.float64)
        if not np.all(np.isfinite(x)):
            raise ValueError(f"{name}: float WAV contains NaN or infinite samples")
    else:  # pragma: no cover - guarded by _parse_fmt
        raise ValueError(f"{name}: unsupported encoding {enc}")
    return x.reshape(-1, fmt.channels)


def float_to_pcm16(x: np.ndarray) -> tuple[np.ndarray, int]:
    """Quantize float samples (full scale = 1.0) to int16.

    Scales by 32768, rounds half to even (``numpy.rint``) and clips to
    [-32768, 32767]. Returns ``(samples, number_of_clipped_samples)``.
    """
    y = np.rint(np.asarray(x, dtype=np.float64) * 32768.0)
    clipped = int(np.count_nonzero((y > 32767.0) | (y < -32768.0)))
    np.clip(y, -32768.0, 32767.0, out=y)
    return y.astype(np.int16), clipped


def read_wav_float(path: PathLike) -> tuple[WavFormat, np.ndarray]:
    """Decode a WAV file to float64 samples of shape ``(frames, channels)``.

    Integer PCM is scaled by ``2**(bits-1)`` (8-bit audio is unsigned with offset 128),
    so 16/24/32-bit values are represented exactly. Float files are returned as stored.
    """
    fmt, raw = _read_payload(path)
    return fmt, _decode_float(fmt, raw, os.fspath(path))


def read_wav(path: PathLike) -> PcmAudio:
    """Read a WAV file and return interleaved signed 16-bit little-endian PCM.

    16-bit integer files are returned byte-for-byte. Other formats are converted:
    8-bit unsigned ``(v - 128) * 256`` (exact); 24/32-bit integer and float samples are
    scaled to 16-bit full scale, rounded half to even and clipped to [-32768, 32767].
    Channels and sample rate are preserved. Raises ValueError on malformed files.
    """
    name = os.fspath(path)
    fmt, raw = _read_payload(path)
    if fmt.encoding == "s16":
        pcm = raw
    elif fmt.encoding == "u8":
        v = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
        pcm = v.astype("<i2").tobytes()
    else:
        samples, _ = float_to_pcm16(_decode_float(fmt, raw, name))
        pcm = samples.astype("<i2").tobytes()
    return PcmAudio(pcm=bytes(pcm), sample_rate=fmt.sample_rate, channels=fmt.channels, sample_width=2)


def read_wav_pcm16_mono(path: PathLike, expected_rate: Union[int, None] = None) -> PcmAudio:
    """Read a WAV file that must already be 16-bit integer mono PCM (no conversion).

    Raises ValueError if the file is not 16-bit mono or, when ``expected_rate`` is
    given, if its sample rate differs.
    """
    name = os.fspath(path)
    fmt, raw = _read_payload(path)
    if fmt.encoding != "s16" or fmt.channels != 1 or fmt.valid_bits != 16:
        raise ValueError(
            f"{name}: expected 16-bit mono PCM, got {fmt.describe()} with {fmt.channels} channel(s)"
        )
    if expected_rate is not None and fmt.sample_rate != int(expected_rate):
        raise ValueError(f"{name}: expected sample rate {expected_rate} Hz, got {fmt.sample_rate} Hz")
    return PcmAudio(pcm=bytes(raw), sample_rate=fmt.sample_rate, channels=1, sample_width=2)


# --------------------------------------------------------------------------- writing


def _check_format(sample_rate: int, channels: int, sample_width: int) -> None:
    if not isinstance(sample_rate, (int, np.integer)) or not 1 <= int(sample_rate) <= 0xFFFFFFFF:
        raise ValueError(f"invalid sample rate {sample_rate!r}")
    if not isinstance(channels, (int, np.integer)) or not 1 <= int(channels) <= 0xFFFF:
        raise ValueError(f"invalid channel count {channels!r}")
    if sample_width not in (1, 2, 3, 4):
        raise ValueError(f"invalid sample width {sample_width!r} (expected 1, 2, 3 or 4 bytes)")
    if int(sample_rate) * int(channels) * int(sample_width) > 0xFFFFFFFF:
        raise ValueError("byte rate does not fit in 32 bits")


def wav_header(data_bytes: int, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """Canonical 44-byte WAVE_FORMAT_PCM header for ``data_bytes`` bytes of PCM.

    If ``data_bytes`` is odd (only possible with 8-bit mono), the RIFF size accounts
    for the mandatory pad byte, which the caller must append after the data.
    """
    _check_format(sample_rate, channels, sample_width)
    if not isinstance(data_bytes, (int, np.integer)) or data_bytes < 0:
        raise ValueError(f"invalid data size {data_bytes!r}")
    data_bytes = int(data_bytes)
    block_align = int(channels) * int(sample_width)
    if data_bytes % block_align:
        raise ValueError(f"data size {data_bytes} is not a multiple of the block align {block_align}")
    riff_size = 36 + data_bytes + (data_bytes & 1)
    if riff_size > 0xFFFFFFFF:
        raise ValueError("data too large for a RIFF/WAVE file (4 GiB limit)")
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        WAVE_FORMAT_PCM,
        int(channels),
        int(sample_rate),
        int(sample_rate) * block_align,
        block_align,
        8 * int(sample_width),
        b"data",
        data_bytes,
    )


def atomic_write_bytes(path: PathLike, chunks) -> None:
    """Write ``chunks`` (iterable of bytes) to ``path`` atomically.

    The data goes to a temporary file in the same directory, is flushed and fsynced,
    then renamed over the destination, so readers never see a partial file.
    """
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    for _ in range(100):
        tmp = directory / f".{path.name}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    else:  # pragma: no cover
        raise OSError(f"could not create a temporary file in {directory}")
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in chunks:
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:  # make the rename durable; best effort (not supported everywhere)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_wav(path: PathLike, pcm: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> None:
    """Write PCM bytes as a canonical 44-byte-header WAV file, atomically."""
    pcm = bytes(pcm)
    header = wav_header(len(pcm), sample_rate, channels, sample_width)
    pad = b"\x00" if len(pcm) & 1 else b""
    atomic_write_bytes(path, (header, pcm, pad))


# --------------------------------------------------------------------------- arrays


def pcm16_to_array(pcm: bytes) -> np.ndarray:
    """Little-endian signed 16-bit PCM bytes -> writable native int16 array."""
    if len(pcm) % 2:
        raise ValueError(f"16-bit PCM must have an even number of bytes, got {len(pcm)}")
    return np.frombuffer(pcm, dtype="<i2").astype(np.int16)


def array_to_pcm16(a) -> bytes:
    """Integer sample array -> little-endian signed 16-bit PCM bytes.

    Values must already be integers within [-32768, 32767]; no silent wrap-around
    or rounding happens here (use :func:`float_to_pcm16` for float data).
    """
    arr = np.asarray(a)
    if arr.dtype == np.int16:
        return arr.astype("<i2", copy=False).tobytes()
    if arr.dtype.kind not in "iu":
        raise ValueError(f"array_to_pcm16 needs an integer array, got dtype {arr.dtype}; use float_to_pcm16")
    if arr.size and (int(arr.min()) < -32768 or int(arr.max()) > 32767):
        raise ValueError("sample values outside the signed 16-bit range")
    return arr.astype("<i2").tobytes()
