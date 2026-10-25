# 交接词：直接粘给新会话（自包含）

你在 **rui-dot2-bridge** 项目上接手。请先读
`01_Code/projects/rui-dot2-bridge/docs/session-brief.md`（一页，含全部状态、登录方式、已知坑），
需要细节再读 `docs/handoff-2026-10-25-p4.md` 与 `docs/cold-start-0.4.2.md`。

## 项目目标

把已解锁的 Echo Dot 2（EchoMuse 固件，TLS WebSocket 8870 + X-EM-Token）做成 rui 的语音卫星：
**不改 rui 源码**，用 out-of-tree adapter/plug（`RUI_ADAPTER_PLUGINS` + `rui-device/v1` manifest）；
AEC 归设备（`ref_channels=0`）；部署一律 Docker。

## 当前状态（2026-10-25 15:45）

- 测试机 **.34** 跑 `rui-dot2:0.3.4` 下游镜像，healthy；Dot 2 在线：
  `devices=1, enabled=1, audio_plane=true, speaker_connected=true, adapter_plugins.registered=["dot2"]`，
  8870 wss 在听，日志有 `dot2 link: dot2 registered`。
- rui 上游最新 **0.4.1**（`0.4`=它，digest `380327e5…`）；**0.4.2 未发布**。
- 我们仓库 **PR #1 已合入**（squash `dd314cd`）：`rui_compat ">=0.3.0 <0.5.0"`、plugin 1.1.1、adapter 零改动。
- 契约自测：rui **0.3.4 / 0.4.0 / 0.4.1 全部 15 PASS / 0 FAIL / 2 WARN / 5 SKIP**（报告在 `docs/contract-report-0.4.*.json`）。
- 已修复：`audio.playback.rate` 改 48000（否则不播）；**自听**（rui follow-up 窗口把残响转成 turn）→
  "播放中保持麦克风 + 播放后静音 2.5 s"（`DOT2_PLAYBACK_GATE_MS=2500`）；重启后 14 s 设备重连，0 traceback。
- 未完成：**真人段**（说"你好小瑞"→回合、播放中打断、音量/LED、安静房间误唤醒基线）。

## 服务器登录与路径（.34）

- SSH **公钥认证免密**：`ssh -o BatchMode=yes yes999zc@<lan-ip>`（不需要密码）
- 部署目录：`~/rui`（docker compose、host 网络、容器名 `rui`；`config/`、`data/`、`dot2-secrets/`）
- 构建目录：`~/rui-dot2`（本仓库 rsync 上去的源码，用于在 x86_64 宿主构建镜像）
- 容器内关键路径：`/config/config.yaml`、`/var/lib/rui`（data_dir，含 `plugs/dot2-echomuse/`）、`/opt/rui-secrets`（= `~/rui/dot2-secrets`，0600）
- 运行密钥：`~/rui/.env`、`~/rui/config/config.yaml`（**不要外传/不要贴出**）
- 设备：Echo Dot 2 `<lan-ip>`（EchoMuse fw `20261024-1911-dev`）；证书/X-EM-Token 在 `~/rui/dot2-secrets/`
- 备份/回滚：`~/rui-backup-20261025-095406`、`~/rui-upgrade-20261025-095711`、`~/rui/docker-compose.yml.vanilla-0.3.4`、0.2.2 镜像仍在本地

## 下一步（按序）

1. **盯 0.4.2**：出现新 tag 时先确认 **ghcr 镜像可拉 + digest**（"tag 有了但 publish 失败"真实发生过，0.4.0 就是）；
2. 0.4.2 可用后按 `docs/cold-start-0.4.2.md` 执行：**纯用户视角 agent** 部署 vanilla 0.4.2 →
   我加 adapter 层（构建 `rui-dot2:0.4.2`、装 manifest 到 data/plugs、compose 切换、官方 `POST /api/v1/setup/device`）→
   **真机回合/打断 + 回滚演练** → 结果回写 rui#7；
3. 0.4.1 起可**撤掉** `DOT2_PLAYBACK_GATE_MS`，改用 `audio.voice.followup_delay_ms=2500` + `followup_replay=false`；
   并确认真机 `/api/v1/devices/dot2/status` 显示 **8870 / DOT2_EM_TOKEN**；
4. 补第 3 点里的真人段验收。

## 待决策

1. 0.4.2 冷启动在 **.34 原地受控切换**（维护者已按 9 哥意见确认）还是换干净主机 —— 9 哥最终拍板；
2. 设备侧 OWW（"你好小瑞"本地唤醒）：英文 stock 先跑通 vs `oww_forge` 训中文模型；
3. 多设备仲裁（rui#11）：等 rui 回 Q1–Q7 再实现。

## 硬约束

- **不碰 `<lan-ip>`**（生产 NAS）；**不改 rui 源码**；**未经批准不升级/重启 .34**；
- 结论必须回写 GitHub（#3 device_wake / #7 接入 / #11 多设备仲裁），聊天里说的不算；
- 不在文档/聊天/commit 里出现 token、证书、密钥；
- **讨论室那条线（飞书群 / agent-mailbox / agent-room）已冻结**，9 哥要单独再议，不要恢复或推进。

## 与 rui 的沟通渠道

GitHub issue：**#7**（接入/0.4.x）、**#3**（device_wake）、**#11**（多设备仲裁）。
维护者已确认：**0.4.2 发布后由他在 #7 公布 tag/digest**，我们按自己的冷启动清单执行并记录真机回合/回滚结果。
