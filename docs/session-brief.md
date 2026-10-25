# 会话简报（唯一入口；新会话先读这个）

> 最后更新：2026-10-25 16:10 ｜ 上下文重置/新开会话时的**唯一入口**。
> 冻结声明：讨论室（飞书群 / agent-mailbox / agent-room）这条线**已冻结**，9 哥要单独再议，不要继续推进。

## 一句话现状

Dot 2 已作为 **rui 0.4.2** 卫星跑通（.34：vanilla 0.4.2 冷启动 + 下游 adapter 层，`rui-dot2:0.4.2` healthy，8870 在线）；
`#7` 清单里 8870/`DOT2_EM_TOKEN` 与回滚演练已验证，**撤门控那项实测不成立（自听复现）已回退**；
真人段按 9 哥要求暂缓。参数/效果调试归我们后续工作，当前目标是配合把上游 rui 写好。

## 服务器与环境

| 项 | 值 |
|---|---|
| 测试主机 | **yes999zc@<lan-ip>**（Debian 12，8C/6.7G，host 网络，Docker 28.5.2 / compose v2.40.3） |
| 登录方式 | SSH **公钥认证**（已配置好，非交互可用）：`ssh -o BatchMode=yes yes999zc@<lan-ip>`；无需密码 |
| 部署目录 | `~/rui`（compose：`docker-compose.yml` + `config/` + `data/` + `dot2-secrets/`） |
| 构建目录 | `~/rui-dot2`（本仓库 rsync 上去的源码，x86_64 宿主构建镜像用；切换期间曾临时移到 `~/.hold-rui-dot2-*`） |
| 当前镜像 | **rui-dot2:0.4.2**（官方 0.4.2 + `/opt/rui-plugins/rui_dot2_bridge`，manifest 1.1.1），healthy |
| 备份/回滚 | `~/rollback-switch-0.4.2-20261025-154420/`（state + 0.3.4 镜像 tar + 0.4.2 快照）、`~/rui-backup-20261025-095406`、`~/rui-upgrade-20261025-095711`、`~/rui/docker-compose.yml.vanilla-0.3.4` |
| 设备 | Echo Dot 2 / EchoMuse fw `20261024-1911-dev` / `<lan-ip>`，wss 8870 + `X-EM-Token` |
| 设备凭据 | 证书+token 在 `~/rui/dot2-secrets/`（0600，挂载为容器内 `/opt/rui-secrets`）；`/var/lib/rui/plugs/dot2-echomuse/` 必须有 manifest |
| 密钥位置 | 运行时 secrets 在 `~/rui/.env`（0600）与 `~/rui/config/config.yaml`；本机另有 `~/.hermes/.env` |

## 上游与契约状态

| 项 | 值 |
|---|---|
| rui 上游 | 最新 **0.4.2**（tag `v0.4.2`=`3c4af3e`，ghcr `0.4.2`=`0.4`，digest `sha256:3791ccfd…`）；0.4.2 只改控制台/文档 |
| 当前部署 | `rui-dot2:0.4.2`（base 官方 0.4.2）+ manifest `1.1.1` / `rui_compat ">=0.3.0 <0.5.0"` |
| 契约自测 | rui **0.3.4 / 0.4.0 / 0.4.1 / 0.4.2**：均 **15 PASS / 0 FAIL / 2 WARN / 5 SKIP**（`docs/contract-report-0.4.*.json`） |
| #7 清单 | ① /health ✓ ② status 8870 + `DOT2_EM_TOKEN` ✓ ③ **撤门控 ✗（自听复现，已回退）** ④ 回滚演练 ✓ / 真人回合暂缓 |
| rui#11 | Q1–Q7 已答复、仲裁决策冻结：**0.5 直接单实例多 plane**；多实例只是过渡；adapter 不为仲裁加字段 |
| 设备唤醒 | `device_wake` 契约已实现但设备缺 OWW 运行时/模型（stock 仅英文），诚实降级 KWS；见 `docs/p4-device-wake-feasibility.md` |

## 未决决策

1. 真人段何时补（说"你好小瑞"→回合、播放中打断、音量/LED、安静房间误唤醒基线）——9 哥安排时间；
2. 设备侧 OWW：英文 stock 先跑通（B）vs `oww_forge` 训中文"你好小瑞"（C）——建议先 B 再 C；
3. 多设备：等上游 0.5 Phase 2 契约草案，我们一次实现到位（不改 adapter 字段）。

## 下一步（按序）

1. 真人段补测（9 哥在位时）：唤醒→回合、播放中打断 ≤300 ms、音量/LED、安静房间 3–5 次基线；
2. 把 0.4.2 实测结果回写 rui：**cold-start 卡点单独开 issue** + **#7 回帖**（门控实测、token 显示口径、
   healthcheck 提示、回滚数据）——见 `docs/switch-0.4.2-report.md`；
3. `ready=false / token_missing` 显示口径修法三选一（启动注入 `DOT2_EM_TOKEN` / manifest 回默认 `RUI_DEVICE_TOKEN` / 上游支持文件来源）；
4. 音量偏小（9 哥实测）属我们 adapter 的 loudness/EQ 链路，后续单独调，不阻塞上游；
5. `.34` 控制台 auth token 已于 16:06 轮换（旧值在会话记录里出现过，未外传；轮换后容器重建、新 token 验证 200）。

## 必读文件

1. `docs/session-brief.md`（本页）
2. `docs/switch-0.4.2-report.md`（0.4.2 切换 + 门控发现 + 回滚演练）
3. `docs/deploy-0.4.2-user-report.md`（新用户视角冷启动报告，5 个卡点）
4. `docs/next-session-prompt.md`（交接词，可直接粘给新会话）
5. `ROADMAP.md`「当前状态（v4）」
6. `docs/watch-log.md`（与 rui 的往来；最新在末尾）

## 常用命令

    ssh -o BatchMode=yes yes999zc@<lan-ip> 'cd ~/rui && docker compose ps'
    ssh ... 'curl -sS http://127.0.0.1:8080/health'
    ssh ... 'docker compose logs --tail=80 rui'
    # 控制台 token（不要打印到聊天/文档）
    ssh ... 'docker exec rui rui token'
    # 契约自测（不部署）
    docker run --rm --entrypoint python -e PYTHONPATH=/opt/rui-plugins \
      -v ~/rui-dot2/src:/opt/rui-plugins ghcr.io/yes999zc/rui:0.4.2 \
      -m rui plug-test /opt/rui-plugins/rui_dot2_bridge \
      --adapter rui_dot2_bridge.adapter:build --json
    # 构建下游镜像（x86_64 宿主，legacy builder）
    cd ~/rui-dot2 && DOCKER_BUILDKIT=0 docker build -f deploy/downstream/Dockerfile \
      --build-arg RUI_IMAGE=ghcr.io/yes999zc/rui:0.4.2 -t rui-dot2:0.4.2 .
    # 非人工回合（验证播放链路）
    ssh ... 'TOK=$(docker exec rui rui token); curl -s -H "X-Auth-Token: $TOK" -H "Content-Type: application/json" \
      -d "{\"text\":\"现在几点\"}" http://127.0.0.1:8080/api/v1/debug/say'
    # 回滚到 0.3.4（三件套：load 镜像 + 恢复 .env/compose/config 快照 + up）
    # 详见 docs/switch-0.4.2-report.md §七 / §二

## 操作红线

- 不碰 `<lan-ip>`（生产 NAS）；不改 rui 源码；未批准不升级/重启 .34；
- 结论回写 GitHub（#3/#7/#11 + cold-start 反馈 issue），聊天里说的不算；
- 不在文档/聊天/commit 里出现 token/证书/密钥；
- **讨论室（飞书 / agent-mailbox / agent-room）已冻结**，不要再推进或恢复，等 9 哥单独安排。

## 已知坑（踩过）

| 坑 | 事实 |
|---|---|
| 播放被拒 | `audio.playback.rate` 必须 **48000**，否则报 "wire is 48000" 且不播 |
| 音量小 | `devices[].volume` 是百分数：60% = 设备 codec level 76 ≈ **−25.5 dB**（127=unity），且响度阶梯只给 `EQ_100` +14.9 dB；实测 60 比 100 低约 30–40 dB。部署用 **volume: 100** |
| 自听 | **0.4.2 实测**：只设 `followup_delay_ms=2500`+`followup_replay=false` 不够，仍会生成垃圾 turn；`DOT2_PLAYBACK_GATE_MS=2500` 必须保留 |
| 设备 AEC | Dot 抵消自己喇叭声 → 从它喇叭注入唤醒词永远唤不醒，唤醒只能真人发声 |
| vanilla 原地升级 | 已有 out-of-tree adapter 的安装切 vanilla 必然 unhealthy（enabled device 不可驱动）；且旧 manifest `<0.4.0` 会被拒 |
| manifest 双份 | 镜像内一份（loader 门控）+ `data/plugs/dot2-echomuse/` 一份（catalog/wake 策略）；升级时两份都要新 |
| 回滚 | 0.3.4 schema 不认 `followup_*` 两键 → 回滚必须用切换前 config 快照；`RUI_UID/GID` 保持 1000 |
| PyYAML | 容器内没有 PyYAML；改 config 用宿主 python3（有 PyYAML 6.0）或 awk/sed |
| 构建 | 必须 x86_64 宿主，用 `DOCKER_BUILDKIT=0`；构建上下文里的 `dev/*/audio.sock` 会打 socket 警告（无害） |
| tag ≠ 镜像 | 0.4.0 曾 tag 有了但 publish 失败；以 ghcr digest 可拉为准 |
