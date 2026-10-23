#!/usr/bin/env bash
# Start rui (house-ear v1, frozen checkout) on this Mac for dot2-adapter development.
#
#   ./scripts/dev_rui.sh                 # console on http://127.0.0.1:8091
#   ./scripts/dev_rui.sh --no-audio      # control plane only (no audio child process)
#
# It runs the READ-ONLY checkout's own console entry point:
#     .venv/bin/python -m house_ear serve
# with HOUSE_EAR_CONFIG pointing at dev/rui/config.yaml. Nothing under
# 01_Code/projects/house-ear is written by this script or by rui's dev mode.
#
# Environment knobs (all optional):
#   RUI_ADAPTER_DIR  where out-of-tree adapter modules live; prepended to PYTHONPATH.
#                    Default: dev/rui/adapters (created on first run, empty).
#   RUI_ADAPTER_SPEC register adapters this frozen build does not ship. Format
#                    "name=module:function[,name2=module2:function2]" — the same
#                    (name, "module:function") pair as house_ear.device.registry.
#                    Needed for dot2: the build's registry only knows xiaoai/mock.
#                    Example: RUI_ADAPTER_SPEC='dot2=dot2_adapter:build'
#   RUI_PY           python interpreter (default: house-ear/.venv/bin/python)
#   RUI_CONFIG       config path (default: dev/rui/config.yaml)
#   HERMES_ENV       secret source (default: ~/.hermes/.env, read by dev/rui/env)
#
# Logs: dev/rui/logs/rui.log (append; the console also writes data_dir/service.log).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOUSE_EAR="${HOUSE_EAR:-$(cd -- "$ROOT/../house-ear" && pwd)}"
PY="${RUI_PY:-$HOUSE_EAR/.venv/bin/python}"
CONFIG="${RUI_CONFIG:-$ROOT/dev/rui/config.yaml}"
LOG_DIR="$ROOT/dev/rui/logs"
LOG="$LOG_DIR/rui.log"
DATA_DIR="$ROOT/dev/rui/data"
export RUI_ADAPTER_DIR="${RUI_ADAPTER_DIR:-$ROOT/dev/rui/adapters}"
# The rui audio plane is a CHILD process (`python -m house_ear.audio.process`):
# an in-memory registry.register() in this process never reaches it. src/sitecustomize.py
# is imported by both interpreters, so that is what actually registers dot2.


die() { echo "dev_rui: $*" >&2; exit 1; }

# --- preflight ------------------------------------------------------------
[ -x "$PY" ] || die "python not found/executable: $PY (is the house-ear venv present?)"
[ -f "$CONFIG" ] || die "config not found: $CONFIG"
[ -d "$HOUSE_EAR/src/house_ear" ] || die "house-ear checkout not found under: $HOUSE_EAR"

mkdir -p "$LOG_DIR" "$DATA_DIR" "$RUI_ADAPTER_DIR"

# --- credentials + config -------------------------------------------------
# dev/rui/env maps ~/.hermes/.env names onto the variables config.yaml names.
# It only warns about missing values (rui degrades those providers to "not
# warmed"); it never prints a value.
# shellcheck source=../dev/rui/env
. "$ROOT/dev/rui/env"
export HOUSE_EAR_CONFIG="$CONFIG"
# AF_UNIX socket for the plane <-> control IPC. The default under ~/.local is
# outside the workspace sandbox here (EPERM at bind), so keep it in dev/.
# resolve_socket_path() hashes anything over 96 bytes, so this stays short.
export HOUSE_EAR_AUDIO_SOCK="$ROOT/dev/rui/audio.sock"
export PYTHONUNBUFFERED=1

# --- out-of-tree adapters (dot2), before house_ear imports anything --------
# PYTHONPATH is the reserved slot: an adapter module placed in $RUI_ADAPTER_DIR is
# importable, but the frozen registry still has to be told about the name.
export PYTHONPATH="$ROOT/src:$RUI_ADAPTER_DIR${PYTHONPATH:+:$PYTHONPATH}"

# --- validate config now (same loader `serve` uses) and read the bound port -
PORT="$("$PY" - "$CONFIG" <<'PY'
import sys
from house_ear.config import ConfigError, load_config
try:
    print(load_config(sys.argv[1]).web.port)
except ConfigError as exc:
    print(f"dev_rui: config validation failed:\n{exc}", file=sys.stderr)
    raise SystemExit(2)
PY
)" || exit $?

# A second rui on the same port is a terse uvicorn traceback; say it plainly instead.
if "$PY" - "$PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.4)
raise SystemExit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
then
    die "port $PORT already has a listener; stop the other rui/uvicorn first"
fi

# --- log ----------------------------------------------------------------
{
    echo
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') dev_rui start ==="
    echo "config=$CONFIG"
    echo "python=$PY"
    echo "port=$PORT"
    echo "PYTHONPATH=$PYTHONPATH"
    echo "adapters=${RUI_ADAPTER_SPEC:-<none registered>}"
} >> "$LOG"
# Keep the terminal readable while still appending every line to the log.
# A pipeline, not `exec > >(tee ...)`: process substitution needs /dev/fd, which the
# DSH file sandbox (and some restricted shells) refuse with "Operation not permitted".
# pipefail (set at the top) makes the pipeline's status the python process's, not tee's.

echo "rui: config=$CONFIG"
echo "rui: log=$LOG"
echo "rui: console=http://127.0.0.1:${PORT}  (health: /health)"

# --- run ------------------------------------------------------------------
if [ -n "${RUI_ADAPTER_SPEC:-}" ]; then
    # Register first, then run the stock CLI in the same process: registry state is
    # module-level, so it has to be the process that builds the adapters.
    "$PY" - "$@" <<'PY' 2>&1 | tee -a "$LOG"
import os
import sys

from house_ear.device import registry

spec = os.environ.get("RUI_ADAPTER_SPEC", "")
for item in spec.split(","):
    item = item.strip()
    if not item:
        continue
    name, sep, target = item.partition("=")
    if not sep or not name or ":" not in target:
        raise SystemExit(f"RUI_ADAPTER_SPEC entry must be name=module:function, got {item!r}")
    registry.register(name, target)
    print(f"adapter registered: {name} -> {target}")

from house_ear.__main__ import main

raise SystemExit(main(sys.argv[1:]))
PY
else
    "$PY" -m house_ear serve "$@" 2>&1 | tee -a "$LOG"
fi
