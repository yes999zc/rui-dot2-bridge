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

## 现状

| 项 | 状态 |
|---|---|
| Dot 2 解锁 / root / EchoMuse 设备端 v2.18.1 | ✅ 已就绪（见 `hermes-dot2-voice`） |
| EchoMuse controller（旧链路） | ✅ 可用，作为回滚路径保留 |
| 设备协议与 PCM 格式核实 | ✅ 2026-10-24（读 EchoMuse 上游源码） |
| rui adapter 接入点核实 | ✅ 2026-10-24（读 rui 源码） |
| rui 生产部署 | ✅ NAS `192.168.5.168:8080`（v0.0.1，1 台 LX06 在线，`/health` 正常） |
| rui 接管 AEC 可行性 | ✅ 已分析：2ch 方案，零改 rui（[`docs/aec-takeover.md`](docs/aec-takeover.md)） |
| dot2 adapter 代码 | ⬜ Phase 1 |
| 唤醒 / barge-in 标定 | ⬜ Phase 2 |

## 目录

```
docs/findings.md        事实清单，每条带源码出处
docs/adr-0001-…md       为什么用 adapter 顶替 controller（含被否决的方案）
docs/protocol-map.md    设备帧 ↔ rui AudioFrame 映射、时序
docs/aec-takeover.md    rui 接管 AEC 的可行性（2ch 方案、ch8 去混叠坑、验收口径）
docs/p3-firmware-patch.md  P3 固件补丁计划（上游代码路径、约 100 行改动、A/B 槽安装与回滚）
docs/p0-p1-report.md    P0/P1 真机实测报告（协议、音量、TTS 播放、声学回路转写、KWS 差距）
docs/p2-runbook.md      P2 真人实测 runbook（唤醒/打断/误唤醒，含工具用法）
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
