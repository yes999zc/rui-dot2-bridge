# Dot 2 → rui 卫星适配

把已解锁并装好 EchoMuse 设备端的 Echo Dot 2（RS03QR / biscuit）改造成 [rui](../rui) 的一个语音卫星。

**核心结论：不重新适配 EchoMuse controller，而是用 rui 的 adapter 顶替它。**
EchoMuse controller 是一个和 rui 竞争的全栈助手（HA Assist + ESPHome 编排），留着它，rui 只能挂在它后面。
本项目只保留设备侧已经跑通的部分——**设备端固件 + 设备协议 + 已下发的 TLS 证书**——把 controller 换成一个 rui 侧 adapter。
取舍见 [`docs/adr-0001-adapter-as-controller.md`](docs/adr-0001-adapter-as-controller.md)。

## 拓扑

```
                      现在的（要拆掉）
 Dot 2 ──wss(/control,/data)──▶ EchoMuse controller ──ESPHome──▶ HA Assist ──▶ (没有 rui)

                      目标
 Dot 2 ──wss(/control,/data)──▶ rui adapter "dot2"（本项目）──▶ rui audio plane
        ▲                                                        │
        └──────────── speaker PCM / mic PCM ◀────────────────────┘
                                                        rui: KWS → VAD → ASR → LLM → TTS
```

设备不做改动、不重刷、不重新配网：它信任的 CA 和 token 已经在 `/data/local/etc/echomuse/`，本项目复用同一套证书对设备提供 `wss://`。

## 为什么可行（三条已确认的事实）

1. **设备协议是文档化的、能力协商的。** `register → ack`，之后 `mic_start` / `/data` 帧；上行 `0x01` = S16 单声道 16 kHz（80 ms 一批，12.5 帧/s），下行 `0x02` = S16 单声道 48 kHz。见 [`docs/protocol-map.md`](docs/protocol-map.md)。
2. **设备支持静态 controller 地址**，不必依赖 mDNS：`/data/local/etc/echomuse/controller.json` → `{"endpoints":[{"host":"…","port":8767,"tls_port":8770}],"mdns":false}`。老项目里那条 `dns-sd -P` 前台进程可以彻底不要。
3. **rui 的设备层就是为"加一个 adapter"设计的**：`DeviceAdapter` 契约 + `AdapterName` 已是普通字符串 + `registry.register(name, target)` 钩子。adapter 可以放在**本仓库**，不需要改 rui 任何文件。

## 现状（2026-10-25）

> **重大变化**：rui 已发布 **0.2.2**。测试实例改成 Docker 部署，控制台 **8080**、设备单入口 **4412**，
> 0.1.x 的 8095/8870/8872 在 0.2.2 中已不存在；Dot2 的 EchoMuse 协议（wss + `X-EM-Token`）暂时接不上，
> 桥迁移**暂停**，等 rui 定稿 plug/接口文档。背景、成果、问题、下一步见
> [`docs/handoff-2026-10-25.md`](docs/handoff-2026-10-25.md)。
>
> 设备侧：P3 补丁固件**已安装**（server_b，`20261024-1911-dev`），独立仓库
> [`echomuse-dot2-ref`](https://github.com/yes999zc/echomuse-dot2-ref) v0.1.0；server_a 保留旧固件可回滚。
> AEC 结论：设备 AEC（14–18 dB）已是该链路的上限，生产用 `ref_channels=0` + 设备 AEC。


| 项 | 状态 |
|---|---|
| Dot 2 解锁 / root / EchoMuse 设备端 v2.18.1 | ✅ 已就绪（见 `hermes-dot2-voice`） |
| **P1 adapter（可运行）** | ✅ 真机实测：register/ack、单声道 80ms→20ms、48k 播放、EOS/flush、30% 音量强制、12 灯 LED、34 单测 |
| **rui 端到端** | ✅ 文本回合 + TTS 出声（`periods=131 underruns=0`）；声学回采 ASR 转写正确；`audio_plane/speaker_connected` 在线 |
| **唤醒** | 🟡 30% 音量下已实测到真实 `wake: 你好小瑞`（讯飞音色）；唤醒率/误唤醒等真人 P2 标定 |
| **回滚** | ✅ 杀 adapter → 设备 40 s 落到旧 controller；恢复后 45 s 回来（旧 controller 接管会推 85/127，故无人值守时保持停止） |
| **P3 固件补丁** | ✅ 已编译未安装（`v2.19.0-ea.1` + patch，sha256 `876f63b4…`）；2ch 链路已用假设备在 rui 里验证 |
| **响度** | 原厂 EQ_50…EQ_100 阶梯 + look-ahead 限幅（**+9.7 dB A 计权、零削波**）；参数运行时可调，见 `docs/loudness-eq.md` |
| **音量** | 上限可配，现为 **100%（level 127 = 0 dB）**；控制台回读设备实际值；物理按键在此上限内有效 |
| **麦克风** | 设备预增益 **18 dB**（默认 24 dB 会削波；实测语音段峰值 0.70–0.88） |
| **插头** | `plug.json` 声明（transport/audio/volume/capabilities/loudness/control），`build()` 按 manifest<env<DeviceConfig 取值 |
| **静音** | `DOT2_MUTE=1` 重启：音量上限=0，唤醒/ASR/打断照常，只是不出声 |
| **播放空档** | 已修：flush + pong 屏障 + 0.45 s 预缓冲（延迟 +0.45 s，设备 `underruns=0`）；rui 侧更优修法见 [rui#1](https://github.com/yes999zc/rui/issues/1) |
| **工具链** | ✅ `scripts/check.sh`、`doctor.py`、`device_health.py`、`p2_trials.py`、`soak.py`；ruff 干净 |
| rui 生产部署（NAS） | ⬜ 待 NAS 部署目录 + 验收日镜像更新；override/runbook 已备好 |
| 旧 EchoMuse controller | 容器保留可启动，默认停止（30% 音量约束） |

## 独立测试实例

旧方案（systemd + venv + rui 0.1.3，8095/8870/8872）已于 2026-10-24 整体删除。
现在测试实例是 **rui v0.2.2 官方 Docker 镜像**（控制台 8080，设备入口 4412，`network_mode: host`），
由一次"纯用户视角"的部署完成，报告见 [`docs/deploy-0.2.2-user-report.md`](docs/deploy-0.2.2-user-report.md)。
旧部署的踩坑与复盘仍保留在 [`docs/deploy-honor-nas.md`](docs/deploy-honor-nas.md)、
[`docs/deploy-honor-nas-retro.md`](docs/deploy-honor-nas-retro.md)。

## AEC（谁消回声）

三份调研在本仓库：

- [`docs/aec-rui-analysis.md`](docs/aec-rui-analysis.md) — rui AEC 诊断：帧对齐正确、线性上限 14–18 dB、
  rui 增量 ERLE 0.5–9 dB 不是系统 ERLE；
- [`docs/aec-device-tuning.md`](docs/aec-device-tuning.md) — 设备 AEC：稳态 14–18.2 dB；tail/AGC 参数在
  当前拓扑下无效；可动的只有 `wakeMic`/播放电平/固件改动；
- [`docs/aec-parameter-model.md`](docs/aec-parameter-model.md) — `who`/`reference`/`constraints`
  参数模型，封闭 vs 开放两类音箱的默认值。

结论：**生产走 `ref_channels=0` + 设备 AEC**；`DOT2_PLAYBACK_GATE_MS` 作为可选半双工兜底。
设备侧唤醒（`oww_wake`）作为 barge 的正式路径，已向 rui 提契约请求
[yes999zc/rui#3](https://github.com/yes999zc/rui/issues/3)。

## 目录

```
docs/findings.md        事实清单，每条带源码出处
docs/adr-0001-…md       为什么用 adapter 顶替 controller（含被否决的方案）
docs/protocol-map.md    设备帧 ↔ rui AudioFrame 映射、时序
docs/aec-takeover.md    rui 接管 AEC 的可行性（2ch 方案、ch8 去混叠坑、验收口径）
docs/p3-firmware-patch.md  P3 固件补丁计划（上游代码路径、约 100 行改动、A/B 槽安装与回滚）
docs/p0-p1-report.md    P0/P1 真机实测报告（协议、音量、TTS 播放、声学回路转写、KWS 差距）
docs/p2-runbook.md      P2 真人实测 runbook（唤醒/打断/误唤醒，含工具用法）
docs/plug-socket-contract.md  插头/插座 工作界面（rui 定义插座、本仓库交付插头；当前盲开）
docs/milestone-m1.md    M1 验收清单 + NAS 一次性部署流程（本地磨平 → NAS 验证）
docs/deploy.md          部署与回滚 runbook
src/rui_dot2_bridge/    adapter 实现（Phase 1 起填充）
src/sitecustomize.py    注入 rui 进程完成 adapter 注册（不改 rui 仓库）
scripts/check.sh        一条命令自检：语法 / 单测 / 配置 / ruff / doctor
scripts/doctor.py       部署前体检（证书、配置、端口、rui、设备音量上限）
scripts/device_health.py  设备侧健康快照（mic 连续流 / AEC / 音量 / 内存 / 连的是谁）
scripts/p2_trials.py    P2 真人唤醒/打断测量记录器
scripts/soak.py         长稳记录（每分钟一行 JSONL）
ROADMAP.md              分阶段计划与验收标准
```

## 相关仓库

- [rui](https://github.com/yes999zc/rui) — 助手本体（ASR / LLM / TTS / 会话 / 播放仲裁）。**本项目不改它。**
- [hermes-dot2-voice](https://github.com/yes999zc/hermes-dot2-voice) — 设备侧前置工作：解锁、刷机、还原。
- [EchoMuse](https://github.com/wilbowes/EchoMuse) — 设备端固件与设备协议（MIT）。本项目只读它的协议实现。

## 不做

- 不改 rui 仓库的源码；接入只走 `PYTHONPATH` + `sitecustomize.py` 注册。
- 不重刷设备、不重新配网、不切 emOS（Phase 3 若需要多声道才动固件）。
- 不在 Dot 上跑 ASR/LLM（483 MB 内存）。
- 不动小爱（LX06）那条链路。
