"""Protocol hygiene: what the client sends to the PIR server never names the selection
(requirement 7).

These are FUNCTIONAL / protocol-inspection tests, NOT a cryptographic security proof. They
assert that the selected track's identifiers and the client's secret key never appear on the
wire, and that the request sequence under policy "pad" is identical for two different tracks
(so the sequence itself carries no length information), while policy "stop" leaks the length.

A short (3-byte) track id can appear by chance inside a random ciphertext body; that is not a
leak. Identifiers are therefore checked against the textual request head (request line +
headers), and distinctive long strings (filename, title, SHA-256) against the full capture.
"""
from __future__ import annotations

import re

import pytest

from conftest import SMOKE_DIR, split_http_messages
from pmr.client.pirclient import PirHttpClient
from pmr.he.seal_io import save_bytes
from pmr.pir.engine import BackendConfig

PATH_ALLOW = [
    re.compile(r"^/healthz$"),
    re.compile(r"^/v1/catalog$"),
    re.compile(r"^/v1/pir/params$"),
    re.compile(r"^/v1/pir/sessions$"),
    re.compile(r"^/v1/pir/sessions/[A-Za-z0-9_-]{16,64}$"),
    re.compile(r"^/v1/pir/sessions/[A-Za-z0-9_-]{16,64}/queries$"),
    re.compile(r"^/v1/pir/queries/[A-Za-z0-9_-]{16,64}/blocks$"),
    re.compile(r"^/v1/pir/queries/[A-Za-z0-9_-]{16,64}$"),
]
TOKEN = re.compile(r"/(sessions|queries)/[A-Za-z0-9_-]{16,64}")


def normalize(path: str) -> str:
    return TOKEN.sub(r"/\1/<tok>", path)


def request_lines(raw: bytes):
    out = []
    for line in raw.split(b"\r\n"):
        m = re.match(rb"^(GET|POST|DELETE|HEAD|PUT) (\S+) HTTP/1\.[01]$", line)
        if m:
            path, _, query = m.group(2).decode("latin1").partition("?")
            out.append((m.group(1).decode(), path, query))
    return out


def forbidden_strings(track: dict) -> list[bytes]:
    items = [track["id"], track["title"], track["artist"], track["file"],
             track["file"].split("/")[-1], track["sha256_pcm"]]
    return [s.encode("utf-8") for s in items if s]


@pytest.fixture
def served(pir_server, recording_proxy):
    """A PIR server plus a recording proxy in front of it; returns (service, proxy)."""
    def _make(config=None):
        svc, url = pir_server(SMOKE_DIR, config=config)
        proxy = recording_proxy(url)
        return svc, proxy
    return _make


def test_no_identifier_or_key_leaks_on_the_wire(served):
    svc, proxy = served()
    track_index = 2
    client = PirHttpClient(proxy.base_url, record_wire=True)
    client.open_session()
    result = client.retrieve(track_index, policy="pad")
    track = svc.manifest["tracks"][track_index]
    assert result.complete and len(result.pcm) == track["pcm_bytes"]  # retrieval actually ran
    captured = proxy.snapshot()
    client.close()

    msgs = split_http_messages(captured)
    heads = b"\n".join(h for h, _ in msgs)
    assert heads, "no requests were captured by the proxy"

    # 1. no catalog identifier appears in a request line or header (where a leak would live)
    for needle in forbidden_strings(track):
        assert needle not in heads, f"leaked identifier {needle!r} in request headers"
    for other in svc.manifest["tracks"]:
        assert other["id"].encode() not in heads

    # 2. distinctive long strings must not appear even inside request bodies (collision-free)
    for needle in (track["file"].encode(), track["file"].split("/")[-1].encode(),
                   track["title"].encode(), track["sha256_pcm"].encode()):
        assert needle not in captured, f"leaked {needle!r} on the wire"

    # 3. the secret key never appears (several >= 32-byte windows over the full capture)
    sk = client.engine.crypto._secret_key_bytes_for_tests()
    assert len(sk) >= 64
    windows = [sk[i:i + 32] for i in range(0, len(sk) - 32, max(1, (len(sk) - 32) // 16))]
    for w in windows:
        assert w not in captured, "secret-key bytes leaked on the wire"

    # 4. every request path is on the allow-list and the index is not a path/query element
    lines = request_lines(heads)
    assert lines, "no requests were captured by the proxy"
    for method, path, query in lines:
        assert any(p.match(path) for p in PATH_ALLOW), f"unexpected path {path!r}"
        for key in (query.split("&") if query else []):
            assert key.split("=", 1)[0] in ("start", "count"), f"unexpected query parameter {key!r}"
        assert str(track_index) not in path.split("/"), "index appears as a path segment"


def test_pad_policy_request_sequence_identical_across_tracks(served):
    svc, proxy = served()
    client = PirHttpClient(proxy.base_url, record_wire=True)
    client.open_session()

    def transcript(idx):
        start = len(client.transport.log)
        res = client.retrieve(idx, policy="pad")
        entries = client.transport.log[start:]
        seq = [(e.method, normalize(e.path.split("?")[0]),
                tuple(sorted(re.findall(r"(start|count)=(\d+)", e.path)))) for e in entries]
        query_body = next((len(e.request_body) for e in entries if e.method == "POST"), 0)
        block_bodies = [len(e.request_body) for e in entries if "blocks" in e.path]
        return seq, query_body, block_bodies, res.blocks_fetched

    seq1, qb1, bb1, n1 = transcript(0)  # a longer track
    seq2, qb2, bb2, n2 = transcript(2)  # the shortest track
    client.close()

    assert seq1 == seq2, "pad-policy request sequence differs between tracks"
    assert n1 == n2 == svc.layout.num_blocks  # both fetch every catalog block
    assert all(n == 0 for n in bb1) and all(n == 0 for n in bb2)  # block requests have empty bodies
    # the single query upload differs only by ciphertext-compression noise, independent of index
    assert abs(qb1 - qb2) <= 0.03 * qb1, (qb1, qb2)


def test_stop_policy_leaks_length(served):
    svc, proxy = served()
    client = PirHttpClient(proxy.base_url, record_wire=True)
    client.open_session()

    def block_count(idx):
        start = len(client.transport.log)
        res = client.retrieve(idx, policy="stop", first_batch=1, batch=4)
        reqs = len([e for e in client.transport.log[start:] if "blocks" in e.path])
        return res.blocks_fetched, reqs

    short_blocks, short_reqs = block_count(2)   # 65600 bytes -> 9 u16 blocks
    long_blocks, long_reqs = block_count(1)     # 112000 bytes -> 14 u16 blocks
    client.close()

    assert short_blocks == svc.layout.blocks_for_record(2)
    assert long_blocks == svc.layout.blocks_for_record(1)
    assert short_blocks != long_blocks, "stop policy should fetch a track-length-dependent count"
    assert short_reqs != long_reqs


def test_didactic_secret_context_not_leaked(pir_server, recording_proxy):
    """The didactic client uploads only the public context; its secret key stays local."""
    svc, url = pir_server(SMOKE_DIR, config=BackendConfig.didactic("u16"))
    proxy = recording_proxy(url)
    client = PirHttpClient(proxy.base_url, record_wire=True)
    client.open_session()
    client.retrieve(1, policy="pad")
    captured = proxy.snapshot()
    client.close()

    sk = save_bytes(client.engine.crypto._ctx.secret_key().data)
    assert len(sk) >= 64
    windows = [sk[i:i + 32] for i in range(0, len(sk) - 32, max(1, (len(sk) - 32) // 16))]
    for w in windows:
        assert w not in captured, "didactic secret-key bytes leaked on the wire"
    # and the public context *is* uploaded (sanity: the session setup body is non-trivial)
    assert any(e.method == "POST" and e.path.endswith("/sessions") and len(e.request_body) > 1000
               for e in client.transport.log)
