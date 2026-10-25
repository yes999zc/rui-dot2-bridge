<div align="center">

# rui-dot2-bridge

**把已解锁的 Echo Dot 2（EchoMuse 固件）变成 [rui](https://github.com/yes999zc/rui) 的语音卫星。**
不改 rui 源码：一个 out-of-tree adapter + 一个 Docker 下游镜像，设备侧 AEC 归设备。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![rui](https://img.shields.io/badge/rui-0.3.4%20→%200.5.0-blue)](https://github.com/yes999zc/rui)
[![Device](https://img.shields.io/badge/device-Echo%20Dot%202%20RS03QR-8A2BE2)](#前提条件)
[![Contract tests](https://img.shields.io/badge/plug--test-15%20PASS%20%2F%200%20FAIL-brightgreen)](docs/contract-report-0.5.0.json)

</div>

---

> **状态**：rui 0.5.0 上通过干净安装 + 真机验收（唤醒→回合、播放中打断服务端 ≤16 ms、安静房间 0 误唤醒）。
> 上游契约自测 0.3.4 / 0.4.0 / 0.4.1 / 0.4.2 / 0.5.0 均为 **15 PASS / 0 FAIL / 2 WARN / 5 SKIP**。

## 🤖 如果你想让 agent 帮你安装（推荐）

本项目从设计上就是"交给 agent 装"的：安装步骤已经写成给 AI agent 的指令。你只需要把下面这段**整段复制粘贴**
给你的 coding agent（Claude Code / Codex / DeepSeek 等，需要能执行 shell 和 Docker 命令），它会自己阅读仓库文档、
核对前提条件、逐步部署并汇报。

```text
请把 https://github.com/yes999zc/rui-dot2-bridge 部署成 rui 的语音卫星（Echo Dot 2 + EchoMuse 固件）。

1. 先完整阅读仓库里的 README.md、docs/agent-install.md、docs/clean-install-0.5.0.md，
   再开始动手；不要凭记忆安装。
2. 按 docs/agent-install.md 的检查清单核对前提条件（设备已解锁并刷好 EchoMuse、rui 0.5.0+
   已运行、Docker 宿主可用、8080/8870 端口等）；缺什么先列出来问我，不要猜。
3. 严格按文档步骤执行：构建/切换下游镜像 → 安装 plug manifest → 写 .env/config 参数 →
   POST /api/v1/setup/device 添加设备 → 跑验收清单。任何一步失败先停下来把日志发给我。
4. 安全规则：修改前备份 config/.env；不要把任何 token/证书打印到聊天或日志；只改本仓库和
   部署目录；不要碰其它服务或生产主机；不要修改 rui 源码。
5. 完成后给我：每一步的命令与结果、/health 与 /device status 关键字段、验收清单勾选，
   以及"还缺什么 / 没做到什么"。
```

更细的 agent 指令（检查清单、失败处理、验收命令）在 [docs/agent-install.md](docs/agent-install.md)。

## 它是什么

```
Echo Dot 2 (EchoMuse 固件) ──wss:8870 + X-EM-Token──▶ rui-dot2-bridge
                                                        （rui 音频面内的 out-of-tree adapter）
                                                         │ AudioFrame 16 kHz mono，ref_channels=0
                                                         ▼
                                                    rui 控制面 ──▶ ASR / LLM / TTS ──▶ 设备喇叭
```

- **不改 rui 源码**：以官方 `RUI_ADAPTER_PLUGINS` + `rui-device/v1` manifest 接入；rui 升级只需抬
  `rui_compat` 上界（当前 `>=0.3.0 <0.6.0`）。
- **设备只做 I/O 和它自己的 AEC**：`ref_channels=0`，rui 侧 AEC 关闭；adapter 负责 TLS/帧协议/音量/响度/播放收尾。
- **一切 Docker**：官方镜像 + `deploy/downstream/Dockerfile` 构建下游镜像（adapter 层），host 网络跑在 NAS 上。
- **契约优先**：每个 rui 版本都跑官方 `plug-test`，报告在 `docs/contract-report-*.json`。
- **无私有依赖**：MIT；不包含、也不分发任何 Amazon 专有固件或密钥。

## 前提条件

| 类别 | 要求 | 说明 |
|---|---|---|
| 设备 | **Echo Dot 2（第 2 代，型号 RS03QR）** + USB 线 | 只有这个型号支持本项目；解锁需要打开设备 |
| 解锁 | 用 **[firebreak](https://github.com/yes999zc/firebreak)** 把它从 Amazon 固件解锁（基于 amonet） | 有变砖风险，按 firebreak 文档操作；解锁后可回 stock |
| 固件 | **[EchoMuse](https://github.com/wilbowes/EchoMuse)**（第三方开源固件，MIT）**v2.19.0-ea.1 或更新**，装进 `server_b` 槽 | A/B 双槽 + 自动回滚，`server_a` 保留原厂固件可回退 |
| 可选补丁 | **[echomuse-dot2-ref](https://github.com/yes999zc/echomuse-dot2-ref)**：上行加 2ch（beam + 去混叠播放参考） | 只在你想让 **rui 侧**做 AEC 时需要；默认 AEC 归设备，不需要 |
| 控制器 | **[rui](https://github.com/yes999zc/rui) ≥ 0.5.0**，跑在同一局域网的 Docker 宿主（NAS / x86_64） | 需要 8080（控制台）、8870（设备 wss）、8872（响度，仅 loopback） |
| 设备凭据 | EchoMuse 的 TLS 材料（`ca.pem` / `server.pem` / `server.key`）+ `X-EM-Token` | 配对时生成；放部署目录 `dot2-secrets/`（0600，绝不进 git） |
| 网络 | 设备与 rui 宿主同一局域网，设备能访问宿主 **8870** | 设备通过 `controller.json` 静态指向宿主 |
| 宿主 | Docker Compose v2；x86_64 | 8C / 6G 级别足够，不需要 GPU |

> ⚠️ 解锁/刷机会改动设备分区，**风险自负**。请先读完 [firebreak](https://github.com/yes999zc/firebreak) 与
> [EchoMuse](https://github.com/wilbowes/EchoMuse) 的文档，并保留回滚路径（原厂槽 `server_a`）。

## 刷机流程（摘要）

完整步骤以上游项目文档为准，这里给顺序和容易踩的点：

1. **解锁**：按 [firebreak](https://github.com/yes999zc/firebreak) 把 Echo Dot 2 从 Amazon 固件切到可 root 的
   amonet 环境（USB 连接，过程中会重启几次）。
2. **装 EchoMuse 固件**：按 [EchoMuse](https://github.com/wilbowes/EchoMuse) 的设备安装说明，把 `server_b` 槽换成
   EchoMuse 的 `server` 二进制（A/B 双槽；`server_a` 保留原厂固件）。
3. **（可选）2ch 参考补丁**：如果你想让 rui 侧做 AEC，用
   [echomuse-dot2-ref](https://github.com/yes999zc/echomuse-dot2-ref) 的 `scripts/build.sh` + `scripts/install.sh`
   装到空闲槽；`scripts/rollback.sh` 秒级回滚。默认路线（设备 AEC）**不需要**这步。
4. **让设备指向 rui 宿主**：在设备上写 `controller.json`：
   `{"endpoints":[{"host":"<rui-host>","port":8867,"tls_port":8870}],"mdns":false}`。
   EchoMuse 也支持 mDNS 自动发现，静态写法更稳。
5. **回滚**：把 `server` 符号链接切回 `server_a`（原厂槽）即可；EchoMuse 的 `start_server.sh` 在连续快速崩溃时
   也会自动回滚。

## 安装（手动，Docker）

```bash
# 1. 取代码
git clone https://github.com/yes999zc/rui-dot2-bridge.git
cd rui-dot2-bridge

# 2. 在 x86_64 宿主上构建下游镜像（官方 rui + adapter 层）
DOCKER_BUILDKIT=0 docker build -f deploy/downstream/Dockerfile \
  --build-arg RUI_IMAGE=ghcr.io/yes999zc/rui:0.5.0 -t rui-dot2:0.5.0 .

# 3. 部署目录（示例 ~/rui）：compose + config + data + dot2-secrets
#    参考 deploy/downstream/docker-compose.yml；dot2-secrets/ 放部署目录（0600）

# 4. 安装 plug manifest（镜像内一份 + data/plugs 一份）
sh deploy/downstream/install-plug-manifest.sh ~/rui/data

# 5. .env 关键项（完整示例见 deploy/downstream/README.md）
#    RUI_IMAGE=rui-dot2:0.5.0
#    DOT2_SECRETS_DIR=/opt/rui-secrets
#    DOT2_EQ_DIR=/opt/rui-secrets/eq        # 从设备拉出的 EQ_*.cfg（响度曲线）
#    DOT2_VOLUME_CAP=100

# 6. 起容器
cd ~/rui && docker compose up -d

# 7. 用官方 API 添加设备（不要手搓 config.yaml 的 devices 段）
curl -sS -X POST http://127.0.0.1:8080/api/v1/setup/device \
  -H "X-Auth-Token: $(docker compose exec -T rui rui token)" \
  -H 'Content-Type: application/json' \
  -d '{"id":"dot2","adapter":"dot2","host":"<rui-host>","room":"客厅","volume":100}'
```

### 验收清单

| 项 | 判据 |
|---|---|
| 契约 | `plug-test`（命令见下）→ **0 FAIL** |
| 健康 | `/health` → `devices=1`、`audio_plane=true`、`speaker_connected=true`、`adapter_plugins.registered=["dot2"]` |
| 端口 | `8080`、`8870` 在听（`8872` 仅 loopback） |
| 设备 | 日志 `dot2 link: dot2 registered`；`/api/v1/devices/dot2/status` → `listen_port=8870` |
| 播放 | `/debug/say` 一条长回复：`playback_gap_count=0`、`max_gap=0`；长回复后无伪造 turn |
| 恢复 | `docker compose restart` 后 ≤60 s 设备重连 |

```bash
docker run --rm --entrypoint python -e PYTHONPATH=/opt/rui-plugins \
  -v "$PWD/src:/opt/rui-plugins:ro" ghcr.io/yes999zc/rui:0.5.0 \
  -m rui plug-test /opt/rui-plugins/rui_dot2_bridge \
  --adapter rui_dot2_bridge.adapter:build --json
```

## 已知限制

- **唤醒词走 rui KWS**（当前配置为中文「你好小瑞」）：设备端 OWW 运行时/模型不随固件分发，中文 OWW 需要自己训
  （[openWakeWord](https://github.com/dscripka/openWakeWord) / EchoMuse `oww_forge`），本项目暂不包含。
- **单设备 / 单音频面**：rui 0.5 的 `multi_device` 默认关闭；多卫星/多房间仲裁在 rui 后续版本。
- **AEC 归设备**（`ref_channels=0`）：想让 rui 侧做 AEC 时，用上面提到的 2ch 参考补丁。
- 播放后的麦克风静音窗口使用 rui 原生 `devices[].audio.voice.post_playback_mute_ms`（建议 2500 ms），本项目不再自带门控。
- 上游 rui 的已知小问题（如设备已连接但 status 显示 `ready=false/token_missing`）见 [rui#7](https://github.com/yes999zc/rui/issues/7)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/agent-install.md](docs/agent-install.md) | **给 agent 的安装指令**（推荐入口） |
| [deploy/downstream/](deploy/downstream/) | 下游镜像、compose、manifest 安装脚本 |
| [docs/clean-install-0.5.0.md](docs/clean-install-0.5.0.md) | 0.5.0 干净安装手册（含回滚） |
| [docs/clean-install-0.5.0-result.md](docs/clean-install-0.5.0-result.md) | 0.5.0 干净安装 + 验收记录 |
| [docs/contract-report-0.5.0.json](docs/contract-report-0.5.0.json) | 官方 plug-test 报告（同目录有 0.3.4 / 0.4.x） |
| [docs/oww-route-b-status.md](docs/oww-route-b-status.md) | 设备端唤醒（OWW）现状与路线 |
| [docs/real-machine-round-1.md](docs/real-machine-round-1.md) | 真机验收第 1 轮记录 |
| [docs/protocol-map.md](docs/protocol-map.md) | EchoMuse 设备协议 ↔ rui AudioFrame 映射 |

## 致谢

本项目站在这些项目/社区的肩膀上，真心感谢：

- **[@wilbowes/EchoMuse](https://github.com/wilbowes/EchoMuse)** — 设备侧开源固件（MIT）。没有它，Echo Dot 2 仍然是
  一台锁死的 Amazon 设备；本项目的帧协议、A/B 槽、TLS 材料约定全部沿用它的设计与文档。
- **[amonet](https://github.com/amonet/everything)** 与 **[@yes999zc/firebreak](https://github.com/yes999zc/firebreak)** —
  解锁/刷机工具链，让设备可以跑第三方固件。
- **[rui（小瑞）](https://github.com/yes999zc/rui)** — 控制器 / 音频面 / 插件契约的上游；本项目 adapter 完全按它的
  `Socket & Plug v1` 契约实现，未改其源码。
- **[openWakeWord](https://github.com/dscripka/openWakeWord)** — 未来设备端唤醒（OWW）的模型与训练工具链。
- 示例配置里用到的 **讯飞 TTS**、**DeepSeek LLM** 等第三方服务，仅作默认示例，与它们无隶属关系。

> Amazon Echo、Echo Dot 是 Amazon.com, Inc. 的商标；本项目与 Amazon 无关联，也未获其背书。
> 本项目不包含、也不分发任何 Amazon 专有固件或密钥。

## 许可证

[MIT](LICENSE)。随附的第三方组件（EchoMuse、rui 等）各自遵循其原许可证。
