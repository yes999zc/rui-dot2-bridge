# 为什么小爱 LX06 不卡、Echo Dot 2 卡：rui 播放链路只读对比

- 日期：2026-10-24
- 范围：只读调查 `house-ear`（rui）与 `xiaoai-client`、本仓库 adapter；**未改任何代码/配置，未重启 rui/设备，未动音量/静音**。
- 结论对应 issue：yes999zc/rui#1（“流式 TTS 到达与 Pacer 预存 320ms 不匹配”）。本次证据表明：**该 issue 描述的现象在 Dot 上首先由另一条更确定的路径触发**，TTS 断供是同一数量级的第二来源，不是本次样本的主因。

---

## 0. 结论摘要

1. **两者共用同一套 rui 播放调度**：`Pacer` + `Credit` + `_speak_loop`，没有“小爱分支/小爱参数”。差异全部在 adapter 及其背后的设备链路。
2. **Dot 的 ~400ms 空档是每轮回答开头必现的 flush 竞态，不是句中随机 TTS 抖动。**
   - rui 每轮首块前调用 `adapter.stop_playback()`（`rui: src/house_ear/audio/process.py:1108`）。
   - Dot adapter 把 `speaker_flush` 发在 **control WebSocket**，随后 PCM 发在 **data WebSocket**（`bridge: src/rui_dot2_bridge/device_link.py:117-118, 412-428, 437-439`）。两条 TCP 连接之间**没有顺序保证**。
   - 设备先收了 2 个 100ms 块（4 个 period，recvSpan 5–15ms）才处理到 flush，于是丢弃这 ~171–200ms 并结束流；下一段重新开流，服务端被反压 406ms。
   - 实测：`calls.log` 中 11 次 406/407/417ms 空档，**11/11 都在每轮回答第 2 块之后**；设备日志同一秒必有 `speaker_flush received` + `voice stream complete (periods=4 … recvSpan=5ms)`。见 §5。
3. **小爱没有这个竞态**：它只有一条 WebSocket，`stop_play`、`start_play`、PCM 全在同一条有序连接上；`play()` 会等 `start_play` 的回执，再静置 80ms 才发第一帧（`rui: src/house_ear/device/xiaoai/adapter.py:178-211`）。播放器的 kill/spawn 发生在**第一帧之前**，flush 不可能落在音频后面。
4. **小爱还有设备侧缓冲和播放头回报**：client 的 mpsc 队列 4×100ms=**400ms**，aplay stdin pipe 8KB=**250ms**，ALSA buffer **300ms**；`consumed` 事件让 rui 的 `Credit` 生效（实际 window=8，不是 2）。Dot 这三样都没有：`reports_consumption` 默认 `False`、无 `wait_consumed`、adapter 无播放队列，42.7ms 帧收到就同步转发。
5. **Dot 仍有一个真实的下限风险**：rui 的 `Pacer` 只保 250ms、上限 320ms（实测 lead max 343–344ms），且 `_speak_loop` 在队列瞬空时会硬睡 0.4s（`process.py:1141-1146`）。若 TTS 真的断 400ms，Dot 没有额外缓冲可吸收；小爱也只是“不容易被听见”（aplay 不重启 + 队列/ALSA 吸收一部分），并非理论上免疫。
6. **不改 rui 时，adapter 侧最小修复**：`stop_playback()` 在**空闲时直接 no-op**（去掉每轮开头的 flush 竞态），再加 ~400ms 发送侧抖动队列兜底。`reports_consumption` 只有在 EchoMuse 固件真的能回报播放头时才可补；当前设备没有这个事件，不能凭空实现。

---

## 1. rui 的公共播放路径（两种设备完全一样）

### 1.1 Pacer：只保 250ms，上限 320ms

`rui: src/house_ear/audio/process.py:83-97`

```python
PLAY_CHUNK_MS = 100
PREBUFFER_S = 0.25
CATCHUP = 1.0
MAX_LEAD_S = 0.32
```

`Pacer.sleep_for()`（`process.py:116-145`）按“已交给设备的音频何时播完”计算 `due`，目标是让设备手里始终有 ~250ms，最多 320ms。它是**墙钟**，不知道设备真实播放头。

实测（`/tmp/rui-prod-final.log:137, 186, 238, …`）：`reply: 13 chunks, lead max 343/344 ms`。即 rui 在 Dot 上实际也只领先约 344ms。

### 1.2 Credit：设备回报只作“在途上限”，且需要 adapter 声明支持

- 类默认 `window=2`（`process.py:159`），但音频面实例化用的是配置：`Credit(window=cfg.audio.playback.credit_window)`（`process.py:254`）。
- 配置默认 **8**，不是 2：`rui: src/house_ear/config/schema.py:236-242`、`config.example.yaml:86`、`config/template.yaml:60`。本仓库 dev 配置未覆盖，因此也是 8。
- 只有 adapter 声明 `reports_consumption=True` 才启用：`process.py:816-831` 的 `_credit_sync()` / `_credit_wait()`；否则直接返回 `False`，`_speak_loop` 只按墙钟。

> 所以“Credit window=2”是类默认值/老部署实测值，不是当前 rui 的行为。

### 1.3 `_speak_loop` 的时序

`rui: src/house_ear/audio/process.py:963-1146`：

1. 从无界 `_speak_queue` 取一个块（控制面按 100ms 切好，`audio/client.py:214-232`）；
2. 首个 `segment` 且 `not _reply_open` → **`await adapter.stop_playback()`**（`process.py:1095-1108`）；
3. `await self._credit_wait(adapter, seconds)`（无回报则立即返回）；
4. `await adapter.play(pcm, sample_rate=target, channels=1)`（`process.py:1114-1116`）；
5. 若有 credit，`_credit_note()`；
6. `Pacer.sleep_for()` 后 `asyncio.sleep(wait)`；
7. 若 `_speak_queued_s <= 0` 且队列空：**硬睡 0.4s** 再考虑恢复音乐（`process.py:1141-1146`）。

`_reply_open` 在队列空 0.5s 后复位（`process.py:978-985`），因此**每一轮回答都会走一次第 2 步 `stop_playback()`**。

### 1.4 PLAY_PCM → adapter

控制面每 100ms 一帧（`audio/client.py:228-231`），音频面重采样到 `audio.playback.rate` 后逐块调用 `adapter.play()`。**没有任何按设备型号分叉的调度参数。**

---

## 2. 小爱 LX06 / Xiaomi 的 adapter 与播放路径

### 2.1 文件与关键函数

| 文件 | 关键函数 | 作用 |
|---|---|---|
| `rui: src/house_ear/device/xiaoai/adapter.py` | `play()` 178-211、`stop_playback()` 245-256、`wait_consumed()` 223-243、`_on_text()` 380-397 | LX06 的 WebSocket adapter |
| `rui: src/house_ear/device/xiaoai/protocol.py` | `playback_payload()` 105-119、`stream()` 41-47 | 播放器几何/音频帧 |
| `xiaoai-client: packages/client-rust/src/services/audio/play.rs` | `start()` 87-190、`play()` 192-210、`stop()` 56-85 | 设备端 aplay 播放器 |

### 2.2 下行实现

- **单条 WebSocket**：`self._conn` 同时承载 `Request`、`Response`、`play` 流（`adapter.py:183, 211, 256, 260-267`）。所有控制与音频**同序**。
- **每轮新起播放器，且在首帧之前完成**：

  `adapter.py:186-208`

  ```python
  if not self._playing:
      await self.call("start_play", protocol.playback_payload(sample_rate), timeout=2.0)
      await asyncio.sleep(0.08)   # let aplay open before the first chunk
      self.consumed = 0
      self.sink_generation += 1
      self._playing = True
  ```

  设备端 `start()` 会先 kill 旧 aplay、spawn 新 aplay，再返回（`play.rs:56-85, 115-130`）。因此 kill/spawn 的成本**发生在第一帧 PCM 之前**。
- **100ms 块**：`bytes_per_chunk = sample_rate * 100 / 1000 * 2`（`adapter.py:209-211`）。
- **播放头回报**：client 每写进 aplay 一块就发 `{"event":"consumed","n":i}`（`play.rs:181-188`）；adapter 转成 `consumed`/`_consumed_event`（`adapter.py:386-396`），`wait_consumed()` 供 `Credit` 使用（`adapter.py:223-243`）。
- **stop 也是有序的**：`stop_playback()` 发 `stop_play` 请求并把 `_playing=False`（`adapter.py:245-256`）；下一帧 `play()` 必然先 `start_play` 等回执 + 80ms。flush/restart 不会落到已开始的音频中间。

### 2.3 设备侧缓冲（小爱的“余量”在哪）

| 层 | 大小 | 证据 |
|---|---:|---|
| client mpsc 队列 | 4 chunks × 100ms = **400ms** | `xiaoai-client: play.rs:132-135` |
| aplay stdin pipe | 8KB = **250ms** @16k mono | `play.rs:17-20, 120-127` |
| aplay/ALSA buffer | **300ms**（period 60ms ×5） | `rui: protocol.py:105-119` |
| 合计（能力上限，非时刻全满） | **~950ms** | 上述三处 |

注意：rui 的 Pacer 只会有意领先 ~250–344ms，所以这 950ms 不是时刻装满；关键在于**小爱在客户端/内核层存在可增长的队列，且 aplay 跨块保活、不因一次 starvation 重新开流**。短停顿表现为 ALSA underrun 后继续，不会额外叠加一次 prime。

---

## 3. Echo Dot 2（out-of-tree）的 adapter 与播放路径

### 3.1 文件与关键函数

| 文件 | 关键函数 | 作用 |
|---|---|---|
| `bridge: src/rui_dot2_bridge/adapter.py` | `play()` 336-366、`_drain_frames()` 380-385、`_eos_after_idle()` 392-412、`stop_playback()` 414-423 | Dot adapter |
| `bridge: src/rui_dot2_bridge/device_link.py` | `_send_control()` 412-418、`send_pcm()` 420-428、`end_stream()` 430-435、`flush()` 437-439 | 双 WebSocket 设备链路 |
| `bridge: src/rui_dot2_bridge/frames.py` | `split_playback()` 58-65 | 42.7ms 帧 |

### 3.2 下行实现

- **双 WebSocket**：`self._control` 与 `self._data` 是两条独立连接（`device_link.py:117-118`）；flush 走 control，PCM 走 data。**跨连接无顺序**。
- **没有播放队列/lead**：`play()` 只做响度处理，把 PCM 追加到 `_frame_buffer`，然后 `_drain_frames()` 立刻按 4096B（42.7ms）**同步** `await send_pcm()` 发走（`adapter.py:353-365, 380-385`）。adapter 不按时间排空，也没有“先存 400ms 再放”的 lead。
- **没有播放头回报**：本仓库全局搜不到 `reports_consumption` / `wait_consumed` / `consumed`；协议里 contract 默认 `reports_consumption=False`（`rui: src/house_ear/device/contract.py:171-187`）。因此 rui 的 `_credit_wait()` 对 Dot 恒为 `False`，**只按墙钟 pacing**。
- **`stop_playback()` 无条件 flush**：

  `adapter.py:414-423`

  ```python
  async def stop_playback(self, fade_ms: int = 120) -> None:
      ...
      self._playing = False
      self._frame_buffer.clear()
      self._flush_dump()
      if self._link is not None:
          await self._link.flush()      # -> {"type":"speaker_flush"} on /control
  ```

  `device_link.flush()` 只 `await` 了 control socket 的 `send`，**没有等设备确认**（`device_link.py:437-439`）。
- **EOS 已改为 1.2s**：`EOS_IDLE_S = 1.2`（`adapter.py:88-93`），避免 TTS 断 0.4s 就把流切断（这是 10:19 之前的另一个 bug，已修）。
- **设备侧缓冲**：adapter 注释写“~300ms”（`adapter.py:91-92`）；`frames.py:67-69` 又写“device primes ~1.1s of buffer before it starts”。两者不矛盾但都说明：**缓冲在固件内部，rui 看不见，也没有播放头回报**。

### 3.3 结论

Dot 的有效余量 = rui Pacer 的 250–344ms + EchoMuse 固件内部（约 300ms 量级，且流重启时会重新 prime）。**adapter 自己没有队列，也无法告诉 rui “设备还差多少”**。

---

## 4. 对比表

| 维度 | 小爱 LX06 | Echo Dot 2 |
|---|---|---|
| 控制/音频通道 | **同一条 WS**，天然有序 | control + data **两条 WS**，无序 |
| 每轮首帧前的重启 | `start_play` 请求/响应 + spawn + **80ms 静置** | 无 start 概念；`stop_playback` 只发 flush，不等确认 |
| flush 是否可能落在音频后 | 不会（同序 + 先 start 后 PCM） | **会**（control 的 flush 与 data 的 PCM 竞态） |
| adapter 自己的播放队列 | 无（但有设备 client 队列） | **无** |
| 设备侧队列 | mpsc 4×100ms=400ms + pipe 250ms + ALSA 300ms | 固件内部 ~300ms，不可见 |
| 播放头回报 | `consumed` 事件 → `wait_consumed()` | **无** |
| rui Credit | 启用（实际 window=8） | **不启用**（`reports_consumption=False`） |
| 一次 starvation 的后果 | aplay 继续跑，不重启流 | 设备 `voice stream complete` → 下一段重新 prime |
| 每轮开头的固定空洞 | 无 | **有，~406ms** |

---

## 5. 实测证据：406ms 到底在哪

### 5.1 `calls.log`：11/11 都在每轮回答第 2 块之后

`bridge: calls.log:27-34`（10:28:51 这一轮）：

```text
10:28:51.302066 bytes=9600 gap_ms=4959   <- 本轮第 1 块（前面是上一轮/静默）
10:28:51.308211 bytes=9600 gap_ms=6      <- 第 2 块
10:28:51.714424 bytes=9600 gap_ms=406    <- 空档
10:28:51.720563 bytes=9600 gap_ms=6
10:28:51.726623 bytes=9600 gap_ms=6
10:28:51.771669 bytes=9600 gap_ms=45
10:28:51.871661 bytes=9600 gap_ms=100
10:28:51.971451 bytes=9600 gap_ms=100
```

同类 406/407/417ms 共 11 次（`calls.log:29, 83, …`；完整列表可用 `grep -n 'gap_ms=4[0-1][0-9]' calls.log` 复现），**全部**满足：前面一个 3.3–16.8s 的轮次间隔 → 两块 6ms 连发 → 406ms → 3–4 块连发 → 45ms → 恢复 100ms 节奏。

`calls.log` 的时间戳写在 `adapter.play()` 内、`_drain_frames()` **之前**（`adapter.py:354-364`），所以相邻时间戳的差 = 上一块的发送耗时 + pacer 等待。406ms 期间的队列是**非空**的（同一时刻 rui 日志 `speak finished, 7 chunks still queued (0.6 s)`，`/tmp/rui-prod-final.log:177`），因此**不是** `_speak_loop` 的 0.4s 硬睡，也不是 TTS 没送到；只能是 `adapter.play()` 里的 `send_pcm()` 被设备反压。

### 5.2 设备日志：同一秒 flush + 4 个 period

`/tmp/dot2-mic2.log:3033-3034`（设备时区 UTC，对应 CST 10:28:51）：

```text
2026/10/24 02:28:51 [control] speaker_flush received — discarding buffered playback
2026/10/24 02:28:51 [speaker] voice stream complete — returning to silence
  (periods=4 underruns=0 minDepth=-1 primeWait=26ms recvSpan=5ms maxGap=1ms)
```

- `periods=4` × 42.7ms ≈ **171ms**，正好对应“2 个 100ms 块已经被接收”。
- `recvSpan=5ms` 对应 `calls.log` 里那两块 6ms 的间隔。
- 随后是一段新的 `voice stream`（`/tmp/dot2-mic2.log:3037`），说明**前两块被 flush 丢掉，剩余音频重新开流**。

同一 pattern 在该设备日志中反复出现（`grep -c 'periods=4 underruns=0 minDepth=-1'` = 41 次，覆盖的回轮次多于 `calls.log` 的时间窗），且与 `calls.log` 中每轮开头的 400ms 空档一一对应。

### 5.3 机制（已证实 + 推测边界）

已证实：

1. rui 每轮第一块前调用 `stop_playback()`（`process.py:1108`）。
2. Dot adapter 的 `stop_playback()` 无条件发 `speaker_flush`（`adapter.py:414-423`），走 control WS；PCM 走 data WS（`device_link.py:412-428`）。
3. 两条连接无序，设备可以先把前两块 PCM 读完，再处理 flush。
4. flush 到达后设备结束当前流（`voice stream complete (periods=4 …)`）。
5. 服务端下一次 `play()` 在 ~406ms 后才返回/进入，表现为 `calls.log` 的空档。

推测（未直接读到 EchoMuse 固件源码）：

- 这 ~406ms 是设备结束流 + 下一段重新 prime 的耗时；`primeWait` 统计在这类流上会出现 1–2s 的异常值，说明设备的流统计/prime 状态跨 flush 没有干净重置（与本仓库 `docs/findings.md:63-75` 的观察一致）。
- 即使设备在 ~170ms 内播完了前两块，flush 之后到新流 prime 之间仍是静音，听感就是“说了两个字，卡一下，再继续”。

### 5.4 为什么不是 issue 里说的“TTS 断供 vs Pacer 320ms”

- 本次样本的 11 次空档**全部在每轮开头**，且发生时 `_speak_queue` 非空（0.6–1.8s 已排队）；TTS 断供不会如此规律地只发生在第 2 块之后。
- 但 issue 的机制**真实存在**：如果 TTS 在句中真的断 400ms，`_speak_loop` 的 `sleep(0.4)` + Pacer 250ms 会让 Dot 饿死（见 §6.2）。它是同一数量级的第二来源，可能在别的回合/别的网络条件下出现。

---

## 6. 回答四个问题

### 6.1 小爱不卡的关键差异是什么？

**三层，按重要性排序：**

1. **重启/flush 与首帧的顺序**：小爱单 WS + `start_play` 回执 + 80ms 静置，播放器重启发生在第一帧 PCM 之前；Dot 双 WS，flush 可能落在前两块之后，把已开始的音频切断并触发重新 prime。**这是本次 406ms 的直接原因（已证实）。**
2. **设备侧缓冲层**：小爱在 client 里有 4×100ms=400ms 的 mpsc 队列、8KB=250ms 的 aplay pipe、300ms 的 ALSA buffer；即使 rui 只领先 250ms，设备端也有独立于 rui 的队列可以吸收短抖动。Dot 没有 adapter 侧队列，只有固件内部 ~300ms，且流一断就重新 prime。
3. **播放头回报**：小爱有 `consumed` 事件，rui 的 `Credit` 能生效（window=8，只作在途上限）；Dot 没有，rui 只能“pacing by the clock”。

> “小爱完全不卡”不能只归因于缓冲：如果 TTS 真断 400ms，aplay 也会 underrun。但小爱**不会把一次 underrun 升级成一次流重启 + prime**，所以听感上限好得多；再加上客户端队列，400ms 抖动通常被吞掉。

### 6.2 Dot 的 adapter 缺哪一环？

- **缺“有序的 start/flush 屏障”**：没有 `start_play` 之类的请求/响应把“重启完成”同步到第一帧之前；flush 走另一条连接且不等确认。
- **缺自己的播放队列/lead**：`play()` 直接同步转发 42.7ms 帧，没有 400ms 的 prebuffer 层，无法把设备的一次 400ms 反压挡在音频之外。
- **缺 `wait_consumed`/`reports_consumption`**：rui 的 `Credit` 对 Dot 恒关闭，无法知道设备真实的播放头，只能按墙钟（250–320ms）pacing。
- 另外一个**代码级**的 400ms 来源（共用，不是 Dot 独有）：`_speak_loop` 在队列瞬空时硬睡 0.4s（`process.py:1141-1146`），且不响应“新块到达”事件；Dot 没有缓冲吸收，会直接听成空档。本次样本没有命中它（队列非空），但它是真实风险。

### 6.3 rui 侧最小改动是什么（不改 rui 的前提下，adapter 侧能补什么）？

**P0（最小、直接消掉本次 406ms）**：让 `stop_playback()` 在“本来就没在播”时 no-op。

```python
# bridge: src/rui_dot2_bridge/adapter.py
async def stop_playback(self, fade_ms: int = 120) -> None:
    if self._eos_task is not None:
        self._eos_task.cancel()
        self._eos_task = None
    # 设备已经 idle（上一轮已发 EOS，_playing=False）时，flush 只会和
    # 随后 data WS 上的前两块 PCM 抢跑；rui 每轮开头都会调用本函数。
    if not self._playing and not self._frame_buffer:
        return
    self._playing = False
    self._frame_buffer.clear()
    self._flush_dump()
    if self._link is not None:
        await self._link.flush()
```

理由：rui 调用 `stop_playback()` 的初衷是“每轮新起播放器/清掉残留”，这是 LX06 的 `start_play` 语义（`process.py:1100-1108` 的注释）。Dot 没有保活播放器：上一轮 EOS 已经结束流，空 idle 时不需要 flush。barge/`StopAudio` 时 `_playing` 为 `True`，flush 照旧，打断语义不变。

**P1（兜底 400ms 抖动）**：在 adapter 内加一个发送侧 lead 队列：

- 首块到达后先攒 ~400ms（或 4×100ms），再由独立 drain task 按实时速率 `send_pcm`；
- `play()` 只入队立即返回，rui 的 Pacer 不再被设备反压卡住；
- 代价是首次出声 +400ms（与 issue 里“Pacer 预存 320ms 不匹配”的取舍一致）。

这等价于把小爱 client 的 `mpsc::channel(4)` 搬到 Dot adapter 里，是**不依赖固件**就能做的。

**P2（可选）**：如果 EchoMuse 固件能在 `/control` 或 `/data` 上提供任何“已播放/已接收”计数，补 `reports_consumption` + `wait_consumed`，让 `Credit` 生效。**当前源码/日志里没有这个事件**，不能只靠 adapter 凭空实现。

**P3（feedback 给 rui#1，不在本仓库改）**：

- `Pacer` 的 `PREBUFFER_S`/`MAX_LEAD_S` 对“无播放头回报的设备”应可配置到 ≥400ms；
- `_speak_loop:1141-1146` 的 0.4s 硬睡应改为“等待队列有新块或超时”，否则它本身就能制造 400ms 空洞；
- issue 的描述建议补一句“Dot 上还有每轮开头的 flush/prime 竞态”，避免只往 TTS 到达方向修。

### 6.4 证据分级

**已证实（代码 + 日志）**

- 两者共用 Pacer/Credit，无设备分支（`process.py:963-1146`）。
- 小爱单 WS、start_play 回执 + 80ms、consumed 回报（`adapter.py:178-243`；`play.rs:115-188`）。
- Dot 双 WS、无 `reports_consumption`、`play()` 同步转发、`stop_playback()` 无条件 flush（`device_link.py:117-118, 412-439`；`adapter.py:336-423`）。
- 每轮开头 406ms 空档与设备 `periods=4`/flush 的时间对齐（`calls.log`；`/tmp/dot2-mic2.log:3033-3034`）。

**高置信推测**

- 406ms 的剩余部分是 EchoMuse 结束流后重新 prime 的时间；固件没有干净重置 prime 统计（`primeWait` 异常值支持这一点，但未读固件源码）。
- 小爱的 400ms mpsc + 250ms pipe + 300ms ALSA 在真实回合里吸收了多少，取决于 TTS 到达曲线；没有做同源对照录音，不能量化“吸收率”。

**未证实**

- EchoMuse 固件内部真正的 buffer 深度（~300ms 来自 adapter 注释，~1.1s prime 来自 `frames.py` 文档，两者都不是实测曲线）。
- 其它 TTS 供应商/网络条件下，Dot 句中是否也会出现非开头的 400ms 空档（本次样本没有）。

---

## 7. 复现（只读）

```bash
cd ~/AI_Workspace/01_Code/projects/rui-dot2-bridge

# 1) 每轮开头的 400ms 空档
grep -n 'gap_ms=4[0-1][0-9]' calls.log

# 2) 对应的 rui 侧：队列非空、lead max
grep -nE 'reply: first chunk|speak finished|reply: [0-9]+ chunks' /tmp/rui-prod-final.log | tail -30

# 3) 设备侧：flush 后 4 个 period 结束流
grep -nE 'speaker_flush received|periods=4 .*recvSpan=' /tmp/dot2-mic2.log | tail -20
```

---

## 8. 一句话

**小爱不卡，不是因为 rui 给它开了小灶，而是因为它的播放器重启发生在第一帧之前（单 WS + start_play 回执 + 80ms），并且设备侧有 400ms 队列 / 250ms 管道 / 300ms ALSA 和播放头回报；Dot 卡，是因为 rui 每轮开头的那次 `stop_playback()` 被 adapter 发到了另一条 WebSocket 上，flush 追上前两块 PCM，把 200ms 音频切掉并触发重新 prime，而 adapter 既没有队列也没有播放头回报来吸收这 ~400ms。**
