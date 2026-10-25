# 会话简报（唯一入口；新会话先读这个）

> 最后更新：2026-10-25 15:45 ｜ 上下文重置/新开会话时的**唯一入口**。
> 冻结声明：讨论室（飞书群 / agent-mailbox / agent-room）这条线**已冻结**，9 哥要单独再议，不要继续推进。

## 一句话现状

Dot 2 已作为 **rui 0.3.4** 卫星跑通（下游镜像 + Socket/Plug v1 插头，设备在线 8870）；
**等 rui 0.4.2 发布**后在 .34 做受控切换 + 真机回合/回滚。代码与文档已推送 main。

## 服务器与环境

| 项 | 值 |
|---|---|
| 测试主机 | **yes999zc@<lan-ip>**（Debian 12，8C/6.7G，host 网络） |
| 登录方式 | SSH **公钥认证**（已配置好，非交互可用）：`ssh -o BatchMode=yes yes999zc@<lan-ip>`；无需密码 |
| 部署目录 | `~/rui`（compose：`docker-compose.yml` + `config/` + `data/` + `dot2-secrets/`） |
| 构建目录 | `~/rui-dot2`（本仓库 rsync 上去的源码，用于在 x86_64 宿主构建镜像） |
| 当前镜像 | **rui-dot2:0.3.4**（官方 0.3.4 + `/opt/rui-plugins/rui_dot2_bridge`），healthy |
| 备份 | `~/rui-backup-20261025-095406`（.env/config/compose）、`~/rui-upgrade-20261025-095711`、`~/rui/docker-compose.yml.vanilla-0.3.4` |
| 设备 | Echo Dot 2 / EchoMuse fw `20261024-1911-dev` / `<lan-ip>`，wss 8870 + `X-EM-Token` |
| 设备凭据 | 证书+token 在 `~/rui/dot2-secrets/`（0600，挂载为容器内 `/opt/rui-secrets`）；容器内 `/var/lib/rui/plugs/dot2-echomuse/` 必须有 manifest |
| 密钥位置 | 运行时 secrets 在 `~/rui/.env`（0600）与 `~/rui/config/config.yaml`（密钥不在此文档展开）；本机另有 `~/.hermes/.env` |

## 上游与契约状态

| 项 | 值 |
|---|---|
| rui 上游 | 最新 **0.4.1**（`0.4` 也指它，digest `sha256:380327e5…`，amd64+arm64）；**0.4.2 未发布** |
| 我们的兼容改动 | PR #1 已合入 `dd314cd`：`rui_compat ">=0.3.0 <0.5.0"`、plugin 1.1.1、adapter 零改动 |
| 契约自测 | rui 0.3.4 / 0.4.0 / 0.4.1：均 **15 PASS / 0 FAIL / 2 WARN / 5 SKIP**（报告在 `docs/contract-report-0.4.x.json`） |
| 维护者承诺 | 0.4.2 发布后由 rui 在 [#7](https://github.com/yes999zc/rui/issues/7) 公布 tag/digest；已同意我们在 .34 做受控切换（停 0.3.4→起 0.4.2→真机回合→回滚），不做并行实例 |
| 多设备 | 我们提了 [rui#11](https://github.com/yes999zc/rui/issues/11)（来源分层/同房间回声/可解释仲裁），等 rui 回 Q1–Q7；0.4.x 仍单音频面单设备 |
| 设备唤醒 | `device_wake` 契约已实现；设备缺 OWW 运行时/模型（stock 仅英文），当前诚实降级 KWS；见 `docs/p4-device-wake-feasibility.md` |

## 未决决策

1. 0.4.2 冷启动放 **.34 原地受控切换**（维护者已按 9 哥意见确认）还是换干净主机（9 哥未最终拍板）；
2. 设备侧 OWW：英文 stock 先跑通 vs `oww_forge` 训中文"你好小瑞"；
3. 多设备仲裁：等 rui 契约草案再实现（当前倾向）。

## 下一步（按序）

1. **盯 0.4.2**：有新 tag 先确认 **ghcr 镜像可拉 + digest**（tag 有了但 publish 失败的情况真实发生过）；
2. 0.4.2 可用后按 `docs/cold-start-0.4.2.md`：纯用户视角 agent 部署 vanilla → 加 adapter 层 → 真机回合/打断/回滚 → 回报 rui#7；
3. 0.4.1 起可撤 `DOT2_PLAYBACK_GATE_MS`，改 `audio.voice.followup_delay_ms=2500` + `followup_replay=false`；确认真机 status 显示 8870 / `DOT2_EM_TOKEN`；
4. 真人段补测：说"你好小瑞"→回合、播放中打断、音量/LED、安静房间误唤醒基线。

## 必读文件

1. `docs/session-brief.md`（本页）
2. `docs/next-session-prompt.md`（交接词，可直接粘给新会话）
3. `ROADMAP.md`「当前状态（v4）」
4. `docs/handoff-2026-10-25-p4.md`
5. `docs/cold-start-0.4.2.md`
6. `docs/watch-log.md`（与 rui 的往来；最新在末尾）

## 常用命令

    ssh -o BatchMode=yes yes999zc@<lan-ip> 'cd ~/rui && docker compose ps'
    ssh ... 'curl -sS http://127.0.0.1:8080/health'
    ssh ... 'docker compose logs --tail=80 rui'
    # 契约自测（两种镜像都跑）
    docker run --rm --entrypoint python -e PYTHONPATH=/opt/rui-plugins \
      -v ~/rui-dot2/src:/opt/rui-plugins ghcr.io/yes999zc/rui:0.4.1 \
      -m rui plug-test /opt/rui-plugins/rui_dot2_bridge \
      --adapter rui_dot2_bridge.adapter:build --json
    # 构建下游镜像（x86_64 宿主，legacy builder）
    cd ~/rui-dot2 && DOCKER_BUILDKIT=0 docker build -f deploy/downstream/Dockerfile \
      --build-arg RUI_IMAGE=ghcr.io/yes999zc/rui:0.4.2 -t rui-dot2:0.4.2 .
    # 回滚
    sed -i 's/^RUI_IMAGE=.*/RUI_IMAGE=rui-dot2:0.3.4/' ~/rui/.env && cd ~/rui && docker compose up -d --force-recreate

## 操作红线

- 不碰 `<lan-ip>`（生产 NAS）；不改 rui 源码；未批准不升级/重启 .34；
- 结论回写 GitHub（#3/#7/#11），聊天里说的不算；
- 不在文档/聊天/commit 里出现 token/证书/密钥；
- **讨论室（飞书 / agent-mailbox / agent-room）已冻结**，不要再推进或恢复，等 9 哥单独安排。

## 已知坑（踩过）

| 坑 | 事实 |
|---|---|
| 播放被拒 | `audio.playback.rate` 必须 **48000**，否则报 "wire is 48000" 且不播 |
| 自听 | rui follow-up 窗口把残响转成 turn；播后静音 2.5 s 解决（0.4.1 起用上游配置） |
| 设备 AEC | Dot 抵消自己喇叭声 → 从它喇叭注入唤醒词永远唤不醒，唤醒只能真人发声 |
| tag ≠ 镜像 | 0.4.0 曾 tag 有了但 publish 失败；以 ghcr digest 可拉为准 |
| 回滚 | `RUI_UID/GID` 保持 1000；uid 0 在 `cap_drop: ALL` 下 crash-loop |
| PyYAML | 容器内没有 PyYAML；改 config 用 awk/sed |
| 构建 | 必须 x86_64 宿主，用 `DOCKER_BUILDKIT=0`（否则 BuildKit 会卡） |
