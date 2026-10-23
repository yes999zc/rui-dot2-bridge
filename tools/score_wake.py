"""Score a WAV with rui's own KWS model, offline (P2 helper).

    python tools/score_wake.py --wav /tmp/dot2-live.wav \
        --model-dir .../sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20 \
        --keywords dev/rui/keywords.txt [--highpass-hz 100]

Prints every hit with its time, and the same after a high-pass, so the two
numbers can be compared without restarting the audio plane. This is the
measurement behind docs/p2-wake.md; it does not decide anything on its own,
because a synthetic voice is not a person.
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: need mono 16-bit, got {wav.getnchannels()}ch/{wav.getsampwidth() * 8}bit")
        rate = wav.getframerate()
        data = wav.readframes(wav.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float64), rate


def highpass(x: np.ndarray, rate: int, hz: float) -> np.ndarray:
    """2nd-order RBJ high-pass, mono float."""
    w0 = 2 * math.pi * hz / rate
    alpha = math.sin(w0) / (2 * (1 / math.sqrt(2)))
    cos0 = math.cos(w0)
    b0 = (1 + cos0) / 2
    b1 = -(1 + cos0)
    b2 = (1 + cos0) / 2
    a0 = 1 + alpha
    a1 = -2 * cos0
    a2 = 1 - alpha
    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    y = np.zeros_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i, sample in enumerate(x):
        out = b[0] * sample + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        y[i] = out
        x2, x1 = x1, sample
        y2, y1 = y1, out
    return y


def score(x: np.ndarray, rate: int, model_dir: str, keywords: str) -> list[float]:
    sys.path.insert(0, str(Path("01_Code/projects/house-ear/src").resolve()))
    from house_ear.audio.kws import WakeWord

    spotter = WakeWord(model_dir=model_dir, keywords_file=keywords, rate=rate)
    hits: list[float] = []
    step = rate // 50
    pcm = np.clip(x, -32768, 32767).astype("<i2").tobytes()
    for start in range(0, len(pcm), step * 2):
        for hit in spotter.accept(pcm[start:start + step * 2], ts_ms=start / 2 / rate * 1000):
            hits.append(hit.ts_ms / 1000.0)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--highpass-hz", type=float, default=100.0)
    args = parser.parse_args()

    x, rate = read_wav(args.wav)
    print(f"wav: {len(x) / rate:.2f} s @ {rate} Hz, dc={x.mean():.0f}, rms={20 * math.log10(np.sqrt((x ** 2).mean()) / 32768):.1f} dBFS")
    raw_hits = score(x, rate, args.model_dir, args.keywords)
    print("hits (raw):", [round(t, 2) for t in raw_hits])
    filtered = highpass(x, rate, args.highpass_hz)
    hp_hits = score(filtered, rate, args.model_dir, args.keywords)
    print(f"hits (high-pass {args.highpass_hz:.0f} Hz):", [round(t, 2) for t in hp_hits])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
