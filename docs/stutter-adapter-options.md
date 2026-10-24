# Dot 2 播放 400 ms 空档：adapter 侧兜底方案

- 日期：2026-10-24
- 范围：只读调研。未改代码/配置，未重启 rui，未碰设备音量/静音（设备当前 `0/127` 保持静音）。
- 相关：rui issue #1；同目录 `docs/stutter-rui-xiaoai.md`（小爱对照与 406 ms flush 竞态）。
- 结论先行：**不要简单删掉每轮开头的 `stop_playback()` flush**。它确实制造了 control/data 双 WS 的竞态，但它同时是 Dot 固件 1.024 s prime 门的“旁路”。正确的最小修复是 **flush + 控制面 ping/pong 屏障**；在此之上，再用 **可配置的 250–300 ms 发送侧 lead 队列 + 42.7 ms 定速 + 空档补零** 作为句中 TTS 断供的兜底。`wait_consumed` 在 v2.18.1 上没有设备播放头可报，不能作为本次方案。

---

## 0. 结论摘要

1. **本次观察到的 406 ms 有两个层次**：
   - **每轮回答开头**的 406 ms：`stop_playback()` 把 `speaker_flush` 发到 `/control`，PCM 发到 `/data`，两条 TCP 连接没有顺序保证；设备先收了前 2 个 100 ms 块，再处理 flush，把它们丢掉并结束流。同目录 `stutter-rui-xiaoai.md` 已用 11/11 的 `calls.log` + 设备 `periods=4 recvSpan=5ms` 对齐证实。
   - **句中 TTS 到达空档**：rui 的 `Pacer` 稳态只保 250 ms（上限 320 ms），Dot 没有 adapter 侧队列；一旦设备队列在空档处排空，固件可能把这次“排空”当成流自然结束，下一段重新 prime **1.024 s**，把 400 ms 放大成约 1.0–1.4 s 的停顿。
2. **设备侧真实缓冲比“约 300 ms”大得多**：`0x02` 是 4096 B = **42.667 ms**；固件深度队列 **128 个 period ≈ 5.46 s**；ALSA 硬件环 **4×2048 stereo frames = 170.7 ms**；最后一个 period 被取走后还会响约 **213.3 ms**；prime 门是 **24 个 period = 1.024 s**（或 EOS）。所以瓶颈不在设备容量，而在“rui 只领先 250 ms”和“prime 门是否被绕过/是否被误关”。
3. **设备没有逐块播放头回报**：只有流排空后一条 `playback_stats`（`periods/underruns/minDepth/primeWaitMs/recvSpanMs/maxGapMs/bytesRecv`），没有 `consumed`/进度事件。`audioStream.lastTakeNs` 只在设备内部给 AEC 用。**要补 `wait_consumed` 必须改固件 + 协议**，而且 `Credit` 只是“在途上限”，不增加 lead，无法填 400 ms 空档。
4. **方案排序**：
   - **P0（必做，~0.5 天）**：`stop_playback()` 改成“flush → 控制面 ping/pong 屏障 → 再放首帧”；若上一路仍在播，顺序为 `flush → EOS → 屏障`（`flush` 已把 `eosPending` 置 true，EOS 只负责清 `discarding`，不需要第三次 flush）。消掉 406 ms，额外延迟约一个 RTT（5–50 ms），且保留 prime 旁路，不会把首声推迟 1 s。
   - **P1（兜底，~1–1.5 天，配置开关）**：adapter 内 lead 队列，**目标 250–300 ms**（覆盖 400 ms 空档减去设备 ALSA 的 170 ms 后仍有余量），独立 task 按 42.667 ms 定速发送；队列空时补零保持流，避免 `eosPending` 被误消费导致重新 prime。首次出声约 **+250–300 ms**（保守可设 400 ms，则 +400 ms）。
   - **P2（不推荐）**：`wait_consumed` / `reports_consumption`。当前固件没有可用的逐块播放头；即使补上，`Credit` 的语义是限制“领先多少”，不是保证“领先够多”，对空档无帮助。
5. **不建议**：无脑把 `stop_playback()` 改成 idle no-op。它消掉 406 ms，但会让每轮重新走 1.024 s prime 门；短回复（<1.024 s 音频）甚至要等到 EOS（约 1.7 s）才出声。这个代价比 406 ms 更大。
6. **验证**：先用单测 + 设备 `DOT2_INJECT_PORT` 注入固定 PCM 做 A/B（设备静音，只看 `underruns/minDepth/primeWait/recvSpan`），再在允许出声的窗口做人耳 A/B。验收：句中无 400 ms 空档、设备 `underruns=0`、首声延迟增加 ≤350 ms（P1 默认 300 ms）。

---

## 1. 先把两类 400 ms 分开

### 1.1 A 类：每轮开头的 flush 竞态（已定位，P0 解决）

rui 在每轮回答第一块前调用 `adapter.stop_playback()`（`house-ear: src/house_ear/audio/process.py:1095-1108`）。我们的实现：

```python
# bridge: src/rui_dot2_bridge/adapter.py:414-423
async def stop_playback(self, fade_ms: int = 120) -> None:
    ...
    self._playing = False
    self._frame_buffer.clear()
    if self._link is not None:
        await self._link.flush()          # -> {"type":"speaker_flush"} on /control
```

`flush` 走 `/control`（`device_link.py:437-439`），PCM 走 `/data`（`device_link.py:420-428`）。两条 WebSocket 是两条 TCP 连接，**没有跨连接顺序**。设备可以先把 data 上的前两个 100 ms 块 `PumpPeriod` 掉，再读到 control 上的 flush：

- 设备日志：`[control] speaker_flush received` + `voice stream complete (periods=4 ... recvSpan=5ms)`；
- 4 个 period ≈ 171 ms，正好是 `calls.log` 里那两块 6 ms 连发的音频；
- 随后设备进入 discard/重新开流，`calls.log` 在约 406 ms 后才继续 `play()`。

同目录 `stutter-rui-xiaoai.md` 已给出 11/11 的证据和复现命令，这里不重复。

### 1.2 B 类：句中 TTS 到达空档（本方案要兜底的对象）

rui 的 `Pacer`（`house-ear: src/house_ear/audio/process.py:95-97, 116-145`）：

```python
PREBUFFER_S = 0.25
CATCHUP = 1.0
MAX_LEAD_S = 0.32
```

注意：**稳态目标是 250 ms，不是 320 ms**；320 ms 只是 `target` 的上限。`CATCHUP=1.0` 意味着当前版本不会随块增长 lead。实测 rui 日志里 `lead max` 约 343–344 ms，是突发瞬间的峰值。

我们的 adapter 在 `play()` 里同步转发，没有任何按时间的 lead：

```python
# adapter.py:336-366
self._frame_buffer.extend(pcm)
await self._drain_frames()      # 只按 4096 B 切，不按时间发
...
# adapter.py:380-385
while len(self._frame_buffer) >= SPEAKER_CHUNK_BYTES:
    await self._link.send_pcm(...)
```

`send_pcm()` 也不排队，逐帧 `await conn.send()`（`device_link.py:420-428`）。因此 rui 的到达节奏 = 设备收到的节奏；`Pacer` 的 250 ms 就是“adapter 之外”的全部余量。

如果某次 TTS 在句中停 400 ms，设备队列会先被消耗；排空后固件的行为取决于 `eosPending` 标志（见 §2.3）。在当前“每轮开头 flush”的路径下，`eosPending` 通常是 **true**：排空会被 `drained()` 当成“自然结束”，`playing` 置 false，下一块音频必须重新满足 24 period（1.024 s）或 EOS 才能播。也就是说：

> **400 ms 的源空档，可能被固件的“误判自然结束 + 重新 prime”放大成约 1.0–1.4 s 的停顿。**

这解释了为什么长回复里同样的 400 ms 到达空档可以 `underruns=0`（队列里还有 15–20 个 period），而短回复/刚开流时同一空档会明显“卡”。

### 1.3 对“320 ms 不够”说法的修正

- rui 实际稳态 lead ≈ 250 ms；
- 设备 ALSA 环 ≈ 170.7 ms；
- 合计约 **421 ms**，理论上刚好覆盖 400 ms，几乎没有余量（调度抖动、USB/WiFi、period 对齐都会吃掉几毫秒到几十毫秒）；
- 如果 prime 被绕过（首声很快），这 421 ms 就是全部；如果没有被绕过，prime 会额外给约 1 s，但短回复会等 EOS。
- 所以“400 ms 空档”既可表现为短回复的 200–400 ms 空洞，也可表现为“误判结束 + prime”的 1 s 级停顿；两者要用不同层面的手段：**屏障/状态机** 解决前者，**lead + 补零** 解决后者。

---

## 2. 现状核对：adapter、契约、设备

### 2.1 adapter 播放路径（逐函数）

| 函数 | 行为 | 关键点 |
|---|---|---|
| `Dot2Adapter.play()` `adapter.py:336-366` | 校验 48 kHz mono；首块 `reset_stream()`；响度处理；追加 `_frame_buffer`；`await _drain_frames()`；置 `_playing=True`；重置 EOS idle | 同步、无队列、无时间节奏 |
| `_drain_frames()` `adapter.py:380-385` | 每凑满 4096 B 就 `send_pcm()`，循环到不足一帧 | 一次 `play()`（100 ms = 9600 B）会发 2 个整帧 + 留 1408 B；下一块补齐 |
| `_eos_after_idle()` `adapter.py:387-412` | 1.2 s 无新块：把不足一帧的尾巴 10 ms 淡出、补零到 4096 B，发 `0x03` | 1.2 s 是经验值；设备约 213 ms 的尾部还在响，不会切掉 |
| `stop_playback()` `adapter.py:414-423` | 取消 EOS、清本地 buffer、`link.flush()` | **`fade_ms` 参数未使用**；不发 EOS；不等待设备确认 |
| `send_pcm()` `device_link.py:420-428` | `split_playback()` 后逐帧 `send`，持 `_send_lock` | 无出站队列；`AUDIO_QUEUE_CHUNKS=64` 只是**上行麦克风**队列 |
| `end_stream()` / `flush()` `device_link.py:430-439` | 发 `0x03` / `speaker_flush`（control） | 都不等设备 ack |

`frames.py` 的线上事实：`SPEAKER_CHUNK_BYTES=4096`，48 kHz mono S16 = **42.667 ms/帧**（`frames.py:29-33, 58-65`）。4096 B = 2048 mono samples；设备 `toStereo()` 后是 8192 B = 2048 stereo frames，正好一个 ALSA period。

### 2.2 `wait_consumed` 契约与我们的实现状态

`house-ear: src/house_ear/device/contract.py:171-187`：

```python
reports_consumption: bool = False
consumed: int = 0
sink_generation: int = 0

async def wait_consumed(self, since: int, timeout: float) -> int:
    """Newest consumed chunk index (> since), or `since` if none arrived in time."""
    return since
```

`Credit`（`process.py:148-196`）只有在 `adapter.reports_consumption` 为 true 时才启用；否则 `_credit_sync()` 直接返回 false，`_speak_loop` 完全按墙钟 pacing（`process.py:816-839`）。

本仓库全局搜不到 `reports_consumption` / `consumed` / `sink_generation` / `wait_consumed`（`grep` 空结果），所以：

- **我们没有实现 `wait_consumed`**；
- rui 对 Dot 走的就是“pacing by the clock”，连 `Credit.wait()` 的 1.5 s 超时分支都不会进入（因为 `_credit_sync` 先返回 false）；
- `Credit` 的 `window` 类默认是 2，但音频面实例化用的是 `Credit(window=cfg.audio.playback.credit_window)`（`process.py:254`），配置默认 **8**（`house-ear: src/house_ear/config/schema.py:242`），不是 2。即使实现，8 块 = 800 ms 也只是“最多领先 800 ms”，不是“保证领先 800 ms”。

### 2.3 设备固件：缓冲、prime、播放头

源码：`/tmp/echomuse-inspect/device`（EchoMuse v2.18.1 树，只读）。

| 项 | 数值 | 位置/说明 |
|---|---:|---|
| `0x02` 到达 → `PumpPeriod(data[1:])` | 4096 B mono = 42.667 ms | `internal/client/data.go:940-949` |
| `toStereo()` | 4096 B mono → 8192 B stereo | `internal/bindings/speaker/pcm_speaker.go:722-725` |
| 深度队列 | `audioChanDepth=128` → **5.46 s** | `pcm_speaker.go:59`；`pump` 仅在队满时阻塞（`stream.go:128-161`） |
| prime 门 | `primePeriods=24` → **1.024 s**（或 EOS） | `pcm_speaker.go:68`；`stream.go:246-255` |
| ALSA 硬件环 | `4 × 2048` stereo frames = **170.7 ms** | `pcm_speaker.go:41-44` |
| 最后一个 period 的余响 | `(8192+2048)/48000` = **213.3 ms** | `pcm_speaker.go:796-807` |
| 最大可排队 | 128×42.667 + 213 ≈ **5.67 s** | 设备可以吃下 burst；反压只会在 5.46 s 后出现 |
| 播放头 | **无逐块回报** | 只有 EOS 后 `playback_stats`；`lastTakeNs` 仅供设备内部 `VoiceAudible/VoiceSoundingUntil` |

`audioStream` 的关键状态机（`stream.go`）：

- `pump()`：`discarding` 时丢弃；否则入 channel，记录到达间隔。
- `ready(prime)`：`n==0` false；`playing` true；否则 `n>=prime || eosPending`。
- `take()`：取一个 period，`playing=true`，`lastTakeNs=now`，`periods++`。
- `drained()`：`playing=false`；`eosPending.Swap(false)` 为 true → 自然结束（返回 stats）；为 false → underrun（注入静音，不结束流）。
- `flush()`：drain 队列；若 `active` 则 `discarding=true`（丢弃后续直到 EOS）；**无条件 `eosPending=true`**。
- `endStream()`：`active=false`；`discarding=false`；如果之前是 discarding，**不再**设 `eosPending`（避免把下一次 drain 误判为结束）。

这里有一个**关键的过载**：`eosPending` 同时承担“真实 EOS 已到”和“flush 要求绕过 prime”两个语义。由此产生两个后果：

1. 如果 flush 在 idle 时执行，`eosPending=true` 会一直留到下一个流；下一个流的 prime 门被绕过（首声快），但第一次队列排空会被 `drained()` 当成自然结束，`playing=false`，后续音频要重新等 24 period（1.024 s）。
2. 如果为了省延迟而删掉 flush（P0 no-op），`eosPending=false`，下一个流必须等 24 period 或 EOS；短回复会等 EOS（约 1.2 s 后），首声反而更晚。

所以**不能只改一边**：要么让 flush 有序（屏障），要么改固件把“prime 旁路”从 `eosPending` 里拆出来。后者是设备侧的正解（新增 `startNow` 标志，在 `take()` 第一个 period 后清除；`eosPending` 只表示真实 EOS），但不在本次“adapter 只写报告”的范围内，列为长期建议。

### 2.4 设备日志的实测事实

从 `adb -s G090LF0964340RN6 shell su -c 'cat /tmp/server.log'` 只读统计（窗口内 355 条 `voice stream complete`）：

- `maxGap` 在 **300–499 ms** 的流：**155 条，全部 `underruns=0`**；其中 166–249 period 的长流（如 `periods=249`、`recvSpan≈10.66 s`、`maxGap≈401 ms`）`minDepth` 仍有 **13–23 个 period（≈555–980 ms）**，那条 `periods=249` 的流是 `minDepth=16`。说明**只要 prime 正常、流不被误判结束，设备 5.46 s 的队列完全能吸收 400 ms 到达空档**，不需要 we 再加 400 ms。
- `underruns>0` 的 9 条流，`maxGap` 都在 **1.1–1.3 s**，即 TTS 停顿超过 `EOS_IDLE_S=1.2` 后被拆流/重新 prime 的那一类；这与 `docs/findings.md:63-75` 的结论一致。
- `primeWait` 呈双峰：94 条长流 `<100 ms`（prime 被绕过），26 条 750–999 ms、8 条 1.0–1.25 s（prime 正常）。这既证明 flush 旁路确实在发生，也证明一旦走正常 prime，首声要等约 1 s。
- 设备日志里 `voice stream complete (periods=4 … recvSpan=5ms)` 与 `speaker_flush received` 同秒出现，是 A 类 406 ms 的直接证据；此时 `periods=4` 就是被 flush 丢掉的前两块。

> 因此：**A 类（开头竞态）用 P0 屏障解决；B 类（句中 TTS 空档）在 prime 正常时本来就被设备队列吸收，真正危险的是“flush 旁路后队列排空被误判为结束 → 重新 prime”。P1 的 lead + 补零就是防止这个误判，同时在 TTS 真的断供时提供余量。**

---

## 3. 方案对比

评分针对 **B 类句中空档**（A 类已由 P0 解决）；延迟指“首声”增加。

| 方案 | 能否消除 400 ms | 额外延迟 | 对打断/停播 | 实现复杂度 | 主要风险 |
|---|---|---|---|---|---|
| **(a) 播放预缓冲**：首块后攒 400–600 ms 再下发 | 能覆盖**不超过目标**的空档；超过则仍会断 | +400–600 ms（每个回复） | `stop_playback` 丢弃本地缓冲即可；设备 flush 仍切断设备队列 | 低–中（状态 + 队列；若同步阻塞 rui 则高） | 短回复被拖慢；连续多次长空档会耗尽；不解决 `eosPending` 误判 |
| **(b) 实现 `wait_consumed`** | **不能** | 0 | 无直接帮助 | 高（固件 + 协议 + adapter 映射） | v2.18.1 没有播放头；`Credit` 只限“在途”，窗口 8 反而可能减少 lead；控制面进度还会被 flush/数据竞争影响 |
| **(c) 自建队列 + 42.7 ms 定速（空档补零/保持流）** | **单独不能**（补零不产生语音）；但能防止“排空被误判结束 → 1 s prime”，把放大后的 1 s 停顿压回源空档 | 单独 +0；若加 lead 目标则 +目标 | 停播要 cancel task + 清队列 + flush；补零会延后后续音频但保持时间线 | 中（task、EOS、竞态、测试） | 补零可能掩盖 underrun 诊断；发送 task 与 stop/EOS 竞态；调度抖动 |
| **(a)+(c) 组合** | 能覆盖 ≤ 目标的源空档；超过目标时退化为“干净的空档 + 不重新 prime” | +250–300 ms（推荐）；保守 400 ms | 停播清队列 + flush；设备尾部 ~213 ms 仍会响（与现在同） | 中（~120–180 行 + 测试） | 同 (a)/(c)；需灰度开关 |
| **(d) 删掉/空闲 no-op flush，恢复设备 prime** | 对 400 ms 空档有效，但… | **+0.7–1.2 s；短回复等到 EOS ≈ +1.7 s** | 不改变打断逻辑 | 最低（几行） | 首声严重回退；短回复不可接受；不建议单独使用 |

### 3.1 (a) 播放预缓冲

做法：`play()` 只把处理后的 PCM 追加到队列；当队列时长 ≥ `LEAD_MS`（或首块后超过 `MAX_WAIT`）时，开始把队列按 42.667 ms 发给设备。

- **消除空档的条件**：`LEAD_MS + 设备 ALSA 170.7 ms ≥ 空档长度 + 抖动余量`。按 400 ms 空档，`LEAD_MS≈250 ms` 即可（总余量 ~421 ms）；保守 400 ms 则总余量 ~571 ms。
- **代价**：首声 +LEAD_MS（250–400 ms）。对短回复，`MAX_WAIT` 限制等待，避免为了凑目标而无限等。
- **打断/停播**：本地队列最多 LEAD_MS 的音频，`stop_playback` 可以瞬间丢弃；设备侧 `speaker_flush` 仍会切断深度队列，ALSA 里最多 ~213 ms 已经交给硬件的音频还会响（与现在一致，不可能更好）。
- **复杂度**：如果只做“攒够后一次 burst”，约 30–50 行；但 burst 会把设备队列瞬间推到 LEAD_MS，且后续仍按到达转发，lead 会随空档消耗而不再恢复。要稳定维持 lead，需要 (c) 的定速发送。
- **风险**：TTS 平均速率低于实时时，任何 lead 都会被耗尽；短回复延迟；与 EOS 的先后（不能在队列还有音频时发 0x03，虽然设备会播完，但统计/duck 会提前结束）。

### 3.2 (b) 实现 `wait_consumed`

**契约层面可行，设备层面不可行。**

- adapter 需要提供 `reports_consumption=True`、`consumed`、`sink_generation`、`wait_consumed(since, timeout)`；`Credit` 才会启用。
- 设备只有流结束后的一条 `playback_stats`（`docs/device-controller-interface.md:191`），没有逐 period 的 `consumed`。`audioStream.take()` 里的 `lastTakeNs/periods` 是设备内部变量，没有发到 control。
- 若要做，需要固件新增：在 `take()`/`silenceLoop` 每个 period（或每 N 个）通过 `/control` 发 `{type:"playback_progress", stream, played, mono}`；`stream` 用于 `sink_generation`，`played` 映射到 `consumed`。还要处理 flush/重连/序号回绕。
- **即使做了，也不解决空档**：`Credit` 是“不超过 window 块领先”，不是“至少领先 N 块”。当前 `credit_window=8`（800 ms）如果生效，反而会把 lead 限制在 800 ms；对 400 ms 空档未必更差，但它不会主动增加缓冲。它适合解决“写入阻塞/管道过满”，不是“源断供”。
- 结论：**不作为本次方案**；如果未来要补，和设备侧 `primePeriods` 可配置、`startNow` 标志一起做，才有意义。

### 3.3 (c) 队列 + 42.7 ms 定速 + 空档补零

- **单独使用**：队列只做“到达即入、每 42.667 ms 出一帧”，没有任何 lead；空档时补零保持流。它能把“设备排空 → 误判结束 → 1.024 s prime”变成“设备持续收到静音 → 流不重启”，但不能把 400 ms 的源空档变成语音。听感仍然是 400 ms 静音，只是不会变成 1.4 s。
- **补零的副作用**：补零会占掉时间线，后续真实音频相应后移；如果下一块在 5 ms 后就到，最多多补 42.7 ms。相对“重新 prime 1 s”，这个代价可接受。补零应计入 `underrun_frames`，不能假装没有空档。
- **与 (a) 组合**：lead 覆盖源空档；补零兜住“超过 lead”的极端情况，避免二次放大。这是推荐形态。
- **复杂度**：一个 `asyncio.Task` + `deque` + `Event`；`stop_playback` 用 generation 取消旧 task；EOS 由 task 在“队列空 + 1.2 s 无新数据”时发；测试要覆盖 stop/EOS 竞态。

### 3.4 (d) 为什么“删掉 flush”不是答案

这是工作区里已经存在的一个未提交改动（`stop_playback` 在 `_playing=False` 且 `_frame_buffer` 空时直接 return）。它确实能消掉 A 类 406 ms，但：

- 删掉 flush 后，设备不会收到 `eosPending=true`，新流要等 **24 period = 1.024 s**（或 EOS）才开声；
- rui 的 `Pacer` 只领先 250 ms，24 period 要等约 1 s 才凑齐；
- 短回复（音频 <1.024 s）永远凑不齐 24 period，只能等 adapter 在最后一块后 1.2 s 发 EOS，首声约 **1.7 s** 后；
- 设备日志里 `primeWait=2330ms / 1232ms` 的短流就是这类等待的现成证据。

因此：**要么保留 flush 但让它有序（P0 屏障），要么改固件把 prime 旁路拆成独立标志；不能只删 flush。** 如果实测表明该设备的 prime 实际远小于 1.024 s（例如旧固件/不同配置），no-op 仍可作为最简方案；但按本仓库 v2.18.1 源码，代价如上一段所述。

---

## 4. 推荐方案

### 4.1 P0：flush + 控制面屏障（先做，消掉 A 类）

目标：保持 `eosPending` 旁路（首声快），同时保证 flush 先于首帧 PCM 被设备处理。

`DeviceLink` 增加：

```python
async def control_barrier(self, timeout: float = 0.30) -> bool:
    """Send an app-level ping and wait for its pong on /control.

    The control WS is ordered, so a pong proves every preceding control
    message (speaker_flush / EOS) has been handled. PCM on /data cannot
    overtake it.
    """
```

实现要点：复用现有 `_ping_seq/_pong_waiters`，把 `_on_pong` 从“只记 RTT”扩展为“唤醒对应 Future/Event”；用唯一 id；超时返回 False 并 `log.warning`（老固件/control 拥塞时退化为原来的竞态，但不阻塞回复）。

`stop_playback()` 的状态机：

```text
if 空闲（not _playing and not _frame_buffer）:
    link.flush()                 # 故意 arm eosPending，旁路 1.024 s prime
    await link.control_barrier() # 保证 flush 先处理
else:
    link.flush()                 # 丢掉设备深度队列，并 arm discarding/eosPending
    link.end_stream()            # 清 discarding（EOS 是 disarm 的唯一方式）
    await link.control_barrier() # EOS 之后 eosPending 仍为 true，prime 旁路保留
清本地 _frame_buffer / 取消 _eos_task / _playing=False
```

顺序理由：设备 `flush()` 在 `active` 时会 arm `discarding`，同时无条件把 `eosPending` 置 true；`endStream()` 清掉 `discarding`，但因为它看到 `wasDiscarding=true`，不会再改 `eosPending`，所以 **EOS 之后 `eosPending` 仍为 true**，不需要第三次 flush。空闲时没有 active 流，一次 flush 就够。

代价：每次回复多一个 control RTT（本机/局域网约 5–50 ms）。收益：A 类 406 ms 消失；保留首声速度；不引入 1 s prime 延迟。

> 如果不想加屏障，退而求其次是在 flush 后 `await asyncio.sleep(0.05-0.1)` 再放首帧；这没有协议保证，只是经验值，不如 ping/pong。

### 4.2 P1：lead 队列 + 42.667 ms 定速 + 补零（配置开关，兜底 B 类）

参数建议：

| 参数 | 建议值 | 理由 |
|---|---:|---|
| `DOT2_PLAYBACK_LEAD_MS` | **300**（默认）；可设 250/400 | 300 + 设备 ALSA 170.7 ≈ 471 ms；覆盖 400 ms 空档有 ~71 ms 余量。250 只剩 ~21 ms，偏紧；400 是保守值，+400 ms 延迟 |
| `LEAD_MAX_WAIT_MS` | 400–500 | 短回复不能为凑目标无限等 |
| `FRAME_MS` | **42.667** | 与设备 period 对齐；用绝对 deadline 累加，避免 drift |
| `EOS_IDLE_S` | 保持 1.2 | 设备尾部 ~213 ms，1.2 s 足够；改大只会拖慢音乐恢复 |
| `PAD_ON_EMPTY` | **开** | 防止 `eosPending` 在排空时被误消费，导致重新 prime；计入 `underrun_frames` |

为什么目标不是 400–600 ms：设备 ALSA 环的 170.7 ms 也是“已交给硬件、尚未播出”的缓冲，属于同一笔余量。我们只需要补足 `400 - 170 ≈ 230 ms`，再加 50–100 ms 抖动余量 → 300 ms。若现场测量发现 ALSA 实际更浅（或设备换固件），再把默认调到 400。

首次出声延迟：lead 300 ms + 一个 period 42.7 ms ≈ **+343 ms**（不把 ALSA 环算作首声延迟；它主要增加稳态余量）。保守 400 ms 则 +443 ms。

### 4.3 不推荐 / 后续

- **不推荐 (b)**：没有设备播放头；`Credit` 不是“增加缓冲”的工具。
- **(d) 只作为临时实验**，不能作为最终修复；若要用，必须同时把设备 `primePeriods` 降到 8–12 或新增 `startNow` 标志。
- **长期（设备侧，单独排期）**：把 `primePeriods` 做成可配（或 8–12），并把 `eosPending` 拆成 `eosPending` + `startNow`；这样 adapter 不再需要靠 flush 来旁路 prime，P0/P1 都可以简化。

---

## 5. 最小实现草图（函数/状态机级，不写完整代码）

### 5.1 P0：`DeviceLink.control_barrier`

```python
# device_link.py
self._pong_events: dict[int, asyncio.Future[None]] = {}

async def control_barrier(self, timeout: float = 0.30) -> bool:
    self._ping_seq += 1
    ping_id = self._ping_seq
    fut = loop.create_future()
    self._pong_events[ping_id] = fut
    try:
        await self._send_control({"type": "ping", "id": ping_id})
        await asyncio.wait_for(fut, timeout)
        return True
    except (TimeoutError, ConnectionClosed):
        return False
    finally:
        self._pong_events.pop(ping_id, None)

def _on_pong(self, message):
    ...
    if fut := self._pong_events.get(ping_id):
        if not fut.done():
            fut.set_result(None)
```

### 5.2 P0：`Dot2Adapter.stop_playback`

```python
async def stop_playback(self, fade_ms: int = 120) -> None:
    cancel_eos_task()
    if self._link is None:
        self._playing = False
        self._frame_buffer.clear()
        return
    if self._playing or self._frame_buffer:
        await self._link.flush()
        await self._link.end_stream()
    else:
        await self._link.flush()
    await self._link.control_barrier()
    self._playing = False
    self._frame_buffer.clear()
    self._flush_dump()
```

注意：`flush()` 已经把 `eosPending` 置 true；`end_stream()` 只负责在 `active` 时清掉 `discarding`，不会再改 `eosPending`。所以 `flush → EOS → 屏障` 就够了，不需要第二次 flush。若未来固件把 `eosPending` 语义拆开，这一段可以简化。

### 5.3 P1：`PlaybackSender` 状态机

状态：`IDLE → BUFFERING → PLAYING → DRAINING → IDLE`；`_gen` 每次 stop/新流递增，旧 task 看到 `gen` 不匹配就退出。

```text
play(pcm):
    pcm = loudness.process(pcm)          # 保持现有处理位置
    if state == IDLE: gen += 1; q.clear(); t0 = now(); state = BUFFERING; start task
    q.append(pcm); last_enqueue = now; event.set()

sender(gen):
    while gen == self._gen:
        if state == BUFFERING:
            if q.ms >= LEAD_MS or now - t0 >= MAX_WAIT:
                state = PLAYING; deadline = now
            else:
                await event.wait(timeout=small); continue
        if state == PLAYING:
            if q.empty():
                if now - last_enqueue >= EOS_IDLE:
                    send padded tail; link.end_stream(); state = IDLE; return
                send one zero frame            # 保持流，防 eosPending 被误消费
                underrun_frames += 1
                deadline += FRAME
                await sleep_until(deadline); continue
            frame = q.pop_frame()              # 4096 B
            await link.send_pcm(frame)
            deadline += FRAME
            await sleep_until(deadline)        # 落后时最多补 1–2 帧，不 burst
```

`stop_playback`：`gen += 1`；取消 task；清 q；按 §5.2 发 flush/EOS/屏障。

`EOS`：由 sender 在“队列空 + 距最后入队 ≥1.2 s”时发；不要把 EOS 放在 `play()` 的 idle 计时器里，避免和 sender 竞态。尾部不足 4096 B 时沿用现有 `fade_out_pcm` + 补零。

### 5.4 观测字段

在 `health()` / 日志增加：

- `play_lead_ms`：入队总时长 − 已发送时长；
- `play_start_delay_ms`：首块入队 → 首帧发送；
- `send_gap_max_ms`：PLAYING 期间相邻 `send_pcm` 的最大间隔（空档诊断）；
- `underrun_frames`：补零帧数；
- `streams` / `max_lead_ms`。

设备侧同时保留 `periods/underruns/minDepth/primeWait/recvSpan/maxGap`，两边对得上才算验证。

---

## 6. 如何验证（最小实验）

### 6.1 单测（无设备，最先做）

用 `tests/test_adapter.py` 的 `FakeLink` 扩展一个记录 `time.monotonic()` 的 `send_pcm`：

1. **定速**：喂 10 块 100 ms 音频（9400/9600 B），断言相邻 `send_pcm` 的时间差 `42.667 ± 10 ms`；不能一次 burst。
2. **空档覆盖**：喂 `5×100 ms → 400 ms 空档 → 5×100 ms`；`LEAD_MS=300` 时断言空档期间没有 `underrun_frames`；`LEAD_MS=0` 时断言至少出现一次补零，且不会触发 `end_stream`（模拟 `eosPending` 误判）。
3. **首声延迟**：断言 `play_start_delay_ms ≤ LEAD_MAX_WAIT + 50`。
4. **stop**：在 BUFFERING/PLAYING 两种状态调用 `stop_playback`，断言旧 task 退出、无后续帧、flush/EOS/屏障顺序正确（用事件列表）。
5. **EOS**：最后一块后 1.2 s 发 EOS；尾部补零到 4096 B；不会在队列还有音频时提前 EOS。
6. **屏障**：FakeLink 的 `control_barrier` 可立即返回；另写一个超时用例，断言超时后仍继续（只告警）。

### 6.2 离线回放（用已有 `calls.log`）

写一个纯函数 `simulate(arrivals, lead_ms)`（不碰设备）：

- 输入：`calls.log` 的 `(timestamp, bytes)`；用 1.0 s 间隔切流；
- 输出：`start_delay`、`underrun_frames`、`max_send_gap`；
- 对 A 类开头的 406 ms，P0 后应消失，不应计入 B 类；对 B 类，把 406 ms 当作“句中空档”做敏感性分析（例如人为把 gap 移到第 5 块后）；
- 验收：`lead=300` 时，400 ms 空档的模拟 underrun 为 0；`lead=0` 时 >0；`lead=400` 时余量更大但延迟 +100 ms。

> 注意：现有 `calls.log` 的 406 ms 主要是 A 类，不是 B 类的真实样本；不能拿它直接证明 P1 在线上有效，只能做“如果空档发生在句中”的敏感性验证。

### 6.3 设备 A/B（静音下可做，只看统计）

设备当前故意静音（level 0/127），**不要改音量**；静音不影响 `PumpPeriod`、period 计数和 underrun 统计。

1. 生成一个 48 kHz mono WAV：3 段语音/音调，段间在**发送侧**人为留 400 ms（不是文件里的静音，而是分 3 次 `play()` 调用，中间 sleep 400 ms）。
2. 通过 `DOT2_INJECT_PORT=8871` 的 loopback hook 注入：`python -m rui_dot2_bridge.inject_cli --wav ...`；分别用 `DOT2_PLAYBACK_LEAD_MS=0` 和 `300` 跑。
3. 每次跑完读设备日志：

```bash
export PATH=/opt/homebrew/share/android-commandlinetools/platform-tools:$PATH
adb -s G090LF0964340RN6 shell su -c 'cat /tmp/server.log' | grep 'voice stream complete' | tail -5
```

验收：
- `LEAD=300`：`underruns=0`，`minDepth` 不低于 ~5，且没有因空档产生的第二条 `voice stream complete`；
- `LEAD=0`：应能看到与空档对应的 `minDepth=0`/`UNDERRUN`（或流被拆），作为对照；
- `primeWait` 不应出现 1 s 级的异常跳变（说明没有重新 prime）。

4. 用 `adb shell` 看 `speaker_flush received` 的时间与首帧时间，确认 P0 后 flush 不再落在前两块之后。

### 6.4 人耳 A/B（需要允许出声的窗口；本次不做）

- 10 次同一句长回复，P0+P1 开/关各 5 次（顺序打乱）；
- 记录“说完→出声”P50/P90，验收 P1 增加 ≤300 ms（若设 300 ms）；
- 记录句中卡顿次数；验收 0 次；
- 设备日志验收 `underruns=0`、无异常 `primeWait`、无 1-period 幽灵流。

---

## 7. 风险与开放问题

1. **P0 屏障的 RTT**：本机/局域网 5–50 ms；如果 control WS 拥塞或设备忙，超时 300 ms 会拖慢首声；需要超时后继续（只告警），不能阻塞回复。
2. **P0 的 flush→EOS 顺序**：在真机上要确认 `endStream()` 确实清掉 `discarding`（源码如此），并且 EOS 之后 `eosPending` 仍为 true（`flush` 已置位，`endStream` 在 `wasDiscarding` 时不改），这样下一流仍走 prime 旁路。
3. **P1 不能创造音频**：连续多次空档或 TTS 平均慢于实时时，lead 仍会耗尽；补零只能防止二次 prime，不能消除源空档。需要监控 `underrun_frames`。
4. **短回复**：`LEAD_MAX_WAIT` 要保证“在只收到一块时也能在 ~400–500 ms 内开声”；否则短回复延迟会比现在明显。
5. **EOS/stop 竞态**：sender task、`_eos_after_idle`、`stop_playback` 三者只能有一个 owner；建议用 `_gen` + 单一 sender 任务，删掉独立的 `_eos_task`。
6. **补零与统计**：补零会让设备 `periods` 增加、`bytesRecv` 增加，但不是真实语音；adapter 侧必须单独记 `underrun_frames`，不要用设备 `periods` 判断空档。
7. **设备侧长期修复**：`primePeriods` 可配 + `eosPending` 拆分是根治；否则 adapter 永远要维护“flush 旁路”的隐式契约。
8. **未验证**：本轮没有在 P0/P1 落地后重启 rui，也没有出声；所有延迟数字是代码/固件推算，需按 §6 实测确认。

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
| adapter `play/_drain/eos/stop` | `src/rui_dot2_bridge/adapter.py:336-423` |
| `send_pcm/end_stream/flush` | `src/rui_dot2_bridge/device_link.py:412-439` |
| 4096 B = 42.667 ms | `src/rui_dot2_bridge/frames.py:29-33, 58-65` |
| 设备帧分发 `0x02/0x03` | `/tmp/echomuse-inspect/device/internal/client/data.go:940-949` |
| 设备缓冲常量 | `.../internal/bindings/speaker/pcm_speaker.go:25, 41-51, 59, 68, 796` |
| 设备流状态机 | `.../internal/bindings/speaker/stream.go:128-161, 184-215, 246-288, 295-322` |
| 设备 `playback_stats` 语义 | `/tmp/echomuse-inspect/docs/device-controller-interface.md:191` |
| 设备 `speaker_flush` | `.../docs/device-controller-interface.md:216`；`internal/client/control.go:992-998` |
| 小爱对照与 A 类证据 | `docs/stutter-rui-xiaoai.md` |

### 一句话

**先给 flush 加控制面屏障（保留 prime 旁路），再用 250–300 ms 的 adapter lead + 42.7 ms 定速 + 空档补零兜住句中 TTS 断供；`wait_consumed` 在 v2.18.1 上没有播放头可报，不能作为本次方案。**
