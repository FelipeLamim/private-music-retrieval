"""HE backends: BFV (sealapi) and didactic (TenSEAL), key separation, input validation,
parameter validation, fresh randomness, oblivious expansion and noise (requirement 4).

These tests exercise the crypto layer directly (no HTTP) through BFVClientCrypto /
BFVServerCrypto and the didactic pair. They check functional correctness and that server-side
objects reject malformed or privacy-breaking inputs; they are NOT a cryptographic proof.
"""
from __future__ import annotations

import itertools

import numpy as np
import pytest
import tenseal.sealapi as sealapi

from pmr.he.bfv import (
    BFVClientCrypto,
    BFVServerCrypto,
    CryptoError,
    expansion_levels,
    galois_elements,
)
from pmr.he.didactic import DidacticClientCrypto, DidacticServerCrypto
from pmr.he.params import BFVParams, ParameterError, build_context
from pmr.he.seal_io import save_bytes

T = 65537


def db_columns(server: BFVServerCrypto, values):
    """Encode one block per record; record r gets the slot pattern values[r]."""
    return [server.encode_record_block(np.asarray(v, dtype=np.uint64)) for v in values]


def selector_recover(client, server, values, idx, *, galois=None, mode="selector"):
    r = len(values)
    blocks = db_columns(server, values)
    if mode == "selector":
        prepared = server.prepare_query("selector", client.selector_query(idx, r), r)
    else:
        gk = server.load_galois_keys(galois)
        prepared = server.prepare_query("expanded", [client.expanded_query(idx, r)], r, gk)
    resp = server.answer_block(prepared, blocks)
    return client.decrypt_slots(resp)


# =========================================================================== parameter validation

@pytest.mark.parametrize("params,needle", [
    (BFVParams(4096, 65536), "prime"),                         # t not prime
    (BFVParams(4096, 65539), "1 mod 2N"),                      # prime but 65539 % 8192 != 1
    (BFVParams(2048, 65537), "power of two"),                  # N below the minimum
    (BFVParams(4096, 65537, tuple([2**36 - 59] * 4)), "128-bit"),  # 144 bits > 109-bit bound
    (BFVParams(4096, 65537, (2**54 - 33,)), "at least two"),   # single prime, no mod switching
])
def test_build_context_rejects_bad_parameters(params, needle):
    with pytest.raises(ParameterError) as exc:
        build_context(params)
    assert needle in str(exc.value)


def test_build_context_accepts_default():
    bundle = build_context(BFVParams())
    assert bundle.slot_count == 4096
    assert bundle.chain_length >= 2  # mod switching available


# =========================================================================== key separation

def test_server_holds_no_secret_key():
    server = BFVServerCrypto(BFVParams())
    for attr in ("_secret_key", "_decryptor", "_keygen"):
        assert not hasattr(server, attr)
    assert not hasattr(server, "_secret_key_bytes_for_tests")
    # the client, by contrast, owns secret-key material
    client = BFVClientCrypto(BFVParams())
    assert hasattr(client, "_secret_key")
    assert len(client._secret_key_bytes_for_tests()) > 0


def test_didactic_server_rejects_secret_key_context():
    client = DidacticClientCrypto(BFVParams())
    # a public context is accepted; a secret-key context is refused
    DidacticServerCrypto(client.public_context(), 4096)
    with pytest.raises(CryptoError) as exc:
        DidacticServerCrypto(client._secret_context_for_tests(), 4096)
    assert "secret key" in str(exc.value)


def test_didactic_server_requires_public_key():
    client = DidacticClientCrypto(BFVParams())
    import tenseal as ts
    # a context with neither secret nor public key must be refused
    bare = ts.context(ts.SCHEME_TYPE.BFV, poly_modulus_degree=4096, plain_modulus=T,
                      coeff_mod_bit_sizes=[36, 36, 37], n_threads=1)
    blob = bare.serialize(save_public_key=False, save_secret_key=False,
                          save_galois_keys=False, save_relin_keys=False)
    with pytest.raises(CryptoError):
        DidacticServerCrypto(blob, 4096)


# =========================================================================== malformed query inputs

@pytest.fixture(scope="module")
def ctx_pair():
    client = BFVClientCrypto(BFVParams())
    server = BFVServerCrypto(BFVParams())
    return client, server


def test_server_rejects_garbage_ciphertext(ctx_pair):
    client, server = ctx_pair
    with pytest.raises(CryptoError):
        server.prepare_query("selector", [b"not a real ciphertext at all!!" * 4, *client.selector_query(0, 2)[1:]], 2)


def test_server_rejects_truncated_ciphertext(ctx_pair):
    client, server = ctx_pair
    good = client.selector_query(0, 2)
    with pytest.raises(CryptoError):
        server.prepare_query("selector", [good[0][:40], good[1]], 2)


def test_server_rejects_ntt_form_query(ctx_pair):
    client, server = ctx_pair
    ct = sealapi.Ciphertext()
    server._evaluator  # ensure attribute exists
    blob = client.selector_query(0, 2)[0]
    from pmr.he.seal_io import load_ciphertext
    loaded = load_ciphertext(client.bundle.context, blob)
    ev = sealapi.Evaluator(client.bundle.context)
    ev.transform_to_ntt_inplace(loaded)
    ntt_blob = save_bytes(loaded)
    with pytest.raises(CryptoError):
        server.prepare_query("selector", [ntt_blob, client.selector_query(0, 2)[1]], 2)


def test_server_rejects_wrong_level_query(ctx_pair):
    client, server = ctx_pair
    from pmr.he.seal_io import load_ciphertext
    loaded = load_ciphertext(client.bundle.context, client.selector_query(0, 2)[0])
    ev = sealapi.Evaluator(client.bundle.context)
    ev.mod_switch_to_next_inplace(loaded)  # now at a different (non-first) level
    with pytest.raises(CryptoError):
        server.prepare_query("selector", [save_bytes(loaded), client.selector_query(0, 2)[1]], 2)


def test_server_rejects_wrong_selector_count(ctx_pair):
    client, server = ctx_pair
    with pytest.raises(CryptoError):
        server.prepare_query("selector", client.selector_query(0, 2), num_records=3)


def test_server_rejects_transparent_query_ciphertext(ctx_pair):
    """A transparent (all-zero) ciphertext reveals its plaintext and makes products crash in
    SEAL. The server must reject it at load time for every db_form (not defer to a crash)."""
    client, server = ctx_pair
    ct = sealapi.Ciphertext(client.bundle.context)
    ct.resize(client.bundle.context, client.bundle.first_parms_id, 2)
    assert ct.is_transparent()
    transparent = save_bytes(ct)
    for db_form in ("ntt", "plain", "raw"):
        srv = BFVServerCrypto(BFVParams(), db_form=db_form)
        with pytest.raises(CryptoError):
            srv.prepare_query("selector", [transparent, client.selector_query(0, 2)[1]], 2)


def test_expanded_without_galois_keys_rejected(ctx_pair):
    client, server = ctx_pair
    with pytest.raises(CryptoError):
        server.prepare_query("expanded", [client.expanded_query(0, 4)], 4, galois_keys=None)


def test_expanded_wrong_blob_count_rejected(ctx_pair):
    client, server = ctx_pair
    gk = server.load_galois_keys(client.galois_keys(4))
    with pytest.raises(CryptoError):
        server.prepare_query("expanded", [client.expanded_query(0, 4), client.expanded_query(1, 4)], 4, gk)


# =========================================================================== malformed responses (client side)

def test_client_rejects_garbage_response():
    client = BFVClientCrypto(BFVParams())
    with pytest.raises(CryptoError):
        client.decrypt_slots(b"garbage response bytes that are long enough to pass length checks")


def test_client_rejects_truncated_response():
    client = BFVClientCrypto(BFVParams())
    server = BFVServerCrypto(BFVParams())
    blocks = db_columns(server, [np.arange(1, 4097), np.arange(2, 4098)])
    prepared = server.prepare_query("selector", client.selector_query(0, 2), 2)
    resp = server.answer_block(prepared, blocks)
    with pytest.raises(CryptoError):
        client.decrypt_slots(resp[:64])


def test_client_rejects_ntt_form_response():
    client = BFVClientCrypto(BFVParams())
    from pmr.he.seal_io import load_ciphertext
    server = BFVServerCrypto(BFVParams(), db_form="plain", mod_switch=False)
    blocks = db_columns(server, [np.arange(1, 4097), np.arange(2, 4098)])
    prepared = server.prepare_query("selector", client.selector_query(0, 2), 2)
    resp = server.answer_block(prepared, blocks)
    loaded = load_ciphertext(client.bundle.context, resp)
    ev = sealapi.Evaluator(client.bundle.context)
    ev.transform_to_ntt_inplace(loaded)
    with pytest.raises(CryptoError):
        client.decrypt_slots(save_bytes(loaded))


def test_didactic_client_rejects_garbage_response():
    client = DidacticClientCrypto(BFVParams())
    with pytest.raises(CryptoError):
        client.decrypt_slots(b"not a serialized bfv vector", 4096)


# =========================================================================== fresh randomness

@pytest.mark.parametrize("seeded", [True, False])
def test_fresh_randomness_queries_differ_but_decode_exact(seeded):
    client = BFVClientCrypto(BFVParams(), seeded=seeded)
    server = BFVServerCrypto(BFVParams())
    values = [np.full(4096, 11, np.uint64), np.full(4096, 22, np.uint64), np.full(4096, 33, np.uint64)]
    blocks = db_columns(server, values)
    q1 = client.selector_query(1, 3)
    q2 = client.selector_query(1, 3)
    assert q1 != q2, "two queries for the same index must differ byte-wise (fresh randomness)"
    for q in (q1, q2):
        prepared = server.prepare_query("selector", q, 3)
        out = client.decrypt_slots(server.answer_block(prepared, blocks))
        assert int(out[0]) == 22 and int(out[2048]) == 22


# =========================================================================== selector round trips (ablation grid)

@pytest.mark.parametrize("db_form,mod_switch,seeded", list(itertools.product(
    ("ntt", "plain", "raw"), (True, False), (True, False))))
def test_selector_round_trip_grid(db_form, mod_switch, seeded):
    client = BFVClientCrypto(BFVParams(), seeded=seeded)
    server = BFVServerCrypto(BFVParams(), db_form=db_form, mod_switch=mod_switch)
    values = [np.full(4096, (r * 7 + 3) % (T - 1) + 1, np.uint64) for r in range(5)]
    blocks = db_columns(server, values)
    for idx in range(5):
        prepared = server.prepare_query("selector", client.selector_query(idx, 5), 5)
        out = client.decrypt_slots(server.answer_block(prepared, blocks))
        assert int(out[0]) == (idx * 7 + 3) % (T - 1) + 1


# =========================================================================== expansion

def test_expansion_helpers():
    assert expansion_levels(1) == 1 and expansion_levels(2) == 1 and expansion_levels(3) == 2
    assert expansion_levels(16) == 4 and expansion_levels(17) == 5
    assert len(galois_elements(10, 4096)) == expansion_levels(10)


@pytest.mark.parametrize("R", [1, 2, 3, 10, 16, 17, 37])
@pytest.mark.parametrize("db_form", ["ntt", "raw"])
def test_expanded_mode_selects_correctly(R, db_form):
    """Each expanded selector, end-to-end through answer_block, recovers exactly record idx."""
    client = BFVClientCrypto(BFVParams())
    server = BFVServerCrypto(BFVParams(), db_form=db_form)
    values = [np.full(4096, (r % (T - 2)) + 1, np.uint64) for r in range(R)]
    blocks = db_columns(server, values)
    galois = client.galois_keys(R)
    gk = server.load_galois_keys(galois)
    for idx in range(R):
        prepared = server.prepare_query("expanded", [client.expanded_query(idx, R)], R, gk)
        out = client.decrypt_slots(server.answer_block(prepared, blocks))
        assert int(out[0]) == (idx % (T - 2)) + 1, (R, idx)


def test_expanded_query_index_out_of_range():
    client = BFVClientCrypto(BFVParams())
    with pytest.raises(ValueError):
        client.expanded_query(5, 5)
    with pytest.raises(ValueError):
        client.selector_query(-1, 3)


# =========================================================================== noise budget

def test_noise_budget_positive_after_mod_switch():
    client = BFVClientCrypto(BFVParams())
    server = BFVServerCrypto(BFVParams(), db_form="ntt", mod_switch=True)
    values = [np.full(4096, 40000, np.uint64), np.full(4096, 50000, np.uint64)]
    blocks = db_columns(server, values)
    prepared = server.prepare_query("selector", client.selector_query(1, 2), 2)
    resp = server.answer_block(prepared, blocks)
    assert client.noise_budget(resp) > 0
    out = client.decrypt_slots(resp)  # would raise if the budget were exhausted
    assert int(out[0]) == 50000


def test_mod_switch_shrinks_response():
    client = BFVClientCrypto(BFVParams())
    values = [np.full(4096, 12345, np.uint64), np.full(4096, 54321, np.uint64)]
    sizes = {}
    for ms in (True, False):
        server = BFVServerCrypto(BFVParams(), db_form="ntt", mod_switch=ms)
        blocks = db_columns(server, values)
        prepared = server.prepare_query("selector", client.selector_query(0, 2), 2)
        sizes[ms] = len(server.answer_block(prepared, blocks))
    assert sizes[True] < sizes[False]  # switching to the last modulus shrinks the ciphertext


# =========================================================================== didactic backend

@pytest.mark.parametrize("packing_slots", [4096, 2048])
def test_didactic_round_trip(packing_slots):
    client = DidacticClientCrypto(BFVParams())
    server = DidacticServerCrypto(client.public_context(), packing_slots)
    values = [[(r + 1)] * packing_slots for r in range(3)]
    blocks = [DidacticServerCrypto.encode_record_block(np.asarray(v, np.uint64)) for v in values]
    for idx in range(3):
        sel = client.selector_query(idx, 3, packing_slots)
        prepared = server.prepare_query(sel, 3)
        out = client.decrypt_slots(server.answer_block(prepared, blocks), packing_slots)
        assert int(out[0]) == idx + 1


def test_didactic_rejects_selector_size_mismatch():
    client = DidacticClientCrypto(BFVParams())
    server = DidacticServerCrypto(client.public_context(), 4096)
    sel = client.selector_query(0, 2, 2048)  # wrong slot count
    with pytest.raises(CryptoError):
        server.prepare_query(sel, 2)


def test_didactic_silent_record_recovers_zero():
    """The didactic answer starts from a fresh Enc(0) and skips all-zero records; a silent
    record therefore decodes back to all zeros."""
    client = DidacticClientCrypto(BFVParams())
    server = DidacticServerCrypto(client.public_context(), 4096)
    blocks = [DidacticServerCrypto.encode_record_block(np.zeros(4096, np.uint64)),
              DidacticServerCrypto.encode_record_block(np.full(4096, 7, np.uint64))]
    prepared = server.prepare_query(client.selector_query(0, 2, 4096), 2)
    out = client.decrypt_slots(server.answer_block(prepared, blocks), 4096)
    assert int(out[0]) == 0 and int(out[123]) == 0
