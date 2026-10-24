# 独立测试实例部署：honor-nas（192.168.5.34）

> 2026-10-24 部署。**与 `.168` 生产 NAS 完全隔离**：生产 rui、Mac dev 实例、Dot 的
> 当前 endpoint 都没有改；这台服务器只是多跑一个 rui 用于单独测试。
> 部署身份：Dot2 桥开发智能体 / dot2-bridge-agent。

## 服务器与路径

| 项 | 值 |
|---|---|
| 主机 | `192.168.5.34`（`horner-nas`，Debian 12 bookworm，x86_64） |
| 资源 | 8 核 / 6.7 GB / 磁盘 38 GB 空闲 |
| rui 仓库 | `/home/yes999zc/AI_Workspace/01_Code/projects/rui-test` @ `50421ee`（0.1.3） |
| 运行目录 | `/home/yes999zc/rui-test/`（`config.yaml`、`env` 0600、`data/`、`secrets/` 0700、`eq/`、`keywords.txt`、`logs/`） |
| 桥接包 | `/home/yes999zc/rui-bridge/`（`src/`、`plug.json`；`PYTHONPATH` 注入） |
| 模型 | `/home/yes999zc/AI_Workspace/03_Data/Models/sherpa-onnx/`（KWS 39 MB、SenseVoice 1.1 GB、silero_vad） |
| 服务 | `rui-test.service`（systemd，`Restart=on-failure`，已 enable） |
| 端口 | **8095** 控制台、**8870** Dot 入站（TLS）、**8872** 响度控制（仅 loopback） |

## 关键操作

```bash
# 状态 / 日志
ssh yes999zc@192.168.5.34 'systemctl status rui-test --no-pager; tail -50 ~/rui-test/logs/rui.log'

# 重启
ssh yes999zc@192.168.5.34 'sudo systemctl restart rui-test'

# 健康（loopback，无 token 也可）
curl -s http://192.168.5.34:8095/health   # 走 127.0.0.1 或带 X-Auth-Token

# API 需要 X-Auth-Token: <见 config.yaml 的 web.auth_token>
```

控制台 token 在 `~/rui-test/config.yaml` 的 `web.auth_token`（本文件不抄录）。

## 验证结果

- `systemctl is-active rui-test` → active；`/health` → `status=ok`、`audio_plane=true`、`devices=1`、`device_errors=0`
- dot2 adapter 在 8870 等待设备接入（`volume ceiling 100%`）
- `sherpa_asr: local asr ready: model.int8.onnx`；`/api/v1/asr` 用 `data/ack.wav` 转写出「在呢。」
- KWS 模型独立加载验证通过（`KeywordSpotter` 加载 39 MB 模型 + `keywords.txt`）
- TTS（讯飞）在启动时渲染了 ack + 3 条 filler，说明密钥与网络通

## 踩过的坑（复用时要记得）

1. **私库克隆**：服务器 GitHub SSH 被墙，走本地代理 `http://127.0.0.1:7890`；用 Mac 的
   `gh auth token` 以 `https://x-access-token:<token>@github.com/...` 一次性克隆，随后
   `git remote set-url origin https://github.com/yes999zc/rui.git` 去掉 token。
2. **sherpa-onnx 缺 onnxruntime 1.28.2**：PyPI 的 `onnxruntime` 最高 1.31.0，符号版本不匹配
   （`VERS_1.28.2' not found`）；wheel 的 `sherpa_onnx.libs/` 也没有它。解决办法：
   从 ORT GitHub release 下载 `onnxruntime-linux-x64-1.28.2.tgz`，把
   `libonnxruntime.so*` 复制进 `.venv/.../site-packages/sherpa_onnx/lib/`。
3. **密钥**：讯飞三件套从 Mac `~/.hermes/.env` 复制到 `~/rui-test/env`（0600）；
   `HOUSE_EAR_LLM_KEY` 复用服务器自己的 `~/.hermes/.env` 里的 `DEEPSEEK_API_KEY`。
4. **配置**：从 `dev/rui/config.yaml` 生成，替换数据/模型路径、`web.host 0.0.0.0` /
   `port 8095` / `auth_token` / `allow_anonymous: false`、`devices[].host=192.168.5.34`；
   关键词文件指向 `~/rui-test/keywords.txt`。
5. **模型**：Mac → 服务器 rsync（LAN，1.1 GB 约十几秒到一分钟）。

## Dot 已切换（2026-10-24 19:05）

`/data/local/etc/echomuse/controller.json` 现在：

```json
{"endpoints":[
  {"host":"192.168.5.34","port":8867,"tls_port":8870},
  {"host":"192.168.5.18","port":8867,"tls_port":8870},
  {"host":"192.168.5.18","port":8767,"tls_port":8770}
],"mdns":false}
```

- `endpoints[0]` = 荣耀服务器 `.34`；`[1]` = Mac dev 实例（fallback）；`[2]` = 旧 controller。
- **备份**：`/data/local/etc/echomuse/controller.json.bak-20261024-1904`（切回时 `cp` 回去再重启 server）。
- 切换方法（设备 shell 的重定向不可靠，必须 `push` + `su cp`）：

```bash
printf '%s' '<json>' > /tmp/controller.json
adb push /tmp/controller.json /data/local/tmp/controller.json
adb shell "su -c 'cp /data/local/tmp/controller.json /data/local/etc/echomuse/controller.json; chmod 644 /data/local/etc/echomuse/controller.json'"
adb shell "su -c 'kill -9 <server pid>'"   # start_server.sh 会自动拉起并重读配置
```

- 验证：设备日志 `Registered as G090LF0964340RN6`；`.34 /health` `speaker_connected=true`；
  Mac 实例 `speaker_connected=false`（它仍在跑、静音，作为 fallback）。
- 回归测试：`POST /api/v1/debug/say` 经 `.34` 播出（设备 `periods=152`），回复正常。

## 回滚

把备份 `cp` 回 `controller.json`，重启设备 server（kill 后 start_server.sh 自动拉起）；
Dot 会回到 Mac 实例。`.168` NAS 从头到尾没有参与。
