#!/usr/bin/env python3
"""Check whether TenSEAL 0.3.14's sealapi can produce seed-compressed (Serializable) ciphertexts.

SealPIR's 46 KB query relies on SEAL's Serializable<Ciphertext> from encrypt_symmetric. This
script probes the Python binding and, if it works, measures the serialized size of one fresh
ciphertext (N = 4096, BFVDefault(4096), t = 65537, default zstd compression) with and without
seed compression. sealapi's save() writes to a path; files go to the project-local TMPDIR.
Prints one JSON line.
"""
import json
import os
import tempfile

import tenseal.sealapi as s

parms = s.EncryptionParameters(s.SCHEME_TYPE.BFV)
parms.set_poly_modulus_degree(4096)
parms.set_coeff_modulus(s.CoeffModulus.BFVDefault(4096, s.SEC_LEVEL_TYPE.TC128))
parms.set_plain_modulus(s.Modulus(65537))
ctx = s.SEALContext(parms, True, s.SEC_LEVEL_TYPE.TC128)
kg = s.KeyGenerator(ctx)
enc = s.Encryptor(ctx, kg.secret_key())
pt = s.Plaintext("1")
result = {"binding": "tenseal.sealapi Encryptor.encrypt_symmetric(Plaintext) -> Serializable"}
try:
    ser = enc.encrypt_symmetric(pt)
    result["returned_type"] = type(ser).__name__
    result["has_save"] = hasattr(ser, "save")
    with tempfile.TemporaryDirectory() as d:
        p_seeded, p_full = os.path.join(d, "seeded.bin"), os.path.join(d, "full.bin")
        ser.save(p_seeded)
        ct = s.Ciphertext(ctx)
        enc.encrypt_symmetric(pt, ct)
        ct.save(p_full)
        result["seeded_symmetric_ciphertext_bytes"] = os.path.getsize(p_seeded)
        result["full_symmetric_ciphertext_bytes"] = os.path.getsize(p_full)
        loaded = s.Ciphertext(ctx)
        loaded.load(ctx, p_seeded)
        result["seeded_reloads"] = loaded.size() == 2
except Exception as e:  # noqa: BLE001 - the failure itself is the finding
    result["error_type"] = type(e).__name__
    result["error"] = str(e).splitlines()[0][:300]
print(json.dumps(result))
