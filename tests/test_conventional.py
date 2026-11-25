"""Conventional (non-private) baseline (requirement 9).

Single-track and whole-database downloads are exact, and the conventional server records the
track index in its access log (that is the whole point of the contrast) while the PIR server
never receives it.
"""
from __future__ import annotations

import json

import pytest

from conftest import SMOKE_DIR, http_request, split_http_messages
from pmr.conventional.client import ConventionalClient


def test_single_track_download_exact(conventional_server, smoke_records):
    svc, url = conventional_server(SMOKE_DIR)
    client = ConventionalClient(url)
    try:
        for i in range(len(smoke_records)):
            res = client.retrieve_track(i)
            assert res.pcm == smoke_records[i], i
    finally:
        client.close()


def test_whole_database_download_exact(conventional_server, smoke_records):
    svc, url = conventional_server(SMOKE_DIR)
    client = ConventionalClient(url)
    try:
        for i in range(len(smoke_records)):
            res = client.retrieve_via_database(i)
            assert res.pcm == smoke_records[i], i
    finally:
        client.close()


def test_access_log_records_track_index(conventional_server, smoke_records):
    svc, url = conventional_server(SMOKE_DIR)
    client = ConventionalClient(url)
    try:
        assert svc.access_log == []
        client.retrieve_track(2)
        client.retrieve_track(0)
        assert [e["track"] for e in svc.access_log] == [2, 0]
        before = len(svc.access_log)
        client.retrieve_via_database(1)  # whole-database download logs no per-track index
        assert len(svc.access_log) == before
    finally:
        client.close()


def test_unknown_track_404_and_bad_index_400(conventional_server):
    _, url = conventional_server(SMOKE_DIR)
    assert http_request(url, "GET", "/v1/tracks/99/pcm")[0] == 404
    assert http_request(url, "GET", "/v1/tracks/abc/pcm")[0] == 400
    assert http_request(url, "GET", "/healthz")[0] == 200
    assert http_request(url, "GET", "/v1/catalog")[0] == 200
    stats = json.loads(http_request(url, "GET", "/v1/server/stats")[2])
    assert "access_log_entries" in stats


def test_pir_server_never_receives_index_unlike_conventional(
        conventional_server, pir_server, recording_proxy, smoke_records):
    """Side-by-side: the conventional server logs the index; the PIR wire never carries it."""
    from pmr.client.pirclient import PirHttpClient

    idx = 2
    conv_svc, conv_url = conventional_server(SMOKE_DIR)
    conv_client = ConventionalClient(conv_url)
    try:
        conv_client.retrieve_track(idx)
    finally:
        conv_client.close()
    assert conv_svc.access_log[-1]["track"] == idx  # the baseline learns the selection

    pir_svc, pir_url = pir_server(SMOKE_DIR)
    proxy = recording_proxy(pir_url)
    pir_client = PirHttpClient(proxy.base_url, record_wire=True)
    pir_client.open_session()
    res = pir_client.retrieve(idx, policy="pad")
    captured = proxy.snapshot()
    pir_client.close()

    assert res.pcm == smoke_records[idx]
    track = pir_svc.manifest["tracks"][idx]
    heads = b"\n".join(h for h, _ in split_http_messages(captured))
    # nothing that names the selected track reaches the PIR server (id/title in the textual
    # request head; distinctive long strings even inside bodies)
    assert track["id"].encode() not in heads and track["title"].encode() not in heads
    for needle in (track["file"].encode(), track["sha256_pcm"].encode()):
        assert needle not in captured
    # and the PIR service keeps no per-selection access log at all
    assert not hasattr(pir_svc, "access_log")
