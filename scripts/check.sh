#!/usr/bin/env bash
# One command for "is this checkout still healthy": compile, unit tests, config, lint.
#
#   ./scripts/check.sh            # tests + config validation
#   ./scripts/check.sh --nodevice # same, and skip the live rui/adb probes
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RUI_PY:-$ROOT/../house-ear/.venv/bin/python}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

echo "== syntax"
"$PY" - <<'PY'
import pathlib
for path in list(pathlib.Path("src").rglob("*.py")) + list(pathlib.Path("tests").rglob("*.py")) + \
        list(pathlib.Path("scripts").glob("*.py")) + list(pathlib.Path("tools").glob("*.py")):
    compile(path.read_text(), str(path), "exec")
print("   ok")
PY

echo "== unit tests"
"$PY" -m unittest discover -s tests -q

echo "== config"
"$PY" - <<'PY'
import os, sys
sys.path.insert(0, str(__import__("pathlib").Path("../house-ear/src").resolve()))
from rui.config import load_config
cfg = load_config("dev/rui/config.yaml")
dev = next((d for d in cfg.devices if d.adapter == "dot2"), None)
cap = float(os.environ.get("DOT2_VOLUME_CAP", "100"))
assert dev is not None, "no dot2 device in the config"
assert dev.volume <= cap, f"config volume {dev.volume}% above the {cap:.0f}% ceiling"
assert cfg.audio.playback.rate == 48000, cfg.audio.playback.rate
print("   ok:", dev.host, dev.listen_port, f"{dev.volume}%")
PY

echo "== lint"
if "$PY" -c "import ruff" >/dev/null 2>&1; then
  "$PY" -m ruff check src tests tools scripts 2>&1 | tail -5 || true
else
  echo "   ruff not installed in this venv, skipped"
fi

echo "== doctor"
"$PY" scripts/doctor.py | tail -6
