"""End-to-end PIR engine recovery for every backend variant (requirement 5).

``retrieve_in_process`` runs one query and reuses it for every block, exactly like the real
client loop, then trims padding. Exactness is byte-for-byte against the catalog PCM.
"""
from __future__ import annotations

import hashlib
import itertools

import pytest

from conftest import FULL_DIR, rand_pcm
from pmr.catalog import load_catalog
from pmr.he.params import BFVParams
from pmr.pir.engine import (
    BackendConfig,
    PirClientEngine,
    PirServerEngine,
    retrieve_in_process,
)

OPT_COMBOS = [
    BackendConfig("optimized", pk, qm, df, ms, True, 4096, BFVParams())
    for pk, qm, df, ms in itertools.product(("u16", "u8"), ("selector", "expanded"),
                                            ("ntt", "plain", "raw"), (True, False))
]
DID_COMBOS = [BackendConfig.didactic("u8"), BackendConfig.didactic("u16")]
ALL_COMBOS = OPT_COMBOS + DID_COMBOS
ALL_IDS = [c.label() for c in ALL_COMBOS]


@pytest.mark.parametrize("config", ALL_COMBOS, ids=ALL_IDS)
def test_full_track_recovery_every_variant(config, smoke_records):
    server = PirServerEngine(smoke_records, config)
    client = PirClientEngine(config, server.layout)
    for i in range(len(smoke_records)):
        pad = retrieve_in_process(server, client, i, stop_at_track_end=False)
        stop = retrieve_in_process(server, client, i, stop_at_track_end=True)
        assert pad == smoke_records[i], f"{config.label()} track {i} (policy pad)"
        assert stop == smoke_records[i], f"{config.label()} track {i} (policy stop)"


SMALL = [BackendConfig(), BackendConfig(packing="u8"),
         BackendConfig.didactic("u8"), BackendConfig.didactic("u16")]
SMALL_IDS = [c.label() for c in SMALL]


@pytest.mark.parametrize("config", SMALL, ids=SMALL_IDS)
def test_silent_and_partial_blocks(make_catalog, config):
    bb = 8192
    pcms = [
        rand_pcm(2 * bb, seed=1),   # two full blocks of real audio
        bytes(2 * bb),              # a silent (all-zero) track, two blocks
        rand_pcm(bb + 2, seed=2),   # last block carries only two bytes (partial block)
    ]
    d = make_catalog(pcms)
    _, records = load_catalog(str(d))
    server = PirServerEngine(records, config)
    client = PirClientEngine(config, server.layout)
    for i in range(len(records)):
        assert retrieve_in_process(server, client, i) == records[i], (config.label(), i)


@pytest.mark.parametrize("config", [BackendConfig(), BackendConfig(query_mode="expanded"),
                                    BackendConfig.didactic("u16")],
                         ids=["opt-selector", "opt-expanded", "didactic"])
def test_single_track_catalog_R1(make_catalog, config):
    d = make_catalog([rand_pcm(3 * 8192, seed=5)])
    _, records = load_catalog(str(d))
    assert server_client_recover(records, config, 0) == records[0]


@pytest.mark.parametrize("packing", ["u16", "u8"])
@pytest.mark.parametrize("slots", [1024, 2048])
def test_small_slot_counts(smoke_records, packing, slots):
    config = BackendConfig(packing=packing, slots=slots)
    server = PirServerEngine(smoke_records, config)
    assert server.packing.block_bytes == slots * (2 if packing == "u16" else 1)
    client = PirClientEngine(config, server.layout)
    for i in range(len(smoke_records)):
        assert retrieve_in_process(server, client, i) == smoke_records[i]


def test_exact_block_multiple_lengths(make_catalog):
    """Lengths that are an exact multiple, one byte over and one byte under a block boundary."""
    bb = 8192
    pcms = [rand_pcm(bb, 1), rand_pcm(bb + 2, 2), rand_pcm(bb - 2, 3), rand_pcm(2 * bb, 4)]
    d = make_catalog(pcms)
    _, records = load_catalog(str(d))
    config = BackendConfig()
    server = PirServerEngine(records, config)
    client = PirClientEngine(config, server.layout)
    for i in range(len(records)):
        assert retrieve_in_process(server, client, i, stop_at_track_end=True) == records[i]
        assert retrieve_in_process(server, client, i, stop_at_track_end=False) == records[i]


def server_client_recover(records, config, index, *, stop=True):
    server = PirServerEngine(records, config)
    client = PirClientEngine(config, server.layout)
    return retrieve_in_process(server, client, index, stop_at_track_end=stop)


@pytest.mark.slow
def test_full_catalog_single_track_recovery():
    """One full ~3-minute track from data/catalog/full with the default optimized u16 backend."""
    manifest, records = load_catalog(str(FULL_DIR))
    config = BackendConfig()  # optimized, u16, selector, ntt, mod switch, seeded
    server = PirServerEngine(records, config)
    client = PirClientEngine(config, server.layout)
    index = 3
    out = retrieve_in_process(server, client, index, stop_at_track_end=True)
    assert out == records[index]
    assert hashlib.sha256(out).hexdigest() == manifest["tracks"][index]["sha256_pcm"]
