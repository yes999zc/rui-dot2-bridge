"""Loudness processing for the Dot 2 output path: stock EQ ladder + limiter.

Why this exists: the stock FireOS output applies a volume-indexed FIR
(`EQ_50`…`EQ_100`, boundaries 50/60/70/80/90/100 %) plus a multiband
compressor. EchoMuse is flat, so at 100 % the Dot 2 is ~10-14 dB quieter than
stock. With the owner's approval (2026-10-24 A/B listening) we rebuild that
stage on the controller side: FIR curve by volume, then a look-ahead limiter.

Corrected ladder (2026-10-26): the vendor files are not a tone ladder at all.
`EQ_50`, `EQ_60` and `EQ_70` are byte-identical, and every curve has the same
shape (a +20…+26 dB hump at 150-250 Hz, a -8…-12 dB hole at 2-3 kHz) with only
the scalar gain changing. Replaying it on a small mono speaker gave +23 dB
100-300 Hz over 1-3 kHz and kept the limiter at -10 dB average, heard as
"muffled, like it is inside a barrel". `ladder_correct` (default on) keeps the
six tier names and the level→tier mapping but rebuilds each tier as a
small-speaker target: bass falls and presence rises with the tier, and the
sub-bass the driver cannot reproduce is rolled off instead of feeding the
limiter. `ladder_correct=false` restores the raw vendor curve for A/B.

Everything here is streaming and stateful: rui hands us PCM in chunks, so the
FIR keeps its overlap-add tail, the biquads keep their delay line and the
limiter keeps its gain across calls. No chunk boundaries in the output.

Parameters are runtime-tunable (see `control.py`): rui's backend can switch
modes, trim the gain, edit parametric bands and limiter settings without a
restart. `LoudnessConfig` is the serialisable truth.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

CURVES = ("EQ_50", "EQ_60", "EQ_70", "EQ_80", "EQ_90", "EQ_100")
BOUNDARIES = ((50, "EQ_50"), (60, "EQ_60"), (70, "EQ_70"),
              (80, "EQ_80"), (90, "EQ_90"), (100, "EQ_100"))

#: Tier index of each stock curve (EQ_50 = 0 … EQ_100 = 5).
CURVE_TIERS = {name: index for index, name in enumerate(CURVES)}

#: Corrected-ladder target (relative to 1 kHz, before the limiter):
#: a broad bass shelf, a broad presence lift, and a 2nd-order roll-off below
#: the driver's usable range. Constants rather than config because they define
#: the meaning of the six tiers; only the per-tier gains are tunable.
LADDER_BASS_HZ = 350.0
LADDER_PRESENCE_HZ = 4200.0
LADDER_PRESENCE_OCTAVES = 1.05
LADDER_HIGHPASS_HZ = 70.0
#: The target curve is sampled on this fine grid before the impulse is cut down
#: to ``ladder_taps``. A 1024-point grid at 48 kHz has its first non-DC bin at
#: 46.9 Hz, so 20-47 Hz cannot be described there; the old ``freqs[0]=freqs[1]``
#: workaround left the sub-bass unshaped (EQ_100 measured only -2.6 dB at 20 Hz,
#: i.e. no 2nd-order roll-off at all). Designing at 8192 points (5.9 Hz bins)
#: and keeping the central 1024 taps realises the roll-off while the linear-
#: phase group delay stays 512 samples = 10.67 ms.
LADDER_DESIGN_TAPS = 8192
#: 100-300 Hz may sit at most this far above 1-3 kHz (design target, asserted
#: by tests/test_dsp.py). The old vendor curve measured ~+25 dB.
LADDER_BASS_TARGET_DB = 6.0
#: 3-6 kHz may not fall this far below 1-3 kHz (old vendor curve: ~-3 dB).
LADDER_PRESENCE_FLOOR_DB = -1.0


@dataclass
class Band:
    """One parametric band (RBJ cookbook)."""

    type: str = "peaking"      # peaking | lowshelf | highshelf
    freq: float = 1000.0
    gain_db: float = 0.0
    q: float = 0.7


@dataclass
class LoudnessConfig:
    enabled: bool = True
    mode: str = "stock"        # stock (FIR ladder) | custom (parametric) | off
    trim_db: float = -2.0      # owner-approved 2026-10-24: the last word of a phrase
    #                            ("套") felt sharp at 0 dB; -2 dB keeps the loudness
    curve_ladder: bool = True  # follow the stock volume boundaries
    # Corrected ladder (2026-10-26). The vendor ladder is one fixed tone at six
    # gains, and EQ_50/60/70 are the same file. These values rebuild the tiers:
    # bass falls and presence rises from EQ_50 to EQ_100, so every tier has a
    # different curve, not just a different scalar gain.
    ladder_correct: bool = True
    ladder_bass_db_50: float = 9.0      # 100-300 Hz shelf at tier EQ_50
    ladder_bass_db_100: float = 6.0     # … at tier EQ_100
    ladder_presence_db_50: float = 0.8  # broad 3-6 kHz lift at EQ_50
    ladder_presence_db_100: float = 3.5 # … at EQ_100
    ladder_gain_db_50: float = 2.0      # absolute gain at 1 kHz, tier EQ_50
    ladder_gain_db_100: float = 10.0    # … at EQ_100
    ladder_taps: int = 1024             # linear-phase FIR (same delay as stock)
    limiter_ceiling_db: float = -1.0
    limiter_lookahead_ms: float = 5.0
    limiter_release_ms: float = 80.0
    # Optional post-EQ tone shaping, kept for runtime tuning. The 2026-10-24
    # v11 defaults (-6 dB low shelf at 250 Hz, -2 dB high shelf at 4 kHz) were
    # an attempt to tame the stock curve's bass hump; with the corrected ladder
    # that hump is gone, and stacking both makes the speaker thin. Defaults are
    # therefore flat, and the fields remain for A/B and user preference.
    lowcut_hz: float = 250.0
    lowcut_db: float = 0.0
    highshelf_hz: float = 4000.0
    highshelf_db: float = 0.0
    bands: list[dict] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> LoudnessConfig:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_json(self) -> dict:
        return asdict(self)

    def merged(self, patch: dict) -> LoudnessConfig:
        data = self.to_json()
        for key, value in patch.items():
            if key in data and key != "bands":
                data[key] = value
        if "bands" in patch:
            data["bands"] = [dict(b) for b in patch["bands"]]
        return LoudnessConfig.from_json(data)


def fade_in_pcm(pcm: bytes, rate: int = 48000, ms: float = 10.0) -> bytes:
    """Linear ramp from zero on the first ``ms`` of a PCM block.

    Used on a late tail fragment: the fragment is sent one EOS-idle after the
    last whole period, so its first sample lands on a silent wire and a hard
    onset is heard as a click (the 20 ms tail of the 490 ms "在呢" cue,
    2026-10-25). A short ramp turns that into an inaudible swell.
    """
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    ramp = min(len(samples), max(1, int(rate * ms / 1000.0)))
    if ramp >= 2:
        samples[:ramp] *= np.linspace(0.0, 1.0, ramp)
    return samples.astype("<i2").tobytes()


def fade_out_pcm(pcm: bytes, rate: int = 48000, ms: float = 10.0) -> bytes:
    """Linear ramp to zero on the last ``ms`` of a PCM block (click-free stop).

    The Dot 2 ends a 0x02 stream with whatever the last sample is; a non-zero
    sample stepping to silence is audible as a small "tick" after the last word.
    The processor's FIR/limiter delay (~16 ms) also means the natural tail is
    cut, so the ramp has to be applied here rather than skipped as redundant.
    """
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    ramp = min(len(samples), max(1, int(rate * ms / 1000.0)))
    if ramp >= 2:
        samples[-ramp:] *= np.linspace(1.0, 0.0, ramp)
    return samples.astype("<i2").tobytes()


def load_curves(directory: Path) -> dict[str, np.ndarray]:
    curves: dict[str, np.ndarray] = {}
    for name in CURVES:
        path = directory / f"{name}.cfg"
        if not path.is_file():
            continue
        raw = path.read_text()
        taps = [float(part) for part in raw.replace("\n", " ").split(",") if part.strip()]
        curves[name] = np.asarray(taps, dtype=np.float64)
    return curves


def curve_for_level(curves: dict[str, np.ndarray], level: int, ladder: bool = True) -> tuple[str, np.ndarray]:
    if not ladder:
        name = "EQ_100" if "EQ_100" in curves else sorted(curves)[-1]
        return name, curves[name]
    percent = max(0, min(127, int(level))) * 100.0 / 127.0
    for boundary, name in BOUNDARIES:
        if percent <= boundary and name in curves:
            return name, curves[name]
    name = "EQ_100" if "EQ_100" in curves else sorted(curves)[-1]
    return name, curves[name]


def ladder_tier(name: str) -> int:
    """Tier index for a stock curve name (unknown names are treated as EQ_100)."""
    return CURVE_TIERS.get(name, len(CURVES) - 1)


def ladder_params(name: str, config: LoudnessConfig) -> dict[str, float]:
    """Per-tier target: bass, presence and absolute 1 kHz gain, in dB."""
    fraction = ladder_tier(name) / (len(CURVES) - 1)
    return {
        "bass_db": config.ladder_bass_db_50
        + (config.ladder_bass_db_100 - config.ladder_bass_db_50) * fraction,
        "presence_db": config.ladder_presence_db_50
        + (config.ladder_presence_db_100 - config.ladder_presence_db_50) * fraction,
        "gain_db": config.ladder_gain_db_50
        + (config.ladder_gain_db_100 - config.ladder_gain_db_50) * fraction,
    }


def ladder_tone_db(freqs: np.ndarray, params: dict[str, float]) -> np.ndarray:
    """Target magnitude (dB, relative to 1 kHz) for one corrected tier.

    A broad bass shelf, a broad presence lift and a 2nd-order roll-off below
    ``LADDER_HIGHPASS_HZ``. The roll-off matters: the Dot 2 driver cannot
    reproduce 20-60 Hz, but the stock curve still boosted 100-200 Hz by >30 dB,
    so every kick pushed the limiter down for the whole phrase.
    """
    f = np.maximum(np.asarray(freqs, dtype=np.float64), 1.0)
    bass = params["bass_db"] / (1.0 + (f / LADDER_BASS_HZ) ** 2)
    presence = params["presence_db"] * np.exp(
        -0.5 * (np.log2(f / LADDER_PRESENCE_HZ) / LADDER_PRESENCE_OCTAVES) ** 2
    )
    ratio = (f / LADDER_HIGHPASS_HZ) ** 2
    highpass = 10.0 * np.log10(ratio ** 2 / (1.0 + ratio ** 2))
    return bass + presence + highpass


def build_ladder_curve(name: str, config: LoudnessConfig, rate: int = 48000) -> np.ndarray:
    """Linear-phase FIR realising the corrected target for one tier.

    The stock files are still what selects the tier (and what
    ``ladder_correct=false`` plays), but the corrected tone is designed here so
    the six tiers differ even when the vendor files are byte-identical.

    Design grid vs. FIR length: the smooth target is sampled at
    ``LADDER_DESIGN_TAPS`` (5.9 Hz bins at 48 kHz) and the central
    ``ladder_taps`` samples are kept. Sampling the same target directly on the
    1024-point grid aliases the impulse and leaves everything below the first
    bin (46.9 Hz) uncontrolled; the old code papered over that by copying bin 1
    into bin 0, which is why the 70 Hz high-pass measured -2.6 dB at 20 Hz
    instead of the -17 dB the design asks for. The rectangular cut is
    deliberate: a Hann/Blackman taper widens the low-frequency transition
    (measured -7.4/-5.6 dB at 20 Hz) and undoes the roll-off.
    """
    taps = max(64, int(config.ladder_taps))
    taps -= taps % 2  # even length: the group delay is taps//2 = 10.67 ms @ 48 kHz
    design_taps = max(LADDER_DESIGN_TAPS, taps * 8)
    design_taps += design_taps % 2
    freqs = np.fft.rfftfreq(design_taps, 1.0 / rate)
    params = ladder_params(name, config)
    magnitude = 10.0 ** ((ladder_tone_db(freqs, params) + params["gain_db"]) / 20.0)
    # The target is a 2nd-order high-pass: zero at DC. Pin the DC bin to 0
    # instead of evaluating the formula at f=0 (which uses the 1 Hz guard).
    magnitude[0] = 0.0
    # Zero-phase design -> shift to linear phase, then keep the central taps.
    impulse = np.fft.irfft(magnitude, design_taps)
    impulse = np.roll(impulse, design_taps // 2)
    start = design_taps // 2 - taps // 2
    impulse = impulse[start:start + taps].copy()
    # Truncation loses a little gain; pin |H(1 kHz)| to the requested gain so
    # the level ladder stays monotonic instead of drifting by a few tenths of a
    # dB. Measure at 16384 points rather than on the 1024-point grid.
    spectrum = np.abs(np.fft.rfft(impulse, 1 << 14))
    grid = np.fft.rfftfreq(1 << 14, 1.0 / rate)
    at_1k = float(np.interp(1000.0, grid, spectrum))
    impulse *= 10.0 ** (params["gain_db"] / 20.0) / max(at_1k, 1e-12)
    return impulse


class FirStream:
    """Overlap-add FIR: stateful, one output sample per input sample."""

    def __init__(self, taps: np.ndarray) -> None:
        self.taps = np.asarray(taps, dtype=np.float64)
        self.tail = np.zeros(max(0, len(self.taps) - 1), dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        y = np.convolve(x, self.taps)
        y[: len(self.tail)] += self.tail
        out = y[: len(x)]
        self.tail = y[len(x):]
        return out


def _biquad_coeffs(band: Band, rate: int) -> tuple[float, float, float, float, float]:
    """RBJ low/high shelf and peaking coefficients, normalised by a0."""
    a = 10 ** (band.gain_db / 40.0)
    w0 = 2 * math.pi * max(1e-6, min(rate / 2 - 1, band.freq)) / rate
    cos_w, sin_w = math.cos(w0), math.sin(w0)
    alpha = sin_w / (2 * max(1e-3, band.q))
    if band.type == "lowshelf":
        sq = 2 * math.sqrt(a) * alpha
        b0 = a * ((a + 1) - (a - 1) * cos_w + sq)
        b1 = 2 * a * ((a - 1) - (a + 1) * cos_w)
        b2 = a * ((a + 1) - (a - 1) * cos_w - sq)
        a0 = (a + 1) + (a - 1) * cos_w + sq
        a1 = -2 * ((a - 1) + (a + 1) * cos_w)
        a2 = (a + 1) + (a - 1) * cos_w - sq
    elif band.type == "highshelf":
        sq = 2 * math.sqrt(a) * alpha
        b0 = a * ((a + 1) + (a - 1) * cos_w + sq)
        b1 = -2 * a * ((a - 1) + (a + 1) * cos_w)
        b2 = a * ((a + 1) + (a - 1) * cos_w - sq)
        a0 = (a + 1) - (a - 1) * cos_w + sq
        a1 = 2 * ((a - 1) - (a + 1) * cos_w)
        a2 = (a + 1) - (a - 1) * cos_w - sq
    else:  # peaking
        b0 = 1 + alpha * a
        b1 = -2 * cos_w
        b2 = 1 - alpha * a
        a0 = 1 + alpha / a
        a1 = -2 * cos_w
        a2 = 1 - alpha / a
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


class BiquadStream:
    """One RBJ biquad in direct form I, stateful across chunks."""

    def __init__(self, band: Band, rate: int) -> None:
        self.b0, self.b1, self.b2, self.a1, self.a2 = _biquad_coeffs(band, rate)
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        for i, sample in enumerate(x):
            y = b0 * sample + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            out[i] = y
            x2, x1 = x1, sample
            y2, y1 = y1, y
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


class Limiter:
    """Look-ahead peak limiter; gain state carries across chunks."""

    def __init__(self, rate: int, ceiling_db: float = -1.0,
                 lookahead_ms: float = 5.0, release_ms: float = 80.0,
                 hard_clip_db: float = -0.1) -> None:
        self.rate = rate
        self.ceiling = 10 ** (ceiling_db / 20.0)
        # A hair below the target: gain smoothing lags by a sample or two, and a
        # 0.001 dB overshoot is not worth a hard clip.
        self.target = self.ceiling * 0.999
        self.hard_clip = 10 ** (hard_clip_db / 20.0)
        self.lookahead = max(1, int(rate * lookahead_ms / 1000))
        self.release = math.exp(-1.0 / max(1.0, rate * release_ms / 1000.0))
        self.gain = 1.0
        self.min_gain = 1.0
        self.max_gain = 1.0
        # Per-stream gain statistics. The offline diagnosis of 2026-10-26 had to
        # re-implement the chain because nothing exposed how hard the limiter was
        # working; the average is what tells "the EQ is too hot" apart from
        # "the peaks are hot".
        self.gain_db_sum = 0.0
        self.gain_db_count = 0
        # Output is delayed by the lookahead so the window really is in the
        # future; the old per-chunk version padded the chunk end with its own
        # last sample, which under-limited the last ~5 ms of every 100 ms and
        # is what made the integrated path sound worse than the offline render.
        self.pending = np.zeros(self.lookahead, dtype=np.float64)

    @property
    def average_gain_db(self) -> float:
        """Mean gain the limiter applied since the last reset (dB, ≤ 0)."""
        return self.gain_db_sum / self.gain_db_count if self.gain_db_count else 0.0

    def reset_stats(self) -> None:
        self.min_gain = 1.0
        self.max_gain = 1.0
        self.gain_db_sum = 0.0
        self.gain_db_count = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        combined = np.concatenate([self.pending, x])
        # peak must be taken over |signal|: with signed samples a window can sit
        # entirely on a large NEGATIVE excursion (asymmetric speech after the EQ),
        # and forgetting abs() let those negative peaks through to the hard clip -
        # the "破音" heard on 2026-10-24 during P2. Regression in tests.
        magnitude = np.abs(combined)
        padded = np.concatenate([magnitude, np.full(self.lookahead, magnitude[-1])])
        windows = np.lib.stride_tricks.sliding_window_view(padded, self.lookahead + 1)
        peak = windows.max(axis=1)[: len(x)]
        desired = np.minimum(1.0, self.target / np.maximum(peak, 1e-12))
        out = np.empty(len(x), dtype=np.float64)
        gains = np.empty(len(x), dtype=np.float64)
        g = self.gain
        release = self.release
        gmin = gmax = g
        for i, target in enumerate(desired):
            g = target if target < g else release * g + (1.0 - release) * target
            out[i] = combined[i] * g
            gains[i] = g
            gmin = min(gmin, g)
            gmax = max(gmax, g)
        self.gain = g
        self.min_gain = min(self.min_gain, gmin)
        self.max_gain = max(self.max_gain, gmax)
        self.gain_db_sum += float(np.sum(20.0 * np.log10(np.maximum(gains, 1e-9))))
        self.gain_db_count += len(gains)
        self.pending = combined[len(x):]
        return np.clip(out, -self.hard_clip, self.hard_clip)


class LoudnessProcessor:
    """Stock EQ (FIR ladder) or custom parametric EQ, then the limiter."""

    def __init__(self, config: LoudnessConfig, curve_dir: Path | None,
                 rate: int = 48000) -> None:
        self.rate = rate
        self.curves = load_curves(curve_dir) if curve_dir else {}
        self.config = config
        self.curve_name = ""
        self._level = 127
        self._fir: FirStream | None = None
        self._shelves: list[BiquadStream] = []
        self._biquads: list[BiquadStream] = []
        self._limiter = Limiter(rate, config.limiter_ceiling_db,
                                config.limiter_lookahead_ms, config.limiter_release_ms)
        self._rebuild()
        if config.enabled and config.mode == "stock" and not self.curves:
            log.warning("loudness: mode=stock but no EQ_*.cfg found; running flat "
                        "(pull them from the device into DOT2_EQ_DIR)")

    # ------------------------------------------------------------ configuration
    def _apply_curve(self, name: str, taps: np.ndarray) -> None:
        """Install the FIR for one tier: corrected target or raw vendor taps."""
        self.curve_name = name
        if self.config.ladder_correct:
            self._fir = FirStream(build_ladder_curve(name, self.config, self.rate))
        else:
            self._fir = FirStream(taps)

    def _rebuild(self) -> None:
        cfg = self.config
        if not cfg.enabled or cfg.mode == "off":
            self.curve_name = ""
            self._fir = None
            self._shelves = []
            self._biquads = []
            return
        if cfg.mode == "stock":
            if self.curves:
                name, taps = curve_for_level(self.curves, self._level, cfg.curve_ladder)
                self._apply_curve(name, taps)
            else:
                # Flat fallback: describe() must not keep reporting the tier
                # that was active before the curves went missing.
                self.curve_name = ""
                self._fir = None
        elif cfg.mode == "custom":
            # No stock curve is playing in custom mode; a stale curve_name here
            # made GET /loudness claim EQ_100 was still applied.
            self.curve_name = ""
            self._fir = None
            self._biquads = [BiquadStream(Band(**band), self.rate) for band in cfg.bands]
        # Tone shaping applies in both modes, after the EQ and before trim/limit.
        self._shelves = []
        if cfg.lowcut_db:
            self._shelves.append(BiquadStream(
                Band(type="lowshelf", freq=cfg.lowcut_hz, gain_db=-abs(cfg.lowcut_db), q=0.7), self.rate))
        if cfg.highshelf_db:
            self._shelves.append(BiquadStream(
                Band(type="highshelf", freq=cfg.highshelf_hz, gain_db=-abs(cfg.highshelf_db), q=0.7), self.rate))
        self._limiter = Limiter(self.rate, cfg.limiter_ceiling_db,
                                cfg.limiter_lookahead_ms, cfg.limiter_release_ms)

    def update(self, patch: dict) -> LoudnessConfig:
        self.config = self.config.merged(patch)
        self._rebuild()
        return self.config

    def set_level(self, level: int) -> str:
        """Switch the stock curve to the one this device level selects.

        With the corrected ladder each tier is a whole curve, so the FIR is
        rebuilt only when the tier changes (not on every chunk within a tier).
        """
        self._level = max(0, min(127, int(level)))
        if self.config.mode != "stock" or not self.curves or not self.config.curve_ladder:
            return self.curve_name
        name, taps = curve_for_level(self.curves, self._level, True)
        if name != self.curve_name or self._fir is None:
            self._apply_curve(name, taps)
        return name

    def reset_stream(self) -> None:
        """Start a new playback stream with clean state.

        The FIR tail, the biquad delay lines and the limiter's look-ahead line all
        carry samples across chunks. That is what makes chunked processing seamless
        *within* a stream, but across an idle boundary it replays the last few
        milliseconds of the previous stream on top of the first samples of the new
        one - heard as a click/noise right after a reply ("在呢" followed by a
        rattle, 2026-10-24).
        """
        if self._fir is not None:
            self._fir.tail[:] = 0.0
        for biquad in (*self._biquads, *self._shelves):
            biquad.x1 = biquad.x2 = biquad.y1 = biquad.y2 = 0.0
        self._limiter.pending[:] = 0.0
        self._limiter.gain = 1.0
        self._limiter.reset_stats()

    # -------------------------------------------------------------- processing
    def process_bytes(self, pcm: bytes) -> bytes:
        cfg = self.config
        if not cfg.enabled or cfg.mode == "off" or not pcm:
            return pcm
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        if self._fir is not None:
            x = self._fir.process(x)
        for biquad in self._biquads:
            x = biquad.process(x)
        for shelf in self._shelves:
            x = shelf.process(x)
        if cfg.trim_db:
            x = x * (10 ** (cfg.trim_db / 20.0))
        x = self._limiter.process(x)
        return np.clip(x, -1.0, 1.0).__mul__(32767.0).astype("<i2").tobytes()

    def describe(self) -> dict:
        params = ladder_params(self.curve_name, self.config) if self.curve_name else {}
        return {
            "enabled": self.config.enabled,
            "mode": self.config.mode,
            "curve": self.curve_name,
            "curves_loaded": sorted(self.curves),
            "trim_db": self.config.trim_db,
            "ladder": {
                "corrected": bool(self.config.ladder_correct),
                "tier": ladder_tier(self.curve_name) if self.curve_name else None,
                **{key: round(value, 2) for key, value in params.items()},
            },
            "tone": {"lowcut_hz": self.config.lowcut_hz, "lowcut_db": self.config.lowcut_db,
                     "highshelf_hz": self.config.highshelf_hz,
                     "highshelf_db": self.config.highshelf_db},
            "bands": self.config.bands,
            "limiter": {
                "ceiling_db": self.config.limiter_ceiling_db,
                "lookahead_ms": self.config.limiter_lookahead_ms,
                "release_ms": self.config.limiter_release_ms,
            },
        }

    @staticmethod
    def dump_config(path: Path, config: LoudnessConfig) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(config.to_json(), ensure_ascii=False, indent=2))
        tmp.replace(path)

    @staticmethod
    def load_config(path: Path) -> LoudnessConfig:
        if not path.is_file():
            return LoudnessConfig()
        try:
            return LoudnessConfig.from_json(json.loads(path.read_text()))
        except Exception:  # noqa: BLE001 - a broken file must not mute the speaker
            log.warning("loudness: ignoring unreadable %s", path)
            return LoudnessConfig()
