# AEC 参数模型：一套规则同时支持封闭类与开放类音箱

- 日期：2026-10-24
- 角色：Dot2 桥（插头侧）设计调研；rui 是插座
- 只读声明：本次只读源码与文档，**未改任何代码/配置，未重启设备或 rui，未改音量**；本文件是唯一产出
- 结论：AEC 不能建模成一个布尔值，而要建模成 **`who`（谁消）+ `reference`（参考从哪来、可不可信）+ `constraints`（什么组合合法）** 三件事

---

## 0. 结论（先看这 6 条）

1. **`who` 是策略，`reference` 是物理事实，`constraints` 是校验。** 三者分开，才能让同一套 rui 规则同时吃“厂商全包”的封闭音箱和“固件可改”的开放音箱。
2. **封闭类默认**：`who=device`、`reference.source=none`、controller AEC 关闭、barge 只保留 wake 模式。
3. **开放类默认**：`who=controller`、`reference.source=device_loopback`、`same_frame=true`、`dealiased=true`、`alignment=frame_aligned`、controller AEC 开启。
4. **选择依据不是品牌**，而是 manifest 的 `openness` + `Capability.REFERENCE` + `DeviceInfo.ref_channels` + 固件 feature（`ref_uplink` / `dot2_hw_ref`）+ `device.bypassable/controllable`。
5. **rui 现在只有全局 `audio.aec`，只能靠 `ref_channels` 隐式分流，无法表达“一台设备 controller、另一台 device”，也无法表达“有 ref 但仍让 device 消”。** 最小新增是 `devices[].aec`（每设备覆盖）+ `DeviceInfo.features`（能力门控）+ 观测字段；这是本报告对 rui 的 P0 需求。
6. **`both`（级联）默认关闭。** AGC 会让回声路径非线性，级联只在测量/调参时开；设备侧 ERLE 预算 ≤15 dB，总目标 ≥30 dB。`who=controller` 时设备 AEC 必须能关；关不掉就只能 `both`（还需 `cascade_allowed`）或 `device`。

---

## 1. 两类音箱 × AEC 参数总表

> 读法：这张表是“默认值 + 允许范围”的速查；字段的精确定义在 §2–§6，推导与映射在 §8。
> `—` = 不适用；`n/a` = 物理上不存在；`auto` = 由 §3.4 的算法在部署时解析。

| 参数 | 封闭类（device AEC；LX06 默认） | 开放类（controller AEC；Dot2） | 依据 / 备注 |
|---|---|---|---|
| **身份** | | | |
| `openness` | `closed`（厂商 DSP 不可控） | `open`（固件可改、参考可上送） | 也可 `semi`：厂商给回采但 DSP 不可控 |
| `who` | `device` | `controller` | `device` / `controller` / `both` / `none` |
| `who_allowed` | `[device, none]` | `[controller, device, none]` | 硬白名单；运行时不得越界 |
| 权限类（rui `class`） | A 类也可是 closed | B 类也可是 closed | **权限等级 ≠ 音频开放度**，不要混用 |
| **参考通道** | | | |
| `reference.source` | `none`（或 `vendor_tap`） | `device_loopback`（ch8） | 厂商回采见 §4.2 |
| `reference.channels` | `0`（或 `2`，厂商回采） | `1` | LX06 的 ch6/ch7 同源，rui 只取第一路 |
| `reference.same_frame` | `false` | `true` | 与 mic 同一 TDM/交织帧 |
| `reference.fixed_delay_ms` | `null` | `0` | 开放类同帧 + dealias 锁定；LX06 厂商回采实测 2.4 ms（§10.3） |
| `reference.dealiased` | `false` | `true` | raw ch8 只有 ~1 dB；去混叠后 ~25.7 dB |
| `reference.alignment` | `unknown` | `frame_aligned` | rui 不支持 `search`，见 §5 |
| `reference.rate/format` | 随厂商 | `16000` / `s16le` | 与 `voice.rate` 一致最省事 |
| **控制侧** | | | |
| `controller.enabled` | `false` | `true` | 与 `who` 保持一致 |
| `controller.filter_len` | `—`（4096 仅占位） | `2048`（128 ms；保守 4096） | taps @ `voice.rate`；公式见 §8.2 |
| `controller.frame_ms` | `—` | `20` | speex 内部固定 128 样本（8 ms @16k） |
| `controller.delay_search_ms` | `—` | `[0, 0]` | rui 当前无搜索；`search` 视为不支持 |
| `controller.double_talk` | `—` | `dtd` + `freeze_weights=true` | 半双工会杀掉 barge |
| `controller.cancel` | `—` | `mix` | 单麦时等价单滤波器；`per_channel` 仅诊断 |
| `controller.barge_in` | `false` | `true` | 这里指“允许 wake 打断” |
| `controller.erle_target_db` | `—` | `20`（stretch 25） | 只统计 controller 这一级 |
| **设备侧** | | | |
| `device.enabled` | `true` | `false` | 接管时必须推 `aecEnabled=false` |
| `device.tail_ms` | `300`（厂商值） | `64` | hw ref 时设备固定 64 ms，`aecTailMs` 不生效 |
| `device.delay_ms` | `0` | `0` | hw ref 时 `aecDelayMs` 不生效 |
| `device.ref_source` | `auto` | `hw` | `auto` 会探测；接管时建议钉 `hw` |
| `device.agc` | `null`（厂商内部，不可观测） | `false` | controller AEC 需要线性回声路径 |
| `device.barge_in` / 阈值 | `null`（厂商内部） / `0.05` | `false` / `0.05` | 设备 OWW 的 barge；rui KWS 接管时无关 |
| 可控性 | 通常 `controllable=false` | `controllable=true`，设备接受 8 个（适配器现推 7 个） | 见 §6 |
| **约束** | | | |
| `constraints.mutual_exclusion` | `true` | `true` | 同一时刻只允许一个“主消” |
| `constraints.cascade_allowed` | `false` | `false`（默认） | 只有测量/调参才开 |
| `constraints.ref_required_for` | `[acoustic_barge]` | `[controller, acoustic_barge]` | wake barge 不需要参考 |
| `constraints.fallback` | `[device, none]` | `[controller, device, none]` | 降级链见 §11 |
| 迁移期（无 `audio.aec` 块） | 旧规则：有 ref → controller（仅迁移期） | 旧规则：有 ref → controller（仅迁移期） | **不能默认回退**，见 §8.3 |
| **rui 派生** | | | |
| `audio.aec.enabled` | `false` | `true` | 全局 kill switch + 每设备 `who` 门控 |
| `audio.aec.filter_len` | `4096`（不生效） | `2048` / `4096` | 见 §8.2 |
| `audio.capture.mic/ref` | `1/0` 或 `6/2` | `1/1` | 平面以帧为准，config 是期望值 |
| `audio.voice.barge_in` | `true`（wake） | `true` | `mode != none` |
| **观测** | | | |
| 主要字段 | `device_aec_enabled` | `ref_active` / `ref_locked` / `aec_erle_db` | 见 §12.2 |
| **验收** | | | |
| controller ERLE | n/a（不可见） | ≥20 dB（stretch 25） | 播放中、稳定 10 min |
| 自听 turn | ≤2 / 50 | ≤1 / 50（gate 0/20） | 见 §12.1 |
| wake barge | 20/20 | ≥18/20 | 播放中喊唤醒词 |
| 打断静音 P50 | ≤600 ms | ≤600 ms | 服务端 ≤300 ms + 设备缓冲 ~300 ms |

附注：LX06 现网（open-xiaoai 8ch）不是 `reference.source=none`，而是 `vendor_tap`（ch6/ch7）；本表“封闭类”列给的是“厂商只给处理后单声道”的最小形态，8ch 变体见 §10.3。`null` 在设备侧表示“厂商内部、不可观测/不可控”，不是“关闭”。

---

## 2. 参数分层：L0 插头 / L1 部署 / L2 运行时

### 2.1 三层与优先级

```
L0 物理能力（plug manifest，只读事实）
      │  设备能做什么、参考从哪来、固件支持什么 feature
      ▼
L1 部署覆盖（rui config.yaml，运维策略）
      │  这台/这类设备默认谁消、barge 开不开、参数初值
      ▼
L2 运行时（control plane API，临时/调试）
      │  改 who、改设备 tail、改 barge 阈值；受 L0 硬约束
      ▼
effective plan（生效参数 + provenance + 校验结果）
```

优先级：**L2 > L1 > L0（仅限策略与数值）；物理事实 L0 最高，L1/L2 只能“确认”不能“改写”。**
这与现有 `plug.json < DOT2_* 环境变量 < rui DeviceConfig` 的方向一致（`plug-socket-contract.md:73-76`），但补了一条：**物理事实不能被部署覆盖掉**。

### 2.2 L0：manifest 声明什么

AEC 块建议放在 `audio.aec` 下（完整 JSON 见 §10）：

```yaml
audio:
  capture: { ... }              # 物理布局：rate/channels/mic_channels/ref_channels/bits/shifts
  aec:
    openness: closed|semi|open
    who: device|controller|both|none     # 默认策略
    who_allowed: [...]                   # 允许运行时切换的范围
    reference:
      source: none|vendor_tap|device_loopback
      channels: 0..N
      used_channels: 1
      same_frame: true|false
      rate: 16000
      format: s16le
      bits: 16
      fixed_delay_ms: 0|null
      alignment: frame_aligned|fixed|search|unknown
      dealiased: true|false
      requires_feature: ""               # dot2_hw_ref / ref_uplink
      availability: always|while_playing|conditional
      active_rms_dbfs: -80
      min_snr_db: 6
    controller:
      enabled: true
      filter_len: 2048
      frame_ms: 20
      delay_search_ms: [0, 0]
      double_talk: dtd|freeze|half_duplex|none
      freeze_weights: true
      cancel: mix|per_channel
      barge_in: true
      erle_target_db: 20
      erle_floor_db: 6
      hp_hz: 90
      cpu_rtf_max: 0.5
    device:
      present: true
      bypassable: true
      controllable: true
      observable: true
      enabled: false
      tail_ms: 64
      delay_ms: 0
      ref_source: hw|sw|auto
      agc: false
      barge_in: false
      barge_in_threshold: 0.05
      mic_gain_db: 18
      params: [aecEnabled, aecTailMs, aecDelayMs, aecRefSource, agcEnabled, bargeInEnabled, bargeInThreshold, micGainDb]
      clamps: {tail_ms: [50, 500], delay_ms: [0, 1000], frame_ms: 32}
    barge_in:
      mode: wake|acoustic|both|none
      max_stop_ms: 600
    constraints:
      mutual_exclusion: true
      cascade_allowed: false
      cascade_device_erle_db: 15
      ref_required_for: [controller, acoustic_barge]
      fallback: [controller, device, none]
      sticky_ms: 30000
```

字段归属规则：

| 字段组 | 谁能改 | 说明 |
|---|---|---|
| `capture.*`、`reference.*` | **只有 L0** | 物理事实；L1/L2 只能断言相等，否则拒绝 |
| `openness`、`who_allowed` | **只有 L0** | 开放度与许可范围不能由部署扩大 |
| `who`、`barge_in.mode` | L0 默认，L1/L2 可改 | 必须落在 `who_allowed` 内 |
| `controller.*` | L0 默认，L1/L2 可改 | 受 schema 上下限与 `cancel` 约束 |
| `device.*` | L0 默认，L1/L2 可改 | 受设备 clamps；`controllable=false` 时只是“期望值” |
| `constraints.*` | **只有 L0** | 只能收紧、不能放松；见 §3.2 |

### 2.3 L1：rui 部署覆盖

现有 rui 的 AEC 配置是**全局**的（`schema.py:308-312`）：

```yaml
audio:
  aec:
    enabled: true          # 全局 kill switch；语义保持不变
    filter_len: 4096       # taps @ voice.rate
    cancel: mix            # mix | per_channel
```

新增**每设备覆盖**（P0 需求，见 §9）：

```yaml
devices:
  - id: living
    adapter: dot2
    aec:                   # 可选；缺省继承 audio.aec
      who: controller
      barge_in: {mode: wake, max_stop_ms: 600}
      controller: {filter_len: 2048, cancel: mix}
      device: {agc: false, tail_ms: 64}
  - id: bedroom
    adapter: xiaoai
    aec:
      who: device
      barge_in: {mode: wake}
```

没有 `devices[].aec` 时，`audio.aec` 仍是默认策略；这样旧配置不改也能跑（迁移规则见 §8.3）。

### 2.4 L2：control plane API

建议的最小接口（只提需求，不改 rui）：

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v1/devices/{id}/aec` | 返回 effective plan + provenance + state + 最近一次校验 |
| `PUT` | `/api/v1/devices/{id}/aec` | 局部 patch：`who`、`barge_in.mode`、`controller.*`、`device.*`；校验失败返回 422 + 原因 |
| `POST` | `/api/v1/devices/{id}/aec/reset` | 回到 L1 |
| `POST` | `/api/v1/devices/{id}/aec/probe` | 可选：放 chirp/粉噪，测延迟与 ERLE（用户主动触发） |

L2 规则：

- 只能**收窄** `who_allowed`，不能新增参考、不能把 `bypassable=false` 变成可旁路；
- `filter_len`/`enabled` 改动通常需要重启音频面（现有 config catalog 标注 `restart`，`catalog.py:638-650`），API 要返回 `restart_required`；
- `who` 改动要审计 + 显式确认，且默认**不持久化**（TTL 30 min 或重启失效）；要持久化必须显式“保存到 L1”；
- 任何改动返回**生效值**，不是请求值。

### 2.5 合并与 provenance

每个生效字段记录来源：`manifest` / `config` / `runtime` / `derived`。控制台回答“为什么这台是 controller AEC”时，必须能指到具体一层，而不是只看一个布尔值。

---

## 3. “谁来做”：owner 语义与不变式

### 3.1 四个取值

| `who` | 设备 AEC | 控制器 AEC | 需要参考 | acoustic barge | 典型 |
|---|---|---|---|---|---|
| `device` | 开 | 关 | 不需要 | 不可用 | 封闭类 LX06 |
| `controller` | 关（或旁路） | 开 | **需要** | 可用 | 开放类 Dot2 |
| `both` | 开（≤15 dB 预算） | 开 | 需要，且必须是**设备 AEC 之前**的参考 | 可用 | 测量/调参，不做默认 |
| `none` | 关 | 关 | 不需要 | 不可用 | 调试/无 AEC 兜底 |

### 3.2 不变式（硬约束）

- **I1**：`who=controller` ⇒ `reference.available=true` ∧ `controller.enabled=true` ∧ `device.enabled=false`。
- **I2**：`who=device` ⇒ `controller.enabled=false`；设备 AEC 的实际状态由厂商决定，`device.enabled` 只是期望值。
- **I3**：`who=both` ⇒ `device.enabled=true` ∧ `controller.enabled=true` ∧ `constraints.cascade_allowed=true` ∧ `reference.source ∈ {device_loopback, vendor_tap}` ∧ 参考是**设备 AEC 之前**的信号。
- **I4**：`who=none` ⇒ 两个 `enabled` 都关；`barge_in.mode` 不得为 `acoustic`/`both`。
- **I5**：`barge_in.mode ∈ {acoustic, both}` ⇒ `who ∈ {controller, both}` ∧ `reference.available=true`。**wake 模式不受此限**（实测无 AEC 也能打断，`aec-takeover.md:38-41`）。
- **I6**：`who=controller` 且 `device.bypassable=false` ⇒ 非法；只能退成 `both`（如果 `cascade_allowed`）或 `device`。
- **I7**：`controller.filter_len` ≥ `ceil((reference.fixed_delay_ms + room_tail_ms) * voice.rate / 1000)`，128 对齐；`alignment=search` 时还要覆盖搜索上限（rui 当前不支持，见 §5）。
- **I8**：`device.tail_ms`/`device.delay_ms` 只在 `device.ref_source=sw` 时有效；`hw` 参考下设备固定 64 ms、忽略这两个值（`aec.go:62-70, 330-342`）。

### 3.3 `both`（级联）的额外约束

级联不是“两个 AEC 都开就更好”，它有三个物理问题：

1. **参考必须在设备 AEC 之前。** 如果只能拿到“设备 AEC 之后”的信号，控制器看到的是已经非线性的残差，无法用线性滤波器建模；这种 `both` 必须拒绝。
2. **AGC 破坏线性。** 设备 AGC 只作用于 bounded `lockMic` turn 流（`config.go:117-121`），但它会让回声路径随时间变化；级联时 `device.agc=false`，否则控制器滤波器追不上。
3. **过抑制会泵。** 设备已经消了 20 dB，控制器再消 20 dB，近端语音的微小失真会被放大；所以设备侧预算 ≤15 dB，控制器目标 ≥20 dB，总目标 ≥30 dB，且只在 `openness=open`（或 `semi` 且厂商 tap 在 AEC 前）时允许。

结论：`cascade_allowed=false` 是默认；打开它需要显式理由（测量、对比、厂商 AEC 不可关但参考可用），并写入审计。

一个必须说清的缺口：**设备侧没有“ERLE 目标”参数**（`aec.go` 只有 `tailMs`/`delayMs`/开关），所以 `cascade_device_erle_db=15` 目前只能通过“减小 tail”或“只开设备 AEC 的一小段”间接逼近，不能精确设定。若设备实际 ERLE 已经 20 dB 且无法调低，`both` 只应作为测量态，不应作为产品默认；此时诚实的选择是 `device`（承认厂商 AEC 已经够用）或 `controller`（如果能旁路设备 AEC）。

### 3.4 自动选择算法（`who=auto`）

```
输入：manifest（openness/reference/device/controller）、probe 的 DeviceInfo、firmware features
输出：who + 理由

allowed(x) = x in manifest.who_allowed

1. 若 manifest.who != auto：直接用它（但仍要过 §3.2 校验）。
2. 若 openness == closed：
       allowed("device") && device.present ? device : none
3. 若 openness == semi：
       allowed("device") && device.present
           ? device
           : (allowed("controller") && reference.available ? controller : none)
       # 厂商回采默认只做“可选辅助”，不自动接管
4. 若 openness == open：
       ref_ok = reference.available
                && Capability.REFERENCE in info.capabilities
                && DeviceInfo.ref_channels > 0
                && reference.dealiased
                && reference.alignment != search
                && feature_ok(reference.requires_feature)
       if ref_ok && allowed("controller") && controller.allowed && device.bypassable:
            controller
       elif ref_ok && allowed("both") && controller.allowed && constraints.cascade_allowed:
            both                # 只有 L0 明确允许级联才会走到这里
       elif allowed("device") && device.present:
            device
       elif ref_ok && allowed("controller"):
            controller          # 参考不可信时也聊胜于无，但 erle_target 降级
       else:
            none
```

`feature_ok` 的来源：

- 适配器 `probe()` 的 `DeviceInfo.features`（P0 新增，见 §9）；Dot2 是 `ref_uplink`，控制器侧宣告 `dot2_hw_ref`；
- `Capability.REFERENCE` 是粗粒度摘要，`DeviceInfo.ref_channels > 0` 才是可用的参考；两者不一致时以 `ref_channels` 为准并告警；
- `DeviceInfo.ref_channels` 与 `AudioFrame.reference_channels` 必须一致；不一致以帧为准并告警；
- `DeviceClass`（A/B）只用于安装/权限提示，**不作为 AEC 选择依据**；一个 B 类设备完全可能 `openness=closed`（能 root，但厂商 DSP 不可旁路）。

---

## 4. 参考通道模型

### 4.1 字段定义

| 字段 | 取值 | 含义 | 谁提供 |
|---|---|---|---|
| `source` | `none` / `vendor_tap` / `device_loopback` | 参考的物理来源 | L0 |
| `channels` | 0..16 | 物理参考通道数 | L0 |
| `used_channels` | 1 | 控制器实际使用的通道数；rui 只取第一路（`process.py:490`） | L0 |
| `same_frame` | bool | 是否与 mic 在同一交织帧；true 则无需跨流对齐 | L0 |
| `rate` / `format` / `bits` | 16000 / s16le / 16 | 参考的采样格式 | L0 |
| `fixed_delay_ms` | 0 / 2.4 / null | 已知固定延迟；null = 未知，不能自动接管 | L0 |
| `alignment` | `frame_aligned` / `fixed` / `search` / `unknown` | 对齐方式 | L0 |
| `dealiased` | bool | 是否已做抗混叠重建 | L0 |
| `requires_feature` | string | 固件 feature 名；空 = 不需要 | L0 |
| `availability` | `always` / `while_playing` / `conditional` | 参考什么时候有值 | L0 |
| `active_rms_dbfs` | -80 | `ref_active` 的门限 | L0/派生 |
| `min_snr_db` | 6 | 参考可信的最低 SNR | L0 |

### 4.2 三种来源

1. **`device_loopback`（硬件回采，Dot2 ch8）**
   - 与 mic 同一采集帧，`same_frame=true`，`fixed_delay_ms=0`；
   - **必须去混叠**：ch8 是 48 kHz 输出“每 3 个取 1 个”塞进 16 kHz 采集，9/12/15/20 kHz 会落到 7/4/1/4 kHz；粉噪实测 raw ch8 只有 **1 dB** 消除，用输出环重建后 **25.7 dB**（`dealias.go:11-31`）；
   - 设备侧 `dealias` 用 95-tap Blackman 低通（截止 7.2 kHz）重建参考（`dealias.go:74-94`），并且 `WriteFar` 在 `aecEnabled=false` 时仍然喂输出环（`aec.go:265-279`）；
   - 控制器拿到的是 16 kHz mono S16 的“干净参考”，不是原始 ch8。

2. **`vendor_tap`（厂商/客户端提供的回采，LX06 ch6/ch7）**
   - 与 mic 同一交织帧，`same_frame=true`；LX06 实测 2.4 ms lead（`aec.py:17-18`）；
   - 厂商链路是否已滤波取决于 client；LX06 现网 controller AEC 实测 25.2 dB（`aec.py:230-235`），可认为参考质量足够；
   - 如果厂商只给“处理后音频”而不给回采，则是 `source=none`，走 `device`。

3. **`none`（无参考）**
   - 控制器无法做 AEC；只能依赖设备 AEC（如果存在）；
   - `who` 只能是 `device` 或 `none`；`barge_in.mode` 只能是 `wake` 或 `none`。

### 4.3 参考“可用”不等于“可信”

这是本次 2ch 调试最重要的教训：`ref_active=true` 但 ERLE≈0，说明参考存在但不可信（通道错位/混叠/延迟），而不是“AEC 没调好”。（`findings.md:129`）

判定顺序：

| 观测 | 结论 | 动作 |
|---|---|---|
| `ref_active=false` 且正在播放 | 参考缺失 | 降级 |
| `ref_active=true` 但 `aec_erle_db < erle_floor_db` 持续 10 s | 参考不可信/错位/混叠 | 降级，标记原因 |
| `ref_rms_db < -60` 且正在播放 | 参考太弱/量化 | 检查 micGain/ref gain |
| `ref_locked=false` | dealias 未锁定，参考可能是 raw | 降级，提示 `dealias_unverified` |
| measured delay 与 `fixed_delay_ms` 差 >10 ms | 对齐错了 | 适配器整数样本平移，或降级 |

---

## 5. 控制侧参数

| 参数 | 默认 | 范围/取值 | 说明 |
|---|---|---|---|
| `controller.enabled` | `true`（开放类） | bool | 与 `who` 一致；`false` 时 rui 不建 `AecChain` |
| `controller.filter_len` | `2048` / `4096` | 256..16384 | taps @ `voice.rate`；公式见 §8.2 |
| `controller.frame_ms` | `20` | 10..40 | rui 的 block；speex 内部固定 128 样本（8 ms @16k） |
| `controller.delay_search_ms` | `[0, 0]` | `[min, max]` | **rui 当前没有延迟搜索**；`alignment=search` 必须拒绝或由适配器预对齐 |
| `controller.double_talk` | `dtd` | `dtd` / `freeze` / `half_duplex` / `none` | `dtd` = speex 内建双讲检测；`freeze` = 播放时冻结通道权重（rui 已做）；`half_duplex` 会杀掉 barge，不推荐 |
| `controller.freeze_weights` | `true` | bool | 对应 `aec.py:276` 的 `freeze=self.ref_active`；避免权重追着回声跑 |
| `controller.cancel` | `mix` | `mix` / `per_channel` | `mix` = 阵列混音一路（LX06 25.2 dB）；`per_channel` = 每麦一路（20.9 dB、6× CPU），只做诊断 |
| `controller.barge_in` | `true` | bool | 允许 wake 打断；acoustic 另见 §7 |
| `controller.erle_target_db` | `20` | 10..35 | 只统计 controller 这一级；`both` 时不是总目标 |
| `controller.erle_floor_db` | `6` | 0..15 | 低于它持续 10 s 触发降级 |
| `controller.hp_hz` | `90` | 60..150 | mic 与 ref 必须同截止（`aec.py:83-89`） |
| `controller.cpu_rtf_max` | `0.5` | 0.1..0.9 | 超过则降 filter_len 或降级 |

**不支持的能力要显式拒绝**：`alignment=search`、`half_duplex` + barge、`per_channel` + 单麦，都不应该“静默近似”。

---

## 6. 设备侧参数（开放设备可推）

来自 EchoMuse `device/internal/config/config.go` 与 `internal/aec/aec.go`：

| 模型字段 | 设备 JSON | 默认 | 范围/语义 | 备注 |
|---|---|---|---|---|
| `device.enabled` | `aecEnabled` | `true` | bool | 接管时推 `false`；`false` 仍保持 `WriteFar`/dealias 供参考上送 |
| `device.tail_ms` | `aecTailMs` | `300` | 50..500 | `hw` 参考下不生效（固定 64 ms）；`0` 无法表达（`>0` 才应用） |
| `device.delay_ms` | `aecDelayMs` | `0` | 0..1000 | `hw` 参考下不生效；指针类型，0 可表达 |
| `device.ref_source` | `aecRefSource` | `auto` | `auto` / `hw` / `sw` | 接管时钉 `hw`；AEC 关时设备上报 `off`（`RefSource()`），不能靠它判断参考来源 |
| `device.agc` | `agcEnabled` | `true` | bool/null | 只作用于 bounded `lockMic`；wake 流强制关；controller AEC 时建议 `false`；`null` = 厂商内部不可观测 |
| `device.barge_in` | `bargeInEnabled` | `false` | bool/null | 设备 OWW 的 barge；rui KWS 接管时可关；`null` = 厂商内部不可观测 |
| `device.barge_in_threshold` | `bargeInThreshold` | `0.05` | >0 / null | 同上；`owwOnDevice=off` 时无关 |
| `device.mic_gain_db` | `micGainDb` | `24`（设备默认） | 0..42 | 我们推 18，避免削波；量化余量见 §13 |
| `device.frame_ms` | — | `32` | 固定 512 样本 | 设备 AEC 的周期，不可配 |
| `device.params` | — | — | 可推字段列表 | 不含 `frame_ms` |
| `device.controllable` | — | — | bool | 厂商是否接受这些推送 |
| `device.observable` | — | — | bool | 是否能读到设备 AEC 状态/att |

**推送映射**（适配器 → 设备 `config` 消息）：

```json
{
  "type": "config",
  "aecEnabled": false,
  "aecTailMs": 64,
  "aecDelayMs": 0,
  "aecRefSource": "hw",
  "agcEnabled": false,
  "bargeInEnabled": false,
  "bargeInThreshold": 0.05,
  "micGainDb": 18
}
```

现状（2026-10-24 19:34，提交 `2bcda35`）：适配器已经能通过 `DOT2_AEC_TAIL_MS` / `DOT2_AEC_DELAY_MS` / `DOT2_AGC_ENABLED` / `DOT2_BARGE_IN_ENABLED` / `DOT2_BARGE_IN_THRESHOLD` 把上述字段推给设备（`adapter.py:144-170`、`device_link.py:302-311`）；`aecRefSource` 还没推（7/8），且**仍走环境变量，不是 manifest / rui API**。模型落地时应把这些 env 换成 `aec.device.*` 的正式来源，并让 rui 的 `devices[].aec` 能覆盖它们。设备侧已经支持这些字段（`config.go:281-346`），所以这是“来源改造”，不是“协议缺失”。

---

## 7. 两类默认策略与 barge 语义

### 7.1 默认策略

| 项 | 封闭类 `closed` | 半开放 `semi` | 开放类 `open` |
|---|---|---|---|
| `who` | `device` | `device`（可显式改 `controller`） | `controller` |
| `reference` | `none` | `vendor_tap`（同帧、可能有 2.4 ms lead） | `device_loopback`（同帧、0 ms、dealiased） |
| controller AEC | 关 | 默认关；显式开启需 `who=controller` | 开 |
| device AEC | 开（厂商） | 开（厂商） | 关（旁路） |
| `device.bypassable` | 通常 false | 看厂商 | true |
| `device.controllable` | 通常 false | 看 client | true |
| `barge_in.mode` | `wake` | `wake`；只有显式 `who=controller` 且参考可信时才可 `both` | `wake`（acoustic 待 rui 实现） |
| `fallback` | `[device, none]` | `[device, controller, none]` | `[controller, device, none]` |
| ERLE 目标 | 不可见 | controller 级 ≥20 | controller 级 ≥20 |

### 7.2 barge-in 要拆成两种

现有 rui 的 barge 路径是 **wake 触发**：KWS 命中就 flush + `stop_playback`，**不看 AEC**（`process.py:593-622`）。实测无 AEC 也能打断 4 次、服务端 0 ms（`aec-takeover.md:38-41`）。所以：

- `barge_in.mode=wake`：不需要参考，封闭类也能用；误触发靠 KWS 阈值和回声抑制（或设备 AEC）。
- `barge_in.mode=acoustic`：需要“用户说话”检测（VAD/ASR）在播放中可靠工作，**必须有 AEC**；rui 目前没有这条路径，模型中保留字段但标记 `unsupported`，不要对外承诺。
- `barge_in.mode=both`：wake 优先，acoustic 作为增强；同样受 I5 约束。

设备侧的 `bargeInThreshold` 是设备 OWW 的阈值；我们规定 `owwOnDevice=off`、`wakeWordEnabled=false`（`device_link.py:285-295`），所以它默认无关。不要把它和 rui KWS 阈值混为一谈。

---

## 8. 与 rui 现有配置的映射

### 8.1 字段映射

| rui 字段 | 现值/默认 | 由什么派生 | 公式/注意 |
|---|---|---|---|
| `audio.aec.enabled` | `true` | `effective.who ∈ {controller, both}` ∧ `controller.enabled` ∧ `ref.available` | 旧行为在全局开关打开后只按 `ref_channels<1` 关（`process.py:404-410`）；新行为必须再加 `who` 门控 |
| `audio.aec.filter_len` | `4096` | `controller.filter_len` 或 auto | 见 §8.2；clamp 256..16384 |
| `audio.aec.cancel` | `mix` | `controller.cancel` | `per_channel` 仅 `mic_channels>1`；单麦时 `mix` 就是一个滤波器 |
| `audio.capture.rate` | `48000` | manifest `capture.rate` | 平面不以它为准（`process.py:4-8`），doctor 用 |
| `audio.capture.channels` | `8` | `mic_channels + ref_channels` | 必须等于 manifest 的两者之和（`schema.py:173-180`） |
| `audio.capture.mic_channels` | `6` | manifest | 运行时以 `AudioFrame.mic_channels` 为准 |
| `audio.capture.ref_channels` | `2` | manifest | 运行时以 `AudioFrame.reference_channels` 为准 |
| `audio.capture.bits/shifts` | `32/12/16` | manifest | LX06 mic 2**23、ref 2**28，两个 shift 不能合并 |
| `audio.voice.frame_ms` | `20` | `controller.frame_ms` | 只影响 block；speex 内部 128 样本 |
| `audio.voice.barge_in` | `true` | `barge_in.mode != none` | wake 模式也应为 true |
| `audio.playback.target_silence_ms` | `300` | `barge_in.max_stop_ms` − 设备缓冲 | 设备 ALSA ~300 ms 另计，别把它算进服务端 |
| `devices[].aec` | 无 | L1 每设备覆盖 | **新增**；没有它就无法混合编队 |
| adapter `config` 推送 | `aecEnabled`/`micGainDb` + tail/delay/agc/barge（env） | `aec.device.*` | 已能推（`2bcda35`），但来源是 env；需改为 manifest/`devices[].aec` |
| `Metrics.aec_enabled` | `self.aec is not None` | 派生 | 建议改成 `effective.who` + `state` |
| `Metrics.aec_erle_db` | `AecChain.erle_db` | 派生 | 只在 `ref_active` 时有值（`aec.py:311-314`），且是 controller 级 |

### 8.2 派生公式

```text
# filter_len：taps @ voice.rate
room_tail_ms = 200        # 房间尾音预算；未知/大房间用 250
need_ms = reference.fixed_delay_ms + room_tail_ms
taps    = ceil(need_ms * voice.rate / 1000)
taps    = round_up_to_128(max(256, taps))
taps    = min(taps, 16384)
```

推荐值：

| 设备 | delay | tail | 计算 | 建议 |
|---|---|---|---|---|
| Dot2 | 0 ms | 64 ms（hw） | (0+64)×16 = 1024 | 2048（留 2× 房间余量） |
| LX06 | 2.4 ms | ~250 ms | (2.4+250)×16 ≈ 4038 | 4096（现网默认） |
| 未知 | — | — | — | 4096 + 标记 `needs_measurement` |

`cancel` 的选择：

- `mic_channels == 1` → `mix`（等价单滤波器，`per_channel` 没有意义）；
- `mic_channels > 1` → 默认 `mix`；`per_channel` 只在需要每通道 ERLE 诊断时开，且接受 6× CPU 与更差 ERLE（LX06 实测 25.2 vs 20.9，`aec.py:230-241`）。

### 8.3 迁移期必须保留的旧行为

**不要因为引入 `who=auto` 就把现网 LX06 从 controller AEC 切回 device AEC。** 现网 LX06 的 25.2 dB 是 controller AEC 测出来的；如果默认策略把它切走，是一次无声回退。

迁移规则：

1. **没有 `audio.aec` 块的设备**：完全走旧规则——`audio.aec.enabled=true` 且 `ref_channels>0` → controller AEC 开；否则关（`process.py:404-410`）。
2. **声明了 `audio.aec` 的设备**：走新策略；LX06 的 manifest 应显式写 `openness: semi`、`who: controller`（保留现网；前提是厂商 AEC 可关，否则用 `both`/`device`），而不是依赖 `closed` 默认。
3. `audio.aec.enabled=false` 仍是全局 kill switch：它关掉所有 controller AEC，但不改变各设备的 device AEC 状态。
4. 新策略的 `who=device` 只有在 manifest 明确声明、或封闭类无参考时才生效；不能仅凭 `class=A` 推断。

### 8.4 需要 rui 侧确认的现有语义

| # | 语义 | 现状 | 为什么要确认 |
|---|---|---|---|
| 1 | `filter_len` 的单位 | taps @ `voice.rate`（`schema.py:261`） | 避免有人按 ms 填 |
| 2 | `cancel=mix` 单麦 | 一个滤波器 | 确认不是“6 麦专用” |
| 3 | `capture.*` 是否只是期望 | 平面用帧（`process.py:4-8, 488-490`） | 混合编队时不能拿全局 capture 卡所有设备 |
| 4 | `ref_channels>0` 是否自动开 AEC | 是（`process.py:407`） | 新模型要改成 `who` 门控；否则 `who=device` + 有 ref 会被误开 |
| 5 | `aec_erle_db` 的作用域 | controller 级、`ref_active` 时才有值 | 不能拿它当“总 ERLE”；closed 类为 null |
| 6 | barge 是否依赖 AEC | wake 路径不依赖（`process.py:593-622`） | 不要把 wake barge 也关掉 |
| 7 | `AudioFrame.reference_channels` vs `DeviceInfo.ref_channels` | 前者是运行时真值，后者是 probe 声明 | 不一致时以帧为准并告警 |

---

## 9. rui 侧最小新增/确认字段（只提需求，不改代码）

### P0：没有这些，模型无法落地

| 字段/能力 | 位置 | 为什么 | 兼容性 |
|---|---|---|---|
| `DeviceManifest.audio.aec`（可选块） | `plugins/models.py`（`DeviceManifest` 当前 `extra="forbid"`，`models.py:304-318`） | 让插头声明 `openness/who/reference/constraints`；现在只有 `capabilities: list[Capability]`，无法表达“谁消” | 新字段可选；没有则走旧规则 |
| `DeviceConfig.aec`（每设备覆盖） | `config/schema.py:120-151` | `audio.aec` 是全局的，混合编队无法一台 controller、一台 device | 可选；缺省继承全局 |
| `DeviceInfo.features` + `firmware` | `device/contract.py:39-52` | 门控 `requires_feature`（`ref_uplink`/`dot2_hw_ref`），并在控制台显示固件 | 加默认空值，旧 adapter 不受影响 |
| `Metrics.aec_owner/aec_state/aec_degrade_reason/ref_source/ref_locked/self_hearing_turns/barge_stop_p50_ms` | `audio/protocol.py:103-150` | 没有这些，降级与自听无法观测；`ref_active` 单独会骗人（`findings.md:129`） | 加默认值，旧控制面忽略未知字段 |
| 配置校验谓词（I1–I8） | `config` 加载 | 拒绝 `controller` + 无参考、`both` + 无 cascade、`per_channel` + 单麦 | 硬错误 + 明确文案，不静默 |
| `_build_aec` 按 `who` 门控 | `audio/process.py:404-420` | 现在只看全局 `audio.aec.enabled` + `ref_channels`，不看 `who`；必须改成 `effective.who ∈ {controller,both}` | 旧配置映射为 `who=controller`（有 ref）保持行为 |

### P1：强烈建议

| 字段/能力 | 为什么 |
|---|---|
| 每设备 capture 期望 / doctor 按设备校验 | 混合编队下全局 `audio.capture` 会误报；平面已用帧，只有 doctor 需要改 |
| `GET/PUT /api/v1/devices/{id}/aec` + provenance | 运行时调参、A/B、故障定位；没有它只能改 config 重启 |
| `audio.aec.double_talk` / `delay_search_ms` | 如果 rui 要支持 `alignment=search`；不支持就显式拒绝，不要假装 |
| `device_aec_erle_db`（可选） | 设备能报 att 时，`both` 才有总 ERLE；不能报就显示 null |
| `audio.aec.enabled` 语义拆分 | 全局 kill switch 与每设备 `controller.enabled` 分开，避免“关全局”把 device AEC 也误解为关 |

### P2：未来

| 字段/能力 | 为什么 |
|---|---|
| `barge_in.mode=acoustic`（VAD 检测） | 真正的“说话即打断”；当前只有 wake 打断，不要对外承诺 |
| `POST .../aec/probe`（chirp/粉噪） | 自动测延迟与 ERLE，减少“靠猜” |
| `AecChain` 前置整数样本平移 | 适配器可做；如果 rui 做，能覆盖 `alignment=fixed` 的未知延迟 |
| `Capability.AEC_DEVICE` / `AEC_CONTROLLER` | 如果 `audio.aec` 块不被接受，作为退而求其次的粗粒度能力；不如块精确 |

### 需要 rui 侧明确答复的 8 个问题

1. `DeviceManifest` 是否接受新增 `audio` 块？`rui-device/v0` 是否升 v0.1，还是用 `protocol` 的 extra 字段过渡？
2. `devices[].aec` 是否进入 `DeviceConfig`？还是只允许 `params["aec"]`（无类型校验）？
3. `DeviceInfo` 能否加 `features`/`firmware`？adapter 的 `probe()` 是否允许返回固件 feature 集合？
4. `aec_erle_db` 是否明确为“controller 级、仅 ref_active 时有值”？closed 类显示 null 还是 N/A？
5. `ref_channels>0` 自动开 AEC 的旧行为，是否同意改为 `who` 门控（迁移期保留旧规则）？
6. `audio.capture` 是否降级为“期望值”，由 manifest/帧决定实际布局？混合编队时 doctor 怎么校验？
7. wake barge 是否确认不依赖 AEC？如果是，`barge_in.mode` 的 `wake` 不应被 `who=device/none` 关掉。
8. `both` 的 `cascade_allowed` 是否只允许 L0 打开？L1/L2 能否临时打开（用于测量）？

---

## 10. JSON 参数示例

> 下面是 manifest 的 `audio` 片段（放进 `rui-device/v0` 的机型 manifest，或 dot2 `plug.json` 的 `audio` 下）。
> `null` 表示“未知”，不是“0”；`who_allowed` 是硬白名单。
> 注意：dot2 现在的 `plug.json` 只有 `capabilities.aec: "device"` 这个标量（`plug.json:48`），和下面的结构不一致；落地时要改成 `audio.aec`，或让 `audio.aec` 覆盖标量。

### 10.1 开放类 Dot2：controller AEC + 2ch ref

```json
{
  "audio": {
    "capture": {
      "rate": 16000,
      "channels": 2,
      "mic_channels": 1,
      "ref_channels": 1,
      "frame_ms": 80,
      "format": "s16le",
      "bits": 16,
      "mic_gain_db": 18
    },
    "aec": {
      "openness": "open",
      "who": "controller",
      "who_allowed": ["controller", "device", "none"],
      "reference": {
        "source": "device_loopback",
        "channels": 1,
        "used_channels": 1,
        "same_frame": true,
        "rate": 16000,
        "format": "s16le",
        "bits": 16,
        "fixed_delay_ms": 0,
        "alignment": "frame_aligned",
        "dealiased": true,
        "requires_feature": "dot2_hw_ref",
        "availability": "while_playing",
        "active_rms_dbfs": -80,
        "min_snr_db": 6
      },
      "controller": {
        "enabled": true,
        "filter_len": 2048,
        "frame_ms": 20,
        "delay_search_ms": [0, 0],
        "double_talk": "dtd",
        "freeze_weights": true,
        "cancel": "mix",
        "barge_in": true,
        "erle_target_db": 20,
        "erle_floor_db": 6,
        "hp_hz": 90,
        "cpu_rtf_max": 0.5
      },
      "device": {
        "present": true,
        "bypassable": true,
        "controllable": true,
        "observable": true,
        "enabled": false,
        "tail_ms": 64,
        "delay_ms": 0,
        "ref_source": "hw",
        "agc": false,
        "barge_in": false,
        "barge_in_threshold": 0.05,
        "mic_gain_db": 18,
        "params": [
          "aecEnabled", "aecTailMs", "aecDelayMs", "aecRefSource",
          "agcEnabled", "bargeInEnabled", "bargeInThreshold", "micGainDb"
        ],
        "clamps": {
          "tail_ms": [50, 500],
          "delay_ms": [0, 1000],
          "frame_ms": 32
        }
      },
      "barge_in": {
        "mode": "wake",
        "max_stop_ms": 600
      },
      "constraints": {
        "mutual_exclusion": true,
        "cascade_allowed": false,
        "cascade_device_erle_db": 15,
        "ref_required_for": ["controller", "acoustic_barge"],
        "fallback": ["controller", "device", "none"],
        "sticky_ms": 30000
      }
    }
  }
}
```

要点：

- `who=controller` + `device.enabled=false` + `bypassable=true`：设备 AEC 可关，控制器接管；
- `agc=false`：避免 AGC 让回声路径非线性；
- `barge_in.mode=wake`：acoustic 还没实现，不要写 `both`；
- `who_allowed` 不含 `both`：Dot2 默认不允许级联；测量时如果确实需要 `both`，必须先由 L0 把 `both` 加入白名单并打开 `cascade_allowed`，**L2 不能越过白名单**。

### 10.2 封闭类 LX06：device AEC + 无参考

这是“厂商只给处理后单声道”的形态：控制器拿不到回采，只能信任设备 AEC。现网 open-xiaoai 的 8ch 变体见 §10.3。

```json
{
  "audio": {
    "capture": {
      "rate": 16000,
      "channels": 1,
      "mic_channels": 1,
      "ref_channels": 0,
      "frame_ms": 80,
      "format": "s16le",
      "bits": 16
    },
    "aec": {
      "openness": "closed",
      "who": "device",
      "who_allowed": ["device", "none"],
      "reference": {
        "source": "none",
        "channels": 0,
        "same_frame": false,
        "fixed_delay_ms": null,
        "alignment": "unknown",
        "dealiased": false
      },
      "controller": {
        "enabled": false,
        "filter_len": 4096,
        "frame_ms": 20,
        "cancel": "mix",
        "barge_in": false
      },
      "device": {
        "present": true,
        "bypassable": false,
        "controllable": false,
        "observable": false,
        "enabled": true,
        "tail_ms": 300,
        "delay_ms": 0,
        "ref_source": "auto",
        "agc": null,
        "barge_in": null,
        "barge_in_threshold": null,
        "clamps": {
          "tail_ms": [50, 500],
          "delay_ms": [0, 1000],
          "frame_ms": 32
        }
      },
      "barge_in": {
        "mode": "wake",
        "max_stop_ms": 600
      },
      "constraints": {
        "mutual_exclusion": true,
        "cascade_allowed": false,
        "ref_required_for": ["acoustic_barge"],
        "fallback": ["device", "none"],
        "sticky_ms": 30000
      }
    }
  }
}
```

要点：

- `reference.source=none`：控制器拿不到回采，只能信任设备 AEC；
- `device.bypassable=false`：即使以后拿到参考，也不能直接 `who=controller`，只能 `both`（如果 `cascade_allowed`）；
- `barge_in.mode=wake`：封闭类仍可“播放中喊唤醒词打断”，这是实测能力，不是 AEC 能力；
- `who_allowed` 不含 `controller`：除非厂商/ client 证明回采可用，否则不允许运行时切过去。

### 10.3 LX06 实测变体：厂商回采

现网 LX06 1.94.13 + open-xiaoai 实际能拿到 8ch（6 mic + 2 ref），并且 controller AEC 实测 25.2 dB。如果要把这个事实写进 manifest，按下面的 diff 改（其余同 10.2）：

```diff
 audio.aec:
-  openness: closed
-  who: device
-  who_allowed: [device, none]
+  openness: semi
+  who: controller            # 若厂商 AEC 可关
+  who_allowed: [controller, device, none]
   reference:
-    source: none
-    channels: 0
-    same_frame: false
-    fixed_delay_ms: null
-    alignment: unknown
-    dealiased: false
+    source: vendor_tap
+    channels: 2               # ch6/ch7 同源，控制器只取第一路
+    used_channels: 1
+    same_frame: true
+    fixed_delay_ms: 2.4
+    alignment: fixed
+    dealiased: true
+    availability: while_playing
   controller:
-    enabled: false
+    enabled: true
     filter_len: 4096
     cancel: mix
   device:
-    enabled: true
+    enabled: false
-    bypassable: false
+    bypassable: true          # 若关不掉，见下方“注意”
     controllable: false
     ref_source: auto
-    agc: null
+    agc: false
 audio.capture:
-  rate: 16000
-  channels: 1
-  mic_channels: 1
-  ref_channels: 0
+  rate: 48000
+  channels: 8
+  mic_channels: 6
+  ref_channels: 2
-  bits: 16
+  bits: 32
+  mic_shift: 12
+  ref_shift: 16
```

注意：如果厂商 AEC 关不掉（`bypassable=false`），`who=controller` 就变成了事实上的 `both`（级联），这违反 I6。这种“声明 controller、实际级联”的情况必须避免：

- 确认厂商 AEC 可关 → `who=controller` + `bypassable=true`；
- 关不掉 → 诚实写 `who=both` + `cascade_allowed=true` + `who_allowed` 加入 `both` + `device.agc=null/false`，并接受级联的 ERLE 预算。

**不要用一个布尔值掩盖物理事实。**

---

## 11. 校验与回退策略

### 11.1 校验谓词（伪代码，不是补丁）

```python
def validate(plan, info):
    hard, soft = [], []

    ref_ok = plan.reference.source != "none" and info.ref_channels > 0

    if plan.who == "controller":
        if not ref_ok:
            hard.append("controller needs a reference channel")
        if not plan.controller.enabled:
            hard.append("who=controller but controller.enabled=false")
        if plan.device.enabled:
            hard.append("who=controller requires device.enabled=false")
        if plan.device.present and not plan.device.bypassable:
            hard.append("device AEC cannot be bypassed; use both or device")
    elif plan.who == "both":
        if not plan.constraints.cascade_allowed:
            hard.append("both requires cascade_allowed=true")
        if not plan.device.enabled:
            hard.append("both requires device.enabled=true")
        if plan.reference.source not in ("device_loopback", "vendor_tap"):
            hard.append("both needs a pre-device-AEC reference")
        if plan.device.agc:
            hard.append("AGC makes the cascade echo path non-linear; set agc=false")
        if (plan.device.tail_ms or 0) > 500 or (plan.device.delay_ms or 0) > 1000:
            hard.append("device params out of clamp")
    elif plan.who == "device":
        if not plan.device.present:
            hard.append("who=device but no device AEC is present")
        if plan.controller.enabled:
            hard.append("who=device requires controller.enabled=false")
    elif plan.who == "none":
        if plan.controller.enabled or plan.device.enabled:
            hard.append("who=none but an AEC is enabled")

    if plan.barge_in.mode in ("acoustic", "both"):
        if plan.who not in ("controller", "both") or not ref_ok:
            hard.append("acoustic barge needs controller AEC + a reference")
    if plan.who == "controller" and plan.controller.cancel == "per_channel":
        if info.mic_channels < 2:
            soft.append("per_channel with one mic is pointless; use mix")
    if plan.controller.filter_len < required_taps(plan):   # required_taps：见 §8.2
        soft.append("filter_len shorter than delay+tail; echo tail may leak")
    if plan.reference.alignment == "search":
        hard.append("rui has no delay search; align in the adapter or use fixed")
    if not plan.who in plan.who_allowed:
        hard.append(f"who={plan.who} not in who_allowed={plan.who_allowed}")

    return hard, soft
```

处理：

- **hard**：拒绝应用 L1/L2 的该字段，退回上一级有效值；`aec_state=invalid`，`aec_degrade_reason=validation_failed`；绝不静默。
- **soft**：应用，但记录 warning；控制台显示。
- 配置加载期的 hard 错误应阻止启动该设备的音频面（或降级到 `device/none` 并明确告警），而不是让 plane 带病运行。

### 11.2 启动预检

```
1. 读 manifest -> 基本 schema 校验
2. 若设备在线：probe() -> DeviceInfo（ref_channels/features/firmware）
3. 比对 manifest 与 probe：
   - ref_channels 不一致 -> 以 probe 为准，告警
   - 缺 requires_feature -> reference 视为不可用
   - firmware 低于最低版本 -> 拒绝 controller 接管
4. 解析 who（§3.4）-> effective plan
5. validate（§11.1）-> hard 则降级，soft 则记录
6. 生成 rui config 派生值（§8.1）并启动音频面
7. 把 plan + provenance + validation 暴露到 GET /aec
```

### 11.3 运行时看门狗

每 2 s（与现有 metrics 同频）检查：

| 触发 | 条件 | 动作 |
|---|---|---|
| `ref_missing` | `ref_active=false` 且 `playback_s` 在增长，连续 3 个窗口 | 降一级 |
| `low_erle` | `aec_erle_db < erle_floor_db` 持续 10 s | 降一级 |
| `ref_silent` | `ref_rms_db < -60` 且正在播放 10 s | 降一级 |
| `dealias_lost` | `ref_locked=false` 持续 5 s | 降一级，原因 `dealias_unverified` |
| `self_hearing` | 10 个 turn 内自听 ≥2 | 降一级；同时保留 AEC，不建议直接关 |
| `cpu_over` | `rtf > cpu_rtf_max` 持续 30 s | 先降 `filter_len` 一档；仍超则降级 |
| `device_unreachable` | 设备 config 推不下去 | 标记 `device_actual=unknown`；不要假装已关 |

### 11.4 降级 / 恢复状态机

```
开放类：
  controller --ref_missing/low_erle/dealias_lost--> device --不可控/不可旁路--> none
       ^                                              |
       +----------- 60 s 稳定 + ERLE>=target ---------+

封闭类：
  device --设备 AEC 不可用/不可观测且自听严重--> none
       ^
       +----------- 人工确认 + 设备恢复 -----------+
```

规则：

- **只降不升的自动恢复要慢**：至少 60 s 稳定（`ref_active`、ERLE ≥ target、无自听），最多每 5 min 升一级；避免抖动。
- **粘滞**：每次切换后 `sticky_ms=30000` 内不再切；重复失败按 1/2/5/10 min 退避。
- **`both` 降级先丢 controller**，保留 device；不要反过来。
- **每次切换发事件**（`aec_state_changed`），带 `from/to/reason/at`，写日志 + metrics；控制台必须能看到“当前在哪一级”。
- **人工覆盖**：L2 可强制某状态，但要有 TTL 与 reason；到期回到自动策略。

### 11.5 降级矩阵

| 故障 | 开放类动作 | 封闭类动作 |
|---|---|---|
| 参考缺失 | controller → device | device 不变 |
| 参考混叠/低 ERLE | controller → device；标记 `dealias_unverified` | n/a |
| dealias 未锁定 | controller → device | n/a |
| 自听 turn 超阈值 | controller → device；不要直接关 AEC | device → none（若设备 AEC 也无效） |
| 设备 AEC 关不掉 | 只能 `both` 或 `device`，不能声明 `controller` | n/a |
| controller AEC 库缺失 | device | device |
| CPU 超预算 | 先降 `filter_len`，再降级 | n/a |
| barge 误触发 | 保留 AEC，调 KWS 阈值；不要关 AEC | 保留 wake barge，调阈值 |
| 设备不可达 | 保持 controller（如果参考还在），标记 `device_actual=unknown` | 标记 `device_actual=unknown`，不要假装 |

---

## 12. 验收指标

### 12.1 分类型目标

| 指标 | 封闭类（device AEC） | 开放类（controller AEC） | 测法 |
|---|---|---|---|
| controller ERLE P50 | n/a（不可见） | ≥20 dB（stretch 25） | 播放音乐/粉噪 10 min，取 `aec_erle_db` 的 P50 |
| 设备侧 ERLE（参考值） | n/a | 18.2 dB（Dot2 64 ms tail，`2bcda35` 提交信息） | 仅作对照；低于 20 dB 是 controller 接管的理由之一 |
| 总 ERLE（`both`） | n/a | ≥30 dB | 仅级联测量；设备侧 ERLE + controller 残差 |
| `ref_active` | n/a | 播放中 100% | 控制台 metrics |
| `ref_locked` | n/a | ≥95% 播放时间 | dealias 日志/健康字段（需新增） |
| 自听 turn | ≤2 / 50 | ≤1 / 50；gate 0/20 | `self_hearing_turns`（需新增） |
| wake barge 成功 | 20/20 | ≥18/20 | 播放中喊唤醒词 |
| 打断静音 P50 | ≤600 ms | ≤600 ms | 服务端 `barge_stop_ms` + 设备缓冲 ~300 ms |
| 误唤醒 | ≤2 / 8 h | ≤2 / 8 h | 静音/音乐/电视背景 |
| 上行带宽 | ≤32 KB/s | ≤70 KB/s | 设备/适配器统计 |
| CPU | n/a（设备） | AEC rtf ≤0.5 | `doctor`/`aec_rtf` |
| ASR 退化 | ≤2% 相对基线 | ≤2% | 同一组 20 句 |

> 注：20.3 dB 是早期 bench 数据（16/300 ms tail，`aec-takeover.md:12`）；18.2 dB 是 64 ms tail 的现网数据（`2bcda35` 提交信息）。两者设置不同，不要直接比较。

**判定口径**：

- 开放类必须先过“参考可信”（`ref_active` + `ref_locked` + ERLE ≥ 20），再谈自听；ERLE 0.5–9 dB 的现状（`handoff-morning.md:55`）不算通过。
- 封闭类没有 controller ERLE，不能拿 `aec_erle_db=null` 当“通过”；用自听 turn 与 barge 成功率作为代理指标。
- 所有指标都要记录**当前 `who`**；否则“ERLE 差”可能只是跑在 `device` 上。

### 12.2 可观测字段

| 字段 | 类型 | 来源 | 为什么需要 |
|---|---|---|---|
| `aec_owner` | `device/controller/both/none` | effective plan | 回答“现在谁在消” |
| `aec_state` | `active/degraded/disabled/invalid` | 状态机 | 不是布尔值能表达的 |
| `aec_degrade_reason` | enum | 状态机 | 故障定位 |
| `ref_source` | `device_loopback/vendor_tap/none` | manifest + probe | 参考从哪来 |
| `ref_active` | bool | `AecChain`（已有） | 参考是否有能量 |
| `ref_locked` | bool | dealias/适配器（新增） | 参考是否可信；`ref_active` 单独会骗人 |
| `ref_delay_ms` | float | 声明/测量（新增） | 对齐诊断 |
| `ref_rms_db` | float | 已有 | 参考电平/量化 |
| `aec_erle_db` | float/null | 已有 | controller 级效果 |
| `aec_erle_p50_db` | float | 新增 | 稳态效果，避免瞬时值 |
| `self_hearing_turns` | int | `SpokenMemory` 计数（新增） | 自听是最终用户指标 |
| `barge_count` | int | 已有 | 打断次数 |
| `barge_stop_p50_ms` | float | 日志聚合（新增） | 打断延迟 |
| `device_aec_enabled` | bool | 适配器/config ack（新增） | 设备实际状态，不是请求值 |
| `device_agc_enabled` | bool | 同上 | 级联诊断 |
| `controller_filter_len` | int | effective plan | 复现问题 |
| `plan_provenance` | map | 新增 | 每个字段来自 manifest/config/runtime |

### 12.3 测量方法

1. **ERLE**：播放 10 min 音乐/粉噪，采样 `aec_erle_db`，取 P50/P10；同时记录 `ref_active/ref_locked`。
2. **自听**：20 轮问答，统计“ASR 文本 = 最近 TTS 文本（10 s 窗口）”的 turn；rui 的 `SpokenMemory` 已有判定逻辑（`session/rules.py:316+`），只需加计数。
3. **barge**：播放长 TTS/音乐，20 次喊唤醒词，记录成功次数与 `barge_stop_ms`；设备缓冲另计 ~300 ms。
4. **误唤醒**：8 h 背景音（音乐/电视/安静），统计误触发。
5. **参考可信**：对比 raw ch8 与 dealias 后的 ERLE；如果只有 1 dB 量级，说明 dealias 没生效（`dealias.go:11-31`）。
6. **级联**：先测 device-only ERLE，再测 both 的总 ERLE；如果总 ERLE 没有提升，关掉级联。

---

## 13. 风险与开放问题

1. **16-bit 量化天花板（Dot2）**：Dot2 的采集是 S24；设备内部 AEC/波束链路与上行 S16 之间存在量化差（`aec-takeover.md:93` 的口径）。设备记录语音 RMS 0.0001–0.0006 FS（16-bit 只有 3–20 LSB），靠 `micGainDb` 预抬升。无论具体在哪一级量化，controller 侧只看到 S16，ERLE 上限要实测；不够就退回设备 AEC。
2. **dealias 锁定不是必然**：`clean()` 在匹配失败时回退 raw ch8（`dealias.go:183-252`）；raw 参考只有 ~1 dB。必须有 `ref_locked` 观测，否则会把“参考坏了”误判成“AEC 参数不对”。
3. **`ref_active=true` 会骗人**：2ch 序号 bug 时 `ref_active=true`、ERLE≈0、自听（`findings.md:129`）。验收必须同时看 ERLE + `ref_locked` + 自听。
4. **`both` 的物理风险**：AGC/非线性/过抑制；默认关闭，设备侧 ERLE ≤15 dB，且必须 `agc=false`。设备没有 ERLE 目标参数，这个预算只能靠 tail/开关间接逼近；做不到就不要开 `both`（见 §3.3）。
5. **rui 没有延迟搜索**：`alignment=search` 目前不可实现；要么适配器做整数样本平移，要么拒绝。不要写“支持 search”的承诺。
6. **每设备 vs 全局配置**：这是最大的结构缺口；没有 `devices[].aec`，混合编队只能靠“有没有 ref”隐式分流。
7. **LX06 厂商 AEC 是否可关未知**：`bypassable` 必须由 probe 证实，不能假设；关不掉就不能声明 `who=controller`。
8. **`aec_erle_db` 不是总 ERLE**：它只统计 controller 这一级；`both` 时用户听到的是两级之和，但日志只给一级。
9. **acoustic barge 未实现**：当前只有 wake 打断；`barge_in.mode` 里保留 `acoustic` 但标 `unsupported`，不要对外承诺。
10. **运行时改 `who` 可能需要重启音频面**：`filter_len`/`enabled` 属于 `restart` 效果（`catalog.py:638-650`）；API 必须返回 `restart_required`，不能假装热生效。
11. **`plug.json` 与 rui `rui-device/v0` 字段不一致**：dot2 的 `capabilities.aec` 是标量，rui 的 v0 manifest 是 `capabilities` 列表 + `params`，且 `extra="forbid"`。AEC 模型落地前需要一次字段对齐/迁移（把 `plug.json` 映射成 `rui-device/v0`，或给 rui 加 `audio` 块）。
12. **权限等级 ≠ 开放度**：`class=A/B` 是安装/root 权限；`openness=closed/semi/open` 是音频控制权。两者不能互相推断。

---

## 14. 证据索引（本次只读核对）

### rui 侧

| 文件 | 关键行 | 内容 |
|---|---|---|
| `src/house_ear/config/schema.py` | 120-151, 154-180, 183-200, 259-266, 308-312 | `DeviceConfig`（含 `params`）、`CaptureConfig`、`VoiceConfig.barge_in`、`AecConfig`、`AudioConfig` 全局 |
| `src/house_ear/audio/aec.py` | 1-19, 83-89, 126-187, 208-314 | speexdsp MDF、90 Hz 高通、`ref_active`、ERLE、`mix`/`per_channel`、2.4 ms lead |
| `src/house_ear/audio/process.py` | 4-8, 404-420, 474-499, 587-622, 1516-1525 | 平面以帧为准、`_build_aec` 只看全局开关 + `ref_channels`、只取第一路参考、barge 路径、metrics |
| `src/house_ear/device/contract.py` | 23-31, 39-59, 62-95 | `Capability.REFERENCE`、`DeviceInfo`、`AudioFrame.reference_channels` |
| `src/house_ear/audio/protocol.py` | 90-150, 167-190 | `Ready`/`Metrics`/`Wake` 字段 |
| `src/house_ear/plugins/models.py` | 94-118, 304-318 | `ProtocolSpec` extra allow；`DeviceManifest` extra forbid + `capabilities: list[Capability]` |
| `src/house_ear/plugins/builtin.py` | 25-90 | LX06 内置 manifest（A 类、reference、inbound） |
| `docs/socket-plug.md` | 64-132, 169-195 | `rui-device/v0` 字段、A/B 类最小清单 |
| `docs/adapter-contract.md` | 20-70 | `Capability` 语义、tiers（tier 1 无参考、tier 2 有回采） |
| `docs/remaining-work.md` | 89-132 | A/B 类定义、插座/插头方向 |

### Dot2 插头侧

| 文件 | 关键行 | 内容 |
|---|---|---|
| `plug.json` | 17-26, 44-49, 64-70 | capture 1ch/0ref、`capabilities.aec="device"`、limits |
| `docs/plug-socket-contract.md` | 27-37, 41-65, 73-76 | 边界归属、盲开 manifest、优先级 |
| `docs/aec-takeover.md` | 12-18, 28-31, 38-42, 60-65, 73-77, 93, 113-121 | Dot2 20.3 dB、ch8 混叠、barge 实测、方案 B、验收口径 |
| `docs/p3-firmware-patch.md` | 44-58, 69-101, 114-118, 160, 187-190 | `RefForUplink`、2ch 成帧、`aecEnabled=false`、假设备 0.1 dB、验收 |
| `docs/findings.md` | 129 | 2ch 序号错位 → `ref_active=true`、ERLE≈0、自听 |
| `docs/handoff-morning.md` | 55 | 现网 ERLE 0.5–9 dB、自听未消失 |
| `src/rui_dot2_bridge/adapter.py` | 144-170, 561-619 | 设备侧 DSP 环境变量、`DeviceInfo`/`REFERENCE`、manifest 读取 |
| `src/rui_dot2_bridge/device_link.py` | 302-311 | config 推送：`aecEnabled`/`micGainDb`/`aecTailMs`/`aecDelayMs`/`agcEnabled`/`bargeIn*` |

### 设备侧（只读 EchoMuse 源码）

| 文件 | 关键行 | 内容 |
|---|---|---|
| `device/internal/aec/aec.go` | 48-82, 204-242, 252-299, 310-354 | `FrameSize=512`、`hwTailMs=64`、`SetHardwareRef`、`RefForUplink`、`RefSource`、`SetParams`（hw 时忽略 delay/tail） |
| `device/internal/aec/dealias.go` | 11-31, 55-72, 183-252 | ch8 混叠、95-tap 低通、`clean()` 回退 raw |
| `device/internal/config/config.go` | 117-155, 237-243, 281-346, 583-597, 627-639 | `AgcEnabled`、`AecEnabled/AecDelayMs/AecTailMs/AecRefSource`、默认值、wire 字段、`MicGainDb` clamp |

---

## 15. 一句话给 rui

**不要把“谁消回声”做成设备的品牌判断，也不要只做成一个布尔值；把它做成 `who` + `reference` + `constraints` 的三元组，并允许每设备覆盖。** 这样 LX06（closed/device）和 Dot2（open/controller）走同一套代码，新增一台音箱只需要写 manifest，不需要改 rui。
