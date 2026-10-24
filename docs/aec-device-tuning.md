# 设备端 AEC 调优：Echo Dot 2 + EchoMuse（设备接管 AEC 的参数与验收）

- 日期：2026-10-24
- 设备：Echo Dot 2 / `G090LF0964340RN6` / `<lan-ip>`，EchoMuse `20261024-1911-dev`（含 2ch 参考补丁，caps 有 `aec_hw_ref` + `ref_uplink`）
- 控制器：`<lan-ip>` 上的 rui 测试实例（`rui-test.service`），插头 `rui-dot2-bridge`，`ref_channels=0`、`micGainDb=18`、`aecEnabled=true`
- 配套文档：`docs/aec-parameter-model.md` 讲 `who/reference/constraints` 的抽象模型；**本文只回答设备端：为什么只有 ~10 dB、能调到多少、怎么验**。
- 立场：按 9 哥定的方向，**开放设备（Dot 2）目标态是 `who=device`**；模型文档把"开放类默认"写成 `who=controller`，本文把 `who=controller` 降为**兜底**（§5.2）。
- 依据：只读源码 `/tmp/echomuse-ref-build`（HEAD `50bbbc3` 2.27.0-ea.1 + 本项目 ref-uplink 补丁，**与设备运行版同代**）、只读 `rui.log`、`docs/audit-device-audio.md` 现场日志、rui 源码 `house-ear`。
- 约束：本次只读，未改代码/配置，未重启设备或 rui，未改音量；**所有实验由 Lead 执行**。
- 行号说明：`device/...` 行号是上述 HEAD 的行号，上游演进后会失效，引用时以函数/常量名为准。

---

## 0. 一句话结论

设备端 AEC 的**链路本身是对的**（ch8 同帧硬件参考 + 去混叠 + speexdsp MDF），但**配置里能调的 AEC 旋钮在硬件参考路径上几乎全是惰性的**：`aecTailMs`/`aecDelayMs` 在 `src=hw` 时不再参与建滤波器，`agcEnabled`/`bargeInEnabled` 在 `lock_mic=false` 的常开流里根本不执行。真正还能把 `att` 从 14–18 dB 往上推的，只有 **麦克风选择（wakeMic）、麦克风增益/削波、播放电平与频谱、以及一个必须动固件的 hwTail 常量**。按现有硬件和播放电平估算，设备端可达上限约 **20–24 dB**，而要在 127 音量下彻底压掉自听需要 **~31 dB**，所以"设备接管 AEC"应当定位为**把自听降到可控 + 由控制器兜底 gate**，而不是"设备 AEC 一开就无需控制器策略"。

---

## 1. 结论（先看这 8 条）

1. **当前 `att≈7–11 dB` 不是一个稳定的收敛值**，它混了三件事：冷启动（0–2 dB，约 4 s 才收敛）、短回复（2–3 s 的回复基本全程在爬坡）、以及双讲窗口（`att` 把用户说话的帧也算进去）。现场 01:22 的 8 秒连续播放窗口读数是 **10.8–16.7 dB、围绕 14 dB 波动**；Lead 在 19:34 的提交说明里另记了一次 **18.2 dB**。两者合起来是 **14–18.2 dB 的收敛带**，取决于内容、音量与是否跨断连。依据：`docs/audit-device-audio.md:245-253`、commit `2bcda35`。
2. **`aecTailMs`/`aecDelayMs` 对当前硬件参考路径无效**（源码级确认）：`SetParams` 在 `hwRef && st!=nil && enabled 不变` 时直接存值返回；真正建滤波器用的是常量 `hwTailMs=64`，不是 `aecTailMs`。所以"把 tail 调到 300/500 看 att 变化"这个实验**必然读不出差异**，过去若试过，结论是假的。依据：`device/internal/aec/aec.go:70, 236-242, 330-342, 358-376`。
3. **AGC/barge 在本拓扑下也不执行**：AGC 的条件是 `lockMic && agcEnabled`，而插头只发 `mic_start{lock_mic:false}`；`bargeInEnabled/Threshold` 只喂给设备端影子唤醒器，而 `owwOnDevice=off` + `wakeWordEnabled=false` 时该 scorer 根本不建。两者都**不是**自听的解，也不是 AEC 的解。依据：`device/internal/client/data.go:1226-1227`、`device/cmd/server.go:1290-1355`。
4. **本拓扑只有一路麦克风真正上线**：`lock_mic=false` ⇒ beamformer 永远 `lockedChannel=-1` ⇒ 输出固定 ch6（中心麦）或 `wakeMic` 指定的周边麦。7 个 speex 滤波器每周期都在自适应（6.04 ms/32 ms ≈ 单核 19%），但只有 1 路会被上行。依据：`device/internal/beamformer/beamformer.go:341-347, 391-394, 314-321`。
5. **"设备 AEC 调到可用"的物理上限被回声/语音比卡住**：播放回复时上行回声 RMS 约 **-9 dBFS（≈12000，S16）**，用户正常说话约 -15…-20 dBFS（估算）。要让残差落到 ASR 不敏感的 -40 dBFS，需要 **31 dB ERLE**；bench 收敛值是 **20.4 dB**，本房间实测 **14–18.2 dB**。**任何参数组合都不可能把 ~18 dB 变成 31 dB**。依据：现场 `mic=5684…7722`（`att` 行）、`ref=1578…2236`、上行 = 抽取后 ×2（`aecHeadroom`），见 §3.3。
6. **能真正改变数字的三个旋钮**（按性价比排序）：
   - **`wakeMic`（0=ch6 中心，1–6=MK1–MK6）**：换一路麦就换一条回声路径；上游在 Dot 2 上实测同一 wake sound 在不同麦上是 **24–26 dB 到 −7 dB** 的跨度（`aec.go:99-105`）。这是唯一"只推配置就可能拿到几个 dB"的 AEC 相关参数。
   - **播放电平/频谱**（音量、`loudness.trim_db`、`lowcut_hz`、bass guard）：回声电平每降 6 dB，所需 ERLE 就少 6 dB；同时减少小喇叭的非线性失真。**这是唯一能把 31 dB 需求降进可达区的杠杆**。
   - **`micGainDb`**：不改线性 ERLE（分子分母同比），只改削波和量化；目前 18 dB 下仍会在双讲时削波（现场 6/7、11/17），建议固定在 15–18 之间不再上探。
7. **2ch 参考补丁与设备 AEC 不冲突，但也不该同时"工作"**：`aecEnabled=true` + `ref_channels=1` 时，上行是 `[设备 AEC 之后的 mono | 去混叠 ch8]`（补丁的 mic 分支在 AEC 之后，`data.go:1412`），此时 rui 的 AEC 是在消"已经消过的残差"，`aec_erle_db` 只会读到 0–5 dB，**不要据此判断设备 AEC 无效**。生产上 `owner=device` ⇒ `ref_channels=0`、不宣告 `dot2_hw_ref`，补丁保持休眠、零成本。**只在做 A/B 或想级联兜底时**才开参考。
8. **验收不要只看 `att`**：`att` 是"帧 RMS 的算术平均之比"，不是 ERLE（见 §2.6），且它不上报（设备日志只 `SendLog` 了 3 处，`[aec] att=` 不在其中）。真正可执行的验收是 **上行录音 A/B 算衰减 + 20 条回复 0 自听 turn + 播放中唤醒召回**（§6、§7）。

---

## 2. 设备端 AEC 的完整链路（源码级）

### 2.1 拓扑与帧

```
9ch S24_3LE @16k  ──► beamformer（选一路，ch0-6）
   ch0-5: 6 个周边麦 MK1-MK6（30° 间隔，330/30/90/150/210/270）
   ch6  : 中心麦 MK7
   ch7  : 播放回采 LEFT（喇叭不播这一路）
   ch8  : 播放回采 RIGHT（真正被播出的那一路）→ AEC 的 far-end
                          │
                          ▼
              mic 抽取（micGainDb，AEC 开时先让 6 dB headroom）
                          │
                          ▼
        ProcessMicsWithRef：7 路各一个 speex MDF（512 样本/帧 = 32 ms，1024 taps = 64 ms）
                          │
                          ▼
              makeup +6 dB（把 headroom 还回去）→ VAD →（仅 lockMic 时）AGC
                          │
                          ▼
                  上行 0x01 帧，80 ms/块，S16 mono @16k
```

- 帧长：`FrameSize=512`（32 ms @16 k），`sampleRate=16000`（`aec.go:48-51`）。
- 麦克风周期：ALSA 一次给 160 ms（2560 样本，5 个 speex 帧），`Process` 支持任意 512 整数倍，**不是 512 就整段 bypass 并打日志**（`aec.go:536-545`）。
- 上行块：80 ms（`vadOwwChunkBytes=1280*2`，`data.go:1398` 起）。

### 2.2 参考来源：hw ch8 与 sw tap

| | 硬件参考（ch8） | 软件 tap（喇叭 ALSA 写入处） |
|---|---|---|
| 来源 | 与麦克风**同一个 TDM 帧**、同一 ADC 时钟 | 48 k 立体声下混 `(L+R)/2` 后 3:1 抽取 |
| 对齐 | 结构性对齐；残余是实测 **+33 样本（2.06 ms）** 转换器+声学延迟，被滤波器 tail 吸收 | 需要 ring（3 s）+ `aecDelayMs` + 占用率调节器去"猜"远端在哪一刻 |
| 滤波长度 | 固定 `hwTailMs=64` | `aecTailMs`（默认 300，clamp 50–500） |
| 相关参数 | `aecDelayMs` 只被 store，不生效 | `aecDelayMs`（默认 0，clamp 0–1000）生效 |
| 何时用 | ch8 被"证明"是回采后（见下） | ch8 证明失败时的回退 |

- 证明条件（`data.go:395-447`）：ch8 **必须既见过 bit-exact 全零（空闲）、又见过有音频（播放）**，两者都满足才 latch 到 hw，且**单向不可逆**（除非 operator 推 `aecRefSource`）。日志：`[aec] ch8 confirmed as the playback loopback`。
- 强制档：`aecRefSource` = `auto|hw|sw`（`config.go:138-155, 678-693`），`hw/sw` 是可逆的 A/B 开关，`auto` 是检测。**我们的 config push 目前没有这个字段**（§4.7）。
- `hwMissing`：hw 模式下 ref 为 nil 或长度不匹配，帧原样透传并计数，`att` 会读成 ~0 dB。日志：`[aec] hardware reference missing or mismatched`。
- ch8 的坑：它是 48 k 输出**每 3 个取 1 个**塞进 16 k 采集，**无抗混叠**；9/12/15/20 kHz 会在 ch8 落到 7/4/1/4 kHz 而麦克风里没有。`dealias.go` 用输出环重建参考（95-tap Blackman sinc，截止 7.2 kHz，环 1.37 s），上游实测粉噪：**raw ch8 只有 1 dB，重建后 25.7 dB**。日志：`[aec] de-aliased reference locked to the output`。
  - 对**语音 TTS** 影响小（TTS 本身带限，>8 kHz 能量少）；对**音乐**是关键，这也是上游说"music 让 barge-in 上不去"的原因。

### 2.3 滤波器

- speexdsp MDF/AUMDF，vendored 1.2.1；`FLOATING_POINT` + KISS FFT（`aec.go:23-33`）。
- 硬件路径：每个麦一个 state，`tail=64 ms`（1024 taps）。上游 bench（2026-09-22）结论：**收敛后 16 ms 和 300 ms 一样深（20.3 vs 20.4 dB）**，64 ms 只为"更活泼的房间"留 4 倍余量；300 ms 要 ~10 s 收敛，64 ms 约 4 s（`aec.go:62-70`）。
- **这个 bench 结论是"近无混响"条件下的（推断）**：16 ms 的滤波器只能覆盖直达声+最早反射。客厅 RT60 量级 0.3–0.5 s（未实测），64 ms 之后的混响尾巴是**无法被 64 ms 滤波器消掉的**——这是本房间收敛值只有 ~14 dB、而 bench 有 20.4 dB 的最合理解释（§3.2 给判别实验）。
- 状态持久化：`/data/local/etc/echomuse/aec_echo_path.v2.bin` + `.mics`（7 路），`stateMagic="EMAEC1"`，头里固定 `frame=512/tailMs/rate=16000`；`saveMinDb=12`、`saveEvery=24h`，只在 hw 路径且 `ref>100`（真在播）时保存（`persist.go:18-24, 40-62, 155-190`）。
  - 注意：**头里没有 `micGainDb`**。改了 `micGainDb` 之后加载的旧滤波器增益失配 6 dB，会"比冷启动更差"一小段时间——实验时**先定死 micGain 再调别的**。
  - 加载日志：`[aec] loaded the saved echo paths for 7 microphones`；保存日志：`[aec] saved the echo path (xx.xdB cancellation, N bytes)`。

### 2.4 7 麦与单麦的差异

- 7 路各自一个 echo state，**播放期间每周期 7 路都跑**（这样任一轮次 lock 到哪一路，那一路都已经学好了）；`quietHold=8` 帧（256 ms）无播放后，未使用的那 6 路休息（`aec.go:72-81, 685-740`）。
- 成本：**6.04 ms / 32 ms ≈ 单核 19%**（JOURNAL 2026-10-03）。
- `att` 报的是**当前选中那一路**（`on=chN`）。上游实测同一 wake sound 在不同麦上消除量 **24–26 dB 到 −7 dB** 不等（`aec.go:99-105`）——**麦间差异比参数差异大得多**。
- **本拓扑只用一路**（`lock_mic=false` ⇒ ch6 或 `wakeMic`），另外 6 路的算力是**纯浪费**；这给了"用 1 路换更长 tail"的固件空间（§4.6）。

### 2.5 AGC / beamformer / VAD 的位置

| 级 | 在 AEC 前/后 | 本拓扑是否执行 | 依据 |
|---|---|---|---|
| beam 选麦 | 前（只选通道，不做波束求和） | 是（固定 ch6/wakeMic） | `beamformer.go:351-414` |
| AEC（7 路） | — | 是（`aecEnabled=true`） | `data.go:1296-1308` |
| makeup +6 dB | 后 | 是 | `data.go:1309-1311, 374-393` |
| VAD（RMS，`0.004`×gainLin） | 后 | 计算，但**只用于 lockMic 轮次**的门控/端点 | `data.go:1318-1336, 1400-1408` |
| AGC（target 0.08、max 20×、attack .05/release .005） | **AEC 之后** | **否**（`lockMic=false`） | `data.go:1226-1227, 1373-1379`；`processor.go:22-29` |
| RNNoise NS | 已移除（控制器侧 em_ns/DTLN） | — | `processor.go:8-15` |

- 结论：**AGC 不是自听的原因，也不是可用的解**；常开流里它被 `lockMic` 短路。

### 2.6 `att` 到底是什么（不是 ERLE）

`noteAttLocked`（`aec.go:640-672`）：

```go
if refRMS <= 100 && micRMS <= 500 { return }   // 空闲不统计
statFrames++; statInSum += micRMS; statOutSum += outRMS; statRefSum += refRMS
if statFrames < 32 { return }                   // 32 × 32ms ≈ 1s
att = 20*log10(inAvg/outAvg)                    // 帧 RMS 的算术平均，不是能量比
```

读法要点：

1. **`inAvg`/`outAvg` 是"每帧 RMS 的算术平均"**，不是能量平均，也不是标准 ERLE 定义；数值可比，但不要当学术 ERLE 引用。
2. **统计窗口包含双讲**：只要 `ref>100` 或 `mic>500` 就计入。用户说话时 `out≈mic`，`att→0`，会把 1 s 均值拉低。**判断收敛必须在"没人在说话"的纯 TTS 窗口**。
3. **`mic`/`out` 都是 makeup 之前、headroom 之后的值**；因为后续 ×2 的 makeup 同时放大 `out`，`att` 恰好等于**上行端的真实回声衰减**（数学上分子分母同比缩放），可直接当"rui 看到的回声被压了多少 dB"。
4. **`mic` 不是播放电平**（那是 `ref`）；`mic` 是"回声+用户+噪声"的混合。
5. `frames=` 是累计 hw 帧数，可用于确认"滤波器跑了多久"（`814192` 帧 ≈ 7.2 h 播放）。
6. 只有 hw 路径有 `src=hw(ch8)`；sw 路径打 `ring=… (delay=…ms)` 没有 `src`。

---

## 3. 为什么现在只有 ~10 dB；上限在哪

### 3.1 先把读数分层（同一个 14 dB 有三种含义）

| 读法 | 典型值 | 含义 |
|---|---|---|
| 回复刚开始 0–2 s | 0–8 dB | **冷启动**。hw 确认后会重建 64 ms 滤波器并加载存档；没有存档时从零收敛，64 ms 约 4 s |
| 2–4 s 回复全程 | 7–11 dB | 冷启动 + 短窗口；这是"看起来只有 10 dB"的主要来源 |
| ≥8 s 纯播放（无人说话） | **14–18.2**（01:22 一组 10.8–16.7、中位 ~14；Lead 另一次 18.2） | 本房间/127 音量/ch6 的**收敛带**，随内容与连接稳定性浮动 |
| 用户同时说话 | 0–8 dB | **双讲**，`att` 定义导致，不代表 AEC 变差 |

→ **"设备 AEC 只有 10 dB"要先排除测量口径**。做实验必须用 **≥12 s 的 TTS、无人说话、取最后 3 条 `att` 行**。

### 3.2 14 dB 收敛值的天花板由谁决定（4 个候选 + 判别实验）

| # | 候选上限源 | 证据 | 判别实验（详见 §4） |
|---|---|---|---|
| 1 | **房间混响超出 64 ms tail** | bench "16 ms 与 300 ms 等价"是在近无混响条件；客厅 RT60 量级 0.3–0.5 s（假设） | **F1**：固件把 `hwTailMs` 改 128/192/256，若 `att` 涨 ≥3 dB 即坐实 |
| 2 | **小喇叭在 127 音量的非线性失真** | 127 是数字 unity，EchoMuse 又缺 stock 的 +10–14 dB EQ，用户会把音量顶满；失真分量是线性滤波器**原理上消不掉**的 | **E1**：音量降到 117/107（-5/-10 dB）重测；若 `att` 明显上升即坐实 |
| 3 | **中心麦 ch6 的回声耦合最大** | 上游实测麦间 24–26 dB 到 −7 dB | **B1**：`wakeMic` 0–6 扫一遍，读 `on=chN` + `att` |
| 4 | **滤波器未收敛/存档缺失** | 短回复 + `micGainDb` 改动使旧存档失配 | 设备日志查 `loaded the saved echo paths` / `saved the echo path`；用 12 s 回复看 `att` 是否单调爬升 |

**我的排序（置信度）**：1（中高）> 2（中）> 3（中）> 4（对短回复高、对长回复低）。
理由：01:22 的 8 s 窗口里 `att` 在 14 附近**上下波动而不是单调爬升**，说明当时已收敛；bench 20.4 vs 现场 14 的 6 dB 差，用"混响尾巴"解释最自洽（64 ms 之外的混响能量正好是 -10…-15 dB 量级），其次才是失真。

### 3.3 定量：要把自听压掉，需要多少 dB

现场数字（`audit-device-audio.md:245-253`，纯播放窗口）：

- `ref ≈ 2000`（-24.3 dBFS，ch8 是播放的数字回采）
- `mic ≈ 6000`（-14.7 dBFS，headroom 之后）
- 上行无 AEC 时 = `mic × 2`（makeup 还原）≈ **12000（-8.7 dBFS）**
- 即：**播放回声在麦上比它自己的数字参考高约 +15 dB**（近场小喇叭 + 麦距几 cm），这是物理事实，不是 bug。

需要的 ERLE（把残差压到目标电平）：

| 设备音量 | 相对 127 | 上行回声 RMS（无 AEC） | 残差 -30 dBFS 需 | -35 dBFS 需 | **-40 dBFS 需** |
|---:|---:|---:|---:|---:|---:|
| 127 | 0 dB | 12000 (-8.7) | 21.3 dB | 26.3 dB | **31.3 dB** |
| 117 | -5 dB | 6750 (-13.7) | 16.3 | 21.3 | 26.3 |
| 107 | -10 dB | 3800 (-18.7) | 11.3 | 16.3 | 21.3 |
| 97 | -15 dB | 2140 (-23.7) | 6.3 | 11.3 | 16.3 |
| 87 | -20 dB | 1200 (-28.7) | 1.3 | 6.3 | 11.3 |

对照可达性：

| 来源 | 收敛 ERLE | 说明 |
|---|---:|---|
| 本机现状（127、ch6、客厅） | **14–18.2 dB** | 现场日志 + Lead 实测 |
| 本机 bench（64 ms，近无混响） | 20.4 dB | 上游 2026-09-22 |
| 同算法、256 ms 滤波（rui，LX06） | 24–30 dB | `house-ear/audio/aec.py:1-10` |
| 设备端可达上限（估算，127、换麦+固件 tail） | **20–24 dB** | 未实测 |
| 免 gate 彻底压自听所需（127） | **31 dB** | 上表 |

**结论：127 音量下，设备端 AEC 不可能单靠参数做到"无自听"；把音量降到 ~107（-10 dB）后，21 dB 就能到 -40 dBFS，才进入"可调得到"的区间。** 这条是 9 哥需要拍的产品取舍：**要么接受控制器 gate 兜底，要么把 TTS 电平/低频压下来**（EchoMuse 在 127 本来就比 stock 轻 10–14 dB，再降是听感问题）。

---

## 4. 参数实验矩阵（Lead 执行；每项只改一个变量）

### 4.0 实验纪律（否则数据不可用）

1. **同一段音频**：用 12–15 s 的固定 WAV（`DOT2_INJECT_PORT=8871` + `python -m rui_dot2_bridge.inject_cli --wav /tmp/long16k.wav`；WAV 要放在 `.34` 上，或先 `scp` 过去），不要用"讲个故事"（长度和内容不可控）。
2. **纯播放、无人说话**：`att` 取**最后 3 条**（稳态）。
3. **A/B 录音法**（不依赖 adb，见 §7.2）：`aecEnabled=false` 录一遍上行、`true` 再录一遍，同一窗口算 `20log10(rms_off/rms_on)`，这就是可比的 ERLE。
4. **每次只改一个参数**；改 `micGainDb` 会让已保存的 echo path 增益失配，**先把 micGain 定死**。
5. **注意当前控制面每 ~30–60 s 断连一次**（§7.3），一次 A/B 要在同一个连接窗口内完成，且把断连次数记为无效样本。
6. 记录：`att / mic / out / ref / on=chN / frames`、`[aec] enabled … tail=`、`far-end reference:`、`hardware reference missing`、`AEC BYPASSED`、`reference underrun`（后三个出现即本次数据作废）。

### 4.1 A 组：owner 与参考源（0 风险，先做）

| ID | 参数 | 取值 | 预期 | 读法 |
|---|---|---|---|---|
| A1 | `aecEnabled` | `false` → `true` | off 时 `att` 行消失、`stats.aecRef=off`；on 时出现 `src=hw(ch8)` | 作为 A/B 录音法的分母 |
| A2 | `aecRefSource` | `auto`（现状）/`hw`/`sw` | `hw` 与 auto 等价（ch8 已确认）；`sw` 应显著变差（历史 ~0–3 dB） | 日志 `[aec] far-end reference: hardware (ch8…)` / `software tap…`；`att` 行有无 `src=hw` |
| A3 | 启动序列 | — | 应看到**两条** `[aec] enabled`：先 `tail=300ms`（boot 的 sw 构建），ch8 确认后 `tail=64ms`（hw 构建） | **只有 `tail=300ms` 说明 hw 没切成功**，`att` 是 sw 的，全部数据作废 |
| A4 | 持久化 | — | boot 后 `[aec] loaded the saved echo paths for 7 microphones`；长回复后 `[aec] saved the echo path (≥12 dB…)` | 无 loaded ⇒ 每次 boot 都冷启动，短回复读数必然低 |

### 4.2 B 组：麦克风选择（**最值得先做**，纯配置）

| ID | 参数 | 取值 | 预期 | 读法 |
|---|---|---|---|---|
| B1 | `wakeMic` | `0`（=ch6 中心，现状）/ `1..6`（MK1..MK6） | 每换一路，回声耦合/指向变一次；上游麦间跨度 24→−7 dB。可能拿到 **±3–6 dB** | `att` 行的 `on=chN`；同时听 `mic` 电平（用户语音不能掉太多） |
| B2 | `beamformingEnabled` + `beamAngle` | `false`；`angle=0/90/180/270` | **本拓扑预期零变化**（`lock_mic=false` ⇒ 永远走 omni 通道） | 用来证明"beam 角度不是旋钮"；若确有变化，说明有 `beam_lock` 在发（查 `[beam] locked` 日志） |
| B3 | `beam_lock`（控制面消息，非 config） | 不发（现状）vs 发 | 发了才会换周边麦；**不建议**为 AEC 引入，除非 B1 证明周边麦更好 | 日志 `[beam] locked to chN` |

> B1 的判据：选 `att` 最高、且 `mic` 不比 ch6 低 3 dB 以上（用户语音信噪比优先于纯 att）的那一路。B1 与 F1 独立，可先做。

### 4.3 C 组：增益与削波（只影响量化/失真，不改线性 ERLE）

| ID | 参数 | 取值 | 预期 | 读法 |
|---|---|---|---|---|
| C1 | `micGainDb` | `15` / `18`（现状）/ `21` / `24` | `att` **基本不变**（±1 dB）；`mic` 线性变化；≥21 时双讲削波增加 | `att` 行 + 设备 `clipped=`（`[data] VAD diag`）；`makeupClipped` 在 AEC 后计数 |
| C2 | `adcMicpga` | `40`（默认/Amazon 工厂值）→ `32`/`48` | 模拟增益，改变量化底噪与模拟削波；**风险高于收益** | 只在 C1 用尽后做；必须同时看 `clipped` |
| C3 | `adcDigitalGain` | `88` → `80`/`96` | 同上，数字级 | 同上 |

> 现状 18 dB 是之前为避免 24 dB 削波选的（`docs/findings.md` 2026-10-24）。**不建议为 AEC 再上探**：线性路径上与 ERLE 无关，削波只会喂给自适应滤波器失真。

### 4.4 D 组：明确无效项（做一次，写进结论，关闭问题空间）

| ID | 参数 | 取值 | 预期 | 备注 |
|---|---|---|---|---|
| D1 | `aecTailMs` | `50` / `100` / `300` / `500` | **hw 路径零变化**（存储但不建滤波器） | 若读了差异 ⇒ 说明当前走的是 sw（回看 A3） |
| D2 | `aecDelayMs` | `0` / `100` / `250` | **hw 路径零变化**；sw 路径上 ≥100 会让回声早于参考（非因果）⇒ 0 dB | 同上 |
| D3 | `agcEnabled` | `false` / `true` | **零变化**（`lock_mic=false`，AGC 从不执行） | 日志 `[data] VAD diag … agc=false` 可确认 |
| D4 | `bargeInEnabled` / `bargeInThreshold` | on/off；`0.05`/`0.25` | **零变化**（只喂影子唤醒器，`owwOnDevice=off` 时不建） | 若要真正生效必须 `owwOnDevice=on`，见 §5.4 |
| D5 | `vadThreshold/vadSpeechMs/vadSilenceMs` | 默认 0.004/80/600 | 对常开流**不影响上行**，只影响 lockMic 轮次端点 | 不要为 AEC 调它 |

> D 组合计约 1 小时，价值极高：**它证明"过去把 tail/delay/AGC 当旋钮"的整条路线是空的**，把后续精力集中到 B/E/F。

### 4.5 E 组：播放侧（影响非线性 + 混响激励；**唯一能把需求拉进可达区**）

| ID | 参数 | 取值 | 预期 | 读法 |
|---|---|---|---|---|
| E1 | 设备音量 | `127`（现状）/`117`/`107` | `att` 每 -5 dB 音量约升 1–4 dB（若失真主导），或几乎不变（若混响主导） | `ref` 同步 -5 dB；`att` 对比 |
| E2 | `loudness.trim_db` | `-2`（现状）/`-4`/`-6` | 同上，且不动用户音量感知的绝对值 | 控制器侧，改 adapter env/文件 |
| E3 | `loudness.lowcut_hz` / `lowcut_db` | `250/6`（现状）→ `320/12` | 减少小喇叭低频行程 ⇒ 失真下降、`att` 上升 | 同上；听感会变薄，需 9 哥拍 |
| E4 | bass guard（设备侧） | on / off | **今天无效**：设备输出链没交给设备（`output_chain` 未声明），`bassGuard*` 只存不用；要压低频请走 E3（控制器侧 lowcut） | `em-config.json` 的 `bassGuard*` 与 `eqBands` 都不生效 |

> E1 是**诊断**（用来区分"失真主导"还是"混响主导"），不是产品建议；E2/E3 才是可落地的产品参数。**9 哥已明确不要动音量，E1 只在诊断时短暂执行并记录**。

### 4.6 F 组：固件常量（**决定上限，必须改固件/重建；高价值**）

| ID | 改动 | 取值 | 预期 | 成本/风险 |
|---|---|---|---|---|
| F1 | `hwTailMs` | `64` → `128` / `192` / `256` | 若混响主导：**+3–6 dB**；若失真主导：0 | 需重建+推送 `server` 二进制；成本估算（非实测）：MDF 块数随 tail 增，128 ms≈2×、256 ms≈4× 单麦；7 麦 ×256 ms 会到 70%+ 单核 |
| F2 | `NumMics` 7→1（只保 ch6/当前 `wakeMic`） | `1` | 释放 ~6/7 的 AEC 算力，F1 的 256 ms 估算仍只占 ~10–12% 单核 | 前提：不用 `beam_lock`/`lock_mic=true`；一旦要用周边麦必须改回 |
| F3 | `att` 上报 | 把 `att` 塞进 `stats` 或 `SendLog` | 控制器可直接看，不用 adb | 小改动，**建议与 F1 一起做**，否则每轮实验都要人肉 adb |
| F4 | saved path 版本键 | 头里加 `micGainDb`（或改名 v3） | 改增益后不再加载失配滤波器 | 低风险 |
| F5 | `hwTailMs` 变成配置项 | 复用 `aecTailMs` 作为 hw 上限 | 以后可远程 A/B，无需 OTA | 需防止 fleet 推送误重建（现有早退逻辑要保留） |

> 成本估算：F2 是 F1 的"预算来源"。**推荐组合：F2（1 路）+ F1（256 ms）+ F3（上报）**——这是唯一可能在 127 音量把 `att` 推到 20–24 dB 的路径；若 F1 后 `att` 不动，则确认上限由失真/物理耦合决定，别再投入。

### 4.7 在 `.34` 上怎么推（现状 + 需要补的代码）

**当前 config push 的字段**（`src/rui_dot2_bridge/device_link.py:296-314`）：

```json
{"type":"config","startupVolume":…,"owwOnDevice":"off","wakeWordEnabled":false,
 "aecEnabled":true,
 "micGainDb":18,
 "aecTailMs":300,          // commit 2bcda35 已加（env DOT2_AEC_TAIL_MS）
 "aecDelayMs":…,           // 同上（DOT2_AEC_DELAY_MS）
 "agcEnabled":…,           // 同上（DOT2_AGC_ENABLED；本拓扑无效）
 "bargeInEnabled":…,       // 同上（DOT2_BARGE_IN_ENABLED；本拓扑无效）
 "bargeInThreshold":…}     // 同上（DOT2_BARGE_IN_THRESHOLD）；adapter 侧写的是 int(_barge_thr)，设 "0.05" 会在 build() 直接 ValueError；设 0 又会被设备的 `>0` 判空忽略
```

**缺口**：`aecRefSource`、`wakeMic`、`beamformingEnabled`、`beamAngle`、`adcMicpga`、`adcDigitalGain`、`vad*` **都没接**。B1/A2 需要各加一行（字段名见 `device/internal/config/config.go` 的 `ConfigMessage`，`aecRefSource` 在 `:597`、`wakeMic` 在 `:592`）。

**执行方式**（Lead）：

```bash
# 0) 备份并确认当前值
ssh yes999zc@<lan-ip> 'cp ~/rui-test/env ~/rui-test/env.bak.$(date +%F-%H%M%S); grep -E "^DOT2_" ~/rui-test/env'
# 1) 改一个旋钮（示例：wakeMic=2 → 需要先给 adapter 加 DOT2_WAKE_MIC 并透传）
# 2) 重启让插头在重连时重推 config
ssh yes999zc@<lan-ip> 'sudo systemctl restart rui-test'
# 3) 确认设备重连 + 推送生效（设备日志侧看 [config] / [aec] enabled 行）
ssh yes999zc@<lan-ip> 'grep -E "dot2 registered|aec off" ~/rui-test/logs/rui.log | tail -3'
```

> 注意：**每次重连都会重推 config**（`_serve` 里注册成功即推）。当前连接每 30–60 s 断一次，所以改 env 后甚至不用重启，等下一次重连即可（但为了确定性，建议重启）。
> `aecTailMs/aecDelayMs` 变化会触发 `SetParams` 重建——**在 hw 路径上不会**（早退），所以 D1/D2 不会破坏已收敛滤波器；但 `aecEnabled` 切 false/true 会重建，A1 的 A/B 请放在同一会话且顺序固定。

---

## 5. 设备 AEC 与 2ch 参考补丁的关系；"owner=device" 怎么表达

### 5.1 四种组合（同一份补丁，不同声明）

| `aecEnabled` | `ref_channels` | 上行内容 | rui AEC | 用途 |
|---|---|---|---|---|
| `true` | `0` | `[post-AEC mono]` | 关（自动告警） | **生产推荐：owner=device** |
| `false` | `1` | `[pre-AEC mono \| 去混叠 ch8]` | 开（256 ms） | 旧 B 方案：rui 接管 |
| `true` | `1` | `[post-AEC mono \| 去混叠 ch8]` | 开会得到 ~0–5 dB | **级联/诊断**（见下） |
| `false` | `0` | `[pre-AEC mono]` | 关 | 裸基线（A/B 的分母） |

关键代码事实：补丁的 `RefForUplink` 在 AEC **之前**抽参考（`data.go:1272-1279`），但 `mono` 进入 `buf` 是在 AEC **之后**（`data.go:1300-1311` → `1412`）。所以：

- **`aecEnabled=false` 时**，`[mic|ref]` 才是"预 AEC 麦 + 参考"，rui 可以干净接管；
- **`aecEnabled=true` 时**，`[mic|ref]` 是"设备已消过的麦 + 参考"，rui 在消残差（级联）。
- ⚠️ `docs/aec-takeover.md` §3 表里写的"波束 mono（预 AEC）+ 参考"只对 `aecEnabled=false` 成立，**该文档需要加这一句限定**（我不改它，留给 Lead）。

### 5.2 设备 AEC 开时，2ch 参考还有意义吗？

- **对设备自身**：没有。参考抽取只在 `refUplink=true` 时执行（`data.go:1273`），关掉不花算力；`dealias` 的 ring 为设备自己的 AEC 供料，与上行无关。
- **对控制器**：
  1. **诊断**：rui 的 `aec_erle_db` 在级联下≈0–5 dB，可以当作"设备残差里还有多少可线性消掉的部分"的探针；若它还能读到 10 dB+，说明设备 AEC 的残差主要是**线性**的（还有救），若≈0 则残差是失真/噪声（没救）。
  2. **兜底级联**：设备 18 dB + rui 20 dB ≈ 38 dB，正好覆盖 §3.3 的 31 dB 需求。**这是"127 音量 + 免 gate"唯一现实的工程解**，代价是上行 64 KB/s + rui 一个 4096-tap 滤波器。
  3. **不建议**把"级联"当默认：它违背"控制器只消费处理后的音频"的方向，且 rui 的 AEC 会把设备残差里的用户语音也当双讲处理（误消风险）。
- **结论**：`owner=device` 时 `ref=0`；把 `ref=1` 保留为**实验/兜底开关**，并在插头文档里写清"级联时 `aec_erle_db` 不可当设备指标"。

### 5.3 插头声明怎么写（现状已基本正确）

`plug.json` 当前：

```json
"audio": { "capture": { "mic_channels": 1, "ref_channels": 0, "mic_gain_db": 18 } },
"capabilities": { "aec": "device", "barge_in": "wake" }
```

`owner=device` 的完整表达（建议写进插头规范，而不只是这个仓库）：

| 字段 | 值 | 含义 |
|---|---|---|
| `capabilities.aec` | `"device"` | 回声消除由设备完成，控制器**不得**再开 AEC |
| `audio.capture.mic_channels` | `1` | 上行只有处理后的单声道 |
| `audio.capture.ref_channels` | `0` | 不声明参考通道 ⇒ rui 的 AEC 与"回声型 barge-in"自动关闭（`process.py:404-410`） |
| `ack.features` | 不含 `dot2_hw_ref` | 设备不发 2ch（`device_link.py:446-450`） |
| config push | `aecEnabled: true`（+ 建议 `aecRefSource:"hw"` 锁死） | 保证不因 auto 检测失败退回 sw |
| rui `audio.aec.enabled` | 可留 `true`（会被 ref=0 门掉，但打 warning）或显式 `false` | 语义干净建议显式 false，并接受"回声型 barge-in 关闭" |

> 建议给插座规范补一个**一致性校验**：`capabilities.aec=="device"` 时若 `ref_channels>0` 或 rui `aec.enabled=true`，应告警"双重 AEC"。现在两边都不会告警。
> 另外 `barge_in: "wake"` 是准确的：**wake 打断不依赖 AEC**；"回声型 barge-in"在 `ref=0` 下不可用（日志 `aec off: device has no reference channel`）。

### 5.4 如果一定要"设备侧 barge-in"

只有一条路：`owwOnDevice=on` + `bargeInEnabled=true` + `bargeInThreshold`（0.25 量级，不是默认 0.05）+ `wakeWordEnabled=true`，让设备用自己的（已 AEC 的）流做唤醒打分并触发打断。代价：

- 唤醒从 rui 的 KWS 换到设备 OWW，**与当前 rui 的 KWS/仲裁体系并存且语义冲突**（`oww_trigger` 让设备侧直接触发轮次）；
- 阈值 0.05 太低，上游记录过 8 s/24 s 的**假打断**（`controller/em_barge.py:1-60`），必须 0.25 + 连续两帧；
- 在 14 dB 残差下，假打断率会偏高，**不建议在 att<18 dB 前启用**。

---

## 6. 验收标准（建议写进 P4 的 DoD）

### 6.1 硬指标

| # | 指标 | 通过线 | 观测 |
|---|---|---|---|
| V1 | 稳态 `att`（12–15 s 纯 TTS，最后 3 行中位） | **≥ 18 dB 且不低于基线 18.2 − 1 dB**（<15 视为不可用）。**注意：18 dB 只说明"不退化"，不代表通过** | 设备 `[aec] att=… src=hw` |
| V2 | 残差电平 `out` | **≤ 900**（≈ -31 dBFS）且 `mic/out ≥ 17 dB` | 同上 |
| V3 | 自听率（关键） | **20 条回复（2/5/10/15 s 各 5 条）→ 0 个 phantom turn**：播放中及结束后 2 s 内不产生新 turn，`听到的` 不含回复文本（尤其"在呢。"） | rui 控制台 turn 历史 + `rui.log` |
| V4 | 播放中唤醒召回 | 20 次 ≥ 18 次（正常音量、1–3 m） | 人工计数（`self_test_voice.py` 是唤醒→命令的冒烟测试，不做召回统计） |
| V5 | 稳定性（30 min 连续） | 0 × `hardware reference missing`、0 × `AEC BYPASSED`、0 × `reference underrun`、CPU 不触发 `thermalCoreLimit<4` | 设备日志 + `stats` |
| V6 | 存档 | boot 后 1 次 `loaded the saved echo paths`；长回复后 1 次 `saved the echo path` | 设备日志 |

### 6.2 分级判定

| 稳态 `att` | 判定 | 自听（无 gate） | 后续 |
|---|---|---|---|
| **≥ 25 dB** | 接近免 gate | 大概率 0/20 | 只有"固件长 tail + 降电平/压低频"叠加才可能，属于 stretch |
| **20–24 dB** | 好 | 长回复仍偶发 | 固件 F1/F2 的目标；仍需 gate 兜底 |
| **18–20 dB（现状 18.2）** | 基线 | **会自听**（现场已证） | 靠 V3 的 gate + E2/E3 频谱；不要把 18 当终点 |
| **15–18 dB** | 边缘 | 频繁 | 换麦（B1）/ 压电平（E1–E3） |
| **< 15 dB** | 不可用 | — | 走 F1/F2 固件路线；或退回 `ref=1` + rui AEC（B 方案） |

> 这张表的核心：**18 dB 不是"通过"，是"起点"**。现场 18.2 dB 仍然自听，正是"att 单指标不够、必须用 V3 判"的证据。

### 6.3 不要用的假指标

- **`att` 的 1 s 单行**（噪声大，且可能正好是双讲）；
- **`aec_erle_db`**（owner=device 时是 null；级联时≈0，见 §5.2）；
- **"没有 `hardware reference missing`"**（只证明 ref 到了，不证明消得动）；
- **"att 比昨天高了"**（不同回复长度/音量不可比；必须同一 WAV）。

---

## 7. 观测方法与现场异常

### 7.1 设备日志（唯一能看到 `att` 的地方）

```bash
# 设备日志在 /tmp/server.log，RAM-backed，重启即清；时间是 UTC（+8 = 本地）
adb -s G090LF0964340RN6 shell su -c 'grep "\[aec\]" /tmp/server.log' | tail -40
adb -s G090LF0964340RN6 shell su -c 'grep -E "\[aec\]|clipped=|\[data\] VAD diag" /tmp/server.log' | tail -60
```

> ⚠️ **本机（Mac）没有 adb**（`/opt/homebrew/bin`、`~/Library/Android/sdk`、`05_Tools` 均无），设备 5555 也未开（USB adb）。所以这一步**必须在插着 USB 的机器上做**；我在本次调研中无法读到实时 `att`，报告里的现场数字来自 `docs/audit-device-audio.md` 的 01:22 记录。

需要 `grep` 的关键行：

| 模式 | 含义 |
|---|---|
| `[aec] enabled: frame=512 tail=64ms` | hw 滤波器已建（`tail=300ms` 那条是 boot 的 sw 构建） |
| `[aec] far-end reference: hardware (ch8, frame-aligned)` | ch8 已确认 |
| `[aec] de-aliased reference locked to the output` | 去混叠锁上（音乐场景必需） |
| `[aec] att=… src=hw(ch8) on=chN frames=N` | 主指标 |
| `[aec] hardware reference missing or mismatched` | ref 缺失，帧透传（数据作废） |
| `[aec] mic buffer … AEC BYPASSED` | 帧长不对（数据作废） |
| `[aec] reference underrun` | sw 路径才会，hw 路径不该出现 |
| `[aec] saved/loaded the echo path(s)` | 持久化状态 |

### 7.2 不依赖 adb 的 A/B 录音法（**推荐作为主验收**）

思路：`DOT2_RECORD` 录的是 adapter 真正交给 rui 的上行（post-AEC + 6 dB makeup；本拓扑 AGC 不执行 ⇒ 干净）。同一段 WAV、同一 `micGainDb`、同一 `wakeMic`、同一音量下，**AEC 开/关两次**，比值就是回声衰减。因为 makeup 同时作用于两次，会约掉。

```bash
# 一次一个文件：每次改完 env 要重启（DOT2_RECORD 在进程启动时打开，跨进程不会混写）
# 轮次 1（分母）
ssh yes999zc@<lan-ip> 'grep -q "^DOT2_RECORD=" ~/rui-test/env || echo "DOT2_RECORD=" >> ~/rui-test/env; \
  sed -i "s|^DOT2_RECORD=.*|DOT2_RECORD=/tmp/aec_off.wav|; s|^DOT2_AEC_ENABLED=.*|DOT2_AEC_ENABLED=0|" ~/rui-test/env'
ssh yes999zc@<lan-ip> 'sudo systemctl restart rui-test'
# 等设备重连后注入同一段 15 s WAV（DOT2_INJECT_PORT=8871 需已设置）
ssh yes999zc@<lan-ip> 'python -m rui_dot2_bridge.inject_cli --port 8871 --wav /tmp/long16k.wav'
# 轮次 2（分子）：同法，DOT2_RECORD=/tmp/aec_on.wav、DOT2_AEC_ENABLED=1，重启后再注入
```

```python
# 用 100 ms 帧 RMS 的"最高 10% 均值"比较：不要求两次文件对齐，只要求两次播放的是同一段音频
import wave, numpy as np
def loudness(p, frame=1600):            # 1600 samples = 100 ms @16k
    with wave.open(p) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64)
    n = len(a)//frame
    fr = a[:n*frame].reshape(n, frame)
    r = np.sqrt((fr*fr).mean(axis=1))
    return float(np.sort(r)[-max(1, n//10):].mean())   # 播放段的代表电平
off, on = loudness('/tmp/aec_off.wav'), loudness('/tmp/aec_on.wav')
print(f'ERLE≈{20*np.log10(off/on):.1f} dB   (off={off:.0f} on={on:.0f})')
```

- 前提：两次同音量、同 `micGainDb`、同 `wakeMic`、无人说话、无削波；播放的是**同一段音频**。
- 这个数应当与设备 `att` 的中位接近（差异来自 `att` 的"算术平均"口径）；**相差 >3 dB 时以录音法为准**（它才是 rui 真正收到的）。
- 若不想改 `DOT2_RECORD`，`DOT2_UPLINK_DUMP`（原始块、无 WAV 头）也行，但需要自己按 16 k/S16 解析；两者都不需要动设备。
- 录到的文件里会包含断连空洞（§7.3）和注入前的静音，所以用"最高 10% 均值"而不是全文件 RMS。

### 7.3 现场异常：控制面/数据面每 30–60 s 断一次（**必须先解决，否则 A/B 不可信**）

`rui.log` 19:33–19:35 的稳定模式（22 次 data plane connect）：

```
19:34:43 dot2 registered (fw 20261024-1911-dev, …)
19:34:46 control closed
19:34:51 dot2 registered…
19:35:19 control closed
19:35:25 dot2 registered…
```

- 每次重连都会重推 config + `mic_start`，并且 **data plane 一起断** ⇒ 播放/收音有 5 s 级空洞。一次 12 s 的 TTS 很可能跨断连，`att` 窗口被切碎，**这是当前最可能让实验数据"看起来只有 10 dB"的现场因素之一**。
- 已排除：不是两个 adapter 抢 8870（`ss -ltnp` 只有一个 PID 2390980）。
- 待查（Lead）：设备侧 `[control]`/`[auth]`/`closing` 日志、rui 的 pong RTT（`PING_INTERVAL_S` 与 30 s 超时）、是否 `mic_start` 触发；以及设备是否在跑 watchdog。
- **在解决前**：每次实验把"断连次数"记下来，只取**同一个连接窗口内 ≥10 s 的连续播放**做样本。

### 7.4 `att` 不上报（可观测性缺口）

- 设备 `stats` 只带 `aecRef`（`hw|sw|off`，`stats.go:69-73`），`att` 只写本地日志；`SendLog` 全仓只有 3 处调用（`cmd/server.go:540,543,858`），不含 `[aec]`。
- 因此控制器（`device_link.py` 的 `kind=="log"` 分支）在 `log_level=info` 下**看不到任何 AEC 行**；`.34` 当前就是 `info`。
- 短期：Lead 临时把 `log_level` 调 `debug` 只能看到 `SendLog` 的三类消息，**看不到 `att`**——`att` 必须走 adb。
- 长期：F3（把 `att` 塞进 stats/SendLog）。**没有 F3，AEC 调优不可规模化。**

---

## 8. 建议执行顺序（按信息增益/成本排序）

1. **先修测量**：解决 §7.3 的断连；建立 §7.2 的录音 A/B 脚本；确认 A3/A4 两条启动日志。（半天）
2. **D 组**（1 h）：一次性证明 tail/delay/AGC/barge 无效，写进结论，停止再试。
3. **A2**：`aecRefSource=hw` 锁死（防止 auto 检测失败悄悄退回 sw）。
4. **B1**：`wakeMic` 0–6 扫描 —— 唯一"只推配置"就可能拿 dB 的项；选 att 最高且语音不掉的麦。
5. **E1 诊断**：127 vs 117 vs 107，判断是"混响主导"还是"失真主导"；结果决定下一步。
6. **F2+F1+F3**（需固件）：1 路 + 256 ms + att 上报，重测。这是唯一可能到 20–24 dB 的组合。
7. **E2/E3**（若 E1 指向失真）：调 loudness trim/lowcut，**需 9 哥拍听感**。
8. **只有在 att ≥ 20 dB 后**才讨论"设备侧 barge-in"（§5.4），否则保持 `barge_in: "wake"`。
9. 若 F1 证明 tail 不是瓶颈（att 不动）⇒ 设备端上限由物理耦合/失真决定，**接受 18 dB + 控制器 gate**，或退回 `ref=1` 级联（§5.2）。

---

## 附录 A：关键常量/默认值（源码核实）

| 项 | 值 | 位置 |
|---|---|---|
| 采样率 / 帧 | 16000 Hz / 512（32 ms） | `aec.go:48-51` |
| hw tail | **64 ms 固定**（1024 taps） | `aec.go:70` |
| sw tail | `aecTailMs` 默认 300，clamp 50–500 | `config.go:240`、`aec.go:59-60` |
| delay | `aecDelayMs` 默认 0，clamp 0–1000 | `config.go:239`、`aec.go:58` |
| 麦数 | 7（ch0–6），ch7/8 回采 | `aec.go:74`、`beamformer.go:14-28` |
| 每帧 AEC 成本 | 6.04 ms / 32 ms ≈ 19% 单核（7 麦） | `aec.go:104-105` |
| micGain | 默认 24，clamp 0–42；本项目 18 | `config.go:228, 627-639` |
| AEC 时 mic 抽取 | `gainLin / 2`（6 dB headroom），AEC 后 ×2 makeup | `data.go:374-393, 1260-1311` |
| AGC | target 0.08、max 20×、min 0.5、attack .05、release .005；**仅 lockMic** | `processor.go:22-29`、`data.go:1226-1227` |
| 设备 VAD | 0.004 / 80 ms / 600 ms；阈值以 pre-gain 单位校准 | `config.go:211-213`、`data.go:1324-1325` |
| barge | 设备默认 0.05（控制器侧的默认已移到 0.25） | `config.go:219`、`controller/em_barge.py:1-60` |
| adcMicpga / adcDigitalGain | 40 / 88（Amazon 工厂值，原始控制索引，不是 dB） | `config.go:226-227` |
| saved path | `saveMinDb=12`、`saveEvery=24h`、`…/aec_echo_path.v2.bin(.mics)` | `persist.go:18-24, 194-204` |
| dealias | 95 taps、截止 7.2 kHz、环 1.37 s；raw 1 dB vs 重建 25.7 dB（粉噪） | `dealias.go:1-30, 70-75` |

## 附录 B：一句话回答 9 哥的三个问题

1. **为什么只有 ~10 dB？** 短回复的冷启动 + 双讲把 `att` 拉低了；本房间在 127 音量的**收敛带 14–18.2 dB**。再往上被三件事按住：64 ms 的硬件 tail 覆盖不了客厅混响（bench 20.4 dB 是近无混响条件）、127 音量下小喇叭的非线性失真（线性滤波器原理上消不掉）、以及中心麦 ch6 的回声耦合最强。
2. **理论能调到多少？** 只推配置：**14 → 17 dB 量级**（估算，主要靠换麦 `wakeMic`）。加固件（1 路 + 256 ms tail）：**20–24 dB**（估算，未实测）。要到"127 音量免 gate 无自听"所需的 **31 dB**，本硬件/算法做不到；降到 ~107 音量后 21 dB 就够，这才进入可达区。
3. **哪些参数影响自听？** 真正影响的只有：**播放电平/频谱（最大）、hwTail（固件）、`wakeMic`、`micGainDb`（削波）**；`aecTailMs/aecDelayMs/agcEnabled/bargeIn*/beamAngle/vad*` 在本拓扑下**全部无效**（源码级确认，见 §4.4）。
