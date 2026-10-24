#!/usr/bin/env bash
# Package what the NAS needs, without any secret and without this Mac's dev state.
#
#   ./scripts/make_deploy_bundle.sh [outdir]
#
# The tarball contains the adapter package, the compose override, the firmware
# patch, the docs a deployment needs, and the preflight tools. It deliberately
# excludes secrets/, dev/ (local config + recordings), artifacts/ and .git.
# Copy it to the NAS, unpack, put certs in secrets/, run scripts/doctor.py.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/artifacts}"
STAMP="$(date +%Y%m%d-%H%M)"
NAME="rui-dot2-bridge-$STAMP"
WORK="$(mktemp -d /tmp/dot2-bundle.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$OUT" "$WORK/$NAME"
cp -R "$ROOT/src" "$ROOT/deploy" "$ROOT/patches" "$ROOT/scripts" "$ROOT/tools" "$ROOT/tests" \
      "$ROOT/pyproject.toml" "$ROOT/README.md" "$ROOT/ROADMAP.md" "$ROOT/CHANGELOG.md" \
      "$ROOT/plug.json" \
      "$WORK/$NAME/" 2>/dev/null || true
# Vendor EQ curves: not in git (artifacts/), but the deployment needs them.
if compgen -G "$ROOT/artifacts/eq-prototype/EQ_*.cfg" >/dev/null; then
  mkdir -p "$WORK/$NAME/eq"
  cp "$ROOT"/artifacts/eq-prototype/EQ_*.cfg "$WORK/$NAME/eq/"
fi
mkdir -p "$WORK/$NAME/docs"
for doc in deploy-nas.md handoff-morning.md p0-p1-report.md p2-runbook.md \
           aec-takeover.md aec-offline-result.md p3-firmware-patch.md adr-0001-adapter-as-controller.md; do
  [ -f "$ROOT/docs/$doc" ] && cp "$ROOT/docs/$doc" "$WORK/$NAME/docs/"
done
# Drop caches and anything that only makes sense on this Mac.
find "$WORK/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$WORK/$NAME" -name '*.pyc' -delete 2>/dev/null || true

TAR="$OUT/$NAME.tar.gz"
tar -C "$WORK" -czf "$TAR" "$NAME"
shasum -a 256 "$TAR" | tee "$TAR.sha256"
echo "bundle: $TAR ($(du -h "$TAR" | cut -f1))"
echo
echo "next (on the NAS):"
echo "  tar -xzf $(basename "$TAR") && cd $NAME"
echo "  mkdir -p secrets && chmod 700 secrets   # ca.pem server.pem server.key device_token, mode 600"
echo "  python scripts/doctor.py --config /path/to/rui/config/config.yaml --secrets secrets"
