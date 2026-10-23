# P3 固件补丁计划：把「去混叠参考」交给 rui

> 状态：**已核实上游代码路径，未编译、未烧写**。触发条件是 P2 的 Gate G1 判定
> "单声道不够"。adapter 侧已经就绪（`DOT2_REF_CHANNELS=1` → 2ch AudioFrame + `REFERENCE` 能力）。

上游版本：EchoMuse tag **`v2.18.1`**（commit `b18a0be`），与设备上的固件同版本。
设备安装是 **A/B 双槽 + 自动回滚**（`/data/local/bin/server -> server_a`，`start_server.sh`
在 3 次 <15 s 的快速退出后自动把符号链接换回去并退出让 init 重启），所以试点是安全的。

## 目标

上行从 `[mono beam]`（512 字节/ms）改为 `[beam mono | de-aliased ch8 ref]` 交织 2ch：

- 让 rui 的 AEC 拿到**与麦克风同帧、且无混叠**的参考（离线验证：参考重建把 ERLE
  从 6.9 dB 提到 38.3 dB，见 `docs/aec-offline-result.md`）；
- 由 controller 在 `ack.features` 里宣告 `dot2_hw_ref` 才启用；旧 controller 仍收单声道，
  所以**同一份固件向前向后都兼容**；
- 带宽 32 → 64 KB/s；设备侧同时关掉 7 麦 AEC（省单核 ~19%），改为参考抽取（~1–2%）。

## 上游代码事实（已核实）

| 事实 | 位置 |
|---|---|
| 原始采集 9ch S24 16 kHz，ch0-6 麦、ch8 播放回采；`beam.EchoRef(raw)` 取 ch8 | `internal/bindings/mic/pcm_microphone.go`、`internal/client/data.go` |
| ch8 是 48 kHz 输出每 3 取 1、**无抗混叠**；`dealias.clean(ref)` 用输出环重建干净参考 | `internal/aec/dealias.go` |
| `WriteFar` 由喇叭 ALSA 循环**每周期无条件**调用，且**先** `dealias.feed(period)`，之后才按 `enabled/hwRef` 提前返回 | `internal/aec/aec.go:406` |
| AEC 开关只影响 `Enabled()/Process*`，不影响参考抽取所需的输出环 | 同上 |
| 控制器能力在 `ack.features` 里读入；`connect()` 手里就有 `*DataClient` | `internal/client/control.go:284/539/686` |
| 上行成帧在 `streamMic`，80 ms 一批（`vadOwwChunkBytes=2560`） | `internal/client/data.go:120/1107/1418` |

## 补丁（约 100 行，4 个文件）

### 1) `internal/aec/aec.go` — 暴露参考给上行

```go
// RefForUplink returns the hardware reference for one captured period with the
// 3:1 decimation aliases removed (dealias.clean). It does NOT cancel anything,
// so it is valid with aecEnabled=false; the output ring it needs is fed by
// WriteFar on every speaker period regardless of that switch.
func (c *Canceller) RefForUplink(rawRef []byte) []byte {
    c.mu.Lock()
    d := c.dealias
    c.mu.Unlock()
    if d == nil {
        return rawRef
    }
    return d.clean(rawRef) // mic-goroutine side, its own ring mutex
}
```

### 2) `internal/client/data.go` — 特性开关 + 2ch 成帧

```go
// 新增字段与 setter
refUplink atomic.Bool
func (d *DataClient) SetRefUplink(on bool) { d.refUplink.Store(on) }
const vadOwwChunkBytes2Ch = vadOwwChunkBytes * 2
```

在 `streamMic` 的 `!lockMic` 分支（约 1380–1420 行）里，把「每 80 ms 一块 mono 直接
`sendFrame(chunk)`」改成：

```go
if d.refUplink.Load() {
    // 2ch: [beam mono | cleaned ref], same 80 ms boundary.
    frame := make([]byte, 0, len(chunk)*2)
    for i := 0; i+1 < len(chunk); i += 2 {
        frame = append(frame, chunk[i], chunk[i+1])
        if i+1 < len(refChunk) { frame = append(frame, refChunk[i], refChunk[i+1]) }
        else                    { frame = append(frame, 0, 0) }
    }
    sendFrame(frame)
}
```

`refChunk` 的产生点：在 beam 之后、AEC 之前（同一 `raw` 周期）：

```go
var refChunk []byte
if d.refUplink.Load() {
    if rawRef := d.beam.EchoRef(raw); rawRef != nil {
        refChunk = d.aec.RefForUplink(rawRef)   // 16 kHz mono S16, 512 samples
    }
}
```

要点：
- **不要**把 `EchoRef` 塞进 `if d.aec.Enabled()` 里（现有代码就是那样，因为只给 AEC 用）；
  ref-uplink 需要独立提取。
- 参考必须是**本周期**的（同帧对齐），不要走软件 tap 的 ring/aecDelayMs。
- 批次大小、`sendFrame` 的 `0x01` 帧类型都不变，只是负载变成 2ch。
- VAD/AGC/preroll 仍只作用于 mono，不变。

### 3) `internal/client/control.go` — 能力协商

- `capabilities()` 增加 `"ref_uplink"`（声明本固件能送 2ch）；
- `connect()` 里 `c.setFeatures(first.Features)` 之后：

```go
data.SetRefUplink(hasFeature(first.Features, "dot2_hw_ref"))
```

`hasFeature` 是本仓库里已有写法的同类小函数（参考 `setFeatures` 附近的处理）。

### 4) 控制器侧（本项目，已就绪）

- `DOT2_REF_CHANNELS=1` → adapter 宣告 `REFERENCE`、`AudioFrame(channels=2, ref=1)`；
- rui 配置 `audio.capture: {channels: 2, mic_channels: 1, ref_channels: 1}`、`aec.cancel: mix`；
- adapter 在 connect 时推 `aecEnabled:false`（关设备 AEC，避免级联）。

## 构建

```bash
git clone --depth 1 --branch v2.18.1 https://github.com/wilbowes/EchoMuse.git /tmp/echomuse-p3
# GoTinyAlsa 是 sibling 目录（compile.sh 会把它挂进容器）
cd /tmp/echomuse-p3/device
docker build -t echomuse-compiler compiler      # 首次：NDK21 + Go 1.24 镜像
./compile.sh                                     # 产物 build/server，版本号自动带 -dev
sha256sum build/server
```

## 安装 / 验证 / 回滚（用设备已有的 A/B 槽）

```bash
# 安装到空闲槽
adb push build/server /data/local/bin/server_b.new
adb shell su -c 'mv /data/local/bin/server_b.new /data/local/bin/server_b && chmod 755 /data/local/bin/server_b'
adb shell su -c 'ln -sf server_b /data/local/bin/server'
adb shell su -c 'kill $(pidof server)'   # supervisor 会用新槽重启；3 次快速退出会自动换回 server_a

# 验证
adb shell su -c 'cat /tmp/server.log'      # 期望：[aec] disabled（controller 推的）+ 无 panic
adb shell su -c 'readlink /data/local/bin/server'   # server_b
curl -s http://127.0.0.1:8091/api/v1/audio | jq '.metrics.aec_enabled, .metrics.aec_erle_db'
# rui: 期望 aec_enabled=true（rui 自己的），erle_db ≥ 20

# 回滚（任选其一）
adb shell su -c 'ln -sf server_a /data/local/bin/server && kill $(pidof server)'
# 或 3 次快速退出自动回滚；或断电重启后 start_server.sh 从符号链接决定
```

## 验收（G2）

- rui 控制台 `aec_erle_db` ≥ 20 dB（设备 AEC 基线同量级）；
- 播放中 barge-in 可用；误唤醒不高于 P2 的真人基线；
- 上行 ≤ 70 KB/s；设备可用内存 > 100 MB；CPU 不高于改动前；
- 断电重启自恢复；**不宣告 `dot2_hw_ref` 即回退单声道**（无需重刷）。

## 风险

| 风险 | 应对 |
|---|---|
| `dealias` 未 lock（首次、或环路被滤波）→ 参考仍有混叠 | 上游本身有 fallback；先用设备日志确认 `[aec] ch8 ... lock` 类证据；必要时先在实验室抓 raw 9ch 离线验证 |
| 参考与麦克风周期边界不一致 | 同一 `raw` 周期内同时取 `beam` 输出与 `EchoRef`，构造上同帧；用 chirp 交叉相关验证 ≤ 1 样本 |
| 2ch 上行把 WiFi 打满 | 64 KB/s 实测；若 > 70 KB/s 或丢包，退回单声道 |
| 固件分叉维护 | 补丁只碰 4 个文件、约 100 行；记录上游 tag（v2.18.1），升级时 rebase |
| 烧写后无法回滚 | A/B 槽 + 自动回滚已核实；旧 `server_a` 不覆盖 |
