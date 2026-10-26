"""Measure the Dot 2 playback chain before/after the corrected EQ ladder.

This is the reproducible source of the numbers in
``docs/plan/speaker-tuning.md`` §EQ 修正 (rui repo). It runs the *production*
``LoudnessProcessor`` -- FIR ladder, shelves, trim and limiter -- so the report
cannot drift away from what the adapter plays.

    python tools/eq_report.py
    python tools/eq_report.py --json
    python tools/eq_report.py --wav /tmp/diag-music.wav   # your own 48 kHz mono S16

Frequency response is measured with an impulse through the linear part of the
chain (equivalent to a sweep, but exact at every bin). The limiter average is
measured on a deterministic 10 s programme (pink noise + 60 Hz kick,
band-limited to 8 kHz, RMS -15.4 dBFS), the same recipe the 2026-10-26
diagnosis used. It is generated here so the report does not depend on
gitignored recordings.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from pathlib import Path

import numpy as np

# Run from a checkout without installing the package (`python tools/eq_report.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rui_dot2_bridge.dsp import LoudnessConfig, LoudnessProcessor

RATE = 48000
FREQS = (100, 200, 500, 1000, 2000, 4000, 8000)
DENSE = (20, 30, 47, 50, 60, 70, 80, 100, 125, 150, 174, 200, 250, 300, 400, 500, 700,
         1000, 1500, 2000, 3000, 4000, 5000, 6000, 7000, 8000)
#: Points used to show that the 2nd-order roll-off survives the 1024-tap cut.
SUB_BASS = (20, 30, 47, 60)
#: Representative device levels (0-127) that select EQ_50 … EQ_100.
TIER_LEVELS = ((50, 50), (60, 64), (70, 77), (80, 89), (90, 102), (100, 127))

#: The 2026-10-24 "v11" tuning, kept only as the before/after baseline.
LEGACY = dict(ladder_correct=False, lowcut_db=6.0, highshelf_db=2.0, trim_db=-2.0)


def pink_kick_music(seconds: float = 10.0) -> np.ndarray:
    rng = np.random.default_rng(3)
    n = int(RATE * seconds)
    spectrum = rng.standard_normal(n // 2 + 1) + 1j * rng.standard_normal(n // 2 + 1)
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    freqs[0] = freqs[1]
    spectrum /= np.sqrt(freqs)
    signal = np.fft.irfft(spectrum, n)
    signal /= math.sqrt(float(np.mean(signal ** 2)))
    kick_t = np.arange(int(0.12 * RATE)) / RATE
    kick = np.sin(2 * math.pi * 60.0 * kick_t) * np.exp(-kick_t / 0.04)
    for start in range(0, n - len(kick), int(0.5 * RATE)):
        signal[start:start + len(kick)] += 0.9 * kick
    spectrum = np.fft.rfft(signal)
    spectrum[freqs > 7900] = 0.0
    signal = np.fft.irfft(spectrum, n)
    return signal / math.sqrt(float(np.mean(signal ** 2))) * 10 ** (-15.4 / 20.0)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != RATE:
            raise SystemExit(f"{path}: need mono 16-bit {RATE} Hz PCM")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2") / 32768.0


def response_db(processor: LoudnessProcessor) -> np.ndarray:
    """Chain magnitude response at ``DENSE``, measured through process_bytes()."""
    processor.reset_stream()
    impulse = np.zeros(RATE, dtype=np.float64)
    impulse[0] = 0.25  # below the limiter ceiling, so the chain stays linear
    pcm = (impulse * 32767.0).astype("<i2").tobytes()
    out = np.frombuffer(processor.process_bytes(pcm), dtype="<i2") / 32768.0
    spectrum = np.abs(np.fft.rfft(out, 1 << 16))
    grid = np.fft.rfftfreq(1 << 16, 1.0 / RATE)
    return 20.0 * np.log10(np.maximum(np.interp(DENSE, grid, spectrum), 1e-12))


def band_mean(db: np.ndarray, low: float, high: float) -> float:
    return float(np.mean([value for freq, value in zip(DENSE, db, strict=False)
                          if low <= freq <= high]))


def a_weighted_rms_db(signal: np.ndarray) -> float:
    """A-weighted RMS in dBFS (same weighting the 2026-10-26 diagnosis used)."""
    n = 1 << int(math.ceil(math.log2(max(2, len(signal)))))
    spectrum = np.fft.rfft(signal, n)
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    f2 = np.maximum(freqs, 1e-6) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2)
    )
    weighted = np.fft.irfft(spectrum * ra * 10 ** (2.0 / 20.0), n)[: len(signal)]
    return 20.0 * math.log10(max(float(np.sqrt(np.mean(weighted ** 2))), 1e-12))


def limiter_report(processor: LoudnessProcessor, signal: np.ndarray) -> dict:
    processor.reset_stream()
    pcm = np.clip(signal, -1.0, 1.0).__mul__(32767).astype("<i2").tobytes()
    chunks = [processor.process_bytes(pcm[i:i + 9600]) for i in range(0, len(pcm), 9600)]
    out = np.frombuffer(b"".join(chunks), dtype="<i2") / 32768.0
    peak = float(np.max(np.abs(out)))
    rms = float(np.sqrt(np.mean(out ** 2)))
    return {
        "average_gain_db": round(processor._limiter.average_gain_db, 2),
        "min_gain_db": round(20 * math.log10(max(processor._limiter.min_gain, 1e-9)), 2),
        "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2),
        "crest_db": round(20 * math.log10(max(peak, 1e-9) / max(rms, 1e-9)), 2),
        "a_weighted_in_dbfs": round(a_weighted_rms_db(signal), 2),
        "a_weighted_out_dbfs": round(a_weighted_rms_db(out), 2),
        "clipped_samples": int(np.sum(np.abs(out) >= 32767 / 32768)),
    }


def tier_rows(processor: LoudnessProcessor) -> list[dict]:
    rows = []
    for label, level in TIER_LEVELS:
        name = processor.set_level(level)
        db = response_db(processor)
        ref = float(np.interp(1000.0, DENSE, db))
        rows.append({
            "tier": f"EQ_{label}",
            "level": level,
            "curve": name,
            "points_db": {str(freq): round(float(np.interp(freq, DENSE, db) - ref), 1)
                          for freq in FREQS},
            "sub_bass_db": {str(freq): round(float(np.interp(freq, DENSE, db) - ref), 1)
                            for freq in SUB_BASS},
            "bass_vs_mid_db": round(band_mean(db, 100, 300) - band_mean(db, 1000, 3000), 2),
            "presence_vs_mid_db": round(band_mean(db, 3000, 6000) - band_mean(db, 1000, 3000), 2),
        })
    return rows


def run(cfg_dir: Path, signal: np.ndarray, label: str) -> dict:
    legacy = LoudnessProcessor(LoudnessConfig(**LEGACY), cfg_dir)
    corrected = LoudnessProcessor(LoudnessConfig(), cfg_dir)
    return {
        "label": label,
        "legacy_v11": {
            "config": LEGACY,
            "tiers": tier_rows(legacy),
            "limiter": limiter_report(legacy, signal),
        },
        "corrected": {
            "config": {"ladder_correct": True, "lowcut_db": 0.0, "highshelf_db": 0.0,
                       "trim_db": -2.0},
            "tiers": tier_rows(corrected),
            "limiter": limiter_report(corrected, signal),
        },
    }


def print_report(report: dict) -> None:
    for block in ("legacy_v11", "corrected"):
        print(f"\n== {report['label']} / {block}")
        print(f"{'tier':6s} {'curve':7s} " + " ".join(f"{f:>7d}" for f in FREQS)
              + "  bass   pres")
        for row in report[block]["tiers"]:
            points = " ".join(f"{row['points_db'][str(f)]:+7.1f}" for f in FREQS)
            print(f"{row['tier']:6s} {row['curve']:7s} {points} "
                  f"{row['bass_vs_mid_db']:+6.2f} {row['presence_vs_mid_db']:+6.2f}")
            sub = " ".join(f"{freq:>2d}:{row['sub_bass_db'][str(freq)]:+6.1f}"
                           for freq in SUB_BASS)
            print(f"{'':14s}sub-bass {sub}  dB re 1 kHz")
        limiter = report[block]["limiter"]
        print(f"limiter: avg {limiter['average_gain_db']:+.2f} dB, "
              f"min {limiter['min_gain_db']:+.2f} dB, "
              f"peak {limiter['peak_dbfs']:+.2f} dBFS, "
              f"crest {limiter['crest_db']:+.2f} dB, "
              f"clipped {limiter['clipped_samples']}")
        print(f"A-weighted: {limiter['a_weighted_in_dbfs']:+.2f} → "
              f"{limiter['a_weighted_out_dbfs']:+.2f} dBFS "
              f"({limiter['a_weighted_out_dbfs'] - limiter['a_weighted_in_dbfs']:+.2f} dB)")
    print("\npoints are dB relative to 1 kHz; bass = mean(100-300) - mean(1k-3k);")
    print("pres = mean(3k-6k) - mean(1k-3k).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cfg-dir", default="artifacts/eq-prototype")
    parser.add_argument("--wav", help="use this 48 kHz mono S16 file for limiter stats")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg_dir = Path(args.cfg_dir)
    if not (cfg_dir / "EQ_100.cfg").is_file():
        raise SystemExit(f"no EQ_100.cfg under {cfg_dir} (pull the vendor files first)")
    signal = read_wav(Path(args.wav)) if args.wav else pink_kick_music()
    label = str(Path(args.wav)) if args.wav else "synthetic pink+kick 10 s RMS -15.4 dBFS"
    report = run(cfg_dir, signal, label)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
