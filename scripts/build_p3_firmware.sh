#!/usr/bin/env bash
# Build the P3 firmware (2-channel uplink) without touching the device.
#
#   ./scripts/build_p3_firmware.sh [workdir]
#
# Clones EchoMuse at the tag that introduced the de-alias reference (v2.19.0-ea.1),
# applies patches/0001-ref-uplink-*.patch, builds the NDK cross-compiler image and
# compiles device/build/server. It prints the sha256 and the path; installing is a
# separate, deliberate step (docs/p3-firmware-patch.md) because it reboots audio
# on the Dot.
#
# Requirements: docker (OrbStack is fine), git, network for the base image.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-/tmp/echomuse-p3-build}"
TAG="${EM_TAG:-v2.19.0-ea.1}"
PATCH="$(ls "$ROOT"/patches/0001-ref-uplink-*.patch | head -1)"

export PATH="/opt/homebrew/bin:$HOME/.orbstack/bin:$PATH"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.orbstack/run/docker.sock}"

[ -n "$PATCH" ] || { echo "no patch file under $ROOT/patches" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }

echo "== workdir $WORK (tag $TAG)"
rm -rf "$WORK"
git clone --depth 1 --branch "$TAG" https://github.com/wilbowes/EchoMuse.git "$WORK"
# The device build replaces GoTinyAlsa with the sibling checkout; it is a git
# submodule and an uninitialised one fails with "reading /GoTinyAlsa/go.mod".
git -C "$WORK" submodule update --init --depth 1 GoTinyAlsa

echo "== applying $PATCH"
git -C "$WORK" apply --index "$PATCH"
git -C "$WORK" diff --stat --cached

echo "== building the compiler image (first run pulls NDK + Go 1.24)"
if ! docker image inspect echomuse-compiler >/dev/null 2>&1; then
  docker build -t echomuse-compiler "$WORK/device/compiler"
fi

echo "== cross-compiling"
( cd "$WORK/device" && ./compile.sh )

BIN="$WORK/device/build/server"
[ -f "$BIN" ] || { echo "build/server missing" >&2; exit 1; }
echo "== artifact"
ls -l "$BIN"
shasum -a 256 "$BIN"
file "$BIN"
echo
echo "install (deliberate, reboots the audio pipeline on the Dot):"
echo "  adb push '$BIN' /data/local/bin/server_b.new"
echo "  adb shell su -c 'mv /data/local/bin/server_b.new /data/local/bin/server_b && chmod 755 /data/local/bin/server_b && ln -sf server_b /data/local/bin/server && kill \$(pidof server)'"
echo "rollback: ln -sf server_a /data/local/bin/server && kill \$(pidof server)"
