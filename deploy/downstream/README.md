# Downstream image: rui + dot2 plug

Upstream rui (0.6.x) loads out-of-tree adapters through the manifest-gated
`RUI_ADAPTER_PLUGINS` hook (contract: `docs/socket-plug-v1.md` §4.4 in the rui repo).
This directory is the supported "official image + adapter layer" recipe: **no rui
source changes**, the core is the upstream image, only the plug is added.

## What is in the image

| Path | Contents |
|---|---|
| `/opt/rui-plugins/rui_dot2_bridge/` | our adapter package **and** its `plugin.yaml` + `devices/rs03qr.yaml` |
| `ENV PYTHONPATH=/opt/rui-plugins` | makes `rui_dot2_bridge.adapter` importable |
| `ENV RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build` | registers the adapter under the name `dot2` |

`plugin.yaml` must keep `trust: trusted` (or `builtin`): a manifest-less or
`third-party` entry is ignored, and built-in names cannot be overridden. The manifest
is what gates the hook, not the env var alone.

## Build

Build on the target architecture (the test host is x86_64; an arm64 Mac cannot
`--load` an amd64 image into its own docker without a registry):

```bash
# on the deploy host, from a checkout/sync of this repo
docker build -f deploy/downstream/Dockerfile -t rui-dot2:0.6.0 .
```

Upgrade = rebuild this layer on a new base tag; `docker compose up -d` again.

## Run

```bash
# 1. materials next to this file: config/, data/, dot2-secrets/ (0600), .env
# 2. compose override: RUI_IMAGE=rui-dot2:0.6.0 and RUI_UID/RUI_GID of the host user
docker compose -f docker-compose.yml up -d
```

The device entry in `config/config.yaml` (written by the wizard or by hand) must be:

```yaml
devices:
  - id: dot2
    adapter: dot2              # the name registered by RUI_ADAPTER_PLUGINS
    host: <lan-ip>         # display only; the device dials us
    room: 客厅
    volume: 60
    listen_port: 8870          # EchoMuse wss port; manifest default is the same
    token_env: DOT2_EM_TOKEN   # EchoMuse X-EM-Token, NOT rui's RUI_DEVICE_TOKEN
    params:
      plug_id: dot2-echomuse   # ties the device to the v1 manifest
```

### Install the manifest into the data dir (required for device_wake)

The adapter is gated twice by two different readers:

| Reader | Looks at | Consequence if missing |
|---|---|---|
| device loader (`RUI_ADAPTER_PLUGINS`) | `plugin.yaml` next to the adapter module (baked in at `/opt/rui-plugins/rui_dot2_bridge/`) | env entry ignored, adapter never registers |
| `PlugCatalog` / wizard / audio-plane `_wake_policy` | `<data_dir>/plugs/<dir>/plugin.yaml` (`./data/plugs/dot2-echomuse/`) | with `params.plug_id` set the plane **fails closed to RUI KWS**; device_wake never activates |

So after (re)deploying, copy the manifest into the data volume once:

```bash
sh deploy/downstream/install-plug-manifest.sh ~/rui/data
```

Keep those files in sync with the image when the plug version changes; the script is
idempotent and only writes `plugin.yaml` + `devices/*.yaml`.

Two-layer auth, per contract: `RUI_DEVICE_TOKEN` is rui's transport admission for
its own inbound protocol; `DOT2_EM_TOKEN` is the EchoMuse inner auth the adapter
validates itself. They are separate secrets with separate env names.

## Evidence to collect after deploy

- `GET /api/v1/plugs` lists `dot2-echomuse` (source `user`)
- `/health` has `adapter_plugins.registered` containing `dot2` and `warnings: 0`
- the log line `adapter plugin ... registered` (name `dot2`), never `ignored`
- audio plane binds `0.0.0.0:8870` and the Dot2 registers (`dot2 link: registered`)