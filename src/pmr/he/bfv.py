"""Optimized BFV backend over Microsoft SEAL 4.1.1 (``tenseal.sealapi``).

Client side (:class:`BFVClientCrypto`) owns the only secret key. Server side
(:class:`BFVServerCrypto`) holds public parameters, the database plaintexts and, in the
"expanded" query mode, the client's Galois keys. No primitive is implemented here: key
generation, encryption, NTT, products, automorphisms, modulus switching and serialization are
SEAL calls. The query-expansion procedure (Angel et al., IEEE S&P 2018, Sec. 3) is composed from
SEAL operations; it is not the SealPIR library.

Query modes
  * ``selector``: R ciphertexts; ciphertext i encrypts the constant polynomial [i == x], i.e. a
    vector with every slot equal to the selector bit (the original didactic query).
  * ``expanded``: one ciphertext encrypting inv(2^l) * X^x (coefficient encoding); the server
    expands it obliviously into the R selector ciphertexts with l = ceil(log2 R) rounds of
    Galois automorphisms. Requires R <= N.

Database forms (ablation of the preprocessing step)
  * ``ntt``:   plaintexts encoded once and kept in NTT form (fastest, most memory)
  * ``plain``: plaintexts encoded once, coefficient form (SEAL transforms on every product)
  * ``raw``:   only slot values kept; encoded on every request (no preprocessing)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import tenseal.sealapi as sealapi

from .params import BFVParams, SealContextBundle, build_context
from .seal_io import SerializationError, load_ciphertext, load_galois_keys, save_bytes

QUERY_MODES = ("selector", "expanded")
DB_FORMS = ("ntt", "plain", "raw")


class CryptoError(ValueError):
    """Invalid cryptographic input (wrong parameters, malformed ciphertext, exhausted noise)."""


def expansion_levels(num_records: int) -> int:
    return max(1, math.ceil(math.log2(num_records))) if num_records > 1 else 1


def galois_elements(num_records: int, n: int) -> list[int]:
    return [n // (1 << j) + 1 for j in range(expansion_levels(num_records))]


# --------------------------------------------------------------------------- client

class BFVClientCrypto:
    """Trusted-client key material and operations. The secret key never leaves this object."""

    def __init__(self, params: BFVParams, *, seeded: bool = True):
        self.bundle: SealContextBundle = build_context(params)
        ctx = self.bundle.context
        self.params = self.bundle.params
        self.n = self.params.poly_modulus_degree
        self.t = self.params.plain_modulus
        self.seeded = seeded
        self._keygen = sealapi.KeyGenerator(ctx)
        self._secret_key = self._keygen.secret_key()
        self._encryptor = sealapi.Encryptor(ctx, self._secret_key)  # symmetric-key encryption
        self._decryptor = sealapi.Decryptor(ctx, self._secret_key)
        self._encoder = sealapi.BatchEncoder(ctx)
        self._one = sealapi.Plaintext("1")
        self._zero = sealapi.Plaintext("0")

    # The secret key is intentionally not exposed. For tests that must verify that it never
    # appears on the wire, a serialized copy can be produced explicitly:
    def _secret_key_bytes_for_tests(self) -> bytes:
        return save_bytes(self._secret_key)

    def _encrypt(self, pt: Any) -> bytes:
        if self.seeded:
            return save_bytes(self._encryptor.encrypt_symmetric(pt))  # seeded Serializable
        ct = sealapi.Ciphertext()
        self._encryptor.encrypt_symmetric(pt, ct)
        return save_bytes(ct)

    def selector_query(self, index: int, num_records: int) -> list[bytes]:
        if not 0 <= index < num_records:
            raise ValueError("index out of range")
        return [self._encrypt(self._one if i == index else self._zero) for i in range(num_records)]

    def expanded_query(self, index: int, num_records: int) -> bytes:
        if not 0 <= index < num_records:
            raise ValueError("index out of range")
        if num_records > self.n:
            raise ValueError("expanded mode supports at most N records")
        m = 1 << expansion_levels(num_records)
        inv = pow(m, -1, self.t)
        pt = sealapi.Plaintext(f"{inv:X}x^{index}" if index else f"{inv:X}")
        return self._encrypt(pt)

    def galois_keys(self, num_records: int) -> bytes:
        elts = galois_elements(num_records, self.n)
        if self.seeded:
            return save_bytes(self._keygen.create_galois_keys(elts))
        gk = sealapi.GaloisKeys()
        self._keygen.create_galois_keys(elts, gk)
        return save_bytes(gk)

    def decrypt_slots(self, blob: bytes, *, check_noise: bool = True) -> np.ndarray:
        """Decrypt one response ciphertext into batching slot values (uint64 array)."""
        try:
            ct = load_ciphertext(self.bundle.context, blob)
        except SerializationError as exc:
            raise CryptoError(str(exc)) from None
        if ct.size() != 2 or ct.is_ntt_form():
            raise CryptoError("unexpected response ciphertext shape")
        if check_noise and self._decryptor.invariant_noise_budget(ct) <= 0:
            raise CryptoError("response noise budget exhausted; decryption would be incorrect")
        pt = sealapi.Plaintext()
        self._decryptor.decrypt(ct, pt)
        return np.asarray(self._encoder.decode_uint64(pt), dtype=np.uint64)

    def noise_budget(self, blob: bytes) -> int:
        return int(self._decryptor.invariant_noise_budget(load_ciphertext(self.bundle.context, blob)))


# --------------------------------------------------------------------------- server

@dataclass
class PreparedQuery:
    """Selector ciphertexts ready for reuse across all blocks of one retrieval."""

    selectors: list[Any]  # sealapi.Ciphertext, NTT form when db_form == "ntt"
    mode: str
    prepare_ms: float = 0.0


class BFVServerCrypto:
    """Server-side evaluation. Holds no secret key; never decrypts."""

    def __init__(self, params: BFVParams, *, db_form: str = "ntt", mod_switch: bool = True):
        if db_form not in DB_FORMS:
            raise ValueError(f"db_form must be one of {DB_FORMS}")
        self.bundle = build_context(params)
        self.params = self.bundle.params
        self.n = self.params.poly_modulus_degree
        self.db_form = db_form
        self.mod_switch = mod_switch
        ctx = self.bundle.context
        self._ctx = ctx
        self._evaluator = sealapi.Evaluator(ctx)
        self._encoder = sealapi.BatchEncoder(ctx)
        self._first = self.bundle.first_parms_id
        self._last = self.bundle.last_parms_id
        self._monomials = [sealapi.Plaintext(f"1x^{self.n - (1 << j)}") for j in range(int(math.log2(self.n)))]

    # -- database preprocessing
    def encode_record_block(self, slot_values: np.ndarray) -> Any:
        """Turn one record block (uint64 slot values) into the stored representation."""
        if self.db_form == "raw":
            return np.ascontiguousarray(slot_values, dtype=np.uint64)
        pt = sealapi.Plaintext()
        self._encoder.encode(slot_values.tolist(), pt)
        if self.db_form == "ntt":
            self._evaluator.transform_to_ntt_inplace(pt, self._first)
        return pt

    def _as_plaintext(self, stored: Any) -> Any:
        if self.db_form != "raw":
            return stored
        pt = sealapi.Plaintext()
        self._encoder.encode(stored.tolist(), pt)
        return pt

    # -- query handling
    def load_galois_keys(self, blob: bytes) -> Any:
        try:
            return load_galois_keys(self._ctx, blob)
        except SerializationError as exc:
            raise CryptoError(str(exc)) from None

    def _load_query_ct(self, blob: bytes) -> Any:
        try:
            ct = load_ciphertext(self._ctx, blob)
        except SerializationError as exc:
            raise CryptoError(str(exc)) from None
        if ct.size() != 2 or ct.is_ntt_form() or ct.parms_id() != self._first:
            raise CryptoError("query ciphertext has unexpected size, form or level")
        if ct.is_transparent():
            # A transparent ciphertext reveals its plaintext; SEAL refuses to multiply it, which
            # otherwise surfaced as an uncaught RuntimeError (HTTP 500) in plain/raw db_form.
            raise CryptoError("query ciphertext is transparent (reveals its plaintext); refusing")
        return ct

    def expand(self, ct: Any, num_records: int, galois_keys: Any) -> list[Any]:
        """Oblivious expansion of one query ciphertext into ``num_records`` selectors."""
        # Round j turns entry k (0 <= k < 2^j) into entries k and k + 2^j; bit j of the final
        # index is decided in round j. Since 2^j < R for every round but the last, only the
        # last round can produce indices >= R, and those are skipped.
        if not 1 <= num_records <= self.n:
            raise CryptoError("expanded mode supports 1..N records")
        ev = self._evaluator
        levels = expansion_levels(num_records)
        cts: list[Any] = [ct]
        for j in range(levels):
            elt = self.n // (1 << j) + 1
            if not galois_keys.has_key(elt):
                raise CryptoError("missing Galois key for query expansion")
            mono = self._monomials[j]
            half = len(cts)
            nxt: list[Any] = [None] * (2 * half)
            for k, c0 in enumerate(cts):
                g0 = sealapi.Ciphertext()
                ev.apply_galois(c0, elt, galois_keys, g0)
                a = sealapi.Ciphertext()
                ev.add(c0, g0, a)
                nxt[k] = a
                if k + half < num_records:
                    c1 = sealapi.Ciphertext()
                    ev.multiply_plain(c0, mono, c1)  # c0 * X^(N - 2^j) = -c0 * X^(-2^j)
                    ev.negate_inplace(c1)
                    g1 = sealapi.Ciphertext()
                    ev.apply_galois(c1, elt, galois_keys, g1)
                    b = sealapi.Ciphertext()
                    ev.add(c1, g1, b)
                    nxt[k + half] = b
            cts = nxt
        out = cts[:num_records]
        if any(c is None for c in out):
            raise CryptoError("expansion produced an unexpected number of selectors")
        return out

    def prepare_query(self, mode: str, blobs: Sequence[bytes], num_records: int, galois_keys: Any = None) -> PreparedQuery:
        if mode == "selector":
            if len(blobs) != num_records:
                raise CryptoError("selector query must contain one ciphertext per record")
            selectors = [self._load_query_ct(b) for b in blobs]
        elif mode == "expanded":
            if len(blobs) != 1:
                raise CryptoError("expanded query must contain exactly one ciphertext")
            if galois_keys is None:
                raise CryptoError("expanded query requires Galois keys from session setup")
            selectors = self.expand(self._load_query_ct(blobs[0]), num_records, galois_keys)
        else:
            raise CryptoError(f"unknown query mode {mode!r}")
        if self.db_form == "ntt":
            for c in selectors:
                self._evaluator.transform_to_ntt_inplace(c)
        return PreparedQuery(selectors, mode)

    def answer_block(self, prepared: PreparedQuery, record_blocks: Sequence[Any]) -> bytes:
        """Compute sum_i sel_i * pt_i for one block index and serialize the result."""
        if len(record_blocks) != len(prepared.selectors):
            raise CryptoError("record/selector count mismatch")
        ev = self._evaluator
        acc = sealapi.Ciphertext()
        tmp = sealapi.Ciphertext()
        first = True
        for sel, stored in zip(prepared.selectors, record_blocks):
            pt = self._as_plaintext(stored)
            if first:
                ev.multiply_plain(sel, pt, acc)
                first = False
            else:
                ev.multiply_plain(sel, pt, tmp)
                ev.add_inplace(acc, tmp)
        if self.db_form == "ntt":
            ev.transform_from_ntt_inplace(acc)
        if self.mod_switch:
            ev.mod_switch_to_inplace(acc, self._last)
        return save_bytes(acc)
