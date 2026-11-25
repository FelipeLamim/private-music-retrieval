"""Tests for catalog generation, manifest validation, verification and import (pmr.audio.dataset)."""
import copy
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from pmr.audio import dataset, synth
from pmr.audio.dataset import (
    ALLOWED_LICENSES,
    generate_catalog,
    import_track,
    load_manifest,
    main,
    manifest_json,
    profile_specs,
    verify_catalog,
)
from pmr.audio.wavio import pcm16_to_array, read_wav_format, read_wav_pcm16_mono, write_wav

ROOT = Path(__file__).resolve().parents[1]
BY = dict(license="CC-BY-4.0", source_url="https://example.org/track/1",
          license_url="https://creativecommons.org/licenses/by/4.0/",
          attribution='"Test Song" by Test Artist, CC BY 4.0', confirm_license_verified=True)


@pytest.fixture(scope="module")
def smoke_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("cat") / "smoke"
    generate_catalog(d, profile="smoke")
    return d


@pytest.fixture
def catalog(smoke_dir, tmp_path):
    d = tmp_path / "catalog"
    shutil.copytree(smoke_dir, d)
    return d


def wav_bytes(samples: np.ndarray, rate: int, bits: int = 16, tag: int = 1) -> bytes:
    """Interleaved samples (frames, channels) as a WAV file (int PCM or float)."""
    frames, channels = samples.shape
    if tag == 3:
        data = samples.astype("<f4").tobytes()
    elif bits == 24:
        v = samples.astype("<i4").reshape(-1)
        data = b"".join(struct.pack("<i", int(x))[:3] for x in v)
    else:
        data = samples.astype("<i2").tobytes()
    block = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def sine(seconds, rate, freq=440.0, amp=0.5):
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def snapshot(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


# ----------------------------------------------------------------------------- generation


def test_generate_smoke_catalog(smoke_dir):
    m = load_manifest(smoke_dir)
    assert m["schema_version"] == 1 and m["channels"] == 1 and m["sample_width"] == 2 and m["sample_rate"] == 16000
    assert (smoke_dir / "manifest.json").read_text(encoding="utf-8") == manifest_json(m)  # sorted keys, canonical
    tracks = m["tracks"]
    assert [t["index"] for t in tracks] == [0, 1, 2, 3] and [t["id"] for t in tracks] == ["t00", "t01", "t02", "t03"]
    durations = [t["duration_s"] for t in tracks]
    assert len(set(durations)) == 4 and all(2.0 <= d <= 4.0 for d in durations)
    for t in tracks:
        path = smoke_dir / t["file"]
        assert path.stat().st_size == 44 + t["pcm_bytes"]  # canonical header
        audio = read_wav_pcm16_mono(path, expected_rate=16000)
        assert hashlib.sha256(audio.pcm).hexdigest() == t["sha256_pcm"]
        assert t["pcm_bytes"] == 2 * t["num_samples"] and t["origin"] == "synthetic" and t["license"] == "CC0-1.0"
        assert synth.TrackSpec.from_dict(t["generator"]).duration_s == t["duration_s"]
    assert verify_catalog(smoke_dir) == []
    assert verify_catalog(smoke_dir, rerender=True) == []


def test_generation_is_reproducible(tmp_path, smoke_dir):
    m = generate_catalog(tmp_path / "again", profile="smoke")
    assert snapshot(tmp_path / "again") == snapshot(smoke_dir)
    assert m == load_manifest(tmp_path / "again")


def test_profiles_and_overrides():
    assert profile_specs("full") == synth.default_specs()
    mini = profile_specs("mini")
    assert len(mini) == 10 and len({s.duration_s for s in mini}) == 10
    assert all(12.0 <= s.duration_s <= 20.0 for s in mini)
    smoke = profile_specs("smoke")
    assert len(smoke) == 4 and len({s.duration_s for s in smoke}) == 4
    custom = profile_specs("smoke", n_tracks=7, duration_s=1.5, base_seed=5)
    assert [s.duration_s for s in custom] == [1.5] * 7
    for bad in (dict(profile="huge"), dict(n_tracks=0), dict(duration_s=0.0)):
        with pytest.raises(ValueError):
            profile_specs(**{"profile": "smoke", **bad})


def test_regenerate_removes_stale_tracks_and_refuses_imported(tmp_path):
    d = tmp_path / "c"
    generate_catalog(d, profile="smoke", n_tracks=6, duration_s=0.3)
    assert (d / "tracks" / "t05.wav").exists()
    generate_catalog(d, profile="smoke", n_tracks=3, duration_s=0.3)
    assert sorted(p.name for p in (d / "tracks").iterdir()) == ["t00.wav", "t01.wav", "t02.wav"]
    assert verify_catalog(d) == []
    src = tmp_path / "own.wav"
    src.write_bytes(wav_bytes((sine(0.5, 16000)[:, None] * 32767).astype(np.int16), 16000))
    import_track(d, src, title="Mine", artist="Me", license="CC0-1.0", source_url=None, license_url=None,
                 attribution=None, confirm_license_verified=True, origin="user_own")
    with pytest.raises(ValueError, match="imported tracks"):
        generate_catalog(d, profile="smoke")


# ----------------------------------------------------------------------------- manifest schema


def _break(m, path, value=KeyError):
    m = copy.deepcopy(m)
    target = m
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return m


@pytest.mark.parametrize(
    "path,value,message",
    [
        (("tracks", 0, "sha256_pcm"), KeyError, "missing keys"),
        (("catalog_id",), KeyError, "catalog_id"),
        (("schema_version",), 2, "schema_version"),
        (("channels",), 2, "mono"),
        (("tracks", 1, "index"), 0, "index"),
        (("tracks", 1, "id"), "t00", "duplicate id"),
        (("tracks", 0, "sha256_pcm"), "abc", "sha256_pcm"),
        (("tracks", 0, "file"), "../../etc/passwd.wav", "relative path"),
        (("tracks", 0, "file"), "/abs/t00.wav", "relative path"),
        (("tracks", 0, "pcm_bytes"), 12, "pcm_bytes"),
        (("tracks", 0, "duration_s"), 99.0, "duration_s"),
        (("tracks", 0, "license"), "CC-BY-NC-4.0", "license"),
        (("tracks", 0, "license_url"), "https://creativecommons.org/licenses/by-sa/4.0/", "does not match"),
        (("tracks", 0, "origin"), "downloaded", "origin"),
        (("tracks", 0, "generator"), None, "generator"),
        (("tracks", 0, "modifications"), "none", "modifications"),
        (("tracks", 0, "title"), "", "title"),
        (("tracks",), [], "must not be empty"),
    ],
)
def test_manifest_schema_validation(catalog, path, value, message):
    m = json.loads((catalog / "manifest.json").read_text(encoding="utf-8"))
    (catalog / "manifest.json").write_text(json.dumps(_break(m, path, value)), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_manifest(catalog)
    assert verify_catalog(catalog) and message in verify_catalog(catalog)[0]


def test_manifest_third_party_needs_source(catalog):
    m = json.loads((catalog / "manifest.json").read_text(encoding="utf-8"))
    t = m["tracks"][0]
    t.update(origin="third_party", generator=None, source_url=None)
    (catalog / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="source_url is required"):
        load_manifest(catalog)
    (catalog / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_manifest(catalog)
    with pytest.raises(ValueError, match="not found"):
        load_manifest(catalog / "nowhere")


# ----------------------------------------------------------------------------- verify


def test_verify_detects_corrupted_files(catalog):
    p = catalog / "tracks" / "t01.wav"
    raw = bytearray(p.read_bytes())
    raw[44 + 1001] ^= 0x40  # flip bit 14 of sample 500 (high byte, little-endian): changes it by 16384
    p.write_bytes(bytes(raw))
    problems = verify_catalog(catalog)
    assert len(problems) == 1 and problems[0].startswith("t01: sha256_pcm mismatch")
    assert "max abs sample difference vs fresh render = 16384" in problems[0]
    assert "(1 samples differ)" in problems[0] and "fresh render matches the manifest hash" in problems[0]
    q = catalog / "tracks" / "t02.wav"
    q.write_bytes(q.read_bytes()[:-100])  # truncated
    (catalog / "tracks" / "t03.wav").unlink()
    (catalog / "tracks" / "extra.wav").write_bytes(b"x")
    problems = verify_catalog(catalog)
    text = "\n".join(problems)
    assert "t02: unreadable WAV" in text and "truncated" in text
    assert "t03: missing file" in text and "unlisted file in catalog: tracks/extra.wav" in text
    assert main(["verify", str(catalog)]) == 1


# ----------------------------------------------------------------------------- import


def test_import_converts_and_records_modifications(catalog, tmp_path):
    left, right = sine(1.5, 44100, amp=0.6), sine(1.5, 44100, amp=0.2)
    stereo24 = np.rint(np.stack([left, right], axis=1) * 8388607).astype(np.int32)
    src = tmp_path / "song.wav"
    src.write_bytes(wav_bytes(stereo24, 44100, bits=24))
    before = load_manifest(catalog)
    entry = import_track(catalog, src, title="Test Song", artist="Test Artist", notes="unit test", **BY)
    assert entry["index"] == len(before["tracks"]) == 4 and entry["id"] == "t04"
    mods = entry["modifications"]
    assert any("24-bit signed integer PCM to 16-bit" in m for m in mods)
    assert any("downmixed 2 channels to mono" in m for m in mods)
    assert any("resampled from 44100 Hz to 16000 Hz" in m and "resample_poly" in m and "up=160, down=441" in m for m in mods)
    assert any("rounded" in m and "0 samples clipped" in m for m in mods)
    m = load_manifest(catalog)
    assert m["tracks"][-1] == entry and entry["origin"] == "third_party" and entry["generator"] is None
    fmt = read_wav_format(catalog / entry["file"])
    assert (fmt.sample_rate, fmt.channels, fmt.encoding) == (16000, 1, "s16")
    x = pcm16_to_array(read_wav_pcm16_mono(catalog / entry["file"]).pcm).astype(np.float64)
    assert x.size == entry["num_samples"] == int(np.ceil(left.size * 160 / 441))
    spectrum = np.abs(np.fft.rfft(x[2000:-2000]))
    assert abs(np.argmax(spectrum) * 16000 / (x.size - 4000) - 440.0) < 2.0  # pitch preserved
    assert abs(np.abs(x).max() / 32768 - 0.4) < 0.01  # mean of 0.6 and 0.2 amplitudes
    att = (catalog / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for needle in ("`t04`", "Test Song", "Test Artist", "https://example.org/track/1", "CC-BY-4.0",
                   "resampled from 44100 Hz", "unit test"):
        assert needle in att
    assert verify_catalog(catalog) == []
    (catalog / "ATTRIBUTION.md").unlink()
    assert any("ATTRIBUTION.md missing" in p for p in verify_catalog(catalog))


def test_import_float_and_already_compliant_audio(catalog, tmp_path):
    pcm = np.rint(sine(0.8, 16000, freq=300) * 20000).astype(np.int16)
    src = tmp_path / "own.wav"
    write_wav(src, pcm.tobytes(), 16000)
    entry = import_track(catalog, src, title="My Recording", artist="Me", license="cc0-1.0", source_url=None,
                         license_url=None, attribution=None, confirm_license_verified=True, origin="user_own")
    assert entry["modifications"] == [] and entry["license"] == "CC0-1.0" and entry["source_url"] is None
    assert entry["license_url"] == ALLOWED_LICENSES["CC0-1.0"]
    assert read_wav_pcm16_mono(catalog / entry["file"]).pcm == pcm.tobytes()  # copied unchanged
    with pytest.raises(ValueError, match="already in the catalog"):
        import_track(catalog, src, title="Again", artist="Me", license="CC0-1.0", source_url=None,
                     license_url=None, attribution=None, confirm_license_verified=True, origin="user_own")
    fsrc = tmp_path / "loud.wav"
    fsrc.write_bytes(wav_bytes(np.stack([sine(0.5, 16000) * 2.5], axis=1), 16000, bits=32, tag=3))
    e2 = import_track(catalog, fsrc, title="Loud", artist="Me", license="CC-BY-3.0", source_url="https://example.org/l",
                      license_url="https://creativecommons.org/licenses/by/3.0/br/", attribution="Me (CC BY 3.0)",
                      confirm_license_verified=True, origin="user_own")
    assert any("32-bit IEEE float" in m for m in e2["modifications"])
    clipped = int(next(m for m in e2["modifications"] if "clipped" in m).split(";")[1].split()[0])
    assert clipped > 1000
    assert [t["index"] for t in load_manifest(catalog)["tracks"]] == list(range(6))
    assert verify_catalog(catalog) == []


@pytest.mark.parametrize(
    "overrides,message",
    [
        (dict(confirm_license_verified=False), "refusing to import"),
        (dict(license="CC-BY-SA-4.0"), "ShareAlike"),
        (dict(license="CC-BY-NC-4.0"), "not allowed"),
        (dict(license="MIT"), "not allowed"),
        (dict(license=""), "not allowed"),
        (dict(attribution=""), "attribution is required"),
        (dict(source_url=None), "source_url is required"),
        (dict(source_url="ftp://example.org/x"), "http"),
        (dict(license_url="https://creativecommons.org/licenses/by-sa/4.0/"), "does not match"),
        (dict(origin="synthetic"), "origin"),
        (dict(title="  "), "title is required"),
    ],
)
def test_import_license_rules(catalog, tmp_path, overrides, message):
    src = tmp_path / "x.wav"
    write_wav(src, (np.ones(800, dtype=np.int16) * 100).tobytes(), 16000)
    before = snapshot(catalog)
    with pytest.raises(ValueError, match=message):
        import_track(catalog, src, **{**dict(title="T", artist="A", **BY), **overrides})
    assert snapshot(catalog) == before  # refused imports change nothing


def test_import_cc0_third_party_needs_source(catalog, tmp_path):
    src = tmp_path / "x.wav"
    write_wav(src, (np.arange(800, dtype=np.int16)).tobytes(), 16000)
    with pytest.raises(ValueError, match="source_url is required"):
        import_track(catalog, src, title="T", artist="A", license="CC0-1.0", source_url=None, license_url=None,
                     attribution=None, confirm_license_verified=True)


@pytest.mark.parametrize(
    "head,kind",
    [(b"fLaC\x00\x00\x00\x22", "FLAC"), (b"ID3\x04\x00\x00\x00\x00", "MP3"), (b"OggS\x00\x02", "Ogg"),
     (b"\x00\x00\x00\x20ftypM4A ", "MP4")],
)
def test_import_rejects_non_wav(catalog, tmp_path, head, kind):
    src = tmp_path / "input.bin"
    src.write_bytes(head + b"\x00" * 64)
    with pytest.raises(ValueError, match=f"not a WAV file \\(detected: {kind}.*convert it to WAV first.*ffmpeg"):
        import_track(catalog, src, title="T", artist="A", **BY)


def test_import_rejects_compressed_wav_with_hint(catalog, tmp_path):
    fmt = struct.pack("<HHIIHH", 7, 1, 8000, 8000, 1, 8)  # mu-law
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", 4) + b"\x00" * 4
    src = tmp_path / "ulaw.wav"
    src.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    with pytest.raises(ValueError, match="mu-law.*convert it to WAV first"):
        import_track(catalog, src, title="T", artist="A", **BY)


def test_import_into_new_catalog(tmp_path):
    src = tmp_path / "a.wav"
    write_wav(src, (np.arange(1600, dtype=np.int16) * 3).tobytes(), 16000)
    d = tmp_path / "mine"
    entry = import_track(d, src, title="First", artist="Me", license="CC0-1.0", source_url=None,
                         license_url=None, attribution=None, confirm_license_verified=True, origin="user_own")
    m = load_manifest(d)
    assert entry["index"] == 0 and m["catalog_id"] == "user-mine" and len(m["tracks"]) == 1
    assert verify_catalog(d) == []


# ----------------------------------------------------------------------------- CLI


def test_cli_generate_verify_info_import(tmp_path, capsys):
    d = tmp_path / "cli"
    assert main(["generate", "--profile", "smoke", "--out", str(d), "--n-tracks", "2", "--duration-s", "0.5", "--seed", "7"]) == 0
    out = capsys.readouterr().out
    assert "catalog synthetic-smoke-7-n2-d0.5: 2 tracks" in out and "wall time" in out
    assert main(["verify", str(d), "--rerender"]) == 0
    assert "OK" in capsys.readouterr().out
    assert main(["info", str(d)]) == 0
    assert "t01" in capsys.readouterr().out
    src = tmp_path / "s.wav"
    src.write_bytes(wav_bytes(np.rint(np.stack([sine(0.4, 22050)] * 2, axis=1) * 30000).astype(np.int16), 22050))
    common = ["import", str(d), "--src", str(src), "--title", "CLI Song", "--artist", "Someone", "--license", "CC-BY-4.0",
              "--source-url", "https://example.org/s", "--license-url", "https://creativecommons.org/licenses/by/4.0/",
              "--attribution", "CLI Song by Someone (CC BY 4.0)"]
    assert main(common) == 2
    assert "refusing to import" in capsys.readouterr().err
    assert main(common + ["--confirm-license-verified", "--notes", "via CLI"]) == 0
    out = capsys.readouterr().out
    assert "imported t02 (index 2)" in out and "downmixed 2 channels" in out and "resampled from 22050 Hz" in out
    assert main(["info", str(d), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tracks"][2]["title"] == "CLI Song"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    run = subprocess.run([sys.executable, "-m", "pmr.audio.dataset", "verify", str(d)], env=env,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert dataset.verify_catalog(d) == []
