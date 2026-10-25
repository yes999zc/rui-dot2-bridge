# 会话简报（每次里程碑后刷新；新会话先读这个）

> 最后更新：2026-10-25 15:36 ｜ 用途：上下文重置（50–60%）或新开会话时的**唯一入口**。
> 读完这页，再按"必读文件"里的顺序补细节即可接手。

## 一句话现状

Dot 2 已作为 **rui 0.3.4** 卫星跑通（下游镜像 + Socket/Plug v1 插头，设备在线 8870）；
**等 rui 0.4.2 发布**后在 .34 做受控切换 + 真机回合/回滚。桥代码与文档已推送 main。

## 关键事实（不要重新推导）

| 项 | 值 |
|---|---|
| 测试实例 | yes999zc@<lan-ip>，~/rui，compose，host 网络 |
| 当前镜像 | rui-dot2:0.3.4（= 官方 0.3.4 + /opt/rui-plugins/rui_dot2_bridge），healthy |
| 设备 | Echo Dot 2 / EchoMuse fw 20261024-1911-dev / <lan-ip>，wss 8870 + X-EM-Token |
| 设备配置 | devices[]: dot2（adapter dot2，listen_port 8870，token 从 /opt/rui-secrets/device_token 读） |
| rui 上游 | 最新 **0.4.1**（0.4 也指它；digest 380327e5…）；**0.4.2 未发布** |
| 契约 | PR #1 已合入 dd314cd（rui_compat ">=0.3.0 <0.5.0"）；plug-test 0.3.4/0.4.0/0.4.1 全为 15 PASS / 0 FAIL / 2 WARN / 5 SKIP |
| 多设备 | rui#11 是我们的提案（来源分层/同房间回声/可解释仲裁），等 rui 回 Q1–Q7；0.4.x 仍单音频面单设备 |
| 会话通道 | 01_Code/tools/agent-mailbox（两个 DSH 会话主通道）+ 飞书群「dot2 × rui 联调室」（手机镜像） |

## 未决决策（等人拍板）

1. 0.4.2 冷启动放 **.34 原地受控切换**（推荐，维护者已确认 9 哥同意）还是换干净主机；
2. 设备侧 OWW（"你好小瑞"本地唤醒）走 **英文 stock 先跑通** 还是 **oww_forge 训中文模型**；
3. 多设备仲裁是否等 rui 契约草案出来再做（当前倾向：等）。

## 下一步（按序）

1. **0.4.2 出来** → 按 docs/cold-start-0.4.2.md 执行：纯用户视角 agent 部署 vanilla → 我加 adapter 层 → 真机回合/打断/回滚 → 回报 rui#7；
2. 0.4.1 起可**撤掉** DOT2_PLAYBACK_GATE_MS，改用 audio.voice.followup_delay_ms=2500 + followup_replay=false；确认 status 显示 8870 / DOT2_EM_TOKEN；
3. 真人段仍缺：说"你好小瑞"→回合、播放中打断、音量/LED、安静房间误唤醒基线（task-4 未完成）。

## 必读文件（按此顺序）

1. ROADMAP.md「当前状态（v4）」——一页纸；
2. docs/handoff-2026-10-25-p4.md——P4 全过程与证据；
3. docs/cold-start-0.4.2.md——下一步的操作清单；
4. docs/watch-log.md——与 rui 的往来记录（最新在末尾）；
5. docs/p4-device-wake-feasibility.md——device_wake/OWW 的边界与三条路线。

## 操作红线

- 不碰 <lan-ip>（生产 NAS）；不改 rui 源码；未批准不升级/重启 .34；
- 结论必须回写 GitHub（#3/#7/#11），聊天/信箱里的决定不算数；
- 不在任何文档、聊天、commit 里出现 token/证书/密钥。

## 已知坑（踩过的，别重踩）

| 坑 | 事实 |
|---|---|
| 播放被拒 | audio.playback.rate 必须 **48000**（设备线格式），否则每条回复报 "wire is 48000" 且不播 |
| 自听 | rui follow-up 窗口会把残响转成 turn；用"播放中保持麦克风 + 播放后静音 2.5 s"解决（0.4.1 起改用上游配置） |
| 设备 AEC | Dot 会抵消**自己喇叭**的声音 → 从它喇叭注入唤醒词永远唤不醒，唤醒只能真人发声 |
| tag ≠ 镜像 | 0.4.0 曾 tag 有了但 docker-publish 失败；以 ghcr digest 可拉为准 |
| 回滚 | RUI_UID/GID 必须保持 1000；uid 0 在 cap_drop: ALL 下 crash-loop |
| 容器内无 PyYAML | 改 config 用 awk/sed 或宿主机脚本，别 docker exec python -c 'import yaml' |
| 镜像内 build | 必须 x86_64 宿主；本机/并发 BuildKit 会卡，用 DOCKER_BUILDKIT=0 |

## 新会话开场白（粘给下一个自己）

> 接手 rui-dot2-bridge 项目：先读 01_Code/projects/rui-dot2-bridge/docs/session-brief.md，
> 再按它"必读文件"的顺序补细节。当前等 rui 0.4.2，之后按 docs/cold-start-0.4.2.md 做 .34 受控切换。
> 跨会话对话入口是 01_Code/tools/agent-mailbox（./room.py tail）。
