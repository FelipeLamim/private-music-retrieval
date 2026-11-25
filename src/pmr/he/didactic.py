"""Didactic baseline backend: the original prototype's protocol, generalized to whole tracks.

Uses TenSEAL's high-level ``BFVVector`` API as in the original experiment:
one encrypted selector vector per record (the selector bit repeated in every slot), the
server receives the public context (public key, no secret key), starts from a fresh
encryption of zero, and adds ``selector_i * record_i`` for every non-zero record. Every
product re-encodes the record (no preprocessing) and the response stays at the full
coefficient-modulus level (no modulus switching).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import tenseal as ts

from .bfv import CryptoError
from .params import BFVParams, build_context


class DidacticClientCrypto:
    def __init__(self, params: BFVParams):
        self.params = params.resolved()
        build_context(self.params)  # validates N, t and the 128-bit bound before use
        self.n = self.params.poly_modulus_degree
        self.t = self.params.plain_modulus
        self._ctx = ts.context(
            ts.SCHEME_TYPE.BFV,
            poly_modulus_degree=self.n,
            plain_modulus=self.t,
            coeff_mod_bit_sizes=[int(q).bit_length() for q in self.params.coeff_modulus],
            n_threads=1,
        )

    def public_context(self) -> bytes:
        return self._ctx.serialize(
            save_public_key=True, save_secret_key=False, save_galois_keys=False, save_relin_keys=False
        )

    def _secret_context_for_tests(self) -> bytes:
        return self._ctx.serialize(save_secret_key=True)

    def selector_query(self, index: int, num_records: int, slots: int) -> list[bytes]:
        if not 0 <= index < num_records:
            raise ValueError("index out of range")
        return [ts.bfv_vector(self._ctx, [int(i == index)] * slots).serialize() for i in range(num_records)]

    def decrypt_slots(self, blob: bytes, slots: int) -> np.ndarray:
        try:
            values = ts.bfv_vector_from(self._ctx, blob).decrypt()
        except Exception as exc:
            raise CryptoError(f"cannot decrypt response: {type(exc).__name__}: {exc}") from None
        if len(values) != slots:
            raise CryptoError("response has an unexpected number of slots")
        return np.asarray(values, dtype=np.int64)


class DidacticPrepared:
    def __init__(self, selectors: list):
        self.selectors = selectors
        self.mode = "selector"


class DidacticServerCrypto:
    def __init__(self, public_context: bytes, slots: int):
        try:
            self._ctx = ts.context_from(public_context, n_threads=1)
        except Exception as exc:
            raise CryptoError(f"invalid public context: {type(exc).__name__}") from None
        if self._ctx.has_secret_key():
            raise CryptoError("refusing a context that contains a secret key")
        if not self._ctx.has_public_key():
            raise CryptoError("public context must contain the public key")
        self.slots = slots

    @staticmethod
    def encode_record_block(slot_values: np.ndarray) -> list[int]:
        return [int(x) for x in slot_values]

    def prepare_query(self, blobs: Sequence[bytes], num_records: int) -> DidacticPrepared:
        if len(blobs) != num_records:
            raise CryptoError("selector query must contain one ciphertext per record")
        selectors = []
        for b in blobs:
            try:
                v = ts.bfv_vector_from(self._ctx, b)
            except Exception as exc:
                raise CryptoError(f"invalid selector ciphertext: {type(exc).__name__}") from None
            if v.size() != self.slots:
                raise CryptoError("selector size does not match the block size")
            selectors.append(v)
        return DidacticPrepared(selectors)

    def answer_block(self, prepared: DidacticPrepared, record_blocks: Sequence[list[int]]) -> bytes:
        if len(record_blocks) != len(prepared.selectors):
            raise CryptoError("record/selector count mismatch")
        result = ts.bfv_vector(self._ctx, [0] * self.slots)  # fresh Enc(0) with the public key
        for sel, record in zip(prepared.selectors, record_blocks):
            if any(record):  # decision depends only on public database content
                result += sel * record
        return result.serialize()
