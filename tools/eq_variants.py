"""Generate loudness variants for listening (evaluation only, not shipped).

The first approved candidate (02) put 89 % of its energy in 120-400 Hz because
the stock EQ boosts 150 Hz by ~40 dB; the small speaker distorts there. These
variants trade some low end for cleanliness while keeping the mid boost that
made speech louder.

    PYTHONPATH=src python tools/eq_variants.py
"""

from __future__ import annotations

import math
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rui_dot2_bridge.dsp import Limiter, load_curves  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EQ_DIR = ROOT / "artifacts/eq-prototype"
SOURCE = EQ_DIR / "00-source.wav"
OUT = EQ_DIR / "variants"


def read(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        data = wav.readframes(wav.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0, rate


def write(path: Path, x: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(x, -1.0, 1.0)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((pcm * 32767.0).astype("<i2").tobytes())


def fft_convolve(x: np.ndarray, taps: np.ndarray) -> np.ndarray:
    n = 1 << int(math.ceil(math.log2(len(x) + len(taps) - 1)))
    y = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(taps, n), n)
    return y[: len(x)]


def zero_phase_gain(x: np.ndarray, rate: int, fn) -> np.ndarray:
    """Apply a magnitude-only filter (evaluation: no latency, no phase)."""
    n = 1 << int(math.ceil(math.log2(len(x))))
    spectrum = np.fft.rfft(x, n)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    return np.fft.irfft(spectrum * fn(freqs), n)[: len(x)]


def highpass_mag(freq: float, order: int = 2):
    def fn(f):
        ratio = np.maximum(f, 1e-6) / freq
        return ratio ** order / np.sqrt(1 + ratio ** (2 * order))
    return fn


def lowshelf_mag(freq: float, gain_db: float):
    def fn(f):
        # smooth transition: full cut below freq, none above 2*freq
        t = np.clip(np.log2(np.maximum(f, 1e-6) / freq), 0, 1)
        return 10 ** (-gain_db * (1 - t) / 20.0)
    return fn


def metrics(x: np.ndarray, rate: int, label: str) -> dict:
    n = 1 << int(math.ceil(math.log2(len(x))))
    spectrum = np.fft.rfft(x, n)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    power = np.abs(spectrum) ** 2
    low = power[(freqs >= 20) & (freqs < 400)].sum() / max(power[20:].sum(), 1e-12)
    return {
        "label": label,
        "peak_dbfs": round(20 * math.log10(max(np.max(np.abs(x)), 1e-12)), 2),
        "a_rms_dbfs": round(a_weighted_rms(x, rate), 2),
        "energy_below_400hz": round(100 * low, 1),
    }


def a_weighted_rms(x: np.ndarray, rate: int = 48000) -> float:
    n = 1 << int(math.ceil(math.log2(max(2, len(x)))))
    spectrum = np.fft.rfft(x, n)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    f2 = np.maximum(freqs, 1e-6) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2))
    weighted = np.fft.irfft(spectrum * ra * 10 ** (2.0 / 20.0), n)[: len(x)]
    return 20 * math.log10(max(float(np.sqrt(np.mean(weighted ** 2))), 1e-12))


def main() -> int:
    x, rate = read(SOURCE)
    curves = load_curves(EQ_DIR)
    limiter = lambda ceiling=-1.0: Limiter(rate, ceiling_db=ceiling)  # noqa: E731

    variants: dict[str, np.ndarray] = {}
    eq100 = fft_convolve(x, curves["EQ_100"])
    eq90 = fft_convolve(x, curves["EQ_90"])

    variants["v0-current"] = limiter().process(eq100)
    variants["v1-hp120"] = limiter().process(zero_phase_gain(eq100, rate, highpass_mag(120)))
    variants["v2-lowcut10"] = limiter().process(zero_phase_gain(eq100, rate, lowshelf_mag(200, 10)))
    variants["v3-eq90"] = limiter().process(eq90)
    variants["v4-ceil3"] = limiter(-3.0).process(eq100)
    variants["v5-hp120-lowcut6"] = limiter().process(
        zero_phase_gain(zero_phase_gain(eq100, rate, highpass_mag(120)), rate, lowshelf_mag(220, 6)))

    # v6: multiband — tame the band the small speaker cannot take, leave the
    # speech band unmodulated. Low band gets a static cut to -8 dBFS peak.
    lo = zero_phase_gain(eq100, rate, highpass_mag(250))
    hi = eq100 - lo
    lo_peak = max(np.max(np.abs(lo)), 1e-9)
    lo = lo * min(1.0, 10 ** (-8 / 20) / lo_peak)
    variants["v6-multiband"] = limiter().process(hi + lo)

    # v7: custom speech-shaped EQ instead of the stock low boost (+10 kHz-ish clarity)
    def custom_mag(f):
        low = 10 ** (12 * np.clip(np.log2(150 / np.maximum(f, 1e-6)) + 1, 0, 1) / 20.0)
        mid = 10 ** (10 * np.exp(-0.5 * (np.log2(np.maximum(f, 1e-6) / 1000.0) / 0.9) ** 2) / 20.0)
        high = 10 ** (4 * np.clip(np.log2(np.maximum(f, 1e-6) / 4000.0), 0, 1) / 20.0)
        return low * mid * high

    custom = zero_phase_gain(x, rate, custom_mag)
    variants["v7-custom-speech"] = limiter().process(custom)

    # v8: EQ_100 with the low band cut 12 dB, mids untouched
    variants["v8-eq100-lowcut12"] = limiter().process(zero_phase_gain(eq100, rate, lowshelf_mag(250, 12)))

    # In between v0 (no cut) and v8 (12 dB), plus a gentle high-shelf trim for
    # the "a bit sharp" impression the bass cut creates.
    def highshelf_cut(freq: float, db: float):
        def fn(f):
            t = np.clip(np.log2(np.maximum(f, 1e-6) / freq), 0, 1)
            return 10 ** (-db * t / 20.0)
        return fn

    variants["v9-lowcut6"] = limiter().process(zero_phase_gain(eq100, rate, lowshelf_mag(250, 6)))
    variants["v10-lowcut8"] = limiter().process(zero_phase_gain(eq100, rate, lowshelf_mag(250, 8)))
    variants["v11-lowcut6-hscut2"] = limiter().process(
        zero_phase_gain(zero_phase_gain(eq100, rate, lowshelf_mag(250, 6)),
                        rate, highshelf_cut(4000, 2)))
    variants["v12-lowcut8-hscut1"] = limiter().process(
        zero_phase_gain(zero_phase_gain(eq100, rate, lowshelf_mag(250, 8)),
                        rate, highshelf_cut(4000, 1)))

    rows = []
    for name, audio in variants.items():
        write(OUT / f"{name}.wav", audio, rate)
        rows.append(metrics(audio, rate, name))
    print(f"source A-weighted RMS: {a_weighted_rms(x, rate):.2f} dBFS")
    for row in rows:
        print(f"{row['label']:20s} peak={row['peak_dbfs']:6.2f}  A={row['a_rms_dbfs']:6.2f}  "
              f"low<400Hz={row['energy_below_400hz']:5.1f}%")
    print(f"\nwritten to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
