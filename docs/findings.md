# 事实清单（带出处）

2026-10-24 通过只读 EchoMuse 上游源码（`/tmp/echomuse-inspect`，commit `87a3d21`）与 rui 源码（`/tmp/rui-inspect`）核实。
**行号会随上游演进失效，引用时以函数/常量名为准。**

## 一、设备侧（Dot 2 + EchoMuse 设备端 v2.18.1）

| 事实 | 出处（EchoMuse） |
|---|---|
| 三条 WSS：`/control`（JSON）、`/data`（二进制）、`/shell/{id}`（按需） | `docs/device-controller-interface.md` §The three planes |
| 发现：mDNS `_emcontroller._tcp.local`，或静态文件 `/data/local/etc/echomuse/controller.json` | `device/internal/discovery/static.go:16` |
| 静态文件格式 `{"endpoints":[{"host","port","tls_port"}],"mdns":false}`，每次重连重新读取 | `static.go` `staticControllerConfig` |
| 设备 pin 的 CA：`/data/local/etc/echomuse/ca.pem`；token：`/data/local/etc/echomuse/token` | `device/internal/client/tlscreds.go:27-28` |
| TLS ServerName 固定为 `echomuse-controller`（是身份标签，不解析） | `tlscreds.go:33` |
| 三个平面都带 `X-EM-Token` 头；持有 CA 的设备只走 wss | `tlscreds.go` `headerFor()` + 文档 §Link auth & TLS |
| 握手：设备先发 `register`（含 `capabilities`），controller 回 `ack` | 文档 §Registration / §`/control` |
| keepalive：controller 每 20 s ping，30 s 无 pong 断开 | 文档 §Keepalive |
| **上行麦克风：`/data` 帧 `0x01` + S16LE 单声道 16 kHz** | 文档 §`/data` — `0x01` |
| 连续流按 **80 ms** 切块发送（`vadOwwChunkBytes = 1280*2`） | `device/internal/client/data.go:120` |
| 连续流（`mic_start` 不带 `lock_mic`）不受 VAD 门控；受限轮次才发 `0x04`/`0x05` 哨兵 | `data.go` `!lockMic` 分支 + 文档 |
| **下行喇叭：`0x02` + S16LE 单声道 48 kHz**，`0x03` 为 EOS | `controller/em_controller.py:229 SPEAKER_RATE` |
| 设备原始采集是 **9 声道 S24_3LE 16 kHz**：ch0-6 麦克风，ch7/ch8 播放回采 | `device/internal/bindings/mic/pcm_microphone.go:42-46`、`rawtap_bench.go:15-16` |
| 原始 9 声道**不出设备**：设备内做完波束/AEC/AGC 后只送单声道 | `data.go` `beam.Process` → `aec.Process` → `sendFrame(mono)` |
| 设备自带唤醒可关：`owwOnDevice` = `off` / `shadow` / `on` | `device/internal/config/config.go` |
| `listen_state` = `stream` / `local` / `degraded`，由 controller 能力与配置决定 | 文档 §`listen_state` |

**结论：** controller 侧要实现的子集是 `register/ack` + `mic_start/mic_stop` + `/data` 的 `0x01/0x02/0x03` + ping/pong + TLS/token。
`0x04/0x05`（音乐）、`0x07`（私密聆听）、`0x08`（BLE）都不需要。

## 二、rui 侧

| 事实 | 出处 |
|---|---|
| 设备契约：`DeviceAdapter`（`probe/open/close/frames/play/stop_playback/set_volume/set_led`） | `src/house_ear/device/contract.py:140` |
| 帧结构：`AudioFrame(pcm, channels, sample_rate, reference_channels)`，参考通道跟在麦克风通道后面 | `contract.py:62-96` |
| `DeviceConfig.adapter` 是**普通字符串**，不是 enum → 外部 adapter 可直接配置 | `src/house_ear/config/schema.py:125` |
| 注册钩子：`registry.register(name, target)`，注释明确是给 downstream build 用的 | `src/house_ear/device/registry.py:48` |
| 音频面是子进程：`sys.executable -m house_ear.audio.process`，继承环境变量 | `src/house_ear/audio/client.py:407-410` |
| **无参考通道时 AEC 与 barge-in 被关闭**，单声道回落到 `block[:,0]` | `src/house_ear/audio/process.py:377`、`_frame_to_voice` |
| 播放前统一重采样到 `audio.playback.rate` 再调 `adapter.play(..., channels=1)` | `process.py:727/863/971/1294` |
| 默认 `playback.rate=16000`（LX06 适配）；**本项目要改成 48000** 才能对上设备下行格式 | `schema.py` `PlaybackConfig` |
| 默认 `capture` 是 8ch（6 mic + 2 ref），支持 1ch/0ref 的配置 | `schema.py` `CaptureConfig` |
| 两个进程都要能 import 到 adapter：父进程 + 音频面子进程 | 同上，`registry.build_adapters` 在 `web/api/app.py:1152`、`audio/process.py:365` 调用 |
| 现有 LX06 回连端口 4412（inbound）；本 adapter 自己当服务端，端口可另定 | `docs/xiaoai-protocol.md` |

## 三、凭据与素材

| 东西 | 位置 | 说明 |
|---|---|---|
| controller 服务端证书 `server.pem` / `server.key` / `ca.pem` | `01_Code/services/echomuse-controller/data/tls/`（key 已是 600） | 搬到本项目 `secrets/`，gitignore；**丢失 = 设备要重新配网刷机** |
| 设备 token | 旧 controller SQLite `devices.token`，同时设备端存在 `/data/local/etc/echomuse/token` | 只在运行时比对，不写进仓库 |
| rui 现有 provider 凭据 | `~/.hermes/.env` 里已有 `XFYUN_*`、`SILICONFLOW_API_KEY`、`DEEPSEEK_API_KEY` | 与 rui 的 `xfyun_tts` / `siliconflow` / `openai_compat` 对得上，Phase 1 可直接用 |

## 四、待核实（Phase 0 用实测填补）

- [ ] 设备当前 `listen_state` 与 `owwOnDevice` 实际值（controller 下发的配置是什么）。
- [ ] 80 ms 上行块在 Mac/NAS 上的真实到达抖动（旧链路日志里有过 `RTT: 21 idle excursions in 11m, worst 5372ms`）。
- [ ] `mic_start(lock_mic:false)` 与 `mic_start(lock_mic:true)` 在 rui 侧的取舍：连续流给 rui 自己做 KWS/VAD，还是用设备的 VAD 门控。
- [ ] 单声道（设备侧 AEC 已处理）下播放中唤醒的误唤醒率——决定 Phase 3 是否必须上 9 声道。
- [ ] 设备对 `ack.features` 的依赖：不宣告任何 feature 是否完全等同旧行为。
