# rui v0.2.2 Docker 部署报告（纯用户视角）

- 执行时间：2026-10-24 22:24–22:28（Asia/Shanghai）
- 目标主机：`yes999zc@<lan-ip>`（Debian 12，x86_64，8 核 / 6.7 GB / 38 GB 可用）
- 部署方式：只用上游 `yes999zc/rui` v0.2.2 自带的 `deploy/install.sh` + `docker-compose.yml`，未读本机 `rui-dot2-bridge` 的任何 findings/retro/aec 文档，未改固件、未 adb、未碰 `<lan-ip>`
- 版本：tag `v0.2.2`，commit `7bc79c1`（`release: bump 0.2.2 (wizard P0/P1, console label audit)`）
- 镜像：`ghcr.io/yes999zc/rui:0.2.2`，digest `sha256:05ed982a4cbe17e4dbedbca191989f43e0dd0ab93d63fa4f0bad83bc0991e738`

## 结论

**部署成功，无阻塞。** 容器 `rui` 已启动并通过健康检查（`Up (healthy)`），控制台 `/health` 返回 `status=ok`，`/api/v1/doctor` 可运行，日志无 traceback / fatal。

但有两个必须让 Lead 知道的偏差：

1. **任务书里的端口 8095 / 8870 / 8872 在 rui v0.2.2 中不存在。** 上游 README、`docs/deploy.md`、`docker-compose.yml`、`config.example.yaml` 统一是：控制台 **8080**，音箱 client 回连 **4412**；仓库里没有任何“响度服务”或 8872 端口。按“以 compose/文档为准”的要求，本次按上游默认部署：控制台 `http://<lan-ip>:8080/`，设备通道 `ws://<lan-ip>:4412/`（当前无设备，4412 未监听，属预期）。
2. **provider 密钥为空、无设备**，因此 `/api/v1/doctor` 为 `verdict=degraded`（`llm_key` 缺失、`ack_wav` 未生成），且 `audio_plane=false`、4412 不监听。这是无密钥/无设备冷装的预期状态，不是启动失败；需要 Lead 填参数后才会变成完整可用。

监听者唯一性：`sudo ss -ltnp` 在 8080 上只有 1 个监听者（容器内 `rui` 进程，pid 42631），4412/8870/8872/8095 均无监听。

## 1. 实际命令序列与每步结果

| # | 命令（在相应机器上） | 结果 |
|---|---|---|
| 1 | `ssh yes999zc@<lan-ip> 'docker ps; docker compose version; ss -ltn \| grep -E ":8095\|:8870\|:8872" \|\| true'` | 主机 `horner-nas`；无容器、无旧 rui；Compose v2.40.3；三个预期端口均空闲；Docker 28.5.2；磁盘 38G 可用、内存 6.7G |
| 2 | 本机：`gh auth token` + `git -c http.extraheader=... clone --depth 1 --branch v0.2.2 https://github.com/yes999zc/rui.git` | 成功；`git describe --tags` = `v0.2.2`，commit `7bc79c1`。token 只在本地进程环境中使用，未落到 .34、未写入日志/报告 |
| 3 | `scp deploy/install.sh yes999zc@<lan-ip>:~/rui-install-v0.2.2.sh` | 成功，远端脚本 sha256 与本地一致 |
| 4 | `ssh ... 'RUI_HOME=$HOME/rui RUI_REF=v0.2.2 RUI_IMAGE=ghcr.io/yes999zc/rui:0.2.2 sh ~/rui-install-v0.2.2.sh'` | 成功。`docker pull` 直接拉下 362 MB 镜像（GHCR 包实际可匿名拉取，不需要登录）；`fetch-models` 从镜像 seed 复制 VAD/KWS，再从 hf-mirror 下载 SenseVoice 239.2 MB + tokens.txt 0.3 MB；`docker compose up -d` 后约 5 s `/health` 通过 |
| 5 | `cd ~/rui && docker compose ps` | `rui  ghcr.io/yes999zc/rui:0.2.2  Up (healthy)` |
| 6 | `sudo ss -ltnp \| grep -E ':8080\|:4412\|:8870\|:8872\|:8095'` | 仅 `0.0.0.0:8080`，`users:(("rui",pid=42631,fd=7))`；其余无监听 |
| 7 | `curl -sS http://127.0.0.1:8080/health` | `{"status":"ok","version":"0.2.2","config_version":2,...,"devices":0,"audio_plane":false,...}` |
| 8 | `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/` | `200`（控制台页面）；`/api/v1/doctor`、`/api/v1/devices`、`/api/v1/config` 无 token 均为 `401`，带 `X-Auth-Token` 均为 `200` |
| 9 | `docker compose exec -T rui rui doctor` | `verdict=degraded`；PASS：arch / cpu_cores / ram_gb / disk_free_gb / capture_layout / playback_layout / aec_rtf / wake_runtime / wake_path；WARN：container_runtime、llm_key、ack_wav（原因见问题清单） |
| 10 | `docker compose exec -T rui rui-healthcheck` | `ok: uptime=84.1s devices=0 enabled=0 audio_plane=False speaker_connected=False`，退出码 0 |
| 11 | `docker compose restart rui` + 12 s 后复查 | 重新 `Up (healthy)`，`/health` 仍 `status=ok`，说明 restart policy 与健康检查可用 |
| 12 | `docker compose logs --since 5m rui \| grep -icE 'traceback\|critical\|fatal\|panic'` | `0` |

其他核验：

- `~/rui/docker-compose.yml` 与 `config.example.yaml` 的 sha256 与 v0.2.2 源码树完全一致（compose `d3c10c10...`，config `fa89a730...`）。
- 镜像内实际运行 `uvicorn 0.0.0.0:8080`，`config.yaml` 的 `web.host=0.0.0.0`、`web.port=8080`、`allow_anonymous=false`，`auth_token` 已由 `setup` 生成（32 字符，未在报告中出现）。
- 设备侧只需 `RUI_DEVICE_TOKEN`（48 hex，已生成在 `.env`，未在报告中出现）+ URL `ws://<lan-ip>:4412/?token=<RUI_DEVICE_TOKEN>`；**没有证书/TLS 要求**（上游文档明确写的是 `ws://`）。
- 容器使用 `network_mode: host`，因此 `docker compose ps` 的 PORTS 列为空是正常的，端口在宿主机上直接监听。

## 2. 问题清单

### 阻塞

- **无。** 镜像可匿名拉取，raw.githubusercontent.com 从 .34 直连可用（实测 1.04 s / 200），hf-mirror 直连可用（0.9 s / 200），未触发文档中的“源码回退构建”。

### 严重

**S1. 任务书预期端口与上游 v0.2.2 不一致，按这些端口配置的设备/监控会连不上。**

- 证据：在 v0.2.2 全仓 grep `8095|8870|8872` 零命中；`docs/deploy.md` 第 5 行“默认 `:8080`”、第 20 行“NAS 的 IP:4412”；`runbook.md` 第 7 行“默认端口 `:8080`、设备通道 `:4412`”；`config.example.yaml` 第 10 行 `port: 8080`、第 20 行 `listen_port: 4412`。
- 实际：8080（控制台）/ 4412（设备，当前未起）；没有 8872 响度服务。
- 建议：确认 8095/8870/8872 是否来自另一个项目/旧版本。如果确实要 8095，只需改 `~/rui/config/config.yaml` 的 `web.port: 8095` 后 `docker compose restart rui`；8870/8872 没有对应实现，改配置也变不出来，需要产品侧确认。

### 一般

**G1. `doctor` 在容器内误报 `container_runtime: none`。**

- 原始输出：`[WARN] container_runtime: none  -> install Docker for the one-command deploy`
- 原因：探测逻辑在容器内看不到 Docker daemon/CLI（镜像是 python-slim，不含 docker）。宿主机 Docker 正常。
- 建议：容器内跳过该项，或用 `/.dockerenv` 判定后显示“running in container”。

**G2. 无设备时 4412 不监听、`audio_plane=false`，但容器仍 healthy。**

- 原始输出：`audio plane did not start: no device this build can drive`；`plane watchdog idle: no drivable device in the config`。
- 这是上游设计（`devices: []` 时 plane 不启动，healthcheck 也不强制 plane）。但意味着**设备链路完全未验证**，不能据此认为“音箱可用”。
- 建议：Lead 填 `devices[]` 后重跑 `rui doctor` 并确认 `ss -ltnp | grep 4412` 有监听、`/health` 的 `audio_plane=true`。

**G3. TTS 无密钥导致启动期 ack 渲染失败（可恢复，非致命）。**

- 原始输出：`ack: xfyun_tts failed: no value for XFYUN_APPID (environment or secrets.yaml)`；`ack: volc_tts failed: no value for VOLC_API_KEY`；`ack: every tts provider failed`；health 中 `"ack":{"mode":"tts","note":"tts failed, plane will use a tone...","wav_exists":false}`。
- 影响：唤醒提示音退化为内置 tone，不影响进程存活。
- 建议：填 `XFYUN_*` 或 `VOLC_API_KEY` 后重启，`ack.wav` 会重新渲染。

**G4. 安装输出把控制台 token 明文打印到 stdout；我把安装日志重定向到文件后，文件里也含该 token。**

- 证据：安装日志第 4 行 `console token: <redacted>`（文档已提示“贴日志前先脱敏”）。
- 处理：已把 `~/rui-install.log` `chmod 600`；报告与本消息均不含该值。
- 建议：`install.sh` 在非 TTY 时默认不打印 token，或提示 `rui token` 获取；至少自动 `chmod 600` 日志。

**G5. `config/`、`data/` 内出现 root 属主文件，普通用户备份/恢复会踩权限。**

- 证据：`config/config.last-good.yaml` 为 `root:root 600`；`data/service.log` 为 `root:root 600`；`config/config.yaml`、`.env`、`data/models` 为 `yes999zc:Users`。
- 原因：容器内服务以 root 运行并写盘，`setup` 以 `--user $(id -u):$(id -g)` 运行。混合属主不影响运行，但 `scripts/backup.sh` 以普通用户跑可能读不到 root 文件。
- 建议：runbook 明确备份需 `sudo`，或 entrypoint 启动后 chown 数据卷。

**G6. `docker compose config` 会展开 `RUI_DEVICE_TOKEN` 明文。**

- 原因：`.env` 被 compose 插值/展开，这是 Compose 的正常行为，不是漏洞；但排障时容易顺手贴出去。
- 建议：报告/日志里统一用 `<redacted>`；不要把 `docker compose config` 原样贴到 issue。

### 文档瑕疵

**D1. 文档说“控制台没有 token 会返回 401”，实测 `GET /` 返回 200。**

- 原始：`docs/deploy.md` 第 35 行“控制台没有 token 会返回 401（页面会提示输入 X-Auth-Token）”；实测 `GET /` = 200，只有 `/api/v1/*` = 401。
- 建议：改成“API 返回 401，首页返回登录页（200）”。

**D2. `.env.example` 与 `docs/deploy.md` §3.4 的变量清单不一致。**

- `.env.example` 只有 `RUI_LLM_KEY / RUI_ASR_KEY / RUI_TTS_KEY / RUI_HA_TOKEN` 四个；`deploy.md` 列了 `VOLC_API_KEY / XFYUN_* / RUI_DEVICE_TOKEN / RUI_IMAGE`，且注明 `RUI_TTS_KEY` 已下线。照 `.env.example` 配会漏 key。
- 建议：以 `install.sh` 生成的那份为准，并让 `.env.example` 与之一致。

**D3. 任务书/上游都没有 8095/8870/8872 的说明（同 S1）。** 文档没有“响度/8872”这个概念，建议 Lead 侧核对来源。

**D4. 文档未说明 GHCR 包可匿名拉取。** `install.sh` 注释假设“private GHCR package 可能拉不动、要源码回退”，实际 `docker pull ghcr.io/yes999zc/rui:0.2.2` 直接成功（362 MB），源码回退路径未被触发。这条不影响使用，但会让人误以为必须登录 GHCR。

## 3. 需要 Lead 配置的参数（只列参数名）

必填（按当前 `config.yaml` 的 provider 链）：

- `RUI_LLM_KEY` — `llm.chain=[deepseek_llm]` 使用
- `XFYUN_APPID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET` — `tts.chain[0]=xfyun_tts`
- `VOLC_API_KEY` — `volc_tts`（TTS 兜底/推荐主用）与可选 `volc_asr`
- `RUI_ASR_KEY` — `asr.chain` 的 `siliconflow_asr` 云端兜底（本地 ASR 已就位，可不填）

按需：

- `RUI_HA_TOKEN` — Home Assistant MCP（`tools.ha`）
- `DEEPSEEK_API_KEY` — `tools.search`（`api_key_env: DEEPSEEK_API_KEY`，与 `RUI_LLM_KEY` 不是同一个变量）
- `VOLC_ACCESS_KEY_ID`、`VOLC_SECRET_ACCESS_KEY` — 控制台额度卡片（只读查询）
- `devices[]` 字段：`id`、`adapter`、`host`、`room`、`wake_words`、`volume`、`listen_port`、`token_env`
- `RUI_DEVICE_TOKEN` — 已生成，设备 client 必须填同一个值（报告不显示值）
- 音乐（可选）：`music.enabled`、`music.secrets_path` 指向的 `lan_media.json`（`url` / `user` / `password`）

不需要填：`RUI_IMAGE` 已固定为 `ghcr.io/yes999zc/rui:0.2.2`。

## 4. 复现步骤（从空主机开始）

```bash
# 0) 前置检查（应无 rui 容器，8080/4412 空闲）
ssh yes999zc@<lan-ip> 'docker ps; docker compose version; ss -ltn | grep -E ":8080|:4412" || true'

# 1) 取 v0.2.2 的安装脚本（私有仓库，用本机已有 GitHub 凭据；不要把 token 写到远端）
#    本机：
git -c http.extraheader="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$(gh auth token)" | base64)" \
    clone --depth 1 --branch v0.2.2 https://github.com/yes999zc/rui.git /tmp/rui-src
scp /tmp/rui-src/deploy/install.sh yes999zc@<lan-ip>:~/rui-install-v0.2.2.sh

# 2) 在 .34 上执行上游一键安装（固定 ref + 固定镜像）
ssh yes999zc@<lan-ip> \
  'RUI_HOME=$HOME/rui RUI_REF=v0.2.2 RUI_IMAGE=ghcr.io/yes999zc/rui:0.2.2 \
   sh ~/rui-install-v0.2.2.sh 2>&1 | tee ~/rui-install.log'
# 直连 raw.githubusercontent.com / hf-mirror 均可用；如网络不同，可先
# export https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890

# 3) 验收
ssh yes999zc@<lan-ip> 'cd ~/rui
  docker compose ps
  sudo ss -ltnp | grep -E ":8080|:4412"
  curl -sS http://127.0.0.1:8080/health
  docker compose exec -T rui rui doctor
  docker compose logs --tail=100 rui'

# 4) 取控制台 token（只显示一次；不要贴出去）
ssh yes999zc@<lan-ip> 'cd ~/rui && docker compose exec -T rui rui token'
```

预计耗时：拉镜像 1–2 min（视网络），模型 239 MB 约 2–4 min，总计约 3–5 min。本次实测从开始到 healthy 约 3 min。

## 5. 主机上保留的可复现文件

| 路径 | 内容 |
|---|---|
| `/home/yes999zc/rui/docker-compose.yml` | 上游 v0.2.2 原样，sha256 `d3c10c10...` |
| `/home/yes999zc/rui/config.example.yaml` | 上游参考配置 |
| `/home/yes999zc/rui/.env` | 600；密钥为空占位，`RUI_DEVICE_TOKEN` 已生成（不显示），`RUI_IMAGE` 已固定 |
| `/home/yes999zc/rui/config/config.yaml` | `setup` 生成的生产模板，`web.port: 8080`、`devices: []`、已生成控制台 token |
| `/home/yes999zc/rui/config/keywords.txt` | 默认唤醒词「你好小瑞」 |
| `/home/yes999zc/rui/data/models/` | SenseVoice 239 MB + Silero VAD + KWS（269 MB） |
| `/home/yes999zc/rui/scripts/` | 上游 backup/restore/acceptance 脚本 |
| `/home/yes999zc/rui-install-v0.2.2.sh` | 本次使用的安装脚本 |
| `/home/yes999zc/rui-install.log` | 600；安装日志，**含控制台 token 明文，勿外发** |

容器：`rui`，`restart: unless-stopped`，`network_mode: host`，healthcheck 30 s 间隔 / 90 s 启动宽限 / 3 次重试，日志 `json-file 10m × 3`。

> 本报告未包含任何明文 token/密钥：GitHub token、控制台 token、device token 均以 `<redacted>` 或“已生成”描述。
