#!/usr/bin/env bash
# Generate a 48 kHz mono 16-bit WAV of the wake word with the macOS voice.
#
#   ./scripts/make_wake_wav.sh "你好小瑞" /tmp/wake.wav
#
# The device wire format is 48 kHz mono S16; say + afconvert is enough and it
# keeps the test independent of the rui TTS provider.
set -euo pipefail
TEXT="${1:-你好小瑞}"
OUT="${2:-/tmp/wake.wav}"
VOICE="${VOICE:-Tingting}"
TMP="/tmp/wake-$$.aiff"  # /tmp: the sandbox's own TMPDIR may be read-only for us
say -v "$VOICE" -o "$TMP" "$TEXT"
afconvert -f WAVE -d LEI16@48000 -c 1 "$TMP" "$OUT"
rm -f "$TMP"
echo "$OUT"
