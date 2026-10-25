# 给 AI agent 的安装指令（推荐入口）

> 这是给 **AI coding agent**（Claude Code / Codex / DeepSeek 等）的执行手册。人类用户只需要把
> [README 里的那段 prompt](../README.md#-如果你想让-agent-帮你安装推荐) 或本文件一起交给 agent。
> agent 需要具备：shell、Docker + Docker Compose v2、ssh（如果部署到远程宿主）、读写目标部署目录的权限。

## 0. 目标

在一台 **x86_64 Docker 宿主** 上，把 `rui-dot2-bridge` 部署成 [rui](https://github.com/yes999zc/rui) 的语音卫星：

- rui ≥ 0.5.0（官方镜像）已经或即将在该宿主上运行；
- 用本仓库的 `deploy/downstream/Dockerfile` 构建下游镜像（官方 rui + dot2 adapter 层）；
- 设备侧（Echo Dot 2 + EchoMuse 固件）已通过 wss 8870 连到该宿主；
- 最终验收：`/health` 正常、`devices=1`、`audio_plane=true`、`speaker_connected=true`、`plug-test` 0 FAIL。

## 1. 开始前：向用户确认的输入

不要猜，逐项问清楚；缺任何一项都先停下来：

1. **rui 宿主**：ssh 字符串或本机（`user@host`），部署目录（例如 `~/rui`）。
2. **rui 版本/镜像**：目标 tag（推荐 `ghcr.io/yes999zc/rui:0.5.0` 或更新的 patch），是否已经在本机。
3. **设备**：Echo Dot 2 的局域网 IP、EchoMuse 固件版本、是否已经解锁/刷好。
4. **设备凭据**：`ca.pem` / `server.pem` / `server.key` / `device_token` 在哪（不要打印内容）。
5. **网络**：设备能访问宿主的 8870；宿主 8080 是否需要对局域网开放。
6. 是否需要 **2ch 参考补丁**（`echomuse-dot2-ref`，让 rui 侧做 AEC）；默认不需要。

## 2. 前提检查（在目标宿主上执行，只读）

```bash
uname -m                                  # 期望 x86_64
docker version --format '{{.Server.Version}}'
docker compose version
curl -sS -m 5 http://127.0.0.1:8080/health   # rui 已在跑？version 是否 ≥ 0.5.0
ss -ltn | grep -E ':8080|:8870|:8872'        # 8870 应空闲（本项目会占用）
ls -la <部署目录>/config <部署目录>/data 2>/dev/null
```

Echo Dot 2 侧（可选，需要 adb + root）：

```bash
adb shell su -c 'cat /data/local/etc/echomuse/controller.json'   # endpoints 指向本宿主 8867/8870
adb shell su -c 'readlink /data/local/bin/server'                # server_b = EchoMuse 槽
```

## 3. 安装步骤（按顺序，每步验证）

### 3.1 取代码

```bash
git clone https://github.com/yes999zc/rui-dot2-bridge.git
cd rui-dot2-bridge
git log -1 --oneline
```

### 3.2 契约自测（不部署，先确认兼容）

```bash
docker run --rm --entrypoint python -e PYTHONPATH=/opt/rui-plugins \
  -v "$PWD/src:/opt/rui-plugins:ro" ghcr.io/yes999zc/rui:0.5.0 \
  -m rui plug-test /opt/rui-plugins/rui_dot2_bridge \
  --adapter rui_dot2_bridge.adapter:build --json | tee /tmp/plug-test.json
```

要求：**0 FAIL**；WARN/SKIP 与 `docs/contract-report-0.5.0.json` 一致（2 WARN / 5 SKIP 属预期：probe 无真机、inbound 真机 I/O）。

### 3.3 构建下游镜像（x86_64 宿主）

```bash
# 必须用 legacy builder：BuildKit 在部分 NAS 上会卡住
DOCKER_BUILDKIT=0 docker build -f deploy/downstream/Dockerfile \
  --build-arg RUI_IMAGE=ghcr.io/yes999zc/rui:0.5.0 -t rui-dot2:0.5.0 .
docker images | grep rui-dot2        # 期望 ≈368 MB
```

### 3.4 准备部署目录

```bash
mkdir -p <部署目录>/{config,data,dot2-secrets}
chmod 700 <部署目录>/dot2-secrets
# 把 ca.pem / server.pem / server.key / device_token 放进 dot2-secrets/（0600）
# 如果用户有 EQ 曲线（EQ_50..EQ_100.cfg），放进 dot2-secrets/eq/（目录 750，文件 640）
```

compose 用 `deploy/downstream/docker-compose.yml` 为模板：**去掉 `build:` 段**（镜像已预构建）、
把 `image:` 指向 `rui-dot2:0.5.0`、保留 `./config ./data ./dot2-secrets` 三个挂载与 host 网络。

### 3.5 写 `.env`（敏感值从用户给的凭据文件取，**不要打印**）

关键项：

```dotenv
RUI_IMAGE=rui-dot2:0.5.0
RUI_UID=1000
RUI_GID=1000
RUI_LLM_KEY=...            # 从用户已有的 rui .env 迁移，或询问
RUI_ASR_KEY=...            # 可选
XFYUN_APPID=...            # TTS 示例（讯飞）
XFYUN_API_KEY=...
XFYUN_API_SECRET=...
DOT2_SECRETS_DIR=/opt/rui-secrets
DOT2_EQ_DIR=/opt/rui-secrets/eq
DOT2_LOUDNESS_FILE=/var/lib/rui/loudness.json
DOT2_VOLUME_CAP=100
```

> 迁移现有 `.env` 时逐键合并，**不要整个覆盖**；覆盖前先 `cp .env .env.bak-$(date +%s)`。

### 3.6 安装 plug manifest（两份）

```bash
# 镜像内一份由 Dockerfile COPY；宿主 data 目录一份给 catalog / wake 策略用
sh deploy/downstream/install-plug-manifest.sh <部署目录>/data
grep -E 'version|rui_compat' <部署目录>/data/plugs/dot2-echomuse/plugin.yaml
# 期望 version 1.1.2+、rui_compat ">=0.3.0 <0.6.0"
```

### 3.7 起容器

```bash
cd <部署目录>
docker compose up -d
docker compose ps                       # 期望 Up (healthy) 或 starting→healthy
sleep 10 && curl -sS http://127.0.0.1:8080/health | head -c 400
```

### 3.8 添加设备（官方 API，不要手改 YAML）

```bash
TOK=$(docker compose exec -T rui rui token)
curl -sS -X POST http://127.0.0.1:8080/api/v1/setup/device \
  -H "X-Auth-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"id":"dot2","adapter":"dot2","host":"<rui-host>","room":"客厅","volume":100}'
```

设备应自动 wss 拨入；等待 ≤60 s。

## 4. 验收（全部通过才算完成）

```bash
TOK=$(docker compose exec -T rui rui token)
curl -sS -H "X-Auth-Token: $TOK" http://127.0.0.1:8080/api/v1/devices/dot2/status | python3 -m json.tool
docker compose logs --tail=80 rui | grep -E 'dot2 link|registered|listen_state'
ss -ltn | grep -E ':8080|:8870'
```

| # | 判据 | 期望 |
|---|---|---|
| 1 | `/health` | `devices=1`、`enabled_devices=1`、`device_errors=0`、`audio_plane=true`、`speaker_connected=true`、`adapter_plugins.registered=["dot2"]` |
| 2 | `/devices/dot2/status` | `listen_port=8870`、`token_env=DOT2_EM_TOKEN`（`ready=false/token_missing` 是上游已知显示 bug，见 rui#7，不影响功能） |
| 3 | 端口 | 8080、8870 在听；8872 仅 127.0.0.1 |
| 4 | 日志 | `dot2 link: dot2 registered` + `listen_state=stream` |
| 5 | 播放 | `POST /api/v1/debug/say {"text":"用三句话介绍你自己"}` → 设备出声，`playback_gap_count=0` |
| 6 | 重启 | `docker compose restart` 后 ≤60 s 设备重连（`speaker_connected=true`） |

## 5. 失败处理

| 症状 | 处理 |
|---|---|
| adapter build 失败 `Permission denied .../eq/EQ_50.cfg` | `chmod 750 dot2-secrets/eq && chmod 640 dot2-secrets/eq/*.cfg` |
| 容器 healthy 但 `audio_plane=false` 且 `device_errors>0` | 看日志里的 adapter 报错；确认 `src` 已随镜像 COPY、manifest 已装 |
| 设备不连 | 查设备 `controller.json` 指向、8870 监听、防火墙；设备日志 `adb shell su -c 'cat /tmp/server.log'` |
| 播放无声 | 确认 `audio.playback.rate=48000`、`devices[].volume=100`、`dot2-secrets/eq` 非空 |
| 长回复后有"自听"第二段 turn | 确认 `devices[].audio.voice.post_playback_mute_ms=2500`（0.5.0+ 原生字段），不要再用旧的 `DOT2_PLAYBACK_GATE_MS` |

## 6. 安全红线（必须遵守）

- **不要**打印、粘贴、提交任何 token / 证书 / `.env` 值；需要引用时只说"已配置"。
- 修改任何文件前先备份（`.env`、`config.yaml`、`docker-compose.yml`）。
- **不要**修改 rui 源码；**不要**在用户的其它服务/生产主机上操作。
- 回滚：`.env` 改回 `RUI_IMAGE=ghcr.io/yes999zc/rui:<旧版本>` + `docker compose up -d`；设备侧回滚见 EchoMuse 的 A/B 槽说明。

## 7. 交付报告（给用户）

1. 每一步的命令与结果（含 `plug-test` 摘要、`/health` 关键字段、`/devices/dot2/status`）；
2. 验收清单 6 项逐条勾选；
3. 改了哪些文件（不要贴密钥）；
4. "没做到 / 需要用户补"的清单；
5. 回滚方法。
