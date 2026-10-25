# Roadmap v2 — Dot 2 → rui 卫星

> 版本：v2（2026-10-24 02:30）｜上一版：v1（2026-10-24 02:10）
> 变化见文末「§9 v1 → v2 变更」。目标仓库：[rui-dot2-bridge](https://github.com/yes999zc/rui-dot2-bridge)（私有）

## 当前状态（2026-10-25 · v3）

> 本节覆盖下面的历史计划；阶段编号沿用 v2，但状态以本节为准。完整交代见
> [`docs/handoff-2026-10-25.md`](docs/handoff-2026-10-25.md)。

| 项 | 状态 |
|---|---|
| P0 协议探针 | ✅ 完成（register/ack、帧率、回滚、抓包） |
| P1 adapter v0（单声道） | ✅ 真机跑通：唤醒→ASR→LLM→TTS、LED、音量、响度、EOS/flush、回滚 |
| P2 唤醒/打断 | 🟡 功能可用：真机 barge 已触发（服务端停推 3–7 ms）；严格 20 次唤醒率/打断延迟仍待真人标定 |
| P3 固件补丁（2ch 参考） | ✅ **已发布并安装**：独立仓库 `echomuse-dot2-ref` v0.1.0；设备跑 `20261024-1911-dev`（server_b），server_a 保留旧固件可回滚 |
| AEC 归属 | ✅ 结论：设备 AEC 稳态 14–18 dB 是该声学链路上限；生产选 **ref_channels=0 + 设备 AEC**，rui AEC 不再必需；2ch 保留但不启用 |
| P4 生产化 | 🔄 测试实例已在另一台服务器用 **Docker** 部署 rui **v0.2.2**（8080 控制台，healthy），但 **Dot2 接入被协议变更阻塞** |
| 误唤醒 | ✅ 基线：20 次文件播放（三档音量 / wakeMic 0、6）**0 误唤醒、0 空唤醒**；此前"自听"是 `debug/say` 把输入记成 turn 的误判 |

**当前阻塞**：rui 0.2.2 改成单入口 4412（`ws//?token=`），且声明式 plug 不执行 entrypoint、一个音频面只驱动一台设备；
EchoMuse 需要 `wss + X-EM-Token`。已提 [rui#7](https://github.com/yes999zc/rui/issues/7) 问契约（多卫星共存、out-of-tree adapter、
AEC/wake 归属、端口、响度）。**在 rui 回复并定稿 plug/接口文档前，桥代码不迁移。**

**下一步**：① 等 rui 答复/文档；② 桥迁移到 0.2.x 契约（端口保持 8870，LX06 共存，唤醒仲裁归 rui）；
③ 以 Docker 方式冷部署（纯用户视角智能体先跑、Lead 再配参数）；④ 端到端回归（唤醒/打断/自听/回滚）。

## 0. 一页纸

| 项 | 内容 |
|---|---|
| **目标** | 说「你好小瑞」→ rui 出答案 → Dot 2 出声；播放中能打断；断线/断电自恢复 |
| **不做什么** | 不改 rui 代码、不重刷设备（P3 前）、不切 emOS、不做多房间/音乐、不碰 LX06 链路 |
| **成功定义（DoD）** | ① 日常对话可用 ② 打断 P50 ≤ 600 ms ③ 24 h 不崩 ④ 回滚旧链路 < 2 min ⑤ 换一台同型号能照做 |
| **总投入** | P0–P2 + P4 约 **9–12 个工作日**；P3（条件项）另加 6–8 天 |
| **技术形态** | rui 侧 adapter 顶替 EchoMuse controller（[ADR-0001](docs/adr-0001-adapter-as-controller.md)），设备零改动 |
| **决定要不要动固件的那一步** | P2 的 Gate G1（单声道下 barge-in 差多少） |

**关键路径**：`P0 → P1 → P2 → G1 →（P3 或直接）P4 → P5`
**可并行**：P0 的 NAS 挂载准备 ↔ 探针；P3 的离线 `dealias` 验证 ↔ P2 标定。

## 1. 前提与约束（2026-10-24 已核实）

| 事实 | 值 / 位置 | 对计划的影响 |
|---|---|---|
| rui 生产实例 | NAS `192.168.5.168:8080`，v0.0.1，1 台 LX06 在线，`/health` 正常，`:4412` 在听 | adapter 最终要挂进 NAS 上的 rui 容器（x86_64） |
| rui 代码状态 | v1 冻结（只做四道发布门槛）；本机 checkout `01_Code/projects/house-ear` @ `e6b1db5` | **本方案零改 rui**，冻结不影响 |
| NAS 镜像版本 | 仍是上一版（本轮 UI 更新未部署，等验收日统一） | P4 必须排在 rui 验收日之后 |
| 设备 | Dot 2 已解锁 + EchoMuse v2.18.1，已 approve，TLS 证书已下发 | 不需要重刷/重配网 |
| 设备发现 | 静态 `controller.json`（有序 endpoints，`mdns:false`） | 不需要 mDNS 常驻进程；天然支持自动回滚 |
| PCM 格式 | 上行 `0x01` S16 mono 16 kHz/80 ms；下行 `0x02` S16 mono **48 kHz**/4096 B | rui `playback.rate=48000`，adapter 不做 DSP |
| 旧 controller | Mac `192.168.5.18:8767/8770`，仍可用 | 保留为回滚路径，证书三件套必须备份 |
| rui 的 B 类标准 | `docs/remaining-work.md` §I 已把 dot2 列为 B 类（高权限）样板（未排期） | 本项目产出可直接作为该标准的输入 |

## 2. 阶段计划

### P0 — 协议探针与素材备份（0.5–1 天｜目标 10-25 完成）

**交付**
- 最小 wss 探针（`/control` + `/data`，互相移植的 `server.pem/key`，校验 `X-EM-Token`），只做 `register → ack`；
- `docs/trace/` 抓包：完整 `register` 字段、`capabilities`、`0x01` 到达节奏与帧长；
- `secrets/` 三件套（`ca.pem/server.pem/server.key`）落盘 + 异地备份；
- `controller.json` 双 endpoint 写入设备，回滚演练记录。

**验收（✅ 2026-10-24 已实测，见 [docs/p0-p1-report.md](docs/p0-p1-report.md)）**
- [x] 连续 ≥ 60 s 收到 `0x01`：12.5 帧/s、80 ms/块、≈31 KB/s、RTT 5.4–7.8 ms
- [x] 从探针发 `0x02` 测试音，Dot 出声（设备日志 `voice stream complete periods=53 underruns=0`）
- [x] 回滚：静态 endpoints[1] 旧 controller 生效；设备按序尝试
- [x] `findings.md` §四「待核实」有答案（`listen_state=stream`、27 项能力、音量 85→38）

**风险**：设备同时只连一个 controller → 探针上线前停旧 controller，或把旧 controller 放 endpoints 第二位。

---

### P1 — adapter v0（单声道，3–4 天｜目标 10-28）

**交付**
- `src/rui_dot2_bridge/adapter.py` 真实现：`open/close/frames/play/stop_playback/probe`；
- 握手状态机（`register→ack→mic_start`）、`X-EM-Token` 校验、断线重拨、ping 保活；
- `AudioFrame(channels=1, rate=16000, reference_channels=0)`，20 ms 切块；
- `sitecustomize.py` 注册 + rui `config.yaml` 覆盖（`playback.rate=48000`、`capture 1ch/0ref`）；
- 单测：帧编解码 + 状态机（录制回放，不接硬件）。

**验收（部分已完成，2026-10-24）**
- [x] TTS 从 Dot 播放：`periods=85 underruns=0 primeWait=13ms recvSpan=3341ms maxGap=123ms`
- [x] 声学回路：30% 音量回放 → 麦克风录音 → `/api/v1/asr` 转出「你好，小瑞。」「小瑞，现在几点？」
- [x] rui 控制台在线：`audio_plane=true`、`speaker_connected=true`，能力如实（无 REFERENCE/MIC_ARRAY）
- [ ] 唤醒 → ASR → 回合：KWS 需真人（自测音量低约 20 dB，见 P2）
- [ ] 连续 50 轮 / 打断指标：等 P2 真人值守时一起做
- [x] 端到端（文本回合）：llm_first 437 ms、tts_first 940 ms、play_start 940 ms

**依赖**：rui 的 ASR/LLM/TTS provider 已可用（NAS 上已验证；本机用 checkout 起 dev 实例，`:8091`）。

---

### P2 — 唤醒与打断标定（3–5 天｜目标 11-02）→ **Gate G1**

**测试矩阵**（每组 ≥ 20 次，原始日志进 `docs/trace/`）

| 场景 | 变量 | 记录 |
|---|---|---|
| 安静房间 | 50 / 80 音量 | 唤醒率、误唤醒 |
| 电视/音乐背景 | 80 音量放歌 | 唤醒率、KWS 阈值、自激 |
| 播放中打断 | 回答播放时说唤醒词 | 打断到静音延迟、丢字 |
| 长稳定 | 8 h 待机 | 误唤醒次数、重连次数 |
| 延迟 | 20 轮 | 首字延迟、端到端、总时长 |

**交付**：`docs/measurements.md`（含每组 n、中位数、p90、失败样本）。

**已备好的工具（2026-10-24）**
- `tools/score_wake.py`：离线用线上同一 KWS 模型给 WAV 打分（含高通前后对比）；
- 结论先行：30% 音量下自回放到达麦克风的唤醒词比模型需要低约 20 dB
  （合成词 0 命中；录音 +10 dB 仍 0；+20 dB 才有 1 命中）——**不是链路故障，必须真人测**。

**Gate G1 阈值（可按实测微调，但要在报告里写原因）**

| 指标 | 门槛 | 不达标含义 |
|---|---|---|
| 安静唤醒 | ≥ 19/20 | KWS 模型/阈值问题（可调，不入 P3） |
| 背景噪声唤醒 | ≥ 17/20 | 同上，或需要做前端增益 |
| 8 h 误唤醒 | ≤ 2 | 阈值/回声问题 |
| 打断静音延迟 | P50 < 600 ms | 可能缺参考通道 → 进 P3 |
| 播放中自激 | 0/50 | AEC 缺失的典型症状 → 进 P3 |

**判定**：只有"打断/自激"两项不达标、且证据指向缺参考通道，才进 P3；否则跳到 P4。

---

### P3 — rui 接管 AEC（条件项，6–8 天｜目标 11-03 → 11-14）

> 完整分析：[docs/aec-takeover.md](docs/aec-takeover.md)；
> 补丁级实施计划（已核实上游代码路径、A/B 槽回滚）：[docs/p3-firmware-patch.md](docs/p3-firmware-patch.md)。
> **这是唯一会碰设备的阶段。** adapter 侧已就绪：`DOT2_REF_CHANNELS=1` 即切 2ch + `REFERENCE` 能力。

**P3.1 离线验证（2 天，可与 P2 并行）**
- 用 P0 的抓包 + 设备 raw 9ch 抓取，离线跑 `dealias`（283 行）+ 一条 speex；
- 目标：复现"raw ch8 = 1 dB vs 去混叠 = 25.7 dB"，确认 25 dB 量级可达；
- 顺带量：S16 量化下的 ERLE 天花板（决定是否需要调 `micGainDb`）。

**P3.2 固件 fork（2–3 天）**
- ack 协商 `dot2_hw_ref`；上行 2ch = 波束 mono（预 AEC）+ 去混叠 ch8；
- 关设备 AEC（`config` 推送 `aecEnabled:false`），但参考抽取链路常开（`WriteFar` 照跑）；
- 交叉编译 → `adb push` 替换 `/data/local/bin/server`，旧二进制留档。

**P3.3 adapter 2ch + A/B（2–3 天）**
- `AudioFrame(channels=2, mic_channels=1, reference_channels=1)`，宣告 `REFERENCE`；
- A/B 对比：设备 AEC vs rui AEC（同房间、同音量、同 20 句），看控制台 `aec_erle_db`。

**验收（Gate G2）**
- [ ] rui `aec_erle_db` ≥ 20 dB（与设备基线同量级）
- [ ] 打断指标比 P2 提升 ≥ 30%，且误唤醒不劣化
- [ ] 上行 ≤ 70 KB/s；设备可用内存 > 100 MB；CPU 不高于改动前
- [ ] 断电重启自恢复；不宣告 `dot2_hw_ref` 即回退单声道（无需重刷）

**不通过的处理**：保留设备 AEC / 单声道，P3 结案为"不做"，把数据写进 `aec-takeover.md`。

---

### P4 — NAS 生产化（里程碑级，一次性部署｜不排日期）

**前置（9 哥 2026-10-24 定）**：**不在 NAS 上调试**。先在 Mac 本地把问题全部磨平，
通过 [`docs/milestone-m1.md`](docs/milestone-m1.md) 的 M1 验收清单，再一次性上 NAS 验证。
NAS 上的 rui 是生产实例（还带小爱 LX06 链路），重启有真实影响，必须攒到节点做。

**交付**
- NAS 上的 adapter 挂载方案（卷路径 + `PYTHONPATH` + `sitecustomize.py`），**不改 rui 镜像**；
- 证书与 token 就位；端口 8867/8870 在 NAS 放行；Dot 的 `controller.json` 指向 NAS；
- 备份/恢复脚本（证书、`config.yaml`、`controller.json` 模板）；
- 运维手册：怎么看"设备掉线 vs rui 挂了"、怎么回滚。

**验收**
- [ ] 24 h soak：无崩溃、无内存单调增长、重连次数记录
- [ ] 拔电重启 Dot ≤ 60 s 恢复对话
- [ ] 断网 5 min 恢复后自动重连
- [ ] 回滚演练（改 `controller.json` → 旧 controller）< 2 min
- [ ] 旧 EchoMuse controller 保持可启动（不删、不卸）

---

### P5 — 交接与标准输入（持续）

**交付**
- README/部署文档做到"换一台同型号能照做"；
- 向 rui 的 B 类标准提交能力矩阵行（**提议，不改 rui 代码**，由 9 哥决定是否排期）；
- 把最终状态回写 `hermes-dot2-voice`（设备侧前置工作）。

## 3. 时间线（按每天 4–6 h 有效投入）

```
10-25  10-26 10-27 10-28 | 10-29 10-30 10-31 11-01 11-02 | 11-03          11-14 | 11-15 11-18
[-- P0 --]                 | [------ P1 ------]           | [---- P3(条件) ----] | [-- P4 --]
         [-- P1 起手 --]   | [-------- P2 --------] G1   |                      |
                                             └── G1 不过 ──────────────────────┘
                          [P3.1 离线验证 可与 P2 并行]
```

- **rui 验收日（10-25）优先**：P0 与验收互不冲突，可并行；
- P2 的 Gate G1 是唯一会改变总工期的地方：过了直接 P4（总 9–12 天），不过加 6–8 天。

## 4. 计量与证据规范（每个阶段都要有数字）

- 每阶段产出 `docs/measurements.md` 的一节：场景、n、中位数、p90、失败样本链接；
- 原始数据进 `docs/trace/`（gitignore 掉可能含 token 的 raw 文件）；
- 结论必须带"测了什么、多少次、什么环境"；**没量就写"未测"**，不写"已优化"。
- 口径统一：延迟从"用户说完"到"设备出声第一帧"；打断从"唤醒触发"到"设备静音"。

## 5. 需要 9 哥 的输入

| # | 事项 | 什么时候需要 | 不给会怎样 |
|---|---|---|---|
| 1 | NAS 上 rui 容器的卷路径 / 是否允许加挂载 | P4 前 | adapter 只能在本机跑 |
| 2 | EchoMuse controller 允许停机切换的时间窗 | P0 | 探针无法上线（可放第二 endpoint 规避） |
| 3 | 设备 token 确认（来源：旧 controller DB / 设备文件） | P0 | 握手校验只能先 `allow_any_token` |
| 4 | 唤醒词与 TTS 音色定稿 | P1 | 标定结果要重跑 |
| 5 | 是否接受维护一个 EchoMuse 固件 fork | **G1 之后、P3 之前** | 只能停在单声道 + 设备 AEC |
| 6 | rui 验收日之后 NAS 何时更新镜像 | P4 | P4 顺延 |

## 6. 风险登记

| 风险 | 概率 | 影响 | 应对 / 触发条件 |
|---|---|---|---|
| 单声道下打断不达标 | 中 | 高 | G1 量化后决定 P3；P3.1 先离线验证再动设备 |
| ch8 混叠被忽略 → ERLE 只有 1 dB | 中 | 高 | 验收硬指标含 `aec_erle_db`；参考必须走 `dealias` |
| S16 量化天花板 <20 dB | 中 | 中 | 调 `micGainDb` 留余量；仍不够就保留设备 AEC |
| 设备 TLS 证书丢失 | 低 | 高（要重配网刷机） | P0 完成双备份；验收单里有这一项 |
| 双 AEC 级联 | 中 | 中 | 接管时必须确认设备侧 `aecEnabled=false` 且日志 att≈0 dB |
| 固件 fork 与上游分叉 | 中 | 中 | 改动控制在"成帧 + 复用 dealias"；记录上游 commit |
| rui 冻结期后续改动影响挂载 | 低 | 中 | 只依赖 `DeviceAdapter` 契约 + `register()`；升级时重跑 smoke |
| 设备 WiFi 抖动 | 中 | 中 | P4 记录重连率，与旧链路 `worst 5372 ms` 基线对比 |
| 两套唤醒打架 | 中 | 中 | 设备唤醒设 `off`，只留 rui KWS |

## 7. 与 rui「B 类」标准的对齐（提案，只读）

按 rui `docs/remaining-work.md` §I，dot2 作为 B 类样板的建议行：

| 维度 | LX06（A 类） | dot2（B 类） |
|---|---|---|
| 权限 | 无 root，原厂共存 | root / 可换二进制 / 可自启动 |
| 麦克风 | 6 麦 + 2 参考（client 已混） | 7 麦 + ch8 参考（可交出参考） |
| AEC | `device`（厂商前端） | `rui`（可回退 `device`） |
| 参考 | `loopback`（同帧，2.4 ms lead） | `loopback`（同帧，需去混叠） |
| 对齐 | `needs_delay`（软件 tap 才需要） | `frame_aligned` |
| 播放 | 16 kHz mono，设备重采样 | 48 kHz mono |
| 灯 / 键 / 音量 | 有 | 有 |
| 回滚 | 恢复 client | `controller.json` + TWRP |

降级链：`rui AEC → 设备 AEC → 无 AEC（仅 ASR，无 barge）`，每一级都要在控制面可见。

## 8. 一眼看懂"现在做到哪"（2026-10-24 03:0x）

| 阶段 | 状态 | 证据 / 阻塞 |
|---|---|---|
| P0 探针 | ✅ | 真机 register/ack、12.5 帧/s、音量 30% 强制、回滚路径（[报告](docs/p0-p1-report.md)） |
| P1 adapter v0 | ✅ 基本完成 | rui 本机 + 真机：TTS 出声（85 periods/0 underruns）、ASR 转写回采人声正确、`/health` 在线 |
| P2 唤醒/打断 | ⬜ 等人 | 工具与配置就绪；自测音量低约 20 dB，真人实测是唯一可信口径 |
| P3 rui 接管 AEC | ⬜ 条件项 | 离线验证已完成（参考重建收益 ≈31 dB）；需 Go/NDK 工具链构建固件，且必须真人回归 |
| P4 NAS 生产化 | ⬜ 等人 | 需要 NAS 上 rui 容器卷路径 + 验收日后的镜像更新；override 与 runbook 已备好 |
| 鲁棒性 | ✅ 已实测 | 杀掉音频面 → watchdog 3 s 重建 → 设备 3 s 重连；8 h soak 已启动（`scripts/soak.py`） |
| 音量 / 灯 / 回滚 | ✅ 已实测 | 音量 30% 全链路强制且控制台可见（76/255）；12 灯接 rui 状态；杀 adapter → 设备 40 s 落到旧 controller，恢复后 45 s 回到本项目（旧 controller 接管会推 85/127，故无人值守时保持停止） |
| P3 固件补丁 | ✅ 已编译未安装 | `v2.19.0-ea.1` + `patches/0001-ref-uplink-*.patch`，ARM 产物 sha256 `876f63b4…`；2ch 链路已用假设备在 rui 里验证 |

代码侧：`device_link.py`（设备协议）、`adapter.py`（rui 契约）、`standalone.py`（无 rui 探针）、
`inject.py`（仅测试用注入）、`volume.py`（30% 上限）、18 个单测通过；`dev_rui.sh` 一条命令起本机 rui。

## 8.5 工作界面：插头 / 插座（2026-10-24 上午）

两条维护线解耦：rui 定义**插座规范**，音箱侧交付**插头**（声明式 manifest + 实现）。
方向、边界归属、盲开假设与待确认项见
[`docs/plug-socket-contract.md`](docs/plug-socket-contract.md)。插座尚未开发，本方先盲开：
下一步把参数收拢成 `plug.json`，`build()` 只从 manifest + 覆盖读参数。

## 9. v1 → v2 变更

1. **P3 重写**：从"9 声道原始流"改为"2ch（波束 + 去混叠 ch8）"，并补上 ch8 混叠这个硬坑与离线验证步骤。
2. **新增生产目标**：rui 实际跑在 NAS `192.168.5.168`，P4 按 NAS 挂载写，不再假设 Mac 常驻。
3. **新增约束**：rui v1 冻结 + NAS 镜像滞后 → P4 排在验收日之后；接入方式坚持零改 rui。
4. **时间线带日期与 Gate**：G1（要不要接管 AEC）成为唯一影响工期的决策点。
5. **新增**：计量与证据规范、需要 9 哥的输入清单、B 类能力矩阵提案、风险表更新。
