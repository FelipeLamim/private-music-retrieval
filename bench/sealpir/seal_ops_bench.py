#!/usr/bin/env python3
"""Calibration twin of seal_ops_bench.cpp, run through TenSEAL 0.3.14's sealapi (SEAL 4.1.1).

Same parameters (BFV, N = 4096, BFVDefault(4096) = {36, 36, 37}), same operations, one thread.
Ciphertext serialization is skipped here: sealapi's save() only writes to a file path.
usage: seal_ops_bench.py [reps] [seed]  -> one JSON object per plain modulus on stdout
"""
import json
import random
import statistics
import sys
import time

import tenseal
import tenseal.sealapi as s


def stats(v):
    v = sorted(v)
    q = statistics.quantiles(v, n=4, method="inclusive")
    return {"median": statistics.median(v), "p25": q[0], "p75": q[2], "min": v[0]}


def time_op(reps, warm, f):
    for _ in range(warm):
        f()
    out = []
    for _ in range(reps):
        t0 = time.perf_counter_ns()
        f()
        out.append((time.perf_counter_ns() - t0) / 1e6)
    return stats(out)


def run(t_value, reps, seed):
    n, r = 4096, 10
    parms = s.EncryptionParameters(s.SCHEME_TYPE.BFV)
    parms.set_poly_modulus_degree(n)
    parms.set_coeff_modulus(s.CoeffModulus.BFVDefault(n, s.SEC_LEVEL_TYPE.TC128))
    parms.set_plain_modulus(s.Modulus(t_value))
    ctx = s.SEALContext(parms, True, s.SEC_LEVEL_TYPE.TC128)
    kg = s.KeyGenerator(ctx)
    pk = s.PublicKey()
    kg.create_public_key(pk)
    gk = s.GaloisKeys()
    kg.create_galois_keys([3], gk)
    enc = s.Encryptor(ctx, pk)
    ev = s.Evaluator(ctx)
    be = s.BatchEncoder(ctx)
    rng = random.Random(seed)

    def rand_pt(ntt):
        p = s.Plaintext()
        be.encode([rng.randrange(t_value) for _ in range(n)], p)
        if ntt:
            ev.transform_to_ntt_inplace(p, ctx.first_parms_id())
        return p

    def new_ct():
        return s.Ciphertext(ctx)

    pt = rand_pt(False)
    ct = new_ct()
    enc.encrypt(pt, ct)
    ct_ntt = new_ct()
    ev.transform_to_ntt(ct, ct_ntt)
    pts, sel = [], []
    for _ in range(r):
        pts.append(rand_pt(True))
        c = new_ct()
        enc.encrypt(rand_pt(False), c)
        ev.transform_to_ntt_inplace(c)
        sel.append(c)
    pt_ntt = pts[0]
    dest, tmp = new_ct(), new_ct()
    acc = new_ct()
    ev.transform_to_ntt(ct, acc)

    res = {}
    res["ntt_fwd"] = time_op(reps, 20, lambda: ev.transform_to_ntt(ct, dest))
    res["ntt_inv"] = time_op(reps, 20, lambda: ev.transform_from_ntt(ct_ntt, dest))
    res["mul_plain_ntt"] = time_op(reps, 20, lambda: ev.multiply_plain(ct_ntt, pt_ntt, dest))
    res["add_inplace"] = time_op(reps, 20, lambda: ev.add_inplace(acc, ct_ntt))
    res["mod_switch_next"] = time_op(reps, 20, lambda: ev.mod_switch_to_next(ct, dest))
    res["apply_galois"] = time_op(reps, 20, lambda: ev.apply_galois(ct, 3, gk, dest))

    def products():
        ev.multiply_plain(sel[0], pts[0], acc)
        for i in range(1, r):
            ev.multiply_plain(sel[i], pts[i], tmp)
            ev.add_inplace(acc, tmp)

    def full():
        products()
        ev.transform_from_ntt_inplace(acc)
        ev.mod_switch_to_next_inplace(acc)

    res["block_R10_products"] = time_op(reps, 20, products)
    res["block_R10_full"] = time_op(reps, 20, full)
    return {"impl": "python-tenseal-sealapi", "tenseal_version": tenseal.__version__,
            "seal_version": "4.1.1 (pinned by TenSEAL 0.3.14 cmake/seal.cmake)",
            "python": sys.version.split()[0], "N": n, "plain_modulus": t_value, "reps": reps,
            "ops_ms": res}


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261121
    for t in (65537, s.PlainModulus.Batching(4096, 21).value()):
        print(json.dumps(run(t, reps, seed)), flush=True)


if __name__ == "__main__":
    main()
