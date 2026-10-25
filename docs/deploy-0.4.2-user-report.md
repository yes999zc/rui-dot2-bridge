# rui 冷启动升级报告：<lan-ip> → 上游 v0.4.2（附 Lead 接手后的最终状态）

- 执行者：`coldstart-user`（fresh-context 运维执行者，无项目背景）
- 本 agent 执行窗口：2026-10-25 15:45–15:55 CST（Asia/Shanghai）
- 目标机：`yes999zc@<lan-ip>`（`horner-nas`，Linux 6.18.18 x86_64，Docker 28.5.2，docker compose v2.40.3）
- 上游基准：`github.com/yes999zc/rui` tag `v0.4.2`，commit `3c4af3e7e320b190300ccc7959c4fd6ec61c0fe8`（2026-10-25 15:21 +08）
- 上游运行镜像：`ghcr.io/yes999zc/rui:0.4.2`，digest `sha256:3791ccfdbd4429bba76eb57776d2b9047538ff3a8766f725e4c2ee19f1d4096b`
- 备份：`/tmp/rui-coldstart-backup/rui-20261025-154903/`（270 MB，76 文件，`chmod -R go-rwx`）
- **最终状态（15:54 观察，Lead 于 15:53 接手完成）**：容器 `rui` 使用本地下游镜像 `rui-dot2:0.4.2`（基于 0.4.2 构建、内置 dot2 bridge），`docker compose ps` = **healthy**，`/health.version=0.4.2`，`config_version=2`，`devices=1`、`enabled_devices=1`、`device_errors=0`、`audio_plane=true`、`speaker_connected=true`，8080/8870/8872 监听，`RestartCount=0`。

> **核心结论（请原样引用）：**
> **“原地把下游 dot2 安装切到 vanilla 0.4.2 时，容器必然 unhealthy（enabled device 不可驱动），上游文档没有覆盖这种情形；vanilla 版本身安装成功。”**

补充事实（以免误读）：vanilla 0.4.2 镜像 + 上游 compose 在我的阶段（15:49–15:52）成功安装并运行，`/health.version=0.4.2`、`config_version=2`、数据完好、无 crash-loop，但因为本机唯一的 enabled 设备是下游 `dot2` adapter，vanilla 镜像不含该 adapter、宿主插件 manifest 又声明 `rui_compat < 0.4.0`，healthcheck 必然失败。**15:53 Lead 接手**后改用本地构建的下游镜像 `rui-dot2:0.4.2`（内置 `rui_dot2_bridge`）并把插件 manifest 升到 1.1.1，容器随即 healthy。final healthy 属于下游路径，不属于 vanilla 路径。

---

## 0. 验收表

| 验收项 | vanilla 阶段（本 agent，15:49–15:52） | 最终状态（Lead 接手后，15:54 观察） |
|---|---|---|
| 镜像/版本 | ✅ `ghcr.io/yes999zc/rui:0.4.2`，label `v0.4.2` | ✅ 本地 `rui-dot2:0.4.2`（base 0.4.2，revision `3c4af3e`）；`/health.version=0.4.2` |
| `/health` | ✅ HTTP 200，`status=ok`，`config_version=2` | ✅ 同上，且 `devices=1`、`device_errors=0` |
| 容器 `healthy` | ❌ **unhealthy**（enabled device 不可驱动） | ✅ **healthy** |
| 音频面/设备 | ❌ `audio_plane=false`、`speaker_connected=false` | ✅ `audio_plane=true`、`speaker_connected=true`、`speaker_device=dot2`、`adapter_plugins.registered=["dot2"]` |
| 生产数据 | ✅ `config/`、`dot2-secrets/`、`data/models/` 与备份逐字节一致 | ✅ secrets/models 仍逐字节一致；`config.yaml` 由 Lead 更新并自动留备份（未丢） |
| 密钥泄露 | ✅ 无 | ✅ 无 |
| crash-loop | ✅ 无（`RestartCount=0`） | ✅ 无（`RestartCount=0`，日志无 traceback/panic） |
| 8080 | ✅ 监听 | ✅ 监听；另 8870/8872（dot2）也监听 |

---

## 1. 环境与初始状态（升级前，15:45–15:49）

### 1.1 主机与工具

```text
uname: Linux horner-nas 6.18.18.c788-trim #788 SMP PREEMPT_DYNAMIC x86_64
docker: 28.5.2 (build ecc6942)
docker compose: v2.40.3
user: yes999zc (uid=1000, gid=1001)，在 docker 组
磁盘: / 63G，可用 37G
rui doctor（容器内）资源视角: 8 cores / 6.7 GB RAM / 35.5 GB free
```

### 1.2 现状（与任务描述有出入，已如实记录）

- `docker ps -a` 为空 —— 与“容器已停止”一致。
- 但“镜像已被清空”并不完全准确：**残留一个未打 tag 的镜像 `b370ad1df70c`**。检查后确认它是原下游 dot2 镜像：
  - label `org.opencontainers.image.version=v0.3.4`，created 2026-10-25T01:05Z，368 MB；
  - 镜像内 `/opt/rui-plugins/rui_dot2_bridge/` 存在；镜像 Env 含 `PYTHONPATH=/opt/rui-plugins`、`RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build`；
  - 我把它重新 tag 成 `rui-dot2:0.3.4` 作为回滚镜像（只加 tag，未删除任何镜像）。
- `~/rui` 顶层：`.env`(600)、`config/`、`data/`、`dot2-secrets/`(700)、`scripts/`、`config.example.yaml`、`docker-compose.yml`(1999 B，下游 dot2 改写版)、`docker-compose.yml.vanilla-0.3.4`(4267 B)、`.env.bak.*`。
- 活动 compose 是**下游 dot2 版**：`image: ${RUI_IMAGE:-rui-dot2:0.3.4}`，额外挂载 `./dot2-secrets:/opt/rui-secrets:ro`，并设置 `PYTHONPATH=/opt/rui-plugins`、`RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build`、`DOT2_*`。
- `.env`（600）共 **18 个 key**。非密钥相关行：`RUI_IMAGE=rui-dot2:0.3.4`、`RUI_UID=1000`、`RUI_GID=1000`，以及 7 个 `DOT2_*` 变量。**其余值未读取、未打印。**
- `config/config.yaml`：`version: 2`（只核对了 version 行与结构，未打印全文），唯一设备 `id=dot2`、`adapter=dot2`、`enabled=true`、`room=客厅`、`volume=60`、`led.enabled=false`；`web.port=8080`。
- `data/plugs/dot2-echomuse/`：下游插件 `version: 1.1.0`、`rui_compat: ">=0.3.0 <0.4.0"`、`runtime: {mode: in-process, entrypoint: "rui_dot2_bridge.adapter:build", listen_port: 8870, token_env: DOT2_EM_TOKEN}`。
- 模型齐全：`data/models/` 27 个文件（SenseVoice、Silero VAD、KWS），269 MB。
- `data/service.log` 升级前最后几行（15:44 CST）显示下游镜像仍在跑、dot2 设备链路是连着的（`dot2 link: wss listening on 8870`、`dot2 registered`、`data plane connected`），随后因停机 `control/data plane closed`。说明这台机器是**真实在用的下游 dot2 集成**，不是纯 vanilla。
- `config/` 里另有历史备份（`config.last-good.yaml`、`config.yaml.bak-*` 等），本 agent 未使用、未修改。

### 1.3 上游材料核对

| 项 | 核对结果 |
|---|---|
| clone | `git clone --depth 1 --branch v0.4.2 https://github.com/yes999zc/rui.git /tmp/rui-up` 成功；commit `3c4af3e` |
| 上游 `docker-compose.yml` | sha256 `3897bfc9f3c881cb8b35444b4f07717bd0675af3ebbf5e539a5f2d0910bbab27`；与主机已有 `docker-compose.yml.vanilla-0.3.4` **字节完全相同** |
| 上游 `config.example.yaml` | sha256 `8b81193a…`；与主机现有 `config.example.yaml` **字节完全相同** |
| release notes | `docs/releases/v0.4.2.md`：no config migration / no schema change / no audio/runtime change；`0.4.1 → 0.4.2` 只需 pull + recreate |
| CHANGELOG 升级说明 | `0.3.2–0.3.4 → 0.4.x`：no data migration、no chown，pull + recreate |
| 镜像 | `docker pull` 成功；label `version=v0.4.2`、`revision=3c4af3e…`；`RepoDigests=[ghcr.io/yes999zc/rui@sha256:3791ccf…]` |

---

## 2. 时间线、变更与归属

| 时间 (CST) | 执行者 | 动作 | 证据 |
|---|---|---|---|
| 15:49:03 | 本 agent | 备份 `/tmp/rui-coldstart-backup/rui-20261025-154903`（270 MB / 76 文件） | `du`/`find` |
| 15:49:0x | 本 agent | `docker tag b370ad1df70c rui-dot2:0.3.4`（回滚镜像 tag） | `docker images` |
| 15:49:1x | 本 agent | 新增 `docker-compose.yml.dot2-0.3.4.bak`；`docker-compose.yml` 覆盖为上游 v0.4.2（sha `3897bfc9…`）；`.env` 第 8 行改为 `ghcr.io/yes999zc/rui:0.4.2` | sha256 / 脱敏 diff |
| 15:49:11 | 本 agent | `docker compose up -d`（vanilla 0.4.2） | 启动日志 |
| 15:50:30–15:51:43 | 本 agent | healthcheck 持续失败，容器 `unhealthy` | `docker inspect .State.Health` |
| 15:52 | 本 agent | 完成 vanilla 阶段取证（`/health`、logs、doctor、plugs、sha 对比） | 本报告 §4 |
| 15:53:23 | **Lead（接手）** | 恢复下游 dot2 `docker-compose.yml`（1999 B，sha `b409f56e…`，与我保存的旧版一致） | 文件 mtime/sha |
| 15:53:23 | **Lead（接手）** | 备份 `.env` 为 `.env.bak-0.4.2-20261025-155323`，把 `RUI_IMAGE` 改为 `rui-dot2:0.4.2` | 文件 mtime/size |
| 15:52:40–15:53 | **Lead（接手）** | 构建本地下游镜像 `rui-dot2:0.4.2`（base 0.4.2 + 内置 `rui_dot2_bridge`） | `docker image inspect` |
| 15:53:26 | **Lead（接手）** | 更新 `data/plugs/dot2-echomuse/plugin.yaml` → `version: 1.1.1`、`rui_compat: ">=0.3.0 <0.5.0"` | 文件 mtime/内容 |
| 15:53 | **Lead（接手）** | 更新 `config/config.yaml`（5733→5788 B）与 `config.last-good.yaml`，并自动生成 `config.yaml.bak-0.4.2-20261025-155323`（旧 5733 B） | sha256/文件列表 |
| 15:53:27 | **Lead（接手）** | recreate 容器为 `rui-dot2:0.4.2` | `StartedAt=07:53:27Z` |
| 15:54–15:55 | 本 agent（只读） | 观察最终 healthy 状态并记录 | §4.8 |

### 2.1 本 agent 实际改动（vanilla 阶段）

| # | 操作 | 对象 | 说明 |
|---|---|---|---|
| 1 | 新增（tag） | `docker tag b370ad1df70c rui-dot2:0.3.4` | 仅加 tag；未删/未覆盖镜像 |
| 2 | 备份 | `/tmp/rui-coldstart-backup/rui-20261025-154903/` | `.env`、`.env.bak.*`、compose 两份、`config.example.yaml`、`config/`、`data/`、`dot2-secrets/`、`scripts/`；`chmod -R go-rwx` |
| 3 | 新增文件 | `~/rui/docker-compose.yml.dot2-0.3.4.bak` | 原下游 compose 副本（1999 B） |
| 4 | 覆盖 | `~/rui/docker-compose.yml` | 换成上游 v0.4.2 原版（4267 B，sha `3897bfc9…`）。**注：15:53 已被 Lead 恢复为下游版** |
| 5 | 修改一行 | `~/rui/.env` 第 8 行 | `RUI_IMAGE=rui-dot2:0.3.4` → `ghcr.io/yes999zc/rui:0.4.2`；其余 17 行未动。**注：15:53 已被 Lead 改为 `rui-dot2:0.4.2`，并删除了 `DOT2_PLAYBACK_GATE_MS`；改动前状态保存在 `.env.bak-0.4.2-20261025-155323`** |
| 6 | 运行态 | `docker compose up -d` | vanilla 0.4.2 容器 |

### 2.2 Lead 接手后的改动（我只读观察，未执行）

- `docker-compose.yml` 恢复为下游 dot2 版（1999 B，sha `b409f56e…`；与我保存的 `.dot2-0.3.4.bak` 和备份中的旧 compose 完全一致）。
- `.env`：`RUI_IMAGE=rui-dot2:0.4.2`；备份 `.env.bak-0.4.2-20261025-155323`（573 B，保留我 15:49 的 `ghcr.io/yes999zc/rui:0.4.2` 状态）；当前 17 个 key（删除了 `DOT2_PLAYBACK_GATE_MS`）。
- 新镜像 `rui-dot2:0.4.2`（id `cb291a3cb317…`，368 MB，created 15:52:40，label 仍为 `v0.4.2` / revision `3c4af3e`，含 `/opt/rui-plugins/rui_dot2_bridge`）。
- `data/plugs/dot2-echomuse/plugin.yaml`：`version: 1.1.1`、`rui_compat: ">=0.3.0 <0.5.0"`。
- `config/config.yaml` + `config.last-good.yaml`：5733→5788 B（内容已变），自动备份 `config/config.yaml.bak-0.4.2-20261025-155323`（5733 B）保留旧内容。

### 2.3 未改项（本 agent）

`config/`（我阶段全部 8 文件）、`data/`（除运行产生的日志/ack 外）、`dot2-secrets/`、`~/.env` 的其他行、`data/plugs/`、设备配置、系统设置、防火墙、ssh、systemd。

---

## 3. 可复现命令序列（vanilla 路径，逐条含结果）

> 以下命令在 Mac 上通过 `ssh -o BatchMode=yes yes999zc@<lan-ip> '<cmd>'` 执行；目标机无需 sudo。
> 涉及 `.env` 只做 `grep`/`sed` 定点操作，不 `cat` 整份文件。

### 3.1 侦察（只读）

```bash
ssh -o BatchMode=yes yes999zc@<lan-ip> 'uname -a; docker --version; docker compose version'
ssh ... 'cd ~/rui && docker ps -a; docker images; ls -la'
ssh ... 'cd ~/rui && grep -nE "^(version|web|devices|providers|audio|tools|music):" config/config.yaml'
ssh ... 'cd ~/rui && grep -nE "^(RUI_IMAGE|RUI_UID|RUI_GID)=" .env'
```

**结果**：SSH 免密可用；容器为空；残留 1 个未 tag 下游镜像；`~/rui` 为下游 dot2 布局；config v2、1 个 `adapter=dot2` 的 enabled 设备。

### 3.2 读上游 v0.4.2（在目标机 clone，然后拷回 Mac 阅读）

```bash
ssh ... 'rm -rf /tmp/rui-up && git clone --depth 1 --branch v0.4.2 \
  https://github.com/yes999zc/rui.git /tmp/rui-up && \
  git -C /tmp/rui-up log -1 --format="%H %s"'
scp -q -r yes999zc@<lan-ip>:/tmp/rui-up /tmp/rui-up   # Mac 侧阅读副本（可选）
```

**结果**：clone 成功，commit `3c4af3e…`；`/tmp/rui-up` 保留未删（Lead 后续要用）。

### 3.3 备份（任何破坏性操作之前）

```bash
ssh ... '
set -eu
TS=$(date +%Y%m%d-%H%M%S)
BK=/tmp/rui-coldstart-backup/rui-$TS
mkdir -p "$BK"
cd ~/rui
cp -a .env .env.bak.* docker-compose.yml docker-compose.yml.vanilla-0.3.4 config.example.yaml "$BK"/
cp -a config data dot2-secrets scripts "$BK"/
chmod -R go-rwx "$BK"
echo "$BK"; du -sh "$BK"; find "$BK" -type f | wc -l
'
```

**结果**：`/tmp/rui-coldstart-backup/rui-20261025-154903`，270 MB / 76 文件；源/备份文件数：config 8=8，data 38=38，secrets 20=20。

### 3.4 恢复回滚镜像 tag（只加 tag）

```bash
ssh ... 'docker tag b370ad1df70c rui-dot2:0.3.4 && docker images rui-dot2'
```

**结果**：`rui-dot2:0.3.4 b370ad1df70c 368MB`。

### 3.5 切到上游 compose + 固定镜像

```bash
ssh ... '
set -eu
cd ~/rui
cp -a docker-compose.yml docker-compose.yml.dot2-0.3.4.bak
cp /tmp/rui-up/docker-compose.yml docker-compose.yml
sed -i "s|^RUI_IMAGE=.*|RUI_IMAGE=ghcr.io/yes999zc/rui:0.4.2|" .env
sha256sum docker-compose.yml
grep -n "^RUI_IMAGE=" .env
docker compose config --quiet && echo "compose config OK"
'
```

**结果**：compose sha256 = `3897bfc9…`（与上游一致）；`.env` 第 8 行 = `RUI_IMAGE=ghcr.io/yes999zc/rui:0.4.2`；`docker compose config` OK。

### 3.6 拉镜像并启动

```bash
ssh ... '
cd ~/rui
docker pull ghcr.io/yes999zc/rui:0.4.2
docker compose up -d
sleep 5
docker compose ps
curl -s -m 5 http://127.0.0.1:8080/health | python3 -m json.tool
'
```

**结果**：`Container rui Created/Started`；`/health` HTTP 200，`version=0.4.2`，`config_version=2`；但 `audio_plane=false`、`speaker_connected=false`、`device_errors=1`。

### 3.7 等健康窗口并取证

```bash
ssh ... '
cd ~/rui
sleep 100
docker compose ps
docker inspect rui --format "Health={{.State.Health.Status}} RestartCount={{.RestartCount}}"
docker compose exec -T rui rui-healthcheck; echo "exit=$?"
docker compose logs --no-color --since 10m rui | grep -Ei "dot2|not drivable|audio plane|incompatible"
'
```

**结果**：90 s start_period 后 `unhealthy`；`Health=unhealthy`、`RestartCount=0`；healthcheck 退出 1，stderr `unhealthy: audio plane not running (1 enabled device(s))`。

### 3.8 最终状态观察（15:54，只读；此时 Lead 已接手）

```bash
ssh ... 'cd ~/rui && docker compose ps'
ssh ... 'cd ~/rui && curl -s http://127.0.0.1:8080/health | python3 -m json.tool'
ssh ... 'docker inspect rui --format "Image={{.Config.Image}} Health={{.State.Health.Status}} RestartCount={{.RestartCount}}"'
ssh ... 'ss -ltn | grep -E ":(8080|8870|8872)\b"'
```

**结果**：`healthy`；`Image=rui-dot2:0.4.2`；`/health.version=0.4.2`、`audio_plane=true`、`speaker_connected=true`；`RestartCount=0`；8080/8870/8872 监听。

---

## 4. 证据

### 4.1 vanilla 阶段：`/health`（HTTP 200）

```json
{
  "status": "ok", "version": "0.4.2",
  "config_version": 2, "config_version_supported": 2,
  "devices": 0, "enabled_devices": 1, "device_errors": 1,
  "adapter_plugins": {"registered": [], "warnings": 0},
  "audio_plane": false, "speaker_connected": false,
  "speaker_device": "", "speaker_last_seen": null
}
```

### 4.2 vanilla 阶段：`docker compose ps`

```text
NAME   IMAGE                        COMMAND      SERVICE   STATUS
rui    ghcr.io/yes999zc/rui:0.4.2   "rui serve"  rui       Up 3 minutes (unhealthy)
```

### 4.3 vanilla 阶段：启动日志（已过滤 token/secret/key）

```text
2026-10-25 07:49:13,244 WARNING rui.web.api.app: device dot2 is configured but not drivable: unknown adapter: 'dot2' (this build has: mock, xiaoai)
2026-10-25 07:49:16,944 WARNING rui.web.api.app: audio plane did not start: no device this build can drive
INFO:     Uvicorn running on http://0.0.0.0:8080
```

### 4.4 Lead 证据 #1：`/api/v1/plugs`

- 未带 token：`HTTP 401 {"detail":"bad or missing X-Auth-Token"}`（符合设计）。
- 带 token（token 只进 shell 变量，未打印）时：

**vanilla 阶段（15:52）** `GET /api/v1/plugs` 的关键项（无密钥）：

```json
{
  "source": "user", "available": false,
  "error": "/var/lib/rui/plugs/dot2-echomuse/plugin.yaml: plugin 'dot2-echomuse' is incompatible: it requires RUI >=0.3.0 <0.4.0, but this build is 0.4.2; refusing to load it",
  "devices": []
}
```

**最终状态（15:55，Lead 重装 manifest 后）**：

```json
{
  "id": "dot2-echomuse", "name": "Echo Dot 2 (EchoMuse) plug",
  "version": "1.1.1", "trust": "trusted", "class": "B",
  "adapter": "dot2", "transport": "inbound", "source": "user",
  "available": true, "error": null, "devices": ["dot2"]
}
```

### 4.5 Lead 证据 #2：`data/plugs/dot2-echomuse/plugin.yaml` 两行原文

**我阶段（15:52）**：

```yaml
4:version: 1.1.0
5:rui_compat: ">=0.3.0 <0.4.0"
```

**最终状态（15:55，Lead 已更新）**：

```yaml
4:version: 1.1.1
5:rui_compat: ">=0.3.0 <0.5.0"
```

→ 这直接回答了“下一步要不要重装 manifest”：**要**。vanilla 0.4.2 拒绝 `<0.4.0` 的 manifest；Lead 已把它更新为 1.1.1 / `<0.5.0`。

### 4.6 Lead 证据 #3：`rui doctor` + 8080 + RestartCount

**vanilla 阶段（15:52）**：

```text
rui doctor  verdict=degraded
  [PASS] arch: x86_64
  [PASS] cpu_cores: 8
  [PASS] ram_gb: 6.7
  [PASS] disk_free_gb: 35.5
  [WARN] container_runtime: none  -> install Docker for the one-command deploy
  [PASS] llm_key: RUI_LLM_KEY from environment
  [PASS] capture_layout: 16000 Hz requested / 1 ch / S16_LE on the device ...
  [PASS] playback_layout: 48000 Hz / 1 ch, queue 200 ms, fade 120 ms
  [PASS] aec_rtf: 0.000
  [PASS] wake_runtime: sherpa-onnx 2.0.0 imported; silero VAD loaded
  [PASS] wake_path: preroll 500 ms ...
  [PASS] ack_wav: 490 ms
  [PASS] device:dot2: <lan-ip>:22 ssh
  AEC load: rtf=0.000  (0.0% of one core)
  device capacity: ~18 speaker(s)
LISTEN 0 2048 0.0.0.0:8080 0.0.0.0:*
Health=unhealthy RestartCount=0 Status=running
```

**最终状态（15:55）**：doctor 仍为 `verdict=degraded`，除 `[WARN] container_runtime: none`（镜像内没有 docker CLI 的常态）外全部 PASS；`device:dot2` PASS；8080 监听；`RestartCount=0`、`Health=healthy`；另 8870/8872 监听。

注意：vanilla 阶段 doctor 对 `device:dot2` 打 PASS，但 Docker health 是 `unhealthy`、日志说 adapter 不存在 —— 这两个“健康”信号不一致（见卡点 C3）。

### 4.7 Lead 证据 #4：容器/宿主改动与 sha256 对比

容器可写层（`docker diff rui`，vanilla 阶段）：

```text
A /data
A /data/models
A /config
```

→ 只有 bind mount 挂载点，容器根文件系统无其他改动（read_only 生效）。

**本 agent 的宿主改动**：

| 对象 | 改动 | 校验 |
|---|---|---|
| `~/rui/docker-compose.yml` | 覆盖为上游 v0.4.2 | sha256 `3897bfc9…`（与上游一致） |
| `~/rui/.env` | 仅第 8 行 `RUI_IMAGE` 改变 | 与备份做脱敏 diff：只有该行不同 |
| `~/rui/docker-compose.yml.dot2-0.3.4.bak` | 新增（旧 compose 副本） | — |
| `docker tag` | `b370ad1df70c → rui-dot2:0.3.4` | 仅加 tag |

**sha256 对比（本 agent 阶段完成时，与 `/tmp/rui-coldstart-backup/rui-20261025-154903/` 比）**：

```text
config/:        ALL files byte-identical（8/8 文件）
dot2-secrets/:  ALL files byte-identical（20/20 文件）
data/models:    ALL 27 files byte-identical
data/:          38/38 文件同数量；仅 2 个运行产物变化：ack.wav、service.log
```

**最终状态复核（15:55，Lead 接手后，与同一备份比）**：

```text
dot2-secrets/:  ALL files byte-identical（20/20）
data/models:    ALL 27 files byte-identical
config/:        config.yaml 与 config.last-good.yaml 已被 Lead 更新（5733→5788 B），
                旧内容保存在 config/config.yaml.bak-0.4.2-20261025-155323（5733 B）
data/:          38/38 文件同数量；CHANGED 仅 3 个：
                ./ack.wav                （运行产物，重新生成）
                ./service.log            （正常追加）
                ./plugs/dot2-echomuse/plugin.yaml （Lead 更新 1.1.0→1.1.1、compat <0.4.0→<0.5.0）
```

→ **密钥/TLS/模型字节未变；配置内容有 Lead 的显式更新且保留了旧备份；数据未丢失。**

### 4.8 最终 healthy 证据（15:54–15:55 只读观察）

```text
docker compose ps:
NAME   IMAGE            COMMAND      SERVICE   STATUS
rui    rui-dot2:0.4.2   "rui serve"  rui       Up About a minute (healthy)

docker inspect:
Image=rui-dot2:0.4.2 Status=running Health=healthy RestartCount=0

/health:
{"status": "ok", "version": "0.4.2", "config_version": 2,
 "devices": 1, "enabled_devices": 1, "device_errors": 0,
 "adapter_plugins": {"registered": ["dot2"], "warnings": 0},
 "audio_plane": true, "speaker_connected": true, "speaker_device": "dot2"}

监听:
LISTEN 0 2048 0.0.0.0:8080
LISTEN 0 100  0.0.0.0:8870
LISTEN 0 100  127.0.0.1:8872

crash-loop 扫描: 0（5 分钟日志内无 traceback/panic/restarting）
```

说明：最终 healthy 使用的是**本地构建的下游镜像 `rui-dot2:0.4.2`**（base 0.4.2 + 内置 `rui_dot2_bridge` + 更新后的插件 manifest），不是 vanilla `ghcr.io/yes999zc/rui:0.4.2` 单独运行的结果。该镜像未推送到 registry，可复现性依赖下游构建输入（本任务不允许读取 `~/rui-dot2*` 源码目录，故不在本报告范围内）。

---

## 5. 卡点清单（分级 + 归因）

### BLOCKING

**B1. vanilla 0.4.2 无法驱动本机唯一 enabled 设备（dot2），healthcheck 必然失败。**
- 现象：vanilla 阶段 `docker compose ps` = `unhealthy`；healthcheck 报 `audio plane not running (1 enabled device(s))`；`audio_plane=false`、`speaker_connected=false`。
- 根因链（缺一不可）：
  1. 生产 `config/config.yaml` 唯一设备 `adapter: dot2`；
  2. upstream vanilla 0.4.2 镜像不含 `rui_dot2_bridge`（只有 `mock`、`xiaoai`），vanilla compose 也没设置 `RUI_ADAPTER_PLUGINS`；
  3. 宿主 `data/plugs/dot2-echomuse` 插件声明 `rui_compat: ">=0.3.0 <0.4.0"`，0.4.2 主动拒绝加载。
- 为什么是 blocking：`deploy/rui-healthcheck.py` 的规则是「有 enabled device 时必须 `audio_plane=true` 且 `speaker_connected=true`」；用户又明确要求不改设备配置（不能把 dot2 置 disabled），所以“vanilla + 现有 config”无法 healthy。
- 归因：**这台机器历史配置带来的问题**（下游 dot2 集成），不是 vanilla 0.4.2 的安装/运行缺陷；上游文档也未覆盖“下游定制安装 → vanilla”的迁移。
- 最终状态：Lead 用**下游 0.4.2 镜像**（`rui-dot2:0.4.2`）+ manifest 1.1.1 解决，容器已 healthy。**vanilla-only 路径仍然 blocking。**

### CONFUSING（上游文档/工具）

**C1. 升级文档假设 compose 是上游原版，未覆盖下游改写版。**
- `docs/deploy.md` §3.6 只说：`.env` 固定 `RUI_IMAGE` → `docker compose pull && docker compose up -d`；
- 本机 compose 是下游 dot2 改写版（额外卷/环境变量/镜像名）。照文档只改 `.env` 再 `up -d`，用的仍是 dot2 compose，只是镜像变量换成 vanilla 0.4.2：容器会带着 `PYTHONPATH=/opt/rui-plugins`、`RUI_ADAPTER_PLUGINS`、dot2-secrets 卷启动，而模块不存在 → 同样 unhealthy，且更难排查。
- 归因：**上游文档问题**（缺少“非 vanilla compose 的升级/替换流程”）。

**C2. `install.sh` 的“重跑安全 / 已有文件不覆盖”对定制安装是陷阱。**
- 读 `deploy/install.sh` 后确认：`[1/5]` 存在 `docker-compose.yml` 就保留（line 118–122），`[3/5]` 存在 `.env` 就保留且**不会更新 `RUI_IMAGE`**（line 187–217），只在 `[5/5]` 用 `RUI_IMAGE="$IMAGE" docker compose ... up -d` 做一次性覆盖（line 330）。
- 因此直接在 `~/rui` 重跑文档中的 `install.sh` 升级：这一次能拉到 0.4.2，但 `.env` 仍写着本地-only 的 `rui-dot2:0.3.4`；重启/reboot/下次裸 `docker compose up -d` 会指向不存在的镜像/回退到 dot2 compose。
- 归因：**上游脚本行为 + 文档未覆盖**（对 pristine install 没问题，对本机历史配置有坑）。

**C3. `rui doctor` 的 PASS 与 Docker health 结论冲突。**
- 同一个容器：`doctor` 输出 `verdict=degraded` 且 `[PASS] device:dot2: <lan-ip>:22 ssh`，但 Docker health 是 `unhealthy`，日志明确 `unknown adapter: 'dot2'`。
- 新用户按 §3.5 的 `doctor` 自检会误以为设备没问题。doctor 的 device 项是历史 SSH 探测，不检查 adapter 是否可构建。
- 归因：**上游工具/文档问题**（自检信号不一致，未说明“SSH 通 ≠ 可驱动”）。

**C4. `/health.status=ok` 与容器 `unhealthy` 语义容易混淆。**
- `/health` 的 `status` 只表示控制面 HTTP 正常；容器健康还需要 `audio_plane`、`speaker_connected`。`deploy.md` §3.5 提了 `audio_plane=true`，但没有把“配了设备时 healthcheck 还要求 speaker_connected”写进验收表；runbook §1 有相关说明，但两处分散。
- 归因：**上游文档问题**（健康语义分散、验收表不完整）。

**C5. v0.4.2 release notes 只写 0.4.1→0.4.2，0.3.4→0.4.2 的升级说明只在 CHANGELOG。**
- 本机是 0.3.4 下游；没有找到“0.3.x downstream → 0.4.x vanilla”的迁移文档，也没有 `rui-dot2-bridge` 的 0.4.x 兼容矩阵/升级步骤（README 只有一句 “dot2 via external rui-dot2-bridge”，无链接/无版本要求）。
- 归因：**上游文档问题**（缺失关键迁移路径）。

### COSMETIC

**K1. `docs/releases/v0.4.2.md` 仍留有发布占位符**：镜像 digest 表格是 `sha256:<fill after the 0.4.2 publish>`，Upgrade smoke 是 `<fill after the upgrade smoke>`；实际 GHCR digest 已可读到。归因：上游文档。
**K2. 镜像“已清空”只清掉了 tag，留下 dangling 下游镜像**；初始状态描述与实际不完全一致。归因：本机历史状态。
**K3. `.env` 里 `DOT2_*` 变量在 vanilla 0.4.2 下已无消费者**（env_file 仍注入，无害但会让人以为还在用）。归因：本机历史配置（Lead 接手后已删除 `DOT2_PLAYBACK_GATE_MS`）。
**K4. `~/rui/console-token.txt` 不存在**（0.4.1+ 的新约定；手动升级不会自动生成）。可用 `docker compose exec -T rui rui token` 找回；不影响本次验收。归因：升级方式（手动 vs install.sh）+ 本机历史。
**K5. `rui doctor` 在容器内报 `[WARN] container_runtime: none -> install Docker for the one-command deploy`**：容器里本来就没有 docker CLI，这是误导性的自检项。归因：上游工具。

---

## 6. 回滚方案

> 当前最终状态已经是 healthy 0.4.2，通常不需要回滚。**不要 `docker compose down -v`。**

### 6.1 回滚到本 agent 的 vanilla 状态（不推荐，会回到 unhealthy）

```bash
BK=/tmp/rui-coldstart-backup/rui-20261025-154903
ssh ... "
cd ~/rui
cp -a \"$BK/docker-compose.yml\" docker-compose.yml    # 上游 v0.4.2 compose
grep -q '^RUI_IMAGE=' .env && sed -i 's|^RUI_IMAGE=.*|RUI_IMAGE=ghcr.io/yes999zc/rui:0.4.2|' .env
docker compose up -d
"
```

### 6.2 回滚到下游 0.3.4（能恢复设备，但版本回到 0.3.4）

```bash
BK=/tmp/rui-coldstart-backup/rui-20261025-154903
ssh ... "
cd ~/rui
docker compose down                     # 删容器，不删 bind mount 数据
cp -a \"$BK/docker-compose.yml\" docker-compose.yml      # 下游 dot2 compose (1999 B)
cp -a \"$BK/.env\" .env                                   # 恢复 RUI_IMAGE=rui-dot2:0.3.4
cp -a \"$BK/config/config.yaml\" config/config.yaml       # 旧 5733 B 配置（旧镜像可能不认新字段）
cp -a \"$BK/data/plugs/dot2-echomuse/plugin.yaml\" data/plugs/dot2-echomuse/plugin.yaml  # 1.1.0 / <0.4.0
docker image inspect rui-dot2:0.3.4 >/dev/null
docker compose up -d
docker compose ps
"
```

注意：
- 回滚镜像 `rui-dot2:0.3.4` 之所以还能用，是因为升级前那个 dangling 镜像 `b370ad1df70c` 还在；若它被 prune，回滚只能重建下游镜像（超出本任务允许的源码范围）。
- 最终状态使用的是本地构建的 `rui-dot2:0.4.2`（未推送 registry）；重建/搬迁时需要下游构建输入，不在本报告范围。
- `config/`、`data/`、`dot2-secrets/` 全程有备份，回滚不会丢数据。

---

## 7. 用户视角复盘：我本来会怎么做、卡在哪

1. **先读 README 的“30 秒快速开始”** → 看到一条 `install.sh` 命令。但这是升级不是新装；README 把升级指向 `docs/deploy.md` §3.6。
2. **按 §3.6 升级** → 只改 `.env` 的 `RUI_IMAGE`，然后 `docker compose pull && docker compose up -d`。
   - **卡点 1**：本机 `docker-compose.yml` 不是上游原版（是下游 dot2 改写版），文档没有说升级前要先替换 compose。
   - **卡点 2**：即使替换了 compose，`RUI_IMAGE` 改成 `ghcr.io/yes999zc/rui:0.4.2` 后，容器起来但 `unhealthy`；日志说 `device dot2 ... not drivable`。
   - **卡点 3**：healthcheck 提示 `docker compose restart rui`，照做无用 —— adapter 根本不存在，重启不会变出模块。
3. **尝试自检** → `docker compose ps` 显示 unhealthy；`/health` 却 `status=ok`；`rui doctor` 又说 device PASS。三个信号互相矛盾，新用户很难判断“到底哪坏了”。
4. **想修** → 看到 `data/plugs/dot2-echomuse` 存在，想“那就重装插件”；但 `plugin.yaml` 的 `rui_compat: ">=0.3.0 <0.4.0"` 明确排除了 0.4.2。README 只说 dot2 走“external rui-dot2-bridge”，没有 0.4.x 的安装/升级说明。
5. **按用户边界** → 不改设备配置、不连设备网络、不动 `data/plugs`，所以正确动作是**停在“版本正确 + 数据完好 + 容器 unhealthy”**，把 B1 作为 blocking 上报，而不是把 dot2 设备 disable 掉来骗过 healthcheck。
6. **我实际做的最终动作**：换上游 compose、只改 `.env` 第 8 行、拉 0.4.2 镜像、起容器、取证、写本报告；没有回滚、没有改设备配置、没有碰设备网络。
7. **Lead 接手后的真实修复路径**（不是我做的，仅记录）：用 0.4.2 作为 base 重建下游 `rui-dot2:0.4.2`（内置 bridge）→ 更新插件 manifest 到 1.1.1 / `<0.5.0` → 恢复下游 compose 与 `RUI_IMAGE=rui-dot2:0.4.2` → recreate。这说明：对这类下游安装，**“升级 rui 版本”和“保留设备能力”是两个动作**，上游 vanilla 文档只覆盖前者。

---

## 8. 如果你是第一次部署，上游文档还缺什么

1. **“已验证安装 → 升级”章节**：给出“如何判断现有 compose 是否上游原版”的命令（例如 `sha256sum docker-compose.yml` 对比 upstream tag），以及“非原版时先替换 compose 再改 `.env`”的步骤。当前 §3.6 只适用于 pristine install。
2. **下游/定制安装的迁移说明**：明确 vanilla 0.4.x 不支持哪些下游 adapter（至少 dot2）、`rui-dot2-bridge` 的兼容版本矩阵和升级步骤；否则用户只能从日志猜。
3. **“什么算 healthy”的统一说明**：`/health.status=ok` ≠ 容器 healthy；有 enabled device 时 healthcheck 还要求 `audio_plane` 和 `speaker_connected`；`rui doctor` 的 device PASS 只是 SSH 探测，不代表可驱动。最好在 `deploy.md` §3.5 的验收表里补一列。
4. **合法的“无设备/设备不可驱动”模式**：文档说“没有 devices[] 时 audio_plane=false 是正常状态”，但一旦有 enabled device 不可驱动，容器就永久 unhealthy。应提供一种明确、可审计的方式（例如显式 disable device + 文档说明），让运维能区分“配置错误”与“故意的设备离线”。
5. **`install.sh` 的升级边界**：明确它在“已有 `docker-compose.yml` / `.env`”时不会替换它们、不会更新 `RUI_IMAGE`；并给出手动升级 checklist。当前“重跑安全”的措辞会让升级者误以为能自动完成迁移。
6. **preflight 检查**：升级前检查 `.env` 的 `RUI_IMAGE` 是否指向本地-only 镜像（`docker image inspect` 失败）、compose 是否与 upstream tag 一致，并在不满足时明确报错，而不是起一个 unhealthy 容器。
7. **发布物完整性**：`docs/releases/v0.4.2.md` 的 digest/smoke 占位符在发布后填上；README 里的 “dot2 via external rui-dot2-bridge” 给个链接和最低版本要求。
8. **自检信号一致性**：让 `rui doctor` 对“adapter 无法构建 / plug 因 `rui_compat` 被拒”给出错误项，或在输出里明确“device SSH PASS ≠ drivable”。

---

## 9. 未执行 / 交接 / 当前状态

- 本 agent 未回滚、未修改设备配置、未改 `data/plugs`、未连设备网络。
- 本 agent 未安装/升级外部 `rui-dot2-bridge`（超出“只用 upstream yes999zc/rui v0.4.2 材料 + vanilla”的范围；本地 0.3.4 bridge 自身声明 `rui_compat <0.4.0`）。
- 本 agent 未创建 `console-token.txt`（避免额外写密钥文件；需要时用 `docker compose exec -T rui rui token`）。
- 保留：`/tmp/rui-up`（upstream clone）、`/tmp/rui-coldstart-backup/rui-20261025-154903`（备份）、`rui-dot2:0.3.4`（回滚镜像 tag）。
- Lead 接手后完成的最终状态：`rui-dot2:0.4.2` + manifest 1.1.1 → 容器 **healthy**，`/health.version=0.4.2`，`config_version=2`，`audio_plane=true`，`speaker_connected=true`，8080/8870/8872 监听，`RestartCount=0`。
- 因此总体交付 A（healthy + version 0.4.2 + 数据保留）在 15:54 后已达成；但 **vanilla-only 路径仍按 B1 记为 expected blocking**。

---

## 附录 A：关键输出（脱敏）

### A.1 镜像

```text
vanilla: ghcr.io/yes999zc/rui:0.4.2
  label version=v0.4.2  revision=3c4af3e7e320b190300ccc7959c4fd6ec61c0fe8
  RepoDigests=[ghcr.io/yes999zc/rui@sha256:3791ccfdbd4429bba76eb57776d2b9047538ff3a8766f725e4c2ee19f1d4096b]

final: rui-dot2:0.4.2 (本地构建)
  id=sha256:cb291a3cb317...
  label version=v0.4.2  revision=3c4af3e...
  含 /opt/rui-plugins/rui_dot2_bridge
```

### A.2 healthcheck 历史（vanilla 阶段）

```text
2026-10-25T15:50:30+08:00 exit=1 unhealthy: audio plane not running (1 enabled device(s)); `docker compose restart rui` brings it back
2026-10-25T15:50:36+08:00 exit=1 ...
2026-10-25T15:50:42+08:00 exit=1 ...
2026-10-25T15:51:12+08:00 exit=1 ...
2026-10-25T15:51:43+08:00 exit=1 ...
```

### A.3 监听端口

```text
vanilla 阶段: 0.0.0.0:8080（仅控制面）
最终状态:     0.0.0.0:8080 + 0.0.0.0:8870 + 127.0.0.1:8872
```

### A.4 备份清单（76 文件，节选）

```text
.env + .env.bak.* + docker-compose.yml + docker-compose.yml.vanilla-0.3.4
config.example.yaml
config/ (8 files)
data/ (38 files: models 27 + plugs 2 + runtime 9)
dot2-secrets/ (20 files: ca.pem/server.pem/server.key/device_token/eq/*)
scripts/ (3 files: backup.sh restore.sh acceptance-v1.py)
pre-upgrade-docker-ps.txt / pre-upgrade-docker-images.txt
```

### A.5 安全说明

- 全程未 `cat`/未打印 `.env`、`config/config.yaml`、`dot2-secrets/*` 的任何值；
- `/api/v1/plugs` 的请求 token 只在同一条 shell 命令内用变量传递，未落盘、未打印；
- 本报告不含任何 key/token/secret 值；设备 IP 等 LAN 信息未展开。
