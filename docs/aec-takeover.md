# rui 接管 AEC 的可行性（B 类高权限设备：Dot 2）

- 日期：2026-10-24
- 结论：**可行**。属于"小改固件 + 零改 rui"的形态，收益大于代价。
- 但有一个必须处理的坑：**不能把 ch8 原样送给 rui**（详见 §3）。

## 1. 结论摘要

| 维度 | 结论 |
|---|---|
| 算法一致性 | 设备与 rui 用的是**同一个库**：speexdsp MDF（设备端 vendored 1.2.1；rui 走 `libspeexdsp`）。不存在"换算法"的风险。 |
| 参考源 | Dot 2 的采集帧 ch8 就是播放回采，**与麦克风同一 TDM 帧**，不需要延迟搜索。设备端已用它测到 20.3 dB ERLE。 |
| 需要的固件改动 | 一处：把上行从"单声道 post-AEC"改成"波束单声道（预 AEC）+ 去混叠参考"2 通道；ack 协商开关，旧 controller 仍收单声道。约 80–120 行（主要是复用现有 `dealias` 与 WriteFar 环）。 |
| 需要改 rui | **不需要**。adapter 用 `reference_channels=1` 交帧，rui 的 AEC 自动启用，控制台已有 `aec_erle_db` 指标。 |
| 带宽 | 上行 32 KB/s → **64 KB/s**（+0.26 Mbps），下行不变。 |
| 设备 CPU | **下降**：设备端 7 麦 AEC 是 6.04 ms / 32 ms 周期 ≈ 单核 19%，关掉后改为"参考抽取"（FIR 抽取，约 1–2%）。 |
| 回退 | 同一条 ack 协商双向切换，**不需要重刷**；旧 controller 仍走单声道 + 设备 AEC。 |
| 主要风险 | 16-bit 量化天花板（设备 AEC 在 S24 上做，rui 只能看到 S16）；必须实测 ERLE 上限。 |

## 2. 两侧现状（证据）

### 设备侧（EchoMuse 固件，只读上游源码）

- 采集：9 通道 `S24_3LE` 16 kHz，ch0–6 麦克风，ch8 播放回采（ch7/ch8 同源，AEC 用 ch8）。
- AEC：speexdsp MDF，vendored 1.2.1，`FrameSize=512`（32 ms @16 kHz），**7 个麦克风各自一个 echo state**。
- 参考路径两种：
  - **硬件回采（ch8）**：与麦克风同帧，`hwTailMs=64`；bench Dot 实测 16 ms 与 300 ms tail 效果相同（20.3 vs 20.4 dB），因为延迟已由"同帧"解决。
  - 软件 tap：在喇叭 ALSA 写入处取 48 kHz 立体声，下混 + 3:1 抽取到 16 kHz，需要 ring + `aecDelayMs` + 占用率调节器——这些**全部只为回答"远端在哪一刻"**。
- 7 麦 AEC 成本：6.04 ms / 32 ms 周期 ≈ 单核 19%（JOURNAL 2026-10-03）。
- AEC 可关：配置项 `aecEnabled`（默认 true）；关掉后波束输出直通。
- **ch8 的陷阱**：它是 48 kHz 输出"每 3 个取 1 个"塞进 16 kHz 采集的，**没有抗混叠滤波**。9/12/15/20 kHz 的稳态音会在 ch8 落到 7/4/1/4 kHz，而麦克风那边（codec 已滤波）没有。粉噪实测：raw ch8 只有 **1 dB** 消除，用输出重建参考后 **25.7 dB**（`dealias.go`，2026-10-18）。这就是"接管 AEC 时必须连参考重建一起搬"的原因。

### rui 侧（`audio/aec.py`）

- speexdsp MDF，`SPEEX_FRAME=128`，`filter_len` 默认 4096 taps（256 ms @16 kHz），可到 16384。
- 拓扑：`cancel="mix"`（默认，阵列混音一路；LX06 实测 25.2 dB）或 `per_channel`（6 路，20.9 dB，6× CPU）。
- 参考：`AudioFrame` 的尾部通道同帧到达，`process.py` 只取第一个参考通道（`ref = block[:, frame.mic_channels]`）。
- 门控：`ref_channels < 1` → AEC 与 barge-in 显式关闭并告警。
- 可观测：`aec_erle_db` 直接进控制台（"回声抑制 dB"）→ 能做 A/B。
- 关于"2.4 ms lead"：那是 LX06 回采通道相对麦克风的实测偏置，**代码里没有 shift 逻辑**；4096 taps 的滤波器本身吸收几毫秒的失配。dot2 的 ch8 与麦同帧，偏置应更小。

## 3. 四种做法与取舍

| 方案 | 上行 | 需要改固件 | rui 侧 | 评价 |
|---|---|---|---|---|
| A（现状） | 1ch mono（post-AEC） | 否 | AEC/barge 关闭 | 基线；P1/P2 用它跑通对话 |
| **B（推荐）** | 2ch：波束 mono（预 AEC）+ **去混叠** ch8 参考 | 是，1 处 | 零改动，1 个 filter | 等价 LX06 的 tier 2；带宽 64 KB/s |
| C（实验） | 8ch：7 麦 + 参考 | 是 | 零改动，7 个 filter | 波束可移到 rui；2 Mbps；rui 的 per-channel 拓扑实测反而更差（20.9 vs 25.2 dB），不优先 |
| D（取巧） | 1ch mono + adapter 合成参考 | 否 | 零改动 | 双 AEC 级联 + 延迟靠校准；只做对照实验，不做产品 |

**B 是唯一同时满足"零改 rui / 带宽可控 / 与设备现有算法同源"的选项。**

## 4. 方案 B 的实现拆解

### 4.1 固件（唯一改动点，fork EchoMuse 设备端）

1. **保持参考抽取链路常开**：即使 `aecEnabled=false`，也继续在喇叭 ALSA 写入处喂输出环（现有 `WriteFar` 路径），供 `dealias` 重建参考。只是不再跑 speex 滤波。
2. **复用 `dealias`**：按 48 kHz 输出环匹配 ch8 的相位，重建一路"低通抽取"的 16 kHz 参考；匹配失败时回退 raw ch8（上游已有该回退）。
3. **上行成帧**：当 controller 在 `ack.features` 里宣告 `dot2_hw_ref` 时，把 `[波束 mono | 去混叠参考]` 交织成 2ch，仍用 `0x01` 帧；否则保持 1ch（旧 controller 无感知）。
4. **关设备 AEC**：controller 通过现有 `config` 推送 `aecEnabled:false`（已有字段，无需新协议）。

> 改动集中在 `internal/client/data.go`（成帧）+ 复用 `internal/aec/{aec,dealias}.go` 的参考路径；**不要**动 beamformer/AGC/VAD。

### 4.2 adapter（本项目）

```python
# 握手后宣告 feature
ack = {"type": "ack", "features": ["dot2_hw_ref"]}
# 上行映射
AudioFrame(device_id=..., channels=2, sample_rate=16000,
           reference_channels=1, pcm=interleaved_mic_ref_s16)
# probe()
DeviceInfo(..., channels=2, mic_channels=1, ref_channels=1,
           capabilities={PLAYBACK, REFERENCE, LED, VOLUME, KEYS})
```

### 4.3 rui 配置（不改代码，只改 config.yaml）

```yaml
audio:
  capture:  {rate: 16000, channels: 2, mic_channels: 1, ref_channels: 1, bits: 16}
  voice:    {rate: 16000, frame_ms: 20}
  aec:      {enabled: true, filter_len: 4096, cancel: mix}
  playback: {rate: 48000, channels: 1}   # 设备下行格式
```

## 5. 必须先量的四件事（P3 的准入条件）

1. **对齐**：ch8 与麦克风在同一采集帧 → 理论 0 样本偏差。用 chirp 做交叉相关验证；若偏置 > 10 ms，adapter 内做整数样本平移（成本为零）。
2. **量化天花板**：设备 AEC 作用在 S24 上；上行协议只到 S16。设备自己记录语音 RMS 在 0.0001–0.0006 FS（16-bit 只有 3–20 LSB），靠 `micGainDb` 预抬升。**必须实测** rui 在 S16 上的 ERLE 上限；不够就调 `micGainDb` 留足余量，或退回设备 AEC。
3. **参考重建是否生效**：对比 raw ch8 与去混叠 ch8 在音乐下的 ERLE（上游数字：1 dB vs 25.7 dB）。若 rui 侧只拿到 raw ch8，就是踩这个坑。
4. **barge-in 误唤醒**：设备 AEC 关闭后，播放中的回声是否会让 rui 的 KWS 误触发；按 rui 的 barge 路径标定阈值。

**验收口径（建议）**：安静房间 ERLE ≥ 20 dB（设备 AEC 基线同量级）、播放中唤醒 20 次 ≥ 18 次、8 小时误唤醒 ≤ 2 次、打断静音 P50 ≤ 600 ms。

## 6. 收益与代价

**收益**
- AEC 参数（`filter_len`/`cancel`）在 rui 侧可调、ERLE 可观测、barge-in 走已验证拓扑；
- 设备省下 19% 单核（7 麦 AEC 关闭），换来参考抽取的 ~1–2%；
- dot2 作为 B 类样板，把"高权限设备可以把 AEC 交给 rui"这件事变成能力矩阵里的一行。

**代价**
- 固件从"用上游发布版"变成"维护一个 fork"（改动小、但要在上游演进时跟）；
- 上行 +32 KB/s；
- 设备自带唤醒（oww）在 AEC 关闭后播放中会退化——但我们本来就规定用 rui 的 KWS，设备唤醒设 `off`。

## 7. 给 rui「多类型音箱适配标准」的输入（只读建议，不改 rui）

按 rui `docs/remaining-work.md` §I 的 A/B 分类，B 类设备建议把"音频前端归属"拆成三个独立维度，而不是一个 AEC 布尔值：

| 维度 | 取值 | dot2 |
|---|---|---|
| `aec` | `device` / `rui` / `none` | `rui`（可切回 `device`） |
| `ref` | `loopback` / `tap` / `none` | `loopback`（ch8，需去混叠） |
| `align` | `frame_aligned` / `needs_delay` | `frame_aligned` |

降级链建议：`rui AEC → 设备 AEC → 无 AEC（仅 ASR，无 barge）`，每一级都要能在 control plane 上看到当前在哪一级——否则"回声没消掉"会被误判成模型问题。

## 8. 下一步

1. **P2 先做**：单声道下量化 barge-in 到底差多少（这是要不要动固件的唯一依据）。
2. 若 Gate 判定需要 → 在离线路由验证 fork：先用 `device/tools/capture_mics` 之类的方式抓 raw 9ch，离线跑 `dealias` + 一条 speex，确认 25 dB 量级可达，再上设备。
3. 设备侧只替换 `/data/local/bin/server` 二进制；旧二进制留档，`adb push` 可秒级回滚。
