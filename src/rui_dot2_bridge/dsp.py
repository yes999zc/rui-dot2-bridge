"""Loudness processing for the Dot 2 output path: stock EQ ladder + limiter.

Why this exists: the stock FireOS output applies a volume-indexed FIR
(`EQ_50`…`EQ_100`, boundaries 50/60/70/80/90/100 %) plus a multiband
compressor. EchoMuse is flat, so at 100 % the Dot 2 is ~10-14 dB quieter than
stock. With the owner's approval (2026-10-24 A/B listening) we rebuild that
stage on the controller side: FIR curve by volume, then a look-ahead limiter.

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
    trim_db: float = 0.0
    curve_ladder: bool = True  # follow the stock volume boundaries
    limiter_ceiling_db: float = -1.0
    limiter_lookahead_ms: float = 5.0
    limiter_release_ms: float = 80.0
    bands: list[dict] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "LoudnessConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_json(self) -> dict:
        return asdict(self)

    def merged(self, patch: dict) -> "LoudnessConfig":
        data = self.to_json()
        for key, value in patch.items():
            if key in data and key != "bands":
                data[key] = value
        if "bands" in patch:
            data["bands"] = [dict(b) for b in patch["bands"]]
        return LoudnessConfig.from_json(data)


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
        self.hard_clip = 10 ** (hard_clip_db / 20.0)
        self.lookahead = max(1, int(rate * lookahead_ms / 1000))
        self.release = math.exp(-1.0 / max(1.0, rate * release_ms / 1000.0))
        self.gain = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        padded = np.pad(np.abs(x), (0, self.lookahead), mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, self.lookahead + 1)
        peak = windows.max(axis=1)[: len(x)]
        desired = np.minimum(1.0, self.ceiling / np.maximum(peak, 1e-12))
        out = np.empty_like(x)
        g = self.gain
        release = self.release
        for i, target in enumerate(desired):
            g = target if target < g else release * g + (1.0 - release) * target
            out[i] = x[i] * g
        self.gain = g
        return np.clip(out, -self.hard_clip, self.hard_clip)


class LoudnessProcessor:
    """Stock EQ (FIR ladder) or custom parametric EQ, then the limiter."""

    def __init__(self, config: LoudnessConfig, curve_dir: Path | None,
                 rate: int = 48000) -> None:
        self.rate = rate
        self.curves = load_curves(curve_dir) if curve_dir else {}
        self.config = config
        self.curve_name = ""
        self._fir: FirStream | None = None
        self._biquads: list[BiquadStream] = []
        self._limiter = Limiter(rate, config.limiter_ceiling_db,
                                config.limiter_lookahead_ms, config.limiter_release_ms)
        self._rebuild()
        if config.enabled and config.mode == "stock" and not self.curves:
            log.warning("loudness: mode=stock but no EQ_*.cfg found; running flat "
                        "(pull them from the device into DOT2_EQ_DIR)")

    # ------------------------------------------------------------ configuration
    def _rebuild(self) -> None:
        cfg = self.config
        if not cfg.enabled or cfg.mode == "off":
            self._fir = None
            self._biquads = []
            return
        if cfg.mode == "stock":
            if self.curves:
                name, taps = curve_for_level(self.curves, 127, cfg.curve_ladder)
                self.curve_name = name
                self._fir = FirStream(taps)
            else:
                self._fir = None
        elif cfg.mode == "custom":
            self._fir = None
            self._biquads = [BiquadStream(Band(**band), self.rate) for band in cfg.bands]
        self._limiter = Limiter(self.rate, cfg.limiter_ceiling_db,
                                cfg.limiter_lookahead_ms, cfg.limiter_release_ms)

    def update(self, patch: dict) -> LoudnessConfig:
        self.config = self.config.merged(patch)
        self._rebuild()
        return self.config

    def set_level(self, level: int) -> str:
        """Switch the stock curve to the one this device level selects."""
        if self.config.mode != "stock" or not self.curves or not self.config.curve_ladder:
            return self.curve_name
        name, taps = curve_for_level(self.curves, level, True)
        if name != self.curve_name:
            self.curve_name = name
            self._fir = FirStream(taps)
        return name

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
        if cfg.trim_db:
            x = x * (10 ** (cfg.trim_db / 20.0))
        x = self._limiter.process(x)
        return np.clip(x, -1.0, 1.0).__mul__(32767.0).astype("<i2").tobytes()

    def describe(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "mode": self.config.mode,
            "curve": self.curve_name,
            "curves_loaded": sorted(self.curves),
            "trim_db": self.config.trim_db,
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
