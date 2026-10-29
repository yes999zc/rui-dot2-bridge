<div align="center">

# rui-dot2-bridge

**把 Echo Dot 2（RS03QR）接到 [rui](https://github.com/yes999zc/rui) 的语音卫星桥。**
不改 rui 核心：一个纯外置插件，设备侧 AEC 归设备，服务端只做决策。

[![License: MIT](https://img.shields.io/github/license/yes999zc/rui-dot2-bridge?style=flat-square)](LICENSE)
[![Release](https://img.shields.io/github/v/release/yes999zc/rui-dot2-bridge?style=flat-square)](https://github.com/yes999zc/rui-dot2-bridge/releases)

</div>

Latest release: https://github.com/yes999zc/rui-dot2-bridge/releases

---

# 中文

## 这是什么？给谁用？

Echo Dot 2 保留它自己的麦克风阵列、硬件 AEC 和喇叭；唤醒、对话、播放都由你的 rui 服务决定。这个仓库提供一个遵循 `rui-device/v1`（Socket & Plug v1）契约的**外置插件**：挂到核心的 `/opt/rui-plugins` 即可，**不需要造下游镜像**。

适合已经有一台（或几台）跑着 rui 的机器、想把闲置 Echo Dot 2 变成语音卫星的家庭：不需要改核心、不需要自己维护一个 fork。

```text
rui 起一个音频平面 → 插件连上设备（wss 控制通道 + 双向音频）→ 说「你好小瑞」→ rui 决定这一轮 → 插件播报/报灯/停播
```

## 架构

![architecture](docs/assets/architecture.png)

源文件：[`docs/assets/architecture.mmd`](docs/assets/architecture.mmd)；导出物 [`architecture.svg`](docs/assets/architecture.svg) / [`architecture.png`](docs/assets/architecture.png)。桥仓没有独立界面，hero 即这张架构图。

| 层 | 在这个仓库里的东西 | 说明 |
|---|---|---|
| **rui 核心（不改）** | 会话/回合、供应商、唤醒仲裁、音频门限 | 用官方镜像，不补丁、不 fork |
| **本仓库（插件）** | `adapter`（`build()` 入口）、`device_link`（wss 控制通道）、`dsp`（EQ/限幅） | 插件清单声明 `rui_compat`，越界时核心会拒载 |
| **设备** | Echo Dot 2 / RS03QR：麦克风阵列、硬件 AEC、喇叭、LED | 设备侧音频参数通过控制通道下发 |

## 特性

- **不改核心**：插件通过 `rui-device/v1` 契约接入，核心镜像保持原样；本仓与核心各自独立发布，`rui_compat` 声明可加载的窗口。
- **双向音频**：上行麦克风、下行播放分开走；播放/停止/音量/指示灯都是独立命令。
- **播放期门控**：`DOT2_PLAYBACK_GATE_MS` 控制「播放时静音上行」的时长（`0` = 交给设备 AEC，配合服务端中文 KWS 时可实现播放中打断）。
- **EQ 与限幅**：`dsp.py` 按房间响度目标做低切与高频搁架，并保证不削波（目标值可用 `/loudness` 复核）。
- **可观测**：`plug-test` 跑契约自测；设备状态、重连、丢包都进 rui 的日志与轨迹。

### 物理按键

设备上报的是 evdev 键码（`clickType`），不是键名；静音状态在同一条消息的 `muted`
字段里。插件把按键映射成 `drain_signals()` 事件交给 rui：

| `clickType` | 物理键 | 事件 `kind` | `text` |
|---|---|---|---|
| 113 | 静音 | `mute` / `unmute`（按 `muted`） | `muted` / `unmuted` |
| 114 | 音量 − | `volume_down` | 观测到的 `muted` |
| 115 | 音量 + | `volume_up` | 观测到的 `muted` |
| 138 | 动作键（圆点） | `action` | 观测到的 `muted` |

- 只在按下（`down: true`）时发一条事件，释放不重复触发。
- 未识别的 `clickType` 每个进程只记一条 `INFO` 日志并忽略，绝不猜成动作键。
- `muted` 同时写进适配器 `/health` 的 `muted`（每设备），与部署期的 `DOT2_MUTE` 无关：后者是音量上限，改它要重启；前者是设备自己的静音键，按下即生效。
- 音量键只上报（明确带 `muted`），不改配置、不写回设备。

## 快速开始

```bash
git clone https://github.com/yes999zc/rui-dot2-bridge.git && cd rui-dot2-bridge
mkdir -p /vol1/rui/plugins && cp -r src/rui_dot2_bridge /vol1/rui/plugins/   # 宿主插件目录
IMG=ghcr.io/yes999zc/rui:latest
docker run --rm -v /vol1/rui/plugins:/opt/rui-plugins:ro -e PYTHONPATH=/opt/rui-plugins \
  --entrypoint rui "$IMG" plug-test /opt/rui-plugins/rui_dot2_bridge \
  --adapter rui_dot2_bridge.adapter:build   # 契约自测（directory 必填）
```

生产用 Compose：核心镜像 + `-v ./plugins:/opt/rui-plugins:ro` + `PYTHONPATH=/opt/rui-plugins` + `RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build`，再按设备设 `DOT2_HOST` / `DOT2_LISTEN_PORT` / `DOT2_EM_TOKEN`（模板见 `deploy/`）。**不需要造下游镜像**。

## 新设备接入：一条命令，AI 可代劳

已解锁 + 已刷 EchoMuse 的 Dot 2，一条命令指到本部署：改的是设备 `/data/local/etc/echomuse/` 下的
`controller.json` / `ca.pem` / `token`（备份 → 写入 → 逐字节读回校验 → 重启设备 server → 等 rui 报 connected）。
主机、端口、令牌、序列号全是参数，**仓库里没有任何真实部署信息**。

```bash
python scripts/onboard_device.py --device-id dot2-b --rui http://<your-rui>:8080 \
  --rui-token-file ~/.rui/console-token.txt --secrets-dir ~/rui/dot2-secrets   # 先加 --dry-run 看计划
```

要填哪些参数、两种模式（只改地址 / 换部署）、回滚与常见错误见 [docs/onboard-device.md](docs/onboard-device.md)。

## 让 AI 帮你装

把下面这段贴给任意 AI 助手：

```text
目标：用 Docker 跑起 rui + 本插件（adapter id = dot2），核心镜像 + 外置插件目录，不造下游镜像。
前置：docker compose version 通过；8080 空闲；5 GB 磁盘。
1) git clone https://github.com/yes999zc/rui.git && cd rui/examples/two-speakers   # 本示例含两台音箱的完整配置
2) git clone https://github.com/yes999zc/rui-dot2-bridge.git && git clone https://github.com/yes999zc/rui-xiaoai-bridge.git
3) mkdir -p plugins && cp -r rui-dot2-bridge/src/rui_dot2_bridge plugins/   # 需要两台都接时，把另一个桥的 src 也 cp 进来
4) 写 ./config/config.yaml（web.auth_token + devices[]，含 dot2 的 host/listen_port）
5) TLS（dot2 的 tls_port 开启时必需）：把原 controller 证书放到 ./secrets/（server.pem + server.key）；没有就运行 `bash ../../scripts/make_dev_tls.sh` 自签。自签只保证平面能起，真机是否接受取决于设备信任的 CA
6) export DOT2_EM_TOKEN=<设备令牌>; docker compose up -d
7) 验收：/health 的 planes 里 dot2 为 running、devices 正确、device_errors=0
失败看：docker compose logs --tail=80 rui 的 config error / adapter / TLS；回滚：down 后换回上一版 image tag；TLS 失败先移走 ./secrets/ 排查
不需要自己编译 ffmpeg（镜像已内置）
```

## 兼容性与前置条件

`rui_compat` 在插件清单里声明本插件支持的 rui 版本窗口，越界时核心会直接拒载；**当前窗口以最新 release 的说明为准**（见页首的 Latest release）。

- 安装：核心镜像 + 外置插件目录（`/vol1/rui/plugins/rui_dot2_bridge`），挂载 `/opt/rui-plugins` 并设 `PYTHONPATH` + `RUI_ADAPTER_PLUGINS`；**不需要造下游镜像**。Python **3.11+**（核心镜像已内置）。
- **设备前置条件**（第三方设备，与厂商**无官方关联**）：
  - Echo Dot 2（RS03QR），固件需已具备设备本地接口并能与 rui 主机互通；
  - 设备与本服务在**同一局域网**；`DOT2_HOST` / 端口 / 设备令牌由你提供；
  - 本项目只提供软件侧的桥接层；设备固件、网络与令牌的准备由你按厂商与设备的既有能力完成。
- 中文唤醒词：走 rui 服务端 KWS（中文）；设备端 OWW 目前只用于英文模型，`DOT2_OWW_ON_DEVICE=off` 时完全关闭。
- 播放音乐需要 rui 侧配置音乐源（如 Navidrome）。
- 第三方设备桥请见 [rui-plugin-template](https://github.com/yes999zc/rui-plugin-template) 与 [docs/bridge-developer](https://github.com/yes999zc/rui/tree/main/docs/bridge-developer/)。

## 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| 插件没被加载 | `rui_compat` 越界或 `plugin.yaml` 不在模块目录 | 看 rui 启动日志里的 manifest 报错；确认 rui 版本在窗口内 |
| 设备连不上 | `DOT2_HOST` / 端口 / 令牌错，或设备不在同一网段 | 先 `rui plug-test`，再看 `device_link` 的重连日志 |
| 播放时喊不醒 | `DOT2_PLAYBACK_GATE_MS` 让上行被静音 | 调小或设 `0`（由设备 AEC 负责），再用中文 KWS 验证打断 |
| 加第二台设备后第一台失效 | 旧版 `DOT2_CONTROL_PORT` / `DOT2_LOUDNESS_FILE` 只能全局配置 | 5 项都按设备取：在 `devices[].params` 里给每台写 `control_port`（不同端口或 `0`）、`loudness_file`、`eq_dir`、`secrets_dir`、`oww_threshold`；env 仍作单台 fallback |
| 声音发闷/发尖 | EQ 目标与房间不匹配 | 重跑 `tools/eq_report.py` 拿测量值，再调 `DOT2_LOUDNESS_*` / `DOT2_EQ_DIR` |
| 中文唤醒词没反应 | 打开了设备端 OWW 但只有英文模型 | 设 `DOT2_OWW_ON_DEVICE=off`，用服务端中文 KWS |

## 已知限制

- 设备端 OWW 只验证过英文模型；中文唤醒词依赖 rui 服务端 KWS。
- EQ 只有测量结论，**没有**人工耳测结论；不同房间仍需现场调。
- 播放与拾音的共存依赖设备 AEC；设备固件不同，打断延迟可能在数百毫秒量级。
- 多台 Dot2 并发、音量与指示灯的系统性验证未做。

## 致谢

契约 `rui-device/v1` 与 `plug-test` 工具来自 [rui](https://github.com/yes999zc/rui)；音频参数化思路参考了公开的设备音频调试实践（见 `docs/aec-*.md`）。

## License

[MIT](LICENSE)。

---

# English

## What this is, and who it is for

The Echo Dot 2 keeps its own microphone array, hardware AEC and speaker; wakes, dialogue and playback are decided by your rui server. This repository ships one **external plug** implementing the `rui-device/v1` (Socket & Plug v1) contract: mount it at the core's `/opt/rui-plugins`; **no downstream image is needed**.

It is for households that already run rui on a machine and want to turn idle Echo Dot 2 units into voice satellites — no core changes and no fork to maintain.

```text
rui starts an audio plane → the plug links to the device (wss control + duplex audio) → "Hey Rui" → rui decides the turn → the plug speaks / lights / stops
```

## Architecture

![architecture](docs/assets/architecture.png)

Source: [`docs/assets/architecture.mmd`](docs/assets/architecture.mmd); exports [`architecture.svg`](docs/assets/architecture.svg) / [`architecture.png`](docs/assets/architecture.png). A bridge has no UI of its own, so this diagram is the hero.

| Layer | What lives here | Notes |
|---|---|---|
| **rui core (unchanged)** | Sessions/turns, providers, wake arbitration, audio gate | Official image, no patches, no fork |
| **This repo (plug)** | `adapter` (`build()` entry), `device_link` (wss control), `dsp` (EQ/limiter) | The manifest declares `rui_compat`; out-of-range versions are refused by the loader |
| **Device** | Echo Dot 2 / RS03QR: mic array, hardware AEC, speaker, LED | Device audio parameters are pushed over the control channel |

## Features

- **No core changes**: the plug speaks `rui-device/v1` and the core image stays untouched; this repo and the core are released independently, and `rui_compat` declares the window that can load it.
- **Duplex audio**: microphone uplink and playback downlink are separate; play/stop/volume/LED are individual commands.
- **Playback gate**: `DOT2_PLAYBACK_GATE_MS` sets how long the uplink is muted while playing (`0` = leave it to the device AEC, which enables barge-in together with server-side Chinese KWS).
- **EQ and limiting**: `dsp.py` applies a low cut and high shelf toward the room's loudness target without clipping (verify with `/loudness`).
- **Observability**: `plug-test` runs the contract suite; device state, reconnects and drops show up in rui logs and traces.

### Physical buttons

The device reports evdev key codes (`clickType`), not key names; the mute state
travels in the same frame's `muted` field. The plug maps keys to
`drain_signals()` events for rui:

| `clickType` | Physical key | Event `kind` | `text` |
|---|---|---|---|
| 113 | mute | `mute` / `unmute` (from `muted`) | `muted` / `unmuted` |
| 114 | volume down | `volume_down` | observed `muted` |
| 115 | volume up | `volume_up` | observed `muted` |
| 138 | action (dot) | `action` | observed `muted` |

- One event per press (`down: true`); the release never double-fires.
- An unknown `clickType` is logged once per process at `INFO` and ignored; it is never guessed into an action.
- `muted` also appears in the adapter's per-device `/health` as `muted`. This is not `DOT2_MUTE` (a deploy-time volume ceiling that needs a restart): the key is device-sovereign and takes effect immediately.
- Volume keys are report-only (with the observed `muted`); they do not change config or write back to the device.

## Quickstart

```bash
git clone https://github.com/yes999zc/rui-dot2-bridge.git && cd rui-dot2-bridge
mkdir -p /vol1/rui/plugins && cp -r src/rui_dot2_bridge /vol1/rui/plugins/   # host plug dir
IMG=ghcr.io/yes999zc/rui:latest
docker run --rm -v /vol1/rui/plugins:/opt/rui-plugins:ro -e PYTHONPATH=/opt/rui-plugins \
  --entrypoint rui "$IMG" plug-test /opt/rui-plugins/rui_dot2_bridge \
  --adapter rui_dot2_bridge.adapter:build   # contract self-test (directory required)
```

In production use Compose: core image + `-v ./plugins:/opt/rui-plugins:ro` + `PYTHONPATH=/opt/rui-plugins` + `RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build`, then set `DOT2_HOST` / `DOT2_LISTEN_PORT` / `DOT2_EM_TOKEN` per device (templates under `deploy/`). **No downstream image is needed**.

## Onboarding a new device: one command, an AI can do it

For an unlocked, EchoMuse-flashed Dot 2, one command rewrites the device's
`/data/local/etc/echomuse/{controller.json,ca.pem,token}` (backup → write → byte-for-byte
read-back → restart the device server → wait until rui reports it connected).
Every host, port, token and serial is a flag; no real deployment details live in this repo.

```bash
python scripts/onboard_device.py --device-id dot2-b --rui http://<your-rui>:8080 \
  --rui-token-file ~/.rui/console-token.txt --secrets-dir ~/rui/dot2-secrets   # add --dry-run to preview
```

See [docs/onboard-device.md](docs/onboard-device.md) for the parameter list, the two modes
(repoint only vs. re-provision), rollback and troubleshooting.

## Let an AI install it

Paste this:

```text
Goal: run rui + this plug (adapter id = dot2) with Docker, using the core image plus an external plug directory (no downstream image).
Preflight: docker compose version; port 8080 free; 5 GB disk.
1) git clone https://github.com/yes999zc/rui.git && cd rui/examples/two-speakers   # complete two-speaker example
2) git clone https://github.com/yes999zc/rui-dot2-bridge.git && git clone https://github.com/yes999zc/rui-xiaoai-bridge.git
3) mkdir -p plugins && cp -r rui-dot2-bridge/src/rui_dot2_bridge plugins/   # copy the other bridge's src too if you run both
4) write ./config/config.yaml (web.auth_token + devices[] with host/listen_port for dot2)
5) TLS (required when dot2's tls_port is on): put the original controller cert into ./secrets/ (server.pem + server.key); if you do not have it, run `bash ../../scripts/make_dev_tls.sh`. Self-signed only proves the plane starts; the device's trusted CA decides acceptance
6) export DOT2_EM_TOKEN=<device token>; docker compose up -d
7) verify: /health.planes shows dot2 running, devices correct, device_errors=0
On failure: docker compose logs --tail=80 rui (config error / adapter / TLS); rollback: down and switch the image tag back; for TLS failures move ./secrets/ away first
No ffmpeg build needed (the image ships it)
```

## Compatibility and prerequisites

`rui_compat` in the plug manifest declares the rui window this plug loads into, and the loader refuses anything out of range; **the current window is whatever the latest release says** (see the Latest release line at the top).

- Install: core image + external plug directory (`/vol1/rui/plugins/rui_dot2_bridge`), mounted at `/opt/rui-plugins` with `PYTHONPATH` + `RUI_ADAPTER_PLUGINS`; **no downstream image is needed**. Python **3.11+** (bundled in the core image).
- **Device prerequisites** (third-party hardware, **not affiliated with any vendor**):
  - Echo Dot 2 (RS03QR) whose firmware already exposes the device's local interface and can reach the rui host;
  - device and server on the **same LAN**; `DOT2_HOST`, ports and the device token are yours to provide;
  - this project only provides the software bridge; preparing the device firmware, network and token is up to you, using the device's existing capabilities.
- Chinese wake words come from rui's server-side KWS; the on-device OWW path only ships an English model and is off by default (`DOT2_OWW_ON_DEVICE=off`).
- Music needs a source configured on the rui side (e.g. Navidrome).
- Writing a third-party device bridge? See [rui-plugin-template](https://github.com/yes999zc/rui-plugin-template) and [docs/bridge-developer](https://github.com/yes999zc/rui/tree/main/docs/bridge-developer/).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Plug never loads | `rui_compat` out of range, or `plugin.yaml` not next to the module | Check the manifest error in rui's startup log; confirm the rui version is inside the window |
| Device will not connect | Wrong `DOT2_HOST` / port / token, or a different subnet | Run `rui plug-test` first, then read `device_link` reconnect logs |
| It cannot hear you while playing | `DOT2_PLAYBACK_GATE_MS` mutes the uplink during playback | Lower it or set `0` (device AEC takes over) and re-test barge-in with Chinese KWS |
| A second device breaks the first | The old `DOT2_CONTROL_PORT` / `DOT2_LOUDNESS_FILE` were host-global | All five knobs resolve per device now: set `control_port` (a distinct port or `0`), `loudness_file`, `eq_dir`, `secrets_dir` and `oww_threshold` under each `devices[].params`; env stays as a single-device fallback |
| Sound is dull or harsh | EQ target does not match the room | Re-run `tools/eq_report.py`, then tune `DOT2_LOUDNESS_*` / `DOT2_EQ_DIR` |
| Chinese wake word ignored | On-device OWW enabled with an English-only model | Set `DOT2_OWW_ON_DEVICE=off` and rely on server-side KWS |

## Known limitations

- The on-device OWW path is only validated with an English model; Chinese wake words rely on server-side KWS.
- EQ has measurements only — **no** listening test; each room still needs on-site tuning.
- Playback and capture coexistence depends on the device AEC; barge-in latency can be in the hundreds of milliseconds depending on firmware.
- Concurrent Dot2 units, volume and LED behaviour have not been systematically verified.

## Credits

The `rui-device/v1` contract and the `plug-test` tool come from [rui](https://github.com/yes999zc/rui); the audio parametrisation follows public device-audio debugging practice (see `docs/aec-*.md`).

## License

[MIT](LICENSE).
