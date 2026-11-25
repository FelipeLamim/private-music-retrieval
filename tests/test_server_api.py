"""PIR server HTTP API: malformed requests map to proper 4xx, never 500 (requirement 6).

Also: non-loopback binds are refused, and a process-sharded server (--workers 2) returns the
same plaintext as --workers 1.
"""
from __future__ import annotations

import json

import pytest
import tenseal.sealapi as sealapi

from conftest import SMOKE_DIR, http_request, raw_request, status_line
from pmr.he.seal_io import save_bytes
from pmr.pir import wire
from pmr.server.app import Limits
from pmr.pir.engine import BackendConfig


def open_session(base_url):
    from pmr.client.pirclient import PirHttpClient

    client = PirHttpClient(base_url)
    client.open_session()
    return client


def post_query(base_url, client, index=0):
    blobs = client.engine.make_query(index)
    kind = ("query.expanded" if client.config.backend == "optimized"
            and client.config.query_mode == "expanded" else "query.selector")
    body = wire.pack(kind, blobs)
    status, _, data = http_request(base_url, "POST", f"/v1/pir/sessions/{client.session}/queries", body)
    return status, data


# =========================================================================== happy-path endpoints

def test_public_endpoints(pir_server):
    svc, url = pir_server(SMOKE_DIR)
    for path in ("/healthz", "/v1/catalog", "/v1/pir/params", "/v1/server/stats"):
        status, _, data = http_request(url, "GET", path)
        assert status == 200, (path, status)
        json.loads(data)
    # the public catalog carries metadata but the params doc describes the backend/layout
    params = json.loads(http_request(url, "GET", "/v1/pir/params")[2])
    assert params["layout"]["num_records"] == 4
    assert params["backend"]["backend"] == "optimized"


def test_unknown_path_404(pir_server):
    _, url = pir_server(SMOKE_DIR)
    status, _, _ = http_request(url, "GET", "/v1/nope")
    assert status == 404
    status, _, _ = http_request(url, "POST", "/v1/pir/unknown")
    assert status == 404


# =========================================================================== token / session validation

@pytest.mark.parametrize("token", ["short", "bad.char", "bad!char", "x" * 65, "-"])
def test_bad_session_token_format_404(pir_server, token):
    _, url = pir_server(SMOKE_DIR)
    status, _, _ = http_request(url, "DELETE", f"/v1/pir/sessions/{token}")
    assert status == 404


def test_unknown_session_404(pir_server):
    _, url = pir_server(SMOKE_DIR)
    tok = "A" * 22  # well-formed but never issued
    status, _, _ = http_request(url, "DELETE", f"/v1/pir/sessions/{tok}")
    assert status == 404
    status, _, data = http_request(url, "POST", f"/v1/pir/sessions/{tok}/queries", b"")
    assert status == 404


def test_unknown_query_404(pir_server):
    _, url = pir_server(SMOKE_DIR)
    tok = "B" * 22
    status, _, _ = http_request(url, "GET", f"/v1/pir/queries/{tok}/blocks?start=0&count=1")
    assert status == 404
    status, _, _ = http_request(url, "DELETE", f"/v1/pir/queries/{tok}")
    assert status == 404


@pytest.mark.parametrize("token", ["bad!", "x", "y" * 100])
def test_bad_query_token_format_404(pir_server, token):
    _, url = pir_server(SMOKE_DIR)
    status, _, _ = http_request(url, "GET", f"/v1/pir/queries/{token}/blocks?start=0&count=1")
    assert status == 404


# =========================================================================== blocks parameters

def test_block_request_parameter_validation(pir_server):
    svc, url = pir_server(SMOKE_DIR)
    client = open_session(url)
    status, data = post_query(url, client)
    assert status == 201
    qid = json.loads(data)["query"]
    nb = svc.layout.num_blocks

    def blocks(qs):
        return http_request(url, "GET", f"/v1/pir/queries/{qid}/blocks?{qs}")[0]

    assert blocks("start=0&count=1") == 200
    assert blocks("start=abc&count=1") == 400     # non-integer
    assert blocks("start=0&count=xyz") == 400
    assert blocks("start=0&count=0") == 400       # count < 1
    assert blocks(f"start=0&count={svc.limits.max_blocks_per_request + 1}") == 400  # over the limit
    assert blocks("start=-1&count=1") == 416      # negative start
    assert blocks(f"start={nb}&count=1") == 416   # past the end
    assert blocks(f"start={nb - 1}&count=2") == 416
    client.close()


# =========================================================================== malformed bodies

def test_bad_setup_container_400(pir_server):
    _, url = pir_server(SMOKE_DIR)
    # selector-mode server: any non-empty setup must parse as a setup.galois container
    status, _, _ = http_request(url, "POST", "/v1/pir/sessions", b"\x00\x01\x02\x03 garbage")
    assert status == 400


def test_bad_query_container_400(pir_server):
    _, url = pir_server(SMOKE_DIR)
    client = open_session(url)
    status, _, _ = http_request(url, "POST", f"/v1/pir/sessions/{client.session}/queries",
                                b"not a wire container at all")
    assert status == 400
    client.close()


def test_wrong_kind_container_400(pir_server):
    _, url = pir_server(SMOKE_DIR)
    client = open_session(url)
    # a setup.galois container sent where a query.selector is expected
    body = wire.pack("setup.galois", [b"x" * 16])
    status, _, _ = http_request(url, "POST", f"/v1/pir/sessions/{client.session}/queries", body)
    assert status == 400
    # a query.expanded container to a selector-mode server
    body = wire.pack("query.expanded", [b"x" * 16])
    status, _, _ = http_request(url, "POST", f"/v1/pir/sessions/{client.session}/queries", body)
    assert status == 400
    client.close()


def test_too_many_queries_per_session_429(pir_server):
    _, url = pir_server(SMOKE_DIR)
    client = open_session(url)
    limit = client.params["limits"]["max_queries_per_session"]
    statuses = [post_query(url, client)[0] for _ in range(limit + 1)]
    assert statuses[:limit] == [201] * limit
    assert statuses[limit] == 429
    client.close()


def test_oversized_body_413(pir_server):
    svc, url = pir_server(SMOKE_DIR, limits=Limits(max_setup_bytes=4096, max_query_bytes=8192))
    port = int(url.rsplit(":", 1)[1])
    # claim a Content-Length far above the limit; the server rejects before reading the body
    raw = (b"POST /v1/pir/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
           b"Content-Length: 99999999\r\nConnection: close\r\n\r\n")
    assert b"413" in status_line(raw_request(port, raw))


def test_chunked_body_411(pir_server):
    _, url = pir_server(SMOKE_DIR)
    port = int(url.rsplit(":", 1)[1])
    raw = (b"POST /v1/pir/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
           b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\n")
    assert b"411" in status_line(raw_request(port, raw))


# =========================================================================== Host / DNS-rebinding

def test_non_loopback_host_header_421(pir_server):
    _, url = pir_server(SMOKE_DIR)
    status, _, _ = http_request(url, "GET", "/v1/catalog", host="evil.example.com")
    assert status == 421


def test_loopback_host_names_allowed(pir_server):
    _, url = pir_server(SMOKE_DIR)
    for host in ("127.0.0.1", "localhost"):
        status, _, _ = http_request(url, "GET", "/v1/catalog", host=host)
        assert status == 200


def test_non_loopback_bind_refused():
    from pmr.httpbase import LoopbackHTTPServer, JsonHandler, require_loopback

    assert require_loopback("127.0.0.1") == "127.0.0.1"
    assert require_loopback("localhost").startswith("127.")
    with pytest.raises(ValueError):
        require_loopback("8.8.8.8")
    with pytest.raises(ValueError):
        LoopbackHTTPServer("8.8.8.8", 0, JsonHandler)  # must not bind a public address


# =========================================================================== transparent ciphertext (bug)

@pytest.mark.parametrize("db_form", ["ntt", "plain", "raw"])
def test_transparent_query_ciphertext_is_rejected_not_500(pir_server, db_form):
    """A transparent (all-zero) selector ciphertext must yield a 4xx for every db_form.

    Before the fix, db_form in {plain, raw} deferred the SEAL failure to block-fetch time and
    returned HTTP 500 instead of rejecting the query at upload time.
    """
    svc, url = pir_server(SMOKE_DIR, config=BackendConfig(db_form=db_form))
    client = open_session(url)
    ctx = client.engine.crypto.bundle.context
    ct = sealapi.Ciphertext(ctx)
    ct.resize(ctx, client.engine.crypto.bundle.first_parms_id, 2)
    assert ct.is_transparent()
    blobs = client.engine.make_query(0)
    blobs[0] = save_bytes(ct)
    body = wire.pack("query.selector", blobs)
    # the failure may surface at query-prepare or (pre-fix) at block fetch; drive both.
    status, _, data = http_request(url, "POST", f"/v1/pir/sessions/{client.session}/queries", body)
    if status == 201:
        qid = json.loads(data)["query"]
        status, _, data = http_request(url, "GET", f"/v1/pir/queries/{qid}/blocks?start=0&count=1")
    assert status != 500, f"db_form={db_form}: server returned 500 for a transparent ciphertext: {data[:120]!r}"
    assert 400 <= status < 500, (db_form, status)
    client.close()


# =========================================================================== fuzzing

def test_fuzz_random_bodies_never_500(pir_server):
    import numpy as np

    svc, url = pir_server(SMOKE_DIR)
    client = open_session(url)
    rng = np.random.default_rng(1234)
    for _ in range(40):
        n = int(rng.integers(0, 4096))
        body = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
        s1, _, _ = http_request(url, "POST", "/v1/pir/sessions", body)
        s2, _, _ = http_request(url, "POST", f"/v1/pir/sessions/{client.session}/queries", body)
        assert s1 != 500 and s2 != 500, (n, s1, s2)
        assert s1 in (201, 400, 413) and s2 in (201, 400, 413, 429)
    client.close()


def test_fuzz_random_block_params_never_500(pir_server):
    import numpy as np

    _, url = pir_server(SMOKE_DIR)
    client = open_session(url)
    _, data = post_query(url, client)
    qid = json.loads(data)["query"]
    rng = np.random.default_rng(7)
    for _ in range(40):
        start = int(rng.integers(-5, 1000))
        count = int(rng.integers(-5, 1000))
        s, _, _ = http_request(url, "GET", f"/v1/pir/queries/{qid}/blocks?start={start}&count={count}")
        assert s != 500, (start, count, s)
    client.close()


# =========================================================================== process sharding (integration)

@pytest.mark.integration
def test_workers_two_matches_workers_one(launch, smoke_records):
    from pmr.client.pirclient import PirHttpClient

    out = {}
    for w in (1, 2):
        srv = launch("pmr.server",
                     ["--catalog", str(SMOKE_DIR), "--port", "0", "--workers", str(w)],
                     ready_name=f"srv_w{w}.json")
        client = PirHttpClient(srv.base_url)
        client.open_session()
        out[w] = client.retrieve(2, policy="pad").pcm
        client.close()
    assert out[1] == out[2]
    assert out[1] == smoke_records[2]
