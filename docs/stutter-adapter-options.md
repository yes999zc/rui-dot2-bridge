# Dot 2 播放 400 ms 空档：adapter 侧兜底方案

- 日期：2026-10-24
- 范围：只读调研。未改代码/配置，未重启 rui，未碰设备音量/静音（设备当前 `0/127` 保持静音）。
- 相关：rui issue #1；同目录 `docs/stutter-rui-xiaoai.md`（小爱对照与 406 ms flush 竞态）。
- **当前状态**：adapter 侧方案已落地（`7a22a4b`，修正 `c766fcd`）。设备日志实测三条回复：`primeWait=14–22 ms, underruns=0, in-stream maxGap=108–156 ms`；`calls.log` 不再出现 400 ms 级块间空档；58 个单测通过。

---

## 0. 结论摘要

1. **400 ms 要分成两类看**：
   - **A 类（每轮开头）**：`stop_playback()` 的 `speaker_flush` 走 `/control`，PCM 走 `/data`；两条 TCP 无跨连接顺序，设备会先收下前两个 100 ms 块、再处理迟到的 flush，把它们丢掉并重新开流。`calls.log` 的 11 次 406/407/417 ms 空档全部是这一类，`stutter-rui-xiaoai.md` 已用设备 `periods=4 recvSpan=5ms` 对齐证实。
   - **B 类（句中 TTS 到达空档）**：rui 的 `Pacer` 稳态只保 250 ms（上限 320 ms）；Dot 若没有额外 lead，400 ms 的 TTS 断供会把设备队列排空，下一段重新 prime **1.024 s**，把 400 ms 放大成约 1.0–1.4 s。
2. **已落地的修复（`7a22a4b`）**：
   - **A 类用“flush + 控制面 ping/pong 屏障”**：flush 仍发（它同时是设备 1.024 s prime 的旁路），但 `DeviceLink.flush()` 紧接着在 `/control` 上发 ping，等设备按序回 pong 后才返回；这样 flush 一定先于首帧 PCM 被处理。代价约一个 RTT（5–50 ms），实测 `primeWait` 回到 14–22 ms。
   - **B 类用 450 ms 预缓冲**：`play()` 先攒 **450 ms（43200 B）** 再开始下发，短回复 0.35 s 兜底开声；`EOS_IDLE_S` 保持 1.2 s。首声约 +0.45–0.5 s，400 ms 空档落在 lead 内。
3. **设备侧容量远大于“约 300 ms”**：`0x02` = 4096 B = **42.667 ms**；深度队列 **128 period ≈ 5.46 s**；ALSA 硬件环 **4×2048 stereo frames = 170.7 ms**；最后一个 period 取走后还会响 **213.3 ms**；prime 门 **24 period = 1.024 s**。瓶颈不在设备容量，而在“flush 与首帧的顺序”和“adapter 有没有 lead”。
4. **`wait_consumed` 不可行，也不解决问题**：设备没有逐块播放头，只有流排空后的 `playback_stats`（`periods/underruns/minDepth/primeWaitMs/recvSpanMs/maxGapMs/bytesRecv`）。要补必须改固件 + 协议；而且 `Credit` 是“最多领先多少”，不是“至少领先多少”，不会填 400 ms 空档。
5. **方案取舍**：
   - **(a) 预缓冲**：已实现，450 ms；能覆盖不超过 lead 的源空档，代价是每个回复 +450 ms。
   - **(b) `wait_consumed`**：不推荐；固件无播放头，且 `Credit` 反而限制 lead。
   - **(c) 自建队列 + 42.7 ms 定速 + 空档补零**：单独不增加 lead，不能消除源空档；作为 (a) 的发送层升级，可在空档超过 450 ms 时防止二次 prime。当前“攒够后 burst”实测未出现 underrun，暂不必上。
   - **(d) idle no-op（删除 flush）**：已被 `7a22a4b` 否决。它消掉 A 类，但恢复 1.024 s prime，实测首声 0.76–0.97 s，比 406 ms 更差。
6. **验证**：单测已覆盖 43200 B 阈值、stop/EOS/屏障顺序；设备侧静音下用 `DOT2_INJECT_PORT` 注入固定 PCM 做 A/B；人耳窗口验收“无句中卡顿、首声增加 ≤0.5 s”。

---

## 1. 两类 400 ms 的机制

### 1.1 A 类：flush 与首帧的跨连接竞态（已修复）

rui 在每轮回答第一块前调用 `adapter.stop_playback()`（`house-ear: src/house_ear/audio/process.py:1095-1108`）。修复前 adapter 无条件 `link.flush()`：

```python
# 修复前（adapter.py:414-423）
self._playing = False
self._frame_buffer.clear()
await self._link.flush()          # -> {"type":"speaker_flush"} on /control
```

- flush 在 `/control`（`device_link.py:441-453`），PCM 在 `/data`（`device_link.py:424-432`）；两条 WebSocket 是两条 TCP 连接，**没有跨连接顺序**。
- 设备可以先把 data 上的前两个 100 ms 块 `PumpPeriod` 掉（4 个 period ≈ 171 ms），再读到 control 上的 flush；`flush()` 若看到 `active`，会 arm `discarding`（丢弃后续直到 EOS）并 drain，前两块因此被丢，流结束，下一段重新 prime。
- 证据：`calls.log` 中 11 次 406/407/417 ms 空档，**11/11 都紧跟在每轮第 2 块之后**；设备日志同一秒出现 `[control] speaker_flush received` + `voice stream complete (periods=4 ... recvSpan=5ms)`。
- 修复：`DeviceLink.flush()` 在发完 `speaker_flush` 后，**在同一条 `/control` 上**发 `{"type":"ping","id":N}` 并等 `pong`（`device_link.py:441-470`）。设备按序处理 control 消息，pong 返回即证明 flush 已被应用；此后 data 上的首帧不可能“抢在 flush 前”。实测 `primeWait` 从 762–968 ms 回到 14–22 ms。

### 1.2 B 类：TTS 到达空档（已用 lead 覆盖）

rui 的 `Pacer`（`house-ear: src/house_ear/audio/process.py:95-97, 116-145`）：

```python
PREBUFFER_S = 0.25
CATCHUP = 1.0
MAX_LEAD_S = 0.32
```

- **稳态目标是 250 ms，不是 320 ms**；`MAX_LEAD_S` 只是上限。`CATCHUP=1.0` 表示当前版本不随块增长 lead。
- rui 日志实测 `lead max 343–344 ms`，是突发瞬间的峰值；稳态仍是 250 ms。
- 我们的 adapter 现在先攒 450 ms，再按到达 burst 下发（`adapter.py:372-397`）。因此设备侧在开声时至少有 450 ms 音频 + 170.7 ms ALSA 环 ≈ **620 ms** lead；400 ms 的源空档落在里面。
- 如果空档长于 lead，设备队列会排空：`drained()` 记 underrun，`playing=false`；下一块音频要等 24 period（1.024 s）或 EOS 才会播。此时表现不是“400 ms”，而是约 1.0–1.4 s。这是 450 ms lead 之外仍需监控的残余风险。

### 1.3 缓冲账（为什么 450 ms 够）

| 层 | 时长 | 说明 |
|---|---:|---|
| rui `Pacer` 稳态 lead | 250 ms | 到达 adapter 之前 |
| adapter prebuffer | **450 ms** | `7a22a4b` 实现，43200 B |
| 设备 ALSA 硬件环 | 170.7 ms | 4×2048 stereo frames |
| 设备深度队列 | 最多 5.46 s | 128 period，非时刻满载 |
| 设备 prime 门 | 1.024 s | 当前被 flush 旁路，实测 `primeWait 14–22 ms` |

- 当前实际 DAC 侧 lead ≈ 450 + 170.7 ≈ **621 ms**（prime 已被旁路，不再额外等 1 s）。
- 覆盖 400 ms 后余量 ≈ **220 ms**；即使 TTS 再抖 100–150 ms，也不至于排空。
- 若把 prebuffer 降到 300 ms，DAC lead ≈ 471 ms，余量 71 ms，偏紧；450 ms 是保守值，也是已实现/已验证值。

---

## 2. 当前实现与两个中间版本的教训

### 2.1 当前 adapter 播放路径（`7a22a4b`）

| 函数 | 行为 | 关键点 |
|---|---|---|
| `Dot2Adapter.play()` `adapter.py:348-387` | 校验 48 kHz mono；首块 `reset_stream()`；响度处理；追加 `_frame_buffer`；未开始时按 `_prebuffer_bytes` 判断，未满则起 0.35 s 兜底 task 并返回；已开始则 `_drain_frames()` | 43200 B = 450 ms；首声延迟约 450 ms |
| `_start_after_prebuffer()` `adapter.py:390-399` | 0.35 s 后若还没开始且有数据，置 `_stream_started=True` 并 drain | 短回复（<450 ms）不会无限等 |
| `_drain_frames()` `adapter.py:413-422` | 未开始直接 return；否则每凑满 4096 B 就 `send_pcm()` | 一次 `play()`（100 ms = 9600 B）发 2 个整帧 + 留 1408 B |
| `_eos_after_idle()` `adapter.py:424-448` | **1.2 s** 无新块：把不满一帧的尾巴 10 ms 淡出、补零到 4096 B，发 `0x03` | 1.2 s 覆盖设备尾部 ~213 ms，也避免 0.7 s 拆流 |
| `stop_playback()` `adapter.py:450-471` | 取消 EOS/prebuffer task；清本地 buffer；`link.flush()` | **总是 flush**（保留 prime 旁路）；`flush()` 内含 ping/pong 屏障；**active 流时还缺一个 EOS**（见 §7.9） |
| `DeviceLink.flush()` `device_link.py:441-470` | 发 `speaker_flush` → 发 ping → 等 id 匹配的 pong（超时 0.8 s） | 屏障保证 flush 先于首帧；超时只告警、不阻塞回复 |
| `send_pcm()` `device_link.py:424-432` | `split_playback()` 后逐帧 `send`，持 `_send_lock` | 无出站队列；`AUDIO_QUEUE_CHUNKS=64` 只是**上行麦克风**队列 |
| `end_stream()` `device_link.py:434-439` | 发 `0x03` | 等设备 drain 后 `playback_stats` |

### 2.2 中间版本 1：idle no-op（已否决）

`c766fcd` 曾把 `stop_playback()` 在 idle 时改成直接 return。它确实消掉 A 类，但：

- 没有 flush → 设备的 `eosPending=false`，新流必须等 **24 period = 1.024 s**（或 EOS）才开声；
- post-fix 设备日志实测 `primeWait=762–968 ms`；短回复甚至等到 EOS，首声约 1.2 s（EOS 0.7）到 1.7 s（EOS 1.2）；
- 结论：**首声代价比 406 ms 空档更大**。`7a22a4b` 恢复 flush，改用屏障。

### 2.3 中间版本 2：prebuffer 单位 bug（已修）

`c766fcd` 的阈值是 `len(_frame_buffer) >= self._prebuffer * 2`：`_prebuffer=450`（ms）被当成字节，阈值只有 **900 B**；第一块 100 ms 就是 **9600 B**，所以第一块就 `_stream_started=True`，0.45 s prebuffer 从未生效。`7a22a4b` 改成：

```python
self._prebuffer_ms = int(env or 450)
self._prebuffer_bytes = int(SPEAKER_RATE * self._prebuffer_ms / 1000) * 2  # 43200 B
...
if len(self._frame_buffer) >= self._prebuffer_bytes:
```

这是本次最值得写进回归测试的一行：**阈值必须按字节算，且 9600 B 不能放行**。

### 2.4 设备固件事实（只读源码）

源码：`/tmp/echomuse-inspect/device`（EchoMuse v2.18.1 树）。

| 项 | 数值 | 位置/说明 |
|---|---:|---|
| `0x02` → `PumpPeriod(data[1:])` | 4096 B mono = 42.667 ms | `internal/client/data.go:940-949` |
| `toStereo()` | 4096 B mono → 8192 B stereo | `internal/bindings/speaker/pcm_speaker.go:722-725` |
| 深度队列 | `audioChanDepth=128` → **5.46 s** | `pcm_speaker.go:59`；`pump` 仅在队满时阻塞（`stream.go:128-161`） |
| prime 门 | `primePeriods=24` → **1.024 s**（或 EOS） | `pcm_speaker.go:68`；`stream.go:246-255` |
| ALSA 硬件环 | `4 × 2048` stereo frames = **170.7 ms** | `pcm_speaker.go:41-44` |
| 最后一个 period 余响 | `(8192+2048)/48000` = **213.3 ms** | `pcm_speaker.go:796-807` |
| 最大可排队 | 128×42.667 + 213 ≈ **5.67 s** | 设备可吃下 burst；反压只在 5.46 s 后出现 |
| 播放头 | **无逐块回报** | 只有 EOS 后 `playback_stats`；`lastTakeNs` 仅供设备内部 `VoiceAudible/VoiceSoundingUntil` |

`audioStream` 状态机（`stream.go`）：

- `pump()`：`discarding` 时丢弃；否则入 channel，记录到达间隔。
- `ready(prime)`：`n==0` false；`playing` true；否则 `n>=prime || eosPending`。
- `take()`：取一个 period，`playing=true`，`lastTakeNs=now`，`periods++`。
- `drained()`：`playing=false`；`eosPending.Swap(false)` 为 true → 自然结束；为 false → underrun（注入静音，不结束流）。
- `flush()`：drain 队列；若 `active` 则 `discarding=true`；**无条件 `eosPending=true`**。
- `endStream()`：`active=false`；`discarding=false`；若之前是 discarding，**不再**设 `eosPending`（避免下一次 drain 误判为结束）。

关键点：`eosPending` 同时承担“真实 EOS”和“flush 旁路 prime”两个语义。这是为什么当前方案**必须保留 flush**：删掉它虽然消掉 A 类，却会让 1.024 s prime 重新生效。长期正解是设备侧把两个语义拆成 `eosPending` + `startNow`，并把 `primePeriods` 做成可配。

### 2.5 `wait_consumed` 契约与我们的实现状态

`house-ear: src/house_ear/device/contract.py:171-187`：

```python
reports_consumption: bool = False
consumed: int = 0
sink_generation: int = 0

async def wait_consumed(self, since: int, timeout: float) -> int:
    """Newest consumed chunk index (> since), or `since` if none arrived in time."""
    return since
```

- `Credit` 只有在 `adapter.reports_consumption` 为 true 时才启用（`process.py:816-839`）；否则 `_credit_sync()` 直接 false，rui 完全按墙钟 pacing。
- 本仓库全局搜不到 `reports_consumption` / `consumed` / `sink_generation` / `wait_consumed`（`grep` 空），**我们没有实现**。
- `Credit` 类默认 `window=2`，音频面实际用 `Credit(window=cfg.audio.playback.credit_window)`（`process.py:254`），配置默认 **8**（`house-ear: src/house_ear/config/schema.py:242`）；8 块 = 800 ms 只是“最多领先 800 ms”，不是“保证领先”。
- 设备只有 EOS 后的 `playback_stats`，没有逐 period 的 `consumed`。要补必须改固件：在 `take()`/`silenceLoop` 每 N 个 period 通过 `/control` 发 `{type:"playback_progress", stream, played, mono}`，adapter 再映射到 `consumed`/`sink_generation`；还要处理 flush/重连/序号回绕。**成本高，且对空档无直接帮助。**

### 2.6 设备日志的实测事实

从 `adb -s G090LF0964340RN6 shell su -c 'cat /tmp/server.log'` 只读统计（窗口内 355 条 `voice stream complete`）：

- `maxGap` 在 **300–499 ms** 的流：**155 条，全部 `underruns=0`**；166–249 period 的长流（如 `periods=249`、`recvSpan≈10.66 s`、`maxGap≈401 ms`）`minDepth` 仍有 **13–23 period（≈555–980 ms）**。说明**只要 prime 正常、流不被误判结束，设备 5.46 s 的队列完全能吸收 400 ms 到达空档**。
- `underruns>0` 的 9 条流，`maxGap` 都在 **1.1–1.3 s**，即停顿超过旧 `EOS_IDLE_S=1.2` 后被拆流/重新 prime 的那一类；这与 `docs/findings.md:63-75` 一致。
- `primeWait` 双峰：94 条长流 `<100 ms`（prime 被旁路），26 条 750–999 ms、8 条 1.0–1.25 s（prime 正常）。这既证明 flush 旁路确实在发生，也证明一旦走正常 prime，首声要等约 1 s。
- 设备日志里 `voice stream complete (periods=4 … recvSpan=5ms)` 与 `speaker_flush received` 同秒出现，是 A 类 406 ms 的直接证据；此时 `periods=4` 就是被 flush 丢掉的前两块。
- **最终版（`7a22a4b`）实测**：三条回复 `primeWait=14–22 ms, underruns=0, in-stream maxGap=108–156 ms`；`calls.log` 不再有 400 ms 级块间空档。说明屏障 + 450 ms prebuffer 的组合把 A、B 两类都压住了。
- **仍有一个独立现象**：每条主流后面常跟一条 `periods=1` 的幽灵流（`primeWait` 3–8 s、`maxGap≈1202 ms`）。它只有 42.7 ms，不携带实质语音，也不影响本方案；但它会污染 `primeWait/recvSpan` 统计，属于流边界/EOS 的独立问题，建议另开 issue。

---

## 3. 方案对比

评分针对 **B 类句中空档**（A 类已由“flush + 屏障”解决）；延迟指“首声”增加。

| 方案 | 能否消除 400 ms | 额外延迟 | 对打断/停播 | 实现复杂度 | 主要风险 |
|---|---|---|---|---|---|
| **(a) 播放预缓冲**：攒 450 ms 再下发 | 能覆盖**不超过 450 ms** 的空档；超过仍会断 | +450 ms（短回复 0.35 s 兜底） | 停播清本地缓冲 + flush；设备尾部 ~213 ms 仍会响 | 低–中（已实现，~30 行） | 首声固定变慢；连续多次长空档会耗尽；单位/阈值必须按字节 |
| **(b) 实现 `wait_consumed`** | **不能** | 0 | 无直接帮助 | 高（固件 + 协议 + adapter 映射） | v2.18.1 无播放头；`Credit` 只限“在途”，窗口 8 反而可能减少 lead |
| **(c) 队列 + 42.7 ms 定速 + 空档补零** | **单独不能**（补零不产生语音）；但能防止“排空 → underrun → 1.024 s prime”把空档放大 | 单独 +0；加 lead 目标则 +目标 | 停播要 cancel task + 清队列 + flush；补零会延后后续音频 | 中（task、EOS、竞态、测试） | 补零掩盖 underrun 诊断；与 stop/EOS 竞态；调度抖动 |
| **(a)+(c) 组合** | 覆盖 ≤450 ms 的源空档；超过时退化为“干净的空档 + 不重新 prime” | +450 ms | 同 (a) | 中（~120–180 行 + 测试） | 目前实测不需要；先保留简单版 |
| **(d) idle no-op，恢复设备 prime** | 对 400 ms 有效，但… | **+0.76–0.97 s（实测）** | 不改变打断逻辑 | 最低（几行） | 首声严重回退；短回复等到 EOS；已否决 |

### 3.1 (a) 播放预缓冲（当前实现）

- **做法**：`play()` 只处理 PCM 并入队；队列 ≥450 ms（43200 B）时开始 `send_pcm`；短回复 0.35 s 兜底。
- **消除空档的条件**：`prebuffer 450 + 设备 ALSA 170.7 ≈ 621 ms ≥ 400 ms + 抖动余量`。当前实测 underruns=0，说明余量足够。
- **延迟**：每个回复首声 +450 ms；短回复受 0.35 s 兜底，最多 +350 ms。
- **打断/停播**：本地最多 450 ms 音频，`stop_playback` 直接丢弃；设备侧 flush 切断深度队列，ALSA 中最多 ~213 ms 已交给硬件的音频仍会响（与所有方案一样，无法更好）。
- **复杂度**：已实现；关键是阈值按字节（43200 B），防回归测试必须覆盖 9600 B 不放行。
- **风险**：TTS 平均速率低于实时时 lead 会被耗尽；连续多次 >450 ms 的断供仍会 underrun 并重新 prime。

### 3.2 (b) 实现 `wait_consumed`（不推荐）

**契约层面可行，设备层面不可行。**

- adapter 需提供 `reports_consumption=True`、`consumed`、`sink_generation`、`wait_consumed(since, timeout)`；`Credit` 才会启用。
- 设备只有流结束后的一条 `playback_stats`（`docs/device-controller-interface.md:191`），没有逐 period 的 `consumed`；`audioStream.take()` 里的 `lastTakeNs/periods` 没有发到 control。
- 要补需固件新增进度消息，并处理 flush/重连/序号；工作量大。
- **即使做了，也不解决空档**：`Credit` 是“不超过 window 块领先”，不是“至少领先 N 块”。当前 `credit_window=8`（800 ms）如果生效，反而可能把 lead 限制在 800 ms；对 400 ms 空档未必更差，但它不会主动增加缓冲。
- 结论：**不作为本次方案**；如果未来要补，和设备侧 `primePeriods` 可配置、`startNow` 标志一起做才有意义。

### 3.3 (c) 队列 + 42.7 ms 定速 + 空档补零（可选升级）

- **单独使用**：队列只做“到达即入、每 42.667 ms 出一帧”，没有 lead；空档时补零保持流。它能把“排空 → underrun → 1.024 s prime”变成“设备持续收到静音 → 流不重启”，但不能把 400 ms 源空档变成语音；听感仍是 400 ms 静音，只是不会变成 1.4 s。
- **补零副作用**：补零占时间线，后续真实音频后移；若下一块 5 ms 后就到，最多多补 42.7 ms。相对重新 prime 1 s，这个代价可接受。补零应计入 `underrun_frames`，不能假装没有空档。
- **与 (a) 组合**：lead 覆盖源空档；补零兜住“超过 lead”的极端情况，避免二次放大。
- **现状**：当前“攒够后 burst”已实测 `underruns=0`，没有证据表明需要定速/补零；先不增加复杂度，等出现 >450 ms 的断供再看。
- **复杂度**：一个 `asyncio.Task` + `deque` + `Event`；`stop_playback` 用 generation 取消旧 task；EOS 由 task 在“队列空 + 1.2 s 无新数据”时发；测试要覆盖 stop/EOS 竞态。

### 3.4 (d) idle no-op（已否决）

`c766fcd` 曾把 `stop_playback()` 在 idle 时改成 no-op。它确实消掉 A 类，但：

- 删掉 flush 后，设备不会收到 `eosPending=true`，新流要等 **24 period = 1.024 s**（或 EOS）才开声；
- rui 的 `Pacer` 只领先 250 ms，24 period 要等约 1 s 才凑齐；
- 短回复（音频 <1.024 s）永远凑不齐 24 period，只能等 adapter 在最后一块后 `EOS_IDLE_S` 发 EOS；按 0.7 s 算约 1.2 s 后，按 1.2 s 算约 1.7 s 后；
- 设备日志 `primeWait=762–968 ms` 是这一版的实测；`primeWait=2330ms / 1232ms` 的短流是更早的同类证据。

结论：**不能只删 flush。** 要么保留 flush 但加屏障（当前做法），要么改固件把 prime 旁路拆成独立标志。

---

## 4. 推荐方案

### 4.1 当前推荐：`7a22a4b` 的 barrier + 450 ms prebuffer

这是本次调研的结论，也是已经落地的实现：

1. **保留 `speaker_flush`**，它同时承担“丢掉上一条流”和“旁路 1.024 s prime”两个职责。
2. **`DeviceLink.flush()` 内置 ping/pong 屏障**：同一 `/control` 上的 pong 证明 flush 已被处理，data 上的首帧不可能抢跑。代价一个 RTT（5–50 ms），超时 0.8 s 只告警。
3. **450 ms prebuffer（43200 B）**：覆盖 400 ms 源空档；短回复 0.35 s 兜底开声。
4. **`EOS_IDLE_S=1.2`**：不要用 0.7；0.7 会把 0.7–1.2 s 的 TTS 停顿拆流并重新 prime。
5. **不改 `wait_consumed`**：没有设备播放头，且 `Credit` 不是增加 lead 的工具。

实测：`primeWait=14–22 ms, underruns=0, in-stream maxGap=108–156 ms`；`calls.log` 无 400 ms 块间空档；58 测试通过。首声成本约 +450 ms。

### 4.2 可选 P1：定速 + 补零（只有出现 >450 ms 断供才做）

如果现场出现“400 ms 以上、连续多次”的 TTS 断供，当前 burst 版仍可能排空；此时把发送层升级为：

- 固定 42.667 ms 一帧（绝对 deadline 累加，落后最多补 1–2 帧，不 burst）；
- 队列空时补一帧静音（计入 `underrun_frames`），保持设备流，避免 `playing=false` 后重新 prime；
- EOS 仍由 sender 在“队列空 + 1.2 s”发；`stop_playback` 用 generation 取消。

这不是当前必需项，先不要为了“更平滑”增加复杂度。

### 4.3 长期（设备侧，单独排期）

- 把 `primePeriods` 做成可配（或降到 8–12），减少短回复的首声等待；
- 把 `eosPending` 拆成 `eosPending`（真实 EOS）+ `startNow`（flush 旁路），在 `take()` 第一个 period 后清 `startNow`；
- 这样 adapter 不再需要靠 flush 来旁路 prime，屏障/prebuffer 都可以简化。

---

## 5. 最小实现草图（函数/状态机级）

### 5.1 已实现：`DeviceLink.flush()` 屏障

```python
async def flush(self) -> None:
    await self._send_control({"type": "speaker_flush"})
    await self._barrier()

async def _barrier(self, timeout: float = 0.8) -> bool:
    self._ping_seq += 1
    ping_id = self._ping_seq
    event = asyncio.Event()
    self._pong_events[ping_id] = event
    try:
        await self._send_control({"type": "ping", "id": ping_id})
        await asyncio.wait_for(event.wait(), timeout)
        return True
    except (TimeoutError, ConnectionClosed):
        log.warning("dot2 link: flush barrier timed out")
        return False
    finally:
        self._pong_events.pop(ping_id, None)
```

### 5.2 已实现：`Dot2Adapter` 的 prebuffer

```text
play(pcm):
    pcm = loudness.process(pcm)
    if not _playing: loudness.reset_stream()
    _frame_buffer.extend(pcm)
    if not _stream_started:
        if len(_frame_buffer) >= _prebuffer_bytes:   # 43200 B, not 900 B
            _stream_started = True
        else:
            start _start_after_prebuffer() if not running
            _playing = True; _schedule_eos(); return
    _drain_frames()
    _playing = True; _schedule_eos()

_start_after_prebuffer():
    await sleep(0.35)
    if _stream_started or not _frame_buffer: return
    _stream_started = True
    _drain_frames()

stop_playback():
    cancel _eos_task / _prebuffer_task
    clear _frame_buffer; _stream_started = False
    await link.flush()          # flush + ping/pong barrier
```

### 5.3 可选：`PlaybackSender`（P1 才需要）

状态：`IDLE → BUFFERING → PLAYING → DRAINING → IDLE`；`_gen` 每次 stop/新流递增，旧 task 看到 `gen` 不匹配就退出。

```text
sender(gen):
    while gen == _gen:
        if BUFFERING:
            if q.ms >= 450 or now - t0 >= 0.35: PLAYING; deadline = now
            else: await event.wait(small); continue
        if PLAYING:
            if q.empty():
                if now - last_enqueue >= 1.2:
                    send padded tail; link.end_stream(); IDLE; return
                if PAD_ON_EMPTY:
                    send zero frame; underrun_frames += 1
                    deadline += 42.667ms; sleep_until(deadline); continue
                await event.wait(small); continue
            frame = q.pop_frame()
            await link.send_pcm(frame)
            deadline += 42.667ms
            await sleep_until(deadline)
```

### 5.4 观测字段

- adapter：`play_lead_ms`、`play_start_delay_ms`、`send_gap_max_ms`、`underrun_frames`；
- 设备：`periods/underruns/minDepth/primeWait/recvSpan/maxGap`；
- 两边对得上才算验证。特别关注 `primeWait`：屏障生效时应 <100 ms；no-op 版本会 ~1 s。

---

## 6. 验证（最小实验）

### 6.1 单测（无设备）

用 `tests/test_adapter.py` 的 `FakeLink` 记录 `send_pcm` 时间：

1. **阈值单位**：`play(9600 B)` 后 `_stream_started is False`（450 ms 需要 43200 B）；喂满 43200 B 后才 `True`。
2. **首声延迟**：长回复 `play_start_delay_ms ≈ 450`；短回复 ≤350+50。
3. **空档覆盖**：`5×100 ms → 400 ms 空档 → 5×100 ms`；450 ms 配置下不应出现 `underrun_frames`。
4. **stop**：BUFFERING/PLAYING 两种状态调用 `stop_playback`，断言旧 task 退出、无后续帧、flush 内含屏障（事件顺序）。
5. **EOS**：最后一块后 1.2 s 发 EOS；尾部补零到 4096 B；队列还有音频时不提前 EOS。
6. **屏障**：超时用例只告警，不阻塞回复。

### 6.2 设备 A/B（静音下可做，只看统计）

设备当前故意静音（level 0/127），**不要改音量**；静音不影响 period 计数和 underrun 统计。

1. 生成 48 kHz mono WAV：3 段音调，发送侧分 3 次 `play()`，中间 sleep 400 ms。
2. 通过 `DOT2_INJECT_PORT=8871` 注入：`python -m rui_dot2_bridge.inject_cli --wav ...`。
3. 读设备日志：

```bash
export PATH=/opt/homebrew/share/android-commandlinetools/platform-tools:$PATH
adb -s G090LF0964340RN6 shell su -c 'cat /tmp/server.log' | grep 'voice stream complete' | tail -5
```

验收：
- `prebuffer=450`：`underruns=0`，`minDepth>0`，`primeWait<100 ms`；
- `prebuffer=0`：应出现与空档对应的 `minDepth=0`/`UNDERRUN`（或流被拆），作为对照；
- 若出现 `maxGap≈1200 ms` 且紧接着新流，说明 EOS/停顿拆流，需要检查 TTS 停顿分布。

4. 看 `speaker_flush received` 与首帧时间：屏障版不应再出现 flush 落在前两块之后。

### 6.3 人耳 A/B（需要出声窗口；本次不做）

- 10 次同一句长回复，barrier+450 ms 与 no-op 对照（如果要做历史对比），顺序打乱；
- 验收：句中卡顿 0 次；首声 P50 增加 ≤0.5 s；`underruns=0`；无 `primeWait≈1 s` 异常。

---

## 7. 风险与开放问题

1. **屏障超时**：本机/局域网 RTT 5–50 ms；control WS 拥塞或设备忙时 0.8 s 超时会拖慢首声。当前策略是超时只告警、继续，接受极小概率的旧竞态。
2. **prebuffer 单位回归**：必须锁死 43200 B；`9600 B` 不能放行。当前 bug 已经教训过一次。
3. **EOS 0.7 的拆流风险**：已回到 1.2；不要为了“尾巴短”再降。TTS 停顿 0.7–1.2 s 会被拆流并重新 prime。
4. **P1 不能创造音频**：连续多次 >450 ms 空档或 TTS 平均慢于实时时，lead 仍会耗尽；补零只能防二次 prime，不能消除源空档。
5. **短回复**：`PREBUFFER_START_S=0.35` 是延迟/保护的折中；如果短回复首声要求更严，可降到 0.25–0.3，但要重新测空档。
6. **EOS/stop 竞态**：当前 `_eos_task`、`_prebuffer_task`、`stop_playback` 三者要保证 generation/取消顺序；P1 若引入 sender task，必须只留一个 owner。
7. **设备侧长期修复**：`primePeriods` 可配 + `eosPending` 拆分是根治；否则 adapter 永远要维护“flush 旁路”的隐式契约。
8. **未验证**：本轮没有重启 rui、没有出声；`primeWait 14–22 ms` 来自 `7a22a4b` 之后的三条设备日志，最终听感仍需人耳窗口确认（设备目前静音）。
9. **active 流上的 flush 仍可能吞下一段回复（独立 correctness 风险）**：设备 `flush()` 在 `active` 时 arm `discarding=true`，只有 `endStream()` 能清；当前 `stop_playback()` 只发 flush、不发 EOS。如果上一路（cue/barge）还没结束，flush 后下一段 reply 的 PCM 会被丢弃，直到它自己的 EOS（约 1.2 s 后）。建议把 `stop_playback` 改成 `flush → end_stream → barrier`（flush 已置 `eosPending`，EOS 只清 discarding），并加“cue 刚响就追问”的回归。本次测量样本大多是 idle 流，未暴露该路径。

---

## 8. 附录：关键位置索引

| 内容 | 位置 |
|---|---|
| rui Pacer 常量 | `house-ear: src/house_ear/audio/process.py:95-97` |
| rui `Credit` | `house-ear: src/house_ear/audio/process.py:148-196` |
| rui `_speak_loop` / `stop_playback` 调用 | `house-ear: src/house_ear/audio/process.py:963-1146`，尤其 `1095-1108`、`1141-1146` |
| rui `Credit` 配置默认 8 | `house-ear: src/house_ear/config/schema.py:242` |
| 契约 `wait_consumed` | `house-ear: src/house_ear/device/contract.py:171-187` |
| 小爱参考实现 `wait_consumed` | `house-ear: src/house_ear/device/xiaoai/adapter.py:223-243` |
| adapter `play/_start_after_prebuffer/_drain/eos/stop` | `src/rui_dot2_bridge/adapter.py:348-471` |
| prebuffer 常量/阈值 | `src/rui_dot2_bridge/adapter.py:97-99, 160-161, 377` |
| `flush` + ping/pong 屏障 | `src/rui_dot2_bridge/device_link.py:441-470` |
| `send_pcm/end_stream` | `src/rui_dot2_bridge/device_link.py:424-439` |
| 4096 B = 42.667 ms | `src/rui_dot2_bridge/frames.py:29-33, 58-65` |
| 设备帧分发 `0x02/0x03` | `/tmp/echomuse-inspect/device/internal/client/data.go:940-949` |
| 设备缓冲常量 | `.../internal/bindings/speaker/pcm_speaker.go:25, 41-51, 59, 68, 796` |
| 设备流状态机 | `.../internal/bindings/speaker/stream.go:128-161, 184-215, 246-288, 295-322` |
| 设备 `playback_stats` 语义 | `/tmp/echomuse-inspect/docs/device-controller-interface.md:191` |
| 设备 `speaker_flush` | `.../docs/device-controller-interface.md:216`；`internal/client/control.go:992-998` |
| 小爱对照与 A 类证据 | `docs/stutter-rui-xiaoai.md` |

### 一句话

**A 类 406 ms 用“flush + 同一条 /control 上的 ping/pong 屏障”消掉（保留 prime 旁路，首声只多一个 RTT）；B 类用 450 ms（43200 B）prebuffer 覆盖 400 ms 源空档，EOS 保持 1.2 s。`wait_consumed` 在 v2.18.1 上没有播放头可报，且 `Credit` 不是增加 lead 的工具，不采用。**
