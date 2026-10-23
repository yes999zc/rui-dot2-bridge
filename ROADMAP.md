# Roadmap — Dot 2 → rui 卫星

**目标**：把 Dot 2 变成 rui 的一个可用语音卫星——唤醒、对话、打断，全链路走 rui，EchoMuse controller 退场。
**不在本路线图内**：rui 仓库的任何改动、设备重刷/重配网、emOS 切换、多房间、音乐/多设备。

**总工期估计**：到"日常可用"约 **1.5–2 周**（Phase 3 是条件项，另算 1–1.5 周）。以 9 哥的实际投入为准，不含 rui 自身的模型/TTS 调试。

## 里程碑总览

| 阶段 | 目标 | 工期 | 产出 | 决策点 |
|---|---|---|---|---|
| P0 | 协议探针 | 0.5–1 d | 实测抓包 + 回滚演练 | 协议子集是否够用 |
| P1 | adapter v0（单声道） | 3–4 d | 能对话的 Dot 2 | 端到端是否通 |
| P2 | 唤醒 / 打断标定 | 3–5 d | 指标报告 | **是否必须上 9 声道** |
| P3 | 多声道升级（条件） | 1–1.5 w | 改固件 + 参考通道 | 设备内存/带宽是否可接受 |
| P4 | 常驻与运维 | 2–3 d | 常驻服务 + 回滚演练 | 是否交给 rui |
| P5 | 收尾交接 | 持续 | 文档 + 可选上游提案 | — |

依赖：P1 ← P0；P2 ← P1；P3 ← P2 的 gate；P4 ← P2/P3。

---

## P0 — 协议探针与快照（0.5–1 天）

**做什么**

1. 用移植的证书起一个最小的 wss 探针（`/control` + `/data`），只做 `register → ack`，把设备真实发出的每条消息落盘。
2. 抓三件事的实测值：`register` 的完整字段与 `capabilities`、`/data` 帧的到达节奏与大小、`mic_start(lock_mic:false)` 后是否真的连续送 PCM。
3. 写 `controller.json` 把设备指向探针，确认 30 s 内重拨成功；然后删文件、确认能落回旧 controller。

**交付**：`docs/trace/` 下的原始消息记录 + `docs/findings.md` 第四节"待核实"逐条填上答案。

**验收**

- 能连续收到 ≥ 60 s 的 `0x01` 帧，帧长与到达间隔记录在案；
- 能从探针发一段 `0x02` 测试音，Dot 出声；
- 旧 controller 恢复后设备自动重连，回滚 < 2 分钟。

**风险**：设备一旦有 controller 就不会同时连第二个；探针上线前先停旧 controller 或把它放在 endpoints 第二位。

---

## P1 — adapter v0：单声道跑通（3–4 天）

**做什么**

在 `src/rui_dot2_bridge/adapter.py` 实现 `DeviceAdapter`：

- `open()`：起 wss（8870）服务端，TLS 用 `secrets/server.pem|server.key`，校验 `X-EM-Token`，等 `register` 回 `ack`，下发 `mic_start(lock_mic:false)`；
- `frames()`：`0x01` → 重切 20 ms → `AudioFrame(channels=1, rate=16000, reference_channels=0)`；
- `play()`：`AudioFrame` 反向，`0x02` 切片发送；流结束补 `0x03`；
- `stop_playback()`：发 `speaker_flush`，清缓冲；
- `probe()`：`DeviceInfo(adapter="dot2", model="Echo Dot 2", capabilities={PLAYBACK, LED, VOLUME})`——**不宣告 `REFERENCE`/`MIC_ARRAY`，因为确实没有**；
- `set_volume()` / `set_led()`：分别映射 `volume_set` / `leds`（没有就先留空实现，但 capability 要诚实）。

配套：`sitecustomize.py` 注册、`secrets/` 校验、rui 的 `config.yaml` 覆盖（`playback.rate=48000`、`capture` 1ch/0ref）。

**交付**：一条完整对话链路；`tests/` 里的帧编解码单测（tracer 录制回放，不接硬件）。

**验收**

- 说唤醒词 → rui 出 ASR 文本 → TTS 从 Dot 播放；
- 连续 50 轮无崩溃、无静默丢帧；
- 端到端（说完 → 首字出声）P50 ≤ 3 s，需与 rui 单独跑 LX06/文本通道的基线对比，超出部分归因；
- 播放中不自激（没有"自己听到自己"的循环）；
- rui 控制台能看到设备在线，能力列表如实显示（无参考通道、无麦克风阵列）。

**注意**

- rui 在 `ref_channels < 1` 时会明确关掉 AEC/barge-in（`audio/process.py:377`），这不是 bug；P2 再评估影响。
- 设备自带唤醒先设 `owwOnDevice: off`，避免和 rui 的 KWS 双重触发。

---

## P2 — 唤醒与打断标定（3–5 天）

**做什么**

1. 标定 rui 的 KWS 在 Dot 2 单声道流上的阈值与唤醒词模型（连续流 80→20 ms 切块后的实际表现）。
2. 测播放中唤醒：设备侧 AEC 已经把回声压掉，**单声道是否足够支撑 barge-in**，这是本阶段的唯一关键问题。
3. 如果不达标，量化差多少（误唤醒率、真唤醒率、打断延迟），作为 P3 的决策依据。

**交付**：`docs/measurements.md`，含每组 ≥ 20 次的统计与原始日志。

**验收（目标值，可按实测微调）**

| 指标 | 目标 |
|---|---|
| 安静房间唤醒率 | ≥ 19/20 |
| 电视/音乐背景唤醒率 | ≥ 17/20 |
| 8 小时误唤醒 | ≤ 2 次 |
| 打断到静音延迟 | P50 < 600 ms |
| 播放中问句自激 | 0 次 / 50 轮 |

**Gate**：以上任一项显著不达标，且确认是缺少参考通道导致 → 进入 P3；否则跳过 P3，直接 P4。

---

## P3 — rui 接管 AEC（条件触发，1–1.5 周）

**只在上一个 Gate 判定"单声道不够"时做。** 完整分析见 [`docs/aec-takeover.md`](docs/aec-takeover.md)。

**做什么**

1. Fork EchoMuse 设备端（Go），开一个 ack 协商的能力 `dot2_hw_ref`：
   上行从 1ch 改成 **2ch = 波束 mono（预 AEC）+ 去混叠后的 ch8 参考**（仍是 `0x01` 帧，旧 controller 仍收单声道）；
   同时保持参考抽取链路常开（关掉设备 AEC 后 `WriteFar` 要照跑，否则 `dealias` 无输出环可用）。
2. 通过现有 `config` 推送 `aecEnabled:false`，让设备不再做 7 麦 speex（省单核 19%）。
3. 用上游 `device/compile.sh` + `compiler/Dockerfile` 交叉编译，`adb push` 替换 `/data/local/bin/server`（**不需要重新跑配置向导**）。
4. adapter 把上行映射成 `AudioFrame(channels=2, mic_channels=1, reference_channels=1)`，宣告 `REFERENCE`；
   rui 的 AEC 自动启用（`aec.enabled=true, cancel=mix`），控制台看 `aec_erle_db`。
5. 下行保持 48 kHz 不变。

**关键坑（已确认，别踩）**：ch8 是 48 kHz 输出每 3 取 1 塞进 16 kHz 采集、**没有抗混叠**。
raw ch8 送进 AEC 实测只有 1 dB 消除，用输出重建参考后 25.7 dB。所以参考必须走 `dealias` 重建，不能直接转发。

**验收**

- rui 控制台 `aec_erle_db` ≥ 20 dB（与设备 AEC 基线同量级）；
- 播放中 barge-in 可用，误唤醒不高于 P2 水平；打断静音 P50 ≤ 600 ms；
- 上行带宽实测 ≤ 70 KB/s，Dot 2 可用内存 > 100 MB、CPU 不高于改动前；
- 设备断电重启后自动回到 adapter；
- 关掉 `dot2_hw_ref` 协商即回退到单声道 + 设备 AEC（无需重刷）。

**风险**：固件改动是**唯一会碰设备的阶段**；也意味着从"用上游发布版"变成"维护一个 fork"。
先在离线路由用离线抓包验证 `dealias` + speex 的可达 ERLE，再上设备；旧 `server` 二进制留档，`adb push` 秒级回滚；不要在同一次改动里做 emOS 切换。

**8ch（7 麦 + 参考）只作为实验**：rui 的 per-channel 拓扑实测 20.9 dB，低于 mix 的 25.2 dB，且带宽 4 倍。除非要测天线/波束算法，不排期。

---

## P4 — 常驻与运维（2–3 天）

**做什么**

- adapter 常驻：跟随 rui 容器（`network_mode: host`）即可，无需额外进程；确认 rui 的 plane watchdog 会拉起崩溃的 adapter。
- 证书与回滚资产：`secrets/` 加密备份 + 记录恢复步骤；`controller.json` 双 endpoint 自动回滚实测。
- 日志：adapter 起停/握手/重拨/丢弃帧计数落到 rui 的 service.log，能一眼看出"设备掉线还是 rui 挂了"。
- 24 小时 soak：记录重连次数、RTT 抖动（旧链路有 `21 idle excursions in 11m, worst 5372ms` 的记录，要作为基线对照）。

**验收**

- 24 h 无进程崩溃、无内存单调增长；
- 拔电重启 Dot 2，≤ 60 s 内恢复可对话；
- 回滚演练一次（改 `controller.json` → 旧 controller）< 2 分钟；
- 断网 5 分钟后恢复，设备自动重连。

---

## P5 — 收尾与交接（持续）

- 补齐 README/部署文档，让"换一台同型号 Dot"能照做；
- 可选：把 adapter 作为 rui 的一个模块提案给上游（**这是 rui 仓库的活，需 9 哥单独决定**，本路线图不排期）；
- 把「设备侧前置工作」的最终状态回写到 `hermes-dot2-voice`。

---

## 风险表

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 单声道不足以支撑打断 | 中 | 高（barge-in 不可用） | P2 提前量化，P3 兜底 |
| 设备 TLS 证书丢失/过期 | 低 | 高（要重新配网刷机） | `secrets/` 双备份，P4 前完成 |
| 两套唤醒（设备 + rui）互相打架 | 中 | 中 | 先关设备侧唤醒；只保留 rui KWS |
| 设备 Wi-Fi 抖动导致断流 | 中 | 中 | 80 ms 块 + rui 的"断线不断面"设计；P4 记录重连率 |
| rui 的 provider（ASR/TTS/LLM）配置不当被误判为链路问题 | 中 | 中 | P1 前先用 rui 的文本/噪声通道验证 provider 链 |
| 改 rui 的诱惑 | 中 | 高（违反约束） | adapter 只走 `PYTHONPATH` + `register()`；需要改动 rui 时先停下来报 9 哥 |

## 与旧路线图的关系

`hermes-dot2-voice` 的 P2（自研 controller 接 Hermes）、P3（唤醒）、P4（emOS）在本项目里被重新定义：
- P2 → 本项目的 P1（但目标从 Hermes 换成 rui，形态从"controller 服务"改成"rui adapter"）；
- P3 → 本项目的 P2；
- P4（emOS）→ 暂不排期，只在 Phase 3 需要更多内存/线路输出时再考虑。

## 立即可以做的三件事

1. 确认 rui 部署在哪台机器、控制台 token、当前 provider 链是否可用（P1 的前置）。
2. 备份 `data/tls/` 三件套（丢了要重刷机）。
3. 跑 P0 探针，把 `docs/findings.md` 的"待核实"填掉——**在写 adapter 之前**。
