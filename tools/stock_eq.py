"""Stock Alexa playback EQ, rebuilt offline from the device's own coefficients.

The Dot 2's stock FireOS applies a volume-indexed FIR (EQ_50..EQ_100 selected by
the volume boundaries [50,60,70,80,90,100] %). EchoMuse ships a flat output, so
the speaker is missing that whole gain stage. This tool reproduces it on the
controller side, for evaluation *before* anything is wired into the adapter.

    python tools/stock_eq.py --check
    python tools/stock_eq.py --in source.wav --level 127 --limiter --out out.wav

Coefficient files are read from ``artifacts/eq-prototype/EQ_*.cfg`` (pulled from
the device read-only; they are vendor files and stay out of git).
"""

from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path

import numpy as np

CFG_DIR = Path("artifacts/eq-prototype")
CURVES = ("EQ_50", "EQ_60", "EQ_70", "EQ_80", "EQ_90", "EQ_100")
#: Stock picks the curve by volume percent; these are the boundaries in AFE.cfg.
BOUNDARIES = ((50, "EQ_50"), (60, "EQ_60"), (70, "EQ_70"),
              (80, "EQ_80"), (90, "EQ_90"), (100, "EQ_100"))


def parse_cfg(path: Path) -> np.ndarray:
    """One line of comma-separated floats -> taps (2048 per curve)."""
    raw = path.read_text()
    taps = [float(part) for part in raw.replace("\n", " ").split(",") if part.strip()]
    return np.asarray(taps, dtype=np.float64)


def load_curves(cfg_dir: Path = CFG_DIR) -> dict[str, np.ndarray]:
    curves = {}
    for name in CURVES:
        path = cfg_dir / f"{name}.cfg"
        if path.is_file():
            curves[name] = parse_cfg(path)
    if not curves:
        raise SystemExit(f"no EQ_*.cfg under {cfg_dir} (pull them from the device first)")
    return curves


def curve_for_level(curves: dict[str, np.ndarray], level: int) -> tuple[str, np.ndarray]:
    percent = max(0, min(127, int(level))) * 100.0 / 127.0
    chosen = "EQ_50"
    for boundary, name in BOUNDARIES:
        if percent <= boundary and name in curves:
            chosen = name
            break
    else:
        chosen = "EQ_100" if "EQ_100" in curves else sorted(curves)[-1]
    return chosen, curves[chosen]


def response_db(taps: np.ndarray, freqs: np.ndarray, fs: int = 48000) -> np.ndarray:
    """Magnitude response in dB at the given frequencies."""
    n = 1 << 16
    h = np.fft.rfft(taps, n)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    mag = np.interp(freqs, f, np.abs(h))
    return 20.0 * np.log10(np.maximum(mag, 1e-12))


def convolve(x: np.ndarray, taps: np.ndarray) -> np.ndarray:
    """Linear convolution, delay-compensated for a symmetric (linear-phase) FIR."""
    n = len(x)
    size = 1 << int(math.ceil(math.log2(n + len(taps) - 1)))
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(taps, size), size)[: n + len(taps) - 1]
    # The stock curves are linear-phase; drop the group delay so audio stays aligned.
    symmetry = float(np.max(np.abs(taps - taps[::-1])))
    delay = (len(taps) - 1) // 2 if symmetry < 1e-9 * (np.max(np.abs(taps)) + 1e-12) else 0
    if delay:
        y = y[delay : delay + n]
    return y


def limit(x: np.ndarray, fs: int = 48000, ceiling_db: float = -1.0,
          lookahead_ms: float = 5.0, release_ms: float = 80.0) -> np.ndarray:
    """Look-ahead peak limiter: gain is computed from the *coming* peak window."""
    ceiling = 10 ** (ceiling_db / 20.0)
    look = max(1, int(fs * lookahead_ms / 1000))
    padded = np.pad(np.abs(x), (0, look), mode="constant")
    windows = np.lib.stride_tricks.sliding_window_view(padded, look + 1)
    peak = windows.max(axis=1)[: len(x)]
    desired = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
    # Attack is immediate, release is a one-pole so it does not pump.
    release = math.exp(-1.0 / (fs * release_ms / 1000.0))
    gain = np.empty_like(desired)
    g = 1.0
    for i, target in enumerate(desired):
        g = target if target < g else release * g + (1.0 - release) * target
        gain[i] = g
    return x * gain


def a_weighted_rms_db(x: np.ndarray, fs: int = 48000) -> float:
    """A-weighted RMS in dBFS (IEC 61672 weights, applied in the frequency domain)."""
    n = 1 << int(math.ceil(math.log2(max(2, len(x)))))
    spectrum = np.fft.rfft(x, n)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    f2 = np.maximum(freqs, 1e-6) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2)
    )
    a = ra * 10 ** (2.0 / 20.0)
    weighted = np.fft.irfft(spectrum * a, n)[: len(x)]
    rms = float(np.sqrt(np.mean(weighted ** 2)))
    return 20.0 * math.log10(max(rms, 1e-12))


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: need mono 16-bit PCM")
        rate = wav.getframerate()
        x = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    return x, rate


def write_wav(path: Path, x: np.ndarray, rate: int) -> int:
    clipped = int(np.sum(np.abs(x) > 1.0))
    pcm = np.clip(x, -1.0, 1.0)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return clipped


def check(curves: dict[str, np.ndarray]) -> None:
    """Parse sanity: known response points and the EQ_100/EQ_50 ratio from upstream docs."""
    freqs = np.array([150.0, 1000.0, 2800.0])
    print(f"{'curve':8s} {'150Hz':>9s} {'1kHz':>9s} {'2.8kHz':>9s}  taps  symmetric")
    for name in CURVES:
        if name not in curves:
            continue
        taps = curves[name]
        db = response_db(taps, freqs)
        symmetry = float(np.max(np.abs(taps - taps[::-1])))
        print(f"{name:8s} {db[0]:+9.2f} {db[1]:+9.2f} {db[2]:+9.2f}  {len(taps):4d}  {symmetry:.2e}")
    if "EQ_100" in curves and "EQ_50" in curves:
        ratio = float(np.max(np.abs(curves["EQ_100"])) / np.max(np.abs(curves["EQ_50"])))
        print(f"\nEQ_100/EQ_50 coefficient ratio = {ratio:.6f} ({20 * math.log10(ratio):+.2f} dB)")
        print("upstream SETUP.md expects 5.334838 (+14.54 dB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cfg-dir", default=str(CFG_DIR))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--in", dest="source")
    parser.add_argument("--out")
    parser.add_argument("--level", type=int, default=127, help="device level 0-127")
    parser.add_argument("--curve", choices=CURVES)
    parser.add_argument("--limiter", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    curves = load_curves(Path(args.cfg_dir))
    if args.check or not args.source:
        check(curves)
        return 0

    x, rate = read_wav(Path(args.source))
    if rate != 48000:
        raise SystemExit(f"{args.source}: expected 48 kHz, got {rate}")
    name, taps = (args.curve, curves[args.curve]) if args.curve else curve_for_level(curves, args.level)
    y = convolve(x, taps)
    clipped_eq = int(np.sum(np.abs(y) > 1.0))
    if args.limiter:
        y = limit(y, rate)
    out = Path(args.out or "eq-out.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    clipped = write_wav(out, y, rate)
    report = {
        "source": str(args.source), "out": str(out), "curve": name, "level": args.level,
        "limiter": bool(args.limiter),
        "source_peak_dbfs": round(20 * math.log10(max(np.max(np.abs(x)), 1e-12)), 2),
        "source_a_rms_dbfs": round(a_weighted_rms_db(x, rate), 2),
        "out_peak_dbfs": round(20 * math.log10(max(np.max(np.abs(y)), 1e-12)), 2),
        "out_a_rms_dbfs": round(a_weighted_rms_db(y, rate), 2),
        "clipped_before_limiter": clipped_eq,
        "clipped_samples_written": clipped,
    }
    report["a_weighted_gain_db"] = round(report["out_a_rms_dbfs"] - report["source_a_rms_dbfs"], 2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
