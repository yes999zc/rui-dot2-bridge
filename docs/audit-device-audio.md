# Echo Dot 2 音频链路只读审计

> 审计对象：`G090LF0964340RN6`（Echo Dot 2 / `biscuit`，FireOS 5.1.1，EchoMuse **v2.18.1**）
> 审计时间：2026-10-24 09:20–09:40 CST（设备日志为 UTC，+8 h 为本地时间）
> 现场状态：rui 控制台音量 **255/255**，设备 `volume_set` 后为 **127/127**；播放正常（`periods=461 underruns=0`）
> 约束：只读。未改任何代码/配置，未重启 rui 或设备，未改音量，未做 docker/git 写入，未播放/注入音频。
> 说明：`/tmp/echomuse-inspect` 与 `/tmp/echomuse-p3b` 的源码是 **2.27.0-ea.1（HEAD）**，不是设备上的 v2.18.1；两者在音量/输出链上的行为按项目文档自 2026-09-24 起未变，且现场 tinymix/日志与该行为一致。因此下文源码行号是 HEAD 的行号，不能当作 v2.18.1 的逐行证据；现场值优先。

---

## 1. 结论摘要（按可能性排序）

| # | 结论 | 置信度 | 性质 |
|---|---|---|---|
| 1 | **127/127 真的是 0 dB，数字链路没有隐藏的固定衰减。** 软件音量、DAC、HP 驱动增益、声道复制、mix、路由逐项都是 0 dB；当前链路在 1 kHz 上就是平的。 | **高（已证实）** | 排除“额外衰减” |
| 2 | **主因：缺少 stock FireOS 在 100% 音量时施加的播放 EQ（`EQ_100.cfg`）。** 按设备自带系数直接计算：1 kHz **+13.70 dB**、150 Hz **+39.91 dB**、白噪 RMS **+18.94 dB**；对 `ack.wav` 的 A 计权增益约 **+13.6 dB**（仅 EQ），叠加简化 MBCL 后约 **+9.8 dB A 计权 / +14.0 dB RMS**。EchoMuse 的 EQ 是平的，所以相对 stock 相当于少了这一级增益，而不是被多衰减了一级。 | **高（系数已证实；听感差额为推算）** | **主因 / 缺失增益级** |
| 3 | **当前 rui 模式下，设备侧输出链是 bypass。** rui adapter 的 `controller_features` 为空，不声明 `output_chain`；设备在 18:56:15 把链交回 controller，此后未切回。因此 `em-config.json` 里的 EQ / bass guard / limiter 参数只是“存着”，**当前不生效**。这排除了 bass guard/limiter 作为“吃 dB”的原因，也说明现在连 EchoMuse 自己的部分处理都没有。 | **高（已证实）** | 排除 DSP 衰减 |
| 4 | **源电平本身不高，且没有 makeup gain。** `ack.wav` 峰值 −4.55 dBFS、RMS −19.0 dBFS；真实回复的硬件 loopback RMS 实测 −26.3…−23.3 dBFS。stock 的 EQ 正是用这段 headroom 把声音推上去；EchoMuse 原样送出。 | **中（RMS 实测，峰值推算）** | 内容/源级因素 |
| 5 | **当前配置的模拟上限是 stock 的 0 dB，不是 codec 的硬件上限。** `HP Driver Gain Volume = 6`，在 0…35 → −6…+29 dB 的映射里正好是 **0 dB**；stock 内放路径也是 6。codec 还有 **+29 dB** 模拟增益未用，但超过 stock 后扬声器失真/寿命未测。`Ext_Amp_Gain 6dB` 经实测是 inert（6/12/18/24 dB 扫动输出变化 0.0 dB）。DAC 数字 127 以上会削波（153 时 65% THD），不是可用余量。 | **高（数值）；中（听感/安全）** | 配置上限 |
| 6 | **路由、静音、jack、start_server 初值均已排除。** h2w=0，内放路由，`Ext_Speaker_Amp_Switch=On`，`Audio_DacMux_Setting=Off`，HP gain 6，PCM 127；`MFP Gpio Mute=On` 在 Dot 2 上“drives nothing”；`start_server.sh` 的 PCM 100（−13.5 dB）已被 `PcmSpeaker.Init` 的 127 覆盖。 | **高（已证实）** | 排除 |
| 7 | 仍未排除：**个体差异（#630，“some Dots and not others”）**、biquad 全 0 vs stock 非 0 的音色差、以及绝对 SPL 未标定。 | 低/中 | 待补证 |

**一句话：** 不是“127 被偷偷衰减了”，而是 **“127 只是 unity，stock 在 unity 之上还有一级 +10~14 dB 的播放 EQ；EchoMuse 没有这一级，而且当前连自己的输出链都没开”**。所以物理最大输出偏小，主要是**缺失的 stock 增益级 + 小扬声器本体**，不是软件侧额外衰减。

---

## 2. 逐条证据

### 2.1 127/127 = 0 dB：每一级都对得上

#### (a) 音量律：0.5 dB/step，127 = unity

- `device/internal/bindings/speaker/swvolume.go:21-33`：
  ```go
  // The law is the DAC control's own — 0.5dB per step, unity at 127 ...
  func VolumeGain(level int) float64 {
      if level <= 0 { return 0 }
      if level >= 127 { return 1 }
      return math.Pow(10, float64(level-127)/40)
  }
  ```
  `(level-127)/40` 对应每步 `20*log10(10^(1/40)) = 0.5 dB`；127 时正好 1.0（0 dB）。
- `device/internal/server/volume.go:14-20`：`volumeMax = 127 // unity gain`，注释明确“0.5dB per step, 0dB at 127”。
- `device/internal/bindings/mixer/mixer.go:28`：`PlaybackVolume = "PCM Playback Volume" // DAC digital volume, 0.5dB steps, 127 = 0dB`。
- `device/CLAUDE.md:1236-1252`：tlv320aic32x4 DAC 数字音量 176 步、0.5 dB、−63.5…+24 dB，**0 dB 在 index 127**；stock FireOS 根本不写这个 control，复位就是 0 dB。127 以上是正增益，153 时 65% THD、170 时 89% THD，所以 firmware 把上限钉在 127。

#### (b) 现场 tinymix：DAC 与模拟级都在 unity/stock 值

2026-10-24 09:26 CST 只读 `tinymix`（完整副本 `/tmp/tinymix-dot2.txt`）：

```
5   ENUM 1 Ext_Speaker_Amp_Switch          On
7   ENUM 1 Headphone_Speaker_Mux           Speaker
13  ENUM 1 Ext_Amp_Gain                    6dB
57  ENUM 1 Audio_DacMux_Setting            Off
58  ENUM 1 Audio_LineOut_Setting            Off
61  INT  2 PCM Playback Volume              127 127
62  INT  2 HP Driver Gain Volume            6 6
63  INT  2 LO Driver Gain Volume            6 6
64  BOOL 2 HP DAC Playback Switch           On On
65  BOOL 2 LO DAC Playback Switch           Off Off
67  ENUM 2 DRC Control                      Disabled Disabled
86  ENUM 1 Right Channel Only               Off
87  ENUM 1 Ignore Ramp Up                   Off
88  ENUM 1 MFP Gpio Mute                   On
164 ENUM 1 Board Channel Config             MonoRight
234 BOOL 1 HPR Output Mixer R_DAC Switch    On
237 BOOL 1 HPL Output Mixer L_DAC Switch    On
```

关键点：

- **PCM 127 127**：DAC 数字 0 dB（见 2.1a）。
- **HP Driver Gain 6 6**：`device/internal/bindings/speaker/jackrouting.go:41-62` 明确 “0..35 range that maps to -6dB..+29dB”，`hpGainInternal = "6"`；6 对应 **0 dB**。stock 内放路径也是 6（`/system/etc/audio_device.xml:15`）。所以这不是“被调到很低”，而是 stock 的 0 dB。
- **HP DAC On On / HPL+HPR Output Mixer On**：输出级已接通，不是“只开了一路”或“DAC 没接”。
- **Ext_Speaker_Amp_Switch On**：内放 amp 使能。
- **Audio_DacMux_Setting Off**：stock 的内放路径同样写 Off（`audio_device.xml:13`），不是 jack 的 On。
- **LO DAC Off / Audio_LineOut Off**：没有走 line-out。
- **DRC Disabled**：没有额外的 DRC 压缩。
- **Right Channel Only Off**：stock 内放路径写 On（`audio_device.xml:14`），但 EchoMuse 故意不复制这个值；见 2.1d。
- **MFP Gpio Mute On**：见 2.5，Dot 2 上这个 control “drives nothing”，不是静音。

#### (c) 现场日志：`Volume set to 127/127`，且 DAC 在 Init 时被拉到 127

`/tmp/server.log`（副本 `/tmp/dot2-server.log`）：

```
1693  2026/10/24 01:22:14 [control] Registered as G090LF0964340RN6 (version v2.18.1)
1695  2026/10/24 01:22:14 Volume set to 127/127
1723  2026/10/24 01:23:00 [speaker] voice stream complete — returning to silence (periods=461 underruns=0 minDepth=-1 primeWait=38ms recvSpan=19366ms maxGap=230ms)
1734  2026/10/24 01:23:19 [speaker] voice stream complete — returning to silence (periods=55 underruns=0 minDepth=-1 primeWait=6ms recvSpan=2034ms maxGap=188ms)
```

- `pcm_speaker.go:324` 先 `PCM Playback Volume 0` 静音，`:349` 再写 `dacUnity = "127"`（`:866-870` 明确 127 是 0 dB index），`:347` 打开 `Ext_Speaker_Amp_Switch`。所以“127”不是只写在软件音量控制器里，DAC 也确实是 127。
- 启动时 `server.log:16` 的 `Volume controller initialised at 100/127` 是软件音量控制器的初值（`server/volume.go:37-39`，`volumeBoot=100`），随后被 controller 的 `volume_set` 改成 127。**当前生效的是 127，不是 100。**
- rui 控制台的 255 刻度不是第二个增益级：`src/rui_dot2_bridge/adapter.py:182` 是 `round(self._link.volume_level * 255 / 127)`，255 ↔ 127；设备只收到一个 `volume_set level=127`（`device_link.py:435-437`），不是 255 和 127 级联。

#### (d) 单声道复制 / mix：没有 −6 dB

- 协议：`src/rui_dot2_bridge/device_link.py:414-419` 和 `frames.py` 明确下行是 **48 kHz mono S16**；`frames.py:58-65` 的 `split_playback` 只切帧，不做 gain。
- 设备侧 `pcm_speaker.go:703-712` 的 `toStereo` 把 mono **原样复制到 L/R 两声道**：
  ```go
  period[i*4+0], period[i*4+1] = lo, hi // L
  period[i*4+2], period[i*4+3] = lo, hi // R
  ```
- `outchain/chain.go:71-75` 的注释和 `Process`/`ProcessFloat`（`:222-242`）都对 L/R 取 `(l+r)/2`；因为两声道完全相同，这个平均是精确的，**不是 −6 dB**。
- `speaker/mix.go:103-106`：`case music == nil:` 时直接返回 voice，“Voice alone is never ducked”。duck 只作用于 music（`duckDb=-18` 在 `em-config.json` 里，但当前无 music，且 voice 不被 duck）。
- 结论：声道复制、混音、duck 都没有吃 dB。

#### (e) 输出链当前是 **bypass**（不是“开着但衰减”）

这是本次审计里容易误判的一点，必须单独说清：

- rui adapter 的 `controller_features` 是空：`src/rui_dot2_bridge/device_link.py:402-404`：
  ```python
  def controller_features(self) -> tuple[str, ...]:
      """What this controller can do. Empty until a feature is actually implemented."""
      return ()
  ```
- 设备只在 controller 的 ack 里带 `output_chain` 时才在设备侧跑链：
  - `device/cmd/server.go:555-557`：`pcmSpeaker.SetOutputChainActive(controlClient.HasFeature(client.FeatureOutputChain))`
  - `device/cmd/server.go:612-616`：每次 config push 重新读 ack；注释明确“a reconnect can land on a controller that does not hand the chain over”。
  - `device/internal/client/control.go:1556-1559`：`FeatureOutputChain` “announced by a controller that sends this device's audio UNPROCESSED and leaves EQ, bass guard and limiter to the device. Absent, the controller is still processing and the device must not.”
  - `device/internal/bindings/speaker/pcm_speaker.go:756-769`：状态变化才打印 `[speaker] output chain now runs on %s`。
- 现场日志的链状态变化：
  ```
  41   2026/10/23 17:47:50 [speaker] output chain now runs on this device
  282  2026/10/23 18:31:55 [speaker] output chain now runs on the controller
  544  2026/10/23 18:54:30 [speaker] output chain now runs on this device
  597  2026/10/23 18:56:15 [speaker] output chain now runs on the controller
  ```
  18:56:15 正是 rui（`192.168.5.18:8870`）接管的时刻；此后 01:22:14 重连没有再打印，因为状态已经是 `false`。**所以当前设备侧 EQ / bass guard / limiter 全部是 passthrough。**
- `em-config.json` 里的 `applied.output`（`Bands=[0×8]`、`Loudness=false`、`GuardEnabled=true`、`GuardDb=-30`、`LimiterEnabled=true`、`LimiterThresholdDb=-1`）只是配置快照；因为 `SetOutputChainActive(false)`，这些参数不进入处理。**`guard_reduction=20.00dB`（`server.log:106`）是 17:51:38 旧 controller 会话的历史值，不是当前状态。**
- 这排除了 bass guard/limiter 作为“当前吃 dB”的原因；也说明当前路径连 EchoMuse 自己的部分 DSP 都没有。
- 这是一个协议层的空档：设备按 ack 认为 controller 在处理（`control.go:1556-1559`），但 rui adapter 没有实现输出链；所以**两边都没处理**。对响度而言这不会加减 dB（链本身没有 boost），但意味着当前听到的是最原始的 TTS。

#### (f) 如果链是开的，它也只切低频/峰值，不会在 1 kHz 吃 dB

即使把输出链打开，按代码：

- EQ：`em-config.json` 的 `eqBands=[0,0,0,0,0,0,0,0]`、`eqLoudness=false`；`outchain/eq.go` 的 `isFlat` 为真时 `sections=nil`，是纯 passthrough。**0 dB。**
- Bass guard：`outchain/bassguard.go:5-10` 是 stock `MBCL.cfg` band 1，20:1 from −50 dBFS，release 200 ms；`:59-82` 只处理 **115 Hz 以下**的低通分量，`GuardDb=-30` 是 floor。1 kHz 不在这个频段。`/tmp/em-config.json` 的 `bassGuardDb=-30` 比 stock 的 `-40` **更不激进**（`/system/vendor/etc/audio-algorithms/MBCL.cfg:47-54`）。
- Limiter：`outchain/limiter.go:59-64`，threshold 默认 −1 dBFS，5 ms lookahead；只有峰值超过 −1 dBFS 才压，且最多压到 −1 dBFS。**不是固定衰减。**
- 结论：即使链开着，1 kHz 仍是 0 dB；`ack.wav` 峰值 −4.55 dBFS，limiter 不会动它。当前链是 bypass，则连这 ≤1 dB 的峰值限制也没有。

#### (g) 127/127 的 dBFS 账

| 环节 | 1 kHz 增益 / 电平 | 依据 |
|---|---:|---|
| TTS 源（绝对电平，不是增益） | `ack.wav` 峰值 **−4.55 dBFS**、RMS **−19.0 dBFS**；真实回复 loopback RMS **−26.3…−23.3 dBFS** | Python 分析；`server.log:1716-1722` |
| adapter / 帧切分 | **0.0 dB** | `frames.py:58-65`、`device_link.py:414-419` |
| mono → stereo 复制 | **0.0 dB** | `pcm_speaker.go:703-712` |
| voice/music mix、duck | **0.0 dB**（voice 不被 duck） | `mix.go:103-106` |
| 输出链 EQ（当前 bypass；若开也 flat） | **0.0 dB** | `em-config.json`；`outchain/eq.go` |
| bass guard（当前 bypass；若开只切 <115 Hz） | **0.0 dB @1 kHz** | `bassguard.go:59-82` |
| limiter（当前 bypass；若开只压 >−1 dBFS 峰值） | **0.0 dB @ −4.55 dBFS** | `limiter.go:59-64` |
| 软件音量 127 | **0.0 dB** | `swvolume.go:21-33`；`server.log:1695` |
| DAC 数字 127 | **0.0 dB** | `mixer.go:28`；`device/CLAUDE.md:1236-1252`；tinymix |
| HP Driver Gain 6 | **0.0 dB**（0…35 = −6…+29 dB） | `jackrouting.go:41-62`；tinymix；`audio_device.xml:15` |
| Ext_Speaker_Amp_Switch On | amp 使能，不是增益 | tinymix；`pcm_speaker.go:347` |
| **合计（1 kHz）** | **0.0 dB** | — |

- 因此：**0 dBFS 输入 → 0 dBFS 到 DAC（当前 bypass）；若链开，峰值被 limiter 限到 −1 dBFS。** `ack.wav` 的 −4.55 dBFS 峰值原样到 DAC。
- **绝对 SPL 不能由 dBFS 推出来**：DAC 之后是 HP 驱动 0 dB + amp + 小扬声器；没有校准麦克风/SPL 计，本次无法给 dB SPL。dBFS 账只能证明“数字链路没有额外衰减”。

---

### 2.2 主因：stock 的 `EQ_100` 播放 EQ 缺失

#### (a) stock 的播放链是什么

设备上的 stock 配置（只读 `cat`）：

- `/system/vendor/etc/audio-algorithms/AFE.cfg:86-92`：Playback 链是
  ```
  AVL (Automatic Volume Leveling) -> UserEQ -> EQ (Equalizer FIR) -> MBCL
  ```
- `AFE.cfg:760-764`：`Equalizer FIR` 的 `Bypass=false`，六档 `EQ_50…EQ_100`，按 `Volume Boundary [50,60,70,80,90,100]` 选择；**100% 时用 `EQ_100.cfg`**。
- `AFE.cfg:729-742`：`AVL` 不 bypass，`AVL_TTS_volTab` 在最大档是 **0 dB**（最后一项 0），所以 100% 时 AVL 不再衰减。
- `/system/vendor/etc/audio-algorithms/UserEQ.cfg`：三段 gain 全 0，Bypass=false，**平直**。
- `MBCL.cfg:31-33`：`Bypass=false`，`inVol=0`；band 1（0–115 Hz）20:1 from −50 dBFS、gainMin −40；band 2/3（115–500、500–7500 Hz）2:1 from −10 dBFS、gainMin −40。MBCL 是**压缩/限幅**，不是增益级；它会把 EQ 推上去的一部分收回来，但不会把 EQ 的增益全部抵消。

> 这不是 EchoMuse 的代码，是 stock FireOS 的 vendor DSP。EchoMuse 直接接管 codec、停掉 `media`/`mixer`，所以这条链**根本不会运行**。

#### (b) `EQ_100.cfg` 的实测响应（按设备自带系数直接算）

从设备 `cat /system/vendor/etc/audio-algorithms/EQ_100.cfg` 取 1024 个 FIR taps，离线做 `H(f)=Σ h[n]e^{-j2πfn/fs}`（fs=48 kHz）。结果：

| Hz | EQ_100 增益 | Hz | EQ_100 增益 |
|---:|---:|---:|---:|
| 50 | +18.0 dB | 700 | +13.8 dB |
| 80 | +24.6 dB | 1000 | **+13.7 dB** |
| 100 | +33.8 dB | 1400 | +11.6 dB |
| 125 | +38.5 dB | 2000 | +5.8 dB |
| 150 | **+39.9 dB** | 2800 | −1.1 dB |
| 200 | +39.3 dB | 3150 | +2.8 dB |
| 250 | +33.6 dB | 3500 | +1.8 dB |
| 350 | +26.0 dB | 5000 | +10.2 dB |
| 500 | +16.4 dB | 8000 | +16.8 dB |

校验：`EQ_100` / `EQ_50` 的系数比是 **5.334838**（+14.54 dB），与项目文档 `SETUP.md:685-690` 的 “EQ_100 is EQ_50 × 5.334838 exactly” 一致；`EQ_50` 在 150 Hz 算得 +25.4 dB，和 `SETUP.md:692-699` 表的 +26.2 dB 在频率分辨率误差内一致。说明解析/缩放是对的，**不是把文件读错或把 Q15 当成了别的格式**。

#### (c) 对 `ack.wav` 的 A 计权/电平影响（离线推算）

用同一份 `EQ_100.cfg` 卷积 `/Users/bakeyzhang/AI_Workspace/01_Code/projects/rui-dot2-bridge/dev/rui/data/ack.wav`（16 kHz → 48 kHz）：

| 处理 | RMS | 峰值 | A 计权 RMS |
|---|---:|---:|---:|
| flat（EchoMuse 当前路径） | −19.00 dBFS | −4.51 dBFS | −19.15 dBFS |
| 仅 `EQ_100` | −0.15 dBFS | +8.61 dBFS | −5.53 dBFS |
| `EQ_100` + 简化 MBCL（band1 20:1@−50/gainMin−40，band2-4 2:1@−10/gainMin−40，峰值限 0 dBFS） | −4.98 dBFS | 0.00 dBFS | −9.34 dBFS |

- **仅 EQ 的 A 计权增益：+13.62 dB**；RMS 增益 +18.85 dB。
- **叠加简化 MBCL 后：A 计权 +9.81 dB；RMS +14.02 dB。**
- 表中 flat 峰值 −4.51 dBFS 是 16→48 kHz 重采样后的值；原始 16 kHz 文件是 −4.55 dBFS（差 0.04 dB，不影响结论）。
- 这里的 MBCL 是简化模型（4 段 LR4 分频 + 包络压缩），不是 vendor DSP 的逐样本实现；**+9.8 dB A 计权 / +14.0 dB RMS 应视为“数量级估计”，不是实测**。但 EQ 系数是设备原文件，+13.7 dB @1 kHz 是确定的。
- `ack.wav` 的能量分布：0–115 Hz **0.0%**、115–300 Hz 2.3%、300–700 Hz 4.6%、**700–2000 Hz 89.0%**。所以对这段“在呢”，A 计权差额主要来自 1 kHz 的 +13.7 dB，而不是低频。
- 对真实回复，loopback RMS 更低（−26…−23 dBFS），EQ_100 的 1 kHz +13.7 dB 会保留更多（MBCL 的 2:1 只在 −10 dBFS 以上才压），**净差额更接近 +12…+14 dB**，而不是更小。

#### (d) EchoMuse 侧没有这一级

- EchoMuse 的输出链 EQ 是 **用户 8 段 EQ**（`outchain/eq.go`），不是 stock 的 1024-tap FIR；当前 `em-config.json` 的 `eqBands` 全 0、`eqLoudness=false`，所以是平的。
- 8 段 EQ 的调节范围是 ±12 dB（`JOURNAL.md:1552`：“Our eight-band EQ cannot express the other half: it needs +26dB against a ±12dB range”），**无法表达** stock 在 150–250 Hz 的 +26…+40 dB。即使把 8 段拉满，也补不回 stock 的低频提升。
- `responseLevel` 是 voice 的相对增益：`config.go:416-429` 的 low/medium/high = 0/+6/+12 dB；但 `responsegain.go:50-58` 的 `cappedResponseGain` 会在 `volume=1.0`（127）时把请求压到 **ceiling = 1/volume = 1.0**。所以**在 127 时把 `responseLevel` 改成 high 也不会增加输出**；它只在低音量时补偿。当前 `em-config.json` 是 `responseLevel=low`。
- `docs/faq.md:455-465` 已经把这个现象写成已知问题：
  - “The internal speaker is quieter than stock. Reported on some Dots and not others of the same model (#630); there is no setting for it yet.”
  - “What remains is that stock applies a substantial driver correction we don't yet — #247.”
- `SETUP.md:674-706` 和 `JOURNAL.md:1552,1584` 记录了同一结论：stock 的 1024-tap FIR 在 150–250 Hz 提升约 +26 dB、2.8 kHz 挖 −15 dB；EchoMuse 只实现了“删除 <115 Hz”的 MBCL 半边，没有实现“提升 150–250 Hz”的 FIR 半边，所以输出会 thin/quiet。
- 驱动器本身也确实是弱的：`device/tools/speaker_profile/README.md:65-74` 测得 **250 Hz 比 1 kHz 低 −18 dB**（三个摆位 0.8 dB 内一致），3.15 kHz 有 +8…+9 dB 峰；`JOURNAL.md:5134` 的独立测量给出 140 Hz −31 dB、175 Hz −25 dB、250 Hz −17 dB。stock 的 +39.9 dB @150 Hz 正是在补这个坑；EchoMuse 平直响应留下的是“薄 + 小”。

#### (e) 结论

**相对 stock 在 100% 音量，EchoMuse 在 127 时少了约 10 dB（A 计权，保守估计）/ 14 dB（RMS）的播放 EQ 增益。** 这是“声音小”的最大可解释项，且与项目自己的 #247/#630 记录吻合。它不是“软件侧额外衰减”，而是“stock 有一个 EchoMuse 没有的增益级”。

---

### 2.3 源电平：不是衰减，但确实有 headroom 没用上

- `ack.wav`（rui 启动时 xfyun TTS 生成的“在呢。”）：
  - 16 kHz mono，0.49 s
  - 峰值 **−4.55 dBFS**
  - RMS **−19.00 dBFS**
  - 活跃帧 RMS **−20.32 dBFS**
  - 0–115 Hz 能量 0.0%，115–300 Hz 2.3%，700–2000 Hz 89.0%
- 真实回复的播放电平（设备 AEC 的硬件 loopback `ref`，ch8，post-volume）：
  ```
  2026/10/24 01:22:51 [aec] att=14.8dB mic=7068 out=1286 ref=2003 src=hw(ch8) on=ch6 frames=814192
  2026/10/24 01:22:52 [aec] att=14.5dB mic=5686 out=1071 ref=1578 ...
  2026/10/24 01:22:53 [aec] att=13.1dB mic=6296 out=1389 ref=1752 ...
  2026/10/24 01:22:55 [aec] att=16.7dB mic=5828 out=851  ref=1612 ...
  2026/10/24 01:22:56 [aec] att=10.8dB mic=5764 out=1667 ref=1775 ...
  2026/10/24 01:22:57 [aec] att=15.6dB mic=6418 out=1071 ref=1678 ...
  2026/10/24 01:22:59 [aec] att=13.1dB mic=7722 out=1710 ref=2236 ...
  ```
  `ref=1578…2236` / 32768 = **−26.3…−23.3 dBFS RMS**。`src=hw(ch8)` 是硬件 loopback，按 `device/CLAUDE.md` 的说明是“every third sample of the 48kHz output”，post-volume，pre-DAC；volume 127 时就是 PCM 本身。它不是麦克风，不能拿 `mic=` 当播放电平。
- adapter/rui 没有在送出前加 gain：
  - `frames.py:58-65` 只切帧；
  - `adapter.py:280-300` 只做 `channels==1`/`sample_rate==48000` 校验和 buffering；
  - rui 的回复路径 `house-ear/src/house_ear/audio/process.py:1116` 是 `np.round(np.clip(audio,-1,1)*32767.0)`，没有固定 gain。
- 因此：**源本身不是 0 dBFS 满幅**。`ack.wav` 峰值只有 −4.55 dBFS，真实回复 RMS 只有 −26…−23 dBFS；如果按 12–18 dB 峰值因子估算，峰值大约 −14…−5 dBFS（这是**推算**，loopback 只给 RMS，没给 peak）。这留下了 5–14 dB 数字 headroom。stock 的 EQ_100 正是把这段 headroom 用掉；EchoMuse 原样送出。
- 这不叫“音量链路衰减”，而是“源/内容电平 + 没有 makeup gain”。它和 2.2 的缺失 EQ 是叠加的。

---

### 2.4 模拟/硬件：当前是 stock 的 0 dB 上限，不是 codec 的硬件上限

- **HP Driver Gain Volume = 6**，映射 0…35 → **−6…+29 dB**（`jackrouting.go:41-62`），所以 6 = **0 dB**。stock 内放路径也写 6（`audio_device.xml:15`）；jack 路径写 11（+5 dB）。当前 6 是 stock 内放值，不是“被调低”。
- **+29 dB 模拟余量存在**：`device/CLAUDE.md:1262-1264` 记录 `HP Driver Gain Volume` 是 live 的，“+18dB commanded → +18.1dB actual, THD 2.25%”，但“that is a taste call to make by ear, and the speaker's behaviour above stock level is unmeasured”。所以硬件/codec 本身还能更响；当前配置只是没有用这段余量。
- **Ext_Amp_Gain 6dB 是 inert**：`device/CLAUDE.md:1258-1261` 实测“sweeping its full 6/12/18/24dB range moves the output 0.0dB while still reading its new value back”。所以它不是可用的 +6/+12/+18/+24 dB 档位。
- **DAC 数字 +24 dB 不可用**：127 以上是正增益，153 时 65% THD、170 时 89% THD，输出从 153 往上已经不再变响（`device/CLAUDE.md:1236-1247`）。firmware 把 `volumeMax` 钉在 127 是正确的。
- **Dot 2 扬声器本体**：250 Hz 比 1 kHz 低 −18 dB，140 Hz −31 dB，3.15 kHz 有 +8…+9 dB 峰（`speaker_profile/README.md:65-74`、`JOURNAL.md:5134`）。这是一个小尺寸、低频很弱的 driver；stock 的 EQ 是在补它，EchoMuse 没有补。
- **绝对 SPL 未标定**：本次没有 SPL 计/校准麦克风；设备自己的 mic 还在 clipping（`server.log:1726` 的 `clipped=14087`；rui 的 `voice_peak_db=-0.6` 是 mic peak，不是播放电平），不能用来反推 SPL。所以“物理最大输出是多少 dB SPL”目前**没有实测答案**。
- **个体差异**：`docs/faq.md:455-458` 明确 “Reported on some Dots and not others of the same model (#630)”。这不是软件链路的解释，但可能是本机额外偏小的硬件因素；需要第二台同型号对比。

**结论：** 当前 127/127 的上限 = stock 的 0 dB 模拟增益 + 平直数字链 + 小扬声器；**不是 codec/功放的硬件硬顶**。codec 还有 +29 dB 模拟增益，但那是超过 stock 的用法，失真/寿命未测；不能把它当作“本来应该默认开”的增益。

---

### 2.5 路由 / 静音 / jack / start_server：逐项排除

| 项 | 现场值 / 证据 | 结论 |
|---|---|---|
| 3.5 mm jack | `/sys/class/switch/h2w/state = 0`；`server.log:19` `[jack] ... (state=0)`；`:21` `[speaker] jack routing applied (internal)` | **未插入**，没有走线路输出 |
| 内放 amp | tinymix `Ext_Speaker_Amp_Switch On`；`pcm_speaker.go:347` Init 时写 On | amp 已开 |
| jack mux | tinymix `Headphone_Speaker_Mux Speaker`；`Audio_DacMux_Setting Off`（stock 内放路径也 Off） | 内放 |
| DAC/输出级 | `HP DAC Playback Switch On On`；`HPL/HPR Output Mixer ... On`；`LO DAC Off`；`Audio_LineOut_Setting Off` | HP 输出级已接通，line-out 关闭 |
| MFP Gpio Mute | tinymix `On`；但 `docs/boards.md:228-231`：“The Dot 2 has a control of the same name that drives nothing.” `hardware.go:129` 的 biscuit `Speaker` 没有 `AmpMute`（radar 才有，`:147`） | **不是静音**；若它真的 mute，会是无声而不是小声 |
| Right Channel Only | tinymix `Off`；stock 内放路径写 `On`；`jackrouting.go:74-80` 解释：wire 是 mono，`toStereo` 复制 L→R，两个声道样本相同，所以这个 control 不改变电平 | 不是 −6 dB |
| `start_server.sh` PCM 100 | `/data/local/bin/start_server.sh:55`：`tinymix -D 0 "PCM Playback Volume" 100 100`（100 = −13.5 dB）；但 `pcm_speaker.go:324,349` 在 Init 时先 0 再 127，现场 tinymix 是 127 | **当前不是 100**；只有在 server 没走到 Init 时才可能停在 100/0。当前日志显示 Init 成功 |
| `amp_off` | `start_server.sh:166-168`：server 退出时 PCM 0 + amp Off | 那是静音，不是小声；当前 server 在跑 |
| AEC | `[aec] enabled: frame=512 tail=300ms delay=0ms`（`server.log:17`）；`em-config.json` `aecEnabled=true` | AEC 开，但不影响播放增益 |
| DRC | tinymix `DRC Control Disabled` | 没有额外压缩 |
| biquad | tinymix `biquad coefficients` 全 0；stock `audio_device.xml:10` 是非 0 的 117 字节 | 这是一个**未量化**的音色差；但主因是 1024-tap `EQ_100`，不是这 117 字节。全 0 在 radar 文档里是“silence”，Dot 2 上音频 audible，说明它不在主路径或为 bypass |

---

### 2.6 小贡献/已排除的 DSP

- **Bass guard**：当前 bypass；即使开，也只切 <115 Hz。`server.log:106` 的 `guard_reduction=20.00dB` 是 **2026-10-23 17:51:38 music** 的旧 controller 会话值，不是当前 voice；`ack.wav` 0–115 Hz 能量 0.0%，所以对这段语音几乎没有影响。`GuardDb=-30` 还比 stock 的 `-40` 更不激进。
- **Limiter**：当前 bypass；即使开，threshold −1 dBFS，`ack.wav` 峰值 −4.55 dBFS，不会触发；最多在满幅峰值上压 ≤1 dB。不是固定损失。
- **8 段 EQ**：当前 `eqBands` 全 0、`eqLoudness=false`，flat。它是用户 EQ，不是 stock 的 driver correction；上限 ±12 dB，无法复现 stock 的 +26…+40 dB。
- **responseLevel**：当前 `low` = 0 dB；即使改 high，在 127 时被 `cappedResponseGain` 压回 1.0（`responsegain.go:50-58`），**不会增加最大输出**。
- **VAD/mic clipping**：`server.log:1726` 的 `clipped=14087` 和 rui 的 `voice_peak_db=-0.6` 是**麦克风**路径，不是扬声器；它们说明 mic 很热，不能用 mic 反推 SPL，但与“音箱声音小”不是同一件事。

---

## 3. 已证实 vs 推测

| 判断 | 状态 | 证据 |
|---|---|---|
| 127 = 0 dB，0.5 dB/step | **已证实** | `swvolume.go:21-33`、`server/volume.go:14-20`、`mixer.go:28`、`device/CLAUDE.md:1236-1252` |
| 软件音量在 127 时为 1.0 | **已证实** | 代码 + `server.log:1695` |
| DAC `PCM Playback Volume` 当前 127 | **已证实** | tinymix 2026-10-24 09:26 |
| HP Driver Gain 6 = 0 dB | **已证实** | `jackrouting.go:41-62` + tinymix + `audio_device.xml:15` |
| Ext_Amp_Gain 6 dB 是 inert | **已证实（项目实测）** | `device/CLAUDE.md:1258-1261` |
| 没有 jack，内放 amp On，路由正确 | **已证实** | h2w=0、`server.log:19,21`、tinymix |
| 当前设备侧输出链是 bypass | **已证实** | `device_link.py:402-404` + `server.log:597` + `server.go:555-557,615-616` |
| stock `EQ_100` 在 1 kHz +13.70 dB、150 Hz +39.91 dB | **已证实（设备系数离线计算）** | `/system/vendor/etc/audio-algorithms/EQ_100.cfg` + Python |
| stock EQ 对 `ack.wav` 的 A 计权增益 +13.6 dB（仅 EQ） | **已证实（离线计算）** | 同上 |
| 叠加 MBCL 后净增益 +9.8 dB A 计权 / +14.0 dB RMS | **推测（简化模型）** | 4 段分频 + 包络压缩近似，不是 vendor DSP 的逐样本实现 |
| 真实回复 loopback RMS −26.3…−23.3 dBFS | **实测** | `server.log:1716-1722` |
| 真实回复峰值 | **推测** | 只有 RMS，峰值因子 12–18 dB 的反推 |
| 绝对 SPL / 物理最大输出 | **未测** | 无 SPL 计/校准麦；设备 mic 在 clipping |
| 本机是否有 #630 的个体偏小 | **未排除** | `docs/faq.md:455-458`，需要第二台同型号 |
| v2.18.1 与 HEAD 2.27 的逐行一致性 | **部分** | 现场 tinymix/日志与 HEAD 行为一致；源码副本不是 v2.18.1 tag |

---

## 4. 还缺什么证据才能确认

1. **绝对 SPL（最关键）**：在 1 m、同一房间、同一 TTS 文件，分别测 stock FireOS 100% 和 EchoMuse 127 的 dB(A)/dB SPL。没有这个，只能证明“数字链 0 dB / 缺 stock EQ”，不能给“物理最大是多少”。
2. **真实 TTS 的峰值**（不只是 RMS）：目前只有 loopback RMS；需要抓 0x02 PCM 的 peak，或在设备侧加一个 peak 遥测（本次不允许改代码）。有了 peak 才能精确算 “127 时还有多少 dB headroom”。
3. **stock MBCL 的精确净增益**：本次的 +9.8 dB A 计权是简化模型；要确认，需要跑 vendor DSP 或做 stock/EchoMuse 同文件 A/B 录音。
4. **v2.18.1 源码 tag 的逐行确认**：当前源码是 2.27.0-ea.1 HEAD；需要 v2.18.1 的 `swvolume.go`/`pcm_speaker.go`/`outchain` 原文，确认没有版本差异。
5. **另一台同型号 Dot 2**：验证 #630 的个体差异；如果另一台明显更响，本机可能是硬件/扬声器个体问题。
6. **biquad 117 字节滤波器的实际作用**：当前全 0 vs stock 非 0；需要确认它是否在 Dot 2 的主路径里，以及它的频响。它不是主因，但会影响音色。
7. **8 段 EQ / responseLevel 的可用上限**：已从代码确认 responseLevel 在 127 被 cap；8 段 EQ 的 ±12 dB 无法复现 stock 的 +26…+40 dB。若要确认“拉满能到多少”，需要离线/实测 EQ 曲线。

---

## 5. 若要验证的最小只读实验（不修改任何东西）

1. **播放中的 tinymix**：在一次正常语音回复期间（用户自己说话触发，不注入音频）执行
   `adb -s G090LF0964340RN6 shell su -c 'tinymix'`，确认 `PCM Playback Volume 127 127`、`HP Driver Gain 6 6`、`Ext_Speaker_Amp_Switch On`、`MFP Gpio Mute On`、`HPL/HPR Output Mixer On`。只读，不写 control。
2. **播放中的 PCM 状态**：
   `adb ... shell su -c 'cat /proc/asound/card0/pcm23p/sub0/status'`，确认 `state: RUNNING`、`format: S16_LE`、`rate: 48000`、`channels: 2`。只读。
3. **回复的 loopback 电平**：在一次回复期间
   `adb ... shell su -c 'cat /tmp/server.log' | grep '\[aec\].*ref='`，记录 `ref=` 的 RMS；和 `ack.wav` 的 −19.0 dBFS 对比。只读。
4. **离线复算 stock EQ**：把设备上的 `EQ_100.cfg` `cat` 下来，对任意 TTS WAV 做卷积，算 1 kHz 响应和 A 计权 RMS；本次已做，可复现。只读。
5. **stock A/B（需要另一台 stock Dot 2）**：同一 WAV、同一位置、stock 100% vs EchoMuse 127，用手机 SPL 表或校准麦在 1 m 测 dB(A)。这是唯一能确认“物理最大”的实验；不需要改代码。
6. **不要做**：不要用 `tinymix` 写 HP gain/PCM、不要重启 rui/设备、不要改音量、不要 docker/git 写入——这些都会改变现场，且本次的目的只是定位。

---

## 6. 备注 / 限制

- 设备日志时间戳是 UTC；本文引用的是 `/tmp/server.log` 的副本 `/tmp/dot2-server.log` 的行号（2026-10-24 09:25 CST 抓取）。`tinymix` 快照是 09:26 CST。
- `voice_peak_db=-0.6`（rui API）和 `VAD diag clipped=14087` 是**麦克风**峰值/削波，不是扬声器播放电平；设备 mic 在 clipping，不能用于 SPL 反推。
- `[aec] ref=` 是硬件 loopback ch8，post-volume、pre-DAC；它是目前最接近“送到 DAC 的 PCM 电平”的现场量，但经过 3:1 抽取，RMS 不是逐样本精确值。
- stock EQ 的 +13.7 dB @1 kHz 是按 `EQ_100.cfg` 系数直接卷积得到的；如果 vendor DSP 对 FIR 有额外的统一缩放，绝对值会整体平移，但 `EQ_100/EQ_50 = 5.334838` 和 `SETUP.md` 的表能互相校验，缩放假设是合理的。
- 本文只回答“为什么小/有没有额外衰减”，不包含修复方案；2.2/2.4 里的 +29 dB、8 段 EQ、responseLevel 等只是说明“还有哪些增益级存在/不存在”，不是建议去改。
