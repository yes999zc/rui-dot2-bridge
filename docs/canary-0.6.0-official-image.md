# rui 0.6.0 官方发布镜像 canary（本机 arm64，2026-10-26）

> 目的：证明**官方发布镜像**（GHCR 上的 digest，不是本地构建的 rui core）可以作为下游底座跑起来，
> 并在它上面叠加 dot2 插件层后同时具备 `xiaoai-lx06` 与 `dot2-echomuse 1.2.0`。
> 不接触生产 `.168`、不需要 SSH；全部在本机 docker（server 29.8.2 / arm64）完成。
>
> 结论：**PASS**（官方 digest 身份核对通过；下游镜像两个 plug 同时在位；两路 plane 都真实起来；`/health` 200 且 `device_errors=0`）。
> 唯一需要提前知道的运维语义：没有真实音箱时 `rui-healthcheck` 会 `exit 1`，这是上游既有行为，与 dot2 层无关（见 §6.4）。

## 一、目标与范围

在范围内：

1. 拉官方镜像并按 digest 核对身份（revision / version / 平台）。
2. 容器内 import smoke：`rui.__version__`、`builtin_plugins()`。
3. 用官方 digest 作底座构建下游镜像（dot2 插件层），核对两个 plug 同时在位。
4. 真起一次下游容器（lx06 + dot2 两台假设备），确认 `/health` 不被设备离线打挂。

不在范围内（本次未做，见 §7）：

- 真实设备连通（LX06 / Dot 2 均未上电，host 是假的 `.local` 名）。
- 音频链路（KWS/VAD/ASR/TTS/AEC）与唤醒、播放实测。
- amd64 平台构建/运行验证（只验了本机 arm64）。
- 生产 `.168` 部署、`git commit/push`、镜像推送。

## 二、镜像身份（官方 digest）

```
$ docker pull ghcr.io/yes999zc/rui@sha256:abef1f276c3c21e9155d97c98913ee6c2e98fdfe842e43b90f64a5db6c12b241
…
Digest: sha256:abef1f276c3c21e9155d97c98913ee6c2e98fdfe842e43b90f64a5db6c12b241
Status: Downloaded newer image for ghcr.io/yes999zc/rui@sha256:abef1f276c3c21e9155d97c98913ee6c2e98fdfe842e43b90f64a5db6c12b241
```

无需 `docker login`（匿名拉取成功，`$HOME/.hermes/.env` 的 token 未使用、未读取）。

Labels（`docker image inspect … --format '{{json .Config.Labels}}'`）：

| label | 值 | 核对 |
|---|---|---|
| `org.opencontainers.image.revision` | `adc4c2b9e93103c0f190d854924c3f4b4298d8b8` | ✅ 以 `adc4c2b` 开头 |
| `org.opencontainers.image.version` | `v0.6.0` | ✅ 与 0.6.0 一致 |
| `org.opencontainers.image.created` | `2026-10-25T16:02:29.929Z` | — |
| `org.opencontainers.image.source` / `.url` | `https://github.com/yes999zc/rui` | ✅ |
| `org.opencontainers.image.title` / `.licenses` | `rui` / `MIT` | — |

```
$ docker image inspect … -f '{{.Architecture}}/{{.Os}} | {{.Config.Entrypoint}} | {{.Config.Cmd}} | {{.Config.User}}'
arm64/linux | [rui] | [serve] | rui

$ docker image inspect … -f '{{json .RepoDigests}}'
["ghcr.io/yes999zc/rui@sha256:abef1f276c3c21e9155d97c98913ee6c2e98fdfe842e43b90f64a5db6c12b241"]
```

多架构清单（`docker buildx imagetools inspect`，非转述）：

```
MediaType: application/vnd.oci.image.index.v1+json
Manifests:
  linux/amd64  sha256:6ab78ef6442faf0cffee4098720774b77196a657f19427ef0283c19970417c48
  unknown/unknown  (attestation-manifest → amd64)
  linux/arm64  sha256:8008cce605d1d70baa4357f844fb4eaeb80e610e4e262cbb776e998b4a55d59f
```

- Release：<https://github.com/yes999zc/rui/releases/tag/v0.6.0>（tag `0.6.0`）。
- 本机拉到并验证的是 `linux/arm64` 子清单（amd64 子清单**未拉取、未运行**）。
- 本地 image id：`sha256:22811ca842c25ce17b69d59c52ccf681c44b0de2d5bff319ff9b875fc998a25d`。

## 三、官方镜像 smoke（不启动服务）

```
$ docker run --rm --entrypoint python <digest> -c "import rui;print(rui.__version__)"
0.6.0

$ docker run --rm --entrypoint python <digest> -c "from rui.plugins.builtin import builtin_plugins;print(builtin_plugins())"
[Plugin(schema='rui-plugin/v0', id='xiaoai-lx06', name='Xiaomi AI Speaker Pro (LX06)', version='0.3.0',
        rui_compat='>=0.3.0 <0.7.0', trust=<TrustLevel.BUILTIN: 'builtin'>,
        devices=[DeviceManifest(id='lx06', model='LX06', …)], …)]
```

汇总：

```
ids = ['xiaoai-lx06']
has dot2 = False
```

官方镜像里确实**没有** dot2（不是我们本地构建的 core）：

```
$ docker run --rm --entrypoint python <digest> -c "import rui_dot2_bridge"
ModuleNotFoundError: No module named 'rui_dot2_bridge'

$ docker run --rm --entrypoint sh <digest> -c "ls -la /opt/rui-plugins"
drwxr-xr-x 1 root root . 
drwxr-xr-x 1 root root xiaoai      # 只有内置 xiaoai，没有下游目录
```

## 四、下游镜像构建（官方 digest + dot2 插件层）

构建参数在 `deploy/downstream/Dockerfile` 里已按 digest pin（`ARG RUI_IMAGE=ghcr.io/yes999zc/rui:0.6.0@sha256:abef1f27…b241`），
构建时未覆盖该参数。

```
$ docker build -f deploy/downstream/Dockerfile -t rui-dot2:0.6.0-official .
#6 [1/2] FROM ghcr.io/yes999zc/rui:0.6.0@sha256:abef1f276c3c21e9155d97c98913ee6c2e98fdfe842e43b90f64a5db6c12b241
#6 DONE 0.6s
#8 [2/2] COPY --chown=1000:1000 src/rui_dot2_bridge /opt/rui-plugins/rui_dot2_bridge
#8 DONE 0.1s
#9 exporting to image
#9 writing image sha256:2086d45900d71babaf6482ba45301a4938201038f6e93aac748bfed2d2112e46 done
#9 naming to docker.io/library/rui-dot2:0.6.0-official done
#9 DONE 0.0s
```

| 项 | 值 |
|---|---|
| 下游 tag | `rui-dot2:0.6.0-official` |
| 下游 image id | `sha256:2086d45900d71babaf6482ba45301a4938201038f6e93aac748bfed2d2112e46` |
| 平台 | `arm64/linux` |
| 继承的 revision / version label | `adc4c2b9e93103c0f190d854924c3f4b4298d8b8` / `v0.6.0` ✅ |
| 构建上下文 | 449.71 kB（`.dockerignore` 生效） |

## 五、下游镜像 smoke（两个 plug 同时在位）

```
$ docker run --rm --entrypoint python rui-dot2:0.6.0-official -c "from rui.plugins.builtin import builtin_plugins;print(builtin_plugins())"
rui 0.6.0
dot2-echomuse 1.2.0 trusted
xiaoai-lx06 0.3.0 builtin
ids = ['dot2-echomuse', 'xiaoai-lx06']
SMOKE OK: both plugins present
```

（`src/rui_dot2_bridge/plugin.yaml`：`id: dot2-echomuse`, `version: 1.2.0`, `rui_compat: ">=0.6.0 <0.7.0"`。）

## 六、真起一次下游容器（`lx06` + `dot2` 两台假设备）

### 6.1 配置（`/tmp` 下最小 config，不入库）

```yaml
version: 3
system: {data_dir: /var/lib/rui, log_level: info, timezone: Asia/Shanghai}
web:    {host: 0.0.0.0, port: 8080, auth_token: "***", allow_anonymous: false}
devices:
  - {id: speaker-a, adapter: xiaoai, host: speaker-a.local, room: 客厅, listen_port: 4412, token_env: RUI_DEVICE_TOKEN}
  - {id: speaker-b, adapter: dot2,   host: speaker-b.local, room: 书房, listen_port: 8870, token_env: DOT2_TOKEN, volume: 100}
ack: {mode: earcon}           # 离线 hermetic，不触发 TTS 渲染
multi_device: {enabled: true, max_planes: 2}
```

- 设备 host 用假 mDNS 名，两台都不可达（**故意**）。
- `/opt/rui-secrets` 放一次性自签 `server.pem`/`server.key`（`openssl req -x509 -days 1`，`chmod 600`，仅 /tmp，不落仓库），
  让 dot2 的 wss listener 能真正起来；`device_token` 未提供，adapter 按设计降级为「admission check disabled」并只打 warning。
- device token 用一次性 dummy 值（`RUI_DEVICE_TOKEN` / `DOT2_TOKEN`），不是生产 secret。

### 6.2 启动与 `/health`（映射 18091 → 8080）

```
$ docker run -d --name rui-canary-060 -p 18091:8080 … rui-dot2:0.6.0-official
$ curl -s http://127.0.0.1:18091/health
HTTP 200

status = "ok"                     version = "0.6.0"          config_version = 3
devices = 2                       enabled_devices = 2        device_errors = 0
adapter_plugins = {"registered": ["dot2"], "warnings": 0}
audio_plane = true                speaker_connected = false
multi_device = {"enabled": true, "max_planes": 2, "refused": [], "error": ""}

--- audio_devices ---
speaker-a | room=客厅 | running=True  | connected=False | pid=29 | error=(none)
speaker-b | room=书房 | running=True  | connected=False | pid=53 | error=(none)
```

**关键点**：两台设备都连不上，但 `/health` 仍是 `200 / status=ok / device_errors=0`；
离线只体现在每台设备的 `speaker_connected=false`（以及真实部署时 `rui-healthcheck` 的提示），没有把控制面打挂。

### 6.3 下游层在 plane 子进程里也生效

两个 plane 各自独立启动，日志里都能看到插件注册与监听：

```
plane: rui.device.loader: adapter plugins: registered dot2
plane: rui.device.registry: image plug 'xiaoai-lx06' registered adapter 'lx06'
plane: rui_dot2_bridge.adapter: dot2 adapter: X-EM-Token source: env
plane: websockets.server: server listening on 0.0.0.0:4412
plane: xiaoai_plug.adapter: xiaoai adapter listening on :4412 for speaker-a
plane: websockets.server: server listening on 0.0.0.0:8870
plane: rui_dot2_bridge.device_link: dot2 link: wss listening on 8870
plane: rui_dot2_bridge.adapter: dot2 adapter: waiting for speaker-b on port 8870 (volume ceiling 100%)
```

```
$ docker exec rui-canary-060 python -c "…connect_ex…"
4412 listening
8870 listening
```

→ 说明 dot2 不只是主进程 import 成功，**plane 子进程从 `/opt/rui-plugins` 加载了下游适配器并开了 wss 监听**。

### 6.4 一个必须知道的语义：没有真音箱时 `rui-healthcheck` `exit 1`

```
$ docker exec rui-canary-060 python /usr/local/bin/rui-healthcheck
unhealthy: no speaker connected (2 enabled device(s)): speaker-a (客厅): no speaker; speaker-b (书房): no speaker; …
exit=1
```

对照实验（**纯官方镜像**、同一台假 lx06、同样无真音箱）：

```
$ docker exec rui-canary-official python /usr/local/bin/rui-healthcheck
unhealthy: no speaker connected (1 enabled device(s)): speaker-a (客厅): no speaker; …
exit=1
官方 /health: {'status': 'ok', 'version': '0.6.0', 'devices': 1, 'device_errors': 0, 'adapter_plugins': {'registered': []}, 'audio_plane': True}
```

→ 同为 `exit 1`，因此这是**上游既有探针语义**（Docker `healthcheck` 要求「有音箱连上」），
不是 dot2 层引入的回归。本次 canary 用假 host，容器在 Docker 眼里会是 `unhealthy`，属预期；
真机部署里 LX06 回连后即恢复。

### 6.5 清理

```
$ docker rm -f rui-canary-060 rui-canary-official
rui-canary-060
rui-canary-official
```

只删容器，**保留**两个镜像（`ghcr.io/yes999zc/rui@sha256:abef…`、`rui-dot2:0.6.0-official`），未删任何他人镜像。

## 七、结论

**PASS**（官方发布镜像可作为下游底座）。

已证明：

| # | 断言 | 证据 |
|---|---|---|
| 1 | 官方 digest 可匿名拉取 | `Status: Downloaded newer image …` |
| 2 | digest 身份正确 | revision `adc4c2b…` + version `v0.6.0` |
| 3 | 平台匹配本机 | `arm64/linux` |
| 4 | 官方 core 干净 | `builtin_plugins()` 只有 `xiaoai-lx06`；`import rui_dot2_bridge` → `ModuleNotFoundError` |
| 5 | 下游层构建成功 | `rui-dot2:0.6.0-official` / `sha256:2086d459…` |
| 6 | 下游两个 plug 同时在位 | `dot2-echomuse 1.2.0` + `xiaoai-lx06 0.3.0` |
| 7 | 两路 plane 真实起来 | 4412 + 8870 均 listening，`audio_devices` 两行 `running=true` |
| 8 | 设备离线不打挂控制面 | `/health` HTTP 200、`device_errors=0` |

未覆盖（明确列出，不要当成已验证；**范围为 0.6.0 官方镜像 canary 轮次，2026-10-26，本机 arm64，无真机**）：

- **未连真机**：LX06 / Dot 2 都没上电，唤醒、播放、AEC、音量、回连 token 校验均为未验证。
  本轮**不**用于否定 README 的 30/30、26/30 等数字——那些来自 rui 0.7.1/p21 真机轮次（2026-10-26 15:01，见 README 支持矩阵）；两轮**同一日期（2026-10-26）、不同时刻（00:19–00:30 vs 15:01）、镜像与范围不同**，不可互证。
- **未验 amd64**：只在 arm64 拉了/跑了镜像；amd64 子清单未拉取、未运行。
- **未验音频链路**：`ack.mode=earcon`，未渲染 TTS；KWS/VAD 模型路径未挂载，plane 只在监听态，未处理音频。
- **未验 `read_only` / `user 1000:1000` / `network_mode: host`** 的 compose 组合（本次用 `-p` 端口映射、默认用户、可写 rootfs）。
- **未提交**：`docs/canary-0.6.0-official-image.md` 是新增未跟踪文件；现有 pin 改动（Dockerfile / README / docker-compose / contract-report）原样保留，未回滚、未重写、未 commit。
- **未做**：镜像推送、生产 `.168` 部署、`contract-report` 复跑。

## 八、命令清单（可复现）

```bash
IMG=ghcr.io/yes999zc/rui@sha256:abef1f276c3c21e9155d97c98913ee6c2e98fdfe842e43b90f64a5db6c12b241

# 1. 拉取 + 身份
docker pull "$IMG"
docker image inspect "$IMG" --format '{{json .Config.Labels}}'
docker image inspect "$IMG" -f '{{.Architecture}}/{{.Os}}'

# 2. 官方 core smoke
docker run --rm --entrypoint python "$IMG" -c "import rui;print(rui.__version__)"
docker run --rm --entrypoint python "$IMG" -c "from rui.plugins.builtin import builtin_plugins;print(builtin_plugins())"

# 3. 下游构建 + smoke
docker build -f deploy/downstream/Dockerfile -t rui-dot2:0.6.0-official .
docker image inspect rui-dot2:0.6.0-official -f 'Id={{.Id}}'
docker run --rm --entrypoint python rui-dot2:0.6.0-official \
  -c "from rui.plugins.builtin import builtin_plugins;print(builtin_plugins())"

# 4. 真起容器（配置见 §6.1，一次性 TLS 材料在 /tmp，不落仓库）
docker run -d --name rui-canary-060 -p 18091:8080 \
  -v /tmp/rui-canary/config:/config -v /tmp/rui-canary/data:/var/lib/rui \
  -v /tmp/rui-canary/secrets:/opt/rui-secrets:ro \
  -e RUI_CONFIG=/config/config.yaml -e PYTHONPATH=/opt/rui-plugins \
  -e RUI_ADAPTER_PLUGINS='dot2=rui_dot2_bridge.adapter:build' \
  -e DOT2_SECRETS_DIR=/opt/rui-secrets -e DOT2_LOUDNESS_FILE=/var/lib/rui/loudness.json \
  -e RUI_DEVICE_TOKEN='<dummy>' -e DOT2_TOKEN='<dummy>' \
  rui-dot2:0.6.0-official
curl -s http://127.0.0.1:18091/health
docker exec rui-canary-060 python /usr/local/bin/rui-healthcheck

# 5. 清理容器（保留镜像）
docker rm -f rui-canary-060
```

---

**环境**：macOS / arm64；docker server 29.8.2；`rui-dot2:0.6.0-official` 构建于本仓库工作树
（`deploy/downstream/Dockerfile` 的 pin 改动为既有未提交状态）。
**记录时间**：2026-10-26 00:19–00:30（Asia/Shanghai）。

---

## 九、测试机 `.34` 金丝雀（x86_64，同一 digest，2026-10-26）

上面的 §一–§八 是本机 arm64 的验证；本节是同一 pin 在**测试机 `.34`（x86_64，
Docker 28.5.2）**上的部署级金丝雀，用来补上 arm64 未覆盖的 amd64 子清单、`read_only` /
`user 1000` / compose 覆盖层，以及“有客户端连上时容器真的 healthy”。生产 `.168` 与
`/vol1/rui` 全程未接触。

| | 值 |
|---|---|
| base | `ghcr.io/yes999zc/rui:0.6.0@sha256:abef1f27…b241`，`docker pull` 回显 digest 一致 |
| base id / created | `sha256:5eac9b1331ee…26ee` / `2026-10-25T16:03:03Z` |
| base labels | `…version=v0.6.0`、`…revision=adc4c2b9…` |
| 下游镜像 | `rui-dot2:0.6.0-official` → `sha256:6dd9d373bbc2…0424` |
| 构建 | `docker build -f deploy/downstream/Dockerfile -t rui-dot2:0.6.0-official .`，经典 builder 在已拉取 base 之上 **1 s**（BuildKit 路径在该机到 GHCR 的路由上一度卡在 14 MB 层下载，改用 `DOCKER_BUILDKIT=0` 直接复用本地层，非构建失败） |
| 运行 | compose 覆盖层：bridge 网络，`127.0.0.1:8091→8080`（控制台）、`127.0.0.1:8890→8870`（EchoMuse wss），`read_only: true`、`user 1000:1000`、`cap_drop: ALL`、健康检查 10 s；同机已有的 0.5.3 实例继续占用 8080/8870/4412，无冲突 |
| 配置 | config v3，两台**假**设备：`dot2`（adapter `dot2`，`params.plug_id: dot2-echomuse`）+ `lx06`（adapter `lx06`，镜像自带 plug `xiaoai-lx06`）；`multi_device: {enabled: true, max_planes: 2}`；**不写**全局 `audio` 块，让两个 manifest 各自声明布局 |
| 合成设备 | `/tmp/rui-053-canary/harness/fake_dot2.py --url wss://127.0.0.1:8890 --device-id dot2`，连上 11 ms 后收到 `ACK {"device_id": "dot2"}` |

证据（原样）：

```
$ docker ps --filter name=rui-canary
rui-canary | rui-dot2:0.6.0-official | Up About a minute (healthy)

$ docker exec rui-canary python -c 'from rui.plugins.builtin import builtin_plugins; ...'
[('dot2-echomuse', '1.2.0', <TrustLevel.TRUSTED: 'trusted'>),
 ('xiaoai-lx06', '0.3.0', <TrustLevel.BUILTIN: 'builtin'>)]

$ curl -s http://127.0.0.1:8091/health           # http=200
{"status": "ok", "version": "0.6.0", "config_version": 3, "devices": 2,
 "enabled_devices": 2, "device_errors": 0,
 "adapter_plugins": {"registered": ["dot2"], "warnings": 0},
 "audio_plane": true, "speaker_connected": true, "speaker_device": "dot2"}
# audio_devices: dot2 running=true connected=true; lx06 running=true connected=false（无客户端，预期）

$ curl -s -o /dev/null -w '%{http_code}' -H "X-Auth-Token: <redacted>" \
    http://127.0.0.1:8091/api/v2/meta
404       # {"detail":"Not Found"} —— v2 meta 路由不在 0.6.0 tag 里
```

关键日志：`adapter plugins: registered dot2`、`image plug 'xiaoai-lx06' registered
adapter 'lx06'`、`dot2 adapter: X-EM-Token source: file`、`audio plane up:
device='dot2'`、`audio plane up: device='lx06'`。

本次金丝雀期间修正的两处**配置**错误（未改代码）：

1. `adapter: xiaoai-lx06` → plane 报 `unknown adapter: 'xiaoai-lx06' (this build
   has: dot2, lx06, mock, xiaoai)`。配置字段是 **adapter 名**（`lx06`）；manifest id
   （`xiaoai-lx06`）只在需要区分机型时进 `params.plug_id`。
2. 全局 `audio.capture: {rate: 16000, channels: 1}` 与 lx06 manifest 冲突
   （`mic_channels(6) + ref_channels(2) must equal channels(1)`）。manifest 已声明
   `audio_defaults` 时，全局块要么不写，要么让该设备重述一套自洽布局。

边界：`healthy` 在这里的含义是“控制面 + 两路 plane 起来 + 一条设备链路已连”；链路由合成
客户端持有，客户端退出后健康检查会如实报 `no speaker connected (2 enabled device(s))`。真机
语音验收仍未做。清理：`cd ~/dot2-canary-060/instance && docker compose -f
docker-compose.canary.yml down`（在 `.34` 上执行，镜像可留作缓存）。
