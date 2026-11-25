"""Deterministic synthetic music generator (mono, 16 kHz, signed 16-bit).

Every track is fully described by a :class:`TrackSpec`. :func:`render` turns a spec
into int16 samples using only NumPy/SciPy array code:

* harmony: key + mode (major, minor, dorian, ...), diatonic chord progressions for
  the A and B sections, voice-led pad/arpeggio/stab/broken-chord accompaniment;
* melody: seeded one-bar motifs developed into 4-bar phrases (repetition, sequence,
  rhythmic variant, cadence), varied on every repetition of a section
  (passing/neighbour tones, octave displacement), plus a counter-melody;
* bass line (roots/octaves/walking/syncopated) and a drum kit synthesized from noise
  and sine sweeps (kick, snare, hats, crash, toms, clap) with grooves per meter, fills
  and crashes at section boundaries;
* timbres: band-limited additive wavetables, 2-operator FM, PWM pulse, decaying
  partials (pluck/marimba), ADSR or percussive envelopes, vibrato and chorus;
* song form (intro / A / B / bridge / outro) scaled to the requested duration.

Determinism: all randomness comes from ``numpy.random.Generator(PCG64(...))`` seeded
from ``spec.seed``; no transcendental function is evaluated per sample. The sine
wavetable and the equal-temperament ratios are computed once with :mod:`decimal`
(pure software arithmetic, identical on every platform) and everything else uses
IEEE-754 basic operations, linear interpolation, cumulative sums, ``lfilter`` and an
FFT convolution for the reverb. The same spec gives identical bytes on the same
platform; across CPUs/library builds the FFT or library internals could in principle
differ in the last ulp (see README.md, Dataset and reproducibility).
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, localcontext
from functools import lru_cache
from typing import Iterable, Optional, Sequence

import numpy as np
from scipy.signal import lfilter, oaconvolve

GENERATOR_NAME = "pmr.audio.synth"
GENERATOR_VERSION = "1"
DEFAULT_BASE_SEED = 20261121
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_DURATION_RANGE = (165.0, 195.0)
TARGET_PEAK = 23197  # round(32767 * 10**(-3/20)): -3.0 dBFS sample peak
TABLE_SIZE = 4096
MAX_DURATION_S = 3600.0

# ---------------------------------------------------------------------------- music data

PITCH_CLASSES = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
    "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
}
KEY_NAMES = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")

MODES = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
}

# Diatonic progressions as 0-based scale degrees (rendered as roman numerals in specs).
PROGRESSIONS = {
    "major": ((0, 4, 5, 3), (0, 5, 3, 4), (0, 3, 1, 4), (5, 3, 0, 4), (0, 2, 3, 4), (3, 4, 2, 5), (0, 3, 0, 4)),
    "minor": ((0, 5, 2, 6), (0, 3, 6, 2), (0, 6, 5, 6), (0, 3, 4, 0), (0, 5, 3, 4), (5, 6, 0, 3)),
    "harmonic_minor": ((0, 3, 4, 0), (0, 5, 3, 4), (0, 6, 5, 4), (0, 3, 5, 4), (5, 3, 4, 0)),
    "dorian": ((0, 3, 0, 6), (0, 1, 3, 0), (0, 6, 3, 0), (0, 2, 3, 6), (3, 6, 0, 0)),
    "mixolydian": ((0, 6, 3, 0), (0, 4, 6, 3), (0, 3, 6, 0), (6, 3, 0, 4), (0, 6, 4, 3)),
    "lydian": ((0, 1, 0, 6), (0, 1, 4, 0), (0, 1, 5, 4), (4, 1, 0, 0), (0, 2, 1, 0)),
    "phrygian": ((0, 1, 0, 6), (0, 1, 2, 1), (0, 6, 5, 1), (0, 5, 6, 0), (1, 0, 6, 0)),
}
_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII")


@dataclass(frozen=True)
class Meter:
    name: str
    steps_per_bar: int  # sixteenth-note grid
    steps_per_beat: int  # 4 for simple meters (tempo = quarter notes), 6 for compound (dotted quarters)
    groups: tuple  # accent groups in steps
    compound: bool


METERS = {
    "4/4": Meter("4/4", 16, 4, (4, 4, 4, 4), False),
    "3/4": Meter("3/4", 12, 4, (4, 4, 4), False),
    "5/4": Meter("5/4", 20, 4, (4, 4, 4, 4, 4), False),
    "7/8": Meter("7/8", 14, 4, (4, 4, 6), False),
    "6/8": Meter("6/8", 12, 6, (6, 6), True),
    "12/8": Meter("12/8", 24, 6, (6, 6, 6, 6), True),
}
DRUM_STYLES = {
    "4/4": ("rock", "four", "halftime", "breakbeat"),
    "3/4": ("waltz",),
    "5/4": ("odd",),
    "7/8": ("odd",),
    "6/8": ("compound",),
    "12/8": ("compound",),
}

# Timbres. kinds: table (band-limited additive wavetable), pulse (PWM from two
# band-limited saws), fm (2-operator FM), partials (decaying sine partials).
TIMBRES = {
    "flute": dict(kind="table", spectrum="flute", env=("adsr", 0.04, 0.10, 0.80, 0.12), vibrato=0.004),
    "clarinet": dict(kind="table", spectrum="clarinet", env=("adsr", 0.03, 0.08, 0.85, 0.10), vibrato=0.003),
    "saw_lead": dict(kind="table", spectrum="saw_soft", env=("adsr", 0.01, 0.15, 0.70, 0.10), vibrato=0.005, chorus=6),
    "organ": dict(kind="table", spectrum="organ", env=("adsr", 0.01, 0.05, 0.95, 0.06)),
    "strings_pad": dict(kind="table", spectrum="strings", env=("adsr", 0.35, 0.40, 0.80, 0.60), vibrato=0.003, chorus=7),
    "pulse_lead": dict(kind="pulse", width=0.30, pwm=0.10, pwm_hz=0.9, env=("adsr", 0.008, 0.12, 0.65, 0.08), vibrato=0.004),
    "pulse_pad": dict(kind="pulse", width=0.45, pwm=0.15, pwm_hz=0.35, env=("adsr", 0.25, 0.30, 0.75, 0.50), chorus=8),
    "pulse_bass": dict(kind="pulse", width=0.50, pwm=0.0, pwm_hz=0.0, env=("adsr", 0.005, 0.12, 0.60, 0.05), max_harm=14),
    "saw_bass": dict(kind="table", spectrum="saw", env=("adsr", 0.005, 0.15, 0.55, 0.05), max_harm=10),
    "sine_bass": dict(kind="table", spectrum="sine_bass", env=("adsr", 0.006, 0.20, 0.70, 0.06)),
    "fm_epiano": dict(kind="fm", ratio=1.0, index=1.8, index_decay=0.5, env=("perc", 2.4, 3, 0.25)),
    "fm_bell": dict(kind="fm", ratio=3.5, index=2.2, index_decay=1.0, env=("perc", 3.0, 3, 0.6)),
    "fm_bass": dict(kind="fm", ratio=1.0, index=2.0, index_decay=0.2, env=("adsr", 0.004, 0.25, 0.50, 0.06)),
    "marimba": dict(kind="partials", partials=((1.0, 1.0, 0.45), (3.932, 0.35, 0.10), (9.0, 0.10, 0.04)), release=0.15),
    "pluck": dict(kind="partials", partials=((1, 1.0, 0.8), (2, 0.5, 0.45), (3, 0.33, 0.30), (4, 0.22, 0.22), (5, 0.14, 0.16), (6, 0.09, 0.12)), release=0.25),
    "pluck_bass": dict(kind="partials", partials=((1, 1.0, 0.9), (2, 0.45, 0.45), (3, 0.22, 0.25), (4, 0.11, 0.15)), release=0.08),
}
LEAD_TIMBRES = ("fm_epiano", "pulse_lead", "flute", "fm_bell", "saw_lead", "clarinet", "marimba", "pluck")
COUNTER_TIMBRES = ("flute", "clarinet", "organ", "strings_pad", "fm_bell")
CHORD_TIMBRES = ("strings_pad", "organ", "pulse_pad", "fm_epiano", "pluck", "marimba")
BASS_TIMBRES = ("sine_bass", "pulse_bass", "fm_bass", "pluck_bass", "saw_bass")
KITS = ("acoustic", "electro", "tight", "soft")
CHORD_STYLES = ("pad", "arp", "stabs", "broken")
BASS_STYLES = ("roots", "octaves", "walking", "syncopated")
ROLES = ("lead", "counter", "chords", "bass", "drums")
SECTION_LABELS = ("intro", "A", "B", "bridge", "outro")
FORMS = (
    ("intro", "A", "B", "A", "B", "bridge", "A", "outro"),
    ("intro", "A", "A", "B", "A", "bridge", "B", "outro"),
    ("intro", "A", "B", "bridge", "A", "B", "B", "outro"),
    ("intro", "A", "B", "A", "bridge", "B", "A", "outro"),
)
_ADJECTIVES = (
    "Amber", "Silver", "Hidden", "Quiet", "Copper", "Velvet", "Crimson", "Distant", "Golden", "Hollow",
    "Lunar", "Misty", "Northern", "Paper", "Restless", "Scarlet", "Slow", "Sunlit", "Tidal", "Winter",
    "Wandering", "Electric", "Glass", "Cobalt",
)
_NOUNS = (
    "Harbor", "Lanterns", "Orchard", "Signals", "Meadow", "Circuits", "Rivers", "Station", "Gardens", "Echoes",
    "Bridges", "Comets", "Valley", "Windows", "Tides", "Pathways", "Mirrors", "Engines", "Horizon", "Islands",
    "Clocks", "Summit", "Parade", "Fields",
)

# ---------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class TrackSpec:
    """Every parameter needed to regenerate a synthetic track with :func:`render`."""

    id: str
    title: str
    seed: int
    duration_s: float
    sample_rate: int
    tempo_bpm: float  # beats per minute; the beat is a quarter note (simple meters) or a dotted quarter (6/8, 12/8)
    meter: str
    key: str
    mode: str
    progression: tuple  # roman numerals, A section (intro/outro too)
    progression_b: tuple  # roman numerals, B section (bridge uses it rotated by two chords)
    bars_per_chord: int
    chord_sevenths: bool
    instruments: tuple  # ((role, timbre), ...) for lead, counter, chords, bass, drums (kit)
    chord_style: str
    bass_style: str
    drum_style: str
    swing: float  # delay of off-beat eighths, as a fraction of an eighth note
    melody_density: float  # 0..1, probability scale of melody onsets
    lead_register: int  # MIDI note around which the melody moves
    reverb_mix: float
    reverb_s: float
    form: tuple  # section labels; bar counts are derived from duration and tempo (see plan_sections)
    generator_version: str = GENERATOR_VERSION

    def to_dict(self) -> dict:
        """JSON-compatible dict (tuples become lists)."""
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> "TrackSpec":
        if not isinstance(data, dict):
            raise ValueError("TrackSpec.from_dict needs a dict")
        names = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ValueError(f"unknown TrackSpec fields: {unknown}")
        missing = sorted(n for n in names if n not in data and n != "generator_version")
        if missing:
            raise ValueError(f"missing TrackSpec fields: {missing}")
        kw = dict(data)
        for name in ("progression", "progression_b", "form"):
            kw[name] = tuple(kw[name])
        kw["instruments"] = tuple(tuple(pair) for pair in kw["instruments"])
        spec = cls(**kw)
        validate_spec(spec)
        return spec

    def instrument(self, role: str) -> str:
        return dict(self.instruments)[role]


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


_ROMAN_RE = re.compile(r"^(I|II|III|IV|V|VI|VII)(°|\+)?$", re.IGNORECASE)


def _parse_roman(numeral: str) -> int:
    """0-based scale degree of a roman numeral such as 'IV', 'vi' or 'vii°'."""
    m = _ROMAN_RE.match(numeral.strip()) if isinstance(numeral, str) else None
    if m is None:
        raise ValueError(f"invalid roman numeral {numeral!r}")
    return _ROMAN.index(m.group(1).upper())


def roman_numeral(mode: str, degree: int) -> str:
    """Roman numeral of the diatonic triad on ``degree`` (case/° from its quality)."""
    scale = MODES[mode]
    third = (scale[(degree + 2) % 7] - scale[degree]) % 12
    fifth = (scale[(degree + 4) % 7] - scale[degree]) % 12
    name = _ROMAN[degree]
    if third == 4 and fifth == 7:
        return name
    if third == 3 and fifth == 7:
        return name.lower()
    if third == 3 and fifth == 6:
        return name.lower() + "°"
    return name + "+"


def validate_spec(spec: TrackSpec) -> None:
    """Raise ValueError if the spec cannot be rendered by this generator version."""
    def bad(msg):
        raise ValueError(f"invalid TrackSpec {getattr(spec, 'id', '?')!r}: {msg}")

    if spec.generator_version != GENERATOR_VERSION:
        bad(f"generator_version {spec.generator_version!r} != supported {GENERATOR_VERSION!r}")
    if not isinstance(spec.seed, int) or isinstance(spec.seed, bool) or not 0 <= spec.seed < 2**63:
        bad("seed must be an integer in [0, 2**63)")
    if not isinstance(spec.sample_rate, int) or not 8000 <= spec.sample_rate <= 96000:
        bad("sample_rate must be an integer in [8000, 96000]")
    if not isinstance(spec.duration_s, (int, float)) or not 0.05 <= float(spec.duration_s) <= MAX_DURATION_S:
        bad(f"duration_s must be in [0.05, {MAX_DURATION_S}]")
    if not isinstance(spec.tempo_bpm, (int, float)) or not 30 <= float(spec.tempo_bpm) <= 300:
        bad("tempo_bpm must be in [30, 300]")
    if spec.meter not in METERS:
        bad(f"meter must be one of {sorted(METERS)}")
    if spec.key not in PITCH_CLASSES:
        bad(f"unknown key {spec.key!r}")
    if spec.mode not in MODES:
        bad(f"mode must be one of {sorted(MODES)}")
    for name in ("progression", "progression_b"):
        prog = getattr(spec, name)
        if not isinstance(prog, tuple) or not 1 <= len(prog) <= 16:
            bad(f"{name} must be a tuple of 1..16 roman numerals")
        for numeral in prog:
            _parse_roman(numeral)
    if spec.bars_per_chord not in (1, 2):
        bad("bars_per_chord must be 1 or 2")
    try:
        inst = dict(spec.instruments)
    except (TypeError, ValueError):
        bad("instruments must be ((role, timbre), ...) pairs")
    if sorted(inst) != sorted(ROLES) or len(spec.instruments) != len(ROLES):
        bad(f"instruments must define exactly the roles {ROLES}")
    for role, timbre in inst.items():
        if role == "drums":
            if timbre not in KITS:
                bad(f"unknown drum kit {timbre!r}")
        elif timbre not in TIMBRES:
            bad(f"unknown timbre {timbre!r} for {role}")
    if spec.chord_style not in CHORD_STYLES:
        bad(f"chord_style must be one of {CHORD_STYLES}")
    if spec.bass_style not in BASS_STYLES:
        bad(f"bass_style must be one of {BASS_STYLES}")
    if spec.drum_style not in DRUM_STYLES[spec.meter]:
        bad(f"drum_style for {spec.meter} must be one of {DRUM_STYLES[spec.meter]}")
    if not 0.0 <= float(spec.swing) <= 0.4:
        bad("swing must be in [0, 0.4]")
    if not 0.05 <= float(spec.melody_density) <= 1.0:
        bad("melody_density must be in [0.05, 1]")
    if not isinstance(spec.lead_register, int) or not 55 <= spec.lead_register <= 90:
        bad("lead_register must be a MIDI note in [55, 90]")
    if not 0.0 <= float(spec.reverb_mix) <= 0.8 or not 0.2 <= float(spec.reverb_s) <= 4.0:
        bad("reverb_mix must be in [0, 0.8] and reverb_s in [0.2, 4]")
    if not isinstance(spec.form, tuple) or not spec.form or any(l not in SECTION_LABELS for l in spec.form):
        bad(f"form must be a non-empty tuple of labels from {SECTION_LABELS}")


# ---------------------------------------------------------------------------- exact tables


def _dec_pi() -> Decimal:
    # Recipe from the Python decimal documentation (series for pi).
    with localcontext() as ctx:
        ctx.prec += 2
        three = Decimal(3)
        lasts, t, s, n, na, d, da = 0, three, 3, 1, 0, 0, 24
        while s != lasts:
            lasts = s
            n, na = n + na, na + 8
            d, da = d + da, da + 32
            t = (t * n) / d
            s += t
    return +s


def _dec_sin(x: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec += 2
        i, lasts, s, fact, num, sign = 1, 0, x, 1, x, 1
        while s != lasts:
            lasts = s
            i += 2
            fact *= i * (i - 1)
            num *= x * x
            sign *= -1
            s += num / fact * sign
    return +s


@lru_cache(maxsize=1)
def _sine_table() -> tuple:
    """(table, diff) for one sine period of TABLE_SIZE points, platform independent."""
    n = TABLE_SIZE
    q = n // 4
    with localcontext() as ctx:
        ctx.prec = 34
        pi = _dec_pi()
        quarter = [float(_dec_sin(2 * pi * k / n)) for k in range(q + 1)]
    quarter[q] = 1.0
    tab = np.empty(n, dtype=np.float64)
    for j in range(n):
        quad, r = divmod(j, q)
        v = quarter[r] if quad in (0, 2) else quarter[q - r]
        tab[j] = v if quad < 2 else -v
    tab.setflags(write=False)
    diff = np.roll(tab, -1) - tab
    diff.setflags(write=False)
    return tab, diff


@lru_cache(maxsize=None)
def _pow2_ratio(num: int, den: int) -> float:
    """2 ** (num/den) computed in decimal arithmetic (identical on every platform)."""
    with localcontext() as ctx:
        ctx.prec = 34
        return float(Decimal(2) ** (Decimal(num) / Decimal(den)))


def midi_to_hz(midi: float) -> float:
    m = int(midi)
    octave, step = divmod(m - 69, 12)
    return math.ldexp(440.0 * _pow2_ratio(step, 12), octave)


def _lookup(tab: np.ndarray, diff: np.ndarray, phase: np.ndarray) -> np.ndarray:
    """Linear-interpolated periodic table lookup; ``phase`` in cycles (any real)."""
    x = phase - np.floor(phase)
    x *= TABLE_SIZE
    i = x.astype(np.intp)
    np.minimum(i, TABLE_SIZE - 1, out=i)
    x -= i
    return tab[i] + x * diff[i]


def _sin(phase: np.ndarray) -> np.ndarray:
    tab, diff = _sine_table()
    return _lookup(tab, diff, phase)


def _spectrum(name: str, k: int) -> float:
    """Amplitude of harmonic k (1-based) for a named additive spectrum (rational values only)."""
    if name == "saw":
        return 1.0 / k
    if name == "saw_soft":
        return 1.0 / (k * (1.0 + k / 6.0))
    if name == "strings":
        return 1.0 / (k * (1.0 + k / 10.0))
    if name == "clarinet":
        return 1.0 / k if k % 2 else 0.08 / k
    if name == "flute":
        return {1: 1.0, 2: 0.30, 3: 0.12, 4: 0.05, 5: 0.02}.get(k, 0.0)
    if name == "organ":
        return {1: 1.0, 2: 0.75, 3: 0.5, 4: 0.4, 6: 0.25, 8: 0.18, 10: 0.1}.get(k, 0.0)
    if name == "sine_bass":
        return {1: 1.0, 2: 0.25, 3: 0.08}.get(k, 0.0)
    raise ValueError(f"unknown spectrum {name!r}")


_SPECTRUM_LIMIT = {"flute": 5, "organ": 10, "sine_bass": 3}


@lru_cache(maxsize=512)
def _harmonic_table(spectrum: str, n_harm: int, normalize: bool = True) -> tuple:
    """Band-limited single-cycle wavetable with harmonics 1..n_harm (exact index arithmetic)."""
    sin_tab, _ = _sine_table()
    j = np.arange(TABLE_SIZE, dtype=np.int64)
    tab = np.zeros(TABLE_SIZE, dtype=np.float64)
    for k in range(1, n_harm + 1):
        a = _spectrum(spectrum, k)
        if a:
            tab += a * sin_tab[(k * j) % TABLE_SIZE]
    if normalize:
        tab /= np.max(np.abs(tab))
    tab.setflags(write=False)
    diff = np.roll(tab, -1) - tab
    diff.setflags(write=False)
    return tab, diff


def _n_harmonics(freq: float, sr: int, limit: int) -> int:
    return max(1, min(limit, int(0.45 * sr / freq)))


# ---------------------------------------------------------------------------- envelopes & filters


def _ri(x: float) -> int:
    return int(math.floor(x + 0.5))


def _adsr_env(n_gate: int, n_total: int, sr: int, a: float, d: float, s: float) -> np.ndarray:
    na = max(1, _ri(a * sr))
    nd = max(1, _ri(d * sr))
    t = np.arange(n_total, dtype=np.float64)
    env = np.interp(t, [0.0, float(na), float(na + nd)], [0.0, 1.0, s])
    g = min(n_gate, n_total)
    if g < n_total:
        level = env[g]
        nrel = n_total - g
        env[g:] = level * (1.0 - np.arange(nrel, dtype=np.float64) / nrel)
    env *= env  # convex curve, closer to an exponential than straight lines
    return env


def _poly_decay(n: int, n_decay: int, power: int) -> np.ndarray:
    """(1 - t/T)^power clipped at 0: an exponential-like decay using multiplications only."""
    u = 1.0 - np.arange(n, dtype=np.float64) / max(1, n_decay)
    np.maximum(u, 0.0, out=u)
    out = u.copy()
    for _ in range(power - 1):
        out *= u
    return out


def _attack_ramp(env: np.ndarray, n_att: int) -> np.ndarray:
    n_att = min(n_att, env.size)
    if n_att > 1:
        env[:n_att] *= np.arange(n_att, dtype=np.float64) / n_att
    return env


def _onepole_lp(x: np.ndarray, a: float) -> np.ndarray:
    """y[n] = y[n-1] + a (x[n] - y[n-1]) with rational coefficient a (no trig)."""
    return lfilter([a], [1.0, a - 1.0], x)


def _onepole_hp(x: np.ndarray, a: float) -> np.ndarray:
    return x - _onepole_lp(x, a)


# ---------------------------------------------------------------------------- note synthesis


def _vibrato_phase(freq: float, n: int, sr: int, depth: float, rate_hz: float = 5.5, delay_s: float = 0.25) -> np.ndarray:
    inc = freq / sr
    if depth <= 0 or n < int(delay_s * sr) + 16:
        return np.arange(n, dtype=np.float64) * inc
    t = np.arange(n, dtype=np.float64)
    nd = int(delay_s * sr)
    fade = np.clip((t - nd) / (0.3 * sr), 0.0, 1.0)
    lfo = _sin(t * (rate_hz / sr))
    incs = inc * (1.0 + depth * fade * lfo)
    phase = np.cumsum(incs)
    phase -= incs[0]
    return phase


def _note_env(cfg: dict, n_gate: int, sr: int) -> np.ndarray:
    env_cfg = cfg["env"]
    if env_cfg[0] == "adsr":
        _, a, d, s, r = env_cfg
        n_total = n_gate + max(1, _ri(r * sr))
        return _adsr_env(n_gate, n_total, sr, a, d, s)
    _, decay_s, power, release_s = env_cfg  # percussive
    n_decay = _ri(decay_s * sr)
    n_total = min(n_gate + _ri(release_s * sr), n_decay)
    env = _poly_decay(n_total, n_decay, power)
    if n_gate < n_total:  # damp after note-off
        nrel = n_total - n_gate
        env[n_gate:] *= 1.0 - np.arange(nrel, dtype=np.float64) / nrel
    return _attack_ramp(env, _ri(0.003 * sr))


def render_note(timbre: str, midi: int, n_gate: int, sr: int) -> np.ndarray:
    """Waveform (float64, peak about 1) of one note; length = gate + release."""
    cfg = TIMBRES[timbre]
    freq = midi_to_hz(midi)
    kind = cfg["kind"]
    if kind == "partials":
        n_total = n_gate + _ri(cfg["release"] * sr)
        t = np.arange(n_total, dtype=np.float64)
        out = np.zeros(n_total)
        for ratio, amp, decay in cfg["partials"]:
            f = freq * ratio
            if f >= 0.45 * sr:
                continue
            n_dec = _ri(3.0 * decay * sr)
            part = _sin(t * (f / sr)) * _poly_decay(n_total, n_dec, 3)
            out += amp * part
        rel = n_total - n_gate
        if rel > 0:
            out[n_gate:] *= 1.0 - np.arange(rel, dtype=np.float64) / rel
        return _attack_ramp(out, _ri(0.002 * sr))
    env = _note_env(cfg, n_gate, sr)
    n_total = env.size
    if kind == "table":
        limit = cfg.get("max_harm", _SPECTRUM_LIMIT.get(cfg["spectrum"], 48))
        tab, diff = _harmonic_table(cfg["spectrum"], _n_harmonics(freq, sr, limit))
        cents = cfg.get("chorus", 0)
        if cents:
            r_up = _pow2_ratio(cents, 1200)
            wave = 0.5 * (
                _lookup(tab, diff, _vibrato_phase(freq * r_up, n_total, sr, cfg.get("vibrato", 0.0)))
                + _lookup(tab, diff, _vibrato_phase(freq / r_up, n_total, sr, cfg.get("vibrato", 0.0)) + 0.25)
            )
        else:
            wave = _lookup(tab, diff, _vibrato_phase(freq, n_total, sr, cfg.get("vibrato", 0.0)))
    elif kind == "pulse":
        limit = cfg.get("max_harm", 40)
        tab, diff = _harmonic_table("saw", _n_harmonics(freq, sr, limit), False)
        phase = _vibrato_phase(freq, n_total, sr, cfg.get("vibrato", 0.0))
        width = cfg["width"]
        if cfg["pwm"]:
            width = width + cfg["pwm"] * _sin(np.arange(n_total, dtype=np.float64) * (cfg["pwm_hz"] / sr))
        wave = 0.55 * (_lookup(tab, diff, phase) - _lookup(tab, diff, phase + width))
        cents = cfg.get("chorus", 0)
        if cents:
            phase2 = np.arange(n_total, dtype=np.float64) * (freq * _pow2_ratio(cents, 1200) / sr)
            wave = 0.5 * (wave + 0.55 * (_lookup(tab, diff, phase2) - _lookup(tab, diff, phase2 + width)))
    elif kind == "fm":
        ratio = cfg["ratio"]
        max_index = (0.45 * sr / freq - 1.0) / ratio - 1.0
        index = max(0.0, min(cfg["index"], max_index))
        t = np.arange(n_total, dtype=np.float64)
        ienv = 0.15 + 0.85 * _poly_decay(n_total, _ri(3.0 * cfg["index_decay"] * sr), 3)
        mod = _sin(t * (freq * ratio / sr))
        wave = _sin(t * (freq / sr) + (index * 0.15915494309189535) * ienv * mod)
    else:  # pragma: no cover
        raise ValueError(kind)
    return wave * env


class _NoteCache:
    """Memoizes rendered notes inside one render() call (bounded memory)."""

    def __init__(self, sr: int, max_samples: int = 6_000_000):
        self.sr = sr
        self.max_samples = max_samples
        self.size = 0
        self.store = {}

    def get(self, timbre: str, midi: int, n_gate: int) -> np.ndarray:
        key = (timbre, midi, n_gate)
        wave = self.store.get(key)
        if wave is None:
            wave = render_note(timbre, midi, n_gate, self.sr)
            if self.size + wave.size > self.max_samples:
                self.store.clear()
                self.size = 0
            self.store[key] = wave
            self.size += wave.size
        return wave


# ---------------------------------------------------------------------------- drums


def _noise(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.random(n) * 2.0 - 1.0


def _sweep(sr, n, f0, f1, n_sweep):
    u = 1.0 - np.arange(n, dtype=np.float64) / n_sweep
    np.maximum(u, 0.0, out=u)
    f = f1 + (f0 - f1) * (u * u * u)
    phase = np.cumsum(f) / sr
    return _sin(phase - phase[0])


def make_kit(kit: str, sr: int, rng: np.random.Generator) -> dict:
    """Synthesize drum one-shots (three variants each) for a kit style."""
    p = {
        "acoustic": dict(k0=120, k1=52, kdec=0.38, sn=200, sdec=0.20, hdec=0.050, crash=1.6, click=0.35),
        "electro": dict(k0=160, k1=45, kdec=0.60, sn=230, sdec=0.16, hdec=0.035, crash=1.2, click=0.15),
        "tight": dict(k0=140, k1=60, kdec=0.24, sn=240, sdec=0.12, hdec=0.030, crash=1.0, click=0.45),
        "soft": dict(k0=100, k1=50, kdec=0.30, sn=180, sdec=0.22, hdec=0.060, crash=1.8, click=0.10),
    }[kit]
    j = lambda lo, hi: lo + (hi - lo) * rng.random()  # noqa: E731
    k0, k1 = p["k0"] * j(0.9, 1.1), p["k1"] * j(0.9, 1.1)
    sn = p["sn"] * j(0.92, 1.08)
    out = {name: [] for name in ("kick", "snare", "hat", "open_hat", "crash", "tom_hi", "tom_lo", "clap")}
    for _ in range(3):
        n = _ri(p["kdec"] * sr)
        body = _sweep(sr, n, k0, k1, _ri(0.06 * sr)) * _poly_decay(n, n, 2)
        nc = _ri(0.004 * sr)
        click = np.zeros(n)
        click[:nc] = _onepole_lp(_noise(rng, nc), 0.5) * np.linspace(1.0, 0.0, nc)
        out["kick"].append(_attack_ramp(body + p["click"] * click, 8))

        n = _ri(p["sdec"] * sr * 1.3)
        tone = (_sin(np.arange(n) * (sn / sr)) + 0.5 * _sin(np.arange(n) * (sn * 1.5 / sr)))
        tone *= _poly_decay(n, _ri(0.10 * sr), 3)
        nz = _onepole_lp(_onepole_hp(_noise(rng, n), 0.25), 0.7) * _poly_decay(n, _ri(p["sdec"] * sr * 1.3), 3)
        out["snare"].append(_attack_ramp(0.45 * tone + 0.9 * nz, 4))

        for name, dec in (("hat", p["hdec"]), ("open_hat", p["hdec"] * 6.0)):
            n = _ri(dec * sr * 1.5)
            hz = _onepole_hp(_onepole_hp(_noise(rng, n), 0.45), 0.45)
            out[name].append(_attack_ramp(hz * _poly_decay(n, n, 4 if name == "hat" else 3), 3))

        n = _ri(p["crash"] * sr)
        cz = _onepole_hp(_noise(rng, n), 0.35) * _poly_decay(n, n, 3)
        out["crash"].append(_attack_ramp(cz, 6))

        for name, ft in (("tom_hi", 220.0), ("tom_lo", 140.0)):
            n = _ri(0.28 * sr)
            body = _sweep(sr, n, ft * 1.6, ft * j(0.95, 1.05), _ri(0.05 * sr)) * _poly_decay(n, n, 3)
            out[name].append(_attack_ramp(body, 6))

        n = _ri(0.18 * sr)
        cl = np.zeros(n)
        for off in (0, _ri(0.009 * sr), _ri(0.018 * sr)):
            m = n - off
            cl[off:] += _onepole_hp(_noise(rng, m), 0.3) * _poly_decay(m, _ri(0.012 * sr) if off < _ri(0.018 * sr) else m, 3)
        out["clap"].append(_attack_ramp(0.7 * cl, 3))
    return out


DRUM_GAINS = {"kick": 0.50, "snare": 0.30, "hat": 0.09, "open_hat": 0.07, "crash": 0.08,
              "tom_hi": 0.25, "tom_lo": 0.27, "clap": 0.22}


def _groove(meter: Meter, style: str, tempo: float, rng: np.random.Generator) -> dict:
    """Per-track base drum pattern: kind -> list of (step, velocity)."""
    S = meter.steps_per_bar
    hat_step = 1 if (not meter.compound and tempo < 96 and rng.random() < 0.6) else 2
    hats = [(s, 0.9 if s % 4 == 0 else 0.6) for s in range(0, S, hat_step)]
    extra_kicks = [s for s in (6, 10, 14, 3, 11) if s < S and rng.random() < 0.3][:2]
    g = {"hat": hats}
    if style == "rock":
        g["kick"] = [(0, 1.0), (8, 0.85)] + [(s, 0.6) for s in extra_kicks]
        g["snare"] = [(4, 1.0), (12, 1.0)]
        if rng.random() < 0.5:
            g["open_hat"] = [(14, 0.7)]
            g["hat"] = [h for h in hats if h[0] != 14]
    elif style == "four":
        g["kick"] = [(0, 1.0), (4, 0.9), (8, 0.95), (12, 0.9)]
        g["clap" if rng.random() < 0.5 else "snare"] = [(4, 1.0), (12, 1.0)]
        g["open_hat"] = [(2, 0.7), (6, 0.7), (10, 0.7), (14, 0.7)]
        g["hat"] = [(s, 0.45) for s in range(1, S, 2)]
    elif style == "halftime":
        g["kick"] = [(0, 1.0)] + [(s, 0.6) for s in extra_kicks if s != 8]
        g["snare"] = [(8, 1.0)]
    elif style == "breakbeat":
        g["kick"] = [(0, 1.0), (10, 0.8)] + ([(7, 0.55)] if rng.random() < 0.5 else [])
        g["snare"] = [(4, 1.0), (12, 1.0), (15, 0.3)] + ([(7, 0.25)] if rng.random() < 0.5 else [])
    elif style == "waltz":
        g["kick"] = [(0, 1.0)] + ([(10, 0.5)] if rng.random() < 0.5 else [])
        g["snare"] = [(4, 0.55), (8, 0.55)]
    elif style == "odd":
        if meter.name == "5/4":
            g["kick"] = [(0, 1.0), (12, 0.8)]
            g["snare"] = [(8, 1.0), (16, 0.9)]
        else:  # 7/8 as 2+2+3 eighths
            g["kick"] = [(0, 1.0), (4, 0.6)]
            g["snare"] = [(8, 1.0)]
    elif style == "compound":
        g["kick"] = [(s, 1.0 if s == 0 else 0.8) for s in range(0, S, 12)]
        g["snare"] = [(s, 1.0) for s in range(6, S, 12)]
        if rng.random() < 0.5:
            g["kick"].append((S - 2, 0.55))
        g["hat"] = [(s, 0.9 if s % 6 == 0 else 0.55) for s in range(0, S, 2)]
    else:  # pragma: no cover
        raise ValueError(style)
    return g


def _drum_bar(meter: Meter, groove: dict, mode: str, fill: bool, crash: bool, rng) -> list:
    S = meter.steps_per_bar
    hits = []
    if mode == "off":
        pass
    elif mode == "final":
        hits += [(0, "kick", 1.0), (0, "crash", 1.0)]
    elif mode == "hats":
        hits += [(s, "hat", v * 0.8) for s, v in groove["hat"]]
        hits.append((0, "kick", 0.5))
    elif mode == "halftime":
        hits += [(0, "kick", 1.0), (S // 2, "snare", 0.9)]
        hits += [(s, "hat", 0.5) for s in range(0, S, 4 if not meter.compound else 6)]
    else:
        for kind, pattern in groove.items():
            for s, v in pattern:
                hits.append((s, kind, v))
        if mode == "groove_b":
            hits = [h for h in hits if h[1] != "hat"]
            hits += [(s, "open_hat" if s % 4 == 2 else "hat", 0.55) for s in range(0, S, 2)]
            if rng.random() < 0.5:
                hits.append((S - 4 if S > 8 else S - 2, "kick", 0.6))
        # ghost notes, a little different in every bar
        for s in range(1, S, 2):
            if rng.random() < 0.06:
                hits.append((s, "snare", 0.18))
    if fill and mode not in ("off", "final"):
        start = S - (meter.groups[-1] if meter.groups[-1] >= 4 else 4)
        if rng.random() < 0.5:
            start = S // 2
        hits = [h for h in hits if h[0] < start or h[1] in ("kick",)]
        kinds = ("snare", "tom_hi", "tom_lo")
        step = 1 if rng.random() < 0.5 else 2
        for i, s in enumerate(range(start, S, step)):
            frac = (s - start) / max(1, S - start)
            kind = kinds[min(2, int(frac * 3))] if rng.random() < 0.7 else "snare"
            hits.append((s, kind, 0.55 + 0.4 * frac))
    if crash and mode not in ("off",):
        hits.append((0, "crash", 0.9))
    return hits


# ---------------------------------------------------------------------------- song planning


def _rng(seed: int, *path: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, *path])))


_ST_PLAN, _ST_MELODY, _ST_BASS, _ST_CHORDS, _ST_DRUMS, _ST_KIT, _ST_HUMAN, _ST_REVERB, _ST_COUNTER = range(1, 10)


def _timing(spec: TrackSpec):
    meter = METERS[spec.meter]
    step_s = 60.0 / (float(spec.tempo_bpm) * meter.steps_per_beat)
    bar_s = step_s * meter.steps_per_bar
    duration = float(spec.duration_s)
    tail = min(3.0, max(0.2, 0.08 * duration))
    n_bars = max(1, int((duration - tail) // bar_s))
    return meter, step_s, bar_s, n_bars


def _allocate(form: Sequence[str], n_bars: int) -> list:
    """Split n_bars into (label, bars) sections following the form template."""
    if n_bars < 8:
        return [("A", n_bars)]
    intro = 2 if n_bars < 24 else (4 if n_bars < 72 else 8)
    outro = 2 if n_bars < 24 else 4
    main = [l for l in form if l not in ("intro", "outro")] or ["A"]
    avail = n_bars - intro - outro
    while len(main) > 1 and avail < 4 * len(main):
        main.pop()
    units, rem = divmod(avail, 4)
    weights = [1 if l == "bridge" else 2 for l in main]
    alloc = [1] * len(main)
    extra = units - len(main)
    if extra > 0:
        total_w = sum(weights)
        shares = [extra * w / total_w for w in weights]
        base = [int(s) for s in shares]
        alloc = [a + b for a, b in zip(alloc, base)]
        left = extra - sum(base)
        order = sorted(range(len(main)), key=lambda i: (-(shares[i] - base[i]), i))
        for i in order[:left]:
            alloc[i] += 1
    elif units < len(main):  # only reachable for very short pieces
        alloc = [1] * units + [0] * (len(main) - units)
    sections = [("intro", intro)] + [(l, 4 * a) for l, a in zip(main, alloc) if a] + [("outro", outro + rem)]
    return sections


def plan_sections(spec: TrackSpec) -> list:
    """Sections actually rendered for a spec: list of dicts (label, occurrence, start_bar, bars, start_s)."""
    validate_spec(spec)
    _, _, bar_s, n_bars = _timing(spec)
    out, start, seen = [], 0, {}
    for label, bars in _allocate(spec.form, n_bars):
        occ = seen.get(label, 0)
        seen[label] = occ + 1
        out.append({"label": label, "occurrence": occ, "start_bar": start, "bars": bars, "start_s": start * bar_s})
        start += bars
    return out


@dataclass
class _Theme:
    rhythm: list  # [(onset_step, dur_steps)]
    contour: list  # degree offsets
    alt_rhythm: list
    alt_contour: list
    plan: tuple
    seq_shift: int


def _metric_weight(meter: Meter, s: int) -> float:
    starts = np.cumsum((0,) + meter.groups[:-1])
    if s == 0:
        return 1.0
    if s in starts:
        return 0.7
    if s % 2 == 0:
        return 0.4
    return 0.12


def _bar_rhythm(rng, meter: Meter, density: float) -> list:
    S = meter.steps_per_bar
    onsets = []
    for s in range(S):
        p = min(0.95, max(0.02, density * _metric_weight(meter, s) * 1.5))
        if (s == 0 and rng.random() < 0.85) or (s > 0 and rng.random() < p):
            onsets.append(s)
    if len(onsets) < 2:
        starts = [int(x) for x in np.cumsum((0,) + meter.groups[:-1])]
        onsets = sorted(set(onsets) | set(starts[:3]))
    out = []
    for i, s in enumerate(onsets):
        nxt = onsets[i + 1] if i + 1 < len(onsets) else S
        out.append((s, nxt - s))
    return out


def _contour(rng, n: int) -> list:
    d, out, last_leap = 0, [0], 0
    for _ in range(n - 1):
        r = rng.random()
        if last_leap:
            step = -int(np.sign(last_leap))
            last_leap = 0
        elif r < 0.55:
            step = 1 if rng.random() < 0.5 else -1
        elif r < 0.78:
            step = 2 if rng.random() < 0.5 else -2
        elif r < 0.9:
            step = 0
        else:
            step = int(rng.integers(3, 6)) * (1 if rng.random() < 0.5 else -1)
            last_leap = step
        if abs(d + step) > 5:
            step = -step
        d += step
        out.append(d)
    return out


def _make_theme(rng, meter: Meter, density: float) -> _Theme:
    rhythm = _bar_rhythm(rng, meter, density)
    alt = _bar_rhythm(rng, meter, density * 0.9)
    plans = (("M", "S", "M", "C"), ("M", "V", "M", "C"), ("M", "S", "V", "C"), ("M", "M", "S", "C"))
    return _Theme(
        rhythm=rhythm,
        contour=_contour(rng, len(rhythm)),
        alt_rhythm=alt,
        alt_contour=_contour(rng, len(alt)),
        plan=plans[int(rng.integers(len(plans)))],
        seq_shift=(1, -1, 2, -2)[int(rng.integers(4))],
    )


def _snap_to_chord(d: int, root: int) -> int:
    """Nearest scale degree that belongs to the triad on ``root`` (ties: downwards)."""
    for off in (0, -1, 1, -2, 2, -3, 3):
        if (d + off - root) % 7 in (0, 2, 4):
            return d + off
    return d  # pragma: no cover - every degree is within 3 steps of a chord tone


def _deg_to_midi(scale, base_midi: int, d: int) -> int:
    octave, idx = divmod(d, 7)
    return base_midi + 12 * octave + scale[idx]


def _voicing(scale, tonic_pc: int, root: int, sevenths: bool, prev: Optional[list], lo=52, hi=76) -> list:
    degs = [root, root + 2, root + 4] + ([root + 6] if sevenths else [])
    pcs = [(tonic_pc + scale[d % 7]) % 12 for d in degs]
    candidates = []
    for inv in range(len(pcs)):
        order = pcs[inv:] + pcs[:inv]
        for base in range(lo, lo + 12):
            if base % 12 != order[0]:
                continue
            notes = [base]
            for pc in order[1:]:
                n = notes[-1] + 1
                while n % 12 != pc:
                    n += 1
                notes.append(n)
            if notes[-1] <= hi:
                candidates.append(notes)
    if not candidates:  # pragma: no cover
        return [lo + (pc - lo) % 12 for pc in pcs]
    if prev is None or len(prev) != len(candidates[0]):
        return min(candidates, key=lambda v: (abs(sum(v) / len(v) - 64), v))
    return min(candidates, key=lambda v: (sum(abs(a - b) for a, b in zip(v, prev)), v))


# ---------------------------------------------------------------------------- composition


class _Composer:
    def __init__(self, spec: TrackSpec):
        self.spec = spec
        self.sr = spec.sample_rate
        self.meter, self.step_s, self.bar_s, self.n_bars = _timing(spec)
        self.S = self.meter.steps_per_bar
        self.scale = MODES[spec.mode]
        self.tonic_pc = PITCH_CLASSES[spec.key]
        self.sections = plan_sections(spec)
        base = spec.lead_register - 6
        self.lead_base = base + (self.tonic_pc - base) % 12  # tonic at or above register-6
        self.counter_base = self.lead_base - 12
        b = 36
        self.bass_base = b + (self.tonic_pc - b) % 12  # 36..47
        self.prog_a = [_parse_roman(x) for x in spec.progression]
        self.prog_b = [_parse_roman(x) for x in spec.progression_b]
        self.prog_bridge = self.prog_b[2:] + self.prog_b[:2] if len(self.prog_b) > 2 else self.prog_b[::-1]
        self.events = {role: [] for role in ("lead", "counter", "chords", "bass")}
        self.drum_hits = []
        self.human = _rng(spec.seed, _ST_HUMAN)
        self.bars = self._bar_table()

    # -- timeline -------------------------------------------------------
    def _bar_table(self) -> list:
        bars = []
        for sec in self.sections:
            label, n = sec["label"], sec["bars"]
            prog = {"B": self.prog_b, "bridge": self.prog_bridge}.get(label, self.prog_a)
            for j in range(n):
                chord = prog[(j // self.spec.bars_per_chord) % len(prog)]
                bars.append(dict(label=label, occ=sec["occurrence"], j=j, n=n, chord=chord))
        if bars:
            bars[-1]["chord"] = 0  # always end on the tonic
            if len(bars) > 1 and bars[-1]["label"] == "outro":
                bars[-2]["chord"] = 4 if self.spec.mode in ("major", "harmonic_minor", "lydian") else bars[-2]["chord"]
        return bars

    def time_of(self, bar: int, step: float) -> float:
        t = (bar * self.S + step) * self.step_s
        if self.spec.swing and not self.meter.compound:
            s = step % 4
            if s == 2:
                t += self.spec.swing * 2 * self.step_s
            elif s in (1, 3):
                t += self.spec.swing * self.step_s
        return t

    def add_note(self, role, bar, step, dur_steps, midi, vel, jitter=0.004):
        t = self.time_of(bar, step) + jitter * (2.0 * self.human.random() - 1.0)
        v = vel * (0.94 + 0.12 * self.human.random())
        self.events[role].append((max(0.0, t), dur_steps * self.step_s, int(midi), v))

    # -- melody ----------------------------------------------------------
    def compose_melody(self):
        spec = self.spec
        rng_themes = _rng(spec.seed, _ST_MELODY, 0)
        themes = {"A": _make_theme(rng_themes, self.meter, spec.melody_density),
                  "B": _make_theme(rng_themes, self.meter, min(1.0, spec.melody_density * 1.15))}
        prev = 3
        for bi, bar in enumerate(self.bars):
            label = bar["label"]
            if label in ("intro", "bridge"):
                continue
            if label == "outro" and bar["j"] >= 4 and bi != len(self.bars) - 1:
                continue
            theme = themes["B" if label == "B" else "A"]
            rng = _rng(spec.seed, _ST_MELODY, 1 + bi)
            phrase_pos = bar["j"] % 4
            last_bar = bi == len(self.bars) - 1
            op = theme.plan[phrase_pos]
            if bar["j"] == bar["n"] - 1 or last_bar:
                op = "C"
            root = bar["chord"]
            if op == "C":
                notes = self._cadence(prev, root, last_bar or bar["j"] == bar["n"] - 1, rng)
            else:
                rhythm, contour = (theme.alt_rhythm, theme.alt_contour) if op == "V" else (theme.rhythm, theme.contour)
                anchor = _snap_to_chord(prev if abs(prev - 3) < 4 else 3, root)
                shift = theme.seq_shift if op == "S" else 0
                notes = []
                for (on, dur), c in zip(rhythm, contour):
                    d = anchor + shift + c
                    if _metric_weight(self.meter, on) >= 0.7:
                        d = _snap_to_chord(d, root)
                    notes.append([on, dur, d])
                # ornament repeated sections, and every second phrase inside a section
                notes = self._vary(notes, bar["occ"] + (bar["j"] // 4) % 2, rng)
            if bar["occ"] >= 2 and label in ("A", "B") and bar["j"] >= bar["n"] - 4:
                for n in notes:
                    n[2] += 7 if n[2] < 4 else 0
            for on, dur, d in notes:
                d = max(-4, min(11, d))
                midi = _deg_to_midi(self.scale, self.lead_base, d)
                vel = 0.9 if _metric_weight(self.meter, on) >= 0.7 else 0.72
                gate = dur * (0.85 if not last_bar else 1.0)
                if last_bar and on == notes[-1][0]:
                    gate = (self.spec.duration_s - self.time_of(bi, on)) / self.step_s - 1.5 / self.step_s
                self.add_note("lead", bi, on, max(0.5, gate), midi, vel)
                prev = d

    def _cadence(self, prev, root, final, rng):
        S = self.S
        target = _snap_to_chord(prev, root)
        if final:
            # resolve on the chord root closest to the previous note
            cands = [root + 7 * k for k in range(-1, 3)]
            target = min(cands, key=lambda x: (abs(x - prev), x))
        approach = target + (1 if rng.random() < 0.5 else -1)
        half = S // 2 if not self.meter.compound else 6
        if rng.random() < 0.5:
            return [[0, half, approach], [half, S - half, target]]
        return [[0, max(2, half // 2), approach], [max(2, half // 2), S - max(2, half // 2), target]]

    def _vary(self, notes, occ, rng):
        if occ == 0:
            return notes
        out = []
        for i, (on, dur, d) in enumerate(notes):
            nxt = notes[i + 1] if i + 1 < len(notes) else None
            if nxt is not None and abs(nxt[2] - d) == 2 and dur >= 2 and rng.random() < 0.45:
                h = dur // 2
                out += [[on, h, d], [on + h, dur - h, (d + nxt[2]) // 2]]  # passing tone
            elif dur >= 4 and rng.random() < 0.3:
                h = dur // 2
                out += [[on, h, d], [on + h, dur - h, d + (1 if rng.random() < 0.5 else -1)]]  # neighbour
            else:
                out.append([on, dur, d])
        return out

    # -- counter melody -------------------------------------------------
    def compose_counter(self):
        rng = _rng(self.spec.seed, _ST_COUNTER)
        prev = 2
        for bi, bar in enumerate(self.bars):
            active = bar["label"] == "bridge" or (bar["label"] in ("A", "B") and bar["occ"] >= 1)
            if not active:
                continue
            per_bar = 2 if (bar["label"] == "bridge" or rng.random() < 0.5) else 1
            span = self.S // per_bar
            for k in range(per_bar):
                cands = [d for d in range(-3, 9) if (d - bar["chord"]) % 7 in (0, 2, 4)]
                cands.sort(key=lambda d: (abs(d - prev) if d != prev else 9, d))
                d = cands[0] if rng.random() < 0.7 else cands[min(1, len(cands) - 1)]
                midi = _deg_to_midi(self.scale, self.counter_base, d)
                self.add_note("counter", bi, k * span, span * 0.95, midi, 0.7)
                prev = d

    # -- chords -----------------------------------------------------------
    def compose_chords(self):
        rng = _rng(self.spec.seed, _ST_CHORDS)
        style = self.spec.chord_style
        prev_voicing = None
        arp_patterns = ((0, 1, 2, 3), (0, 1, 2, 1), (0, 2, 1, 3), (3, 2, 1, 0), (0, 1, 2, 3, 2, 1))
        arp = arp_patterns[int(rng.integers(len(arp_patterns)))]
        sub = 1 if (self.spec.tempo_bpm < 95 and not self.meter.compound) else 2
        for bi, bar in enumerate(self.bars):
            v = _voicing(self.scale, self.tonic_pc, bar["chord"], self.spec.chord_sevenths, prev_voicing)
            prev_voicing = v
            last_bar = bi == len(self.bars) - 1
            this_style = style
            if bar["label"] in ("intro", "bridge", "outro") or last_bar:
                this_style = "pad" if style != "arp" or bar["label"] == "bridge" or last_bar else "arp"
            if this_style == "pad" or last_bar:
                if not last_bar and bar["j"] % self.spec.bars_per_chord:
                    continue
                span = self.S * (self.spec.bars_per_chord if not last_bar else 1)
                if last_bar:
                    span = (self.spec.duration_s - self.time_of(bi, 0)) / self.step_s - 1.0 / self.step_s
                for m in v:
                    self.add_note("chords", bi, 0, span, m, 0.55, jitter=0.0)
            elif this_style == "arp":
                notes = v + [v[0] + 12]
                for i, s in enumerate(range(0, self.S, sub)):
                    m = notes[arp[i % len(arp)] % len(notes)]
                    self.add_note("chords", bi, s, sub * 1.6, m, 0.6 if s % 4 else 0.75)
            elif this_style == "stabs":
                positions = [s for s in range(self.S) if (s % 6 in (2, 4) if self.meter.compound else s % 4 == 2)]
                if rng.random() < 0.5:
                    positions = [0] + positions
                for s in positions:
                    for m in v:
                        self.add_note("chords", bi, s, 1.2, m, 0.6, jitter=0.002)
            elif this_style == "broken":
                order = (0, 2, 1, 2)
                for i, s in enumerate(range(0, self.S, 2)):
                    m = v[order[i % 4] % len(v)]
                    self.add_note("chords", bi, s, 2.5, m, 0.6)

    # -- bass -------------------------------------------------------------
    def compose_bass(self):
        rng = _rng(self.spec.seed, _ST_BASS)
        style = self.spec.bass_style
        S = self.S
        groups = [int(x) for x in np.cumsum((0,) + self.meter.groups[:-1])]
        sync = [s for s in range(S) if rng.random() < (0.9 if s == 0 else 0.3 if s % 2 == 0 else 0.18)]
        if 0 not in sync:
            sync = [0] + sync
        for bi, bar in enumerate(self.bars):
            if bar["label"] == "intro" and bar["j"] < bar["n"] // 2:
                continue
            root = bar["chord"]
            nxt = self.bars[bi + 1]["chord"] if bi + 1 < len(self.bars) else 0
            rm = _deg_to_midi(self.scale, self.bass_base, root)
            if rm > 47:
                rm -= 12
            fifth = rm + ((self.scale[(root + 4) % 7] - self.scale[root]) % 12)
            third = rm + ((self.scale[(root + 2) % 7] - self.scale[root]) % 12)
            last_bar = bi == len(self.bars) - 1
            if last_bar:
                span = (self.spec.duration_s - self.time_of(bi, 0)) / self.step_s - 1.0 / self.step_s
                self.add_note("bass", bi, 0, span, rm, 0.9, jitter=0.0)
                continue
            st = style if bar["label"] not in ("bridge", "intro") else "roots"
            if st == "roots":
                self.add_note("bass", bi, 0, groups[1] * 1.8 if len(groups) > 1 else S, rm, 0.95)
                if len(groups) > 2:
                    mid = groups[len(groups) // 2]
                    self.add_note("bass", bi, mid, (S - mid) * 0.9, fifth if rng.random() < 0.4 else rm, 0.8)
            elif st == "octaves":
                for s in range(0, S, 2):
                    self.add_note("bass", bi, s, 1.6, rm + (12 if (s // 2) % 2 else 0), 0.85 if s % 4 == 0 else 0.7)
            elif st == "walking":
                tones = [rm, third, fifth]
                nr = _deg_to_midi(self.scale, self.bass_base, nxt)
                if nr > 47:
                    nr -= 12
                for i, s in enumerate(groups):
                    if i == len(groups) - 1 and len(groups) > 2:
                        m = nr + (1 if rng.random() < 0.5 else -1)
                    else:
                        m = tones[i % 3] if i else rm
                    dur = (groups[i + 1] if i + 1 < len(groups) else S) - s
                    self.add_note("bass", bi, s, dur * 0.85, m, 0.85)
            else:  # syncopated
                for i, s in enumerate(sync):
                    dur = (sync[i + 1] if i + 1 < len(sync) else S) - s
                    m = rm if (i % 3 != 2) else (fifth if rng.random() < 0.6 else rm + 12)
                    self.add_note("bass", bi, s, max(1, dur) * 0.8, m, 0.85 if s == 0 else 0.7)

    # -- drums ------------------------------------------------------------
    def compose_drums(self):
        rng = _rng(self.spec.seed, _ST_DRUMS)
        groove = _groove(self.meter, self.spec.drum_style, float(self.spec.tempo_bpm), rng)
        intro_mode = "hats" if rng.random() < 0.6 else "off"
        bridge_mode = "halftime" if rng.random() < 0.7 else "off"
        for bi, bar in enumerate(self.bars):
            label, j, n = bar["label"], bar["j"], bar["n"]
            last_bar = bi == len(self.bars) - 1
            if last_bar:
                mode = "final"
            elif label == "intro":
                mode = "off" if (intro_mode == "off" or j < n // 2) else "hats"
            elif label == "bridge":
                mode = bridge_mode
            elif label == "outro":
                mode = "hats" if j >= n - 2 else "groove"
            elif label == "B":
                mode = "groove_b"
            else:
                mode = "groove"
            next_label = self.bars[bi + 1]["label"] if bi + 1 < len(self.bars) else None
            fill = (j == n - 1 and next_label is not None and next_label != label) or (j % 8 == 7 and j != n - 1)
            crash = j == 0 and label in ("A", "B") and mode != "off"
            for step, kind, vel in _drum_bar(self.meter, groove, mode, fill, crash, rng):
                t = self.time_of(bi, step) + 0.003 * (2.0 * self.human.random() - 1.0)
                self.drum_hits.append((max(0.0, t), kind, vel * (0.92 + 0.16 * self.human.random()), int(rng.integers(3))))


# ---------------------------------------------------------------------------- rendering

STEM_GAINS = {"lead": 0.40, "counter": 0.17, "chords": 0.14, "bass": 0.32}
REVERB_SENDS = {"lead": 0.35, "counter": 0.45, "chords": 0.6, "bass": 0.0, "drums": 0.12}


@lru_cache(maxsize=None)
def timbre_level(timbre: str, sr: int) -> float:
    """Gain that gives every timbre the same RMS over 0.2 s of a reference note
    (taken right after the attack phase, so slow pads are not over-amplified)."""
    env = TIMBRES[timbre].get("env", ("perc",))
    start = _ri(env[1] * sr) if env[0] == "adsr" else 0
    ref = render_note(timbre, 64, start + _ri(0.4 * sr), sr)[start: start + _ri(0.2 * sr)]
    return 0.3 / float(np.sqrt(np.mean(ref * ref)))


def _mix_notes(buf: np.ndarray, events: Iterable, timbre: str, cache: _NoteCache, gain: float, sr: int) -> None:
    n_buf = buf.size
    gain = gain * timbre_level(timbre, sr)
    for t, dur_s, midi, vel in events:
        start = _ri(t * sr)
        if start >= n_buf:
            continue
        wave = cache.get(timbre, midi, max(16, _ri(dur_s * sr)))
        end = min(n_buf, start + wave.size)
        buf[start:end] += (gain * vel) * wave[: end - start]


def _compose(spec: TrackSpec) -> _Composer:
    comp = _Composer(spec)
    comp.compose_melody()
    comp.compose_counter()
    comp.compose_chords()
    comp.compose_bass()
    comp.compose_drums()
    return comp


def _render_stem(spec: TrackSpec, comp: _Composer, role: str, n: int, cache: Optional[_NoteCache] = None) -> np.ndarray:
    sr = spec.sample_rate
    stem = np.zeros(n)
    if role == "drums":
        kit = make_kit(spec.instrument("drums"), sr, _rng(spec.seed, _ST_KIT))
        for t, kind, vel, variant in comp.drum_hits:
            start = _ri(t * sr)
            if start >= n:
                continue
            wave = kit[kind][variant]
            end = min(n, start + wave.size)
            stem[start:end] += (DRUM_GAINS[kind] * vel) * wave[: end - start]
    else:
        _mix_notes(stem, comp.events[role], spec.instrument(role), cache or _NoteCache(sr), STEM_GAINS[role], sr)
    return stem


def render_stems(spec: TrackSpec) -> dict:
    """Unmastered float64 stems (lead, counter, chords, bass, drums) for analysis."""
    validate_spec(spec)
    n = _ri(float(spec.duration_s) * spec.sample_rate)
    comp = _compose(spec)
    return {role: _render_stem(spec, comp, role, n) for role in ROLES}


def render(spec: TrackSpec) -> np.ndarray:
    """Render a track: int16 mono samples, length round(duration_s * sample_rate)."""
    validate_spec(spec)
    sr = spec.sample_rate
    n_out = _ri(float(spec.duration_s) * sr)
    comp = _compose(spec)

    margin = 4 * sr
    cache = _NoteCache(sr)
    dry = np.zeros(n_out + margin)
    send = np.zeros(n_out + margin)
    for role in ROLES:
        stem = _render_stem(spec, comp, role, n_out + margin, cache)
        dry += stem
        if REVERB_SENDS[role]:
            send += REVERB_SENDS[role] * stem
        del stem
    cache.store.clear()

    # Reverb: decaying filtered noise impulse response (FFT overlap-add convolution).
    rv = _rng(spec.seed, _ST_REVERB)
    n_ir = max(16, _ri(float(spec.reverb_s) * sr))
    pre = _ri(0.018 * sr)
    ir = np.zeros(pre + n_ir)
    ir[pre:] = _onepole_lp(_noise(rv, n_ir), 0.45) * _poly_decay(n_ir, n_ir, 4)
    ir /= np.sqrt(np.sum(ir * ir))
    wet = oaconvolve(send, ir)[: dry.size]
    mix = dry + float(spec.reverb_mix) * wet
    del dry, send, wet

    mix = mix[:n_out]
    mix = lfilter([1.0, -1.0], [1.0, -0.995], mix)  # DC blocker (~13 Hz at 16 kHz)
    n_in = min(n_out, _ri(0.01 * sr))
    if n_in > 1:
        mix[:n_in] *= np.arange(n_in, dtype=np.float64) / n_in
    n_fade = min(n_out, max(_ri(0.05 * sr), min(_ri(1.5 * sr), _ri(0.1 * n_out))))
    if n_fade > 1:
        ramp = 1.0 - np.arange(1, n_fade + 1, dtype=np.float64) / n_fade
        mix[n_out - n_fade:] *= ramp * ramp
    peak = float(np.max(np.abs(mix))) if n_out else 0.0
    if peak <= 0.0:
        return np.zeros(n_out, dtype=np.int16)
    out = np.rint(mix * (TARGET_PEAK / peak))
    return out.astype(np.int16)


# ---------------------------------------------------------------------------- default catalog specs


def _stratified_values(rng, n: int, lo: float, hi: float, step: float) -> list:
    """n distinct grid values in [lo, hi], one per equal-width stratum, shuffled."""
    grid = [lo + i * step for i in range(int(round((hi - lo) / step)) + 1)]
    if n > len(grid):
        raise ValueError(f"cannot pick {n} distinct values on a {step} grid in [{lo}, {hi}]")
    bounds = [round(i * len(grid) / n) for i in range(n + 1)]
    vals = [round(grid[int(rng.integers(bounds[i], bounds[i + 1]))], 6) for i in range(n)]
    order = rng.permutation(n)
    return [vals[int(i)] for i in order]


def _title(base_seed: int, index: int) -> str:
    combos = len(_ADJECTIVES) * len(_NOUNS)
    perm = _rng(base_seed, 2).permutation(combos)
    c = int(perm[index % combos])
    title = f"{_ADJECTIVES[c // len(_NOUNS)]} {_NOUNS[c % len(_NOUNS)]}"
    return title if index < combos else f"{title} {index // combos + 1}"


def track_seed(base_seed: int, index: int) -> int:
    return int(np.random.SeedSequence([base_seed, index]).generate_state(1, dtype=np.uint32)[0])


def make_specs(
    n_tracks: int = 10,
    durations: Optional[Sequence[float]] = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    base_seed: int = DEFAULT_BASE_SEED,
    duration_range: tuple = DEFAULT_DURATION_RANGE,
    duration_step: float = 0.1,
) -> list:
    """Parametric track specs. Tracks are drawn in blocks of ten: inside a block tempos,
    tonics, meters, modes and durations are stratified so they differ pairwise.
    Track i depends only on (base_seed, i) and its duration, so prefixes are stable."""
    if not isinstance(n_tracks, int) or n_tracks < 1:
        raise ValueError("n_tracks must be a positive integer")
    if durations is not None and len(durations) != n_tracks:
        raise ValueError("durations must have one value per track")
    specs = []
    for block in range((n_tracks + 9) // 10):
        rng = _rng(base_seed, 1, block)
        meters = [("4/4", "3/4", "6/8", "4/4", "5/4", "12/8", "4/4", "7/8", "3/4", "4/4")[int(i)] for i in rng.permutation(10)]
        modes = [("major", "minor", "dorian", "mixolydian", "major", "harmonic_minor", "lydian", "minor",
                  "phrygian", "major")[int(i)] for i in rng.permutation(10)]
        tonics = [int(x) for x in rng.permutation(12)[:10]]
        tempo_bins = [70 + 8 * b + int(rng.integers(0, 7)) for b in range(10)]
        compound = [i for i in range(10) if METERS[meters[i]].compound]
        slow = [int(b) for b in rng.permutation(5)]
        tempos = [0] * 10
        used = set()
        for i, b in zip(compound, slow):
            tempos[i] = tempo_bins[b]
            used.add(b)
        rest = [b for b in rng.permutation(10) if int(b) not in used]
        for i in range(10):
            if i not in compound:
                tempos[i] = tempo_bins[int(rest.pop(0))]
        block_durs = _stratified_values(rng, 10, duration_range[0], duration_range[1], duration_step)
        leads = [LEAD_TIMBRES[int(i)] for i in rng.permutation(len(LEAD_TIMBRES))] * 2
        chords = [CHORD_TIMBRES[int(i)] for i in rng.permutation(len(CHORD_TIMBRES))] * 2
        basses = [BASS_TIMBRES[int(i)] for i in rng.permutation(len(BASS_TIMBRES))] * 2
        counters = [COUNTER_TIMBRES[int(i)] for i in rng.permutation(len(COUNTER_TIMBRES))] * 2
        kits = [KITS[int(i)] for i in rng.permutation(len(KITS))] * 3
        cstyles = [CHORD_STYLES[int(i)] for i in rng.permutation(len(CHORD_STYLES))] * 3
        bstyles = [BASS_STYLES[int(i)] for i in rng.permutation(len(BASS_STYLES))] * 3
        for j in range(10):
            index = block * 10 + j
            if index >= n_tracks:
                break
            r = _rng(base_seed, 3, index)
            meter, mode = meters[j], modes[j]
            progs = PROGRESSIONS[mode]
            pa, pb = (int(x) for x in r.permutation(len(progs))[:2])
            lead = leads[j]
            counter = counters[j] if counters[j] != lead else counters[j + 1]
            chord_t = chords[j] if chords[j] != lead else chords[j + 1]
            tempo = float(tempos[j])
            duration = float(durations[index]) if durations is not None else block_durs[j]
            spec = TrackSpec(
                id=f"t{index:02d}",
                title=_title(base_seed, index),
                seed=track_seed(base_seed, index),
                duration_s=duration,
                sample_rate=int(sample_rate),
                tempo_bpm=tempo,
                meter=meter,
                key=KEY_NAMES[tonics[j]],
                mode=mode,
                progression=tuple(roman_numeral(mode, d) for d in progs[pa]),
                progression_b=tuple(roman_numeral(mode, d) for d in progs[pb]),
                bars_per_chord=2 if (tempo >= 118 and r.random() < 0.6) else 1,
                chord_sevenths=bool(r.random() < 0.35),
                instruments=(("lead", lead), ("counter", counter), ("chords", chord_t),
                             ("bass", basses[j]), ("drums", kits[j])),
                chord_style=cstyles[j],
                bass_style=bstyles[j],
                drum_style=DRUM_STYLES[meter][int(r.integers(len(DRUM_STYLES[meter])))],
                swing=0.0 if METERS[meter].compound else (0.0, 0.0, 0.12, 0.2)[int(r.integers(4))],
                melody_density=round(0.4 + 0.35 * float(r.random()), 2),
                lead_register=int(r.integers(67, 77)),
                reverb_mix=round(0.12 + 0.18 * float(r.random()), 2),
                reverb_s=round(0.8 + 0.8 * float(r.random()), 2),
                form=FORMS[int(r.integers(len(FORMS)))],
            )
            validate_spec(spec)
            specs.append(spec)
    return specs


def default_specs(
    n_tracks: int = 10,
    duration_s: Optional[float] = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    base_seed: int = DEFAULT_BASE_SEED,
) -> list:
    """Default catalog specs: ~165-195 s tracks with pairwise different durations
    (per block of ten); ``duration_s`` gives every track the same duration."""
    durations = None if duration_s is None else [float(duration_s)] * n_tracks
    return make_specs(n_tracks, durations=durations, sample_rate=sample_rate, base_seed=base_seed)
