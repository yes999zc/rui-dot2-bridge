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


## 2026-10-24 · 播放"卡"：EOS 空闲阈值太短导致中途换流

**现象**：短回复说到一半出现"咔"（如回答"2"时）。设备日志显示同一条回复被拆成两段流
（先 4 个 period ≈171 ms，再 21 个 period），第二段重新 prime。

**根因**：`EOS_IDLE_S = 0.4`。TTS 音频是突发到达的，句内只要断供超过 0.4 s，adapter 就发
`0x03` 结束流；下一块音频到来时设备重新开流并 prime（30–800 ms），听感即卡顿。

**修复**：`EOS_IDLE_S = 1.2`（设备缓冲约 300 ms，1.2 s 静音仍能在饿死前收流）。验证：同一短
回复现在是一段 61-period 流，`maxGap=101 ms`。

**经验**：设备端 `underruns=0` 不能证明"听感连续"——跨流重启的 prime 不在 underrun 计数里，
要看 `periods/recvSpan/maxGap` 与流的条数。


## 2026-10-24 白天 · 响度/播放层的四个 bug

1. **限幅器漏检负峰**：前瞻峰值用带符号样本求 max，遇到非对称波形（EQ 低频提升后常见）
   只看到正峰；负峰直接撞 −0.1 dBFS 硬削波 → "破音"。修复：窗口最大值取 `np.abs()`。
   回归测试：全负峰信号必须被限到 ceiling 以下。
2. **换流状态残留**：FIR overlap-add 的 tail、biquad 延迟线、限幅器 5 ms 前瞻线都跨 chunk
   保留状态（流内无缝），但跨流会把上一段末尾几毫秒混进新段开头 → "呢"后杂音。
   修复：每段播放开始 `LoudnessProcessor.reset_stream()`。
3. **回复末尾"嗒"**：处理链让信号延迟 ~16 ms，最后非零样本直接跳静音。修复：流结尾对
   部分帧做 10 ms 淡出再补零。不要"扣住最后一帧等淡出"——EOS 是 1.2 s 后触发的，
   扣住的碎片会在 1.2 s 空档后才播。
4. **音量回读两个 bug**：`capped_level()` 未传 cap（默认 30）导致 cap=100 时把 level 38
   误判为"到顶"并显示 255；且回读用目标值而非设备回报值，物理按键降音量不反映。
   修复：投影设备 `reported_level`；单测覆盖 cap=100 下 38/89/127。

## 2026-10-24 · 麦克风削波

设备默认 mic 预增益 24 dB，正常说话就顶到 1.0000（7 次里 6 次削波）。adapter 通过配置推送
`micGainDb=18` 后，语音段峰值 0.70–0.88、0/5 削波；设备 VAD 阈值也从 0.01585 自动降到 0.00794。
该值由 `DOT2_MIC_GAIN_DB` 控制，恢复默认用 24。


## 2026-10-24 · 播放"卡"（~400 ms 空档）：两段根因

详见 rui issue #1 的跟进评论。只读调研（`docs/stutter-rui-xiaoai.md`）把问题拆成两段：

1. **每轮回复开头的 flush 竞态**：rui 在首块音频前调用 `stop_playback()`
   （`house-ear audio/process.py:1108`）；dot2 adapter 的 `speaker_flush` 走 control WS、
   PCM 走 data WS，两条连接无序。设备先收 2×100 ms 块、才处理到 flush，丢弃并结束流，
   下一段重新 prime，服务端被反压 406 ms。证据：11/11 次空档都在第 2 块之后，
   406/407/417 ms；同秒设备日志 `speaker_flush` + `periods=4 recvSpan=5ms`。
   **修法（两次迭代后的正确版）**：flush 必须保留（它是设备 1.024 s prime 门的旁路），
   但 `DeviceLink.flush()` 在发完 `speaker_flush` 后，**在同一条 /control 连接上发一个带 id 的 ping
   并等它的 pong**——设备按序回 pong，因此 pong 到达即证明 flush 已被处理，首帧随后才发，
   跨 socket 无序问题消除（实测 `primeWait` 17–22 ms，而不是去掉 flush 后的 762–968 ms）。
2. **TTS 分片到达节奏**：每条回复先到 ~200 ms 音频，~407 ms 后其余部分成批到达；
   rui `Pacer` 预存上限 320 ms（部分路径 250 ms），不足以覆盖。
   **修法**：adapter 加 0.45 s 预缓冲（`DOT2_PREBUFFER_MS`，短回复 0.35 s 后强制开始）。
   注意第一版把阈值写成了 `_prebuffer*2` 字节（900 B，等于没生效），第二版改为
   `ms × 48000 × 2 / 1000` = 43200 B。

修复后：calls.log 无 400 ms 级块间空档；设备流 `underruns=0`、`maxGap≈700 ms`（EOS 尾部静音）。
代价：回复延迟 +约 0.45 s。EOS 空闲同时从 1.2 s 收到 0.7 s，减少结尾死等。
