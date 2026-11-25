"""Tests for the deterministic synthetic music generator (pmr.audio.synth)."""
import dataclasses
import hashlib
import json

import numpy as np
import pytest
from scipy.signal import fftconvolve

from pmr.audio import synth
from pmr.audio.synth import TrackSpec, default_specs, plan_sections, render

SR = 16000
# sha256 of render(default_specs()[0] with duration_s=3.0), generator version "1", measured on the
# development machine (x86-64, NumPy 1.26.4, SciPy 1.13.1). If this fails after a code change,
# the generator output changed: bump synth.GENERATOR_VERSION and regenerate the catalogs. If it
# fails on another platform without code changes, see README.md (Dataset and reproducibility).
GOLDEN_3S_T00 = "d6240217a7aa22e0c3b73034dab9b75293661525e8976a0e665b0f2cd6960d14"


def short(spec, seconds):
    return dataclasses.replace(spec, duration_s=float(seconds))


def onset_count(x, sr=SR):
    """Onsets = local maxima of the relative spectral flux above 0.05.

    Relative flux: half-wave rectified increase of the log-magnitude spectrum between
    16 ms hops, divided by the previous frame's total log-magnitude. A steady tone stays
    near 0.02 (window leakage only); note and drum onsets reach 0.1-1.7.
    """
    n, hop = 1024, 256
    frames = np.lib.stride_tricks.sliding_window_view(x.astype(np.float64), n)[::hop]
    logmag = np.log1p(np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)))
    flux = np.maximum(0.0, np.diff(logmag, axis=0)).sum(axis=1) / (logmag[:-1].sum(axis=1) + 1e-9)
    peaks = (flux[1:-1] > 0.05) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] >= flux[2:])
    return int(np.count_nonzero(peaks))


def dominant_pitch_classes(x, sr=SR):
    """Histogram of the strongest 80-2000 Hz partial per loud 64 ms frame, as pitch classes."""
    n, hop = 1024, 512
    frames = np.lib.stride_tricks.sliding_window_view(x.astype(np.float64), n)[::hop]
    mag = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    band = (freqs > 80) & (freqs < 2000)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    loud = rms > 0.1 * rms.max()
    dom = freqs[band][np.argmax(mag[loud][:, band], axis=1)]
    pcs = np.round(12 * np.log2(dom / 440.0) + 69).astype(int) % 12
    hist = np.bincount(pcs, minlength=12)
    return int(np.count_nonzero(hist > 0.05 * hist.sum()))


def max_ncc(a, b):
    a = a.astype(np.float64) - a.mean()
    b = b.astype(np.float64) - b.mean()
    c = fftconvolve(a, b[::-1], mode="full")
    return float(np.abs(c).max() / np.sqrt((a * a).sum() * (b * b).sum()))


# ----------------------------------------------------------------------------- specs


def test_default_specs_are_distinct():
    specs = default_specs()
    assert [s.id for s in specs] == [f"t{i:02d}" for i in range(10)]
    durations = [s.duration_s for s in specs]
    assert len(set(durations)) == 10 and all(165.0 <= d <= 195.0 for d in durations)
    tempos = [s.tempo_bpm for s in specs]
    assert len(set(tempos)) == 10 and all(70 <= t <= 150 for t in tempos)
    assert len({synth.PITCH_CLASSES[s.key] for s in specs}) == 10  # pairwise different keys
    assert len({s.meter for s in specs}) >= 4 and len({s.mode for s in specs}) >= 5
    assert len({s.seed for s in specs}) == 10 and len({s.title for s in specs}) == 10
    assert len({s.instrument("lead") for s in specs}) >= 6
    assert all(s.sample_rate == SR and s.generator_version == synth.GENERATOR_VERSION for s in specs)


def test_specs_scale_to_200_with_stable_prefix():
    many = default_specs(200)
    assert many[:10] == default_specs(10)
    assert len({s.id for s in many}) == len({s.title for s in many}) == len({s.seed for s in many}) == 200
    for b in range(20):
        block = many[10 * b: 10 * b + 10]
        assert len({s.tempo_bpm for s in block}) == 10
        assert len({s.key for s in block}) == 10
    assert default_specs(10, base_seed=1) != default_specs(10)
    fixed = default_specs(5, duration_s=7.5)
    assert [s.duration_s for s in fixed] == [7.5] * 5
    assert [dataclasses.replace(s, duration_s=0) for s in fixed] == [
        dataclasses.replace(s, duration_s=0) for s in default_specs(5)
    ]


def test_spec_dict_round_trip_and_validation():
    spec = default_specs()[4]
    d = json.loads(json.dumps(spec.to_dict()))
    assert TrackSpec.from_dict(d) == spec
    with pytest.raises(ValueError):
        TrackSpec.from_dict(dict(d, meter="9/7"))
    with pytest.raises(ValueError):
        TrackSpec.from_dict(dict(d, mode="locrian"))
    with pytest.raises(ValueError):
        TrackSpec.from_dict(dict(d, generator_version="0"))
    with pytest.raises(ValueError):
        TrackSpec.from_dict(dict(d, progression=["I", "XI"]))
    with pytest.raises(ValueError):
        TrackSpec.from_dict(dict(d, instruments=[["lead", "kazoo"]] + d["instruments"][1:]))
    with pytest.raises(ValueError):
        TrackSpec.from_dict(dict(d, unexpected=1))
    with pytest.raises(ValueError):
        render(dataclasses.replace(spec, duration_s=-1.0))


# ----------------------------------------------------------------------------- rendering


def test_render_is_deterministic_and_pinned():
    spec = short(default_specs()[0], 3.0)
    a, b = render(spec), render(TrackSpec.from_dict(spec.to_dict()))
    assert a.dtype == np.int16 and a.tobytes() == b.tobytes()
    assert hashlib.sha256(a.tobytes()).hexdigest() == GOLDEN_3S_T00


def test_different_specs_give_different_audio():
    spec = short(default_specs()[2], 4.0)
    base = render(spec).tobytes()
    assert render(dataclasses.replace(spec, seed=spec.seed + 1)).tobytes() != base
    assert render(dataclasses.replace(spec, tempo_bpm=spec.tempo_bpm + 7)).tobytes() != base
    assert render(dataclasses.replace(spec, key="E" if spec.key != "E" else "F")).tobytes() != base


@pytest.mark.parametrize("seconds", [0.05, 0.5, 1.0, 2.37, 13.3])
def test_requested_duration_peak_and_no_clipping(seconds):
    for spec in default_specs()[:3]:
        x = render(short(spec, seconds))
        assert x.size == int(round(seconds * SR))
        peak = int(np.abs(x.astype(np.int32)).max())
        assert peak == synth.TARGET_PEAK
        assert -3.1 < 20 * np.log10(peak / 32768.0) < -2.9
        assert peak < 32767  # no sample at full scale: nothing clipped


def test_tracks_are_distinguishable():
    specs = default_specs()
    clips = [render(short(s, 12.0)) for s in specs]
    worst = max(max_ncc(clips[i], clips[j]) for i in range(10) for j in range(i + 1, 10))
    assert worst < 0.3, worst


def test_music_is_not_a_constant_tone():
    t = np.arange(10 * SR) / SR
    tone = np.rint(8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    assert dominant_pitch_classes(tone) == 1 and onset_count(tone) < 5  # the metrics reject a tone
    for spec in default_specs()[::3]:
        x = render(short(spec, 20.0))
        assert onset_count(x) >= 25, spec.id
        assert dominant_pitch_classes(x) >= 4, spec.id


def test_full_length_track_has_form_and_does_not_loop():
    spec = default_specs()[3]
    x = render(spec)
    assert x.size == int(round(spec.duration_s * SR))
    sections = plan_sections(spec)
    labels = [s["label"] for s in sections]
    assert labels[0] == "intro" and labels[-1] == "outro" and labels.count("A") >= 2 and "B" in labels
    assert sum(s["bars"] for s in sections) * 60.0 * synth.METERS[spec.meter].steps_per_bar / (
        spec.tempo_bpm * synth.METERS[spec.meter].steps_per_beat) <= spec.duration_s
    # No 4-bar block of samples repeats anywhere in the track.
    bar = int(round(60.0 / spec.tempo_bpm * synth.METERS[spec.meter].steps_per_bar
                    / synth.METERS[spec.meter].steps_per_beat * SR))
    blocks = [x[i:i + 4 * bar].tobytes() for i in range(0, x.size - 4 * bar, 4 * bar)]
    assert len(blocks) > 20 and len(set(blocks)) == len(blocks)
    # The second statement of section A is a variation, not a copy of the first.
    a_secs = [s for s in sections if s["label"] == "A"]
    s0, s1 = (int(round(s["start_s"] * SR)) for s in a_secs[:2])
    assert max_ncc(x[s0:s0 + 4 * bar], x[s1:s1 + 4 * bar]) < 0.95
    assert not np.array_equal(x[s0:s0 + 4 * bar], x[s1:s1 + 4 * bar])


def test_every_parametric_spec_renders():
    for spec in default_specs(200):
        x = render(short(spec, 0.6))
        assert x.size == int(round(0.6 * SR)) and int(np.abs(x.astype(np.int32)).max()) == synth.TARGET_PEAK
