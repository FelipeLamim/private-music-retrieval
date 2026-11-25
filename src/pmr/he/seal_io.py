"""Byte serialization of SEAL objects.

``tenseal.sealapi`` exposes ``save(path)`` / ``load(context, path)`` only, so objects are
round-tripped through a scratch file inside the project-local temporary directory
(``$TMPDIR`` set by ``env.sh``). SEAL's own format is used unchanged (header + zstd
compression); ``load`` runs SEAL's validity checks (``is_valid_for``), and we check again.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
from typing import Any, Callable

import tenseal.sealapi as sealapi

_local = threading.local()
_dir_lock = threading.Lock()
_scratch_dir: str | None = None


def _cleanup() -> None:
    """Remove this process's scratch directory (it may hold serialized key material from tests)."""
    d = _scratch_dir
    if d and d.endswith(f"-{os.getpid()}") and os.path.basename(d).startswith("pmr-sealio-"):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


class SerializationError(ValueError):
    """Raised for malformed or invalid serialized SEAL objects."""


def _scratch_path() -> str:
    global _scratch_dir
    path = getattr(_local, "path", None)
    pid = os.getpid()
    if path is None or getattr(_local, "pid", None) != pid:
        with _dir_lock:
            if _scratch_dir is None or not os.path.isdir(_scratch_dir) or not _scratch_dir.endswith(f"-{pid}"):
                base = os.environ.get("TMPDIR") or tempfile.gettempdir()
                _scratch_dir = tempfile.mkdtemp(prefix="pmr-sealio-", suffix=f"-{pid}", dir=base)
        path = os.path.join(_scratch_dir, f"obj-{threading.get_ident()}.bin")
        _local.path, _local.pid = path, pid
    return path


def save_bytes(obj: Any) -> bytes:
    """Serialize a SEAL object (Ciphertext, Plaintext, GaloisKeys, Serializable...)."""
    path = _scratch_path()
    obj.save(path)
    with open(path, "rb") as fh:
        return fh.read()


def _load(factory: Callable[[], Any], ctx: Any, data: bytes, what: str) -> Any:
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < 16:
        raise SerializationError(f"{what}: empty or truncated data")
    path = _scratch_path()
    with open(path, "wb") as fh:
        fh.write(data)
    obj = factory()
    try:
        obj.load(ctx, path)
    except Exception as exc:  # SEAL raises std::logic_error / invalid_argument / runtime_error
        raise SerializationError(f"{what}: {type(exc).__name__}: {exc}") from None
    try:
        valid = sealapi.is_valid_for(obj, ctx)
    except TypeError:
        valid = True  # no overload for this type; SEAL's load already validated it
    if not valid:
        raise SerializationError(f"{what}: not valid for this context")
    return obj


def load_ciphertext(ctx: Any, data: bytes) -> Any:
    return _load(sealapi.Ciphertext, ctx, data, "ciphertext")


def load_galois_keys(ctx: Any, data: bytes) -> Any:
    return _load(sealapi.GaloisKeys, ctx, data, "galois keys")
