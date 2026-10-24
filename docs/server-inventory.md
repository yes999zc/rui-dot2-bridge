# 服务器实例盘点：horner-nas（<lan-ip>）

> 审计时间：2026-10-24 21:44–21:50 CST
> 主机：**horner-nas** · Debian GNU/Linux 12 (bookworm) · kernel 6.18.18 · 8 vCPU / 6.7 GiB
> 方式：SSH 只读（docker / systemctl / ps / ss / du / 日志 / `/health`）。
> **未重启、未停止、未删除任何服务、容器、镜像；未改任何配置；未动设备。**
> 敏感信息：配置与 env 含 token/secret，本文只写键名，不展开值。
> 范围：`<lan-ip>`；另做一次 `<lan-ip>` 的只读旁证（docker 权限不足，见 §5）。

---

## 0. 一句话结论

**三个名字不是三套东西，而是同一份部署的三行展示：**

- `rui-test` = 唯一的 systemd 服务；
- `house_ear serve` = 这个服务的主进程（控制面）；
- `dot2 audio` = 同一个服务拉起的音频面子进程（`--device dot2`）。

**1 个服务 + 2 个进程 = 1 份部署。三个“名字”都处于运行状态；当前不冲突、不重复。**

补充：**.34 的 Docker 是完全空的**（0 容器 / 0 镜像 / 0 卷 / 0 compose 项目 / 0 构建缓存）。所以这三个名字**不可能是 .34 上的 Docker 容器**；若你确实是在 Docker 管理器里看到，需要确认那个 UI 连的是哪台主机（或是否缓存），见 §3.11。

---

## 1. 结论表（名字 → 实际是什么 → 状态 → 是否冲突）

| 你看到的名字 | 实际是什么 | 关键标识 | 状态 | 是否冲突 / 重复 |
|---|---|---|---|---|
| `rui-test` | systemd 服务 `rui-test.service` | `/etc/systemd/system/rui-test.service`；`enabled` + `active`；cgroup 下正好 2 个进程 | **运行中**（21:39:42 起） | 否；主机上唯一相关 unit |
| `house_ear serve` | 该服务的主进程（控制面） | PID **24822**，PPID 1，`python -m house_ear serve --config /home/yes999zc/rui-test/config.yaml`；监听 **8095** | **运行中** | 否；同一部署，不是独立服务 |
| `dot2 audio` | 同一服务的音频面子进程 | PID **24853**，PPID **24822**，`python -m house_ear.audio.process --config … --device dot2`；监听 **8870**（wss）+ **8872**（127.0.0.1 响度控制） | **运行中** | 否；同一部署，不是独立服务 |
| （Docker 管理器里的“容器”） | **在 .34 上不存在** | `docker ps -a` 空、`docker images` 空、`docker system df` 全 0 | 无 | 无 Docker 冲突 / 重复 |

**实测进程树：**

```
systemd: rui-test.service  (enabled, active since 2026-10-24 21:39:42)
└─ PID 24822  python -m house_ear serve --config /home/yes999zc/rui-test/config.yaml
   │            └─ :8095  HTTP API + 控制台
   └─ PID 24853  python -m house_ear.audio.process --config … --device dot2 \
                  --socket /home/yes999zc/.local/state/house-ear/audio.sock
                ├─ :8870  wss  Dot2 入站（设备 <lan-ip>）
                └─ :8872  http 127.0.0.1 响度控制
```

这不是“三个 systemd 服务”，也不是“三个容器”。它是 rui 源码里定义的 **serve（控制面）+ audio（音频面）双进程模型**，systemd 只负责把两者放进同一个 unit/cgroup。`rui-test` 是“壳”，另外两个名字是壳里的两个进程。

---

## 2. 证据

### 2.1 Docker：0 容器、0 镜像、0 卷、0 compose 项目

```text
$ docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.CreatedAt}}"
NAMES     IMAGE     STATUS    PORTS     CREATED AT
（空）

$ docker images --format "table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}"
REPOSITORY:TAG   IMAGE ID   CREATED   SIZE
（空）

$ docker system df
TYPE            TOTAL   ACTIVE   SIZE   RECLAIMABLE
Images          0       0        0B     0B
Containers      0       0        0B     0B
Local Volumes   0       0        0B     0B
Build Cache     0       0        0B     0B

$ docker compose ls -a
NAME   STATUS   CONFIG FILES
（空）

$ docker info | grep -E "Containers|Running|Images"
Containers: 0
  Running: 0
  Paused: 0
  Stopped: 0
Images: 0
```

- `docker ps -a` 包含已停止容器，仍为空 → **没有任何“跑过但停了”的残留容器**。
- `docker volume ls` 空；`docker network ls` 只有默认的 bridge/host/none，无自定义网络。
- Docker 守护进程本次开机于 `20:48:05` 启动，Docker Root Dir 为 `/var/lib/docker`；daemon 自报 0 对象。
- 因此：**没有 Docker 容器可列创建时间/镜像来源/最近日志**。仓库 `docker-compose.yml` 里写的镜像 `ghcr.io/yes999zc/rui:latest` 本机也未拉取（0 images）。

> 结论：Docker 侧没有“在跑但没被使用”的容器，也没有可清理的镜像/卷/构建缓存（0B）。

### 2.2 systemd：唯一相关 unit 是 `rui-test.service`

`systemctl cat rui-test.service` 全文（无密钥）：

```ini
# /etc/systemd/system/rui-test.service
[Unit]
Description=rui test instance (horner-nas, dot2 inbound)
After=network-online.target

[Service]
Type=simple
User=yes999zc
Group=Users
WorkingDirectory=/home/yes999zc/AI_Workspace/01_Code/projects/rui-test
EnvironmentFile=/home/yes999zc/rui-test/env
ExecStart=/home/yes999zc/AI_Workspace/01_Code/projects/rui-test/.venv/bin/python -m house_ear serve --config /home/yes999zc/rui-test/config.yaml
Restart=on-failure
RestartSec=3
StandardOutput=append:/home/yes999zc/rui-test/logs/rui.log
StandardError=append:/home/yes999zc/rui-test/logs/rui.log

[Install]
WantedBy=multi-user.target
```

关键属性：

| 项 | 值 |
|---|---|
| Loaded | `loaded (/etc/systemd/system/rui-test.service; enabled)` |
| Active | `active (running) since Sat 2026-10-24 21:39:42 CST` |
| Main PID | `24822 (python)` |
| Tasks / Memory | `21` / `720.7M` |
| CPU | `1min 27.579s` |
| Restart 策略 | `Restart=on-failure`，`RestartSec=3s` |
| NRestarts | **0**（systemd 未因失败自动拉起；21:39 为主动重启） |
| Enabled | `enabled`（开机自启） |

交叉检查：

- `systemctl list-units --type=service --state=running` 全量 80 个 unit 中，匹配 `rui|dot2|house` 的**只有** `rui-test.service`。
- `/etc/systemd/system/` 下匹配 `rui|dot2|house|audio` 的 unit 文件**只有** `rui-test.service`。
- `systemctl --user list-unit-files --type=service` 中没有 rui/dot2/house 相关 unit。
- 没有第二个 `rui-test`、`dot2`、`house-ear` 服务，也没有 timer/cron 重复拉起（`crontab -l` 为空）。

### 2.3 进程：只有 2 个，父子关系明确

```text
$ ps -eo pid,ppid,etime,rss,args | grep -Ei "house_ear|rui|dot2|audio" | grep -v grep
24822     1  07:40 644720  …/.venv/bin/python -m house_ear serve --config /home/yes999zc/rui-test/config.yaml
24853 24822  07:36 173856  …/.venv/bin/python -m house_ear.audio.process --config /home/yes999zc/rui-test/config.yaml --device dot2 --socket /home/yes999zc/.local/state/house-ear/audio.sock
```

| PID | PPID | 启动时间 | RSS | CPU | 角色 |
|---|---|---|---|---|---|
| 24822 | 1（systemd） | 21:39:42 | ~645 MB | 2.0% | 控制面 `house_ear serve`，监听 8095 |
| 24853 | **24822** | 21:39:46 | ~174 MB | 21.9% | 音频面 `house_ear.audio.process --device dot2`，监听 8870/8872 |

- `ps --ppid 24853` 为空 → 音频面没有再拉起孙进程/独立 bridge 进程。
- `systemd-cgls /system.slice/rui-test.service` 只显示 24822 与 24853 两个进程。
- 两个进程的 `cwd` 都是 `/home/yes999zc/AI_Workspace/01_Code/projects/rui-test`，使用同一份 `--config /home/yes999zc/rui-test/config.yaml`。
- 子进程在父进程启动 4 秒后出现，由父进程 supervisor 拉起；日志中音频面掉线时 watchdog 会重建（见 §2.7）。

### 2.4 监听端口：每个端口都只有一个监听者

`ss -ltnp` 摘要（只列相关端口 + 对照端口）：

| 端口 | 监听地址 | PID / 进程 | 用途 | 监听者数 | 冲突？ |
|---|---|---|---|---|---|
| **8095** | `0.0.0.0` | 24822 `house_ear serve` | HTTP API + 控制台 | **1** | 无 |
| **8870** | `0.0.0.0` | 24853 `house_ear.audio.process` | Dot2 wss 入站（设备连这里） | **1** | 无 |
| **8872** | `127.0.0.1` | 24853 `house_ear.audio.process` | 响度控制 HTTP（GET/PUT `/loudness`） | **1** | 无；仅回环 |
| 8867 | — | — | 旧桥接的明文控制口（历史文档口径） | **0** | 无；当前未启用 |
| 8080 | — | — | rui 默认端口（生产容器口径） | **0** | 无 |
| 3000 | — | — | 未使用 | **0** | 无 |

`ss -ltnp` 中没有第二个 python 进程监听上述任一端口；`ss -ltn` 对 8095/8870/8872 各只返回 1 条 LISTEN。

> 说明：`8867` 在本机**没有监听**。当前 adapter 的 `plug.json` 只声明 `wss 8870`，响度控制在 `127.0.0.1:8872`；设备已经通过 8870 注册成功（见 §2.5），说明当前链路不依赖 8867。8867 是旧 EchoMuse controller/旧桥接的明文备选口，属于回滚路径，不是当前运行路径的一部分。

### 2.5 Dot2 设备连接：2 条 TCP 连接，但都属于同一个 PID

```text
$ ss -tnp | grep <lan-ip>
ESTAB 0 0 <lan-ip>:8870  <lan-ip>:47266  users:(("python",pid=24853,fd=10))
ESTAB 0 0 <lan-ip>:8870  <lan-ip>:47267  users:(("python",pid=24853,fd=11))
```

- 设备 `<lan-ip>` 到 `<lan-ip>:8870` 有 **2 条连接**，fd 分别 10/11，**都属于 PID 24853**。这是同一台设备的控制通道 + 数据通道，不是两个 controller 在抢。
- 8870 只有 1 个 LISTEN（PID 24853）。如果起第二个 rui 实例，它会在 `bind(8870)` 时直接 `EADDRINUSE`，**根本到不了设备注册阶段**；因此不会出现“两个实例同时抢 Dot2 连接”。
- 日志确认设备注册（**只显示时间/固件/IP，无 token**）：

```text
2026-10-24 21:39:48,242 INFO rui_dot2_bridge.device_link: dot2 link: dot2 registered
  (fw 20261024-1911-dev, fireos, ip <lan-ip>, caps mic,speaker,leds,…,aec_hw_ref,…)
```

- 服务 21:39:42 启动 → 音频面 21:39:46 起 → 设备 21:39:48 注册，启动链路正常。
- `/health`（本机回环 GET，只读）：

```json
{"status":"ok","version":"0.1.3","config_version":2,"config_version_supported":2,
 "uptime_s":406.8,"devices":1,"enabled_devices":1,"device_errors":0,
 "audio_plane":true,"speaker_connected":true,"speaker_device":"dot2"}
```

  `devices=1`、`enabled_devices=1`、`device_errors=0`、`audio_plane=true`、`speaker_connected=true`，与“只有一个 dot2 设备、一个音频面”一致。

### 2.6 配置、数据、日志、模型、磁盘

#### 目录与大小

| 路径 | 大小 | 权限/归属 | 作用 | 是否重复 |
|---|---|---|---|---|
| `/home/yes999zc/AI_Workspace/01_Code/projects/rui-test` | **156 MB** | 代码仓 | rui 源码 + `.venv` + web/tests/docs；git HEAD `1132bc6`，版本 0.1.3 | 否；唯一代码仓 |
| ├─ `.venv` | **144 MB** | — | Python 虚拟环境（editable 安装 `house_ear 0.1.3`） | 否；只此一个 |
| ├─ `src` | 3.3 MB | — | `house_ear` 源码 | 否 |
| `/home/yes999zc/rui-test` | **1.9 MB** | 运行目录 | 配置 + 数据 + 日志 + EQ + secrets | 否；唯一运行目录 |
| ├─ `config.yaml` | 5,936 B | **644** `yes999zc:Users` | 主配置；**含 token/secret 字段，未展开** | 否 |
| ├─ `config.last-good.yaml` | 5,936 B | **600** | 程序在配置加载成功后自动写入的 last-good 快照（21:39:47）；与 `config.yaml` **diff 为空（完全相同）** | 自动快照，不是运行副本 |
| ├─ `env` | 748 B | **600** | XFYUN / DOT2 / DEEPSEEK 等环境变量；**含密钥，未展开** | 否 |
| ├─ `data` | 1.4 MB | 755 | `service.log` 315 KB、`echotest.wav` 1.0 MB、`ack.wav` 15 KB、`ack.stamp` | 否 |
| ├─ `logs` | 336 KB | 755 | `rui.log` 324 KB（systemd append） | 否 |
| ├─ `eq` | 172 KB | 755 | `EQ_50/60/70/80/90/100.cfg` 各 25 KB | 否 |
| ├─ `secrets` | 20 KB | **700** | 证书/密钥目录（未展开） | 否 |
| ├─ `keywords.txt` | 50 B | — | 唤醒词 | 否 |
| `/home/yes999zc/rui-bridge` | **992 KB** | 755 | `rui_dot2_bridge` 插件源码 + `plug.json`；**无 .git、无 .venv** | **不是重复实例**；是被加载的 adapter 插件 |
| `/home/yes999zc/AI_Workspace/03_Data/Models/sherpa-onnx` | **1.2 GB** | — | KWS 39 MB + SenseVoice 1.1 GB + silero VAD 632 KB | 否；全机唯一一份 |
| Docker（镜像/容器/卷/构建缓存） | **0 B** | — | 无 | 无 |
| **合计（上述目录）** | **约 1.3 GB** | — | `du -sch` 结果 | — |

磁盘：`/dev/sda2` 63 GB，已用 24 GB，可用 36 GB（**40%**）。空间不紧张。

#### 两个代码目录的关系（重要）

`rui-bridge` **不是**第二份 rui，也不是残留的空壳：

- `env` 中设置了 `PYTHONPATH=/home/yes999zc/rui-bridge/src`、`DOT2_PLUG_FILE=/home/yes999zc/rui-bridge/plug.json`。
- 运行日志中明确出现 `rui_dot2_bridge.device_link`、`rui_dot2_bridge.control`、`rui_dot2_bridge.adapter`，证明音频面**正在加载**这个插件。
- 它没有 `.venv`、没有 `.git`，只提供 `rui_dot2_bridge` 包和 `plug.json`；主程序仍是 `rui-test` 仓库的 `house_ear`。
- 因此它是**运行时依赖**，删掉会导致 dot2 adapter 下次启动失败。**不要当“重复目录”清理。**

#### 配置/数据是否共用、是否会互相覆盖

- 当前只有**一个** rui 实例，`serve` 与 `audio.process` 共用同一份 `config.yaml`，这是设计内的父子共用，不是两个实例争用。
- 只有音频面（PID 24853）会写 `DOT2_LOUDNESS_FILE=/home/yes999zc/rui-test/loudness.json`；没有第二个写者，当前不存在互相覆盖。
- **但该文件目前不存在**（`ls` 返回 ENOENT）。日志显示 loudness 已启用并加载 `EQ_100` 曲线，说明读的是 `eq/`；`loudness.json` 可能只在 `PUT /loudness` 时落盘。若期望响度设置在重启后保留，需要后续只读验证一次“PUT 后是否生成该文件”（本次未执行）。
- `config.yaml` 权限 **644**，而它含 token/secret；同机其他本地用户可读。`env` 与 `config.last-good.yaml` 是 600，`secrets/` 是 700，相对更严。
- 仓库自带的 compose 部署使用另一套路径（`./config`、`./data`、`/opt/rui-secrets/loudness.json`），与 systemd 这套**不共用文件**；但两者都走 host 网络，真同时起会先撞端口（见 §3.7）。

#### 日志与轮转

- `rui.log` 324 KB，`root:root 644`（systemd 以 root 打开 append 文件），当前无 logrotate 配置（`/etc/logrotate.d/` 无 rui/house/dot2）。
- `data/service.log` 315 KB，`600`，应用自身有轮转（2 MB × 3 的口径）。
- **风险**：`rui.log` 是无轮转的 append 文件，长期跑会持续增长；目前很小，不构成当前故障。

### 2.7 启动/重启时间线（为什么 21:39 重启）

| 时间 | 事件 |
|---|---|
| 20:47:55 | 主机 boot |
| 20:48:05 | dockerd 启动（Docker 仍为 0 对象） |
| 21:39:42 | `rui-test.service` 启动，主进程 24822 |
| 21:39:46 | 音频面子进程 24853 启动 |
| 21:39:47 | 程序在配置加载成功后自动写入 `config.last-good.yaml`（600） |
| 21:39:48 | Dot2 注册成功（`<lan-ip>`，fw `20261024-1911-dev`） |

- `NRestarts=0` → systemd 没有因为失败自动拉起；21:38:12 的上一份实例在 21:39:42 被新实例替换，属于**主动重启**（人工/脚本），不是崩溃循环。
- 日志中 18:58–19:47 有多次人工重启（测试迭代），`grep -Ei "address already in use|bind.*error"` 无命中 → 没有端口抢占失败。
- 19:20–19:47 有若干 `audio plane reader stopped: IncompleteReadError … watchdog will rebuild the plane` 警告，最后一条在 19:47:52；**21:39 重启后未再出现**。
- 21:42–21:45 的日志是 `echotest.wav` 反复播放与 30 秒电平报告，属于现场测试流量，不是异常重启。

### 2.8 密钥处理说明

- `config.yaml` 与 `env` 含 token/secret（设备 token、讯飞三件套、DeepSeek key、`HOUSE_EAR_LLM_KEY` 等），**本文只列键名，不展开值**。
- `plug.json` 只描述协议/能力（`transport.port=8870`、`control.port=8872`、音频参数、音量曲线），**不含 token 明文**。
- 报告中所有日志片段均已避免打印 token/URL 凭据。

---

## 3. 冲突 / 重复风险清单

| # | 风险 | 当前事实 | 判断 | 建议（仅建议，未执行） |
|---|---|---|---|---|
| 3.1 | 端口冲突（8095/8870/8872） | 每个端口恰好 1 个监听者；无 `EADDRINUSE` | **当前无冲突** | 不要再在 .34 起第二份 rui（systemd 或 compose）；迁移前先确认端口空闲 |
| 3.2 | 两个 rui 抢 Dot2 | 8870 只有 PID 24853 监听；设备 2 条连接都属于该 PID；设备已注册 | **当前无争抢** | 保持 8870 独占；任何第二实例都会在 bind 阶段失败，不会静默抢设备 |
| 3.3 | 8867 控制面冲突 | 本机 0 监听；当前走 8870 TLS；设备已注册 | **无冲突** | 8867 只在回滚旧桥接时才需要；不用就保持关闭 |
| 3.4 | 配置/数据共用导致覆盖 | 单实例；`serve` 与 `audio` 共用同一 config 是设计；`loudness.json` 单写者 | **当前无覆盖** | 保持单实例；验证 `loudness.json` 持久化；`config.yaml` 建议 `chmod 600` |
| 3.5 | 日志/模型/venv 重复占盘 | 1 个 venv（144 MB）、1 套模型（1.2 GB）、1 份插件（992 KB）、Docker 0 B | **无重复** | 无需清理；给 `rui.log` 加 logrotate |
| 3.6 | Docker 残留 | 0 容器/0 镜像/0 卷/0 构建缓存/0 compose 项目 | **无残留** | 无需 `docker prune`；不要为“清理”去动 Docker |
| 3.7 | 仓库自带 compose 的潜在冲突 | `docker-compose.yml`：service `house-ear`、`container_name: house-ear`、`network_mode: host`、`restart: unless-stopped`、镜像 `ghcr.io/yes999zc/rui:latest`、挂载 `./config`/`./data`/`./data/models` | **未运行，但一旦运行会撞 8095/8870** | **不要在 .34 上 `docker compose up`**；若将来迁 Docker，必须先停 `rui-test`（需审批），不能并存 |
| 3.8 | `rui-bridge` 未版本化 | 992 KB 源码，无 `.git`、无 `.venv`；但被 `PYTHONPATH`/`plug.json` 实际加载 | **不是重复实例；是维护风险** | 保留；建议纳入 git 或记录 commit/校验和，避免“哪份在跑”说不清 |
| 3.9 | 代码仓有未提交变更 | `git status` 显示 `M uv.lock`；HEAD `1132bc6`；remote `github.com/yes999zc/rui.git` | **不影响当前运行** | 记录/提交 `uv.lock`，保证下次重建可复现 |
| 3.10 | `config.yaml` 权限过宽 | 644，含 token/secret；`env` 600、`secrets/` 700 | **本地信息暴露风险** | 建议 `chmod 600 ~/rui-test/config.yaml`（本次未执行） |
| 3.11 | Docker UI 里看到三个名字 | .34 Docker 0 对象；容器名不允许空格，`house_ear serve`/`dot2 audio` 是命令行/进程标签 | **不是 .34 的容器** | 确认 UI 连的主机/是否缓存；若看的是 `.168`（Bakey-NAS），那边当前账号无 docker 组权限，需授权后另查 |

**关于 3.11 的进一步判断：**

1. `rui-test`、`house_ear serve`、`dot2 audio` 与 .34 上的 **unit 名 + 两条命令行**一一对应；`systemctl status rui-test` / `systemd-cgls` 恰好会把这三行展示在一起。
2. Docker 容器名不能含空格，`house_ear serve` 这种带空格的字符串不可能是容器名；它更像“进程名/命令行摘要”。
3. .34 的 `docker ps -a`（含 stopped）为空，`docker system df` 也全 0 → 本次审计时 daemon 里没有任何容器/镜像；仅凭当前状态无法判断历史上是否跑过。UI 若仍显示，需要确认是缓存、还是另一台 host，不能据此认定 .34 上还有容器。
4. 仓库自带的 `docker-compose.yml` 若在**别处**按目录名启动过，Docker UI 里会留下项目名 `rui-test` + 容器名 `house-ear`（2 个名字）；这也不会产生 `house_ear serve` / `dot2 audio` 这种带空格的名字。
5. `.168`（Bakey-NAS，生产 NAS）SSH 可达，但 `yes999zc` 不在 docker 组，`docker ps` 返回 `permission denied`；本次**无法**确认它上面是否有同名容器。**没有**对 `.168` 做任何变更。

---

## 4. 结论：保留 / 残留 / 清理建议

### 4.1 应当保留（不要删、不要停）

- `rui-test.service`（enabled + active）及其两个进程 24822 / 24853。
- `/home/yes999zc/rui-bridge`（**正在被加载的 dot2 adapter 插件**；删除会让下次启动失去 dot2 支持）。
- `/home/yes999zc/rui-test/`：`config.yaml`、`env`、`config.last-good.yaml`、`data/`、`eq/`、`secrets/`、`keywords.txt`、`logs/`。
- `/home/yes999zc/AI_Workspace/01_Code/projects/rui-test`：源码 + `.venv`（唯一运行环境）。
- `/home/yes999zc/AI_Workspace/03_Data/Models/sherpa-onnx`：1.2 GB 模型（全机唯一一份，删除会破坏 KWS/ASR/VAD）。

### 4.2 残留 / 重复

- **运行中的重复实例：无。**
- **Docker 残留：无**（0 容器/镜像/卷/构建缓存）。
- **重复的 venv / 模型 / 日志：无**；只有一份 venv、一套模型、一份 `rui.log`。
- `config.last-good.yaml` 与 `config.yaml` 当前内容相同，但它是**程序自动写入的 last-good 快照**（由 `house_ear.config.recovery.remember_last_good` 生成），不是“重复运行副本”，应保留。
- `~/rui-test/data/echotest.wav`（1.0 MB）是测试音频，属于可选的测试残留；不清也不影响运行。
- `rui-bridge` 不是残留，是**运行时依赖**；唯一的问题是它没有 git 版本记录。

### 4.3 建议的清理/治理动作（全部为建议，本次未执行）

1. **保持 .34 只用 systemd 这一套**；不要在 .34 上 `docker compose up`（host 网络会撞 8095/8870）。
2. 若未来要迁到 Docker：先在维护窗口停 `rui-test`（需审批），再起 compose；**绝不能两套并存**。
3. 先确认 Docker 管理器看的是哪台主机：.34 `docker ps -a` 为空；`.168` 需 docker 组/授权后另查。若是 UI 缓存，刷新/重登即可，不要删东西。
4. 建议 `chmod 600 ~/rui-test/config.yaml`（含 token/secret，当前 644）。
5. 建议给 `~/rui-test/logs/rui.log` 配 logrotate（当前无轮转；324 KB 很小，但会持续长）。
6. 建议验证 `loudness.json` 持久化：`DOT2_LOUDNESS_FILE` 指向的文件当前不存在；若 PUT 后仍不生成，说明响度设置重启会丢。
7. 建议把 `/home/yes999zc/rui-bridge` 纳入 git 或记录校验和/版本，避免“线上跑的是哪份插件”无法追溯。
8. 8867 保持关闭，除非要回滚旧 EchoMuse controller；当前设备已通过 8870 正常注册。

---

## 5. 审计边界与未决项

- 全程只读：未 `systemctl restart/stop`、未 `docker stop/rm/prune`、未改任何配置、未改设备、未播放/注入音频。
- `journalctl -u rui-test` 因当前用户不在 `adm`/`systemd-journal` 组而无法读取；改用 `systemctl show`（NRestarts/启动时间）+ 应用 `rui.log`/`service.log`。
- `/var/lib/docker` 目录本身 root-only，无法直接 `ls`；但 `docker info`/`docker ps -a`/`docker system df` 均由 daemon 返回 0，足以判定无容器。
- `.168` 只做了 `hostname` + `docker ps -a` 只读旁证；docker 权限不足，未获得其容器列表，也未做任何变更。
- 未登录设备本身；设备在线状态由主机侧 `ss`、`rui.log` 注册记录、`/health` 推断。
- “三个名字”的具体 UI 来源未能 100% 定位（用户描述为“可能在 Docker 管理器”）；本报告按“名字与 unit/进程命令行的一一对应”给出结论，并给出 Docker 为空的硬证据。

---

## 附录：本次使用的只读命令（可复现）

```bash
# Docker
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.CreatedAt}}'
docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}'
docker system df
docker compose ls -a
docker info

# systemd
systemctl list-units --type=service --state=running --no-pager --plain
systemctl cat rui-test.service
systemctl status rui-test.service --no-pager -l
systemctl show rui-test.service -p NRestarts -p ExecMainStartTimestamp -p Restart -p ExecMainPID
systemctl is-active rui-test; systemctl is-enabled rui-test
systemd-cgls --no-pager /system.slice/rui-test.service

# 进程
ps -eo pid,ppid,etime,rss,args | grep -Ei 'house_ear|rui|dot2|audio' | grep -v grep
ps --ppid 24822 -o pid,ppid,args; ps --ppid 24853 -o pid,ppid,args

# 端口 / 连接
ss -ltnp
ss -tnp | grep <lan-ip>

# 健康检查（本机回环，只读 GET）
curl -s http://127.0.0.1:8095/health

# 磁盘 / 日志
du -sh ~/AI_Workspace/01_Code/projects/rui-test ~/rui-test ~/rui-bridge ~/AI_Workspace/03_Data/Models/sherpa-onnx
du -sh ~/AI_Workspace/01_Code/projects/rui-test/.venv ~/rui-test/data ~/rui-test/logs ~/rui-test/eq ~/rui-test/secrets
tail -30 ~/rui-test/logs/rui.log
```
