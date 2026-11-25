"""BFV parameters, SEAL context construction and client-side parameter validation.

All cryptographic operations are delegated to Microsoft SEAL 4.1.1 through the
``tenseal.sealapi`` bindings shipped in the TenSEAL 0.3.14 wheel. This module only
chooses parameters and checks them; it implements no primitive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import tenseal.sealapi as sealapi

# Parameters inherited from the original prototype: N = 4096, t = 65537 (prime, t = 1 mod 2N,
# so batching is available) and SEAL's default coefficient modulus for N = 4096, which is the
# largest modulus allowed for 128-bit classical security by the HomomorphicEncryption.org
# standard table that SEAL enforces (109 bits = 36 + 36 + 37).
DEFAULT_POLY_MODULUS_DEGREE = 4096
DEFAULT_PLAIN_MODULUS = 65537
MIN_POLY_MODULUS_DEGREE = 4096


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    small = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for p in small:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for a in small:  # deterministic for n < 3.3e24
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


@dataclass(frozen=True)
class BFVParams:
    """Public BFV parameters shared by client and server."""

    poly_modulus_degree: int = DEFAULT_POLY_MODULUS_DEGREE
    plain_modulus: int = DEFAULT_PLAIN_MODULUS
    coeff_modulus: tuple[int, ...] = field(default=())  # explicit primes; empty = SEAL BFVDefault

    def resolved(self) -> "BFVParams":
        """Return a copy with the explicit coefficient-modulus primes filled in."""
        if self.coeff_modulus:
            return self
        primes = tuple(
            m.value()
            for m in sealapi.CoeffModulus.BFVDefault(self.poly_modulus_degree, sealapi.SEC_LEVEL_TYPE.TC128)
        )
        return BFVParams(self.poly_modulus_degree, self.plain_modulus, primes)

    def to_public_dict(self) -> dict[str, Any]:
        p = self.resolved()
        return {
            "scheme": "BFV",
            "poly_modulus_degree": p.poly_modulus_degree,
            "plain_modulus": p.plain_modulus,
            "coeff_modulus": list(p.coeff_modulus),
            "coeff_modulus_bits": [int(q).bit_length() for q in p.coeff_modulus],
            "security_level": "tc128",
            "library": "Microsoft SEAL 4.1.1 via TenSEAL 0.3.14 (tenseal.sealapi)",
        }

    @staticmethod
    def from_public_dict(d: dict[str, Any]) -> "BFVParams":
        if d.get("scheme") != "BFV":
            raise ValueError("unsupported scheme")
        coeff = d.get("coeff_modulus")
        if not isinstance(coeff, list) or not all(isinstance(q, int) for q in coeff):
            raise ValueError("coeff_modulus must be a list of integers")
        n, t = d.get("poly_modulus_degree"), d.get("plain_modulus")
        if not isinstance(n, int) or not isinstance(t, int):
            raise ValueError("poly_modulus_degree and plain_modulus must be integers")
        return BFVParams(n, t, tuple(coeff))


class ParameterError(ValueError):
    """Raised when parameters are invalid or below the required security level."""


@dataclass
class SealContextBundle:
    params: BFVParams
    context: Any  # sealapi.SEALContext
    first_parms_id: list[int]
    last_parms_id: list[int]
    slot_count: int

    @property
    def chain_length(self) -> int:
        n, cd = 0, self.context.first_context_data()
        while cd is not None:
            n += 1
            cd = cd.next_context_data()
        return n


def build_context(params: BFVParams, *, require_batching: bool = True) -> SealContextBundle:
    """Create a SEAL context and enforce 128-bit security and batching.

    The client calls this on parameters received from the server, so a server cannot make the
    client encrypt its selection under weak parameters.
    """
    p = params.resolved()
    n, t = p.poly_modulus_degree, p.plain_modulus
    if n < MIN_POLY_MODULUS_DEGREE or n & (n - 1):
        raise ParameterError(f"poly_modulus_degree must be a power of two >= {MIN_POLY_MODULUS_DEGREE}")
    if require_batching and not (_is_prime(t) and t % (2 * n) == 1):
        raise ParameterError("plain_modulus must be a prime congruent to 1 mod 2N for batching")
    if len(p.coeff_modulus) < 2:
        raise ParameterError("at least two coefficient-modulus primes are required (modulus switching)")
    if sum(int(q).bit_length() for q in p.coeff_modulus) > sealapi.CoeffModulus.MaxBitCount(
        n, sealapi.SEC_LEVEL_TYPE.TC128
    ):
        raise ParameterError("coefficient modulus exceeds the 128-bit security bound for this N")
    parms = sealapi.EncryptionParameters(sealapi.SCHEME_TYPE.BFV)
    parms.set_poly_modulus_degree(n)
    parms.set_coeff_modulus([sealapi.Modulus(int(q)) for q in p.coeff_modulus])
    parms.set_plain_modulus(sealapi.Modulus(t))
    ctx = sealapi.SEALContext(parms, True, sealapi.SEC_LEVEL_TYPE.TC128)
    if not ctx.parameters_set():
        raise ParameterError(f"SEAL rejected the parameters: {ctx.parameter_error_message()}")
    q = ctx.first_context_data().qualifiers()
    if q.sec_level != sealapi.SEC_LEVEL_TYPE.TC128:
        raise ParameterError("SEAL did not certify 128-bit security for these parameters")
    if require_batching and not q.using_batching:
        raise ParameterError("parameters do not support batching")
    slot_count = sealapi.BatchEncoder(ctx).slot_count() if q.using_batching else 0
    return SealContextBundle(p, ctx, ctx.first_parms_id(), ctx.last_parms_id(), slot_count)
