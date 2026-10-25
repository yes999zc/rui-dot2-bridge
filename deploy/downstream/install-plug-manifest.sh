#!/bin/sh
# Install the dot2 plug manifest where rui's *catalog* looks for it.
#
# Why both copies exist:
#   * /opt/rui-plugins/rui_dot2_bridge/plugin.yaml (baked into the image) gates the
#     RUI_ADAPTER_PLUGINS hook, because rui's device loader looks for a manifest next
#     to the adapter module before it registers the env entry.
#   * <data_dir>/plugs/<dir>/plugin.yaml is what PlugCatalog (wizard, /api/v1/plugs)
#     and the audio plane's wake policy (_wake_policy -> resolve_device_plug) read.
#     If params.plug_id is set but this copy is missing, the plane FAILS CLOSED to RUI
#     KWS -- device_wake silently never becomes active.
#
# Usage (on the deploy host, from the repo dir):
#   sh deploy/downstream/install-plug-manifest.sh ~/rui/data
set -eu

data_dir="${1:?usage: install-plug-manifest.sh <data_dir>  (e.g. ~/rui/data)}"
src_dir="$(CDPATH= cd -- "$(dirname -- "$0")/../../src/rui_dot2_bridge" && pwd)"
plug_id="dot2-echomuse"
dst="$data_dir/plugs/$plug_id"

test -f "$src_dir/plugin.yaml" || { echo "missing $src_dir/plugin.yaml" >&2; exit 1; }
test -d "$src_dir/devices" || { echo "missing $src_dir/devices/" >&2; exit 1; }

mkdir -p "$dst/devices"
install -m 0644 "$src_dir/plugin.yaml" "$dst/plugin.yaml"
for f in "$src_dir"/devices/*.yaml; do
  install -m 0644 "$f" "$dst/devices/"
done
echo "installed manifest -> $dst"
ls -R "$dst"
