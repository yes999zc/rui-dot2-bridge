# 0.4.2 冷启动测试清单（等镜像可用后执行）

> 触发：`scripts/watch_upstream.py` 报告 **0.4.2 镜像可用**（ghcr tag 存在且 digest 可拉）。
> 目标：验证"一个拿到上游文档的新用户"能在干净环境把 0.4.2 跑起来，然后我们加 adapter 层完成端到端。
>
> 已做的前置（2026-10-25）：在 **0.4.0** 镜像（digest `sha256:c4cd1a61…`）上跑过契约工具——
> 当前 main 的 `rui_compat ">=0.3.0 <0.4.0"` 被 0.4.0 **拒绝**，PR #1 的 `<0.5.0` 通过
> （15 PASS / 0 FAIL / 2 WARN / 5 SKIP，与 0.3.4 基线一致）。即 0.4.x 的 adapter 契约未变，
> 冷启动只差"版本门 + 真机跑通"；**0.4.0 本人不部署**（9 哥要求等 0.4.2）。

## 1. 分工

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| **纯用户视角智能体**（fresh context） | 只按上游 README/docs/release notes 在 `<lan-ip>` 部署 **vanilla 0.4.2**；如实记录所有卡点 | 不读本仓库 docs/handoff/retro/AEC 记录；不改设备；不碰 `.168` |
| **Lead（我）** | 配参数（密钥/设备/音频/AEC/音量）；构建下游镜像（adapter 层）；跑回归；判断哪些问题回写 issue | 不替它"提示"上游文档该怎么做 |

## 2. 预检（Lead，5 分钟）

    ssh yes999zc@<lan-ip> '
      cd ~/rui && docker compose ps
      cp -a .env ~/rui/.env.bak-0.4.2-$(date +%Y%m%d-%H%M%S)
      cp -a config/config.yaml ~/rui/config/config.yaml.bak-0.4.2-$(date +%Y%m%d-%H%M%S)
      cp -a docker-compose.yml ~/rui/docker-compose.yml.bak-0.4.2-$(date +%Y%m%d-%H%M%S)
      docker images | grep -E "rui"
    '

预期基线：当前 0.3.4 下游镜像在线、Dot2 注册、`devices=1`。备份后旧镜像与 compose 都还在，可回滚。

## 3. 步骤 A：纯用户视角部署 vanilla 0.4.2

给智能体的任务书要点（照旧：只读上游材料）：

- 上游：`gh repo yes999zc/rui` tag `v0.4.2`（或 release notes）+ `docs/deploy.md` + `deploy/install.sh` + `docker-compose.yml`；
- 目标：`~/rui` 上把实例升到 0.4.2（保留 `.env` 密钥与 `config/`、`data/`、`dot2-secrets/`）；
- 必查：`docker compose ps` healthy、`/health` version=0.4.2、`rui doctor`、`config_version` 是否需要迁移、
  compose 是否被上游更新（0.3.4 的经验：文档里的升级流程**不会**更新 compose，硬化会静默丢失）、
  `chown 1000:1000 config data` 是否要做、`RUI_UID/GID` 是否保持 1000（uid 0 回滚会 crash-loop）；
- 产出：命令序列 + 卡点分级清单 + 最终健康证据；不打印任何 secret。

## 4. 步骤 B：Lead 加 adapter 层

检查 0.4.2 是否仍接受当前 manifest/entrypoint（0.4 计划把钩子收敛为正式插件加载）：

1. 拉源码对照 `docs/socket-plug-v1.md` 与 `src/rui/device/loader.py`：`RUI_ADAPTER_PLUGINS` 是否仍在、是否改名/收紧；
2. 契约自测先跑（不部署）：

       docker run --rm --entrypoint python -e PYTHONPATH=/opt/rui-plugins \
         -v <repo>/src:/opt/rui-plugins:ro ghcr.io/yes999zc/rui:0.4.2 \
         -m rui plug-test /opt/rui-plugins/rui_dot2_bridge \
         --adapter rui_dot2_bridge.adapter:build --json

   要求 0 FAIL；WARN/SKIP 与 0.3.4 时应一致（host-free + inbound）。
3. 构建下游镜像（必须 x86_64 宿主；本机 BuildKit 并发会卡，用 legacy builder）：

       ssh .34 'cd ~/rui-dot2 && DOCKER_BUILDKIT=0 docker build \
         -f deploy/downstream/Dockerfile --build-arg RUI_IMAGE=ghcr.io/yes999zc/rui:0.4.2 \
         -t rui-dot2:0.4.2 .'

4. 装 manifest（第二读者）与切 compose：

       sh deploy/downstream/install-plug-manifest.sh ~/rui/data
       # compose 里 RUI_IMAGE=rui-dot2:0.4.2；保留 0.3.4 的 compose 备份
       docker compose up -d --force-recreate

5. 参数（`.env` / config，0.3.4 已验证值）：

       audio.playback.rate=48000, channels=1, prebuffer_ms=0
       audio.capture: rate=16000, channels=1, mic_channels=1, ref_channels=0, bits=16
       audio.aec.enabled=false            # AEC 归设备
       DOT2_SECRETS_DIR=/opt/rui-secrets
       DOT2_EQ_DIR=/opt/rui-secrets/eq
       DOT2_LOUDNESS_FILE=/var/lib/rui/loudness.json
       DOT2_AEC_ENABLED=1
       DOT2_CONTROL_PORT=8872
       DOT2_VOLUME_CAP=100
       DOT2_PLAYBACK_GATE_MS=2500         # 播放后静音窗口（自听修复）

6. 设备配置用官方 API：`POST /api/v1/setup/device`，`adapter=dot2`，`id=dot2`，`listen_port` 由 manifest 缺省 8870，
   `token_env=DOT2_EM_TOKEN`（实际 token 从 `dot2-secrets/device_token` 读）。

## 5. 验收（全部通过才算冷启动成功）

| 项 | 判据 |
|---|---|
| 插件 | `/health` → `adapter_plugins.registered=["dot2"]`、`warnings=0`；`/api/v1/plugs` 有 `dot2-echomuse` source=user |
| 设备 | `devices=1`、`enabled_devices=1`、`audio_plane=true`、`speaker_connected=true`；`ss` 有 8080/8870/8872；日志 `dot2 link: dot2 registered` |
| 播放 | `playback_s` 增长、`playback_gap_count=0`、`max_gap=0`、无 "wire is 48000" 报错 |
| 自听 | 长回复（>15 s）后无第二段 turn；follow-up 窗口回放的是被静音的尾巴 |
| 恢复 | `docker compose restart` 后 ≤60 s 设备重连 |
| 契约 | `plug-test` 0 FAIL |
| 真人 | 唤醒→回合；播放中打断（服务端停推 ≤300 ms）；音量/LED 正常 |

## 6. 回滚

    cd ~/rui
    # 回 0.3.4 下游镜像
    sed -i 's/^RUI_IMAGE=.*/RUI_IMAGE=rui-dot2:0.3.4/' .env
    cp docker-compose.yml.bak-0.4.2-* docker-compose.yml
    docker compose up -d --force-recreate
    # 回 vanilla 0.4.2 / 0.3.4 同理；0.2.2 仍在本地可回

判据：/health healthy 且版本正确；设备重连（旧镜像无插件时设备会离线重试，属预期）。

## 7. 已知上游坑（0.3.4 冷部署实测，0.4.2 重点复核）

1. 升级文档不更新 `docker-compose.yml` → 0.4.x 的硬化会静默丢失；
2. 文档 `scripts/backup.sh` 以普通用户跑会因 root 拥有的 `config.last-good.yaml` 失败；
3. `chown 1000:1000 config data` 容易漏，症状像"升级坏了"；
4. `RUI_UID=0` 的回滚配方在 `cap_drop: ALL` 下 crash-loop，必须保留 1000；
5. `install.sh` 重跑不会更新已有 `.env` 的 `RUI_IMAGE`，裸 `up -d` 会静默降级；
6. 容器内 `doctor` 报 `container_runtime: none` 是预期，不是故障；
7. `docker-publish` 失败时 tag 有了但镜像没有（0.4.0 就是这样）——**tag ≠ 可部署**。
