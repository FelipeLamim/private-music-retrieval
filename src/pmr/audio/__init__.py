"""Audio utilities: WAV I/O, deterministic synthetic catalog, user audio import.

Submodules: ``pmr.audio.wavio`` (re-exported below), ``pmr.audio.synth`` (generator)
and ``pmr.audio.dataset`` (catalogs, CLI ``python -m pmr.audio.dataset``). The heavier
submodules are not imported here, so ``import pmr.audio`` only needs NumPy and running
``python -m pmr.audio.dataset`` does not trigger runpy's double-import warning.
"""
from .wavio import (
    PcmAudio,
    array_to_pcm16,
    pcm16_to_array,
    read_wav,
    read_wav_pcm16_mono,
    wav_header,
    write_wav,
)

__all__ = [
    "PcmAudio",
    "array_to_pcm16",
    "pcm16_to_array",
    "read_wav",
    "read_wav_pcm16_mono",
    "wav_header",
    "write_wav",
]
