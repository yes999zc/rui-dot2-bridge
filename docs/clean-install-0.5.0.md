# rui 0.5.0 干净安装测试手册（等 v0.5.0 发布后执行）

> 触发条件：`v0.5.0` tag 存在 **且** `ghcr.io/yes999zc/rui:0.5.0` 的 manifest 可拉（拿到 digest）。
> 9 哥要求：v0.5.0 是对外发布版（他会从这版开始宣传），所以这次必须是**完全干净安装**：
> 派一个**只负责安装**的智能体（零项目背景、只读上游材料），装完由我（Lead）填写配置让它工作。全程 `.34`。

## 一、干净的定义（Lead 在派单前执行）

1. **保留备份，不保留旧状态**：
   - 把现有 `~/rui` 整体改名归档：`mv ~/rui ~/rui-pre-0.5.0-<ts>/`（内含 `.env`、`config/`、`data/`、`dot2-secrets/`、compose 备份），另留
     `~/rollback-switch-0.4.2-*`、`~/rui-backup-*`、`docker save` 的 `rui-dot2:0.4.2` 镜像 tarball（如果还没存）。
2. **清镜像**：`docker compose down`（在旧目录里）→ 删除所有 `rui*` 镜像（`docker rmi ghcr.io/yes999zc/rui:* rui-dot2:*`），
   `docker image prune -f` 清 dangling；`docker system df` 记录。**tag ≠ 可部署**，只认 digest。
3. **清构建目录**：`rm -rf ~/rui-dot2`（rsync 新仓库上去再建），避免旧源码/旧镜像层混淆。
4. **保留设备凭据**（后面配置阶段要用，不属于"安装"）：`~/rui-pre-0.5.0-*/dot2-secrets/` 原样保留在备份目录里，
   等安装完成后由 Lead 拷回新 `~/rui/dot2-secrets/`（0600）。
5. 记录：`before-*.txt/json`（容器、镜像、health、端口）。

## 二、派单：安装专用智能体（fresh context，零背景）

任务书要点（不要给任何本仓库背景，不提 dot2/桥/设备/我们修过的坑）：

- 目标机 `yes999zc@<lan-ip>`（ssh 公钥免密），Docker + compose v2 可用；`~/rui` **不存在**（全新）。
- 只读上游 `github.com/yes999zc/rui` 的 **v0.5.0**（README、`docs/deploy.md`、`deploy/install.sh`、compose、CHANGELOG、release notes）。
- 任务：从零把 vanilla rui 0.5.0 装起来（Docker、host 网络、容器名 `rui`），做到 `/health` version=0.5.0、容器 healthy、
  数据目录可写、控制台 8080 可访问；**不配置任何设备/adapter/密钥外的业务参数**（密钥能给就给，不能给就留空并说明）。
- 如实记录卡点（blocking/confusing/cosmetic），区分"上游文档问题"与"本机环境问题"；不得打印密钥；
  不碰 `<lan-ip>`；不做设备侧操作；破坏性操作前留副本。
- 产出：`/tmp/rui-0.5.0-clean-install/report.md` + 把摘要发回 Lead。

## 三、Lead 接手：填配置让它工作

1. **凭据**：从备份拷回 `dot2-secrets/`（0600），`.env` 按旧模板重建（LLM/ASR/HA/设备 token 等，值从备份 `~/rui-pre-0.5.0-*/​.env` 取，**不打印**）。
2. **契约自测先跑**（不部署）：
   ```
   docker run --rm --entrypoint python -e PYTHONPATH=/opt/rui-plugins \
     -v ~/rui-dot2/src:/opt/rui-plugins ghcr.io/yes999zc/rui:0.5.0 \
     -m rui plug-test /opt/rui-plugins/rui_dot2_bridge --adapter rui_dot2_bridge.adapter:build --json
   ```
   要求 0 FAIL；WARN/SKIP 与 0.3.4/0.4.x 基线一致（15/0/2/5）。
3. **兼容门槛（已与 rui 对齐 2026-10-25 16:37）**：`rui_compat` 已改为 **`>=0.3.0 <0.6.0`**（plugin 1.1.2）。
   rui 确认 0.5.0 **无设备/插件契约变更**（Phase 2 全部在 rui 内部），只需上界；0.5.0 上先跑 plug-test（要求 0 FAIL、
   WARN/SKIP 与基线一致）→ 再构建下游镜像 `rui-dot2:0.5.0`（x86_64 宿主、`DOCKER_BUILDKIT=0`）。
   发布公告在 rui#7（tag + GHCR digest + 平台）。
4. **装 manifest 两份**：镜像内 + `data/plugs/dot2-echomuse/`；设备配置里 `room` 要带上（0.5.0 起 `multi_device.enabled=true` 时必填）；`.env` 恢复 `DOT2_*`（含 `DOT2_PLAYBACK_GATE_MS=2500`、
   `DOT2_VOLUME_CAP=100`）；`config.yaml` 由官方 `POST /api/v1/setup/device` 写入（`adapter=dot2`、`id=dot2`、`listen_port=8870`），
   `devices[].volume=100`，`audio.playback.rate=48000`、capture 16k/1/0 ref、`aec.enabled=false`；
   0.5.0 若已交付 `devices[].audio.voice.post_playback_mute_ms`（#7 B 路线）→ 设 2500 并**撤掉** adapter 门控，否则保留门控。
5. **验收**（沿用 0.4.2 清单）：/health 字段、`/devices/dot2/status` = 8870 / `DOT2_EM_TOKEN`、plug-test 0 FAIL、
   8080/8870/8872、`dot2 link: dot2 registered`、`playback_gap_count=0`、长回复无自听、`docker compose restart` 后 ≤60 s 重连。
6. **真人段**：等新会话统一调度（9 哥会安排），届时再做唤醒/打断/误唤醒基线。
7. **回报**：安装阶段的问题由安装 agent 报告后，我整理成 **独立 issue** 发给 rui（每个问题一个话题，带复现与期望）；
   通过项与契约结果回写 #7；多设备/仲裁相关回写 #11。

## 四、回滚

- 回 0.4.2：旧 `~/rui-pre-0.5.0-*/` 恢复为 `~/rui` + `docker load` 0.4.2 镜像 tarball + `docker compose up -d`；
- 保留：0.4.2 现场快照、旧 `.env`/`config`/compose、`rui-dot2:0.4.2` 镜像。

## 五、注意

- 0.5.0 是"对外发布版"，任何看起来像上游 bug 的东西（不只是文档）都要先确认可复现、再开 issue，避免噪声；
- **tag 有了 ≠ 镜像可拉**（0.4.0 就是这么翻车的），先 HEAD manifest 拿 digest；
- 不要改 rui 源码；不要碰 `.168`；密钥不外传、不贴出。
