# rui → Echo Dot 2 数字链路电平审计

- 审计时间：2026-10-24 09:20–09:40 CST
- 方式：**只读**。读了两个仓库的源码、`/tmp/rui-*.log`、`/tmp/dot2-*.wav`、设备 `/tmp/server.log`、设备 `tinymix` 当前值；没有改代码/配置、没有重启 rui 或设备、没有改音量、没有 docker/git 写操作。
- 设备：Echo Dot 2（biscuit），EchoMuse `v2.18.1`，`<lan-ip>`，USB adb 在线。
- 当前 rui：dev 实例 pid 7006（09:22 启动），`dev/rui/config.yaml`，`dev/rui/env`。

---

## 结论摘要（按可能性排序）

### 0. 一句话

**127/127 时这条数字链路是 1.000×（逐位一致），不存在“藏起来的 −3/−6 dB”。**
早上 02:30–09:15 的“声音很小”首先是 **`DOT2_VOLUME_CAP=30%` → level 38 = −44.5 dB** 造成的真实衰减；09:22 调到 100% → 127/127 后，数字侧已没有可再损失的增益。127 仍嫌小的部分，证据指向：**没有响度处理（设备 output chain 关闭、rui 不做 EQ/压缩）+ TTS 节目电平只有约 −19 dBFS RMS + Dot 2 单喇叭的物理上限**，而不是某一级偷偷乘了系数。

### 1.【高置信 · 已证实】02:30–09:15 的“很小” = 30% 上限 = level 38 = **−44.5 dB**

- `dev/rui/env` 现在写的是 `DOT2_VOLUME_CAP=100`（`dev/rui/env:108`），`dev/rui/config.yaml` 是 `volume: 100`（`dev/rui/config.yaml:38`）。
- 但 03:09 启动的 `prod8` 日志仍打印 `volume ceiling 30%`（`/tmp/rui-prod8.log:14`）；02:30–03:09 的 6 次运行（`/tmp/rui-console.log`、`prod*.log`、`e2e*.log`）**全部**是 30%。
- 30% → `percent_to_level(30)=38`；固件的软件音量是 0.5 dB/step、127=0 dB，所以 38 = `(38−127)×0.5 = −44.5 dB`。设备日志在 09:11 还在把按键调到的 47 拉回 38（`[control] volume 47 above ceiling, pulling back to 38`，设备 `/tmp/server.log` 01:11:15 UTC = 09:11:15 CST）。
- 设备日志在 **01:22:14 UTC = 09:22:14 CST** 才出现 `Volume set to 127/127`；之后没有第二次 `Volume set`。也就是说，**127/127 只存在了最后几分钟**。
- 6 个录音样本（`/tmp/dot2-*.wav`）的时段全部落在这个 30% 区间。

> 这一条单独就足以解释“很小”：30%→100% 是 **44.5 dB**，振幅差 167 倍。若用户说“已经 127 还是小”，那讲的是 09:22 之后的事，不能再用 30% 解释。

### 2.【高置信 · 代码已证实；当前是否命中未证实】音量回读的两个盲区（默认 cap 陷阱 + 只信目标值），能制造“rui 255/255、设备在 30%”

`adapter.py:176-182`：

```python
ceiling = round(self.volume_cap_percent * 255 / 100)
if self._link.volume_level == capped_level(self.volume_cap_percent):
    return ceiling
return round(self._link.volume_level * 255 / 127)
```

`capped_level(percent, cap=VOLUME_CAP_PERCENT)` 的第二参默认值是 **30.0**（`volume.py:18,37`）。这里只传了一个参数，所以这个比较实际是“**当前 level 是否等于 30% 的 level（38）**”，而不是“是否等于 cap 的 level”。

后果（cap=100 时）：

- **路径 A（目标=38）**：rui/控制台要求“音量 30%”时，`set_volume_255(76)` → `set_volume(30)` → `capped_level(30,100)=38`，于是 `volume_255()` 返回 **255**。rui 的 `_reconcile_volume` 看到 `device_volume=255 == want=255`，**不会**再下发；控制台/语音工具回答“100%”，而设备实际在 38/127（−44.5 dB）。
- **路径 B（目标=127、设备实际=38）**：`volume_255()` 用的是目标 `_volume_level`，不是设备回读值（`device_link.py:442-444`）。设备被物理按键**下调**时，`device_link` 只在 `level > target` 时拉回（`device_link.py:308-316`）；低于目标只记录、不恢复。所以设备在 38、目标仍是 127 时，rui 照样显示 255/100%，且永远不会把设备拉回 127。**路径 B 不需要默认-cap 陷阱就能成立**；路径 A 是它额外的一层放大器。

仿真结果（复刻 `adapter.volume_255` 与 `percent_to_level`）：

| 请求百分数 | level | rui 期望 255 刻度 `round(p·2.55)` | adapter 实际返回（cap=100） | 差 |
|---:|---:|---:|---:|---:|
| 30 | 38 | 76 | **255** | **+179** |
| 70 | 89 | 178 | 179 | +1 |
| 90 | 114 | 230 | 229 | −1 |
| 100 | 127 | 255 | 255 | 0 |

- 70/90 的 ±1 只是**两套 rounding 路径不一致**（`p·255/100` vs `level·255/127`），会造成每 16 s 重写一次同样的 level（日志里 `Volume set to 114/127` 每 16 s 重复），听感不变。
- **30 的 +179 不是 round 误差，是“把 30% 当成 100%”**。这个值恰好是旧 30% 上限的 level 38，所以它既是历史默认，也是最容易被撞上的点。
- 现有单测只覆盖 cap=30（`tests/test_adapter.py:199-213`）和 `capped_level(100)==38`（`tests/test_adapter.py:232`），**没有覆盖 cap=100 且 level=38**，因此没被发现。

> 这一条与“当前设备日志是 127”不矛盾：设备现在确实是 127；但它解释了**为什么会出现“两端都显示满、实际很轻”**。只要有一次“把音量调到 30%”或设备被按键降到 38，就会进入这个状态。

### 3.【高置信 · 已证实】127/127 时，TTS PCM → DAC 的每一级都是 1.000×，没有额外衰减

逐级核对（详见证据 §2、§3、§4）：

| 级 | 操作 | 实测/推导增益 | 结论 |
|---|---|---:|---|
| xfyun TTS | `aue=raw` 16 kHz S16，`volume=50` | ack.wav：peak −4.6 dBFS，RMS −19.0 dBFS | 不是“很小”的源头 |
| rui 重采样 16k→48k | 线性插值 | **−0.135 dB RMS**；1 kHz −0.10，3 kHz −0.88，4 kHz −1.53 dB | 只损失一点高频 |
| float→S16 | `round(clip*32767)` | −0.0001 dB | 无 |
| adapter 切帧 | 4096 B 整帧；尾帧 EOS 时补零 | 0 dB，不丢样本 | 无 |
| 线上 | 0x02 单声道 48 kHz | 0 dB | 无 |
| 设备 `toStereo` | L=R 复制 | 0 dB（每声道） | 无 −3/−6 |
| 设备 Mixer | voice-only 直接返回 voice | 0 dB | 无 |
| responseGain | 127 时被 `cappedResponseGain` 钉在 1.0 | 0 dB | 不可能 +6/+12 |
| softVolume | level 127 → gain 1.0，early-return | 0 dB | 无 |
| output chain | 未激活（controller 没宣告 `output_chain`） | 0 dB（passthrough） | 无衰减，也无处理 |
| DAC | `PCM Playback Volume 127 127` | 0 dB | 无 |
| 外部 amp | `Ext_Amp_Gain 6dB`（与 stock HAL 一致） | 6 dB，未被固件改 | 无回归 |

**净结果：−0.14 dB RMS，1 kHz 约 −0.1 dB。** 127 时不存在“数字链路再吃掉几个 dB”的空间。

### 4.【中高置信 · 已证实】没有响度处理；stock FireOS 的扬声器 EQ/DRC 在这条路径上缺席

- adapter 的 ack 里 `features` 是空列表（`device_link.py:401-404`），config push 也没有 `responseLevel`/`outputChain`（`device_link.py:271-280`）。
- 固件的 output chain 只有在 controller 宣告 `output_chain` 时才激活（`internal/client/control.go:1556-1559`、`cmd/server.go:557,615-616`）。所以 `chain.Active()==false`，`Chain.Process` 直接 passthrough；设备日志里也**没有任何** `[speaker] ... output chain:` 行。
- rui 自己也不做 EQ/压缩/限幅（`_speak_loop` 只有重采样、`fade_in`、clip：`audio/process.py:1087-1110`）。
- 结果是：PCM 以平直响应进 DAC。Dot 2 的驱动实测 **250 Hz 相对 1 kHz 为 −18 dB、200 Hz 以下几乎不辐射、3.15 kHz 有 +8 dB 峰**（`/tmp/echomuse-inspect/device/tools/speaker_profile/README.md:65-73`）；stock FireOS 的 EQ 是 **+26 dB @150 Hz、−15 dB @2.8 kHz**（同文件 71-73 行）。
- 注意：这条更像“**薄/刺/不像 stock Alexa**”，不一定是宽带声压下降。对现在这个 `x4_lingxiaowan_boy` 的 ack.wav，能量 52% 在 300–1000 Hz、40% 在 1–3 kHz，150 Hz 只占 0.02%；所以 **+26 dB 低音补偿对它几乎不增加 RMS**，−15 dB @2.8 kHz 反而会降低一点响度。**不要把“缺 EQ”当成已证实的“小”的主因**，它主要是音色差异。

### 5.【中置信 · 已证实测量】TTS 节目电平本身只有约 −19 dBFS RMS，峰均比约 14 dB，链路不做任何归一化/压缩

- `ack.wav`（同一 provider、同一 voice `x4_lingxiaowan_boy`、同一 `volume=50`）：peak **−4.6 dBFS**、RMS **−19.0 dBFS**、crest 14.4 dB。
- 16k→48k 后 RMS −19.1 dBFS。
- 没有任何 AGC/limiter/normalize 把平均电平抬到接近 0 dBFS。对一个以语音为主的回复，−19 dBFS RMS 是正常母带电平，但意味着**平均响度比满幅素材低约 19 dB**；127 只是“不再衰减”，不是“把素材推满”。
- xfyun 的 `volume` 被硬编码为 50（`providers/xfyun_tts.py:61`），没有走配置。50 是 vendor 的默认/正常值；ack 的 peak 已到 −4.6，说明不是被砍半。**没有证据说 50 比 100 低 6 dB**；要证实只能做一次对照合成（本次未做，避免外部 TTS 费用）。

### 6.【未证实】Dot 2 单喇叭的物理上限

127 是固件允许的最大值（`swvolume.go:25-33`：≥127 时 gain=1），DAC 也是 unity。**数字上已经到顶**。剩下的是 1.6 英寸单喇叭在 0 dB 下的最大声压；没有 SPL 计或 127 下的校准录音，无法判断“127 应该有多响”。这是目前唯一无法用现有证据排除的原因。

### 7.【已排除】几个常见的“-3/-6 dB”怀疑

- `toStereo`：L/R 复制，不是 L+R 求和，**没有 −3/−6 dB**（`pcm_speaker.go:703-719`）。
- 设备混音：voice-only 时 `Mixer.Mix` 直接返回 voice，不乘 gain；只有 music 存在时才 `mixInto`（`mix.go:95-116`）。
- 4096 字节切帧：和设备 `periodSize=2048` 样本（mono 4096 B）完全对齐（`pcm_speaker.go:25,47-51`）；`_drain_frames` 只发整帧（`adapter.py:295-300`），最后不足一帧的部分在 EOS 时补零后发送（`adapter.py:307-322`），**不丢样本**。
- EOS/idle：0.4 s 空闲只影响“什么时候结束流/尾帧补零”，不影响已发送样本的幅度。
- `responseLevel`：没有被人设成非 0；环境里没有 `RESPONSE_LEVEL`，默认 `low`=0 dB；且 127 时 `cappedResponseGain` 把 `response × volume` 上限钉在 1.0（`responsegain.go:50-58`），**medium/high 在满音量下也被削成 0 dB**。
- resample：只 −0.135 dB RMS。

---

## 证据

### 1. 音量映射、时间线、设备实际值

#### 1.1 配置与日志

| 证据 | 值 | 位置 |
|---|---|---|
| rui 配置目标 | `devices[].volume: 100` | `dev/rui/config.yaml:38` |
| dev 上限 | `DOT2_VOLUME_CAP=100` | `dev/rui/env:108` |
| 09:22 运行 | `volume ceiling 100%` | `/tmp/rui-morning6.log:14` |
| 09:22 设备 | `Volume set to 127/127` | 设备 `/tmp/server.log` 2026/10/24 01:22:14 UTC |
| 03:09 运行 | `volume ceiling 30%` | `/tmp/rui-prod8.log:14` |
| 03:09 设备（同时间段） | 按键 47 被拉回 `38/127` | 设备 `/tmp/server.log` 01:11:15 UTC |
| 09:19–09:22 | 90% = `114/127`（−6.5 dB） | 设备日志 01:19:02–01:22:14 UTC |
| 09:17–09:19 | 70% 附近 = `89/127`（−19 dB） | 设备日志 01:17:25 UTC |
| 90% 的历史记录 | `90% = level 114 = −6.5 dB` | `docs/handoff-morning.md:12` |

设备日志的时间是 **UTC**；本地是 UTC+8。09:22:14 CST = 01:22:14 UTC。

#### 1.2 百分数 ↔ 设备 level ↔ dB

固件 law：`VolumeGain(level)=10^((level−127)/40)`，即每 step 0.5 dB，127=0 dB（`/tmp/echomuse-inspect/device/internal/bindings/speaker/swvolume.go:21-33`）。

| 百分数 | `percent_to_level` | 设备 dB | 说明 |
|---:|---:|---:|---|
| 30 | 38 | **−44.5** | 02:30–09:15 的实际值，“几乎听不见” |
| 70 | 89 | **−19.0** | 早上短暂到过 |
| 90 | 114 | **−6.5** | 09:19–09:22 |
| 100 | 127 | **0.0** | 09:22 之后 |

30→100 是 **44.5 dB**（振幅 ×167.8）。这是本次审计里最大的一级“衰减”，也是唯一一个能单独解释“很小”的量级。

#### 1.3 设备硬件回采（AEC `ref`）验证 dB 法则

设备每 ~1 s 打一行 `[aec] att=… mic=… out=… ref=…`；`ref` 是 **post-volume** 的硬件 loopback 的 int16 RMS（`internal/aec/aec.go:642`，`docs/aec-takeover.md:12-19`）。同一段回复、不同 level 下的中位数：

| 设备 level | 软件增益 | 理论 dB | n | `ref` 中位数 | 相对 127 实测 | 理论−实测 |
|---:|---:|---:|---:|---:|---:|---:|
| 38 | 0.00595 | −44.5 | 7 | 17 | −39.9 | −4.6* |
| 89 | 0.1122 | −19.0 | 3 | 217 | −17.8 | −1.2 |
| 114 | 0.4732 | −6.5 | 12 | 770 | −6.8 | +0.3 |
| 127 | 1.0000 | 0.0 | 17 | 1678 | 0.0 | 0.0 |

\* 38 时 `ref` 只有 9–17 LSB，已经在 AEC diag 的量化/静音门限附近，所以 4.6 dB 的偏差是测量底噪，不是音量 law 错。

**结论：89/114/127 三档在 ±1.2 dB 内吻合 0.5 dB/step 法则。** 设备确实把 127 当成 0 dB，把 38 当成 −44.5 dB；`Volume set to 127/127` 不是摆设。

#### 1.4 `volume_255` / `set_volume_255` 的两处不一致

**(a) 默认 cap 陷阱**（详见结论 2）：

- `adapter.py:180` 只传一个参数给 `capped_level`；默认 cap 是 30（`volume.py:37`）。
- cap=100 时 `capped_level(100)=38`，于是 level=38 被误判成“在 ceiling”。
- `volume_255` 在 30% 处返回 255，而正确值应是 76（cap=100、30%）或 76（cap=30 的 ceiling）。
- 该行为在 cap=30 的测试里恰好正确（`tests/test_adapter.py:199-201`），所以单测没抓到。

**(b) 目标值 vs 实际值**：

- `volume_255` 用的是 `self._link.volume_level`——adapter 的**目标**，不是设备回读值（`device_link.py:442-444`）。
- 设备 `volume_state` 低于目标时，adapter 只记录 `_reported_level`，不纠正（`device_link.py:308-316`）。
- 因此 `/api/v1/device/volume` 返回的 255 是“我方目标”，**不能当作设备实际电平的独立证据**。当前它返回 `{"volume":255,"range":"0-255"}`（09:32 只读 GET）。
- 真正能证明设备在 127 的是设备自己的 `Volume set to 127/127` + 没有后续 `Volume set` 行；这一点当前成立。

**(c) 30% cap 下的 reconcile 打架**：

cap=30、config=100 时：`volume_255()` 返回 76，rui `want=255`，于是每 16 s 调一次 `set_volume(255)`；adapter 把它夹到 30% → level 38；rui 把本地 `device_volume` 记成 255，下次读回 76 再写。设备日志里 `Volume set to 38/127` 每 16 s 重复一次（01:15:50、01:16:08、01:16:24、01:16:35、01:16:52、01:17:08 UTC），就是这个循环。**听感不变（一直在 38），但“rui 以为 100%”是假的。**

#### 1.5 部署路径里还留着 30%

- `deploy/rui-dot2-override.yml:24`：`DOT2_VOLUME_CAP: "30"`。
- `scripts/doctor.py:26`：默认 `VOLUME_CAP=30`。
- `scripts/check.sh:30`：断言 `dev.volume <= 30`。
- `docs/deploy-nas.md:72-73`：仍写“30% 音量上限由 adapter 强制”。

如果实际接管设备的是 NAS 容器（而不是 09:22 的 dev 实例），它会在连接时把设备压回 38/127。设备当前是 127，说明现在连的是 dev 实例；但**部署路径与 dev 配置不一致**，这是一个随时会复发的“很小”来源。

### 2. TTS → rui 的电平

#### 2.1 xfyun 输出

`providers/xfyun_tts.py:52-62`：

```python
"aue": "raw",
"auf": f"audio/L16;rate={rate}",
"volume": 50,
```

- 请求 16 kHz（`cfg.audio.voice.rate=16000`，`dev/rui/config.yaml:56`），headerless S16LE。
- `volume=50` 是 vendor 默认，硬编码，不读配置。
- 本地已有的 `ack.wav`（同一 provider/voice，非本次调用产生）实测：
  - 16 kHz mono，0.49 s，peak **−4.6 dBFS**，RMS **−19.0 dBFS**；
  - 频带能量：0–100 Hz 0.00%，100–200 Hz 0.02%，200–300 Hz 6.97%，300–1000 Hz 52.16%，1–3 kHz 40.40%，3–4 kHz 0.08%；
  - 谱质心 1028 Hz。

这不像“被 TTS 砍了 6–20 dB”的素材；peak −4.6 已经接近满幅。`volume=50` 是否是 0 dB，vendor 文档没有给出线性 law，**未证实**。

#### 2.2 rui 的重采样与量化（离线复刻）

用仓库里的 `audio/resample.py` 同一算法（`np.interp`）对 `ack.wav` 做 16k→48k：

| 项 | 结果 |
|---|---:|
| RMS 变化 | **−0.135 dB** |
| peak 变化 | 0.000 dB |
| float→int16→float | −0.0001 dB |
| 100 Hz 正弦 | −0.00 dB |
| 500 Hz | −0.02 dB |
| 1 kHz | −0.10 dB |
| 2 kHz | −0.39 dB |
| 3 kHz | −0.88 dB |
| 3.4 kHz | −1.12 dB |
| 4 kHz | −1.53 dB |
| 6 kHz | −3.06 dB |

线性插值在 16k→48k 是“保持 DC/低频、轻微压高频”。**总 RMS 只掉 0.14 dB**；不是“声音小”的原因，最多让 3–4 kHz 的辅音略钝。

`_speak_loop` 其余处理：

- `np.frombuffer(chunk, "<i2") / 32768.0`（`audio/process.py:1087-1088`）；
- 每个句首 `fade_in` 12 ms（`audio/process.py:103,204-210`），只影响每句前 12 ms；
- `np.round(np.clip(audio, -1, 1) * 32767)`（`audio/process.py:1110`）是 unity；
- 没有 AGC/normalize/limiter。

#### 2.3 结论

TTS 素材本身 RMS ≈ −19 dBFS，peak ≈ −4.6 dBFS；rui 不加增益也不衰减。127/127 时 DAC 拿到的就是这路 PCM（只多了 −0.14 dB 的线性插值损失）。

### 3. adapter 播放路径（切帧、EOS、flush）

#### 3.1 4096 字节对齐

- 线上格式：`0x02`，S16LE mono 48 kHz，4096 B/帧（`frames.py:31-33,47-51`）。
- 设备：`periodSize=2048` 样本，`monoPeriodBytes = periodSize*2 = 4096`（`pcm_speaker.go:25,47-51`）；`PumpPeriod` 对 4096 B 做 `toStereo`（8192 B = 2048 stereo frames），正好一个 period。
- `_drain_frames` 只在 `len(buffer) >= 4096` 时发整帧（`adapter.py:295-300`）；不足一帧的尾巴留在 buffer，EOS 时补零到 4096 再发（`adapter.py:307-322`）。**不丢、不重复、不改变幅度。**

#### 3.2 设备流统计

设备日志（09:22–09:23 CST，127/127）：

```
01:23:00 [speaker] voice stream complete — returning to silence
  (periods=461 underruns=0 minDepth=-1 primeWait=38ms recvSpan=19366ms maxGap=230ms)
01:23:00 [speaker] voice stream complete — returning to silence
  (periods=1 underruns=0 minDepth=-1 primeWait=19792ms recvSpan=19767ms maxGap=400ms)
01:23:19 ... (periods=55 underruns=0 ... recvSpan=2034ms)
01:23:19 ... (periods=1 underruns=0 ... primeWait=2480ms recvSpan=2437ms)
```

- 主回复 461 periods × 42.667 ms = 19.67 s，`recvSpan=19.366 s`，**0 underrun**；说明 rui 的 48 kHz PCM 按实时速率到达，设备没有饿死。
- 每次主流之后都有一个 **1-period 的小流**（42.7 ms）。它的 `primeWait/recvSpan` 接近整段回复时长，说明这 1 帧在统计上被归到了主流的收帧窗口，而不是一次干净的“新流从 0 开始”；**具体机制未完全定死**（尾帧、`eosPending`、flush 三者都可能参与）。可以确定的是：它只有 42.7 ms，**不影响主流的电平**；若听到句尾“吞一下/停一下”，这是候选，不是音量问题。
- 没有 `[speaker] ... output chain:` 行 → 设备 output chain 未激活（见 §4.3）。

#### 3.3 `stop_playback` / flush 与 `eosPending`

- 每个回复的第一个 chunk，rui 会 `await adapter.stop_playback()`（`audio/process.py:1101-1108`）→ `link.flush()` → 设备 `speaker_flush`（`device_link.py:431-433`）。
- 设备 `flush()` 若 `active` 会置 `discarding=true`，并**总是**置 `eosPending=true`（`stream.go:196-215`）。
- 正常情况下，上一条回复的 EOS 早已到达（`active=false`），所以不会 `discarding`；但 `eosPending` 会残留到新流，令 prime gate 提前打开、并可能让新流在第一次 buffer 下陷时被当成“自然结束”。这**可能**是日志里 1-period 小流/`primeWait` 异常的来源之一（未闭环），但**只影响流边界/时序，不影响样本幅度**。
- 若上一个流的 EOS 尚未到达就 flush（例如 cue→reply 的窗口），`discarding` 会吞掉新流直到 EOS——这是“丢音/吞字”风险，不是“变小”。

### 4. 设备端：音量、混音、DAC、DSP

#### 4.1 音量（软件）

`swvolume.go:25-33`：

```go
func VolumeGain(level int) float64 {
    if level <= 0 { return 0 }
    if level >= 127 { return 1 }
    return math.Pow(10, float64(level-127)/40)
}
```

`apply` 对每个 period 逐样本乘 gain；`tgt==1 && cur==1` 时直接返回（`swvolume.go:63-82`）——**127 时 bit-exact passthrough**。`volume_set` 路径见 `internal/server/volume.go:100-128`。

#### 4.2 `toStereo` 与 Mixer

`toStereo` 把 mono 样本复制到 L/R（`pcm_speaker.go:703-719`），**没有 0.5 系数**。`Mixer.Mix`：

- `voice != nil && music == nil` → `stepGain(target); return voice`（`mix.go:103-106`）；
- 只有 `voice != nil && music != nil` 才 `mixInto(voice, music)`（`mix.go:112-115`）；
- voice 本身从不 duck（`mix.go:103-106` 的注释和代码）。

所以 voice-only 的 TTS **没有 −3/−6 dB**。设备 `Right Channel Only Off`、`Audio_DacMux_Setting Off`、`HPL/HPR` 都 On（tinymix）也不影响，因为 L=R。

#### 4.3 输出链 / DSP 状态（只读 `tinymix`）

设备当前（09:33 只读）：

| ctl | 值 | 含义 |
|---|---|---|
| `PCM Playback Volume` (61) | `127 127` | DAC 数字音量 unity |
| `Ext_Speaker_Amp_Switch` (5) | `On` | 外部喇叭 amp 开 |
| `Ext_Amp_Gain` (13) | `6dB` | 可选 `6/12/18/24dB`，当前最小档；stock HAL 也没写它 |
| `HP Driver Gain Volume` (62) | `6 6` | stock `ext_speaker_output` 路径也是 6 |
| `DRC Control` (67) | `Disabled Disabled` | 无 DRC |
| `biquad coefficients` (84) | 全 0 | stock `audio_device.xml` 是一长串非零系数；当前未加载 |
| `MFP Gpio Mute` (88) | `On` | 与仓库参考 dump 相同；音频仍出声，说明不是本路径的静音 |
| `Right Channel Only` (86) | `Off` | stock HAL `ext_speaker_output` 写 `On`；L=R 时无电平影响 |
| `Ext_Headphone_Amp_Switch` | `Off` | 走喇叭，不走耳机 |

`/system/etc/audio_device.xml:12-16` 的 stock `ext_speaker_output`：`Audio_DacMux_Setting=Off`、`Right Channel Only=On`、`HP Driver Gain=6`。`/system/etc/audio_init.sh` 只写 HPL/HPR，不写 amp gain。

**结论**：DAC 和模拟增益与 stock 一致（`PCM 127`、`HP 6`、`Ext_Amp 6`）；差异在 **DRC/biquad/Right Channel Only**。这些是音色/保护，不是“把整体乘 0.5”的证据。`Ext_Amp_Gain` 是唯一还有 +18 dB 余量的硬件档位，但 stock 也没用它，**不能算回归**。

#### 4.4 responseLevel

- 设备 env 没有 `RESPONSE_LEVEL`，默认 `low` = 0 dB（`internal/config/config.go:58-60,421-427`）；`state.json` 只有 `{"muted":false}`。
- adapter 的 config push 不带 `responseLevel`（`device_link.py:271-280`）。
- 即使设成 medium/high，127 时 `cappedResponseGain(requested, volume=1.0)` 会把 `requested` 削到 1.0（`responsegain.go:50-58`）——**满音量下 +6/+12 dB 不可用**。这是固件的有意设计：response×volume 不允许超过 unity。
- 因此“谁把 responseLevel 设成非 0”在这条路径上**既没有发生，也不会有用**。

### 5. 录音的离线测量（为什么做不出 30/70/100 表）

#### 5.1 样本与对应运行

| 文件 | mtime (CST) | 对应运行 | 上限 | rui AEC | 时长 |
|---|---|---|---:|---|---:|
| `/tmp/dot2-mic.wav` | 02:32 | standalone P0 探针 | 30% | rui 无参考，off | 42.4 s |
| `/tmp/dot2-live.wav` | 02:42 | `/tmp/rui-prod.log` 02:42:36 | 30% | off | 133.9 s |
| `/tmp/dot2-e2e.wav` | 03:01 | `prod5` 03:01:48 / `e2e3` 02:59:38 | 30% | off | 124.9 s |
| `/tmp/dot2-e2e2.wav` | 03:04 | `prod6` 03:04:04 | 30% | off | 86.8 s |
| `/tmp/dot2-e2e3.wav` | 03:05 | `e2e5` 03:05:06 | 30% | off | 42.8 s |
| `/tmp/dot2-e2e4.wav` | 03:09 | `e2e6` 03:08:19 | 30% | off | 91.4 s |

所有对应日志都打印 `volume ceiling 30%` + `aec off: device has no reference channel`；**没有 70%/90%/100% 的录音**。所以任务里“30/70/100 三档比较”的前提不成立。

#### 5.2 内容质量

| 文件 | 200–4000 Hz 0.5 s RMS p50 | p99 | max | 说明 |
|---|---:|---:|---:|---|
| dot2-e2e.wav | −40.3 | −34.2 | −33.9 | 含每 20.496 s 一次的满幅事件 |
| dot2-e2e2.wav | −41.0 | −33.9 | −33.9 | 同上 |
| dot2-e2e3.wav | −41.1 | −33.9 | −33.9 | 同上 |
| dot2-e2e4.wav | −40.3 | −33.9 | −33.9 | 同上 |
| dot2-live.wav | −40.3 | −34.2 | −33.9 | 同上 |
| dot2-mic.wav | −40.3 | −26.2 | −26.2 | 30.0 s 处有更高的语音/人声，非音量差 |

- 所有文件的满幅事件簇（`|x|≥32000`）都开始于 **10.008 s / 30.504 s / 51.000 s / 71.496 s / 91.992 s / 112.488 s / 132.984 s**，间隔 **20.496 s**；而且每个周期的首样本值依次是 `32000, 32001, 32002, 32003, 32004, 32005, 32006`（确定性递增），随后在 ~10.328 s 出现 `−32512`。这是**确定的周期性伪影**（漂移/计数器过零），不是语音。它与 `docs/p0-p1-report.md:100` 和 `docs/handoff-morning.md:62` 记录的 **~0.05 Hz 低频漂移/满幅摆动**一致（0.05 Hz 周期 ≈20 s）。
- 块均值 lag-1 自相关 0.94（慢漂移），叠加上述满幅事件；以 `dot2-e2e3.wav` 为例，200–4000 Hz 与 4–8 kHz 的能量约各占一半，像被漂移/噪声污染的房间录音，不是干净的 TTS 回采。
- 设备采集链还有 `MIC_GAIN_DB=24` 预抬升（`internal/config/config.go:228`），录音不是校准 SPL，也没有绝对灵敏度。
- 交叉相关只能找到 ~0.47 的弱相似；**无法可靠地挑出“同一句 TTS”**做 A/B。
- 对应运行全部是 30%，且 rui 侧 AEC 关闭（设备侧 AEC 开，但 adapter 不提供参考通道）；录音是设备 AEC 后的 mono mic，**既不是播放参考，也没有音量标定**。

**因此：不能从这 6 个 WAV 得出“30/70/100 的 RMS 表”。** 唯一可用的相对电平证据是 §1.3 的硬件 `ref` 表（post-volume，跨 level 同一法则），它比这些录音更干净。

#### 5.3 录音里能得到的唯一“量级”锚点

`docs/p0-p1-report.md:43-45`：30% 音量下把 48 kHz 自注入送进喇叭，设备麦克风录到的注入段 **RMS ≈ −45 dBFS**。

- 这证明 30% 下“扬声器→空气→麦”只剩约 −45 dBFS，确实非常轻。
- 若线性外推到 127（+44.5 dB），麦克风会到约 **−0.5 dBFS**，即可能削顶——这说明 30%→100% 的跳变是巨大的，不能拿 30% 的录音去推断 127 的听感。

### 6. 已证实 / 推测 / 还缺什么

#### 已证实（有代码或日志直证）

1. 30% cap → level 38 → **−44.5 dB**；02:30–09:15 的所有运行和 6 个录音都在这个区间（`volume.py:18`、`swvolume.go:25-33`、`prod8.log:14`、设备 `Volume set to 38/127`）。
2. 09:22:14 设备收到 `Volume set to 127/127`，之后无 `Volume set`；DAC `PCM Playback Volume 127 127`（设备日志 + tinymix）。
3. 127 时 adapter→设备是 **bit-exact**：4096 对齐、`toStereo` 复制、Mix voice-only 直通、`softVolume` unity early-return、output chain passthrough。
4. `volume_255()` 在 cap≠30 且 level=38 时返回 255（默认 cap=30 的漏参）；cap=100 下“设 30%”会变成“rui 认为 100%”。
5. `device_link` 只对“高于目标”的 `volume_state` 拉回，低于目标的回读只记录不恢复（`device_link.py:308-316`）。
6. 30% cap 下 rui 的 reconcile 与 adapter 的 clamp 互相打架，每 16 s 重写 38（设备日志多次 `Volume set to 38/127`）。
7. 设备 output chain 未激活（adapter features 为空；日志无 output chain 行）；rui 不做 EQ/压缩。
8. TTS ack.wav：peak −4.6 dBFS、RMS −19.0 dBFS；16k→48k 线性插值 −0.135 dB RMS。
9. responseLevel 默认 low=0 dB，且 127 时被 `cappedResponseGain` 钉在 unity。
10. 设备 current `Ext_Amp_Gain=6dB`（最小档）、`HP Driver Gain=6`、`DRC=Disabled`、biquad 全 0；与 stock HAL XML 的 `HP 6` 一致，与 `Right Channel Only On`/非零 biquad 不一致。

#### 推测（有间接证据，未闭环）

1. 127 仍嫌小的主要剩余原因是 **1.6 英寸单喇叭的物理上限**；没有 SPL 测量，无法定量。
2. 没有响度处理会让同一数字电平听起来更薄/更小；但 ack 的能量主要在 300–3000 Hz，stock 的 +26 dB@150 Hz 对它增益很小，**不能把“缺 EQ”说成已证实的主因**。
3. `xfyun volume=50` 可能比 `volume=100` 低几个 dB；ack 的 peak −4.6 说明不是线性砍半，**未证实**。
4. 1-period 尾流/`eosPending` 可能导致句尾的轻微吞音/停顿；**不是电平**。
5. `Ext_Amp_Gain` 还有 12/18/24 dB 档，但 stock 也没用 6 dB 以上，**不是回归**。

#### 还缺哪条证据

1. **设备当前 in-memory `volume_state`**（尤其在一次物理按键之后）。设备日志只在 `Set()` 时打印；adapter 不记录“低于目标”的回报。要证明“现在真的是 127”，最直接的是让设备主动上报一次 `volume_state`（只读查询，不是设音量）。
2. **一次回复的 adapter 出站 PCM（48 kHz）+ 同一秒的设备 `ref`**。现在只有 `ref` 的相对值；要证明“rui 发出去的就是 −19 dBFS”，需要在只读路径上抓一份 `play()` 入参/`send_pcm` 出参，或让设备把 `periodRMS` 打进日志。
3. **127 下的校准录音**：固定距离、同一句 TTS、同一麦克风、AEC 状态已知；最好直接用设备 Ch8 硬件回采（post-volume），而不是房间麦。
4. **同一素材在 38/114/127 三档的录音**：现有 6 个 WAV 全是 30%，无法做任务要求的表。
5. **stock FireOS 在同一台设备、同一音源下的对照**：确认 +26 dB@150 / −15 dB@2.8k 的 EQ 和 DRC 是否真的在 stock 路径生效；本次只看到了 HAL XML 的初始值和 `speaker_profile` 的历史测量。
6. **xfyun `volume=50` 与 100 的本地对照**（需要一次外部 TTS 合成；本次未做，避免费用）。

### 7. 若要验证的最小只读实验（不做修复）

1. **不改音量，只读设备当前电平**：在有人按一下音量键后，立刻读设备 `/tmp/server.log` 是否出现 `Volume set to X/127`；或通过现有 link 的 `volume_state` 回调记录（需要一次只读查询，不要 `volume_set`）。这能区分“目标 127 / 实际 38”。
2. **用现有 `ack.wav` 做离线链路复算**：把 ack.wav 按 `resample.py` 升到 48k，再按 `toStereo`+`softVolume(127)` 复算，确认输出与输入逐样本一致（本次已做，−0.135 dB RMS）。
3. **同素材三档录音**（需要用户配合改音量，超出本次只读范围）：在同一位置、同一麦克风、同一句 `ack.wav` 下录 38/114/127，算 200–4000 Hz 的 1 s RMS；预期差值为 44.5/6.5/0 dB。这是唯一能把“数字电平”与“实际声压”闭环的实验。
4. **SPL 计**：1 m 处、同一句 TTS、127/127，记录 dB(A) 峰值/等效声压；没有这个数就无法判断 127 是“设备极限”还是“还有余量”。
5. **设备 Ch8 回采**：用 `tools/capture_mics` 在播放时抓 Ch8（post-volume 硬件参考），离线算 RMS；这比房间麦干净，但会短暂停 EchoMuse，**本次未做**（属于重启/接管设备，超出只读边界）。

---

## 附：本次没有做的事

- 没有改任何代码/配置/音量，没有重启 rui 或设备，没有 docker/git 写。
- 没有调用外部 TTS；`ack.wav` 是本地已有文件。
- 没有运行 `capture_mics`/`speaker_profile`（它们会停 EchoMuse，属于接管设备）。
- 没有把 `tinymix` 的任何值写回；只读了当前值。
- 没有用 `volume_set`/`volume_state` 主动查询设备（会与 rui 的连接竞争）；设备当前 127 的证据来自设备自身日志的最后一次 `Volume set`。
