# Echo Dot 2 / rui AEC 实测分析（只读调研）

- 日期：2026-10-24
- 范围：rui `house-ear`（工作区）、`rui-dot2-bridge` adapter、EchoMuse 固件（`/tmp/echomuse-ref-build`，含 2ch 参考补丁）、真实 dump `/tmp/uplink2.pcm`、`/tmp/uplink3.pcm`、设备 `/tmp/server.log`
- 配套交叉验证：`docs/aec-device-tuning.md`（同日、同一台设备的设备端调优；结论对照见 §3.7）
- 约束：只读；未改任何代码/配置，未重启 rui 或设备，未改音量/静音；离线分析只用本机 Python + `libspeexdsp`。
- 结论先行：**不是帧对齐/固定延迟问题**。ERLE 低是四件事叠加：设备 AEC 级联（部署默认开着，测试期恰好抓到 off/on 两个窗口）、声学链路的线性上限只有约 14–18 dB、rui 的 4096-tap speex 在 1–2 s 的 TTS 上收敛不足、以及去混叠参考在播放/采集两个时钟域之间每 ~0.35 s 硬重锁一次。设备 AEC 本身稳态已有 18–19 dB（启动段 13 dB），rui 在它后面只能再拿 0–2 dB。

---

## 0. 结论摘要

### 0.1 五条主结论

| # | 结论 | 关键数字 | 置信度 |
|---|---|---|---|
| 1 | **部署默认让设备 AEC 先跑，rui 在 on 的窗口里吃“二手残差”**。部署 override 明确 `DOT2_AEC_ENABLED: "1"`，与 `docs/p3-firmware-patch.md` 写的“推 `aecEnabled:false`”相反。本次两个 dump 恰好抓到 **off（uplink2）/on（uplink3）** 两个窗口；on 的窗口里设备先把 mic 压了 12–19 dB，再上行给 rui，rui 的 ERLE 是“增量 ERLE”，不是系统 ERLE。 | 设备 `att=12.8–19.7 dB`（log 11:22:23–45）；dump3 后两段 mic/ref = −18…−20 dB；rui 在 dump3 上仅 0–2 dB | **高** |
| 2 | **声学/扬声器链路的线性可消除上限约 14–18 dB**，不是 25–30 dB。用 Wiener/H1 和 causal FIR 两种独立方法都得到同一量级；把滤波器加到 1 s 也只多 1.5–2 dB。设备自己的 AEC 稳态也只有 14–19 dB，互相印证。 | dump2 Wiener **15.9 / 14.3 dB**；causal FIR 4096 **16.2 / 14.8 dB**；16384 taps **17.9 / 15.8 dB** | **高** |
| 3 | **rui 的 speex 在短 TTS 上收敛不够**。同一段 dump2，冷启动 4096 taps 只有 8.5 dB；1024 taps 10.4 dB；512 taps 10.3 dB；把同一段 TTS 重复 12 次让滤波器收敛后，4096 taps 能到 **14.0 dB**。所以 0.5–9 dB 里有一部分是“每次播放都从零/半零开始”的收敛损失。 | 1.8 s TTS：4096→8.5 dB，1024→10.4 dB；12 次重复后 4096→14.0 dB | **高** |
| 4 | **去混叠参考不是连续时间序列**：`dealias.clean()` 依赖“ch8 恰好等于输出环每 3 个样本”，播放/采集时钟一漂就失锁；设备日志在 dump3 所在的一分钟里报了 **154 次 relock / 154 次 loss**。dump 里 mic↔ref 的延迟在 ~0.35 s 内从 34.0 漂到 35.1 samples 后复位，和 154/60 s 的节律一致。 | relocks=154/losses=154 in 60 s；延迟 34.0→35.1 samples/0.35 s | **中高** |
| 5 | **“谁做 AEC”的数据不支持现在把 AEC 从设备搬到 rui**。设备 AEC 13–20 dB（稳态 18–19）> rui 冷启动 8.5–10.4 dB / 稳态 14 dB；级联只多 0–2 dB。若 rui 要接管，先修参考连续性、把 `filter_len` 降到 1024、并且加 RES；即便如此上限仍是 10–14 dB 量级，不是 20 dB。 | 设备 13–20 dB（稳态 18–19）；rui 8.5–14 dB；级联 18–20 dB | **高** |

### 0.2 关键数字总表

| 指标 | `/tmp/uplink2.pcm` | `/tmp/uplink3.pcm` |
|---|---|---|
| 时长 / 块数 | 37.6 s / 470 × 5120 B | 62.88 s / 786 × 5120 B |
| TTS 段（本文分析） | 12.24–13.90 s、31.02–32.82 s | 8.24–9.64、26.5–28.3、31.26–34.2、36.0–44.0 s |
| TTS 中 mic / ref RMS | −9.4 / −9.9 dBFS；−9.4 / −10.4 dBFS | −19.0/−10.0；−24.1/−10.4；−26.9/−9.0；−29.6/−10.0 dBFS |
| 设备 AEC 状态（推断） | **OFF**（log 11:20:39 `[aec] disabled`） | **ON**；11:22:04 后 hw ch8（8.24–9.64 s 段仍是软件 tap 预热） |
| 设备侧 att | ~0 dB | 12.8–19.7 dB |
| mic↔ref 主延迟 | **+34 samples（2.1 ms）**，ncc −0.50，增益 −0.53 | 34 samples 处无相关；最佳 160–510 samples，ncc 仅 −0.18 |
| Wiener/H1 线性上限 | **15.9 / 14.3 dB** | **2.3 / 1.3 dB** |
| causal FIR 4096 上限 | 16.2 / 14.8 dB | 2.3 / 1.3 dB |
| rui speex 4096 taps（冷启动） | **8.5 / 5.7 dB** | −1.0 / −1.6 dB（输出反而更响，rui 会走“never worse”回退→ERLE 0） |
| rui speex 1024 taps | 10.4 / 7.1 dB | −0.3 / −0.6 dB |
| rui speex 512 taps | 10.3 / 6.8 dB | 1.5 / −0.7 dB |
| mic 削顶样本（TTS 内） | 3.36% / 5.10% | 0% |
| 参考空闲时是否严格 0 | 是（最长 17.08 s 全零） | 是（最长 16.83 s 全零） |

### 0.3 明确“不是”的原因

- **不是 2ch 帧错位/序号 bug**：5120 B/80 ms、ch1 空闲严格全零、ch0 从不为零，`[mic | ref]` 顺序正确；adapter `_rechunk` 20 ms 步长正好 1280 B，不切样本帧（`adapter.py:41-46`、`frames.py:48-63`）。
- **不是固定延迟超出 256 ms 窗口**：主峰 2.1 ms，远小于 4096 taps/256 ms；±500 ms 搜索没有第二个同量级峰。
- **不是参考通道取错**：rui `ref = block[:, frame.mic_channels]` 在 `mic_channels=1` 时取 ch1，正确（`process.py:488-492`；`contract.py:77-82`）。
- **不是没有参考/`ref_active` 没开**：ch1 在 TTS 时有正常语音能量，`ref_active=true`；`aec_enabled=true` 也已在控制台确认。
- **不是 AEC 库没加载**：`libspeexdsp` 可用，离线复刻与在线 0.5–9 dB 同一量级。

> 口径说明：本文两个 dump 来自 **2ch 实验路径**（`DOT2_REF_CHANNELS=1`、`dev/rui/config-2ch.yaml`）；生产插头 `plug.json` 仍是 `ref_channels:0` + `capabilities.aec:"device"`，即 **设备 AEC only、rui 的 AEC 不启用**。所以“rui ERLE 0.5–9 dB”本身就是实验路径的指标；生产路径没有这个数。下面的“谁做 AEC”是在决定**要不要把生产切到 rui**。

---

## 1. 材料、版本与 dump 可信度

### 1.1 版本差异（必须说明）

**rui（house-ear）**

- 工作区 `pyproject.toml` 版本 `0.1.3`；HEAD 在调研期间还在动（`fdb4b8f` 之后又有提交，最后一次采样到 19:29 的 `test: follow the new VAD mirror order`）。
- `aec.py` 最后一次实质修改是 `6f423ae`（2026-10-22，“cancel on the array mix”），本次读到的 AEC 算法与 0.1.3 部署版应一致；`process.py` 在 0.1.3 之后有 P2.9 的 prebuffer/gap 改动，但 **AEC 调用路径和 ERLE 口径没有变**（`process.py:404-421`、`488-494`、`1516-1522`）。
- 因此下文的 rui 行号以当前工作区为准；若部署镜像比 0.1.3 早，唯一可能差异是 `process.py` 的播放调度，不影响 AEC 结论。

**固件（EchoMuse）**

- `/tmp/echomuse-ref-build` 的 git log 是 `50bbbc3 Release prep 2.27.0-ea.1`，`/tmp/echomuse-inspect` 是 `87a3d21`（同为 2.27.0-ea.1 快照）。
- 但 `docs/p3-firmware-patch.md:8-11` 写的补丁基线是 **v2.19.0-ea.1**；设备实际运行 `20261024-1911-dev`，也是 v2.19.0-ea.1 + 补丁构建的。
- `patches/0001-ref-uplink-v2.19.0-ea.1.patch` 与 2.27 工作树的改动内容一致（3 文件：`aec.go` 加 `RefForUplink`、`data.go` 2ch 成帧、`control.go` 特性协商）。**但 2.27 周边代码可能和 v2.19 不同**，所以：
  - 设备日志里的 `relocks=154`、`att=18.8 dB`、`VAD diag` 是**已安装二进制**的事实，结论不依赖源码版本；
  - `dealias.go`、`ProcessMicsWithRef()` 的行号来自 2.27 快照，v2.19 可能有偏移；涉及“双 `clean()` 调用”的推断单独标了置信度。

**调研期间的外部变更（不是本文作者改的）**：`src/rui_dot2_bridge/adapter.py` 在 19:46 出现未提交改动，包含 AEC tail/delay/AGC/barge 透传与 `DOT2_PLAYBACK_GATE_MS` 半双工门控（`adapter.py:207-209`、`402-407`、`421-...`、`458`）。本文的 dump 分析基于改动前的 2ch 路径；门控是 AEC 之外的兜底，若启用会直接静音播放后的 mic，自听会下降但 barge-in 会受影响。

### 1.2 dump 通道/帧证明

| 检查 | uplink2 | uplink3 | 结论 |
|---|---|---|---|
| 文件大小 / 5120 | 470 整 | 786 整 | 每块 80 ms 2ch，无半块 |
| ch1 非零比例 | 7.9% | 31.7% | 只在播放时有值 |
| ch1 最长连续全零 | 273,351 samples = **17.08 s** | 269,313 = **16.83 s** | ch1 是播放回采，空闲严格 0 |
| ch0 非零比例 | 99.6% | 99.6% | ch0 是 mic，永远有底噪 |
| ch0 削顶（abs(x)≥0.999） | 0.39% 全局，TTS 内 3.36–5.10% | 0% | dump2 的 mic 数字增益 18 dB 在响段削顶 |

**通道顺序是 `[mic | ch8 ref]`，不是反过来。** ch1 在空闲时 bit-exact 全零，这是 ch8 回采的签名；mic 不可能全零。这个证据比“相关性”更硬。

### 1.3 dump 时间线（推断，但与日志逐秒吻合）

dump 文件的 mtime 是 **拷贝时间**（19:21:12 / 19:22:56 本地），不是采集起点。用“时长 + TTS 起点”去对设备日志（UTC）：

| dump | 推断采集窗口（UTC） | 对齐证据 |
|---|---|---|
| uplink2 | ~11:20:35 → ~11:21:12 | 12.24 s 处 TTS 起点 ≈ 11:20:47，紧接 log `11:20:48 de-aliased reference locked`；31.02 s ≈ 11:21:06，紧接 `11:21:07 speaker_flush`；长度 37.6 s 正好到 11:21:12 |
| uplink3 | ~11:21:53 → ~11:22:56 | 31.26 s 处 TTS ≈ 11:22:24，紧接 `11:22:23 att=12.8 dB`；36.0 s ≈ 11:22:29，紧接 `11:22:30 att=18.2 dB / VAD rms=0.04339`；长度 62.88 s 正好到 11:22:56 |

这段时间线解释了为什么两个 dump 的 mic/ref 比例差 18 dB：**uplink2 是设备 AEC 被 controller 关掉的窗口**（`11:20:39 [aec] disabled`），**uplink3 是设备重启后默认 AEC 打开、并在 11:22:04 切到 hw ch8 的窗口**（`[aec] far-end reference: hardware`、`enabled ... tail=64ms`）。这是一次意外的自然 A/B。

> 注意：`docs/handoff-morning.md:55-56` 写的“mic 与 ref 相关性仅 ~0.18@65 ms”不是主路径。我们在 uplink2 的 TTS 段找到的主峰是 **+34 samples（2.1 ms），ncc −0.50**；65 ms 处没有同量级峰。该数字可能来自另一段/设备 AEC 打开的残差，不能作为“延迟 65 ms”的依据。

### 1.4 离线方法

1. **互相关/GCC-PHAT**：16 kHz，搜索 ±8000 samples（±500 ms），归一化互相关；分段 1.6–1.8 s，避免跨播放静音。
2. **线性可消除上限（Wiener/H1）**：`H(f)=S_yx/S_xx`，残差 PSD `S_yy(1-γ²)`，`ERLE=10log10(ΣS_yy/ΣS_yy(1-γ²))`；Welch 2048 点、75% overlap。它给的是**该段内最优 LTI 滤波器的上限**（非因果），比真实 causal AEC 略乐观，适合回答“上限在哪”。
3. **Causal FIR 交叉验证**：解 Toeplitz 正规方程，`y[n]=Σ h[k]x[n-k]`，4096/8192/16384 taps；与 H1 独立，结果一致。
4. **rui speex 复刻**：ctypes 调 `/opt/homebrew/lib/libspeexdsp.dylib`，`speex_echo_state_init(128, taps)`、`SPEEX_ECHO_SET_SAMPLING_RATE=16000`，mic/ref 都过 90 Hz 二阶高通，按 128 samples/帧喂；与 `aec.py:129-187` 同参数。复刻**不含** rui 的 `_out` 缓冲拼接和 `never worse` 回退；对 dump3 这种输出更响的段，rui 实际会回退到 mic，ERLE=0，复刻的负值正好说明这一点。

---

## 2. rui AEC 实现（问题 1）

### 2.1 算法 / 库 / 帧长 / 滤波长度

| 项 | 值 | 证据 |
|---|---|---|
| 库 | **speexdsp MDF**（`libspeexdsp`，ctypes 调用） | `aec.py:1-7`、`46-72`、`126-137` |
| 内部帧长 | **128 samples = 8 ms** | `aec.py:30`、`133` |
| 滤波长度 | **4096 taps = 256 ms @16 kHz**（默认）；可配 256–16384 | `aec.py:129`、`schema.py:259-266` |
| 采样率 | 16 kHz（`voice.rate`） | `aec.py:136-137`、`process.py:412-416` |
| 拓扑 | `cancel="mix"`：阵列混音后**一个** canceller；`per_channel` 可选 | `aec.py:230-241`、`schema.py:263-266` |
| 预处理 | mic/ref 各自 90 Hz 二阶高通（biquad），不做 NS/AGC | `aec.py:83-123`、`242-243`、`261-269` |
| 分块 | 任意 block（rui 是 20 ms = 320 samples）内部再切 128 | `aec.py:152-180` |
| 双讲检测 | **rui 没有显式 DTD**；依赖 speex MDF 内部的 `leak_estimate`/`adapted`/`sum_adapt` 保护（vendored `mdf.c`） | `aec.py` 无 DTD 代码；`/tmp/echomuse-ref-build/device/internal/aec/src/mdf.c:131-141、1089-1129` |
| 延迟处理 | **没有 shift/搜索/对齐**；靠 4096 taps 吸收几毫秒到 256 ms 的失配 | `aec.py` 全文无 delay 逻辑；`docs/aec-takeover.md:43` |
| 参考通道 | `AudioFrame` 尾部通道；`mic_channels = channels - reference_channels`；rui 只取**第一个** ref | `contract.py:77-82`；`process.py:488-492` |
| 同帧对齐假设 | 一个 `AudioFrame` 里所有通道共享同一采样时间轴；ref 与 mic 是**同一时间跨度**的 far-end/near-end | `contract.py:62-89`；`docs/aec-takeover.md:37` |

**一次 AudioFrame 的对齐假设**：rui 认为 `block[:, :mic_channels]` 与 `block[:, mic_channels]` 的**第 n 个采样对应同一时刻**，即 ref 已经是“会在同一时刻到达 mic 的远端信号”。它不做“ref 提前/滞后”的补偿；如果 ref 比 mic 早/晚，滤波器只能用自己的 taps 去学。对 Dot2 的设计意图是 ch8 与 mic 在**同一 TDM 帧**，所以天然对齐；但注意——**上行送的是 `dealias.clean(rawRef)`，不是 raw ch8**，见 §3.4。

### 2.2 delay / 双讲 / ERLE

- **delay**：rui 没有 `aecDelayMs` 之类参数。4096 taps = 256 ms 的滤波器从第 0 个 tap 开始学；2.1 ms 的声学延迟完全在窗口内。`docs/aec-takeover.md:43` 也明确“代码里没有 shift 逻辑”。
- **双讲**：rui 没有自己的 DTD；speex MDF 内部用 `leak_estimate`（线性回归残差/回声比）和 `adapted`/`sum_adapt` 决定步长和是否切到 foreground。rui 没有暴露 `SPEEX_ECHO_*` 的 DTD 参数，也没有“播放时冻结自适应”的逻辑；唯一保护是块级 `never worse`（`aec.py:185-186`、`294-297`）。
- **ERLE 计算**（`aec.py:299-314`）：
  - `mic_power` = `mic_mix` 的 EMA（0.9/0.1，按 20 ms 块）；
  - `res_power` = 实际输出 `out` 的 EMA；
  - `erle = 10log10(mic_power / res_power)`，**只在 `ref_active` 时非 None**；
  - `ref_active` 阈值是 ref RMS > 1e-4（−80 dBFS），`aec.py:252-262`；
  - 如果 `out` 比 `mic` 响 >5%，`out=mic`，ERLE 记 0（`aec.py:294-297`）。
  - **重要**：这是“rui 这一级”的**增量 ERLE**，不是“设备 + rui”的系统 ERLE；设备 AEC 已经在它前面压了 12–19 dB。控制台显示 0.5–9 dB 并不等于系统只有 0.5–9 dB。
- **输出对齐**：`process()` 按 128 帧切，`_out` 里保留不足一个 block 的样本；当 `_out < mic.size` 时用 0 补齐（`aec.py:174-180`）。20 ms block 会剩 64 samples，所以每次播放起点可能插入最多 4 ms 的零，属于小瑕疵，不是回声主因。

### 2.3 参考通道取哪一路 / 对齐假设

- `AudioFrame.reference_channels` 定义为 **`pcm` 尾部的通道数**（`contract.py:77`）。
- adapter 在 `DOT2_REF_CHANNELS=1` 时交 `channels=2, mic_channels=1, ref_channels=1`（`adapter.py:611-613`、`140-142`），成帧为 `[mic | ref]`。
- rui `_frame_to_voice()` 里 `ref = block[:, frame.mic_channels]`（`process.py:490`）；`mic_channels=1` 时就是 ch1，正确。
- 若哪天 `mic_channels` 被配成 2，它会取 ch2（不存在/错通道）；当前配置 `mic_channels:1`（`dev/rui/config-2ch.yaml:47-54`）没问题。
- **设计假设**是“同帧 = 同时间”。对 raw ch8 成立；对 `dealias.clean()` 重建出来的 ref **不成立**，因为重建用的是播放 ALSA 环（播放时钟域），而 mic 是采集时钟域。这是 §3.4 的核心。

### 2.4 与设备 AEC 的实现对照

| 维度 | rui | 设备（EchoMuse） |
|---|---|---|
| 库 | speexdsp MDF（系统 `libspeexdsp`） | speexdsp MDF（vendored 1.2.1） |
| 帧长 | 128 samples（8 ms） | 512 samples（32 ms） |
| 滤波长度 | 4096 taps（256 ms）默认 | hw ref **64 ms**（`hwTailMs=64`）；软件 tap 可到 500 ms |
| 参考 | de-aliased ch8（`dealias.clean`） | **raw ch8**（同 TDM 帧，未经 dealias） |
| 麦克风 | 1 路（Dot2 只上行 1 mic） | 7 路，每路一个 echo state（`NumMics=7`） |
| 双讲 | 无显式 DTD（库内部） | 库内部 + 播放/静音状态、7 路分别自适应 |
| delay | 无，滤波器吸收 | 无（hw 路径同帧） |
| 代码 | `aec.py:126-338` | `aec.go:48-70`、`274-279`、`505-510`、`685-745` |

设备的 64 ms 比 rui 的 256 ms 短，但因为它用 **raw ch8 同帧**、且 7 路分别建模，收敛更快、更稳；`aec.go:62-70` 的 bench 记录明确说 16 ms 与 300 ms tail 效果几乎一样（20.3 vs 20.4 dB），300 ms 反而要 ~10 s 收敛。

---

## 3. dump 实测（问题 2）

### 3.1 延迟：±500 ms 搜索

对 uplink2 的两个 TTS 段做归一化互相关（±8000 samples）：

| 段 | 主峰 lag | 主峰 ncc | 次峰 | 结论 |
|---|---|---|---|---|
| 12.24–13.90 s | **+34 samples（2.12 ms）** | **−0.50** | +90（0.20）、−12（−0.16） | 唯一同量级峰；mic 相对 ref **极性相反**、延迟 2.1 ms |
| 31.02–32.82 s | **+34 samples（2.12 ms）** | **−0.50** | +75（−0.21）、−25（0.15） | 重复一致 |
| 31.26–34.2 s（uplink3） | 34 处 ncc 0.02 | — | 510（−0.18）、0（−0.15） | 设备 AEC 后主路径已不在 34 samples |

- **±500 ms 内没有第二个同量级峰**；65 ms 处没有。主延迟就是 2.1 ms。
- **极性相反**（ncc −0.5、最佳单 tap 增益 −0.53）：speex 的自适应 tap 可以为负，**不是阻塞**；但它意味着滤波器要先学一个负号，冷启动时前几个 block 可能更慢。
- 单 tap 只能给 **1.25 dB**（因为回声路径有长 FIR/房间滤波），所以不能拿“单点相关 0.5”直接推断 ERLE；要用 §3.2 的多 tap 上限。

**判断**：延迟在 rui 的 256 ms 窗口内，**不是对齐问题**；给参考手动加最佳延迟/增益不会把上限从 16 dB 抬到 25 dB（causal FIR 在 d=0/20/34/40/64/96/128 上搜，最佳都是 16.2/14.8 dB 附近，d=0 最好，因为滤波器自己会学延迟）。

### 3.2 线性可消除上限（Wiener/H1 + causal FIR）

**uplink2（设备 AEC OFF，rui 应该能全力消）**

| TTS 段 | 时长 | mic RMS | ref RMS | Wiener/H1 上限 | causal FIR 4096 | 16384 taps（1 s） |
|---|---|---|---|---|---|---|
| 12.24–13.90 | 1.66 s | −9.4 dBFS | −9.9 | **15.9 dB** | 16.2 | 17.9 |
| 31.02–32.82 | 1.80 s | −9.4 | −10.4 | **14.3 dB** | 14.8 | 15.8 |

**uplink3（设备 AEC ON，rui 吃残差）**

| TTS 段 | mic RMS | ref RMS | Wiener/H1 上限 | causal FIR 4096 |
|---|---|---|---|---|
| 31.26–34.2 | −26.9 | −9.0 | **2.3 dB** | 2.3 |
| 36.0–44.0 | −29.6 | −10.0 | **1.3 dB** | 1.3 |

**频带分解（uplink2，两段分别）**

| 频带 | 段1 上限 | 段2 上限 | 说明 |
|---|---|---|---|
| 0–300 Hz | 14.7 | 14.2 | TTS 能量少，参考低 |
| 300–1k | **16.6** | **14.9** | 语音主能量，线性上限就在这 |
| 1–2k | **18.1** | **16.5** | 同上 |
| 2–4k | 3.9 | 3.1 | mic 高频主要是底噪/失真，不是 ref 的线性回声 |
| 4–7k | 5.0 | 1.7 | 同上 |

**结论**：即使用“最优线性滤波器 + 任意延迟/增益”，uplink2 的上限也只有 **16 dB 左右**，不是 25–30 dB。uplink3 的上限只有 **1–5 dB**，因为设备 AEC 的残差已经不是 ref 的线性函数。

**滤波长度不是瓶颈**：4096→8192→16384 taps，上限只从 16.2→16.5→17.9 dB（段1），14.8→15.0→15.8（段2）。所以“rui 的 256 ms 太短”不是主因；1 s 也只多 1.5–2 dB。

**增益不是瓶颈**：Wiener 滤波器自带增益；最佳单 tap 增益 −0.53 只是路径的宽带增益，多 tap 会把它展开。

**延迟不是瓶颈**：±500 ms 搜索 + d=0…128 的 causal FIR 都表明 34 samples 已在窗口内；给参考加最佳延迟不会显著抬高上限。

### 3.3 相关性 / 非线性 / 削顶

- **相干性**（uplink2，12–14 s）：
  - 300–1k：平均 γ² ≈ **0.94**，中位 0.95；
  - 1–2k：平均 **0.91**，中位 0.97；
  - 2–4k：平均 **0.48**；
  - 4–7k：平均 **0.38**。
  - 语音带的 3–6% 不相干，正好把线性上限压到 14–18 dB（γ²=0.95 → 13 dB；0.99 → 20 dB）。
- **不是简单无记忆非线性**：在最佳延迟 34 上做 1/2/3/5/7 阶多项式，ERLE 只从 1.25 提到 1.31 dB（uplink2 段1），几乎没改善。所以“限幅器/削顶”不是唯一原因；更可能是**有记忆的声学/扬声器非线性 + 时变相位**。
- **mic 削顶**：uplink2 的 TTS 段有 3.36%/5.10% 样本削顶（ch0 数字增益 18 dB），但把削顶帧剔除后上限反而降到 12.6/10.8 dB（因为剔掉的是高信噪比帧），所以削顶不是 16 dB 上限的主因；它是**高音量下的伴随现象**，不是根因。
- **高频**：uplink2 的 mic 在 6–7k 比 ref 高 **12.9 dB**，7–7.5k 高 10.3 dB；这部分是 mic 底噪/失真，不是线性回声，所以 2–7k 的上限只有 2–5 dB。
- **设备 AEC 的残差**：uplink3 在 34 samples 处 ncc 0.02，最佳 lag 160–510 samples、ncc 仅 −0.18；说明设备 AEC 的输出**不再是原始回声路径的线性残差**，rui 不能“接着设备的滤波器继续消”。

### 3.4 参考质量：relock 与时钟域（关键）

**设备日志直接证据**：

```
11:22:04 [aec] de-aliased reference locked to the output
11:22:58 [aec] de-aliased reference: locked=true relocks=154 losses=154 backoffs=0 in 60s
```

`relocks == losses == 154`，即**每次失锁都重新锁上**，60 s 内 154 次（≈2.6 次/s；如果只按 dump3 的 TTS 活跃时间 ~20 s 算，≈7.7 次/s）。`losses` 只在“已锁定但 `matches()` 失败”时增加（`dealias.go:208-220`），`relocks` 在重新 `search()` 成功后增加（`dealias.go:242-250`）。

**为什么会失锁**：`dealias` 假设 ch8 恰好等于输出环的每 3 个样本（`dealias.go:55-71`、`126-142`、`208-215`）。它每次 `clean()` 后把 `pos += 3*n`，**不跟踪采样时钟的连续漂移**。播放环（ALSA 写入，播放时钟）和 ch8（采集 TDM 帧，采集时钟）只要相差几十 ppm，几十万个样本后就会差 1 个 48k 样本；`matches()` 要求**每个样本**都在 ±2 LSB 内，于是失锁、`search()` 重新找 `p`、参考被**平移**。设备自己的 AEC 用 **raw ch8**（同帧、同采集时钟），不经过 `dealias`，所以不受这个影响。

**dump 里的旁证**：uplink2 的短时互相关延迟在 12.10–12.45 s 从 **34.06 → 35.07 samples** 单调漂移，然后复位到 ~34；周期 ~0.35 s，和 154/60 s 的节律一致。也就是说，参考的时间基在两次 relock 之间**连续滑走**，每次 relock 跳回。对一个时不变 FIR 来说，这相当于一个**缓慢变化的分数延迟**，高频相干性先掉，低频还能撑住——正好对应 300–2k 上限 14–18 dB、2–7k 只有 2–5 dB。

**但要诚实**：把“1 sample / 0.35 s 的锯齿延迟”直接加到一段干净 ref 上做仿真，线性上限是 **26.6 dB**，不是 16 dB；要降到 17 dB 需要 ~3 samples/0.35 s 的漂移（仿真：amp 3 → 17.3 dB）。所以 **relock 是真实且有害的，但它单独不足以解释 16 dB**；16 dB 里还有声学/扬声器非线性（见 §3.2、§3.3）。设备 AEC 也只有 18–19 dB，说明物理上限就在 18–20 dB 附近，relock 再吃掉 2–4 dB。

**双 `clean()` 调用的代码疑点（中等置信度）**：

- 2.27 快照里，`data.go:1272-1278` 先调 `RefForUplink(rawRef)`，后者调 `dealias.clean()`（`aec.go:274-279`）；
- 随后 AEC 又调 `ProcessMicsWithRef(..., echoRef)`，后者**再次** `clean(ref)`（`aec.go:685-691`）；`ProcessWithRef` 同样（`aec.go:505-510`）。
- `clean()` 是**有状态**的（`pos`/`locked`），同一个 raw ref 调两次会让第二次的 `pos` 已经前进了 `3n`，必然失锁/重锁。
- 如果这是已安装 v2.19 的实际路径，relock 应该是 ~31 次/s（每个 32 ms 周期两次），而不是 154/60 s；所以**要么 v2.19 只调一次，要么报告口径/调用条件不同**。这条只作为“代码审查候选”，需要用已安装二进制的符号/日志确认；不要把它当成已证实的根因。

**参考源的“取点”没问题**：ch8 是播放回采，取 ch8 是对的；问题在 `dealias` 把参考从**采集时钟域**搬到了**播放时钟域**，并且用硬重锁而不是连续重采样。

### 3.5 rui speex 实测（离线复刻）

复刻参数与 `aec.py` 一致：`speex_echo_state_init(128, taps)`、16 kHz、mic/ref 90 Hz 高通、128 samples/帧。每段从**零滤波器状态**开始（模拟 rui 在播放开始时没有预热），测整段 ERLE；另测最后 0.5 s（接近收敛）。

**uplink2（设备 AEC OFF）**

| taps | 段1 总 ERLE | 段1 最后 0.5 s | 段2 总 ERLE | 段2 最后 0.5 s |
|---|---|---|---|---|
| 512（32 ms） | **10.3 dB** | 11.2 | **6.8 dB** | 6.5 |
| 1024（64 ms） | **10.4 dB** | 11.7 | **7.1 dB** | 6.5 |
| 2048（128 ms） | 9.4 | 10.7 | 6.5 | 7.0 |
| 4096（256 ms） | **8.5 dB** | 9.6 | **5.7 dB** | 6.0 |
| 8192（512 ms） | 6.7 | 8.1 | 4.6 | 4.9 |

**uplink3（设备 AEC ON）**

| taps | 8.2–9.7 s | 31.2–34.2 s | 36–44 s |
|---|---|---|---|
| 512 | 1.5 dB | −0.7 | −0.5 |
| 1024 | 1.0 | −0.3 | −0.6 |
| 4096 | −1.0 | −1.6 | −0.8 |

- uplink3 的 4096 taps 输出**比 mic 还响**；rui 的 `never worse` 会回退到 mic，所以实际 ERLE 记 **0**，不是负值。
- uplink2 的 4096 taps 只有 8.5/5.7 dB，和在线 0.5–9 dB 吻合；512/1024 taps 反而更好（10.3/10.4），因为短 TTS 上收敛更快。
- **收敛实验**：把 uplink2 的两段 TTS 重复 12 次，4096 taps 在第 3 次到 13.0 dB、第 12 次到 **14.0 dB**；1024 taps 到 13.3；512 taps 到 12.4。也就是说：
  - 4096 taps 的**稳态**能到 ~14 dB（接近 16 dB 上限），但需要 **~4–6 s 的连续播放**；
  - 单次 1.8 s 的 TTS 只有 8.5 dB，**收敛损失 ~5 dB**；
  - 1024 taps 是短 TTS 的更好折中（10.4 dB 冷启动，13.3 dB 稳态）。

**为什么 4096 反而比 1024 差**：MDF 的 32 个 partition 要依次收敛；TTS 只有 1.7 s，4096 taps 的尾部分区还没学到。设备侧也是同样结论：`hwTailMs=64` 比 300 ms 收敛快得多（`aec.go:62-70`）。

### 3.6 设备 AEC 实测与 VAD

**设备 AEC 状态（log）**：

```
11:20:39 [aec] disabled
11:21:55 [aec] enabled: frame=512 tail=300ms delay=0ms
11:22:04 [aec] far-end reference: hardware (ch8, frame-aligned)
11:22:04 [aec] enabled: frame=512 tail=64ms delay=0ms
11:22:05 [aec] att=9.6dB mic=4123 out=1365 ref=9354 src=hw(ch8) on=ch6
11:22:23 [aec] att=12.8dB mic=5838 out=1332 ref=10359
11:22:27 [aec] att=16.2dB mic=5812 out=896 ref=10881
11:22:30 [aec] att=18.2dB mic=5025 out=620 ref=9745
11:22:45 [aec] att=19.7dB mic=4958 out=516 ref=10881
...
11:22:58 [aec] de-aliased reference: locked=true relocks=154 losses=154 backoffs=0 in 60s
```

- 设备 AEC 在 dump3 的 TTS 段给出 **12.8–19.7 dB**，与 dump3 后两段的 mic/ref 比（−18…−20 dB）一致；8.24–9.64 s 段只有 −9 dB，因为该段发生在 `11:22:04` hw 路径确认之前，设备还在软件 tap/预热状态（log `11:21:55 enabled tail=300ms`、`11:21:58 reference resync`、`11:21:59 underrun`）。
- `att` 是设备内部 `mic`（pre-AEC）与 `out`（post-AEC）的 **32 帧 RMS 算术平均之比**，不是能量比/ERLE，也不是 rui 的增量 ERLE，两者不能直接比。
- 设备 VAD 在 11:22:30 的记录：`rms=0.04339 threshold=0.03177 ... gate=true`。也就是**设备 AEC 后残差仍有 −27.3 dBFS，比设备自己的 VAD 阈值（−30 dBFS）高 3 dB**，VAD 判成 speech。自听不是“AEC 没开”，而是 **稳态 18–19 dB 不够**。
- 设备 AEC 的 64 ms tail、7 路 per-mic、raw ch8 是它比 rui 强的三个结构性原因；它的稳态 18–19 dB 已接近我们测到的物理上限（14–18 dB）。
- **关于“设备 AEC 约 10 dB”**：本次是 P3 固件 + **hw ch8** 路径，TTS 稳态是 **12.8–19.7 dB**（dump3 后两段 −18…−20 dB）；10 dB 更接近 8.24–9.64 s 的**软件 tap/预热段**，或 stock v2.18 的软件 tap 经验值。评估设备 AEC 时应取“hw 路径 + 播放 2 s 后”的稳态值，否则会把设备能力低估 8–10 dB。

### 3.7 与 `docs/aec-device-tuning.md` 的交叉验证

设备端调优文档（同日、同一台设备）与本次 dump 的结论**互相印证**，这里把差异和边界说清：

| 议题 | `aec-device-tuning.md` | 本次 dump / 离线 | 一致性 |
|---|---|---|---|
| “只有 ~10 dB” | 冷启动 0–8 dB、2–4 s 回复 7–11 dB、≥8 s 纯播放收敛带 **14–18.2 dB**；10 dB 是口径问题 | dump3 hw 段 12.8–19.7 dB；8.24–9.64 s 的 −9 dB 正是 hw 确认前的预热段 | ✅ 一致 |
| `att` 定义 | 帧 RMS 的**算术平均**之比，不是能量比/ERLE；统计窗口含双讲 | 我们把它当“设备内部 pre/post 平均”，没有当 ERLE | ✅ 一致（本文应更明确写“算术平均”） |
| 设备可调旋钮 | `aecTailMs`/`aecDelayMs`/`AGC`/`barge*` 在 hw/常开流路径**无效**；真正有效的只有 `wakeMic`、播放电平/EQ、`micGainDb`、固件 `hwTailMs` | 我们的 dump 只看到 hw 64 ms + ch6；无法从 dump 直接测这些旋钮 | ✅ 不冲突 |
| 7 路 per-mic | 7 路都在自适应，但 `lock_mic=false` 只上行 **ch6** 一路；6 路算力浪费 | dump 的 `on=ch6` 与 log 一致 | ✅ 一致 |
| 设备上限 | 仅推配置约 **14→17 dB**（换麦）；固件 1 路 + 256 ms 估算 **20–24 dB**；127 音量免 gate 需 **31 dB** | 我们的 rui 参考线性上限 14–18 dB；设备 hw 实测 18–19 dB；25–31 dB 不可达 | ⚠️ 量级一致，设备 20–24 是估算，未实测 |
| 混响 tail 假设 | 64 ms 覆盖不了客厅混响，是 14 vs bench 20.4 的主要嫌疑 | 我们的 rui 参考把滤波器加到 **1 s** 只多 1.5–2 dB，说明**在 rui 的 de-aliased 参考上**混响尾巴不是主因；这不直接否定设备 raw ch8/64 ms 路径的 F1 实验 | ⚠️ 两者路径不同，需 F1 实测 |
| 生产方向 | `owner=device`、`ref=0`；2ch/级联只作实验/兜底；级联时 rui `aec_erle_db`≈0–5，不可当设备指标 | 我们的 dump3 增量 0–2 dB，完全一致 | ✅ 一致 |

**结论**：本次 dump 不支持“设备 AEC 只有 10 dB/需要换算法”的说法；它支持设备文档的“稳态 14–18 dB + 冷启动/双讲口径”解释。**但 18–19 dB 仍不够**：设备 VAD 在 −27 dBFS 残差上仍 gate=true，要免 gate 需 ~31 dB（设备文档 §3.3），这不是换 AEC 位置能解决的。团队当前 `owner=device, ref=0` 的方向与数据一致；2ch/rui AEC 应定位为实验/兜底，而不是默认。

---

## 4. 结论排序（问题 3）

| 排序 | 原因 | 证据 | 置信度 | 可验证实验 |
|---|---|---|---|---|
| 1 | **设备 AEC 级联 + 配置与文档相反**：设备先消 12–19 dB，rui 的增量上限只剩 1–5 dB；`DOT2_AEC_ENABLED=1` 而不是 P3 文档说的 0 | `deploy/rui-dot2-override.yml:28`；`docs/p3-firmware-patch.md:118`；log `11:20:39 disabled` vs `11:22:04 enabled`；dump3 上限 2.3/1.3 dB | **高** | E1：`DOT2_AEC_ENABLED=0` 重跑，看 rui ERLE 是否升到 8–14 dB |
| 2 | **声学/扬声器链路的线性上限只有 14–18 dB**（127/127、当前 EQ/limiter），不是 25–30 | Wiener 15.9/14.3；causal FIR 16.2/14.8；1 s taps 仅 17.9/15.8；设备 AEC 18–19（独立旁证，但不排除设备滤波器自身也受限）；高频 2–7k 只有 2–5 dB | **高** | E5：把音量降到 70–80% 或换低失真测试信号，看上限是否升到 22–25 dB |
| 3 | **rui speex 4096 taps 在 1–2 s TTS 上收敛不足**：冷启动 8.5 dB，稳态 14 dB；1024 taps 冷启动 10.4 dB | §3.5 表；12 次重复实验；`aec.py:129`、`schema.py:262` | **高** | E2：`filter_len: 1024`（64 ms）A/B；E3：连续播放 10 s 后再测 |
| 4 | **去混叠参考在播放/采集两个时钟域间硬重锁**：154 relock/loss per 60 s；延迟 34.0→35.1/0.35 s；relock 单独仿真只能到 26 dB，是“次因”但会拖慢收敛 | `dealias.go:208-250`；log `11:22:58`；dump 延迟漂移；仿真 1 sample/0.35 s→26.6 dB | **中高** | E4：固件临时改送 raw ch8（或连续重采样版 dealias），比较 rui ERLE 与 relock 计数 |
| 5 | **双 `clean()` 调用（2.27 快照）**：`RefForUplink` 与 `Process*WithRef` 都调有状态的 `clean()`，同一 period 第二次必失锁 | `data.go:1272-1278`；`aec.go:274-279`、`505-510`、`685-691`；但 154 次/60 s 不支持“每周期两次”，v2.19 可能不同 | **中** | E6：在已安装二进制上加计数/反汇编，确认 `clean()` 每 period 调用次数 |
| 6 | **不是帧对齐/序号/通道顺序**：5120 B、ch1 空闲严格 0、20 ms 正好 1280 B、延迟 2.1 ms | §1.2、§3.1；`adapter.py:41-46`；`frames.py:48-63` | **高**（排除项） | 已由 dump 证明 |
| 7 | **ERLE 指标口径误导**：0.5–9 dB 是 rui 增量、EMA、带 `never worse` 回退；不是系统 ERLE | `aec.py:299-314`；dump3 输出更响→回退→0；设备 att 18–19 | **高** | E7：在设备 pre-AEC 处加一个 tap，直接算系统 ERLE |

**排序逻辑**：先看“rui 的输入是什么”（设备 AEC 是否在跑）→ 再看“物理上限”（14–18 dB）→ 再看“rui 离上限差多少”（收敛）→ 最后看“参考是否连续”（relock）。这四层是乘法关系，不是并列的“可能原因”。

---

## 5. 该谁做 AEC（问题 4）

### 5.1 三条路线的实测对比

| 路线 | 系统 ERLE（TTS 中） | TTS 中残余 mic | 优点 | 代价/风险 |
|---|---|---|---|---|
| **设备 AEC only**（当前部署） | **18–19 dB（稳态；启动段 13）** | −27 dBFS | 7 路 per-mic（只上行 ch6）、raw ch8 同帧、64 ms 收敛快；已验证 | 单核 ~19%；残差仍高于设备 VAD 阈值 3 dB；自听仍在 |
| **rui AEC only**（设备 AEC 关） | **8.5 dB 短 TTS / 14 dB 稳态** | −18 / −23 dBFS | 参数可调、ERLE 可观测、CPU 低 | 比设备差 4–10 dB；de-aliased ref 会 relock；4096 taps 收敛慢 |
| **级联**（设备 on + rui on，当前实际） | **18–20 dB** | −27…−29 dBFS | 比设备 alone 多 0–2 dB | rui 增量上限 1–5 dB；参考 relock；CPU/复杂度；ERLE 指标误导 |

> 表中“系统 ERLE”用设备 AEC off 时的 mic≈ref（dump2）与设备 AEC on 时的 mic/ref（dump3）估算；设备 att 是独立日志，三者互相吻合。
>
> **部署现状**：生产插头 `plug.json:20-22` 是 `ref_channels:0` + `capabilities.aec:"device"`（`plug.json:46-49`），adapter 不宣告 `dot2_hw_ref`，设备发单声道，rui AEC 自动关闭。也就是说 **“设备 AEC only”是当前生产态**；2ch/rui AEC 是实验/兜底路径。下面的对比是为了判断“要不要切”，不是描述已上线状态。

### 5.2 判断标准（把选择变成可测的条件）

1. **如果验收是“系统 ERLE ≥20 dB”**：
   - 当前只有**设备 AEC**接近（18–19 dB），但仍差 1–2 dB；
   - **rui alone 不满足**（8.5–14 dB）；
   - 级联 18–20 dB 只是“刚好”，没有余量。
   - 结论：**数据不支持现在把 AEC 从设备搬到 rui**；要先解决物理上限（音量/EQ/扬声器线性化）或加 RES。
2. **如果目标是“rui 拥有 AEC、可调可观测”**：
   - 前提：设备 AEC **必须关**（否则 rui 只能拿 0–2 dB）；
   - 参考必须**连续**（raw ch8，或 dealias 改成连续重采样/跟踪漂移，不能硬重锁）；
   - `filter_len` 先从 **1024（64 ms）** 起步，不要 4096；
   - 加 **RES**（谱减/维纳后滤波）处理非线性残差；
   - ERLE 在**设备 pre-AEC 的 mic** 上测，不能拿 rui 增量指标当系统指标；
   - 即使全部做到，预期是 **10–14 dB**，不是 20 dB；要 20+ 必须降低扬声器失真或加 RES。
3. **如果目标是“消自听/可靠 barge-in”**：
   - 线性 AEC 在 127/127 下到不了 25 dB；设备 18–19 dB 的残差仍比 VAD 阈值高 3 dB；
   - 需要 **RES / 半双工 gating / 降低音量** 中的至少一个；单靠换 AEC 位置解决不了。
4. **如果目标是“省设备 CPU”**：
   - 设备 7 麦 AEC ~19% 单核；rui alone 的 CPU 更低，但质量从 18–19 dB 掉到 8.5–14 dB；省 CPU 的代价是自听/误唤醒变差。是否值得，需要产品侧用 E1/E2 的数据权衡。

### 5.3 设备侧还能调什么

- **AEC 专用旋钮在 hw 路径上大多是惰性的**：`aecTailMs`/`aecDelayMs` 在 `hwRef` 时只存值、不重建滤波器；真正建滤波器的是常量 `hwTailMs=64`（`aec.go:62-70`、`236-242`）。`agcEnabled`/`barge*` 在 `lock_mic=false` 的常开流里不执行。`docs/aec-device-tuning.md` 的 D 组已源码级确认，本次 dump 的 `on=ch6`/`tail=64ms` 与之一致。
- **唯一“只推配置就可能拿 dB”的旋钮是 `wakeMic`（0=ch6，1–6=MK1–MK6）**：换一路麦就换一条回声路径；上游在 Dot 2 上实测同一 wake sound 在不同麦上 **24–26 dB 到 −7 dB** 的跨度。设备文档估算可拿 **±3–6 dB**，但用户语音信噪比不能掉太多。当前 adapter **没有透传 `wakeMic`**（`device_link.py` 的 config 字段缺口），要做实验得先加一行。
- **`micGainDb` 不改线性 ERLE**（分子分母同比），只改削波/量化；18 dB 已在响段削顶（dump2 TTS 内 3.4–5.1%），不建议再上探。
- **播放电平/EQ 是最大的杠杆**：127/127 下扬声器/腔体非线性是 14–18 dB 上限的主要嫌疑；降 3–6 dB 或再削一点低频，可能把线性上限抬到 22–25 dB（需 E5 验证）。这也是把“31 dB 需求”拉进可达区的唯一产品级手段（设备文档 §3.3）。
- **固件 `hwTailMs`（F1）仍值得实测，不要用本文的 1 s 滤波器结果否定它**：我们在 rui 的 de-aliased 参考上把滤波器加到 1 s 只多 1.5–2 dB，说明**那条参考路径**的混响尾巴不是主因；设备用 raw ch8 + 64 ms，是另一条路径，F1（128/192/256 ms）仍可能拿到设备文档估算的 +3–6 dB。F2（7 路→1 路）可为 F1 腾 CPU。
- **参考**：raw ch8 同帧是设备最大的优势；**不要**把设备 AEC 也改成 dealias，否则会引入和 rui 一样的时钟域问题。
- **RES / 半双工 gate**：设备 VAD 已看到残差 gate=true；在 TTS 播放时临时提高 VAD 阈值/关闭 ASR，能立刻消自听，但会牺牲 barge-in。谱域 RES 是从 18 dB 到 25–30 dB 的唯一现实路径。
- **结论**：设备 AEC 的“专用参数”空间很小（tail/delay/AGC/barge 基本无效）；能拿 dB 的是 **`wakeMic` + 播放电平/EQ + 固件 hwTail + RES/gate**。设备文档估算加固件后 **20–24 dB**（未实测），127 音量免 gate 需 **31 dB**，本硬件/算法做不到；不要把“设备 AEC 只有 10 dB”当成可调参数问题——我们实测 hw 稳态是 12.8–19.7 dB，已接近物理上限。

### 5.4 rui 接管的前提清单（若产品决定走这条）

> 团队当前方向（`docs/aec-device-tuning.md` §5.1）：生产 `owner=device`、`ref_channels=0`，2ch/级联只作实验/兜底。下面清单只适用于“决定把生产切到 rui”的场景；不切就保持 `ref=0`，rui AEC 关闭。

| 前提 | 当前状态 | 不做会怎样 |
|---|---|---|
| 设备 AEC 关 | ❌ 部署 override 是 `1` | rui 增量上限 1–5 dB，永远上不去 |
| 参考连续/同时钟域 | ❌ dealias 硬重锁，154/60 s | 相干性掉、收敛慢、ERLE 抖动 |
| `filter_len` 1024（64 ms） | ❌ 当前 4096 | 短 TTS 冷启动少 2 dB，稳态慢 2–3 倍 |
| RES | ❌ 没有 | 非线性残差消不掉，卡在 14 dB |
| 系统 ERLE 口径 | ❌ 只看 rui 增量 | 把 18 dB 的设备功劳误判成 0.5–9 |
| 设备 CPU 回收 | ✅ 关设备 AEC 可省 ~19% | 不关就白折腾 |

---

## 6. 下一步实验（按性价比排序）

| # | 实验 | 操作（由有权限的人做） | 预期/判据 |
|---|---|---|---|
| **E1** | **设备 AEC off 的干净 A/B** | `DOT2_AEC_ENABLED=0`，重启 rui（不改设备音量），播 10 s TTS，抓 2ch dump + rui `aec_erle_db` | 若 rui ERLE 从 0.5–9 升到 **8–14 dB**，证明主因是级联；若仍 <5，查参考/收敛 |
| **E2** | **filter_len 1024 vs 4096** | rui `audio.aec.filter_len: 1024`，同样 TTS/dump | 短 TTS 预期 +1.5–2 dB（10.4 vs 8.5）；稳态略低（13.3 vs 14.0），可接受 |
| **E3** | **连续播放收敛** | 同一 TTS 连续播 10 s（或循环 5 次），测最后 2 s ERLE | 4096 taps 应从 8.5 升到 **13–14 dB**；若升不上去，是参考 relock/路径变化，不是收敛 |
| **E4** | **raw ch8 vs de-aliased ref** | 固件临时 feature flag 送 raw ch8（或修 dealias 为连续重采样），保持设备 AEC off，抓 dump | raw ch8 若 ERLE >18 dB 且 relock=0，证明 dealias 是参考瓶颈；注意音乐下 raw ch8 有混叠，TTS 下可能可接受 |
| **E5** | **电平/失真** | 音量降到 70–80%，或播 −20 dBFS 粉噪/扫频，测线性上限 | 若上限从 14–18 升到 22–25 dB，说明是扬声器非线性；产品需在响度/自听间取舍 |
| **E6** | **确认 `clean()` 调用次数** | 在已安装二进制/源码里数 `RefForUplink` 与 `Process*WithRef` 的 `clean()`；或加日志 | 若每 period 两次，修成“clean 一次、两处共用”；若一次，relock 就是时钟漂移 |
| **E7** | **系统 ERLE 口径** | 在设备 pre-AEC 处加一个 mic tap（或复用 dump2 的 mic 作为 pre-AEC 估计），算 `10log10(P_pre/P_final)` | 当前系统约 18–20 dB；控制台应显示系统值，不是 rui 增量 |
| **E8** | **RES 原型** | 在 rui AEC 后加一个谱域 RES（维纳/谱减），用 dump3 离线评估 | 目标：在 18–19 dB 设备 AEC 上再压 6–10 dB；若能做到，自听/VAD 问题可解 |
| **E9** | **`wakeMic` 0–6 扫描** | 给 adapter 加 `DOT2_WAKE_MIC` 并透传到 config push；每换一路，12 s 纯 TTS，取最后 3 条 `att` | 上游麦间跨度 24→−7 dB；预计 **±3–6 dB**；选 att 最高且用户语音不掉 >3 dB 的一路 |
| **E10** | **固件 `hwTailMs` 64→128/192/256** | 重建 server 二进制；1 路（F2）+ 256 ms（F1）组合 | 若混响主导，预计 **+3–6 dB**；若 `att` 不动，上限由失真/耦合决定，停止投入 |
| **E11** | **锁 `aecRefSource=hw` + `att` 上报** | config push 加 `aecRefSource:"hw"`；把 `att` 塞进 stats/SendLog | 防止 auto 检测失败悄悄退回 sw（历史 ~0–3 dB）；没有上报就无法规模化调优 |

---

## 7. 文件/行号索引

**rui（house-ear，工作区）**

- `src/house_ear/audio/aec.py:29-30` — `SPEEX_FRAME=128`
- `src/house_ear/audio/aec.py:126-137` — `EchoCanceller` 初始化（taps/rate）
- `src/house_ear/audio/aec.py:152-187` — 分块/128 帧/`never worse`
- `src/house_ear/audio/aec.py:208-245` — `AecChain`、`cancel=mix`
- `src/house_ear/audio/aec.py:255-314` — 参考高通、`ref_active`、ERLE
- `src/house_ear/audio/process.py:404-421` — `_build_aec`
- `src/house_ear/audio/process.py:484-494` — `_frame_to_voice` 取 ref
- `src/house_ear/audio/process.py:1516-1522` — metrics `aec_erle_db`
- `src/house_ear/device/contract.py:62-89` — `AudioFrame`/尾部参考语义
- `src/house_ear/config/schema.py:259-266` — `audio.aec.*`

**adapter（rui-dot2-bridge）**

- `src/rui_dot2_bridge/frames.py:48-63` — 去掉 `[0x01][seq u16]`，返回 payload
- `src/rui_dot2_bridge/adapter.py:41-46` — `_rechunk` 20 ms，按 `channels` 切
- `src/rui_dot2_bridge/adapter.py:409-417` — 生成 `AudioFrame(channels=2, reference_channels=1)`
- `src/rui_dot2_bridge/adapter.py:604-613` — `DOT2_AEC_ENABLED` / `DOT2_REF_CHANNELS`
- `plug.json:20-22` — 生产 `ref_channels: 0`
- `plug.json:44-49` — 生产 `capabilities.aec: "device"`
- `deploy/rui-dot2-override.yml:28` — `DOT2_AEC_ENABLED: "1"`
- `docs/p3-firmware-patch.md:118` — 文档说“推 `aecEnabled:false`”（与部署相反）
- `docs/aec-takeover.md:12,31,43,97` — 20.3 dB、25.7 dB、无 shift、≥20 dB 验收
- `docs/aec-device-tuning.md:1-30` / `§3.3` / `§4.4` / `§5.1` — 设备端 14–18.2 dB 收敛带、31 dB 需求、惰性旋钮、owner=device/ref=0
- `docs/handoff-morning.md:55-56` — 0.5–9 dB、0.18@65 ms

**固件（`/tmp/echomuse-ref-build`，2.27.0-ea.1 快照；行号对 v2.19 可能有偏移）**

- `device/internal/aec/aec.go:48-70` — `FrameSize=512`、`hwTailMs=64`
- `device/internal/aec/aec.go:274-279` — `RefForUplink()` → `dealias.clean()`
- `device/internal/aec/aec.go:417-445` — `WriteFar()` 喂输出环
- `device/internal/aec/aec.go:505-510` — `ProcessWithRef()` 再次 `clean()`
- `device/internal/aec/aec.go:685-691` — `ProcessMicsWithRef()` 再次 `clean()`
- `device/internal/aec/dealias.go:55-71` — ring/taps/tol
- `device/internal/aec/dealias.go:96-108` — `feed()`
- `device/internal/aec/dealias.go:186-252` — `clean()` 的 lock/loss/relock
- `device/internal/client/data.go:1272-1278` — `uplinkRef = RefForUplink(rawRef)`
- `device/internal/client/data.go:1411-1440` — 2ch 交织
- `device/internal/bindings/speaker/pcm_speaker.go:560-590` — output chain → volume → echo tap
- `device/internal/processor/processor.go:62-65` — 非 lockMic 流 AGC 直通

**日志（`/tmp/server.log.txt`，UTC）**

- `11:20:39 [aec] disabled`
- `11:21:57 Config applied`（无 `[aec] disabled`，即设备 AEC 保持默认 on）
- `11:22:04 [aec] far-end reference: hardware (ch8, frame-aligned)` / `enabled ... tail=64ms`
- `11:22:05 … 11:22:45 att=9.6 → 19.7 dB`
- `11:22:30 [data] VAD diag: rms=0.04339 threshold=0.03177 gate=true`
- `11:22:58 [aec] de-aliased reference: locked=true relocks=154 losses=154 backoffs=0 in 60s`

---

## 8. 一句话总结

**rui 的 AEC 没有“坏”，但它拿到的是一条被设备 AEC 先削过、又被 dealias 在播放/采集两个时钟域之间反复重锁的参考；而这条声学链路在 127/127 下的线性上限本来就只有 14–18 dB。当前数据支持“设备 AEC 为主、rui 只做 RES/诊断”，不支持“关设备 AEC、让 rui 单独做到 20 dB”。**
