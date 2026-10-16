"""Leitura de blocos PCM; nenhum arquivo de entrada é alterado."""

from dataclasses import dataclass
from pathlib import Path
import wave


@dataclass(frozen=True)
class AudioRecord:
    filename: str
    block: bytes
    valid_bytes: int
    payload_bytes: int
    sample_rate: int
    channels: int
    sample_width: int


def load_records(directory: Path, block_size: int, block_index: int) -> list[AudioRecord]:
    if not 1 <= block_size <= 4096:
        raise ValueError("--block-size precisa estar entre 1 e 4096.")
    if block_index < 0:
        raise ValueError("--block-index não pode ser negativo.")
    if not directory.is_dir():
        raise ValueError(f"Pasta não encontrada: {directory}")

    paths = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".wav"),
        key=lambda p: p.name,
    )
    if not paths:
        raise ValueError(f"Nenhum arquivo WAV encontrado em {directory}.")

    records = []
    offset = block_index * block_size
    for path in paths:
        with wave.open(str(path), "rb") as audio:
            if audio.getcomptype() != "NONE":
                raise ValueError(f"{path.name}: este teste exige WAV PCM sem compressão.")
            frame_size = audio.getnchannels() * audio.getsampwidth()
            payload_bytes = audio.getnframes() * frame_size
            if offset >= payload_bytes:
                raise ValueError(f"Bloco {block_index} inexistente em {path.name}.")
            frame, skip_bytes = divmod(offset, frame_size)
            audio.setpos(frame)
            frames_to_read = (skip_bytes + block_size + frame_size - 1) // frame_size
            raw = audio.readframes(frames_to_read)[skip_bytes:skip_bytes + block_size]
            expected_size = min(block_size, payload_bytes - offset)
            if len(raw) != expected_size:
                raise ValueError(f"{path.name}: dados de áudio truncados.")
            records.append(AudioRecord(
                filename=path.name,
                block=raw.ljust(block_size, b"\x00"),
                valid_bytes=len(raw),
                payload_bytes=payload_bytes,
                sample_rate=audio.getframerate(),
                channels=audio.getnchannels(),
                sample_width=audio.getsampwidth(),
            ))
    return records
