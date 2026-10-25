# 交接词：直接粘给新会话（自包含）

你在 **rui-dot2-bridge** 项目上接手。请先读
`01_Code/projects/rui-dot2-bridge/docs/session-brief.md`（一页，含全部状态、登录方式、已知坑），
需要细节再读 `docs/switch-0.4.2-report.md` 与 `docs/cold-start-0.4.2.md`。

## 项目目标

把已解锁的 Echo Dot 2（EchoMuse 固件，TLS WebSocket 8870 + X-EM-Token）做成 rui 的语音卫星：
**不改 rui 源码**，用 out-of-tree adapter/plug（`RUI_ADAPTER_PLUGINS` + `rui-device/v1` manifest）；
AEC 归设备（`ref_channels=0`）；部署一律 Docker。

## 当前状态（2026-10-25 16:10）

- 测试机 **.34** 跑 `rui-dot2:0.4.2`（官方 0.4.2 + 内置 dot2 bridge，manifest 1.1.1），`healthy`；
  Dot 2 在线：`devices=1, enabled=1, audio_plane=true, speaker_connected=true, device_errors=0,
  adapter_plugins.registered=["dot2"]`，8080/8870/8872 在听。
- rui 上游最新 **0.4.2**（`v0.4.2`=`3c4af3e`，digest `sha256:3791ccfd…`），只改控制台/文档。
- 契约自测：0.3.4 / 0.4.0 / 0.4.1 / **0.4.2** 全部 15 PASS / 0 FAIL / 2 WARN / 5 SKIP。
- **受控切换已完成**（15:44–16:04）：清空旧镜像 → 新用户视角 agent 装 vanilla 0.4.2（版本对但必然 unhealthy）→
  Lead 加 adapter 层 → healthy；回滚演练 0.4.2↔0.3.4 双向通过（21 s / 11 s）。
- **门控结论（与上游建议相反）**：撤 `DOT2_PLAYBACK_GATE_MS` 只留 `followup_delay_ms=2500`+`followup_replay=false`
  会复现自听（垃圾 turn）；恢复门控后 45 s 内 0 条伪造 turn → **门控必须保留**，待回写 #7。
- 音量：`devices[].volume` 60 → **100**（设备 unity + `EQ_100`），9 哥已复听确认「声音可以了」。
- 真人段：9 哥确认历史已做过，0.4.2 **不再重复安排**；用官方 `/debug/say` 覆盖 runner/TTS/播放链路。
- 剩余：`ready=false/token_missing` 显示口径（等 rui 回）、OWW 决策；`.34` 控制台 token 已轮换。

## 服务器登录与路径（.34）

- SSH **公钥认证免密**：`ssh -o BatchMode=yes yes999zc@<lan-ip>`（不需要密码）
- 部署目录：`~/rui`（docker compose、host 网络、容器名 `rui`；`config/`、`data/`、`dot2-secrets/`）
- 构建目录：`~/rui-dot2`（本仓库 rsync 上去的源码）
- 容器内关键路径：`/config/config.yaml`、`/var/lib/rui`（data_dir，含 `plugs/dot2-echomuse/`）、`/opt/rui-secrets`（= `~/rui/dot2-secrets`，0600）
- 运行密钥：`~/rui/.env`、`~/rui/config/config.yaml`（**不要外传/不要贴出**）
- 设备：Echo Dot 2 `<lan-ip>`（EchoMuse fw `20261024-1911-dev`）；证书/X-EM-Token 在 `~/rui/dot2-secrets/`
- 回滚物料：`~/rollback-switch-0.4.2-20261025-154420/`（state tar + `rui-dot2:0.3.4` 镜像 tar + 0.4.2 快照 + sha256）

## 下一步（按序）

1. **回写 GitHub**：cold-start 卡点单独开 issue（反馈从 `docs/deploy-0.4.2-user-report.md` 提炼）；
   #7 回帖（0.4.2 清单逐项、门控实测、token 显示口径、healthcheck 提示、回滚数据）；
    #11 只做"已知悉、不改 adapter 字段"的 ack（如需）。
2. ~~真人段补测~~（已关闭：9 哥确认历史已验证，0.4.2 不重复）。
3. `ready=false / token_missing` 三选一修法（启动注入 `DOT2_EM_TOKEN` / manifest 回默认 `RUI_DEVICE_TOKEN` / 上游支持文件来源）。
4. ~~音量偏小~~（已修复：`devices[].volume` 60→100，9 哥确认可以）。
5. 设备侧 OWW 决策：建议先 B（英文 stock `hey_jarvis` 验机制）再 C（oww_forge 训中文）。

## 待决策

1. ~~真人段何时补测~~（已关闭）；
2. OWW：英文 stock 先跑通 vs 训中文"你好小瑞"；
3. 多设备：等上游 0.5 Phase 2 契约（单实例多 plane），一次实现到位，不为仲裁改 adapter 字段。

## 硬约束

- **不碰 `<lan-ip>`**（生产 NAS）；**不改 rui 源码**；**未经批准不升级/重启 .34**；
- 结论必须回写 GitHub（#3 device_wake / #7 接入 / #11 多设备仲裁 / cold-start issue），聊天里说的不算；
- 不在文档/聊天/commit 里出现 token、证书、密钥；
- **讨论室那条线（飞书群 / agent-mailbox / agent-room）已冻结**，9 哥要单独再议，不要恢复或推进。

## 与 rui 的沟通渠道

GitHub issue：**#7**（接入/0.4.x）、**#3**（device_wake）、**#11**（多设备仲裁）、**新增 cold-start 反馈 issue**。
